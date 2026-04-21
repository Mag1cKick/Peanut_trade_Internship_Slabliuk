"""Tests for strategy/scorer.py"""

from __future__ import annotations

import time

import pytest

from strategy.scorer import ScorerConfig, SignalScorer
from strategy.signal import Direction, Signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    spread_bps: float = 100.0,
    pair: str = "ETH/USDT",
    score: float = 0.0,
    ttl: float = 5.0,
) -> Signal:
    now = time.time()
    return Signal(
        signal_id="test_signal",
        pair=pair,
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=2000.0,
        dex_price=2016.0,
        spread_bps=spread_bps,
        size=1.0,
        expected_gross_pnl=20.0,
        expected_fees=9.0,
        expected_net_pnl=11.0,
        score=score,
        timestamp=now,
        expiry=now + ttl,
        inventory_ok=True,
        within_limits=True,
    )


_NO_SKEW: list[dict] = []
_SKEW_OK = [{"asset": "ETH", "needs_rebalance": False}]
_SKEW_RED = [{"asset": "ETH", "needs_rebalance": True}]


# ---------------------------------------------------------------------------
# ScorerConfig tests
# ---------------------------------------------------------------------------


class TestScorerConfig:
    def test_defaults(self):
        cfg = ScorerConfig()
        assert cfg.spread_weight == 0.4
        assert cfg.liquidity_weight == 0.2
        assert cfg.inventory_weight == 0.2
        assert cfg.history_weight == 0.2
        assert cfg.excellent_spread_bps == 100.0
        assert cfg.min_spread_bps == 30.0

    def test_custom_valid(self):
        cfg = ScorerConfig(
            spread_weight=0.5,
            liquidity_weight=0.1,
            inventory_weight=0.2,
            history_weight=0.2,
        )
        assert cfg.spread_weight == 0.5

    def test_weights_not_summing_to_one_raises(self):
        with pytest.raises(ValueError, match="sum to 1"):
            ScorerConfig(
                spread_weight=0.5,
                liquidity_weight=0.5,
                inventory_weight=0.5,
                history_weight=0.5,
            )

    def test_negative_weight_raises(self):
        with pytest.raises(ValueError, match=">="):
            ScorerConfig(
                spread_weight=-0.1,
                liquidity_weight=0.4,
                inventory_weight=0.4,
                history_weight=0.3,
            )

    def test_excellent_lte_min_raises(self):
        with pytest.raises(ValueError, match="excellent_spread_bps"):
            ScorerConfig(excellent_spread_bps=30.0, min_spread_bps=30.0)

    def test_excellent_below_min_raises(self):
        with pytest.raises(ValueError, match="excellent_spread_bps"):
            ScorerConfig(excellent_spread_bps=20.0, min_spread_bps=30.0)


# ---------------------------------------------------------------------------
# _score_spread
# ---------------------------------------------------------------------------


class TestScoreSpread:
    def setup_method(self):
        self.scorer = SignalScorer()

    def test_below_min_returns_zero(self):
        assert self.scorer._score_spread(10.0) == 0.0

    def test_at_min_returns_zero(self):
        assert self.scorer._score_spread(30.0) == 0.0

    def test_at_excellent_returns_100(self):
        assert self.scorer._score_spread(100.0) == 100.0

    def test_above_excellent_capped_at_100(self):
        assert self.scorer._score_spread(200.0) == 100.0

    def test_midpoint(self):
        # min=30, excellent=100 → midpoint=65 → score=50
        assert self.scorer._score_spread(65.0) == pytest.approx(50.0)

    def test_linear_interpolation(self):
        # 30 bps above min out of 70 bps range → ~42.86
        score = self.scorer._score_spread(60.0)
        expected = (60.0 - 30.0) / (100.0 - 30.0) * 100.0
        assert score == pytest.approx(expected)

    def test_custom_range(self):
        cfg = ScorerConfig(
            excellent_spread_bps=200.0,
            min_spread_bps=50.0,
            spread_weight=0.4,
            liquidity_weight=0.2,
            inventory_weight=0.2,
            history_weight=0.2,
        )
        scorer = SignalScorer(cfg)
        assert scorer._score_spread(125.0) == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# _score_inventory
# ---------------------------------------------------------------------------


class TestScoreInventory:
    def setup_method(self):
        self.scorer = SignalScorer()

    def test_no_skew_returns_60(self):
        sig = _make_signal()
        assert self.scorer._score_inventory(sig, _NO_SKEW) == 60.0

    def test_ok_skew_returns_60(self):
        sig = _make_signal()
        assert self.scorer._score_inventory(sig, _SKEW_OK) == 60.0

    def test_red_skew_returns_20(self):
        sig = _make_signal()
        assert self.scorer._score_inventory(sig, _SKEW_RED) == 20.0

    def test_unrelated_asset_ignored(self):
        sig = _make_signal(pair="ETH/USDT")
        skews = [{"asset": "BTC", "needs_rebalance": True}]
        assert self.scorer._score_inventory(sig, skews) == 60.0

    def test_mixed_skew_any_true_returns_20(self):
        sig = _make_signal()
        skews = [
            {"asset": "ETH", "needs_rebalance": False},
            {"asset": "ETH", "needs_rebalance": True},
        ]
        assert self.scorer._score_inventory(sig, skews) == 20.0


# ---------------------------------------------------------------------------
# _score_history
# ---------------------------------------------------------------------------


class TestScoreHistory:
    def setup_method(self):
        self.scorer = SignalScorer()

    def test_no_history_returns_50(self):
        assert self.scorer._score_history("ETH/USDT") == 50.0

    def test_fewer_than_3_returns_50(self):
        self.scorer.record_result("ETH/USDT", True)
        self.scorer.record_result("ETH/USDT", True)
        assert self.scorer._score_history("ETH/USDT") == 50.0

    def test_all_wins_returns_100(self):
        for _ in range(5):
            self.scorer.record_result("ETH/USDT", True)
        assert self.scorer._score_history("ETH/USDT") == pytest.approx(100.0)

    def test_all_losses_returns_0(self):
        for _ in range(5):
            self.scorer.record_result("ETH/USDT", False)
        assert self.scorer._score_history("ETH/USDT") == pytest.approx(0.0)

    def test_mixed_results(self):
        wins = [True, True, False, True, False]
        for r in wins:
            self.scorer.record_result("ETH/USDT", r)
        assert self.scorer._score_history("ETH/USDT") == pytest.approx(60.0)

    def test_different_pairs_isolated(self):
        for _ in range(5):
            self.scorer.record_result("BTC/USDT", False)
        assert self.scorer._score_history("ETH/USDT") == 50.0

    def test_only_last_20_used(self):
        # 25 results: first 5 are losses, last 20 are all wins
        for _ in range(5):
            self.scorer.record_result("ETH/USDT", False)
        for _ in range(20):
            self.scorer.record_result("ETH/USDT", True)
        assert self.scorer._score_history("ETH/USDT") == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# record_result
# ---------------------------------------------------------------------------


class TestRecordResult:
    def test_record_appends(self):
        scorer = SignalScorer()
        scorer.record_result("ETH/USDT", True)
        assert scorer.recent_results == [("ETH/USDT", True)]

    def test_capped_at_100(self):
        scorer = SignalScorer()
        for i in range(150):
            scorer.record_result("ETH/USDT", i % 2 == 0)
        assert len(scorer.recent_results) == 100

    def test_oldest_dropped(self):
        scorer = SignalScorer()
        for i in range(100):
            scorer.record_result("ETH/USDT", True)
        scorer.record_result("ETH/USDT", False)
        assert len(scorer.recent_results) == 100
        assert scorer.recent_results[-1] == ("ETH/USDT", False)


# ---------------------------------------------------------------------------
# score (composite + required tests)
# ---------------------------------------------------------------------------


class TestScore:
    def setup_method(self):
        self.scorer = SignalScorer()

    def test_score_high_spread(self):
        """Required: 100 bps spread should produce a high score."""
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _NO_SKEW)
        assert s > 60.0

    def test_score_inventory_penalty(self):
        """Required: RED skew should lower the score."""
        sig_ok = _make_signal(spread_bps=100.0)
        sig_red = _make_signal(spread_bps=100.0)
        s_ok = self.scorer.score(sig_ok, _SKEW_OK)
        s_red = self.scorer.score(sig_red, _SKEW_RED)
        assert s_red < s_ok

    def test_score_stored_on_signal(self):
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _NO_SKEW)
        assert sig.score == s

    def test_score_clamped_to_0_100(self):
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _NO_SKEW)
        assert 0.0 <= s <= 100.0

    def test_score_rounded_to_one_decimal(self):
        sig = _make_signal(spread_bps=65.0)
        s = self.scorer.score(sig, _NO_SKEW)
        assert s == round(s, 1)

    def test_below_min_spread_scores_low(self):
        sig = _make_signal(spread_bps=10.0)
        s = self.scorer.score(sig, _NO_SKEW)
        # spread=0, liquidity=80, inventory=60, history=50
        # weighted = 0*0.4 + 80*0.2 + 60*0.2 + 50*0.2 = 38.0
        assert s == pytest.approx(38.0)

    def test_known_composite_value(self):
        # spread=100→score=100, liq=80, inv=60, hist=50 (no history)
        # 100*0.4 + 80*0.2 + 60*0.2 + 50*0.2 = 40+16+12+10 = 78
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _NO_SKEW)
        assert s == pytest.approx(78.0)

    def test_with_history_affects_score(self):
        for _ in range(5):
            self.scorer.record_result("ETH/USDT", True)
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _NO_SKEW)
        # hist=100 → 100*0.4 + 80*0.2 + 60*0.2 + 100*0.2 = 40+16+12+20 = 88
        assert s == pytest.approx(88.0)

    def test_score_with_red_inventory_known_value(self):
        # spread=100→100, liq=80, inv=20, hist=50
        # 100*0.4 + 80*0.2 + 20*0.2 + 50*0.2 = 40+16+4+10 = 70
        sig = _make_signal(spread_bps=100.0)
        s = self.scorer.score(sig, _SKEW_RED)
        assert s == pytest.approx(70.0)


# ---------------------------------------------------------------------------
# apply_decay (required test + edge cases)
# ---------------------------------------------------------------------------


class TestApplyDecay:
    def test_decay_over_time(self):
        """Required: older signals should have lower score after decay."""
        now = time.time()
        ttl = 10.0
        fresh = Signal(
            signal_id="fresh",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=2000.0,
            dex_price=2016.0,
            spread_bps=100.0,
            size=1.0,
            expected_gross_pnl=20.0,
            expected_fees=9.0,
            expected_net_pnl=11.0,
            score=80.0,
            timestamp=now,
            expiry=now + ttl,
            inventory_ok=True,
            within_limits=True,
        )
        # simulate an older signal (8s old out of 10s ttl)
        old = Signal(
            signal_id="old",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=2000.0,
            dex_price=2016.0,
            spread_bps=100.0,
            size=1.0,
            expected_gross_pnl=20.0,
            expected_fees=9.0,
            expected_net_pnl=11.0,
            score=80.0,
            timestamp=now - 8.0,
            expiry=now - 8.0 + ttl,
            inventory_ok=True,
            within_limits=True,
        )
        scorer = SignalScorer()
        assert scorer.apply_decay(fresh) > scorer.apply_decay(old)

    def test_fresh_signal_no_decay(self):
        """A brand-new signal should have decay_factor ≈ 1.0."""
        sig = _make_signal(score=80.0, ttl=10.0)
        scorer = SignalScorer()
        decayed = scorer.apply_decay(sig)
        # age ≈ 0 → factor ≈ 1.0
        assert decayed == pytest.approx(80.0, abs=0.5)

    def test_halfway_expired_has_75_percent_score(self):
        """At 50% of TTL elapsed, decay_factor = 1 - 0.5*0.5 = 0.75."""
        ttl = 10.0
        now = time.time()
        sig = Signal(
            signal_id="t",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=2000.0,
            dex_price=2016.0,
            spread_bps=100.0,
            size=1.0,
            expected_gross_pnl=20.0,
            expected_fees=9.0,
            expected_net_pnl=11.0,
            score=80.0,
            timestamp=now - 5.0,
            expiry=now - 5.0 + ttl,
            inventory_ok=True,
            within_limits=True,
        )
        scorer = SignalScorer()
        decayed = scorer.apply_decay(sig)
        assert decayed == pytest.approx(80.0 * 0.75, abs=0.5)

    def test_fully_expired_has_50_percent_score(self):
        """At 100% of TTL elapsed, decay_factor = 0.5 (not 0)."""
        now = time.time()
        sig = Signal(
            signal_id="t",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=2000.0,
            dex_price=2016.0,
            spread_bps=100.0,
            size=1.0,
            expected_gross_pnl=20.0,
            expected_fees=9.0,
            expected_net_pnl=11.0,
            score=80.0,
            timestamp=now - 10.0,  # ttl=10s, fully elapsed
            expiry=now,
            inventory_ok=True,
            within_limits=True,
        )
        scorer = SignalScorer()
        decayed = scorer.apply_decay(sig)
        assert decayed == pytest.approx(40.0, abs=0.5)

    def test_zero_ttl_returns_zero(self):
        """A signal with zero TTL should return 0."""
        now = time.time()
        sig = Signal(
            signal_id="t",
            pair="ETH/USDT",
            direction=Direction.BUY_CEX_SELL_DEX,
            cex_price=2000.0,
            dex_price=2016.0,
            spread_bps=100.0,
            size=1.0,
            expected_gross_pnl=20.0,
            expected_fees=9.0,
            expected_net_pnl=11.0,
            score=80.0,
            timestamp=now,
            expiry=now,
            inventory_ok=True,
            within_limits=True,
        )
        scorer = SignalScorer()
        assert scorer.apply_decay(sig) == 0.0

    def test_decay_does_not_mutate_signal(self):
        sig = _make_signal(score=80.0)
        scorer = SignalScorer()
        scorer.apply_decay(sig)
        assert sig.score == 80.0
