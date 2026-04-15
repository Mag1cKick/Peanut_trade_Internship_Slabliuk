"""
inventory/rebalancer.py — Portfolio and venue rebalancing planners.

Two planners serve different purposes:

WeightRebalancePlanner — given target asset weights and current holdings,
                         computes the buy/sell orders needed to restore the
                         target allocation.  Purely mathematical; no venue
                         awareness.

RebalancePlanner       — venue-aware transfer planner that works with
                         InventoryTracker.  Detects cross-venue skew,
                         plans on-chain / CEX transfers with fee estimates,
                         and respects minimum operating balances.

CLI::

    python -m inventory.rebalancer --check
    python -m inventory.rebalancer --plan ETH
    python -m inventory.rebalancer --plan-all
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inventory.tracker import InventoryTracker, Venue

# ══════════════════════════════════════════════════════════════════════════════
# WeightRebalancePlanner — target-weight order generator (no venue awareness)
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class RebalanceOrder:
    """A single order produced by WeightRebalancePlanner."""

    asset: str
    side: str
    qty: Decimal
    target_weight: Decimal
    current_weight: Decimal
    deviation_bps: Decimal


class WeightRebalancePlanner:
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
        """Return the list of orders needed to reach target weights."""
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
        Return signed deviation of each target asset from its target weight
        in basis points.
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


TRANSFER_FEES: dict[str, dict] = {
    "ETH": {
        "withdrawal_fee": Decimal("0.005"),
        "min_withdrawal": Decimal("0.01"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDT": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "USDC": {
        "withdrawal_fee": Decimal("1.0"),
        "min_withdrawal": Decimal("10.0"),
        "confirmations": 12,
        "estimated_time_min": 15,
    },
    "BTC": {
        "withdrawal_fee": Decimal("0.0005"),
        "min_withdrawal": Decimal("0.001"),
        "confirmations": 3,
        "estimated_time_min": 30,
    },
}

MIN_OPERATING_BALANCE: dict[str, Decimal] = {
    "ETH": Decimal("0.5"),
    "USDT": Decimal("500"),
    "USDC": Decimal("500"),
    "BTC": Decimal("0.01"),
}


@dataclass
class TransferPlan:
    """One cross-venue transfer recommended by RebalancePlanner."""

    from_venue: Venue
    to_venue: Venue
    asset: str
    amount: Decimal
    estimated_fee: Decimal
    estimated_time_min: int

    @property
    def net_amount(self) -> Decimal:
        """Amount that arrives at the destination after fees."""
        return self.amount - self.estimated_fee


class RebalancePlanner:
    """
    Venue-aware rebalancer that works with :class:`~inventory.tracker.InventoryTracker`.
    """

    def __init__(
        self,
        tracker: InventoryTracker,
        threshold_pct: float = 30.0,
        target_ratio: dict | None = None,
    ) -> None:
        self._tracker = tracker
        self._threshold_pct = threshold_pct
        self._target_ratio = target_ratio

    def check_all(self) -> list[dict]:
        """
        Return skew analysis for every tracked asset.
        """
        return self._tracker.get_skews()

    def plan(self, asset: str) -> list[TransferPlan]:
        """
        Compute the minimum set of transfers to rebalance ``asset`` across venues.
        """
        skew = self._tracker.skew(asset)
        if skew["max_deviation_pct"] <= self._threshold_pct:
            return []

        total = skew["total"]
        if total == 0:
            return []

        venues_data = skew["venues"]
        n = len(venues_data)
        if n < 2:
            return []

        from inventory.tracker import Venue

        def _to_venue(name: str) -> Venue:
            for v in Venue:
                if v.value == name:
                    return v
            raise ValueError(f"Unknown venue: {name}")

        if self._target_ratio is not None:
            targets: dict[str, Decimal] = {
                (v.value if hasattr(v, "value") else str(v)): Decimal(str(frac))
                for v, frac in self._target_ratio.items()
            }
        else:
            equal = Decimal("1") / Decimal(str(n))
            targets = {v: equal for v in venues_data}

        surplus: list[list] = []
        deficit: list[list] = []

        for venue_name, data in venues_data.items():
            target_frac = targets.get(venue_name, Decimal("1") / Decimal(str(n)))
            target_amount = total * target_frac
            delta = data["amount"] - target_amount
            if delta > 0:
                surplus.append([venue_name, delta])
            elif delta < 0:
                deficit.append([venue_name, abs(delta)])

        fee_info = TRANSFER_FEES.get(asset, {})
        fee_amount = fee_info.get("withdrawal_fee", Decimal("0"))
        min_withdrawal = fee_info.get("min_withdrawal", Decimal("0"))
        time_min = fee_info.get("estimated_time_min", 0)

        min_op = MIN_OPERATING_BALANCE.get(asset, Decimal("0"))

        plans: list[TransferPlan] = []

        si = 0
        di = 0
        while si < len(surplus) and di < len(deficit):
            src_name, src_avail = surplus[si]
            dst_name, dst_need = deficit[di]

            src_bal = self._tracker._balances.get(_to_venue(src_name), {}).get(asset)
            src_total = src_bal.total if src_bal is not None else Decimal("0")
            max_transferable = max(Decimal("0"), src_total - min_op)
            transfer_amount = min(src_avail, dst_need, max_transferable)

            if transfer_amount >= min_withdrawal and transfer_amount > fee_amount:
                plans.append(
                    TransferPlan(
                        from_venue=_to_venue(src_name),
                        to_venue=_to_venue(dst_name),
                        asset=asset,
                        amount=transfer_amount,
                        estimated_fee=fee_amount,
                        estimated_time_min=time_min,
                    )
                )

            surplus[si][1] -= transfer_amount
            deficit[di][1] -= transfer_amount
            if surplus[si][1] <= Decimal("0"):
                si += 1
            if deficit[di][1] <= Decimal("0"):
                di += 1

        return plans

    def plan_all(self) -> dict[str, list[TransferPlan]]:
        """
        Return transfer plans for every asset that needs rebalancing.
        """
        result: dict[str, list[TransferPlan]] = {}
        for skew in self.check_all():
            asset = skew["asset"]
            if skew["needs_rebalance"]:
                asset_plans = self.plan(asset)
                if asset_plans:
                    result[asset] = asset_plans
        return result

    def estimate_cost(self, plans: list[TransferPlan]) -> dict:
        """
        Summarise the cost and logistics of a list of transfer plans.
        """
        if not plans:
            return {
                "total_transfers": 0,
                "total_fees_usd": Decimal("0"),
                "total_time_min": 0,
                "assets_affected": [],
            }

        total_fees = sum((p.estimated_fee for p in plans), Decimal("0"))
        max_time = max(p.estimated_time_min for p in plans)
        assets = sorted({p.asset for p in plans})

        return {
            "total_transfers": len(plans),
            "total_fees_usd": total_fees,
            "total_time_min": max_time,
            "assets_affected": assets,
        }


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Venue rebalance planner — connects to demo tracker",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m inventory.rebalancer --check\n"
            "  python -m inventory.rebalancer --plan ETH\n"
            "  python -m inventory.rebalancer --plan-all"
        ),
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--check", action="store_true", help="Show skew for all assets")
    group.add_argument("--plan", metavar="ASSET", help="Show transfer plan for ASSET")
    group.add_argument(
        "--plan-all", action="store_true", help="Show plans for all unbalanced assets"
    )
    args = parser.parse_args(argv)

    from inventory.tracker import InventoryTracker, Venue

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": "9.0", "locked": "0"},
            "USDT": {"free": "5000", "locked": "0"},
        },
    )
    tracker.update_from_wallet(Venue.WALLET, {"ETH": "1.0", "USDT": "500"})

    planner = RebalancePlanner(tracker)

    if args.check:
        skews = planner.check_all()
        print(f"{'Asset':<8}  {'Total':>12}  {'Max Dev %':>10}  {'Needs Rebal':>12}")
        print("-" * 50)
        for s in skews:
            flag = "YES" if s["needs_rebalance"] else "no"
            print(
                f"{s['asset']:<8}  {float(s['total']):>12.4f}  "
                f"{s['max_deviation_pct']:>10.1f}  {flag:>12}"
            )
        return 0

    if args.plan:
        asset = args.plan.upper()
        plans = planner.plan(asset)
        if not plans:
            print(f"No rebalance needed for {asset}.")
            return 0
        print(f"Transfer plans for {asset}:")
        for i, p in enumerate(plans, 1):
            print(
                f"  [{i}] {p.from_venue.value} → {p.to_venue.value}: "
                f"{p.amount} {p.asset}  "
                f"(fee={p.estimated_fee}, net={p.net_amount}, ~{p.estimated_time_min}min)"
            )
        cost = planner.estimate_cost(plans)
        print(
            f"\nTotal fees: {cost['total_fees_usd']}  |  "
            f"Est. time: {cost['total_time_min']} min"
        )
        return 0

    if args.plan_all:
        all_plans = planner.plan_all()
        if not all_plans:
            print("All assets are balanced.")
            return 0
        for asset, plans in all_plans.items():
            print(f"\n{asset}:")
            for p in plans:
                print(
                    f"  {p.from_venue.value} → {p.to_venue.value}: "
                    f"{p.amount} (fee={p.estimated_fee}, ~{p.estimated_time_min}min)"
                )
        all_plan_list = [p for ps in all_plans.values() for p in ps]
        cost = planner.estimate_cost(all_plan_list)
        print(
            f"\nTotal transfers: {cost['total_transfers']}  |  "
            f"Total fees: {cost['total_fees_usd']}"
        )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
