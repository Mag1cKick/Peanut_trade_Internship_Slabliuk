"""
inventory/rebalancer.py — Target-weight portfolio rebalancing planner.

Given current holdings and a set of target asset weights, computes the
minimum set of buy/sell orders required to bring the portfolio back to
its target allocation.

Two filters prevent noise trades:
  - ``deviation_threshold_bps``: skip assets that are already close enough
  - ``min_trade_value``:         skip orders whose notional is too small
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


@dataclass
class RebalanceOrder:
    """A single order produced by the planner."""

    asset: str
    side: str
    qty: Decimal
    target_weight: Decimal
    current_weight: Decimal
    deviation_bps: Decimal


class RebalancePlanner:
    """
    Computes orders to rebalance a portfolio to target weights.
    """

    def __init__(
        self,
        target_weights: dict[str, Decimal],
        min_trade_value: Decimal = Decimal("10"),
        deviation_threshold_bps: Decimal = Decimal("50"),
    ) -> None:
        total = sum(target_weights.values(), Decimal("0"))
        if total > Decimal("1.0001"):
            raise ValueError(
                f"target_weights sum to {total:.6f} which exceeds 1.0 — "
                "weights represent portfolio fractions and must not exceed 100 %"
            )
        if any(w < 0 for w in target_weights.values()):
            raise ValueError("All target weights must be non-negative")
        if min_trade_value < 0:
            raise ValueError("min_trade_value must be non-negative")
        if deviation_threshold_bps < 0:
            raise ValueError("deviation_threshold_bps must be non-negative")

        self._targets = dict(target_weights)
        self._min_trade_value = min_trade_value
        self._threshold_bps = deviation_threshold_bps

    def compute_orders(
        self,
        positions: dict[str, Decimal],
        prices: dict[str, Decimal],
        quote_balance: Decimal,
    ) -> list[RebalanceOrder]:
        """
        Return the list of orders needed to reach target weights.
        """
        portfolio_value = self._portfolio_value(positions, prices, quote_balance)
        if portfolio_value == 0:
            return []

        orders: list[RebalanceOrder] = []

        for asset, target_weight in self._targets.items():
            price = prices.get(asset)
            if price is None or price == 0:
                continue

            current_qty = positions.get(asset, Decimal("0"))
            current_value = current_qty * price
            current_weight = current_value / portfolio_value

            deviation_bps = self._deviation_bps(target_weight, current_weight)
            if deviation_bps < self._threshold_bps:
                continue

            target_value = target_weight * portfolio_value
            delta_value = target_value - current_value
            if abs(delta_value) < self._min_trade_value:
                continue

            delta_qty = delta_value / price
            orders.append(
                RebalanceOrder(
                    asset=asset,
                    side="buy" if delta_qty > 0 else "sell",
                    qty=abs(delta_qty),
                    target_weight=target_weight,
                    current_weight=current_weight,
                    deviation_bps=deviation_bps,
                )
            )

        orders.sort(key=lambda o: o.deviation_bps, reverse=True)
        return orders

    def weight_deviations(
        self,
        positions: dict[str, Decimal],
        prices: dict[str, Decimal],
        quote_balance: Decimal,
    ) -> dict[str, Decimal]:
        """
        Return the signed deviation of each target asset from its target weight,
        expressed in basis points.
        """
        portfolio_value = self._portfolio_value(positions, prices, quote_balance)
        if portfolio_value == 0:
            return {asset: Decimal("0") for asset in self._targets}

        deviations: dict[str, Decimal] = {}
        for asset, target in self._targets.items():
            price = prices.get(asset, Decimal("0"))
            current_qty = positions.get(asset, Decimal("0"))
            current_weight = (current_qty * price) / portfolio_value
            deviations[asset] = (current_weight - target) * Decimal("10000")

        return deviations

    @staticmethod
    def _portfolio_value(
        positions: dict[str, Decimal],
        prices: dict[str, Decimal],
        quote_balance: Decimal,
    ) -> Decimal:
        value = quote_balance
        for asset, qty in positions.items():
            value += qty * prices.get(asset, Decimal("0"))
        return value

    @staticmethod
    def _deviation_bps(target: Decimal, current: Decimal) -> Decimal:
        """Absolute deviation in basis points, relative to target."""
        if target == 0:
            return abs(current) * Decimal("10000")
        return abs(target - current) / target * Decimal("10000")
