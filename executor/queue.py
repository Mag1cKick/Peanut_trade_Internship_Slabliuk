"""
executor/queue.py — Priority queue for pending signals.

Signals are ordered by score descending: the highest-scoring opportunity
is always dequeued first.  The queue is thread-safe (uses a threading.Lock)
so it can be populated from a background price-feed thread while the
executor drains it from the async event loop.
"""

from __future__ import annotations

import heapq
import threading
from dataclasses import dataclass, field

from strategy.signal import Signal


@dataclass(order=True)
class _Entry:
    """Heap entry: negative score so heapq (min-heap) acts as max-heap."""

    priority: float
    sequence: int = field(compare=True)
    signal: Signal = field(compare=False)


class SignalQueue:
    """
    Thread-safe max-priority queue ordered by signal.score.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self.maxsize = maxsize
        self._heap: list[_Entry] = []
        self._lock = threading.Lock()
        self._seq = 0

    def put(self, signal: Signal) -> bool:
        """
        Enqueue a signal.  Returns False if the queue is full.
        """
        with self._lock:
            if len(self._heap) >= self.maxsize:
                lowest = self._heap[0]
                if signal.score <= -lowest.priority:
                    return False
                heapq.heappop(self._heap)

            entry = _Entry(
                priority=-signal.score,
                sequence=self._seq,
                signal=signal,
            )
            self._seq += 1
            heapq.heappush(self._heap, entry)
            return True

    def get(self) -> Signal | None:
        """
        Dequeue the highest-scoring valid signal, or None if empty.
        """
        with self._lock:
            while self._heap:
                entry = heapq.heappop(self._heap)
                if entry.signal.is_valid():
                    return entry.signal
            return None

    def peek_score(self) -> float | None:
        """Return the score of the top signal without removing it."""
        with self._lock:
            if not self._heap:
                return None
            return -self._heap[0].priority

    def __len__(self) -> int:
        with self._lock:
            return len(self._heap)

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._heap)
