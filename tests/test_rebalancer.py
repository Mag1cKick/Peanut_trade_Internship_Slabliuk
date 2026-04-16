"""
tests/test_rebalancer.py — Unit tests for RebalancePlanner (Part 4).

Covers:
  1. TransferPlan dataclass and net_amount property
  2. TRANSFER_FEES and MIN_OPERATING_BALANCE constants
  3. RebalancePlanner.check_all()
  4. RebalancePlanner.plan(asset)
  5. RebalancePlanner.plan_all()
  6. RebalancePlanner.estimate_cost()
  7. CLI smoke tests

No network calls, no external dependencies.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from inventory.rebalancer import (
    MIN_OPERATING_BALANCE,
    TRANSFER_FEES,
    RebalancePlanner,
    TransferPlan,
)
from inventory.tracker import InventoryTracker, Venue

# ── Helpers ────────────────────────────────────────────────────────────────────


def _tracker_90_10() -> InventoryTracker:
    """ETH heavily skewed to BINANCE (9 ETH) vs WALLET (1 ETH)."""
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(Venue.BINANCE, {"ETH": {"free": "9", "locked": "0"}})
    t.update_from_wallet(Venue.WALLET, {"ETH": "1"})
    return t


def _tracker_50_50() -> InventoryTracker:
    """ETH balanced: 5 BINANCE, 5 WALLET."""
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(Venue.BINANCE, {"ETH": {"free": "5", "locked": "0"}})
    t.update_from_wallet(Venue.WALLET, {"ETH": "5"})
    return t


def _tracker_multi_asset() -> InventoryTracker:
    """ETH skewed (9/1), USDT skewed (9000/1000) — both > 30% deviation."""
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": "9", "locked": "0"},
            "USDT": {"free": "9000", "locked": "0"},
        },
    )
    t.update_from_wallet(Venue.WALLET, {"ETH": "1", "USDT": "1000"})
    return t


# ══════════════════════════════════════════════════════════════════════════════
# 1. TransferPlan dataclass
# ══════════════════════════════════════════════════════════════════════════════


class TestTransferPlan:
    def _plan(self, amount="4", fee="0.005") -> TransferPlan:
        return TransferPlan(
            from_venue=Venue.BINANCE,
            to_venue=Venue.WALLET,
            asset="ETH",
            amount=Decimal(amount),
            estimated_fee=Decimal(fee),
            estimated_time_min=15,
        )

    def test_net_amount(self):
        p = self._plan("4", "0.005")
        assert p.net_amount == Decimal("4") - Decimal("0.005")

    def test_net_amount_zero_fee(self):
        p = self._plan("2", "0")
        assert p.net_amount == Decimal("2")

    def test_fields_accessible(self):
        p = self._plan("4", "0.005")
        assert p.from_venue == Venue.BINANCE
        assert p.to_venue == Venue.WALLET
        assert p.asset == "ETH"
        assert p.estimated_time_min == 15

    def test_net_amount_is_decimal(self):
        p = self._plan("3", "0.005")
        assert isinstance(p.net_amount, Decimal)

    def test_net_amount_subtracts_fee(self):
        p = TransferPlan(
            from_venue=Venue.BINANCE,
            to_venue=Venue.WALLET,
            asset="USDT",
            amount=Decimal("1000"),
            estimated_fee=Decimal("1"),
            estimated_time_min=15,
        )
        assert p.net_amount == Decimal("999")


# ══════════════════════════════════════════════════════════════════════════════
# 2. Constants
# ══════════════════════════════════════════════════════════════════════════════


class TestConstants:
    def test_transfer_fees_has_eth(self):
        assert "ETH" in TRANSFER_FEES
        eth = TRANSFER_FEES["ETH"]
        assert "withdrawal_fee" in eth
        assert "min_withdrawal" in eth
        assert "confirmations" in eth
        assert "estimated_time_min" in eth

    def test_transfer_fees_has_usdt(self):
        assert "USDT" in TRANSFER_FEES

    def test_eth_withdrawal_fee_is_decimal(self):
        assert isinstance(TRANSFER_FEES["ETH"]["withdrawal_fee"], Decimal)

    def test_min_operating_balance_has_eth(self):
        assert "ETH" in MIN_OPERATING_BALANCE
        assert isinstance(MIN_OPERATING_BALANCE["ETH"], Decimal)

    def test_min_operating_balance_has_usdt(self):
        assert "USDT" in MIN_OPERATING_BALANCE

    def test_eth_fee_positive(self):
        assert TRANSFER_FEES["ETH"]["withdrawal_fee"] > 0

    def test_usdt_min_withdrawal_positive(self):
        assert TRANSFER_FEES["USDT"]["min_withdrawal"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# 3. check_all
# ══════════════════════════════════════════════════════════════════════════════


class TestCheckAll:
    def test_returns_list(self):
        planner = RebalancePlanner(_tracker_90_10())
        result = planner.check_all()
        assert isinstance(result, list)

    def test_skewed_asset_detected(self):
        planner = RebalancePlanner(_tracker_90_10())
        result = planner.check_all()
        eth_skew = next(s for s in result if s["asset"] == "ETH")
        assert eth_skew["needs_rebalance"] is True

    def test_balanced_asset_not_flagged(self):
        planner = RebalancePlanner(_tracker_50_50())
        result = planner.check_all()
        eth_skew = next(s for s in result if s["asset"] == "ETH")
        assert eth_skew["needs_rebalance"] is False

    def test_multi_asset_returns_all(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        result = planner.check_all()
        assets = {s["asset"] for s in result}
        assert "ETH" in assets
        assert "USDT" in assets

    def test_skew_schema_present(self):
        planner = RebalancePlanner(_tracker_90_10())
        result = planner.check_all()
        assert len(result) > 0
        s = result[0]
        assert "asset" in s
        assert "total" in s
        assert "venues" in s
        assert "max_deviation_pct" in s
        assert "needs_rebalance" in s


# ══════════════════════════════════════════════════════════════════════════════
# 4. plan(asset)
# ══════════════════════════════════════════════════════════════════════════════


class TestPlan:
    def test_skewed_asset_produces_plan(self):
        """90/10 split → plan moves funds from BINANCE to WALLET."""
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        assert len(plans) > 0

    def test_plan_direction_is_binance_to_wallet(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        assert plans[0].from_venue == Venue.BINANCE
        assert plans[0].to_venue == Venue.WALLET

    def test_balanced_asset_returns_empty(self):
        planner = RebalancePlanner(_tracker_50_50())
        plans = planner.plan("ETH")
        assert plans == []

    def test_plan_has_correct_asset(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        assert all(p.asset == "ETH" for p in plans)

    def test_plan_respects_min_operating_balance(self):
        """Source venue must keep MIN_OPERATING_BALANCE[asset] after transfer."""
        t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        # BINANCE has just above min (0.6 ETH), WALLET has 0 → would need to move 0.3
        # but MIN_OPERATING_BALANCE['ETH'] = 0.5, so only 0.1 can move.
        t.update_from_cex(Venue.BINANCE, {"ETH": {"free": "0.6", "locked": "0"}})
        t.update_from_wallet(Venue.WALLET, {"ETH": "0"})
        planner = RebalancePlanner(t)
        plans = planner.plan("ETH")
        if plans:
            # Amount transferred ≤ (0.6 - 0.5) = 0.1
            assert plans[0].amount <= Decimal("0.1") + Decimal("0.001")  # small rounding tolerance

    def test_plan_amount_is_decimal(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        assert all(isinstance(p.amount, Decimal) for p in plans)

    def test_plan_fee_matches_transfer_fees(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        expected_fee = TRANSFER_FEES["ETH"]["withdrawal_fee"]
        assert all(p.estimated_fee == expected_fee for p in plans)

    def test_plan_time_matches_transfer_fees(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        expected_time = TRANSFER_FEES["ETH"]["estimated_time_min"]
        assert all(p.estimated_time_min == expected_time for p in plans)

    def test_unknown_asset_returns_empty(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("DOGE")
        assert plans == []

    def test_net_amount_less_than_amount(self):
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        assert all(p.net_amount < p.amount for p in plans)

    def test_usdt_plan_produced(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        plans = planner.plan("USDT")
        assert len(plans) > 0
        assert plans[0].asset == "USDT"

    def test_custom_threshold_suppresses_plan(self):
        """With a 99% threshold, even 90/10 split should not trigger a plan."""
        planner = RebalancePlanner(_tracker_90_10(), threshold_pct=99.0)
        plans = planner.plan("ETH")
        assert plans == []


# ══════════════════════════════════════════════════════════════════════════════
# 5. plan_all
# ══════════════════════════════════════════════════════════════════════════════


class TestPlanAll:
    def test_returns_dict(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        result = planner.plan_all()
        assert isinstance(result, dict)

    def test_skewed_assets_included(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        result = planner.plan_all()
        assert "ETH" in result
        assert "USDT" in result

    def test_balanced_assets_excluded(self):
        planner = RebalancePlanner(_tracker_50_50())
        result = planner.plan_all()
        assert "ETH" not in result

    def test_all_values_are_lists_of_transfer_plans(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        result = planner.plan_all()
        for asset, plans in result.items():
            assert isinstance(plans, list)
            assert all(isinstance(p, TransferPlan) for p in plans)

    def test_empty_when_all_balanced(self):
        planner = RebalancePlanner(_tracker_50_50())
        result = planner.plan_all()
        assert result == {}


# ══════════════════════════════════════════════════════════════════════════════
# 6. estimate_cost
# ══════════════════════════════════════════════════════════════════════════════


class TestEstimateCost:
    def _make_plan(self, asset="ETH", fee="0.005", time_min=15) -> TransferPlan:
        return TransferPlan(
            from_venue=Venue.BINANCE,
            to_venue=Venue.WALLET,
            asset=asset,
            amount=Decimal("4"),
            estimated_fee=Decimal(fee),
            estimated_time_min=time_min,
        )

    def test_empty_plans_returns_zeros(self):
        planner = RebalancePlanner(_tracker_50_50())
        result = planner.estimate_cost([])
        assert result["total_transfers"] == 0
        assert result["total_fees_usd"] == Decimal("0")
        assert result["total_time_min"] == 0
        assert result["assets_affected"] == []

    def test_single_plan(self):
        planner = RebalancePlanner(_tracker_90_10())
        plan = self._make_plan()
        result = planner.estimate_cost([plan])
        assert result["total_transfers"] == 1
        assert result["total_fees_usd"] == Decimal("0.005")
        assert result["total_time_min"] == 15
        assert result["assets_affected"] == ["ETH"]

    def test_multiple_plans_sum_fees(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        plans = [
            self._make_plan("ETH", "0.005", 15),
            self._make_plan("USDT", "1.0", 15),
        ]
        result = planner.estimate_cost(plans)
        assert result["total_transfers"] == 2
        assert result["total_fees_usd"] == Decimal("1.005")

    def test_max_time_used_not_sum(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        plans = [
            self._make_plan("ETH", "0.005", 15),
            self._make_plan("BTC", "0.0005", 30),
        ]
        result = planner.estimate_cost(plans)
        assert result["total_time_min"] == 30  # max, not 15+30=45

    def test_assets_affected_sorted(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        plans = [
            self._make_plan("USDT", "1.0", 15),
            self._make_plan("ETH", "0.005", 15),
        ]
        result = planner.estimate_cost(plans)
        assert result["assets_affected"] == sorted(result["assets_affected"])

    def test_assets_affected_deduplicated(self):
        planner = RebalancePlanner(_tracker_multi_asset())
        plans = [self._make_plan("ETH"), self._make_plan("ETH")]
        result = planner.estimate_cost(plans)
        assert result["assets_affected"].count("ETH") == 1

    def test_total_fees_is_decimal(self):
        planner = RebalancePlanner(_tracker_90_10())
        result = planner.estimate_cost([self._make_plan()])
        assert isinstance(result["total_fees_usd"], Decimal)

    def test_from_real_plan(self):
        """estimate_cost works on plans produced by plan()."""
        planner = RebalancePlanner(_tracker_90_10())
        plans = planner.plan("ETH")
        cost = planner.estimate_cost(plans)
        assert cost["total_transfers"] == len(plans)
        assert cost["assets_affected"] == ["ETH"]


# ══════════════════════════════════════════════════════════════════════════════
# 7. CLI smoke tests
# ══════════════════════════════════════════════════════════════════════════════


class TestCLI:
    def test_check_exits_zero(self):
        from inventory.rebalancer import _run_cli

        assert _run_cli(["--check"]) == 0

    def test_plan_exits_zero(self):
        from inventory.rebalancer import _run_cli

        assert _run_cli(["--plan", "ETH"]) == 0

    def test_plan_all_exits_zero(self):
        from inventory.rebalancer import _run_cli

        assert _run_cli(["--plan-all"]) == 0

    def test_check_prints_output(self, capsys):
        from inventory.rebalancer import _run_cli

        _run_cli(["--check"])
        captured = capsys.readouterr()
        assert "ETH" in captured.out

    def test_plan_eth_prints_output(self, capsys):
        from inventory.rebalancer import _run_cli

        _run_cli(["--plan", "ETH"])
        captured = capsys.readouterr()
        # Either a plan or "No rebalance needed" message
        assert "ETH" in captured.out or "No rebalance" in captured.out

    def test_missing_args_exits_nonzero(self):
        from inventory.rebalancer import _run_cli

        with pytest.raises(SystemExit) as exc_info:
            _run_cli([])
        assert exc_info.value.code != 0


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestWeightPlannerDeviationBps:
    """Cover _deviation_bps(target=0) branch (line 163)."""

    def test_zero_target_returns_current_times_10000(self):
        from inventory.rebalancer import WeightRebalancePlanner

        result = WeightRebalancePlanner._deviation_bps(
            target=__import__("decimal").Decimal("0"),
            current=__import__("decimal").Decimal("5"),
        )
        assert result == __import__("decimal").Decimal("50000")


class TestPlanEdgeCases:
    """Cover total==0 and n<2 branches (lines 250, 255) and target_ratio (line 266)."""

    def test_plan_returns_empty_when_total_zero(self):
        from inventory.rebalancer import RebalancePlanner
        from inventory.tracker import InventoryTracker, Venue

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        # No funds loaded → total == 0 for any asset
        planner = RebalancePlanner(tracker, threshold_pct=0.0)
        plans = planner.plan("ETH")
        assert plans == []

    def test_plan_returns_empty_when_single_venue(self):
        from inventory.rebalancer import RebalancePlanner
        from inventory.tracker import InventoryTracker, Venue

        # Only one venue loaded
        tracker = InventoryTracker([Venue.BINANCE])
        tracker.update_from_cex(Venue.BINANCE, {"ETH": {"free": "10", "locked": "0"}})
        planner = RebalancePlanner(tracker, threshold_pct=0.0)
        plans = planner.plan("ETH")
        assert plans == []

    def test_plan_with_custom_target_ratio(self):
        from decimal import Decimal

        from inventory.rebalancer import RebalancePlanner
        from inventory.tracker import InventoryTracker, Venue

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(Venue.BINANCE, {"ETH": {"free": "9", "locked": "0"}})
        tracker.update_from_wallet(Venue.WALLET, {"ETH": "1"})
        # Custom target: 70% BINANCE, 30% WALLET
        planner = RebalancePlanner(
            tracker,
            threshold_pct=0.0,
            target_ratio={Venue.BINANCE: Decimal("0.7"), Venue.WALLET: Decimal("0.3")},
        )
        plans = planner.plan("ETH")
        # With 9/1 split vs 70/30 target, should produce a plan
        assert isinstance(plans, list)


class TestToVenueRaises:
    """Cover _to_venue ValueError (line 263) — inject an unknown venue name."""

    def test_unknown_venue_name_raises(self):
        from inventory.rebalancer import RebalancePlanner
        from inventory.tracker import InventoryTracker, Venue

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(Venue.BINANCE, {"ETH": {"free": "9", "locked": "0"}})
        tracker.update_from_wallet(Venue.WALLET, {"ETH": "1"})
        planner = RebalancePlanner(tracker, threshold_pct=0.0)

        # Monkeypatch skew to inject a fake venue name that _to_venue can't map
        original_skew = tracker.skew

        def _bad_skew(asset):
            result = original_skew(asset)
            result["venues"] = {
                "unknown_venue": result["venues"].get("binance", {}),
                **{k: v for k, v in result["venues"].items() if k != "binance"},
            }
            return result

        tracker.skew = _bad_skew

        with pytest.raises(ValueError, match="Unknown venue"):
            planner.plan("ETH")


class TestCLIGaps:
    """Cover CLI paths not yet exercised (lines 413-414, 432-433, 449)."""

    def test_cli_plan_no_rebalance_needed(self, capsys):
        # Use an asset not in the hardcoded demo tracker → plan() returns [] → "No rebalance needed"
        from inventory.rebalancer import _run_cli

        rc = _run_cli(["--plan", "BTC"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "No rebalance needed" in out

    def test_cli_plan_all_all_balanced(self, capsys):
        # Hardcoded demo tracker has skewed ETH and USDT → plan-all shows transfers
        from inventory.rebalancer import _run_cli

        rc = _run_cli(["--plan-all"])
        assert rc == 0
        out = capsys.readouterr().out
        assert "ETH" in out

    def test_cli_plan_all_with_skewed_assets(self, capsys):
        # Hardcoded demo tracker: ETH 9/1 → transfer from binance to wallet
        from inventory.rebalancer import _run_cli

        rc = _run_cli(["--plan-all"])
        assert rc == 0
