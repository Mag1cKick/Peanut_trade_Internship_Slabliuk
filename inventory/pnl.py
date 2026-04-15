"""
inventory/pnl.py — P&L engine combining realized and unrealized positions.

Wraps CostBasisTracker with mark-price valuation to produce:
  - Per-asset P&L snapshots (unrealized, realized, fees, return %)
  - Portfolio-level aggregates

All monetary values are Decimal.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from inventory.tracker import CostBasisTracker


@dataclass
class PnLSnapshot:
    """P&L breakdown for a single asset position."""

    asset: str
    qty: Decimal
    avg_cost: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    total_pnl: Decimal
    return_pct: Decimal


@dataclass
class PortfolioPnL:
    """Aggregate P&L across all positions."""

    snapshots: list[PnLSnapshot]
    total_unrealized: Decimal
    total_realized: Decimal
    total_fees: Decimal
    net_pnl: Decimal


class PnLEngine:
    """
    Computes realized and unrealized P&L for all positions in an
    :class:`~inventory.tracker.CostBasisTracker`.
    """

    def __init__(self, tracker: CostBasisTracker) -> None:
        self._tracker = tracker

    def snapshot(self, mark_prices: dict[str, Decimal]) -> PortfolioPnL:
        """
        Compute a full P&L snapshot for every position in the tracker.
        """
        snapshots: list[PnLSnapshot] = []
        total_unrealized = Decimal("0")
        total_realized = Decimal("0")
        total_fees = Decimal("0")

        for asset, pos in self._tracker.all_positions_including_closed().items():
            mark = mark_prices.get(asset, pos.avg_cost)
            unrealized = self._tracker.unrealized_pnl(asset, mark)
            realized = pos.realized_pnl
            fees = pos.total_fees

            cost_basis = pos.avg_cost * pos.qty
            return_pct = unrealized / cost_basis if cost_basis > 0 else Decimal("0")

            total_unrealized += unrealized
            total_realized += realized
            total_fees += fees

            snapshots.append(
                PnLSnapshot(
                    asset=asset,
                    qty=pos.qty,
                    avg_cost=pos.avg_cost,
                    mark_price=mark,
                    unrealized_pnl=unrealized,
                    realized_pnl=realized,
                    total_fees=fees,
                    total_pnl=realized + unrealized,
                    return_pct=return_pct,
                )
            )

        net_pnl = total_realized + total_unrealized

        return PortfolioPnL(
            snapshots=snapshots,
            total_unrealized=total_unrealized,
            total_realized=total_realized,
            total_fees=total_fees,
            net_pnl=net_pnl,
        )

    def asset_pnl(self, asset: str, mark_price: Decimal) -> PnLSnapshot:
        """
        P&L snapshot for a single asset.
        """
        pos = self._tracker.get_position(asset)
        unrealized = self._tracker.unrealized_pnl(asset, mark_price)
        cost_basis = pos.avg_cost * pos.qty
        return_pct = unrealized / cost_basis if cost_basis > 0 else Decimal("0")

        return PnLSnapshot(
            asset=asset,
            qty=pos.qty,
            avg_cost=pos.avg_cost,
            mark_price=mark_price,
            unrealized_pnl=unrealized,
            realized_pnl=pos.realized_pnl,
            total_fees=pos.total_fees,
            total_pnl=pos.realized_pnl + unrealized,
            return_pct=return_pct,
        )
