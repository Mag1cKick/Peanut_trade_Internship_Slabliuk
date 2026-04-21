"""
strategy/signal.py — Signal data structures for arbitrage opportunity representation.

A Signal is a validated, scored arbitrage opportunity ready for execution.
It captures direction, economics, confidence, and timing in one immutable-ish object.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from enum import Enum


class Direction(Enum):
    BUY_CEX_SELL_DEX = "buy_cex_sell_dex"
    BUY_DEX_SELL_CEX = "buy_dex_sell_cex"


@dataclass
class Signal:
    """A validated arbitrage opportunity ready for execution."""

    signal_id: str
    pair: str
    direction: Direction

    cex_price: float
    dex_price: float

    spread_bps: float
    size: float

    expected_gross_pnl: float
    expected_fees: float
    expected_net_pnl: float

    score: float
    timestamp: float
    expiry: float

    inventory_ok: bool
    within_limits: bool

    bid_ask_spread_bps: float = 0.0

    @classmethod
    def create(cls, pair: str, direction: Direction, **kwargs) -> Signal:
        """
        Factory that injects signal_id and timestamp automatically.
        """
        now = time.time()
        return cls(
            signal_id=f"{pair.replace('/', '')}_{uuid.uuid4().hex[:8]}",
            pair=pair,
            direction=direction,
            timestamp=now,
            **kwargs,
        )

    def is_valid(self) -> bool:
        """
        True if this signal is still actionable.
        """
        return (
            time.time() < self.expiry
            and self.inventory_ok
            and self.within_limits
            and self.expected_net_pnl > 0
            and self.score > 0
        )

    def age_seconds(self) -> float:
        """Seconds since this signal was generated."""
        return time.time() - self.timestamp

    def time_to_expiry(self) -> float:
        """Seconds remaining before expiry. Negative means already expired."""
        return self.expiry - time.time()

    def notional_usd(self) -> float:
        """Trade size in USD (size × reference price)."""
        return self.size * self.cex_price

    def __str__(self) -> str:
        direction_str = (
            "BUY_DEX→SELL_CEX"
            if self.direction == Direction.BUY_DEX_SELL_CEX
            else "BUY_CEX→SELL_DEX"
        )
        return (
            f"Signal({self.pair} {direction_str} "
            f"spread={self.spread_bps:.1f}bps "
            f"net=${self.expected_net_pnl:.2f} "
            f"score={self.score:.0f} "
            f"age={self.age_seconds():.1f}s)"
        )
