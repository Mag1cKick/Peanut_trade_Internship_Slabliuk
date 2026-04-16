"""
exchange/client.py — Unified CEX interface via ccxt (Binance testnet).

Wraps ccxt's Binance exchange with:
  - Normalised return types (Decimal, not float)
  - Rate-limit tracking (request weight budget)
  - Structured logging for every API call
  - Connection health-check on construction

Binance testnet: https://testnet.binance.vision/
"""

from __future__ import annotations

import logging
import time
from decimal import Decimal, InvalidOperation
from typing import Any

log = logging.getLogger(__name__)

_WEIGHT_LIMIT = 1200
_WEIGHT_SAFETY = int(_WEIGHT_LIMIT * 0.9)
_ENDPOINT_WEIGHTS: dict[str, int] = {
    "fetch_order_book": 5,
    "fetch_balance": 10,
    "create_order": 1,
    "cancel_order": 1,
    "fetch_order": 2,
    "fetch_trading_fee": 20,
    "fetch_time": 1,
    "fetch_status": 1,
}


def _to_decimal(value: Any) -> Decimal:
    """Convert any numeric value to Decimal safely."""
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


class ExchangeClient:
    """
    Wrapper around ccxt for Binance testnet.
    """

    def __init__(self, config: dict) -> None:
        try:
            import ccxt
        except ImportError as exc:
            raise ImportError(
                "ccxt is required for ExchangeClient. Install with: pip install ccxt"
            ) from exc

        self._exchange = ccxt.binance(config)
        self._weight_used: int = 0
        self._weight_reset_at: float = time.monotonic() + 60.0

        self._call("fetch_time")
        log.info(
            "ExchangeClient connected to %s (sandbox=%s)",
            self._exchange.id,
            config.get("sandbox", False),
        )

    def fetch_order_book(self, symbol: str, limit: int = 20) -> dict:
        """
        Fetch L2 order book snapshot.
        """
        raw = self._call("fetch_order_book", symbol, limit)

        bids = [(Decimal(str(p)), Decimal(str(q))) for p, q in raw["bids"]]
        asks = [(Decimal(str(p)), Decimal(str(q))) for p, q in raw["asks"]]

        bids = sorted(bids, key=lambda x: x[0], reverse=True)
        asks = sorted(asks, key=lambda x: x[0])

        best_bid = bids[0] if bids else (Decimal("0"), Decimal("0"))
        best_ask = asks[0] if asks else (Decimal("0"), Decimal("0"))

        mid = (
            (best_bid[0] + best_ask[0]) / Decimal("2")
            if best_bid[0] and best_ask[0]
            else Decimal("0")
        )
        spread_bps = (
            (best_ask[0] - best_bid[0]) / mid * Decimal("10000") if mid > 0 else Decimal("0")
        )

        return {
            "symbol": symbol,
            "timestamp": raw.get("timestamp") or int(time.time() * 1000),
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid,
            "spread_bps": spread_bps,
        }

    def fetch_balance(self) -> dict[str, dict]:
        """
        Fetch account balances, filtering out zero-balance assets.
        """
        raw = self._call("fetch_balance")
        result: dict[str, dict] = {}

        for asset, info in raw.items():
            if not isinstance(info, dict):
                continue
            free = _to_decimal(info.get("free", 0))
            locked = _to_decimal(info.get("used", 0))
            total = _to_decimal(info.get("total", 0))
            if total == 0:
                continue
            result[asset] = {"free": free, "locked": locked, "total": total}

        log.debug("fetch_balance: %d non-zero assets", len(result))
        return result

    def create_limit_ioc_order(
        self,
        symbol: str,
        side: str,
        amount: float,
        price: float,
    ) -> dict:
        """
        Place a LIMIT IOC (Immediate Or Cancel) order.
        """
        raw = self._call(
            "create_order",
            symbol,
            "limit",
            side,
            amount,
            price,
            {"timeInForce": "IOC"},
        )
        return self._normalise_order(raw)

    def create_market_order(self, symbol: str, side: str, amount: float) -> dict:
        """
        Place a market order. Same return format as create_limit_ioc_order.
        """
        raw = self._call("create_order", symbol, "market", side, amount)
        return self._normalise_order(raw)

    def cancel_order(self, order_id: str, symbol: str) -> dict:
        """Cancel an open order. Returns order status after cancellation."""
        raw = self._call("cancel_order", order_id, symbol)
        return self._normalise_order(raw)

    def fetch_order_status(self, order_id: str, symbol: str) -> dict:
        """Fetch current status of an order."""
        raw = self._call("fetch_order", order_id, symbol)
        return self._normalise_order(raw)

    def get_trading_fees(self, symbol: str) -> dict:
        """
        Returns fee structure.
        """
        raw = self._call("fetch_trading_fee", symbol)
        return {
            "maker": _to_decimal(raw.get("maker", "0.001")),
            "taker": _to_decimal(raw.get("taker", "0.001")),
        }

    def _call(self, method: str, *args, **kwargs) -> Any:
        """
        Call a ccxt method with rate-limit tracking and logging.

        Raises:
            ccxt.NetworkError: On connection failure.
            ccxt.AuthenticationError: On bad API key/secret.
            ccxt.RateLimitExceeded: When exchange rate limit is hit.
            ccxt.BaseError: For all other ccxt errors.
        """
        import ccxt

        self._consume_weight(method)

        log.debug("→ %s %s", method, args[:2] if args else "")
        t0 = time.monotonic()

        try:
            fn = getattr(self._exchange, method)
            result = fn(*args, **kwargs)
            elapsed = time.monotonic() - t0
            log.debug("← %s OK (%.3fs)", method, elapsed)
            return result

        except ccxt.RateLimitExceeded:
            log.warning("Rate limit exceeded on %s — sleeping 60s", method)
            time.sleep(60)
            raise

        except ccxt.NetworkError as exc:
            log.error("Network error on %s: %s", method, exc)
            raise

        except ccxt.AuthenticationError as exc:
            log.error("Auth error on %s: %s", method, exc)
            raise

        except ccxt.BaseError as exc:
            log.error("Exchange error on %s: %s", method, exc)
            raise

    def _consume_weight(self, method: str) -> None:
        """
        Track request weight and sleep if approaching the limit.
        """
        now = time.monotonic()
        if now >= self._weight_reset_at:
            self._weight_used = 0
            self._weight_reset_at = now + 60.0

        weight = _ENDPOINT_WEIGHTS.get(method, 1)
        if self._weight_used + weight >= _WEIGHT_SAFETY:
            sleep_for = self._weight_reset_at - now
            if sleep_for > 0:
                log.warning(
                    "Rate limit budget at %d/%d — sleeping %.1fs",
                    self._weight_used,
                    _WEIGHT_SAFETY,
                    sleep_for,
                )
                time.sleep(sleep_for)
            self._weight_used = 0
            self._weight_reset_at = time.monotonic() + 60.0

        self._weight_used += weight

    def _normalise_order(self, raw: dict) -> dict:
        """Convert a raw ccxt order dict to normalised form with Decimal values."""
        filled = _to_decimal(raw.get("filled", 0))
        amount = _to_decimal(raw.get("amount", 0))
        avg_price = _to_decimal(raw.get("average") or raw.get("price") or 0)

        fee_info = raw.get("fee") or {}
        fee_cost = _to_decimal(fee_info.get("cost", 0))
        fee_asset = fee_info.get("currency", "")

        raw_status = (raw.get("status") or "").lower()
        if raw_status == "closed" and filled >= amount:
            status = "filled"
        elif raw_status == "closed" and filled < amount:
            status = "partially_filled"
        elif raw_status in ("canceled", "cancelled", "expired"):
            status = "expired"
        else:
            status = raw_status or "unknown"

        return {
            "id": str(raw.get("id", "")),
            "symbol": raw.get("symbol", ""),
            "side": raw.get("side", ""),
            "type": raw.get("type", ""),
            "time_in_force": (raw.get("timeInForce") or raw.get("info", {}).get("timeInForce", "")),
            "amount_requested": amount,
            "amount_filled": filled,
            "avg_fill_price": avg_price,
            "fee": fee_cost,
            "fee_asset": fee_asset,
            "status": status,
            "timestamp": raw.get("timestamp") or int(time.time() * 1000),
        }
