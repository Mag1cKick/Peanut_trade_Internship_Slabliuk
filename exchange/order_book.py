"""
exchange/order_book.py — Order book depth analysis and VWAP estimation.

Takes the normalised output of ExchangeClient.fetch_order_book() and provides:
  - VWAP estimation for a given fill size (market-impact aware)
  - Book imbalance ratio (pressure indicator)
  - Depth within a basis-point band around mid price
  - Liquidity walls (large resting orders)
  - Cumulative depth levels with running qty / value
"""

from __future__ import annotations

from decimal import Decimal
from typing import NamedTuple


class DepthLevel(NamedTuple):
    """One price level with running cumulative totals."""

    price: Decimal
    qty: Decimal
    cumulative_qty: Decimal
    cumulative_value: Decimal


class OrderBookAnalyzer:
    """
    Analyses a normalised order book snapshot.
    """

    def __init__(self, order_book: dict) -> None:
        self._bids: list[tuple[Decimal, Decimal]] = order_book["bids"]
        self._asks: list[tuple[Decimal, Decimal]] = order_book["asks"]
        self._mid: Decimal = order_book["mid_price"]
        self._symbol: str = order_book.get("symbol", "")

    def vwap_to_fill(self, side: str, size: Decimal) -> Decimal | None:
        """
        Compute the VWAP fill price to execute ``size`` quantity.
        """
        if size <= 0:
            raise ValueError(f"size must be positive, got {size}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        levels = self._asks if side == "buy" else self._bids
        remaining = size
        total_value = Decimal("0")

        for price, qty in levels:
            take = min(remaining, qty)
            total_value += price * take
            remaining -= take
            if remaining <= 0:
                break

        if remaining > 0:
            return None

        return total_value / size

    def book_imbalance(self, depth: int = 10) -> Decimal:
        """
        Bid/ask imbalance in the range ``[-1, +1]``.
        """
        bid_qty = sum((q for _, q in self._bids[:depth]), Decimal("0"))
        ask_qty = sum((q for _, q in self._asks[:depth]), Decimal("0"))
        total = bid_qty + ask_qty
        if total == 0:
            return Decimal("0")
        return (bid_qty - ask_qty) / total

    def depth_at_bps(self, bps: Decimal) -> dict:
        """
        Return cumulative bid and ask quantity/value within ``bps`` basis
        points of the mid price.
        """
        zero = Decimal("0")
        if self._mid == 0:
            return {"bid_qty": zero, "ask_qty": zero, "bid_value": zero, "ask_value": zero}

        factor = bps / Decimal("10000")
        bid_threshold = self._mid * (1 - factor)
        ask_threshold = self._mid * (1 + factor)

        bid_qty = sum((q for p, q in self._bids if p >= bid_threshold), zero)
        ask_qty = sum((q for p, q in self._asks if p <= ask_threshold), zero)
        bid_value = sum((p * q for p, q in self._bids if p >= bid_threshold), zero)
        ask_value = sum((p * q for p, q in self._asks if p <= ask_threshold), zero)

        return {
            "bid_qty": bid_qty,
            "ask_qty": ask_qty,
            "bid_value": bid_value,
            "ask_value": ask_value,
        }

    def liquidity_walls(self, min_qty: Decimal) -> dict:
        """
        Find individual price levels where resting quantity >= ``min_qty``.

        Large resting orders act as support (bids) or resistance (asks).

        Returns::

            {
                'bid_walls': [(price, qty), ...],   # sorted best-first
                'ask_walls': [(price, qty), ...],
            }
        """
        bid_walls = [(p, q) for p, q in self._bids if q >= min_qty]
        ask_walls = [(p, q) for p, q in self._asks if q >= min_qty]
        return {"bid_walls": bid_walls, "ask_walls": ask_walls}

    def depth_levels(self, side: str, n: int = 5) -> list[DepthLevel]:
        """
        Return the top ``n`` levels of ``side`` with running cumulative
        quantity and notional value.

        ``side`` is ``"bid"`` or ``"ask"``.
        """
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")

        levels = self._asks if side == "ask" else self._bids
        result: list[DepthLevel] = []
        cum_qty = Decimal("0")
        cum_val = Decimal("0")

        for price, qty in levels[:n]:
            cum_qty += qty
            cum_val += price * qty
            result.append(
                DepthLevel(
                    price=price,
                    qty=qty,
                    cumulative_qty=cum_qty,
                    cumulative_value=cum_val,
                )
            )

        return result

    def spread(self) -> Decimal:
        """Return absolute spread (best_ask - best_bid), or 0 if book is empty."""
        if not self._bids or not self._asks:
            return Decimal("0")
        return self._asks[0][0] - self._bids[0][0]

    def mid_price(self) -> Decimal:
        """Return the mid price passed in at construction."""
        return self._mid
