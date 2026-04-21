"""
strategy/generator.py — Signal generation from live CEX + DEX prices.

SignalGenerator wraps ExchangeClient (Week 3) and optionally a PricingEngine
(Week 2) to detect arbitrage opportunities, validate them against inventory,
and emit Signal objects ready for the executor.

If no pricing module is provided the generator falls back to a price stub
(DEX = CEX mid ± small offset) which is useful for unit tests and demos
where a live DEX connection is unavailable.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from strategy.fees import FeeStructure
from strategy.signal import Direction, Signal

if TYPE_CHECKING:
    from inventory.tracker import InventoryTracker

log = logging.getLogger(__name__)

_INV_BUFFER = 1.01


class SignalGenerator:
    """
    Detect and validate arbitrage opportunities between a CEX and a DEX.
    """

    def __init__(
        self,
        exchange_client,
        pricing_module,
        inventory_tracker: InventoryTracker,
        fee_structure: FeeStructure,
        config: dict | None = None,
    ) -> None:
        cfg = config or {}
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.fees = fee_structure

        self.min_spread_bps: float = cfg.get("min_spread_bps", 50.0)
        self.min_profit_usd: float = cfg.get("min_profit_usd", 5.0)
        self.max_position_usd: float = cfg.get("max_position_usd", 10_000.0)
        self.signal_ttl: float = cfg.get("signal_ttl_seconds", 5.0)
        self.cooldown: float = cfg.get("cooldown_seconds", 2.0)

        self._last_signal_time: dict[str, float] = {}

    def generate(self, pair: str, size: float) -> Signal | None:
        """
        Attempt to generate a signal for the given pair and trade size.
        """
        if self._in_cooldown(pair):
            log.debug("Cooldown active for %s, skipping", pair)
            return None

        prices = self._fetch_prices(pair, size)
        if prices is None:
            return None

        cex_bid = prices["cex_bid"]
        cex_ask = prices["cex_ask"]
        dex_buy = prices["dex_buy"]
        dex_sell = prices["dex_sell"]

        spread_a = (dex_sell - cex_ask) / cex_ask * 10_000 if cex_ask > 0 else 0.0
        spread_b = (cex_bid - dex_buy) / dex_buy * 10_000 if dex_buy > 0 else 0.0

        if spread_a >= spread_b and spread_a >= self.min_spread_bps:
            direction = Direction.BUY_CEX_SELL_DEX
            spread = spread_a
            cex_price = cex_ask
            dex_price = dex_sell
        elif spread_b >= self.min_spread_bps:
            direction = Direction.BUY_DEX_SELL_CEX
            spread = spread_b
            cex_price = cex_bid
            dex_price = dex_buy
        else:
            log.debug(
                "%s no opportunity: spread_a=%.1f bps spread_b=%.1f bps",
                pair,
                spread_a,
                spread_b,
            )
            return None

        trade_value = size * cex_price
        gross_pnl = (spread / 10_000) * trade_value
        fees_usd = self.fees.fee_usd(trade_value)
        net_pnl = gross_pnl - fees_usd

        if net_pnl < self.min_profit_usd:
            log.debug(
                "%s net_pnl=%.2f below min_profit_usd=%.2f, skipping",
                pair,
                net_pnl,
                self.min_profit_usd,
            )
            return None

        inventory_ok = self._check_inventory(pair, direction, size, cex_price)
        within_limits = trade_value <= self.max_position_usd

        signal = Signal.create(
            pair=pair,
            direction=direction,
            cex_price=cex_price,
            dex_price=dex_price,
            spread_bps=spread,
            size=size,
            expected_gross_pnl=gross_pnl,
            expected_fees=fees_usd,
            expected_net_pnl=net_pnl,
            score=0.0,
            expiry=time.time() + self.signal_ttl,
            inventory_ok=inventory_ok,
            within_limits=within_limits,
        )

        self._last_signal_time[pair] = time.time()
        log.info("Generated signal: %s", signal)
        return signal

    def _in_cooldown(self, pair: str) -> bool:
        return time.time() - self._last_signal_time.get(pair, 0.0) < self.cooldown

    def _fetch_prices(self, pair: str, size: float) -> dict | None:
        """
        Fetch CEX best bid/ask and DEX effective prices for ``size``.
        """
        try:
            ob = self.exchange.fetch_order_book(pair)

            best_bid = ob.get("best_bid")
            best_ask = ob.get("best_ask")

            if best_bid is None or best_ask is None:
                return None

            cex_bid = float(best_bid[0])
            cex_ask = float(best_ask[0])

            if cex_bid <= 0 or cex_ask <= 0:
                return None

            if self.pricing is not None:
                dex_buy, dex_sell = self._dex_prices_from_engine(pair, size)
            else:
                log.debug("No pricing module — using DEX price stub for %s", pair)
                mid = (cex_bid + cex_ask) / 2.0
                dex_buy = mid * 1.005
                dex_sell = mid * 1.008

            return {
                "cex_bid": cex_bid,
                "cex_ask": cex_ask,
                "dex_buy": dex_buy,
                "dex_sell": dex_sell,
            }

        except Exception as exc:
            log.warning("_fetch_prices failed for %s: %s", pair, exc)
            return None

    def _dex_prices_from_engine(self, pair: str, size: float) -> tuple[float, float]:
        """
        Query the Week 2 PricingEngine for effective DEX buy and sell prices.
        """
        base, quote = pair.split("/")
        token_in = self._get_token(base)
        token_out = self._get_token(quote)

        buy_quote = self.pricing.get_quote(
            token_out,
            token_in,
            int(size * 10**token_out.decimals),
        )
        dex_buy = float(buy_quote.expected_output) / (size * 10**token_in.decimals)

        sell_quote = self.pricing.get_quote(
            token_in,
            token_out,
            int(size * 10**token_in.decimals),
            1,
        )
        dex_sell = float(sell_quote.expected_output) / (size * 10**token_out.decimals)

        return dex_buy, dex_sell

    def _get_token(self, symbol: str):
        """
        Resolve a token symbol to a Token object via the pricing module.
        """
        if hasattr(self.pricing, "get_token"):
            return self.pricing.get_token(symbol)
        raise NotImplementedError(
            f"Cannot resolve token '{symbol}': pricing module has no get_token() method. "
            "Override _get_token() in a subclass or use pricing=None with the DEX stub."
        )

    def _check_inventory(self, pair: str, direction: Direction, size: float, price: float) -> bool:
        """
        Verify free balances are sufficient for both legs of the trade.
        """
        from inventory.tracker import Venue

        base, quote = pair.split("/")
        needed_quote = size * price * _INV_BUFFER

        if direction == Direction.BUY_CEX_SELL_DEX:
            cex_quote = float(self.inventory.get_available(Venue.BINANCE, quote))
            dex_base = float(self.inventory.get_available(Venue.WALLET, base))
            ok = cex_quote >= needed_quote and dex_base >= size
        else:
            dex_quote = float(self.inventory.get_available(Venue.WALLET, quote))
            cex_base = float(self.inventory.get_available(Venue.BINANCE, base))
            ok = dex_quote >= needed_quote and cex_base >= size

        if not ok:
            log.debug("Inventory check failed for %s %s", pair, direction.value)
        return ok
