"""
config/settings.py — Environment-aware configuration for the arb bot.

Controls:
  - PRODUCTION toggle (testnet vs mainnet Binance)
  - Arbitrum vs Ethereum network selection
  - Default fee structure for each environment
  - Pair-specific Binance trading rule enforcement

Usage:
    from config.settings import Config, TradingRules, get_trading_rules

    cfg = Config()
    print(cfg.binance_base_url)   # "https://testnet.binance.vision" or prod URL
    rules = get_trading_rules("ETH/USDC", exchange_client)
    safe_qty = rules.round_quantity(0.123456789)
"""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Main config
# ---------------------------------------------------------------------------


class Config:
    """
    Single source of truth for environment-specific constants.

    Set PRODUCTION=true in .env to switch to real Binance.
    All other settings default to testnet / safe values.
    """

    PRODUCTION: bool = os.getenv("PRODUCTION", "false").lower() == "true"

    # --- Binance ---
    if PRODUCTION:
        BINANCE_BASE_URL = "https://api.binance.com"
        BINANCE_WS_URL = "wss://stream.binance.com:9443/ws"
        CEX_FEE_BPS = 10.0  # real Binance taker 0.10%
    else:
        BINANCE_BASE_URL = "https://testnet.binance.vision"
        BINANCE_WS_URL = "wss://testnet.binance.vision/ws"
        CEX_FEE_BPS = 10.0  # testnet charges the same fee structure

    # --- Arbitrum One ---
    ARBITRUM_RPC = os.getenv("ARB_RPC_URL", "https://arb1.arbitrum.io/rpc")
    ARBITRUM_CHAIN_ID = 42161
    ARBITRUM_ROUTER = "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24"
    ARBITRUM_FACTORY = "0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9"

    # --- Token addresses (Arbitrum One) ---
    WETH_ADDRESS = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    USDC_ADDRESS = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"  # native USDC
    USDT_ADDRESS = "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"

    # --- Trading defaults ---
    PAIR = "ETH/USDC"
    TRADE_SIZE_ETH = 0.05  # start small in prod
    SCORE_THRESHOLD = 70.0  # only high-confidence signals

    # --- Fee structure for Arbitrum (gas is cheap) ---
    CEX_TAKER_BPS = 10.0  # Binance 0.1%
    DEX_SWAP_BPS = 30.0  # Uniswap V2 0.3%
    GAS_COST_USD = 0.10  # Arbitrum: $0.05 – $0.20 per swap

    @classmethod
    def to_fee_structure(cls):
        """Return a FeeStructure configured for the current environment."""
        from strategy.fees import FeeStructure

        return FeeStructure(
            cex_taker_bps=cls.CEX_TAKER_BPS,
            dex_swap_bps=cls.DEX_SWAP_BPS,
            gas_cost_usd=cls.GAS_COST_USD,
        )

    @classmethod
    def to_signal_config(cls) -> dict:
        """Return a signal config dict with production-appropriate thresholds."""
        return {
            "min_spread_bps": 40.0,  # must clear fees (~40 bps on Arbitrum)
            "min_profit_usd": 1.0,  # at least $1 net after all costs
            "cooldown_seconds": 2.0,
            "signal_ttl_seconds": 5.0,
            "max_position_usd": 500.0,  # hard cap until proven stable
        }


# ---------------------------------------------------------------------------
# Binance trading rules
# ---------------------------------------------------------------------------


@dataclass
class TradingRules:
    """
    Pair-specific Binance exchange filters.

    Binance rejects orders that violate these — they must be applied before
    every CEX order submission.  Values are fetched once from /api/v3/exchangeInfo
    and cached; the exchange does not change them frequently.

    Key filters for ETH/USDC on Binance:
      minNotional = 5.0 USDC   — order must be worth at least this
      stepSize    = 0.0001 ETH — quantity must be a multiple of this
      tickSize    = 0.01 USDC  — price must be a multiple of this
    """

    symbol: str
    min_qty: float = 0.0001
    max_qty: float = 9000.0
    step_size: float = 0.0001  # LOT_SIZE filter
    min_price: float = 0.01
    max_price: float = 1_000_000.0
    tick_size: float = 0.01  # PRICE_FILTER
    min_notional: float = 5.0  # MIN_NOTIONAL filter

    def round_quantity(self, qty: float) -> float:
        """Floor qty to the nearest lot step — Binance rejects non-multiples."""
        if self.step_size <= 0:
            return qty
        precision = max(0, -int(math.floor(math.log10(self.step_size))))
        floored = math.floor(qty / self.step_size) * self.step_size
        return round(floored, precision)

    def round_price(self, price: float) -> float:
        """Round price to the nearest tick."""
        if self.tick_size <= 0:
            return price
        precision = max(0, -int(math.floor(math.log10(self.tick_size))))
        rounded = round(price / self.tick_size) * self.tick_size
        return round(rounded, precision)

    def validate(self, qty: float, price: float) -> tuple[bool, str]:
        """
        Check that a quantity+price pair passes all Binance filters.
        Returns (ok, reason_if_failed).
        """
        qty = self.round_quantity(qty)
        price = self.round_price(price)

        if qty < self.min_qty:
            return False, f"qty {qty} < min_qty {self.min_qty}"
        if qty > self.max_qty:
            return False, f"qty {qty} > max_qty {self.max_qty}"
        if price < self.min_price:
            return False, f"price {price} < min_price {self.min_price}"
        if price > self.max_price:
            return False, f"price {price} > max_price {self.max_price}"
        notional = qty * price
        if notional < self.min_notional:
            return False, f"notional {notional:.2f} < min_notional {self.min_notional}"
        return True, ""


# ---------------------------------------------------------------------------
# Live rule fetching
# ---------------------------------------------------------------------------

_rules_cache: dict[str, TradingRules] = {}

# Hardcoded fallbacks so the bot works even without a live exchangeInfo call.
_FALLBACK_RULES: dict[str, TradingRules] = {
    "ETH/USDC": TradingRules(
        symbol="ETH/USDC",
        min_qty=0.0001,
        max_qty=9000.0,
        step_size=0.0001,
        min_price=0.01,
        max_price=1_000_000.0,
        tick_size=0.01,
        min_notional=5.0,
    ),
    "ETH/USDT": TradingRules(
        symbol="ETH/USDT",
        min_qty=0.0001,
        max_qty=9000.0,
        step_size=0.0001,
        min_price=0.01,
        max_price=1_000_000.0,
        tick_size=0.01,
        min_notional=5.0,
    ),
    "BTC/USDT": TradingRules(
        symbol="BTC/USDT",
        min_qty=0.00001,
        max_qty=900.0,
        step_size=0.00001,
        min_price=0.01,
        max_price=1_000_000.0,
        tick_size=0.01,
        min_notional=5.0,
    ),
    # ARB — Arbitrum governance token (~$0.50), Binance lot size 0.1 ARB
    "ARB/USDC": TradingRules(
        symbol="ARB/USDC",
        min_qty=0.1,
        max_qty=9_000_000.0,
        step_size=0.1,
        min_price=0.0001,
        max_price=10_000.0,
        tick_size=0.0001,
        min_notional=5.0,
    ),
    "ARB/USDT": TradingRules(
        symbol="ARB/USDT",
        min_qty=0.1,
        max_qty=9_000_000.0,
        step_size=0.1,
        min_price=0.0001,
        max_price=10_000.0,
        tick_size=0.0001,
        min_notional=5.0,
    ),
    # MAGIC — TreasureDAO gaming token (~$0.30), Arbitrum-native
    "MAGIC/USDT": TradingRules(
        symbol="MAGIC/USDT",
        min_qty=0.1,
        max_qty=9_000_000.0,
        step_size=0.1,
        min_price=0.0001,
        max_price=10_000.0,
        tick_size=0.0001,
        min_notional=5.0,
    ),
    # PENDLE — yield trading protocol (~$3), Arbitrum-native
    "PENDLE/USDT": TradingRules(
        symbol="PENDLE/USDT",
        min_qty=0.1,
        max_qty=9_000_000.0,
        step_size=0.1,
        min_price=0.001,
        max_price=10_000.0,
        tick_size=0.001,
        min_notional=5.0,
    ),
}


def get_trading_rules(symbol: str, exchange_client=None) -> TradingRules:
    """
    Fetch or return cached Binance trading rules for a symbol.

    If exchange_client is provided, queries /api/v3/exchangeInfo for live
    filters.  Falls back to hardcoded defaults if the call fails or no
    client is available — hardcoded values are conservative and safe.
    """
    if symbol in _rules_cache:
        return _rules_cache[symbol]

    if exchange_client is not None:
        try:
            rules = _fetch_from_exchange(symbol, exchange_client)
            _rules_cache[symbol] = rules
            return rules
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning(
                "Could not fetch trading rules for %s: %s — using fallback", symbol, exc
            )

    fallback = _FALLBACK_RULES.get(symbol, TradingRules(symbol=symbol))
    _rules_cache[symbol] = fallback
    return fallback


def _fetch_from_exchange(symbol: str, exchange_client) -> TradingRules:
    """
    Parse Binance exchangeInfo filters into a TradingRules object.
    Calls ccxt's markets property which caches the exchange info.
    """
    # ccxt loads markets lazily on first access
    markets = exchange_client._exchange.load_markets()
    ccxt_symbol = symbol.replace("/", "")  # ETH/USDC → ETHUSDC for lookup
    market = None
    for s, m in markets.items():
        if s == symbol or m.get("id") == ccxt_symbol:
            market = m
            break

    if market is None:
        raise ValueError(f"Symbol {symbol} not found in exchange markets")

    limits = market.get("limits", {})
    precision = market.get("precision", {})

    # ccxt normalises lot size into precision.amount (decimal places)
    # and price tick into precision.price
    amount_step = 10 ** (-precision.get("amount", 4))
    price_tick = 10 ** (-precision.get("price", 2))

    return TradingRules(
        symbol=symbol,
        min_qty=float(limits.get("amount", {}).get("min", 0.0001)),
        max_qty=float(limits.get("amount", {}).get("max", 9000.0)),
        step_size=amount_step,
        min_price=float(limits.get("price", {}).get("min", 0.01)),
        max_price=float(limits.get("price", {}).get("max", 1_000_000.0)),
        tick_size=price_tick,
        min_notional=float(limits.get("cost", {}).get("min", 5.0)),
    )
