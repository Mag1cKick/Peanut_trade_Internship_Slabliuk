"""
executor/engine.py — Arbitrage executor state machine.

Drives two-leg arb trades (CEX+DEX) through a strict state machine.
Supports CEX-first and DEX-first (Flashbots) ordering, partial-fill
rejection, timeout handling with unwind, circuit breaking, and replay
protection.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import Enum, auto

from strategy.signal import Direction, Signal


class ExecutorState(Enum):
    IDLE = auto()
    VALIDATING = auto()
    LEG1_PENDING = auto()
    LEG1_FILLED = auto()
    LEG2_PENDING = auto()
    DONE = auto()
    FAILED = auto()
    UNWINDING = auto()


@dataclass
class ExecutionContext:
    signal: Signal
    state: ExecutorState = ExecutorState.IDLE

    leg1_venue: str = ""
    leg1_order_id: str | None = None
    leg1_fill_price: float | None = None
    leg1_fill_size: float | None = None

    leg2_venue: str = ""
    leg2_tx_hash: str | None = None
    leg2_fill_price: float | None = None
    leg2_fill_size: float | None = None

    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    actual_net_pnl: float | None = None
    error: str | None = None


@dataclass
class ExecutorConfig:
    leg1_timeout: float = 5.0
    leg2_timeout: float = 60.0
    min_fill_ratio: float = 0.8
    use_flashbots: bool = True
    simulation_mode: bool = True


class CircuitBreaker:
    """
    Open after too many consecutive failures; auto-resets after a cooldown.
    """

    def __init__(self, failure_threshold: int = 3, reset_seconds: float = 60.0) -> None:
        self.failure_threshold = failure_threshold
        self.reset_seconds = reset_seconds
        self._consecutive_failures = 0
        self._opened_at: float | None = None

    def is_open(self) -> bool:
        if self._opened_at is None:
            return False
        if time.time() - self._opened_at >= self.reset_seconds:
            self._opened_at = None
            self._consecutive_failures = 0
            return False
        return True

    def record_success(self) -> None:
        self._consecutive_failures = 0
        self._opened_at = None

    def record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.failure_threshold:
            self._opened_at = time.time()

    def trip(self) -> None:
        """Manually open the breaker (useful for tests)."""
        self._opened_at = time.time() - 1.0
        self._consecutive_failures = self.failure_threshold


class ReplayProtection:
    """Prevent the same signal from executing more than once."""

    def __init__(self) -> None:
        self._executed: set[str] = set()

    def is_duplicate(self, signal: Signal) -> bool:
        return signal.signal_id in self._executed

    def mark_executed(self, signal: Signal) -> None:
        self._executed.add(signal.signal_id)


class Executor:
    """Execute arbitrage trades across CEX and DEX."""

    def __init__(
        self,
        exchange_client,
        pricing_module,
        inventory_tracker,
        config: ExecutorConfig | None = None,
    ) -> None:
        self.exchange = exchange_client
        self.pricing = pricing_module
        self.inventory = inventory_tracker
        self.config = config or ExecutorConfig()

        self.circuit_breaker = CircuitBreaker()
        self.replay_protection = ReplayProtection()

    async def execute(self, signal: Signal) -> ExecutionContext:
        ctx = ExecutionContext(signal=signal)

        if self.circuit_breaker.is_open():
            ctx.state = ExecutorState.FAILED
            ctx.error = "Circuit breaker open"
            return ctx

        if self.replay_protection.is_duplicate(signal):
            ctx.state = ExecutorState.FAILED
            ctx.error = "Duplicate signal"
            return ctx

        ctx.state = ExecutorState.VALIDATING
        if not signal.is_valid():
            ctx.state = ExecutorState.FAILED
            ctx.error = "Signal invalid"
            return ctx

        if self.config.use_flashbots:
            ctx = await self._execute_dex_first(ctx)
        else:
            ctx = await self._execute_cex_first(ctx)

        self.replay_protection.mark_executed(signal)
        if ctx.state == ExecutorState.DONE:
            self.circuit_breaker.record_success()
        else:
            self.circuit_breaker.record_failure()

        ctx.finished_at = time.time()
        return ctx

    async def _execute_cex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        signal = ctx.signal

        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = "cex"

        try:
            leg1 = await asyncio.wait_for(
                self._execute_cex_leg(signal),
                timeout=self.config.leg1_timeout,
            )
        except TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = leg1.get("error", "CEX rejected")
            return ctx

        if leg1["filled"] / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "Partial fill below threshold"
            return ctx

        ctx.leg1_fill_price = leg1["price"]
        ctx.leg1_fill_size = leg1["filled"]
        ctx.state = ExecutorState.LEG1_FILLED

        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = "dex"

        try:
            leg2 = await asyncio.wait_for(
                self._execute_dex_leg(signal, ctx.leg1_fill_size),
                timeout=self.config.leg2_timeout,
            )
        except TimeoutError:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout - unwound"
            return ctx

        if not leg2["success"]:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX failed - unwound"
            return ctx

        ctx.leg2_fill_price = leg2["price"]
        ctx.leg2_fill_size = leg2["filled"]
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    async def _execute_dex_first(self, ctx: ExecutionContext) -> ExecutionContext:
        signal = ctx.signal

        ctx.state = ExecutorState.LEG1_PENDING
        ctx.leg1_venue = "dex"

        try:
            leg1 = await asyncio.wait_for(
                self._execute_dex_leg(signal, signal.size),
                timeout=self.config.leg2_timeout,
            )
        except TimeoutError:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX timeout"
            return ctx

        if not leg1["success"]:
            ctx.state = ExecutorState.FAILED
            ctx.error = "DEX failed (no cost via Flashbots)"
            return ctx

        ctx.leg1_fill_price = leg1["price"]
        ctx.leg1_fill_size = leg1["filled"]
        ctx.state = ExecutorState.LEG1_FILLED

        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = "cex"

        try:
            leg2 = await asyncio.wait_for(
                self._execute_cex_leg(signal, ctx.leg1_fill_size),
                timeout=self.config.leg1_timeout,
            )
        except TimeoutError:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX timeout after DEX - unwound"
            return ctx

        if not leg2["success"]:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = "CEX failed after DEX - unwound"
            return ctx

        ctx.leg2_fill_price = leg2["price"]
        ctx.leg2_fill_size = leg2["filled"]
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    async def _execute_cex_leg(self, signal: Signal, size: float = None) -> dict:
        actual_size = size or signal.size
        if self.config.simulation_mode:
            await asyncio.sleep(0.01)
            return {
                "success": True,
                "price": signal.cex_price * 1.0001,
                "filled": actual_size,
            }
        side = "buy" if signal.direction == Direction.BUY_CEX_SELL_DEX else "sell"
        result = self.exchange.create_limit_ioc_order(
            symbol=signal.pair,
            side=side,
            amount=actual_size,
            price=signal.cex_price * 1.001,
        )
        return {
            "success": result["status"] == "filled",
            "price": float(result["avg_fill_price"]),
            "filled": float(result["amount_filled"]),
            "error": result["status"],
        }

    async def _execute_dex_leg(self, signal: Signal, size: float) -> dict:
        if self.config.simulation_mode:
            await asyncio.sleep(0.05)
            return {"success": True, "price": signal.dex_price * 0.9998, "filled": size}
        raise NotImplementedError("Real DEX execution requires Week 2 integration")

    async def _unwind(self, ctx: ExecutionContext) -> None:
        """Market sell to flatten stuck position."""
        if self.config.simulation_mode:
            await asyncio.sleep(0.01)
            return
        raise NotImplementedError("Real unwind not implemented")

    def _calculate_pnl(self, ctx: ExecutionContext) -> float:
        signal = ctx.signal
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            gross = (ctx.leg2_fill_price - ctx.leg1_fill_price) * ctx.leg1_fill_size
        else:
            gross = (ctx.leg1_fill_price - ctx.leg2_fill_price) * ctx.leg1_fill_size
        fees = ctx.leg1_fill_size * ctx.leg1_fill_price * 0.004
        return gross - fees
