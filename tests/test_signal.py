"""Tests for strategy/signal.py"""

from __future__ import annotations

import time

import pytest

from strategy.signal import Direction, Signal


def _make_signal(**overrides) -> Signal:
    defaults = dict(
        pair="ETH/USDT",
        direction=Direction.BUY_DEX_SELL_CEX,
        cex_price=2352.0,
        dex_price=2348.0,
        spread_bps=17.0,
        size=1.0,
        expected_gross_pnl=4.0,
        expected_fees=2.5,
        expected_net_pnl=1.5,
        score=75.0,
        expiry=time.time() + 10.0,
        inventory_ok=True,
        within_limits=True,
    )
    defaults.update(overrides)
    return Signal.create(**defaults)


class TestSignalFactory:
    def test_create_injects_signal_id(self):
        s = _make_signal()
        assert s.signal_id.startswith("ETHUSDT_")
        assert len(s.signal_id) == len("ETHUSDT_") + 8

    def test_create_injects_timestamp(self):
        before = time.time()
        s = _make_signal()
        after = time.time()
        assert before <= s.timestamp <= after

    def test_unique_ids(self):
        ids = {_make_signal().signal_id for _ in range(100)}
        assert len(ids) == 100

    def test_direction_stored(self):
        s = _make_signal(direction=Direction.BUY_CEX_SELL_DEX)
        assert s.direction == Direction.BUY_CEX_SELL_DEX


class TestSignalValidity:
    def test_valid_signal(self):
        s = _make_signal()
        assert s.is_valid()

    def test_expired_signal(self):
        s = _make_signal(expiry=time.time() - 1)
        assert not s.is_valid()

    def test_no_inventory(self):
        s = _make_signal(inventory_ok=False)
        assert not s.is_valid()

    def test_exceeds_limits(self):
        s = _make_signal(within_limits=False)
        assert not s.is_valid()

    def test_negative_net_pnl(self):
        # net_pnl and score are checked before enqueuing in _generate_one,
        # not in is_valid() — so a signal with negative pnl is still "valid"
        # from the queue's perspective (TTL, inventory, limits all pass).
        s = _make_signal(expected_net_pnl=-0.01)
        assert s.is_valid()

    def test_zero_score(self):
        # score is set by the scorer before queue.put(); is_valid() only guards
        # expiry / inventory / limits so the queue can discard stale signals.
        s = _make_signal(score=0.0)
        assert s.is_valid()

    def test_all_invalid_conditions(self):
        s = _make_signal(
            expiry=time.time() - 1,
            inventory_ok=False,
            within_limits=False,
            expected_net_pnl=-5.0,
            score=0.0,
        )
        assert not s.is_valid()


class TestSignalMetrics:
    def test_age_seconds(self):
        s = _make_signal()
        time.sleep(0.05)
        assert s.age_seconds() >= 0.05

    def test_time_to_expiry_positive(self):
        s = _make_signal(expiry=time.time() + 30)
        assert s.time_to_expiry() > 0

    def test_time_to_expiry_negative_when_expired(self):
        s = _make_signal(expiry=time.time() - 5)
        assert s.time_to_expiry() < 0

    def test_notional_usd(self):
        s = _make_signal(cex_price=2000.0, size=2.5)
        assert s.notional_usd() == pytest.approx(5000.0)

    def test_str_contains_pair(self):
        s = _make_signal(pair="BTC/USDT")
        assert "BTC/USDT" in str(s)

    def test_str_contains_direction(self):
        s = _make_signal(direction=Direction.BUY_DEX_SELL_CEX)
        assert "BUY_DEX" in str(s)

    def test_str_contains_score(self):
        s = _make_signal(score=82.5)
        assert "82" in str(s)
