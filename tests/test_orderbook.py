"""
tests/test_orderbook.py — Unit tests for exchange.orderbook.OrderBookAnalyzer

All tests use hand-crafted order book dicts — no network calls.

Test groups:
  1. walk_the_book  — exact fill, multi-level, insufficient liquidity, slippage, fills detail
  2. depth_at_bps   — correct depth at various bandwidths, empty book
  3. imbalance      — range guarantee, bid-heavy, ask-heavy, balanced, depth param
  4. effective_spread — > quoted spread, zero when empty, round-trip cost
  5. Properties     — mid_price, best_bid/ask, quoted_spread_bps, symbol, timestamp
  6. Input validation — bad side, zero/negative qty
  7. CLI            — _print_analysis smoke test
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from exchange.orderbook import OrderBookAnalyzer, _print_analysis

# ── Helpers ────────────────────────────────────────────────────────────────────


def _book(
    bids: list | None = None,
    asks: list | None = None,
    symbol: str = "ETH/USDT",
    mid: str | None = None,
) -> dict:
    """Build a minimal order book dict as returned by ExchangeClient."""
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
    mid_price = Decimal(mid) if mid else (bids[0][0] + asks[0][0]) / Decimal("2")
    return {
        "symbol": symbol,
        "timestamp": 1700000000000,
        "bids": bids,
        "asks": asks,
        "best_bid": bids[0] if bids else (Decimal("0"), Decimal("0")),
        "best_ask": asks[0] if asks else (Decimal("0"), Decimal("0")),
        "mid_price": mid_price,
    }


def _analyzer(**kw) -> OrderBookAnalyzer:
    return OrderBookAnalyzer(_book(**kw))


# ══════════════════════════════════════════════════════════════════════════════
# 1. walk_the_book
# ══════════════════════════════════════════════════════════════════════════════


class TestWalkTheBook:
    def test_exact_fill_single_level_buy(self):
        """Fill exactly at one price level."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=1.0)
        assert result["fully_filled"] is True
        assert result["avg_price"] == Decimal("2002")
        assert result["levels_consumed"] == 1

    def test_exact_fill_single_level_sell(self):
        a = _analyzer()
        result = a.walk_the_book("sell", qty=1.5)
        assert result["fully_filled"] is True
        assert result["avg_price"] == Decimal("2001")
        assert result["levels_consumed"] == 1

    def test_multiple_levels_avg_price_correct(self):
        """Fill across multiple price levels — avg price is volume-weighted."""
        a = _analyzer()
        # Buy 1.8: 1.0 @ 2002, 0.8 @ 2003
        result = a.walk_the_book("buy", qty=1.8)
        assert result["fully_filled"] is True
        assert result["levels_consumed"] == 2
        expected_avg = (Decimal("2002") * 1 + Decimal("2003") * Decimal("0.8")) / Decimal("1.8")
        assert abs(result["avg_price"] - expected_avg) < Decimal("1e-10")

    def test_multiple_levels_sell(self):
        a = _analyzer()
        # Sell 2.0: 1.5 @ 2001, 0.5 @ 2000
        result = a.walk_the_book("sell", qty=2.0)
        assert result["fully_filled"] is True
        assert result["levels_consumed"] == 2
        expected_avg = (
            Decimal("2001") * Decimal("1.5") + Decimal("2000") * Decimal("0.5")
        ) / Decimal("2.0")
        assert abs(result["avg_price"] - expected_avg) < Decimal("1e-10")

    def test_insufficient_liquidity_returns_false(self):
        """Returns fully_filled=False when book is too thin."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=100.0)
        assert result["fully_filled"] is False

    def test_insufficient_fills_show_available(self):
        """When not fully filled, fills contain what IS available."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=100.0)
        available_qty = sum(f["qty"] for f in result["fills"])
        book_qty = sum(q for _, q in _book()["asks"])
        assert available_qty == book_qty

    def test_total_cost_equals_sum_of_fill_costs(self):
        a = _analyzer()
        result = a.walk_the_book("buy", qty=2.5)
        assert result["total_cost"] == sum(f["cost"] for f in result["fills"])

    def test_slippage_zero_at_single_level(self):
        """No slippage when one level fills the entire order."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=0.5)  # well within first ask level
        assert result["slippage_bps"] == Decimal("0")

    def test_slippage_positive_multi_level_buy(self):
        """Slippage is positive when filling crosses multiple ask levels."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=1.8)
        assert result["slippage_bps"] > Decimal("0")

    def test_slippage_positive_multi_level_sell(self):
        a = _analyzer()
        result = a.walk_the_book("sell", qty=2.0)
        assert result["slippage_bps"] > Decimal("0")

    def test_fills_list_structure(self):
        """Each fill dict has price, qty, cost."""
        a = _analyzer()
        result = a.walk_the_book("buy", qty=1.8)
        for fill in result["fills"]:
            assert "price" in fill
            assert "qty" in fill
            assert "cost" in fill
            assert isinstance(fill["price"], Decimal)
            assert isinstance(fill["qty"], Decimal)
            assert isinstance(fill["cost"], Decimal)

    def test_fill_cost_equals_price_times_qty(self):
        a = _analyzer()
        result = a.walk_the_book("buy", qty=1.8)
        for fill in result["fills"]:
            assert fill["cost"] == fill["price"] * fill["qty"]

    def test_all_return_values_are_decimal(self):
        a = _analyzer()
        result = a.walk_the_book("buy", qty=1.0)
        assert isinstance(result["avg_price"], Decimal)
        assert isinstance(result["total_cost"], Decimal)
        assert isinstance(result["slippage_bps"], Decimal)

    def test_empty_book_not_filled(self):
        a = OrderBookAnalyzer(
            {"bids": [], "asks": [], "mid_price": Decimal("0"), "best_bid": None, "best_ask": None}
        )
        result = a.walk_the_book("buy", qty=1.0)
        assert result["fully_filled"] is False
        assert result["avg_price"] == Decimal("0")

    def test_invalid_side_raises(self):
        a = _analyzer()
        with pytest.raises(ValueError, match="side"):
            a.walk_the_book("long", qty=1.0)

    def test_zero_qty_raises(self):
        a = _analyzer()
        with pytest.raises(ValueError, match="positive"):
            a.walk_the_book("buy", qty=0)

    def test_negative_qty_raises(self):
        a = _analyzer()
        with pytest.raises(ValueError, match="positive"):
            a.walk_the_book("sell", qty=-1.0)


# ══════════════════════════════════════════════════════════════════════════════
# 2. depth_at_bps
# ══════════════════════════════════════════════════════════════════════════════


class TestDepthAtBps:
    def test_bid_depth_10_bps_correct(self):
        """Depth at 10 bps matches manual calculation."""
        a = _analyzer()
        # best bid = 2001, threshold = 2001 × (1 - 0.001) = 1999.001
        # All bids (2001, 2000, 1999) are above 1999.001 → only 2001 and 2000 qualify
        # Actually 1999 × (1-0.001) = 1998.999 — let me recalc
        # threshold = 2001 × (1 - 10/10000) = 2001 × 0.999 = 1998.999
        # Bids: 2001 >= 1998.999 ✓, 2000 >= 1998.999 ✓, 1999 >= 1998.999 ✓
        depth = a.depth_at_bps("bid", 10)
        expected = Decimal("1.5") + Decimal("2.0") + Decimal("0.5")
        assert depth == expected

    def test_ask_depth_10_bps_correct(self):
        a = _analyzer()
        # best ask = 2002, threshold = 2002 × (1 + 10/10000) = 2002 × 1.001 = 2004.002
        # Asks: 2002 <= 2004.002 ✓, 2003 <= 2004.002 ✓, 2004 <= 2004.002 ✓
        depth = a.depth_at_bps("ask", 10)
        expected = Decimal("1.0") + Decimal("0.8") + Decimal("2.0")
        assert depth == expected

    def test_tight_band_excludes_far_levels(self):
        """Very tight band (1 bps) only captures best level."""
        # best bid = 2001, threshold = 2001 × 0.9999 = 2000.7999
        # Only bids >= 2000.7999: 2001 qualifies, 2000 does not
        a = _analyzer()
        depth = a.depth_at_bps("bid", 1)
        assert depth == Decimal("1.5")  # only first bid level

    def test_wide_band_captures_all(self):
        a = _analyzer()
        depth = a.depth_at_bps("ask", 200)
        total_asks = sum(q for _, q in _book()["asks"])
        assert depth == total_asks

    def test_empty_bid_book_returns_zero(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.depth_at_bps("bid", 10) == Decimal("0")

    def test_empty_ask_book_returns_zero(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.depth_at_bps("ask", 10) == Decimal("0")

    def test_returns_decimal(self):
        a = _analyzer()
        assert isinstance(a.depth_at_bps("bid", 10), Decimal)
        assert isinstance(a.depth_at_bps("ask", 10), Decimal)

    def test_invalid_side_raises(self):
        a = _analyzer()
        with pytest.raises(ValueError, match="side"):
            a.depth_at_bps("buy", 10)


# ══════════════════════════════════════════════════════════════════════════════
# 3. imbalance
# ══════════════════════════════════════════════════════════════════════════════


class TestImbalance:
    def test_imbalance_range(self):
        """Imbalance always in [-1.0, +1.0]."""
        for _ in range(5):
            a = _analyzer()
            assert -1.0 <= a.imbalance() <= 1.0

    def test_balanced_book_near_zero(self):
        bids = [(Decimal("100"), Decimal("1"))]
        asks = [(Decimal("101"), Decimal("1"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        assert a.imbalance() == 0.0

    def test_bid_heavy_positive(self):
        bids = [(Decimal("100"), Decimal("3"))]
        asks = [(Decimal("101"), Decimal("1"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        assert a.imbalance() > 0.0

    def test_ask_heavy_negative(self):
        bids = [(Decimal("100"), Decimal("1"))]
        asks = [(Decimal("101"), Decimal("3"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": asks, "mid_price": Decimal("100.5")})
        assert a.imbalance() < 0.0

    def test_all_bids_returns_one(self):
        bids = [(Decimal("100"), Decimal("5"))]
        a = OrderBookAnalyzer({"bids": bids, "asks": [], "mid_price": Decimal("100")})
        assert a.imbalance() == 1.0

    def test_all_asks_returns_minus_one(self):
        asks = [(Decimal("101"), Decimal("5"))]
        a = OrderBookAnalyzer({"bids": [], "asks": asks, "mid_price": Decimal("101")})
        assert a.imbalance() == -1.0

    def test_empty_book_returns_zero(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.imbalance() == 0.0

    def test_depth_param_limits_levels(self):
        """Depth parameter limits how many levels are considered."""
        a = _analyzer()
        imbal_1 = a.imbalance(levels=1)
        imbal_10 = a.imbalance(levels=10)
        # With levels=1: bids=1.5, asks=1.0 → positive imbalance
        assert imbal_1 > 0
        # More levels changes the value
        assert imbal_1 != imbal_10

    def test_returns_float(self):
        a = _analyzer()
        assert isinstance(a.imbalance(), float)


# ══════════════════════════════════════════════════════════════════════════════
# 4. effective_spread
# ══════════════════════════════════════════════════════════════════════════════


class TestEffectiveSpread:
    def test_effective_spread_greater_than_quoted(self):
        """Effective spread >= quoted spread for any qty > 0."""
        a = _analyzer()
        eff = a.effective_spread(qty=2.0)
        quoted = a.quoted_spread_bps
        assert eff >= quoted

    def test_effective_spread_increases_with_qty(self):
        """Larger qty → more levels crossed → higher effective spread."""
        a = _analyzer()
        eff_small = a.effective_spread(qty=0.5)
        eff_large = a.effective_spread(qty=3.5)
        assert eff_large >= eff_small

    def test_effective_spread_zero_when_empty(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.effective_spread(qty=1.0) == Decimal("0")

    def test_effective_spread_returns_decimal(self):
        a = _analyzer()
        assert isinstance(a.effective_spread(qty=1.0), Decimal)

    def test_effective_spread_positive(self):
        """Effective spread must be non-negative."""
        a = _analyzer()
        assert a.effective_spread(qty=1.0) >= Decimal("0")

    def test_effective_spread_zero_mid_returns_zero(self):
        book = _book(mid="0")
        a = OrderBookAnalyzer(book)
        # Mid is 0 → effective spread undefined → return 0
        assert a.effective_spread(qty=0.1) == Decimal("0")


# ══════════════════════════════════════════════════════════════════════════════
# 5. Properties
# ══════════════════════════════════════════════════════════════════════════════


class TestProperties:
    def setup_method(self):
        self.a = _analyzer()

    def test_symbol(self):
        assert self.a.symbol == "ETH/USDT"

    def test_timestamp(self):
        assert self.a.timestamp == 1700000000000

    def test_mid_price(self):
        assert self.a.mid_price == Decimal("2001.5")

    def test_best_bid(self):
        assert self.a.best_bid[0] == Decimal("2001")

    def test_best_ask(self):
        assert self.a.best_ask[0] == Decimal("2002")

    def test_quoted_spread_bps_correct(self):
        # (2002 - 2001) / 2001.5 * 10000 ≈ 4.997 bps
        spread = self.a.quoted_spread_bps
        expected = (Decimal("2002") - Decimal("2001")) / Decimal("2001.5") * Decimal("10000")
        assert abs(spread - expected) < Decimal("0.01")

    def test_quoted_spread_zero_when_empty(self):
        a = OrderBookAnalyzer({"bids": [], "asks": [], "mid_price": Decimal("0")})
        assert a.quoted_spread_bps == Decimal("0")

    def test_missing_timestamp_defaults_zero(self):
        book = {"bids": [], "asks": [], "mid_price": Decimal("0")}
        a = OrderBookAnalyzer(book)
        assert a.timestamp == 0

    def test_missing_symbol_defaults_empty(self):
        book = {"bids": [], "asks": [], "mid_price": Decimal("0")}
        a = OrderBookAnalyzer(book)
        assert a.symbol == ""


# ══════════════════════════════════════════════════════════════════════════════
# 6. CLI smoke test
# ══════════════════════════════════════════════════════════════════════════════


class TestCLIPrintAnalysis:
    def test_print_analysis_runs_without_error(self, capsys):
        """_print_analysis should not raise and produce non-empty output."""
        a = _analyzer()
        _print_analysis(a, qty_small=2.0, qty_large=10.0)
        captured = capsys.readouterr()
        assert "ETH/USDT" in captured.out
        assert "Spread" in captured.out
        assert "Imbalance" in captured.out

    def test_print_analysis_shows_walk_results(self, capsys):
        a = _analyzer()
        _print_analysis(a, qty_small=1.0, qty_large=3.0)
        captured = capsys.readouterr()
        assert "Walk-the-book" in captured.out

    def test_print_analysis_insufficient_liquidity(self, capsys):
        a = _analyzer()
        _print_analysis(a, qty_small=2.0, qty_large=1000.0)
        captured = capsys.readouterr()
        assert "INSUFFICIENT" in captured.out

    def test_print_analysis_shows_effective_spread(self, capsys):
        a = _analyzer()
        _print_analysis(a, qty_small=1.0, qty_large=2.0)
        captured = capsys.readouterr()
        assert "Effective spread" in captured.out
