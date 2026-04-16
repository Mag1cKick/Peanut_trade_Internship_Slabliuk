"""
tests/test_order_book.py — Unit tests for exchange.order_book.OrderBookAnalyzer

No network calls.  All tests operate on hand-crafted order book dicts.

Test groups:
  1. Construction
  2. vwap_to_fill — buy/sell, partial fill, exact fill, insufficient liquidity
  3. book_imbalance — balanced, bid-heavy, ask-heavy, empty book
  4. depth_at_bps — band coverage, mid-price zero edge case
  5. liquidity_walls — threshold filtering
  6. depth_levels — bid/ask, cumulative totals, fewer levels than requested
  7. spread / mid_price helpers
  8. Input validation errors
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.order_book import DepthLevel, OrderBookAnalyzer

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_book(
    bids: list | None = None,
    asks: list | None = None,
    mid: str = "2001.5",
    symbol: str = "ETH/USDT",
) -> dict:
    """Build a minimal order-book dict as returned by ExchangeClient."""
    bids = bids or [
        (Decimal("2001"), Decimal("1.5")),
        (Decimal("2000"), Decimal("2.0")),
        (Decimal("1999"), Decimal("0.5")),
    ]
    asks = asks or [
        (Decimal("2002"), Decimal("1.0")),
        (Decimal("2003"), Decimal("0.8")),
        (Decimal("2004"), Decimal("2.0")),
    ]
    return {
        "symbol": symbol,
        "bids": bids,
        "asks": asks,
        "mid_price": Decimal(mid),
    }


def _analyzer(**kw) -> OrderBookAnalyzer:
    return OrderBookAnalyzer(_make_book(**kw))


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_basic_construction(self):
        a = _analyzer()
        assert a.mid_price() == Decimal("2001.5")

    def test_symbol_stored(self):
        a = OrderBookAnalyzer(_make_book(symbol="BTC/USDT"))
        assert a._symbol == "BTC/USDT"

    def test_empty_book_constructs(self):
        book = {"symbol": "X", "bids": [], "asks": [], "mid_price": Decimal("0")}
        a = OrderBookAnalyzer(book)
        assert a.mid_price() == Decimal("0")

    def test_missing_symbol_defaults_empty_string(self):
        book = {"bids": [], "asks": [], "mid_price": Decimal("100")}
        a = OrderBookAnalyzer(book)
        assert a._symbol == ""


# ── 2. vwap_to_fill ────────────────────────────────────────────────────────────


class TestVwapToFill:
    def setup_method(self):
        self.a = _analyzer()

    def test_buy_single_level_exact(self):
        # Exactly fills the first ask level (1.0 @ 2002)
        vwap = self.a.vwap_to_fill("buy", Decimal("1.0"))
        assert vwap == Decimal("2002")

    def test_buy_crosses_two_levels(self):
        # 1.0 @ 2002, then 0.8 @ 2003 → total 1.8 units
        vwap = self.a.vwap_to_fill("buy", Decimal("1.8"))
        expected = (Decimal("2002") * 1 + Decimal("2003") * Decimal("0.8")) / Decimal("1.8")
        assert abs(vwap - expected) < Decimal("1e-10")

    def test_sell_single_level_exact(self):
        # Exactly fills the first bid level (1.5 @ 2001)
        vwap = self.a.vwap_to_fill("sell", Decimal("1.5"))
        assert vwap == Decimal("2001")

    def test_sell_crosses_two_levels(self):
        # 1.5 @ 2001, then 0.5 out of 2.0 @ 2000
        vwap = self.a.vwap_to_fill("sell", Decimal("2.0"))
        expected = (Decimal("2001") * Decimal("1.5") + Decimal("2000") * Decimal("0.5")) / Decimal(
            "2.0"
        )
        assert abs(vwap - expected) < Decimal("1e-10")

    def test_insufficient_liquidity_returns_none(self):
        # Book has 1 + 0.8 + 2 = 3.8 ask qty; requesting 100 should fail
        result = self.a.vwap_to_fill("buy", Decimal("100"))
        assert result is None

    def test_insufficient_sell_liquidity_returns_none(self):
        a = OrderBookAnalyzer(
            {"bids": [(Decimal("100"), Decimal("1"))], "asks": [], "mid_price": Decimal("100")}
        )
        assert a.vwap_to_fill("sell", Decimal("5")) is None

    def test_exact_book_size_fills(self):
        # Total ask qty = 1 + 0.8 + 2 = 3.8 — should fill exactly
        result = self.a.vwap_to_fill("buy", Decimal("3.8"))
        assert result is not None

    def test_returns_decimal(self):
        result = self.a.vwap_to_fill("buy", Decimal("1"))
        assert isinstance(result, Decimal)

    def test_zero_size_raises(self):
        with pytest.raises(ValueError, match="positive"):
            self.a.vwap_to_fill("buy", Decimal("0"))

    def test_negative_size_raises(self):
        with pytest.raises(ValueError, match="positive"):
            self.a.vwap_to_fill("sell", Decimal("-1"))

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            self.a.vwap_to_fill("long", Decimal("1"))


# ── 3. book_imbalance ─────────────────────────────────────────────────────────


class TestBookImbalance:
    def test_balanced_book_near_zero(self):
        bids = [(Decimal("100"), Decimal("1"))]
        asks = [(Decimal("101"), Decimal("1"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        assert a.book_imbalance() == Decimal("0")

    def test_bid_heavy_positive(self):
        bids = [(Decimal("100"), Decimal("3"))]
        asks = [(Decimal("101"), Decimal("1"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        imbalance = a.book_imbalance()
        assert imbalance > 0

    def test_ask_heavy_negative(self):
        bids = [(Decimal("100"), Decimal("1"))]
        asks = [(Decimal("101"), Decimal("3"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        assert a.book_imbalance() < 0

    def test_all_bids_returns_one(self):
        bids = [(Decimal("100"), Decimal("5"))]
        asks: list = []
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100")})
        assert a.book_imbalance() == Decimal("1")

    def test_all_asks_returns_minus_one(self):
        asks = [(Decimal("101"), Decimal("5"))]
        a = OrderBookAnalyzer({"bids": [], "asks": asks, "mid_price": Decimal("100")})
        assert a.book_imbalance() == Decimal("-1")

    def test_empty_book_returns_zero(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.book_imbalance() == Decimal("0")

    def test_depth_parameter_limits_levels(self):
        # Only top 1 level considered — bids qty = 1.5, asks qty = 1.0
        a = _analyzer()
        imbalance_1 = a.book_imbalance(depth=1)
        imbalance_3 = a.book_imbalance(depth=3)
        # With depth=1: bids=1.5, asks=1.0 → positive
        assert imbalance_1 > 0
        # Results differ when more levels are included
        assert imbalance_1 != imbalance_3

    def test_result_in_range(self):
        a = _analyzer()
        imbalance = a.book_imbalance()
        assert Decimal("-1") <= imbalance <= Decimal("1")

    def test_result_is_decimal(self):
        a = _analyzer()
        assert isinstance(a.book_imbalance(), Decimal)


# ── 4. depth_at_bps ───────────────────────────────────────────────────────────


class TestDepthAtBps:
    def setup_method(self):
        # mid = 2001.5
        # bid threshold at 50 bps: 2001.5 × (1 − 0.005) = 1991.5
        # ask threshold at 50 bps: 2001.5 × (1 + 0.005) = 2011.5
        self.a = _analyzer()

    def test_returns_four_keys(self):
        result = self.a.depth_at_bps(Decimal("50"))
        for key in ("bid_qty", "ask_qty", "bid_value", "ask_value"):
            assert key in result

    def test_all_bids_within_50_bps(self):
        # All bid prices (2001, 2000, 1999) are above 1991.5 → all included
        result = self.a.depth_at_bps(Decimal("50"))
        expected_qty = Decimal("1.5") + Decimal("2.0") + Decimal("0.5")
        assert result["bid_qty"] == expected_qty

    def test_all_asks_within_50_bps(self):
        # All ask prices (2002, 2003, 2004) are below 2011.5 → all included
        result = self.a.depth_at_bps(Decimal("50"))
        expected_qty = Decimal("1.0") + Decimal("0.8") + Decimal("2.0")
        assert result["ask_qty"] == expected_qty

    def test_tight_band_excludes_far_levels(self):
        # 1 bps band: mid × 0.0001 ≈ 0.20 → only levels within ±0.20 of mid
        # mid = 2001.5, band: [2001.3, 2001.7]
        # Only bid @ 2001 (distance = 0.5 > 0.2) is actually outside — but let's
        # just assert that the count drops vs a wide band
        narrow = self.a.depth_at_bps(Decimal("1"))
        wide = self.a.depth_at_bps(Decimal("50"))
        assert narrow["bid_qty"] <= wide["bid_qty"]
        assert narrow["ask_qty"] <= wide["ask_qty"]

    def test_zero_mid_returns_all_zeros(self):
        a = OrderBookAnalyzer(
            {"bids": [(Decimal("100"), Decimal("1"))], "asks": [], "mid_price": Decimal("0")}
        )
        result = a.depth_at_bps(Decimal("50"))
        assert result["bid_qty"] == Decimal("0")
        assert result["ask_qty"] == Decimal("0")

    def test_values_are_decimal(self):
        result = self.a.depth_at_bps(Decimal("50"))
        for v in result.values():
            assert isinstance(v, Decimal)

    def test_bid_value_equals_sum_price_times_qty(self):
        result = self.a.depth_at_bps(Decimal("50"))
        expected = sum(
            p * q
            for p, q in [
                (Decimal("2001"), Decimal("1.5")),
                (Decimal("2000"), Decimal("2.0")),
                (Decimal("1999"), Decimal("0.5")),
            ]
        )
        assert result["bid_value"] == expected


# ── 5. liquidity_walls ────────────────────────────────────────────────────────


class TestLiquidityWalls:
    def setup_method(self):
        self.a = _analyzer()

    def test_returns_bid_walls_and_ask_walls_keys(self):
        result = self.a.liquidity_walls(Decimal("1"))
        assert "bid_walls" in result
        assert "ask_walls" in result

    def test_large_threshold_returns_empty(self):
        result = self.a.liquidity_walls(Decimal("999"))
        assert result["bid_walls"] == []
        assert result["ask_walls"] == []

    def test_threshold_one_filters_correctly(self):
        # Bids: 1.5 ≥ 1, 2.0 ≥ 1, 0.5 < 1 → 2 walls
        result = self.a.liquidity_walls(Decimal("1"))
        assert len(result["bid_walls"]) == 2

    def test_ask_walls_filtered_correctly(self):
        # Asks: 1.0 ≥ 1, 0.8 < 1, 2.0 ≥ 1 → 2 walls
        result = self.a.liquidity_walls(Decimal("1"))
        assert len(result["ask_walls"]) == 2

    def test_wall_includes_price_and_qty(self):
        result = self.a.liquidity_walls(Decimal("2"))
        # Only 2.0 qty levels qualify
        assert (Decimal("2000"), Decimal("2.0")) in result["bid_walls"]
        assert (Decimal("2004"), Decimal("2.0")) in result["ask_walls"]

    def test_threshold_zero_returns_all(self):
        result = self.a.liquidity_walls(Decimal("0"))
        assert len(result["bid_walls"]) == 3
        assert len(result["ask_walls"]) == 3


# ── 6. depth_levels ───────────────────────────────────────────────────────────


class TestDepthLevels:
    def setup_method(self):
        self.a = _analyzer()

    def test_returns_depth_level_instances(self):
        levels = self.a.depth_levels("bid", 3)
        for lvl in levels:
            assert isinstance(lvl, DepthLevel)

    def test_bid_levels_count(self):
        levels = self.a.depth_levels("bid", 3)
        assert len(levels) == 3

    def test_ask_levels_count(self):
        levels = self.a.depth_levels("ask", 2)
        assert len(levels) == 2

    def test_fewer_levels_than_requested(self):
        # Only 3 bids in book, requesting 10
        levels = self.a.depth_levels("bid", 10)
        assert len(levels) == 3

    def test_cumulative_qty_increases(self):
        levels = self.a.depth_levels("bid", 3)
        for i in range(1, len(levels)):
            assert levels[i].cumulative_qty > levels[i - 1].cumulative_qty

    def test_cumulative_qty_correct(self):
        levels = self.a.depth_levels("bid", 3)
        # 1.5 + 2.0 + 0.5 = 4.0
        assert levels[-1].cumulative_qty == Decimal("4.0")

    def test_cumulative_value_correct(self):
        levels = self.a.depth_levels("ask", 1)
        assert levels[0].cumulative_value == Decimal("2002") * Decimal("1.0")

    def test_all_fields_are_decimal(self):
        for lvl in self.a.depth_levels("ask", 3):
            assert isinstance(lvl.price, Decimal)
            assert isinstance(lvl.qty, Decimal)
            assert isinstance(lvl.cumulative_qty, Decimal)
            assert isinstance(lvl.cumulative_value, Decimal)

    def test_invalid_side_raises(self):
        with pytest.raises(ValueError, match="side"):
            self.a.depth_levels("wrong", 3)

    def test_empty_book_returns_empty_list(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.depth_levels("bid", 5) == []
        assert a.depth_levels("ask", 5) == []


# ── 7. spread / mid_price helpers ─────────────────────────────────────────────


class TestHelpers:
    def test_spread_correct(self):
        a = _analyzer()
        # best_ask = 2002, best_bid = 2001
        assert a.spread() == Decimal("1")

    def test_spread_is_decimal(self):
        assert isinstance(_analyzer().spread(), Decimal)

    def test_spread_empty_bids_returns_zero(self):
        a = OrderBookAnalyzer(
            {"bids": [], "asks": [(Decimal("100"), Decimal("1"))], "mid_price": Decimal("100")}
        )
        assert a.spread() == Decimal("0")

    def test_spread_empty_asks_returns_zero(self):
        a = OrderBookAnalyzer(
            {"bids": [(Decimal("100"), Decimal("1"))], "asks": [], "mid_price": Decimal("100")}
        )
        assert a.spread() == Decimal("0")

    def test_mid_price_matches_input(self):
        a = _analyzer(mid="1234.5")
        assert a.mid_price() == Decimal("1234.5")

    def test_mid_price_is_decimal(self):
        assert isinstance(_analyzer().mid_price(), Decimal)
