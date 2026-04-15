"""
tests/test_inventory.py — Unit tests for inventory module

Covers:
  1. CostBasisTracker — record_fill, cost basis, realized PnL, fees, history
  2. RebalancePlanner — compute_orders, weight_deviations, edge cases
  3. PnLEngine       — snapshot, asset_pnl, unrealized/realized aggregates

No network calls, no external dependencies.
"""

from __future__ import annotations

import time
from decimal import Decimal

import pytest

from inventory.pnl import PnLEngine, PnLSnapshot, PortfolioPnL
from inventory.rebalancer import RebalanceOrder
from inventory.rebalancer import WeightRebalancePlanner as RebalancePlanner
from inventory.tracker import CostBasisTracker, Trade

# ══════════════════════════════════════════════════════════════════════════════
# 1. CostBasisTracker
# ══════════════════════════════════════════════════════════════════════════════


class TestCostBasisTrackerBasic:
    def setup_method(self):
        self.t = CostBasisTracker()

    def test_new_tracker_has_no_positions(self):
        assert self.t.all_positions() == {}

    def test_get_position_unknown_asset_returns_zero(self):
        pos = self.t.get_position("BTC")
        assert pos.qty == Decimal("0")
        assert pos.avg_cost == Decimal("0")

    def test_buy_creates_position(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        pos = self.t.get_position("ETH")
        assert pos.qty == Decimal("1")
        assert pos.avg_cost == Decimal("2000")

    def test_buy_increases_qty(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        self.t.record_fill("ETH", "buy", Decimal("2"), Decimal("2100"))
        pos = self.t.get_position("ETH")
        assert pos.qty == Decimal("3")

    def test_buy_weighted_average_cost(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("3000"))
        pos = self.t.get_position("ETH")
        # avg = (2000*1 + 3000*1) / 2 = 2500
        assert pos.avg_cost == Decimal("2500")

    def test_weighted_avg_unequal_qty(self):
        self.t.record_fill("ETH", "buy", Decimal("2"), Decimal("2000"))
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2300"))
        pos = self.t.get_position("ETH")
        # avg = (4000 + 2300) / 3 = 2100
        assert pos.avg_cost == Decimal("2100")


class TestCostBasisTrackerSell:
    def setup_method(self):
        self.t = CostBasisTracker()
        self.t.record_fill("ETH", "buy", Decimal("2"), Decimal("2000"))

    def test_sell_reduces_qty(self):
        self.t.record_fill("ETH", "sell", Decimal("0.5"), Decimal("2200"))
        assert self.t.get_position("ETH").qty == Decimal("1.5")

    def test_sell_books_realized_pnl(self):
        self.t.record_fill("ETH", "sell", Decimal("1"), Decimal("2200"))
        realized = self.t.get_position("ETH").realized_pnl
        # (2200 - 2000) * 1 = 200
        assert realized == Decimal("200")

    def test_sell_at_loss_books_negative_realized(self):
        self.t.record_fill("ETH", "sell", Decimal("1"), Decimal("1800"))
        assert self.t.get_position("ETH").realized_pnl == Decimal("-200")

    def test_full_sell_resets_avg_cost(self):
        self.t.record_fill("ETH", "sell", Decimal("2"), Decimal("2200"))
        pos = self.t.get_position("ETH")
        assert pos.qty == Decimal("0")
        assert pos.avg_cost == Decimal("0")

    def test_sell_more_than_held_capped_to_held(self):
        # Selling 10 when only 2 are held — should sell 2
        self.t.record_fill("ETH", "sell", Decimal("10"), Decimal("2200"))
        pos = self.t.get_position("ETH")
        assert pos.qty == Decimal("0")

    def test_sell_from_zero_position_no_error(self):
        t = CostBasisTracker()
        t.record_fill("ETH", "sell", Decimal("1"), Decimal("2000"))
        assert t.get_position("ETH").qty == Decimal("0")

    def test_all_positions_excludes_sold_out(self):
        self.t.record_fill("ETH", "sell", Decimal("2"), Decimal("2200"))
        assert "ETH" not in self.t.all_positions()


class TestCostBasisTrackerFees:
    def setup_method(self):
        self.t = CostBasisTracker()

    def test_buy_fee_accumulates(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"), fee=Decimal("2"))
        assert self.t.get_position("ETH").total_fees == Decimal("2")

    def test_sell_fee_deducted_from_realized(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        self.t.record_fill("ETH", "sell", Decimal("1"), Decimal("2200"), fee=Decimal("1"))
        pos = self.t.get_position("ETH")
        # realized = (2200 - 2000) * 1 - fee = 200 - 1 = 199
        assert pos.realized_pnl == Decimal("199")
        assert pos.total_fees == Decimal("1")

    def test_multiple_fees_accumulate(self):
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"), fee=Decimal("1"))
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2100"), fee=Decimal("1"))
        self.t.record_fill("ETH", "sell", Decimal("1"), Decimal("2200"), fee=Decimal("2"))
        assert self.t.get_position("ETH").total_fees == Decimal("4")


class TestCostBasisTrackerUnrealizedPnL:
    def setup_method(self):
        self.t = CostBasisTracker()
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))

    def test_unrealized_above_cost(self):
        assert self.t.unrealized_pnl("ETH", Decimal("2200")) == Decimal("200")

    def test_unrealized_below_cost(self):
        assert self.t.unrealized_pnl("ETH", Decimal("1800")) == Decimal("-200")

    def test_unrealized_at_cost_is_zero(self):
        assert self.t.unrealized_pnl("ETH", Decimal("2000")) == Decimal("0")

    def test_unrealized_zero_position_returns_zero(self):
        assert self.t.unrealized_pnl("BTC", Decimal("30000")) == Decimal("0")

    def test_unrealized_is_decimal(self):
        assert isinstance(self.t.unrealized_pnl("ETH", Decimal("2200")), Decimal)


class TestCostBasisTrackerExposure:
    def setup_method(self):
        self.t = CostBasisTracker()
        self.t.record_fill("ETH", "buy", Decimal("2"), Decimal("2000"))
        self.t.record_fill("BTC", "buy", Decimal("0.1"), Decimal("30000"))

    def test_total_exposure(self):
        prices = {"ETH": Decimal("2200"), "BTC": Decimal("32000")}
        # ETH: 2 × 2200 = 4400, BTC: 0.1 × 32000 = 3200
        assert self.t.total_exposure(prices) == Decimal("7600")

    def test_exposure_skips_missing_prices(self):
        prices = {"ETH": Decimal("2200")}
        assert self.t.total_exposure(prices) == Decimal("4400")

    def test_exposure_is_decimal(self):
        assert isinstance(self.t.total_exposure({"ETH": Decimal("2200")}), Decimal)


class TestCostBasisTrackerHistory:
    def setup_method(self):
        self.t = CostBasisTracker()
        self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        self.t.record_fill("BTC", "buy", Decimal("0.1"), Decimal("30000"))
        self.t.record_fill("ETH", "sell", Decimal("0.5"), Decimal("2200"))

    def test_full_history_length(self):
        assert len(self.t.trade_history()) == 3

    def test_filtered_history(self):
        eth_trades = self.t.trade_history("ETH")
        assert len(eth_trades) == 2
        assert all(t.asset == "ETH" for t in eth_trades)

    def test_trade_fields_correct(self):
        trades = self.t.trade_history("BTC")
        assert len(trades) == 1
        t = trades[0]
        assert isinstance(t, Trade)
        assert t.side == "buy"
        assert t.qty == Decimal("0.1")
        assert t.price == Decimal("30000")

    def test_timestamp_auto_set(self):
        before = int(time.time() * 1000)
        t2 = CostBasisTracker()
        t2.record_fill("X", "buy", Decimal("1"), Decimal("100"))
        ts = t2.trade_history("X")[0].timestamp
        assert ts >= before

    def test_timestamp_explicit(self):
        t2 = CostBasisTracker()
        t2.record_fill("X", "buy", Decimal("1"), Decimal("100"), timestamp=999)
        assert t2.trade_history("X")[0].timestamp == 999


class TestCostBasisTrackerValidation:
    def setup_method(self):
        self.t = CostBasisTracker()

    def test_zero_qty_raises(self):
        with pytest.raises(ValueError, match="qty"):
            self.t.record_fill("ETH", "buy", Decimal("0"), Decimal("2000"))

    def test_negative_qty_raises(self):
        with pytest.raises(ValueError, match="qty"):
            self.t.record_fill("ETH", "buy", Decimal("-1"), Decimal("2000"))

    def test_zero_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("0"))

    def test_negative_price_raises(self):
        with pytest.raises(ValueError, match="price"):
            self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("-1"))

    def test_negative_fee_raises(self):
        with pytest.raises(ValueError, match="fee"):
            self.t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"), fee=Decimal("-1"))

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            self.t.record_fill("ETH", "long", Decimal("1"), Decimal("2000"))


class TestCostBasisTrackerMultiAsset:
    def test_independent_positions(self):
        t = CostBasisTracker()
        t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        t.record_fill("BTC", "buy", Decimal("0.5"), Decimal("40000"))
        assert t.get_position("ETH").qty == Decimal("1")
        assert t.get_position("BTC").qty == Decimal("0.5")

    def test_all_positions_returns_both(self):
        t = CostBasisTracker()
        t.record_fill("ETH", "buy", Decimal("1"), Decimal("2000"))
        t.record_fill("BTC", "buy", Decimal("0.5"), Decimal("40000"))
        pos = t.all_positions()
        assert "ETH" in pos and "BTC" in pos


# ══════════════════════════════════════════════════════════════════════════════
# 2. RebalancePlanner
# ══════════════════════════════════════════════════════════════════════════════


def _planner(**kw) -> RebalancePlanner:
    defaults = {
        "target_weights": {"ETH": Decimal("0.6"), "BTC": Decimal("0.4")},
        "min_trade_value": Decimal("10"),
        "deviation_threshold_bps": Decimal("50"),
    }
    defaults.update(kw)
    return RebalancePlanner(**defaults)


class TestRebalancePlannerConstruction:
    def test_valid_weights_construct(self):
        _planner()  # no error

    def test_weights_summing_to_one_ok(self):
        RebalancePlanner({"A": Decimal("0.5"), "B": Decimal("0.5")})

    def test_weights_over_one_raises(self):
        with pytest.raises(ValueError, match="exceed"):
            RebalancePlanner({"A": Decimal("0.8"), "B": Decimal("0.5")})

    def test_negative_weight_raises(self):
        with pytest.raises(ValueError, match="non-negative"):
            RebalancePlanner({"A": Decimal("-0.1")})

    def test_negative_min_trade_value_raises(self):
        with pytest.raises(ValueError, match="min_trade_value"):
            RebalancePlanner({"A": Decimal("0.5")}, min_trade_value=Decimal("-1"))

    def test_negative_threshold_raises(self):
        with pytest.raises(ValueError, match="deviation_threshold_bps"):
            RebalancePlanner({"A": Decimal("0.5")}, deviation_threshold_bps=Decimal("-1"))


class TestRebalancePlannerComputeOrders:
    def setup_method(self):
        # Total portfolio: 2 ETH × 2000 + 10000 cash = 14000
        # ETH weight: 4000/14000 ≈ 28.6% vs target 60% → buy ETH
        # BTC weight: 0/14000 = 0% vs target 40% → buy BTC
        self.planner = _planner()
        self.positions = {"ETH": Decimal("2")}
        self.prices = {"ETH": Decimal("2000"), "BTC": Decimal("40000")}
        self.cash = Decimal("10000")

    def test_returns_list(self):
        orders = self.planner.compute_orders(self.positions, self.prices, self.cash)
        assert isinstance(orders, list)

    def test_buy_orders_generated(self):
        orders = self.planner.compute_orders(self.positions, self.prices, self.cash)
        sides = {o.asset: o.side for o in orders}
        # ETH is under-weight → buy
        assert sides.get("ETH") == "buy"

    def test_order_has_required_fields(self):
        orders = self.planner.compute_orders(self.positions, self.prices, self.cash)
        assert len(orders) > 0
        o = orders[0]
        assert isinstance(o, RebalanceOrder)
        assert o.qty > 0
        assert isinstance(o.deviation_bps, Decimal)

    def test_sorted_by_deviation_descending(self):
        orders = self.planner.compute_orders(self.positions, self.prices, self.cash)
        devs = [o.deviation_bps for o in orders]
        assert devs == sorted(devs, reverse=True)

    def test_at_target_no_order(self):
        # Portfolio exactly at target: 60% ETH, 40% BTC
        # Total = 10000: ETH = 0.6 × 10000 = 6000 → 3 ETH @ 2000
        #                BTC = 0.4 × 10000 = 4000 → 0.1 BTC @ 40000
        planner = RebalancePlanner(
            {"ETH": Decimal("0.6"), "BTC": Decimal("0.4")},
            deviation_threshold_bps=Decimal("50"),
        )
        positions = {"ETH": Decimal("3"), "BTC": Decimal("0.1")}
        prices = {"ETH": Decimal("2000"), "BTC": Decimal("40000")}
        cash = Decimal("0")
        orders = planner.compute_orders(positions, prices, cash)
        assert orders == []

    def test_empty_portfolio_returns_no_orders(self):
        orders = self.planner.compute_orders({}, {}, Decimal("0"))
        assert orders == []

    def test_missing_price_skips_asset(self):
        orders = self.planner.compute_orders({"ETH": Decimal("1")}, {}, Decimal("5000"))
        assert all(o.asset != "ETH" for o in orders)

    def test_small_delta_below_min_trade_skipped(self):
        # Portfolio is 100 USDT cash; target weights lead to tiny order sizes
        planner = RebalancePlanner(
            {"ETH": Decimal("0.5")},
            min_trade_value=Decimal("100"),
        )
        # Give portfolio value of 1 USDT → delta would be 0.5 USDT < 100 min
        orders = planner.compute_orders({}, {"ETH": Decimal("2000")}, Decimal("1"))
        assert orders == []

    def test_sell_order_when_over_weight(self):
        # ETH is heavily over-weight (all holdings in ETH, target only 60%)
        planner = RebalancePlanner(
            {"ETH": Decimal("0.6")},
            deviation_threshold_bps=Decimal("50"),
        )
        # 10 ETH @ 2000 = 20000 (100% ETH), target is 60%
        orders = planner.compute_orders(
            {"ETH": Decimal("10")},
            {"ETH": Decimal("2000")},
            Decimal("0"),
        )
        eth_orders = [o for o in orders if o.asset == "ETH"]
        assert len(eth_orders) == 1
        assert eth_orders[0].side == "sell"

    def test_qty_is_decimal(self):
        orders = self.planner.compute_orders(self.positions, self.prices, self.cash)
        for o in orders:
            assert isinstance(o.qty, Decimal)
            assert o.qty > 0


class TestRebalancePlannerWeightDeviations:
    def setup_method(self):
        self.planner = _planner()

    def test_returns_dict_with_all_target_assets(self):
        devs = self.planner.weight_deviations({}, {}, Decimal("0"))
        assert set(devs.keys()) == {"ETH", "BTC"}

    def test_at_target_deviation_is_zero(self):
        # Portfolio exactly at target: 60% ETH, 40% BTC, no cash
        positions = {"ETH": Decimal("3"), "BTC": Decimal("0.1")}
        prices = {"ETH": Decimal("2000"), "BTC": Decimal("40000")}
        devs = self.planner.weight_deviations(positions, prices, Decimal("0"))
        # ETH: 6000/10000 = 60%, target 60% → 0 bps
        # BTC: 4000/10000 = 40%, target 40% → 0 bps
        for dev in devs.values():
            assert abs(dev) < Decimal("1")  # within rounding

    def test_under_weight_negative_deviation(self):
        devs = self.planner.weight_deviations({}, {"ETH": Decimal("2000")}, Decimal("10000"))
        # ETH current weight = 0, target = 60% → deviation = (0 - 0.6) * 10000 = -6000 bps
        assert devs["ETH"] < 0

    def test_zero_portfolio_returns_zeros(self):
        devs = self.planner.weight_deviations({}, {}, Decimal("0"))
        for v in devs.values():
            assert v == Decimal("0")

    def test_deviation_values_are_decimal(self):
        devs = self.planner.weight_deviations({}, {"ETH": Decimal("2000")}, Decimal("10000"))
        for v in devs.values():
            assert isinstance(v, Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 3. PnLEngine
# ══════════════════════════════════════════════════════════════════════════════


def _tracker_with_eth() -> CostBasisTracker:
    t = CostBasisTracker()
    t.record_fill("ETH", "buy", Decimal("2"), Decimal("2000"), fee=Decimal("4"))
    return t


class TestPnLEngineSnapshot:
    def setup_method(self):
        self.tracker = _tracker_with_eth()
        self.engine = PnLEngine(self.tracker)

    def test_returns_portfolio_pnl(self):
        result = self.engine.snapshot({"ETH": Decimal("2200")})
        assert isinstance(result, PortfolioPnL)

    def test_unrealized_pnl_correct(self):
        result = self.engine.snapshot({"ETH": Decimal("2200")})
        # 2 ETH × (2200 - 2000) = 400
        assert result.total_unrealized == Decimal("400")

    def test_realized_pnl_before_any_sell(self):
        result = self.engine.snapshot({"ETH": Decimal("2200")})
        assert result.total_realized == Decimal("0")

    def test_realized_pnl_after_sell(self):
        self.tracker.record_fill("ETH", "sell", Decimal("1"), Decimal("2300"), fee=Decimal("2"))
        result = self.engine.snapshot({"ETH": Decimal("2300")})
        # realized = (2300 - 2000) * 1 - fee = 300 - 2 = 298
        assert result.total_realized == Decimal("298")

    def test_net_pnl_equals_realized_plus_unrealized(self):
        self.tracker.record_fill("ETH", "sell", Decimal("0.5"), Decimal("2300"))
        result = self.engine.snapshot({"ETH": Decimal("2100")})
        assert result.net_pnl == result.total_realized + result.total_unrealized

    def test_snapshot_includes_all_positions(self):
        self.tracker.record_fill("BTC", "buy", Decimal("0.1"), Decimal("30000"))
        result = self.engine.snapshot({"ETH": Decimal("2200"), "BTC": Decimal("32000")})
        assets = {s.asset for s in result.snapshots}
        assert "ETH" in assets
        assert "BTC" in assets

    def test_missing_mark_price_uses_zero(self):
        result = self.engine.snapshot({})
        assert result.total_unrealized == Decimal("0")

    def test_total_fees_tracked(self):
        result = self.engine.snapshot({"ETH": Decimal("2000")})
        assert result.total_fees == Decimal("4")

    def test_return_pct_positive_when_above_cost(self):
        result = self.engine.snapshot({"ETH": Decimal("2200")})
        eth_snapshot = next(s for s in result.snapshots if s.asset == "ETH")
        assert eth_snapshot.return_pct > 0

    def test_return_pct_zero_when_flat_position(self):
        # Sell everything
        self.tracker.record_fill("ETH", "sell", Decimal("2"), Decimal("2200"))
        result = self.engine.snapshot({"ETH": Decimal("2200")})
        eth_snapshot = next(s for s in result.snapshots if s.asset == "ETH")
        assert eth_snapshot.return_pct == Decimal("0")


class TestPnLEngineAssetPnL:
    def setup_method(self):
        self.tracker = _tracker_with_eth()
        self.engine = PnLEngine(self.tracker)

    def test_returns_pnl_snapshot(self):
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert isinstance(result, PnLSnapshot)

    def test_qty_correct(self):
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert result.qty == Decimal("2")

    def test_avg_cost_correct(self):
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert result.avg_cost == Decimal("2000")

    def test_unrealized_correct(self):
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert result.unrealized_pnl == Decimal("400")

    def test_total_pnl_equals_realized_plus_unrealized(self):
        self.tracker.record_fill("ETH", "sell", Decimal("0.5"), Decimal("2100"))
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert result.total_pnl == result.realized_pnl + result.unrealized_pnl

    def test_unknown_asset_returns_zero_snapshot(self):
        result = self.engine.asset_pnl("DOGE", Decimal("0.1"))
        assert result.qty == Decimal("0")
        assert result.unrealized_pnl == Decimal("0")
        assert result.realized_pnl == Decimal("0")

    def test_mark_price_stored(self):
        result = self.engine.asset_pnl("ETH", Decimal("2500"))
        assert result.mark_price == Decimal("2500")

    def test_return_pct_is_decimal(self):
        result = self.engine.asset_pnl("ETH", Decimal("2200"))
        assert isinstance(result.return_pct, Decimal)


class TestPnLSnapshotDataclass:
    """Sanity-check the dataclass fields themselves."""

    def test_pnl_snapshot_fields(self):
        s = PnLSnapshot(
            asset="ETH",
            qty=Decimal("1"),
            avg_cost=Decimal("2000"),
            mark_price=Decimal("2200"),
            unrealized_pnl=Decimal("200"),
            realized_pnl=Decimal("100"),
            total_fees=Decimal("5"),
            total_pnl=Decimal("300"),
            return_pct=Decimal("0.1"),
        )
        assert s.asset == "ETH"
        assert s.total_pnl == Decimal("300")

    def test_portfolio_pnl_fields(self):
        p = PortfolioPnL(
            snapshots=[],
            total_unrealized=Decimal("200"),
            total_realized=Decimal("100"),
            total_fees=Decimal("5"),
            net_pnl=Decimal("300"),
        )
        assert p.net_pnl == Decimal("300")
        assert p.snapshots == []
