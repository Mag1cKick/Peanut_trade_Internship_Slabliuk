"""
inventory/tracker.py — Per-asset position tracking with weighted-average cost basis.

Tracks open positions and realized P&L:
  - Buys increase quantity and update avg_cost (weighted average)
  - Sells reduce quantity and book realized P&L against avg_cost
  - Fees are tracked separately
  - Full trade history is preserved for audit / replay
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

TradeType = Literal["buy", "sell"]


@dataclass
class Trade:
    """One completed fill."""

    asset: str
    side: TradeType
    qty: Decimal
    price: Decimal
    fee: Decimal
    timestamp: int


@dataclass
class Position:
    """Current state of a single-asset position."""

    asset: str
    qty: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    total_fees: Decimal = field(default_factory=lambda: Decimal("0"))


class InventoryTracker:
    """
    Tracks open positions and realized P&L using weighted-average cost basis.
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []

    def record_fill(
        self,
        asset: str,
        side: TradeType,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
        timestamp: int | None = None,
    ) -> None:
        """
        Record a completed fill.
        """
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")
        if fee < 0:
            raise ValueError(f"fee must be non-negative, got {fee}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        ts = timestamp if timestamp is not None else int(time.time() * 1000)
        self._trades.append(
            Trade(asset=asset, side=side, qty=qty, price=price, fee=fee, timestamp=ts)
        )

        pos = self._positions.setdefault(asset, Position(asset=asset))
        pos.total_fees += fee

        if side == "buy":
            total_value = pos.avg_cost * pos.qty + price * qty
            pos.qty += qty
            pos.avg_cost = total_value / pos.qty
        else:
            sell_qty = min(qty, pos.qty)
            if sell_qty > 0:
                realized = (price - pos.avg_cost) * sell_qty - fee
                pos.realized_pnl += realized
                pos.qty -= sell_qty
                if pos.qty == 0:
                    pos.avg_cost = Decimal("0")

    def get_position(self, asset: str) -> Position:
        """Return the current position for ``asset`` (zero-qty if never traded)."""
        return self._positions.get(asset, Position(asset=asset))

    def all_positions(self) -> dict[str, Position]:
        """Return all positions that currently have non-zero quantity."""
        return {a: p for a, p in self._positions.items() if p.qty > 0}

    def unrealized_pnl(self, asset: str, mark_price: Decimal) -> Decimal:
        """
        Unrealized P&L for ``asset`` at ``mark_price``.
        """
        pos = self.get_position(asset)
        if pos.qty == 0:
            return Decimal("0")
        return (mark_price - pos.avg_cost) * pos.qty

    def total_exposure(self, prices: dict[str, Decimal]) -> Decimal:
        """
        Sum of all position values (qty × mark_price) in quote currency.
        """
        total = Decimal("0")
        for asset, pos in self.all_positions().items():
            if asset in prices:
                total += pos.qty * prices[asset]
        return total

    def all_positions_including_closed(self) -> dict[str, Position]:
        """Return every position ever opened, including those that are now flat."""
        return dict(self._positions)

    def trade_history(self, asset: str | None = None) -> list[Trade]:
        """
        Return the full list of recorded trades, optionally filtered by asset.
        """
        if asset is None:
            return list(self._trades)
        return [t for t in self._trades if t.asset == asset]
