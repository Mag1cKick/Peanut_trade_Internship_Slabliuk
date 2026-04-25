"""
executor/recovery.py — Circuit breaker and replay protection for the executor.

CircuitBreaker uses a sliding time window: it trips when the failure count
inside the window reaches the threshold, then holds open for a cooldown
period before auto-resetting.

Webhook alerts: when the circuit breaker trips it fires a non-blocking HTTP
POST to an optional webhook URL (e.g. Slack incoming webhook, PagerDuty).
The POST is dispatched in a background daemon thread so it never blocks the
trading loop.

ReplayProtection tracks executed signal IDs with a TTL so the dict doesn't
grow unbounded across a long-running session.
"""

from __future__ import annotations

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

from strategy.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    window_seconds: float = 300.0
    cooldown_seconds: float = 600.0
    webhook_url: str = ""


class CircuitBreaker:
    """
    Sliding-window circuit breaker with half-open state (Fowler pattern).

    States:
      CLOSED   — normal operation, all requests allowed.
      OPEN     — tripped, all requests blocked for cooldown_seconds.
      HALF-OPEN — cooldown elapsed; one probe request allowed. If it succeeds
                  the breaker fully resets. If it fails the breaker re-trips
                  immediately, preventing a flood of failing requests from
                  hammering a still-broken downstream service.

    Trips when ``failure_threshold`` failures occur within ``window_seconds``.
    On trip, fires a non-blocking HTTP POST to ``config.webhook_url`` if set.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self.failures: list[float] = []
        self.tripped_at: float | None = None
        self._probe_allowed: bool = False  # True once cooldown elapses

    def record_failure(self) -> None:
        if self._probe_allowed:
            # Half-open probe failed → re-trip immediately
            self._probe_allowed = False
            log.critical("CIRCUIT BREAKER RE-TRIPPED (half-open probe failed)")
            self.tripped_at = time.time()
            return

        now = time.time()
        self.failures.append(now)
        cutoff = now - self.config.window_seconds
        self.failures = [t for t in self.failures if t > cutoff]

        if len(self.failures) >= self.config.failure_threshold:
            self.trip()

    def record_success(self) -> None:
        if self._probe_allowed:
            # Half-open probe succeeded → fully reset
            self._probe_allowed = False
            self.tripped_at = None
            self.failures = []
            log.info("Circuit breaker reset (half-open probe succeeded)")

    def trip(self) -> None:
        self.tripped_at = time.time()
        self._probe_allowed = False
        log.critical("CIRCUIT BREAKER TRIPPED")
        try:
            from monitoring.metrics import CIRCUIT_BREAKER_TRIPS

            CIRCUIT_BREAKER_TRIPS.inc()
        except Exception:
            pass
        if self.config.webhook_url:
            self._send_webhook_async(
                self.config.webhook_url,
                failures=len(self.failures),
                threshold=self.config.failure_threshold,
                cooldown=self.config.cooldown_seconds,
            )

    def is_open(self) -> bool:
        if self.tripped_at is None:
            return False
        if time.time() - self.tripped_at > self.config.cooldown_seconds:
            # Cooldown elapsed — clear trip state and enter half-open.
            # tripped_at/failures are reset here so existing checks still work,
            # but _probe_allowed marks that the NEXT execution is a probe.
            self.tripped_at = None
            self.failures = []
            if not self._probe_allowed:
                self._probe_allowed = True
                log.info("Circuit breaker half-open — allowing probe request")
            return False
        return True

    def time_until_reset(self) -> float:
        if self.tripped_at is None:
            return 0.0
        return max(0.0, self.config.cooldown_seconds - (time.time() - self.tripped_at))

    @staticmethod
    def _send_webhook_async(url: str, **payload) -> None:
        """Fire-and-forget webhook POST in a daemon thread."""

        def _post() -> None:
            body = json.dumps(
                {
                    "text": (
                        f":rotating_light: *Circuit breaker tripped* — "
                        f"{payload.get('failures')}/{payload.get('threshold')} failures "
                        f"in window. Cooldown: {payload.get('cooldown')}s."
                    ),
                    **payload,
                }
            ).encode()
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5):
                    pass
                log.debug("Webhook alert sent to %s", url)
            except urllib.error.URLError as exc:
                log.warning("Webhook alert failed: %s", exc)

        t = threading.Thread(target=_post, daemon=True)
        t.start()


class ReplayProtection:
    """
    TTL-based replay guard.

    Executed signal IDs are remembered for ``ttl_seconds``; entries older
    than the TTL are pruned on each lookup so memory stays bounded.
    """

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self.executed: dict[str, float] = {}
        self.ttl = ttl_seconds

    def is_duplicate(self, signal: Signal) -> bool:
        self._cleanup()
        return signal.signal_id in self.executed

    def mark_executed(self, signal: Signal) -> None:
        self.executed[signal.signal_id] = time.time()

    def _cleanup(self) -> None:
        cutoff = time.time() - self.ttl
        self.executed = {k: v for k, v in self.executed.items() if v > cutoff}
