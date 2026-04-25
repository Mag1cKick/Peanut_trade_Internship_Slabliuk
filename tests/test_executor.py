"""Tests for executor/engine.py"""

from __future__ import annotations

import asyncio
import time
from unittest.mock import patch

import pytest

from executor.engine import (
    ExecutionContext,
    Executor,
    ExecutorConfig,
    ExecutorState,
)
from executor.recovery import CircuitBreaker, CircuitBreakerConfig
from strategy.signal import Direction, Signal

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_signal(
    pair: str = "ETH/USDT",
    spread_bps: float = 100.0,
    score: float = 50.0,
    direction: Direction = Direction.BUY_CEX_SELL_DEX,
    ttl: float = 30.0,
) -> Signal:
    now = time.time()
    return Signal(
        signal_id=f"sig_{pair.replace('/', '')}_{int(now*1000) % 100000}",
        pair=pair,
        direction=direction,
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


def _make_executor(use_flashbots: bool = False) -> Executor:
    cfg = ExecutorConfig(simulation_mode=True, use_flashbots=use_flashbots)
    return Executor(
        exchange_client=None,
        pricing_module=None,
        inventory_tracker=None,
        config=cfg,
    )


# ---------------------------------------------------------------------------
# ExecutorState / ExecutionContext
# ---------------------------------------------------------------------------


class TestExecutionContext:
    def test_default_state_is_idle(self):
        sig = _make_signal()
        ctx = ExecutionContext(signal=sig)
        assert ctx.state == ExecutorState.IDLE

    def test_started_at_set_on_creation(self):
        sig = _make_signal()
        before = time.time()
        ctx = ExecutionContext(signal=sig)
        assert ctx.started_at >= before

    def test_optional_fields_none_by_default(self):
        sig = _make_signal()
        ctx = ExecutionContext(signal=sig)
        assert ctx.leg1_order_id is None
        assert ctx.leg1_fill_price is None
        assert ctx.leg2_tx_hash is None
        assert ctx.finished_at is None
        assert ctx.actual_net_pnl is None
        assert ctx.error is None


# ---------------------------------------------------------------------------
# Executor — required tests
# ---------------------------------------------------------------------------


class TestExecuteSuccess:
    @pytest.mark.asyncio
    async def test_execute_success(self):
        """Both legs fill, state ends at DONE."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.leg1_fill_price is not None
        assert ctx.leg2_fill_price is not None
        assert ctx.actual_net_pnl is not None
        assert ctx.finished_at is not None
        assert ctx.error is None

    @pytest.mark.asyncio
    async def test_execute_success_dex_first(self):
        """DEX-first path also ends at DONE."""
        executor = _make_executor(use_flashbots=True)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.leg1_venue == "dex"
        assert ctx.leg2_venue == "cex"

    @pytest.mark.asyncio
    async def test_execute_success_cex_first_venues(self):
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.leg1_venue == "cex"
        assert ctx.leg2_venue == "dex"

    @pytest.mark.asyncio
    async def test_pnl_calculated_on_done(self):
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert isinstance(ctx.actual_net_pnl, float)

    @pytest.mark.asyncio
    async def test_signal_marked_executed_after_done(self):
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()
        await executor.execute(sig)
        assert executor.replay_protection.is_duplicate(sig)


class TestExecuteCexTimeout:
    @pytest.mark.asyncio
    async def test_execute_cex_timeout(self):
        """CEX timeout results in FAILED state."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()

        async def slow_cex(*_a, **_kw):
            await asyncio.sleep(10)
            return {"success": True, "price": 2000.0, "filled": 1.0}

        with patch.object(executor, "_execute_cex_leg", slow_cex):
            executor.config.leg1_timeout = 0.05
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "timeout" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_dex_timeout_cex_first(self):
        """DEX timeout after CEX fill triggers unwind and FAILED."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()

        async def slow_dex(*_a, **_kw):
            await asyncio.sleep(10)
            return {"success": True, "price": 2016.0, "filled": 1.0}

        with patch.object(executor, "_execute_dex_leg", slow_dex):
            executor.config.leg2_timeout = 0.05
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "timeout" in ctx.error.lower()
        assert "unwound" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_dex_timeout_dex_first(self):
        """DEX timeout in DEX-first path → FAILED (no unwind needed)."""
        executor = _make_executor(use_flashbots=True)
        sig = _make_signal()

        async def slow_dex(*_a, **_kw):
            await asyncio.sleep(10)
            return {"success": True, "price": 2016.0, "filled": 1.0}

        with patch.object(executor, "_execute_dex_leg", slow_dex):
            executor.config.leg2_timeout = 0.05
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "timeout" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_cex_timeout_after_dex(self):
        """CEX timeout after DEX fill triggers unwind."""
        executor = _make_executor(use_flashbots=True)
        sig = _make_signal()

        async def slow_cex(*_a, **_kw):
            await asyncio.sleep(10)
            return {"success": True, "price": 2000.0, "filled": 1.0}

        with patch.object(executor, "_execute_cex_leg", slow_cex):
            executor.config.leg1_timeout = 0.05
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "unwound" in ctx.error.lower()


class TestDexFailureUnwinds:
    @pytest.mark.asyncio
    async def test_execute_dex_failure_unwinds(self):
        """DEX failure after CEX fill triggers unwind."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()

        async def fail_dex(*_a, **_kw):
            return {"success": False, "error": "slippage too high"}

        with patch.object(executor, "_execute_dex_leg", fail_dex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "unwound" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_cex_failure_after_dex_unwinds(self):
        """CEX failure after DEX fill triggers unwind (DEX-first path)."""
        executor = _make_executor(use_flashbots=True)
        sig = _make_signal()

        async def fail_cex(*_a, **_kw):
            return {"success": False, "error": "order rejected"}

        with patch.object(executor, "_execute_cex_leg", fail_cex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "unwound" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_cex_failure_leg1_no_unwind(self):
        """CEX leg1 failure (CEX-first) → FAILED, no unwind (nothing to unwind)."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()

        async def fail_cex(*_a, **_kw):
            return {"success": False, "error": "rejected"}

        with patch.object(executor, "_execute_cex_leg", fail_cex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert ctx.error == "rejected"

    @pytest.mark.asyncio
    async def test_dex_failure_leg1_no_unwind(self):
        """DEX leg1 failure (DEX-first) → FAILED with Flashbots message."""
        executor = _make_executor(use_flashbots=True)
        sig = _make_signal()

        async def fail_dex(*_a, **_kw):
            return {"success": False}

        with patch.object(executor, "_execute_dex_leg", fail_dex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "Flashbots" in ctx.error


class TestPartialFillRejected:
    @pytest.mark.asyncio
    async def test_partial_fill_rejected(self):
        """Fill below min_fill_ratio is rejected."""
        executor = _make_executor(use_flashbots=False)
        executor.config.min_fill_ratio = 0.8
        sig = _make_signal()

        async def partial_cex(*_a, **_kw):
            return {"success": True, "price": 2000.0, "filled": sig.size * 0.5}

        with patch.object(executor, "_execute_cex_leg", partial_cex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "threshold" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_fill_at_exactly_min_ratio_accepted(self):
        """Fill exactly at min_fill_ratio should proceed."""
        executor = _make_executor(use_flashbots=False)
        executor.config.min_fill_ratio = 0.8
        sig = _make_signal()

        async def exact_cex(*_a, **_kw):
            return {"success": True, "price": 2000.0, "filled": sig.size * 0.8}

        with patch.object(executor, "_execute_cex_leg", exact_cex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.DONE

    @pytest.mark.asyncio
    async def test_full_fill_accepted(self):
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE


class TestCircuitBreakerBlocks:
    @pytest.mark.asyncio
    async def test_circuit_breaker_blocks(self):
        """Open circuit breaker prevents execution."""
        executor = _make_executor(use_flashbots=False)
        executor.circuit_breaker.trip()
        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.FAILED
        assert "Circuit breaker" in ctx.error

    @pytest.mark.asyncio
    async def test_failures_eventually_trip_breaker(self):
        """Repeated failures open the circuit breaker."""
        executor = _make_executor(use_flashbots=False)
        executor.circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))

        async def fail_cex(*_a, **_kw):
            return {"success": False, "error": "rejected"}

        with patch.object(executor, "_execute_cex_leg", fail_cex):
            for i in range(3):
                sig = _make_signal()
                sig.signal_id = f"sig_{i}"
                await executor.execute(sig)

        assert executor.circuit_breaker.is_open()

    @pytest.mark.asyncio
    async def test_success_resets_circuit_breaker(self):
        executor = _make_executor(use_flashbots=False)
        executor.circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=3))
        executor.circuit_breaker.record_failure()
        executor.circuit_breaker.record_failure()

        sig = _make_signal()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert not executor.circuit_breaker.is_open()


class TestReplayProtectionIntegration:
    @pytest.mark.asyncio
    async def test_replay_protection(self):
        """Same signal can't execute twice."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal()

        ctx1 = await executor.execute(sig)
        assert ctx1.state == ExecutorState.DONE

        ctx2 = await executor.execute(sig)
        assert ctx2.state == ExecutorState.FAILED
        assert "Duplicate" in ctx2.error

    @pytest.mark.asyncio
    async def test_different_signals_both_execute(self):
        executor = _make_executor(use_flashbots=False)
        sig_a = _make_signal()
        sig_a.signal_id = "sig_a"
        sig_b = _make_signal()
        sig_b.signal_id = "sig_b"

        ctx_a = await executor.execute(sig_a)
        ctx_b = await executor.execute(sig_b)
        assert ctx_a.state == ExecutorState.DONE
        assert ctx_b.state == ExecutorState.DONE


# ---------------------------------------------------------------------------
# Signal validation
# ---------------------------------------------------------------------------


class TestSignalValidation:
    @pytest.mark.asyncio
    async def test_invalid_signal_fails(self):
        """Expired / zero-score signal is rejected at VALIDATING."""
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal(score=0.0, ttl=-1.0)  # already expired
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.FAILED
        assert "invalid" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_valid_signal_proceeds(self):
        executor = _make_executor(use_flashbots=False)
        sig = _make_signal(score=50.0, ttl=30.0)
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE


# ---------------------------------------------------------------------------
# _calculate_pnl
# ---------------------------------------------------------------------------


class TestCalculatePnl:
    def _make_ctx(self, direction: Direction) -> ExecutionContext:
        sig = _make_signal(direction=direction)
        ctx = ExecutionContext(signal=sig)
        ctx.leg1_fill_price = 2000.0
        ctx.leg1_fill_size = 1.0
        ctx.leg2_fill_price = 2020.0
        ctx.leg2_fill_size = 1.0
        return ctx

    def test_buy_cex_sell_dex_positive(self):
        executor = _make_executor()
        ctx = self._make_ctx(Direction.BUY_CEX_SELL_DEX)
        pnl = executor._calculate_pnl(ctx)
        gross = (2020.0 - 2000.0) * 1.0
        fees = 1.0 * 2000.0 * 0.004
        assert pnl == pytest.approx(gross - fees)

    def test_buy_dex_sell_cex_positive(self):
        executor = _make_executor()
        ctx = self._make_ctx(Direction.BUY_DEX_SELL_CEX)
        # leg1=dex_buy at 2000, leg2=cex_sell at 2020 → gross = (2000-2020)*1 = -20?
        # Actually for BUY_DEX: leg1_fill_price is dex buy price, leg2 is cex sell price
        # gross = (leg1 - leg2) * size = (2000 - 2020) * 1 = -20  (bad trade)
        pnl = executor._calculate_pnl(ctx)
        gross = (ctx.leg1_fill_price - ctx.leg2_fill_price) * ctx.leg1_fill_size
        fees = ctx.leg1_fill_size * ctx.leg1_fill_price * 0.004
        assert pnl == pytest.approx(gross - fees)

    def test_fees_subtracted(self):
        executor = _make_executor()
        ctx = self._make_ctx(Direction.BUY_CEX_SELL_DEX)
        pnl = executor._calculate_pnl(ctx)
        gross = (2020.0 - 2000.0) * 1.0
        assert pnl < gross


# ---------------------------------------------------------------------------
# CEX retry logic
# ---------------------------------------------------------------------------


class TestCexRetry:
    @pytest.mark.asyncio
    async def test_retries_on_timeout_succeeds_second_attempt(self):
        """Transient timeout triggers retry; success on 2nd attempt → DONE."""
        executor = _make_executor(use_flashbots=False)
        executor.config.leg1_timeout = 0.05
        executor.config.leg1_max_retries = 2
        executor.config.leg1_retry_base_delay = 0.01
        call_count = 0

        async def flaky_cex(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(10)  # triggers timeout on first call
            return {"success": True, "price": 2000.0, "filled": 1.0}

        with patch.object(executor, "_execute_cex_leg", flaky_cex):
            sig = _make_signal()
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.DONE
        assert call_count == 2

    @pytest.mark.asyncio
    async def test_no_retry_on_permanent_failure(self):
        """Permanent error (insufficient balance) fails immediately without retry."""
        executor = _make_executor(use_flashbots=False)
        executor.config.leg1_max_retries = 3
        call_count = 0

        async def rejected(*_a, **_kw):
            nonlocal call_count
            call_count += 1
            return {"success": False, "error": "insufficient balance"}

        with patch.object(executor, "_execute_cex_leg", rejected):
            sig = _make_signal()
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert call_count == 1  # no retry

    @pytest.mark.asyncio
    async def test_exhausted_retries_returns_failed(self):
        """All retries exhausted → FAILED."""
        executor = _make_executor(use_flashbots=False)
        executor.config.leg1_timeout = 0.05
        executor.config.leg1_max_retries = 1
        executor.config.leg1_retry_base_delay = 0.01

        async def always_timeout(*_a, **_kw):
            await asyncio.sleep(10)
            return {"success": True, "price": 2000.0, "filled": 1.0}

        with patch.object(executor, "_execute_cex_leg", always_timeout):
            sig = _make_signal()
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "timeout" in ctx.error.lower()

    @pytest.mark.asyncio
    async def test_idempotency_key_reused_across_retries(self):
        """The same order_id is passed on all retry attempts."""
        executor = _make_executor(use_flashbots=False)
        executor.config.leg1_timeout = 0.05
        executor.config.leg1_max_retries = 1
        executor.config.leg1_retry_base_delay = 0.01
        received_ids: list[str | None] = []

        async def record_id(*_a, order_id=None, **_kw):
            received_ids.append(order_id)
            if len(received_ids) == 1:
                await asyncio.sleep(10)  # timeout first attempt
            return {"success": True, "price": 2000.0, "filled": 1.0}

        with patch.object(executor, "_execute_cex_leg", record_id):
            sig = _make_signal()
            await executor.execute(sig)

        assert len(received_ids) == 2
        assert received_ids[0] is not None
        assert received_ids[0] == received_ids[1]  # same key reused
