"""
integration/arb_checker.py — End-to-end arbitrage opportunity checker.

Wires Week 2 DEX pricing with Week 3 CEX client and inventory tracking to
produce a full opportunity assessment without executing any trades.

Dependency interfaces
─────────────────────
pricing_engine
    Any object that exposes::

        get_dex_price(base: str, quote: str, size: Decimal) -> dict

    Returned dict must contain:
        price             Decimal  — execution price (quote per base)
        price_impact_bps  Decimal  — price impact of ``size`` in bps
        fee_bps           Decimal  — DEX swap fee in bps (e.g. 30 for Uni V2)

    The real ``pricing.engine.PricingEngine`` can be wrapped in a thin
    adapter that converts Token/int units; see ``SimplePricingAdapter`` below.

exchange_client
    ``exchange.client.ExchangeClient`` instance (or duck-type with
    ``fetch_order_book(pair, limit)`` and ``get_trading_fees(pair)``).

inventory_tracker
    ``inventory.tracker.InventoryTracker`` instance pre-loaded with
    current venue balances.

pnl_engine
    ``inventory.pnl.PnLEngine`` instance for optional trade recording.

CLI::

    python -m integration.arb_checker ETH/USDT --size 2.0
    python -m integration.arb_checker ETH/USDT --size 2.0 --dex-price 2007.21
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inventory.pnl import PnLEngine
    from inventory.tracker import InventoryTracker

_GAS_UNITS: int = 150_000

_DEFAULT_CEX_FEE_BPS = Decimal("10")

_DEFAULT_DEX_FEE_BPS = Decimal("30")


class ArbChecker:
    """
    End-to-end arbitrage check: detect → validate → check inventory.
    """

    def __init__(
        self,
        pricing_engine,
        exchange_client,
        inventory_tracker: InventoryTracker,
        pnl_engine: PnLEngine,
    ) -> None:
        self._pricing = pricing_engine
        self._cex = exchange_client
        self._inventory = inventory_tracker
        self._pnl = pnl_engine

    def check(
        self,
        pair: str,
        size: float = 1.0,
        gas_price_gwei: int = 20,
        eth_price_usd: Decimal | None = None,
    ) -> dict:
        """
        Full arb check for ``pair``.
        """
        from exchange.orderbook import OrderBookAnalyzer
        from inventory.tracker import Venue

        base, quote = pair.split("/")
        size_d = Decimal(str(size))
        ts = datetime.now(tz=UTC)

        dex_data = self._pricing.get_dex_price(base, quote, size_d)
        dex_price: Decimal = dex_data["price"]
        dex_impact_bps: Decimal = Decimal(str(dex_data.get("price_impact_bps", "0")))
        dex_fee_bps: Decimal = Decimal(str(dex_data.get("fee_bps", str(_DEFAULT_DEX_FEE_BPS))))

        raw_book = self._cex.fetch_order_book(pair)
        analyzer = OrderBookAnalyzer(raw_book)

        cex_bid: Decimal = analyzer.best_bid[0]
        cex_ask: Decimal = analyzer.best_ask[0]

        direction: str | None
        gap_bps: Decimal
        cex_slippage_bps: Decimal

        if cex_bid > 0 and dex_price < cex_bid:
            direction = "buy_dex_sell_cex"
            gap_bps = (cex_bid - dex_price) / dex_price * Decimal("10000")
            walk = analyzer.walk_the_book("sell", size)
            cex_slippage_bps = walk["slippage_bps"]
        elif cex_ask > 0 and dex_price > cex_ask:
            direction = "buy_cex_sell_dex"
            gap_bps = (dex_price - cex_ask) / cex_ask * Decimal("10000")
            walk = analyzer.walk_the_book("buy", size)
            cex_slippage_bps = walk["slippage_bps"]
        else:
            direction = None
            gap_bps = Decimal("0")
            cex_slippage_bps = Decimal("0")

        try:
            fees = self._cex.get_trading_fees(pair)
            cex_fee_bps = Decimal(str(fees.get("taker", "0.001"))) * Decimal("10000")
        except Exception:
            cex_fee_bps = _DEFAULT_CEX_FEE_BPS

        gas_eth = Decimal(_GAS_UNITS) * Decimal(gas_price_gwei) / Decimal("1000000000")
        eth_usd = eth_price_usd if eth_price_usd is not None else dex_price
        gas_cost_usd = gas_eth * eth_usd

        notional = dex_price * size_d
        gas_bps = gas_cost_usd / notional * Decimal("10000") if notional > 0 else Decimal("0")

        total_costs_bps = dex_fee_bps + dex_impact_bps + cex_fee_bps + cex_slippage_bps + gas_bps
        net_pnl_bps = gap_bps - total_costs_bps

        if direction == "buy_dex_sell_cex":
            inv = self._inventory.can_execute(
                buy_venue=Venue.WALLET,
                buy_asset=quote,
                buy_amount=notional,
                sell_venue=Venue.BINANCE,
                sell_asset=base,
                sell_amount=size_d,
            )
        elif direction == "buy_cex_sell_dex":
            inv = self._inventory.can_execute(
                buy_venue=Venue.BINANCE,
                buy_asset=quote,
                buy_amount=notional,
                sell_venue=Venue.WALLET,
                sell_asset=base,
                sell_amount=size_d,
            )
        else:
            inv = {
                "can_execute": True,
                "buy_venue_available": Decimal("0"),
                "buy_venue_needed": Decimal("0"),
                "sell_venue_available": Decimal("0"),
                "sell_venue_needed": Decimal("0"),
                "reason": None,
            }

        inventory_ok: bool = inv["can_execute"]
        executable: bool = direction is not None and net_pnl_bps > 0 and inventory_ok

        return {
            "pair": pair,
            "timestamp": ts,
            "dex_price": dex_price,
            "cex_bid": cex_bid,
            "cex_ask": cex_ask,
            "gap_bps": gap_bps,
            "direction": direction,
            "estimated_costs_bps": total_costs_bps,
            "estimated_net_pnl_bps": net_pnl_bps,
            "inventory_ok": inventory_ok,
            "executable": executable,
            "details": {
                "dex_price_impact_bps": dex_impact_bps,
                "cex_slippage_bps": cex_slippage_bps,
                "cex_fee_bps": cex_fee_bps,
                "dex_fee_bps": dex_fee_bps,
                "gas_cost_usd": gas_cost_usd,
            },
        }


class SimplePricingAdapter:
    """
    Minimal pricing_engine shim that satisfies the ArbChecker interface.
    """

    def __init__(
        self,
        price: Decimal | None = None,
        price_impact_bps: Decimal = Decimal("0"),
        fee_bps: Decimal = _DEFAULT_DEX_FEE_BPS,
        price_fn=None,
    ) -> None:
        self._price = price
        self._impact = price_impact_bps
        self._fee = fee_bps
        self._fn = price_fn

    def get_dex_price(self, base: str, quote: str, size: Decimal) -> dict:
        if self._fn is not None:
            return self._fn(base, quote, size)
        return {
            "price": self._price,
            "price_impact_bps": self._impact,
            "fee_bps": self._fee,
        }


def _print_result(result: dict, size: float) -> None:
    pair = result["pair"]
    W = 43
    sep = "═" * W

    dex_p = result["dex_price"]
    cex_bid = result["cex_bid"]
    cex_ask = result["cex_ask"]
    gap = result["gap_bps"]
    direction = result["direction"]
    d = result["details"]
    costs = result["estimated_costs_bps"]
    net = result["estimated_net_pnl_bps"]

    print(f"\n{sep}")
    print(f"  ARB CHECK: {pair} (size: {size} {pair.split('/')[0]})")
    print(sep)

    print("\nPrices:")
    print(f"  DEX (execution):  ${float(dex_p):>10,.2f}")
    print(f"  CEX best bid:     ${float(cex_bid):>10,.2f}")
    print(f"  CEX best ask:     ${float(cex_ask):>10,.2f}")

    if direction:
        print(f"\nGap: {float(gap):.1f} bps  [{direction.replace('_', ' ')}]")
    else:
        print("\nGap: 0.0 bps  [no opportunity]")

    print("\nCosts:")
    print(f"  DEX fee:           {float(d['dex_fee_bps']):>6.1f} bps")
    print(f"  DEX price impact:  {float(d['dex_price_impact_bps']):>6.1f} bps")
    print(f"  CEX fee:           {float(d['cex_fee_bps']):>6.1f} bps")
    print(f"  CEX slippage:      {float(d['cex_slippage_bps']):>6.1f} bps")
    print(f"  Gas:               ${float(d['gas_cost_usd']):.2f}")
    print(f"  {'─' * 30}")
    print(f"  Total costs:       {float(costs):>6.1f} bps")

    verdict_sign = "+" if net >= 0 else ""
    ok_flag = "✅ PROFITABLE" if net > 0 else "❌ NOT PROFITABLE"
    print(f"\nNet PnL estimate: {verdict_sign}{float(net):.1f} bps  {ok_flag}")

    print("\nInventory:")
    inv_buy_ok = "✅" if result["inventory_ok"] else "❌ INSUFFICIENT"
    print(f"  Pre-flight check:  {inv_buy_ok}")

    exec_verdict = (
        "EXECUTE"
        if result["executable"]
        else (
            "SKIP — costs exceed gap"
            if direction and not (net > 0)
            else "SKIP — no opportunity"
            if not direction
            else "SKIP — insufficient inventory"
        )
    )
    print(f"\nVerdict: {exec_verdict}")
    print(sep + "\n")


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arb checker — connects to Binance testnet for CEX data",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m integration.arb_checker ETH/USDT --size 2.0\n"
            "  python -m integration.arb_checker ETH/USDT --size 2.0 --dex-price 2007.21"
        ),
    )
    parser.add_argument("pair", help="Trading pair, e.g. ETH/USDT")
    parser.add_argument("--size", type=float, default=1.0, help="Trade size in base units")
    parser.add_argument("--dex-price", type=float, default=None, help="Override DEX price (USD)")
    parser.add_argument("--gas-gwei", type=int, default=20, help="Gas price in gwei (default 20)")
    parser.add_argument("--depth", type=int, default=20, help="CEX order book depth")
    args = parser.parse_args(argv)

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(_root, ".env"))
    except ImportError:
        pass

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    try:
        from exchange.client import ExchangeClient

        cex_client = ExchangeClient(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "sandbox": True,
                "enableRateLimit": True,
            }
        )
    except Exception as exc:
        print(f"Error connecting to exchange: {exc}", file=sys.stderr)
        return 1

    try:
        raw_book = cex_client.fetch_order_book(args.pair, limit=args.depth)
        from exchange.orderbook import OrderBookAnalyzer

        analyzer = OrderBookAnalyzer(raw_book)
        mid = float(analyzer.mid_price)
    except Exception as exc:
        print(f"Error fetching order book: {exc}", file=sys.stderr)
        return 1

    dex_price_val = (
        Decimal(str(args.dex_price))
        if args.dex_price is not None
        else Decimal(str(mid)) * Decimal("0.998")
    )

    pricing = SimplePricingAdapter(
        price=dex_price_val,
        price_impact_bps=Decimal("1.2"),
        fee_bps=Decimal("30"),
    )

    from inventory.pnl import PnLEngine
    from inventory.tracker import InventoryTracker, Venue

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    base = args.pair.split("/")[0]
    quote = args.pair.split("/")[1]
    tracker.update_from_cex(
        Venue.BINANCE,
        {
            base: {"free": "100", "locked": "0"},
            quote: {"free": "500000", "locked": "0"},
        },
    )
    tracker.update_from_wallet(Venue.WALLET, {base: "100", quote: "500000"})

    checker = ArbChecker(
        pricing_engine=pricing,
        exchange_client=cex_client,
        inventory_tracker=tracker,
        pnl_engine=PnLEngine(),
    )

    try:
        result = checker.check(args.pair, size=args.size, gas_price_gwei=args.gas_gwei)
    except Exception as exc:
        print(f"Error running arb check: {exc}", file=sys.stderr)
        return 1

    _print_result(result, args.size)
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
