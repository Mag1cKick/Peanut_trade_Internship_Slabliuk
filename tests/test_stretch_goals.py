"""Tests for stretch goal modules: recovery webhook, SignalQueue, metrics."""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from executor.queue import SignalQueue
from executor.recovery import CircuitBreaker, CircuitBreakerConfig
from strategy.signal import Direction, Signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(signal_id: str = "sig_test", score: float = 80.0, ttl: float = 30.0) -> Signal:
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
        score=score,
        timestamp=now,
        expiry=now + ttl,
        inventory_ok=True,
        within_limits=True,
    )


# ---------------------------------------------------------------------------
# Webhook alerts
# ---------------------------------------------------------------------------


class TestWebhookAlert:
    def test_no_webhook_when_url_empty(self):
        """trip() with no webhook_url should not attempt any HTTP call."""
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1, webhook_url=""))
        with patch("urllib.request.urlopen") as mock_open:
            cb.record_failure()
            time.sleep(0.05)  # give daemon thread time to run (should not)
            mock_open.assert_not_called()

    def test_webhook_fires_on_trip(self):
        """trip() with webhook_url fires a POST in a background thread."""
        fired = threading.Event()

        def fake_urlopen(req, timeout=5):
            fired.set()
            return MagicMock().__enter__.return_value

        cb = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=1,
                webhook_url="http://localhost:9999/webhook",
            )
        )
        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            cb.record_failure()
            fired.wait(timeout=1.0)

        assert fired.is_set(), "Webhook was not called within 1s"

    def test_webhook_payload_contains_failure_info(self):
        """Webhook POST body includes failure count and threshold."""
        payloads = []

        def capture(req, timeout=5):
            import json

            payloads.append(json.loads(req.data.decode()))
            return MagicMock().__enter__.return_value

        cb = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=2,
                webhook_url="http://localhost:9999/webhook",
            )
        )
        with patch("urllib.request.urlopen", side_effect=capture):
            cb.record_failure()
            cb.record_failure()
            time.sleep(0.1)

        assert payloads, "No webhook payload captured"
        assert "failures" in payloads[0]
        assert "threshold" in payloads[0]
        assert payloads[0]["threshold"] == 2

    def test_webhook_failure_does_not_raise(self):
        """A failed HTTP POST must never propagate to the trading loop."""
        import urllib.error

        cb = CircuitBreaker(
            CircuitBreakerConfig(
                failure_threshold=1,
                webhook_url="http://localhost:9999/webhook",
            )
        )
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            cb.record_failure()  # must not raise
        # If we get here the test passes


# ---------------------------------------------------------------------------
# SignalQueue
# ---------------------------------------------------------------------------


class TestSignalQueue:
    def test_empty_get_returns_none(self):
        q = SignalQueue()
        assert q.get() is None

    def test_put_and_get(self):
        q = SignalQueue()
        sig = _make_signal(score=75.0)
        q.put(sig)
        result = q.get()
        assert result is sig

    def test_priority_order_highest_first(self):
        q = SignalQueue()
        low = _make_signal("low", score=40.0)
        high = _make_signal("high", score=90.0)
        mid = _make_signal("mid", score=65.0)
        q.put(low)
        q.put(high)
        q.put(mid)
        assert q.get().signal_id == "high"
        assert q.get().signal_id == "mid"
        assert q.get().signal_id == "low"

    def test_equal_scores_fifo(self):
        q = SignalQueue()
        first = _make_signal("first", score=80.0)
        second = _make_signal("second", score=80.0)
        q.put(first)
        q.put(second)
        assert q.get().signal_id == "first"
        assert q.get().signal_id == "second"

    def test_expired_signals_skipped(self):
        q = SignalQueue()
        expired = _make_signal("expired", score=90.0, ttl=-1.0)
        valid = _make_signal("valid", score=50.0, ttl=30.0)
        q.put(expired)
        q.put(valid)
        result = q.get()
        assert result.signal_id == "valid"

    def test_all_expired_returns_none(self):
        q = SignalQueue()
        q.put(_make_signal("a", score=90.0, ttl=-1.0))
        q.put(_make_signal("b", score=80.0, ttl=-1.0))
        assert q.get() is None

    def test_maxsize_evicts_lowest(self):
        q = SignalQueue(maxsize=2)
        q.put(_make_signal("low", score=30.0))
        q.put(_make_signal("mid", score=60.0))
        q.put(_make_signal("high", score=90.0))  # evicts "low"
        assert len(q) == 2
        first = q.get()
        assert first.signal_id == "high"

    def test_put_returns_false_when_full_and_score_too_low(self):
        q = SignalQueue(maxsize=2)
        q.put(_make_signal("a", score=80.0))
        q.put(_make_signal("b", score=70.0))
        accepted = q.put(_make_signal("c", score=10.0))
        assert not accepted
        assert len(q) == 2

    def test_len(self):
        q = SignalQueue()
        assert len(q) == 0
        q.put(_make_signal("a"))
        assert len(q) == 1

    def test_bool_false_when_empty(self):
        q = SignalQueue()
        assert not q

    def test_bool_true_when_non_empty(self):
        q = SignalQueue()
        q.put(_make_signal())
        assert q

    def test_peek_score_none_when_empty(self):
        q = SignalQueue()
        assert q.peek_score() is None

    def test_peek_score_returns_top_score(self):
        q = SignalQueue()
        q.put(_make_signal("a", score=70.0))
        q.put(_make_signal("b", score=90.0))
        assert q.peek_score() == pytest.approx(90.0)

    def test_peek_does_not_remove(self):
        q = SignalQueue()
        q.put(_make_signal(score=80.0))
        q.peek_score()
        assert len(q) == 1

    def test_thread_safety(self):
        """Multiple threads putting signals should not corrupt the heap."""
        q = SignalQueue(maxsize=200)
        errors = []

        def producer(start_score: float) -> None:
            try:
                for i in range(20):
                    q.put(_make_signal(f"sig_{start_score}_{i}", score=start_score + i))
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=producer, args=(float(s),)) for s in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert not errors
        assert len(q) <= 200


# ---------------------------------------------------------------------------
# Prometheus metrics (import + basic increment)
# ---------------------------------------------------------------------------


class TestMetrics:
    def test_imports_cleanly(self):
        from monitoring.metrics import (
            SIGNALS_GENERATED,
        )

        assert SIGNALS_GENERATED is not None

    def test_counter_increments(self):
        from monitoring.metrics import SIGNALS_GENERATED

        before = SIGNALS_GENERATED.labels(pair="TEST/USD")._value.get()
        SIGNALS_GENERATED.labels(pair="TEST/USD").inc()
        after = SIGNALS_GENERATED.labels(pair="TEST/USD")._value.get()
        assert after == before + 1

    def test_gauge_set(self):
        from monitoring.metrics import CIRCUIT_BREAKER_OPEN

        CIRCUIT_BREAKER_OPEN.set(1)
        assert CIRCUIT_BREAKER_OPEN._value.get() == 1.0
        CIRCUIT_BREAKER_OPEN.set(0)
        assert CIRCUIT_BREAKER_OPEN._value.get() == 0.0

    def test_histogram_observe(self):
        from monitoring.metrics import SIGNAL_SCORE

        SIGNAL_SCORE.labels(pair="TEST/USD").observe(75.0)
        # If no exception raised, observation worked

    def test_start_metrics_server_callable(self):
        from monitoring.metrics import start_metrics_server

        assert callable(start_metrics_server)
