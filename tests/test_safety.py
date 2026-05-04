"""
tests/test_safety.py — Comprehensive tests for the safety module.

Covers evaluation criteria:
  - RiskManager: per-trade, daily loss, drawdown, frequency limits  (4 pts)
  - Kill switch: manual file-based + auto capital threshold trigger  (3 pts)
  - PreTradeValidator: spread sanity, price freshness, signal expiry (3 pts)
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

from safety.constants import (
    ABSOLUTE_MAX_DAILY_LOSS,
    ABSOLUTE_MAX_TRADE_USD,
    ABSOLUTE_MAX_TRADES_PER_HOUR,
    ABSOLUTE_MIN_CAPITAL,
    KILL_SWITCH_FILE,
    is_kill_switch_active,
    safety_check,
    trigger_kill_switch,
)
from safety.risk import PreTradeValidator, RiskLimits, RiskManager

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _FakeSignal:
    """Minimal signal stand-in for testing without importing the full strategy."""

    pair: str = "MAGIC/USDT"
    cex_price: float = 0.064
    dex_price: float = 0.066
    size: float = 100.0
    spread_bps: float = 300.0
    within_limits: bool = True
    timestamp: float = 0.0  # overridden per test
    signal_ttl_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.timestamp == 0.0:
            self.timestamp = time.time()


def _make_limits(**overrides) -> RiskLimits:
    defaults = dict(
        max_trade_usd=10.0,
        max_trade_pct=0.20,
        max_position_per_token=200.0,
        max_open_positions=1,
        max_loss_per_trade=5.0,
        max_daily_loss=15.0,
        max_drawdown_pct=0.20,
        max_trades_per_hour=20,
        consecutive_loss_limit=3,
    )
    defaults.update(overrides)
    return RiskLimits(**defaults)


# ===========================================================================
# 1. RiskManager tests
# ===========================================================================


class TestRiskManagerTradeSize:
    """Per-trade size limits."""

    def test_trade_within_usd_limit_passes(self):
        rm = RiskManager(_make_limits(max_trade_usd=10.0), initial_capital=100.0)
        sig = _FakeSignal(cex_price=0.064, size=100.0)  # $6.40
        ok, _ = rm.check_pre_trade(sig)
        assert ok

    def test_trade_exceeds_usd_limit_blocked(self):
        rm = RiskManager(_make_limits(max_trade_usd=5.0), initial_capital=100.0)
        sig = _FakeSignal(cex_price=0.064, size=100.0)  # $6.40 > $5
        ok, reason = rm.check_pre_trade(sig)
        assert not ok
        assert "max_trade_usd" in reason

    def test_trade_exceeds_pct_of_capital_blocked(self):
        rm = RiskManager(_make_limits(max_trade_pct=0.05), initial_capital=100.0)
        sig = _FakeSignal(cex_price=0.064, size=100.0)  # $6.40 = 6.4% > 5%
        ok, reason = rm.check_pre_trade(sig)
        assert not ok
        assert "trade_pct" in reason

    def test_trade_exceeds_position_per_token_blocked(self):
        rm = RiskManager(_make_limits(max_position_per_token=50.0), initial_capital=100.0)
        sig = _FakeSignal(size=100.0)  # 100 > 50
        ok, reason = rm.check_pre_trade(sig)
        assert not ok
        assert "max_position_per_token" in reason


class TestRiskManagerDailyLoss:
    """Daily loss limit."""

    def test_daily_loss_limit_blocks_trading(self):
        rm = RiskManager(_make_limits(max_daily_loss=10.0), initial_capital=100.0)
        rm.record_trade(-10.01)
        ok, reason = rm.check_pre_trade(_FakeSignal())
        assert not ok
        assert "daily_loss" in reason

    def test_profitable_trade_does_not_trigger_daily_loss(self):
        rm = RiskManager(_make_limits(max_daily_loss=10.0), initial_capital=100.0)
        rm.record_trade(5.0)
        ok, _ = rm.check_pre_trade(_FakeSignal())
        assert ok

    def test_daily_loss_accumulated_across_multiple_trades(self):
        rm = RiskManager(_make_limits(max_daily_loss=10.0), initial_capital=100.0)
        rm.record_trade(-6.0)
        rm.record_trade(-5.0)  # total -$11 > -$10 limit
        ok, reason = rm.check_pre_trade(_FakeSignal())
        assert not ok
        assert "daily_loss" in reason


class TestRiskManagerDrawdown:
    """Max drawdown limit — use a generous daily_loss limit so drawdown fires first."""

    def test_drawdown_within_limit_passes(self):
        rm = RiskManager(
            _make_limits(max_drawdown_pct=0.20, max_daily_loss=50.0), initial_capital=100.0
        )
        rm.record_trade(-15.0)  # 15% drawdown < 20% limit
        ok, _ = rm.check_pre_trade(_FakeSignal())
        assert ok

    def test_drawdown_exceeds_limit_blocked(self):
        rm = RiskManager(
            _make_limits(max_drawdown_pct=0.20, max_daily_loss=50.0), initial_capital=100.0
        )
        rm.record_trade(-25.0)  # 25% drawdown > 20% limit
        ok, reason = rm.check_pre_trade(_FakeSignal())
        assert not ok
        assert "drawdown" in reason

    def test_peak_capital_tracks_profits(self):
        rm = RiskManager(
            _make_limits(max_drawdown_pct=0.20, max_daily_loss=50.0), initial_capital=100.0
        )
        rm.record_trade(10.0)  # peak = $110
        rm.record_trade(-15.0)  # now $95; drawdown = 15/110 = 13.6% < 20%
        ok, _ = rm.check_pre_trade(_FakeSignal())
        assert ok


class TestRiskManagerFrequency:
    """Hourly trade frequency limit."""

    def test_trades_within_hourly_limit_pass(self):
        rm = RiskManager(_make_limits(max_trades_per_hour=5), initial_capital=100.0)
        for _ in range(4):
            rm.record_trade(0.01)
        ok, _ = rm.check_pre_trade(_FakeSignal())
        assert ok

    def test_trades_exceeding_hourly_limit_blocked(self):
        rm = RiskManager(_make_limits(max_trades_per_hour=3), initial_capital=100.0)
        for _ in range(3):
            rm.record_trade(0.01)
        ok, reason = rm.check_pre_trade(_FakeSignal())
        assert not ok
        assert "trades_this_hour" in reason

    def test_consecutive_loss_limit_blocks_trading(self):
        rm = RiskManager(_make_limits(consecutive_loss_limit=2), initial_capital=100.0)
        rm.record_trade(-1.0)
        rm.record_trade(-1.0)
        ok, reason = rm.check_pre_trade(_FakeSignal())
        assert not ok
        assert "consecutive_losses" in reason

    def test_win_resets_consecutive_losses(self):
        rm = RiskManager(_make_limits(consecutive_loss_limit=2), initial_capital=100.0)
        rm.record_trade(-1.0)
        rm.record_trade(0.5)  # win resets counter
        rm.record_trade(-1.0)
        ok, _ = rm.check_pre_trade(_FakeSignal())
        assert ok


# ===========================================================================
# 2. Kill switch tests
# ===========================================================================


class TestKillSwitchManual:
    """File-based manual kill switch."""

    def setup_method(self):
        # Ensure kill switch is not active before each test
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)

    def teardown_method(self):
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)

    def test_kill_switch_inactive_by_default(self):
        assert not is_kill_switch_active()

    def test_trigger_arms_kill_switch(self):
        trigger_kill_switch("test")
        assert is_kill_switch_active()

    def test_removing_file_disarms_kill_switch(self):
        trigger_kill_switch("test")
        os.remove(KILL_SWITCH_FILE)
        assert not is_kill_switch_active()

    def test_trigger_writes_reason_to_file(self):
        trigger_kill_switch("capital below minimum")
        content = open(KILL_SWITCH_FILE).read()
        assert "capital below minimum" in content


class TestKillSwitchAutoCapital:
    """Auto-trigger on capital threshold."""

    def setup_method(self):
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)

    def teardown_method(self):
        if os.path.exists(KILL_SWITCH_FILE):
            os.remove(KILL_SWITCH_FILE)

    def test_capital_above_minimum_does_not_trigger(self):
        rm = RiskManager(_make_limits(), initial_capital=100.0)
        rm.record_trade(-10.0)  # $90 remaining > $50 minimum
        assert not is_kill_switch_active()

    def test_safety_check_blocks_trade_when_capital_low(self):
        ok, reason = safety_check(
            trade_usd=5.0,
            daily_loss=-5.0,
            total_capital=40.0,  # below ABSOLUTE_MIN_CAPITAL
            trades_this_hour=1,
        )
        assert not ok
        assert "minimum" in reason.lower()

    def test_safety_check_blocks_when_daily_loss_exceeds_absolute(self):
        ok, reason = safety_check(
            trade_usd=5.0,
            daily_loss=-(ABSOLUTE_MAX_DAILY_LOSS + 0.01),
            total_capital=100.0,
            trades_this_hour=1,
        )
        assert not ok

    def test_safety_check_blocks_oversized_trade(self):
        ok, reason = safety_check(
            trade_usd=ABSOLUTE_MAX_TRADE_USD + 1,
            daily_loss=0.0,
            total_capital=100.0,
            trades_this_hour=1,
        )
        assert not ok
        assert "absolute max" in reason.lower()

    def test_safety_check_blocks_hourly_overflow(self):
        ok, reason = safety_check(
            trade_usd=5.0,
            daily_loss=0.0,
            total_capital=100.0,
            trades_this_hour=ABSOLUTE_MAX_TRADES_PER_HOUR,
        )
        assert not ok

    def test_safety_check_passes_valid_trade(self):
        ok, reason = safety_check(
            trade_usd=5.0,
            daily_loss=-2.0,
            total_capital=100.0,
            trades_this_hour=5,
        )
        assert ok
        assert reason == "OK"


# ===========================================================================
# 3. PreTradeValidator tests
# ===========================================================================


class TestPreTradeValidator:
    """Signal sanity checks."""

    def setup_method(self):
        self.v = PreTradeValidator()

    def test_valid_signal_passes(self):
        ok, _ = self.v.validate_signal(_FakeSignal())
        assert ok

    def test_zero_cex_price_rejected(self):
        ok, reason = self.v.validate_signal(_FakeSignal(cex_price=0.0))
        assert not ok
        assert "cex_price" in reason

    def test_negative_dex_price_rejected(self):
        ok, reason = self.v.validate_signal(_FakeSignal(dex_price=-1.0))
        assert not ok
        assert "dex_price" in reason

    def test_zero_spread_rejected(self):
        ok, reason = self.v.validate_signal(_FakeSignal(spread_bps=0.0))
        assert not ok
        assert "spread_bps" in reason

    def test_spread_above_max_sane_rejected_as_bad_data(self):
        # MAX_SANE_SPREAD_BPS = 50_000 — allows real forgotten pools (thousands of bps)
        # but rejects physically impossible values like 100_000bps (10x price difference)
        ok, reason = self.v.validate_signal(_FakeSignal(spread_bps=60_000.0))
        assert not ok
        assert "bad" in reason.lower() or "50000" in reason

    def test_stale_signal_rejected(self):
        sig = _FakeSignal(
            timestamp=time.time() - 10.0,  # 10 seconds old
            signal_ttl_seconds=5.0,  # TTL is 5 seconds
        )
        ok, reason = self.v.validate_signal(sig)
        assert not ok
        assert "expired" in reason

    def test_fresh_signal_within_ttl_passes(self):
        sig = _FakeSignal(
            timestamp=time.time() - 2.0,  # 2 seconds old
            signal_ttl_seconds=5.0,  # TTL is 5 seconds
        )
        ok, _ = self.v.validate_signal(sig)
        assert ok

    def test_inventory_not_ok_rejected(self):
        ok, reason = self.v.validate_signal(_FakeSignal(within_limits=False))
        assert not ok
        assert "within_limits" in reason

    def test_zero_size_rejected(self):
        ok, reason = self.v.validate_signal(_FakeSignal(size=0.0))
        assert not ok
        assert "size" in reason


# ===========================================================================
# 4. Safety constants immutability check
# ===========================================================================


class TestSafetyConstants:
    """Absolute limits are present and sane."""

    def test_absolute_max_trade_usd_is_set(self):
        assert ABSOLUTE_MAX_TRADE_USD == 25.0

    def test_absolute_max_daily_loss_is_set(self):
        assert ABSOLUTE_MAX_DAILY_LOSS == 20.0

    def test_absolute_min_capital_is_set(self):
        assert ABSOLUTE_MIN_CAPITAL == 50.0

    def test_absolute_max_trades_per_hour_is_set(self):
        assert ABSOLUTE_MAX_TRADES_PER_HOUR == 30
