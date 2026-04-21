"""
executor/recovery.py — Circuit breaker and replay protection for the executor.

CircuitBreaker uses a sliding time window: it trips when the failure count
inside the window reaches the threshold, then holds open for a cooldown
period before auto-resetting.

ReplayProtection tracks executed signal IDs with a TTL so the dict doesn't
grow unbounded across a long-running session.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from strategy.signal import Signal

log = logging.getLogger(__name__)


@dataclass
class CircuitBreakerConfig:
    failure_threshold: int = 3
    window_seconds: float = 300.0
    cooldown_seconds: float = 600.0


class CircuitBreaker:
    """
    Sliding-window circuit breaker.
    """

    def __init__(self, config: CircuitBreakerConfig | None = None) -> None:
        self.config = config or CircuitBreakerConfig()
        self.failures: list[float] = []
        self.tripped_at: float | None = None

    def record_failure(self) -> None:
        now = time.time()
        self.failures.append(now)
        cutoff = now - self.config.window_seconds
        self.failures = [t for t in self.failures if t > cutoff]

        if len(self.failures) >= self.config.failure_threshold:
            self.trip()

    def record_success(self) -> None:
        pass

    def trip(self) -> None:
        self.tripped_at = time.time()
        log.critical("CIRCUIT BREAKER TRIPPED")

    def is_open(self) -> bool:
        if self.tripped_at is None:
            return False
        if time.time() - self.tripped_at > self.config.cooldown_seconds:
            self.tripped_at = None
            self.failures = []
            return False
        return True

    def time_until_reset(self) -> float:
        if self.tripped_at is None:
            return 0.0
        return max(0.0, self.config.cooldown_seconds - (time.time() - self.tripped_at))


class ReplayProtection:
    """
    TTL-based replay guard.
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
