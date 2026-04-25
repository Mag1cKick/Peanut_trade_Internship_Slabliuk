"""Tests for executor/recovery.py"""

from __future__ import annotations

import time

from executor.recovery import CircuitBreaker, CircuitBreakerConfig, ReplayProtection
from strategy.signal import Direction, Signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(signal_id: str = "sig_abc123") -> Signal:
    now = time.time()
    return Signal(
        signal_id=signal_id,
        pair="ETH/USDT",
        direction=Direction.BUY_CEX_SELL_DEX,
        cex_price=2000.0,
        dex_price=2016.0,
        spread_bps=100.0,
        size=1.0,
        expected_gross_pnl=20.0,
        expected_fees=9.0,
        expected_net_pnl=11.0,
        score=50.0,
        timestamp=now,
        expiry=now + 30.0,
        inventory_ok=True,
        within_limits=True,
    )


# ---------------------------------------------------------------------------
# CircuitBreakerConfig
# ---------------------------------------------------------------------------


class TestCircuitBreakerConfig:
    def test_defaults(self):
        cfg = CircuitBreakerConfig()
        assert cfg.failure_threshold == 3
        assert cfg.window_seconds == 300.0
        assert cfg.cooldown_seconds == 600.0

    def test_custom(self):
        cfg = CircuitBreakerConfig(failure_threshold=5, window_seconds=60.0, cooldown_seconds=120.0)
        assert cfg.failure_threshold == 5


# ---------------------------------------------------------------------------
# CircuitBreaker — required tests
# ---------------------------------------------------------------------------


class TestCircuitBreakerTrips:
    def test_circuit_breaker_trips(self):
        """3 failures in window trips breaker."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, window_seconds=60.0))
        assert not cb.is_open()
        cb.record_failure()
        cb.record_failure()
        assert not cb.is_open()
        cb.record_failure()
        assert cb.is_open()

    def test_threshold_one(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure()
        assert cb.is_open()

    def test_failures_below_threshold_do_not_trip(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
        for _ in range(4):
            cb.record_failure()
        assert not cb.is_open()

    def test_trip_sets_tripped_at(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        before = time.time()
        cb.record_failure()
        assert cb.tripped_at is not None
        assert cb.tripped_at >= before

    def test_failures_outside_window_not_counted(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, window_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        cb.record_failure()
        assert not cb.is_open()

    def test_only_in_window_failures_count(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2, window_seconds=0.1))
        cb.record_failure()
        time.sleep(0.12)
        cb.record_failure()
        cb.record_failure()
        assert cb.is_open()

    def test_trip_directly(self):
        cb = CircuitBreaker()
        cb.trip()
        assert cb.is_open()


class TestCircuitBreakerResets:
    def test_circuit_breaker_resets(self):
        """Breaker resets after cooldown."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        assert cb.is_open()
        time.sleep(0.07)
        assert not cb.is_open()

    def test_failures_cleared_after_reset(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        cb.is_open()
        assert cb.failures == []

    def test_tripped_at_cleared_after_reset(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        cb.is_open()
        assert cb.tripped_at is None

    def test_not_open_before_cooldown(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60.0))
        cb.record_failure()
        assert cb.is_open()

    def test_can_trip_again_after_reset(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        assert not cb.is_open()
        cb.record_failure()
        assert cb.is_open()


class TestCircuitBreakerMisc:
    def test_closed_by_default(self):
        cb = CircuitBreaker()
        assert not cb.is_open()

    def test_record_success_is_noop(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        cb.record_failure()
        cb.record_success()
        cb.record_failure()
        assert cb.is_open()

    def test_time_until_reset_zero_when_closed(self):
        cb = CircuitBreaker()
        assert cb.time_until_reset() == 0.0

    def test_time_until_reset_positive_when_open(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60.0))
        cb.record_failure()
        t = cb.time_until_reset()
        assert 0.0 < t <= 60.0

    def test_time_until_reset_decreases(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=60.0))
        cb.record_failure()
        t1 = cb.time_until_reset()
        time.sleep(0.05)
        t2 = cb.time_until_reset()
        assert t2 < t1

    def test_window_pruning_keeps_failures_list_bounded(self):
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=100, window_seconds=0.05))
        for _ in range(10):
            cb.record_failure()
        time.sleep(0.07)
        cb.record_failure()
        assert len(cb.failures) == 1


# ---------------------------------------------------------------------------
# Half-open state (Fowler pattern)
# ---------------------------------------------------------------------------


class TestCircuitBreakerHalfOpen:
    def test_half_open_probe_success_fully_resets(self):
        """After cooldown, one successful execution fully resets the breaker."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        assert not cb.is_open()  # cooldown elapsed — probe allowed
        cb.record_success()  # probe succeeded
        assert not cb.is_open()
        assert cb.tripped_at is None
        assert cb.failures == []
        assert not cb._probe_allowed

    def test_half_open_probe_failure_retrips_immediately(self):
        """After cooldown, one failure re-trips without needing window accumulation."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3, cooldown_seconds=0.05))
        cb.record_failure()
        cb.record_failure()
        cb.record_failure()  # trips
        time.sleep(0.07)
        assert not cb.is_open()  # half-open
        cb.record_failure()  # probe fails → immediate re-trip
        assert cb.is_open()

    def test_half_open_does_not_retrigger_on_success_when_closed(self):
        """record_success() on a closed (never tripped) breaker does nothing."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        cb.record_failure()
        cb.record_success()  # not in half-open — no-op
        cb.record_failure()
        assert not cb.is_open()  # still needs one more failure to trip

    def test_probe_flag_set_only_once(self):
        """_probe_allowed is set on first is_open() call after cooldown, not repeatedly."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()
        time.sleep(0.07)
        cb.is_open()  # sets _probe_allowed
        cb.is_open()  # second call — already set
        assert cb._probe_allowed  # still pending until probe runs

    def test_full_cycle_trip_probe_fail_retrap_probe_succeed_reset(self):
        """Full trip → half-open → re-trip → half-open → reset cycle."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, cooldown_seconds=0.05))
        cb.record_failure()  # trip
        time.sleep(0.07)
        assert not cb.is_open()  # half-open
        cb.record_failure()  # probe fails → re-trip
        assert cb.is_open()
        time.sleep(0.07)
        assert not cb.is_open()  # half-open again
        cb.record_success()  # probe succeeds → fully reset
        assert not cb.is_open()
        assert not cb._probe_allowed


# ---------------------------------------------------------------------------
# ReplayProtection — required tests
# ---------------------------------------------------------------------------


class TestReplayBlocks:
    def test_replay_blocks_duplicate(self):
        """Same signal_id blocked."""
        rp = ReplayProtection()
        sig = _make_signal("sig_001")
        rp.mark_executed(sig)
        assert rp.is_duplicate(sig)

    def test_blocks_on_second_check(self):
        rp = ReplayProtection()
        sig = _make_signal("sig_002")
        assert not rp.is_duplicate(sig)
        rp.mark_executed(sig)
        assert rp.is_duplicate(sig)


class TestReplayAllows:
    def test_replay_allows_new(self):
        """Different signal_id allowed."""
        rp = ReplayProtection()
        sig_a = _make_signal("sig_A")
        sig_b = _make_signal("sig_B")
        rp.mark_executed(sig_a)
        assert not rp.is_duplicate(sig_b)

    def test_empty_allows_any(self):
        rp = ReplayProtection()
        assert not rp.is_duplicate(_make_signal("sig_new"))


class TestReplayTtl:
    def test_expired_entry_not_duplicate(self):
        rp = ReplayProtection(ttl_seconds=0.05)
        sig = _make_signal("sig_ttl")
        rp.mark_executed(sig)
        assert rp.is_duplicate(sig)
        time.sleep(0.07)
        assert not rp.is_duplicate(sig)

    def test_cleanup_removes_old_entries(self):
        rp = ReplayProtection(ttl_seconds=0.05)
        for i in range(5):
            rp.mark_executed(_make_signal(f"sig_{i}"))
        time.sleep(0.07)
        rp._cleanup()
        assert len(rp.executed) == 0

    def test_fresh_entries_kept_after_cleanup(self):
        rp = ReplayProtection(ttl_seconds=60.0)
        sig = _make_signal("sig_fresh")
        rp.mark_executed(sig)
        rp._cleanup()
        assert rp.is_duplicate(sig)

    def test_mixed_ttl_only_old_removed(self):
        rp = ReplayProtection(ttl_seconds=0.1)
        old_sig = _make_signal("sig_old")
        rp.mark_executed(old_sig)
        time.sleep(0.12)
        fresh_sig = _make_signal("sig_fresh")
        rp.mark_executed(fresh_sig)
        rp._cleanup()
        assert "sig_old" not in rp.executed
        assert "sig_fresh" in rp.executed
