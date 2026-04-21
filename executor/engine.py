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

from executor.recovery import CircuitBreaker, ReplayProtection
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
        return await asyncio.get_event_loop().run_in_executor(
            None, self._execute_dex_leg_sync, signal, size
        )

    def _execute_dex_leg_sync(self, signal: Signal, size: float) -> dict:
        """
        Real DEX execution via PricingEngine (Week 2) + ChainClient (Week 1).

        Flow:
          1. Get a swap quote from PricingEngine to find the best route and
             expected output (with slippage guard).
          2. Build swapExactTokensForTokens calldata using the route path.
          3. Sign and broadcast via ChainClient; wait for receipt.

        Requires executor to have been constructed with a live pricing_module
        (PricingEngine) and a chain_client attribute on the pricing_module.
        Also requires config.wallet (WalletManager) and config.slippage_bps.
        """
        from pricing.engine import PricingEngine

        if not isinstance(self.pricing, PricingEngine):
            raise RuntimeError("Real DEX execution requires a live PricingEngine")

        wallet = getattr(self.config, "wallet", None)
        if wallet is None:
            raise RuntimeError("ExecutorConfig.wallet must be set for live DEX execution")

        slippage_bps: int = getattr(self.config, "slippage_bps", 50)

        # Resolve token objects via the generator's _get_token convention
        base, quote = signal.pair.split("/")
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            # Selling base on DEX: token_in=base, token_out=quote
            token_in = self.pricing.get_token(base)
            token_out = self.pricing.get_token(quote)
        else:
            # Selling quote on DEX: token_in=quote, token_out=base
            token_in = self.pricing.get_token(quote)
            token_out = self.pricing.get_token(base)

        amount_in = int(size * 10**token_in.decimals)

        try:
            quote_result = self.pricing.get_quote(token_in, token_out, amount_in)
        except Exception as exc:
            return {"success": False, "error": f"Quote failed: {exc}"}

        if not quote_result.is_valid:
            return {"success": False, "error": "Quote invalid (stale reserves)"}

        min_out = int(quote_result.expected_output * (10_000 - slippage_bps) / 10_000)

        # Build swap path as list of checksum addresses
        path = [t.address.checksum for t in quote_result.route.path]

        # ABI-encode swapExactTokensForTokens calldata
        from pricing.fork_simulator import abi_encode

        _SEL_SWAP = bytes.fromhex("38ed1739")
        deadline = int(time.time()) + 120
        calldata = _SEL_SWAP + abi_encode(
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [amount_in, min_out, path, wallet.address, deadline],
        )

        # Build, sign, and broadcast the transaction
        from chain.builder import TransactionBuilder
        from core.types import Address, TokenAmount

        router = Address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
        chain_client = self.pricing.client

        try:
            tx = (
                TransactionBuilder(chain_client, wallet)
                .to(router)
                .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
                .data(calldata)
                .build()
            )
            signed = wallet.sign_transaction(tx.to_dict())
            tx_hash = chain_client.send_transaction(signed.rawTransaction)
            receipt = chain_client.wait_for_receipt(tx_hash, timeout=self.config.leg2_timeout)
        except Exception as exc:
            return {"success": False, "error": f"DEX tx failed: {exc}"}

        if not receipt or receipt.get("status") != 1:
            return {"success": False, "error": "DEX tx reverted"}

        # Derive effective fill price from expected output
        amount_out = quote_result.expected_output
        effective_price = (amount_out / 10**token_out.decimals) / size

        return {
            "success": True,
            "price": effective_price,
            "filled": size,
            "tx_hash": tx_hash,
        }

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
