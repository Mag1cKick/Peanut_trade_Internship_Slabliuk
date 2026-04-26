"""
executor/engine.py — Arbitrage executor state machine.

Drives two-leg arb trades (CEX+DEX) through a strict state machine.
Supports CEX-first and DEX-first (Flashbots) ordering, partial-fill
rejection, timeout handling with unwind, circuit breaking, and replay
protection.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum, auto

from executor.recovery import CircuitBreaker, ReplayProtection
from strategy.fees import FeeStructure
from strategy.signal import Direction, Signal

log = logging.getLogger(__name__)

# Errors from the CEX that indicate a permanent business-logic failure.
# These are never retried — the order was understood and rejected.
_PERMANENT_CEX_ERRORS = frozenset(
    {
        "insufficient",
        "balance",
        "rejected",
        "invalid",
        "forbidden",
        "not found",
        "duplicate",
        "permission",
    }
)


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
    # CEX leg: Binance REST confirms in ~50ms; 500ms is already generous.
    # 5s was wrong — a stuck CEX order wastes the entire arb window.
    leg1_timeout: float = 0.5
    leg2_timeout: float = 60.0
    min_fill_ratio: float = 0.8
    use_flashbots: bool = True
    simulation_mode: bool = True
    fee_structure: FeeStructure | None = None
    # Retry (Microsoft Retry Pattern): exponential backoff for transient CEX failures.
    # Permanent failures (rejected, insufficient balance) are never retried.
    leg1_max_retries: int = 2
    leg1_retry_base_delay: float = 0.05  # 50ms → 100ms → 200ms with jitter
    # DEX execution parameters
    wallet: object = None  # WalletManager
    chain_client: object = None  # ChainClient
    slippage_bps: int = 50
    unwind_slippage_bps: int = 150
    dex_router: str = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
    tx_deadline_seconds: int = 120


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
            try:
                from monitoring.metrics import REPLAY_BLOCKS

                REPLAY_BLOCKS.inc()
            except Exception:
                pass
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

        order_id = f"{signal.signal_id}_{uuid.uuid4().hex[:8]}"
        leg1, err = await self._cex_with_retry(signal, order_id=order_id)
        if leg1 is None:
            ctx.state = ExecutorState.FAILED
            ctx.error = err
            return ctx

        if leg1["filled"] / signal.size < self.config.min_fill_ratio:
            ctx.state = ExecutorState.FAILED
            ctx.error = "Partial fill below threshold"
            return ctx

        ctx.leg1_fill_price = leg1["price"]
        ctx.leg1_fill_size = leg1["filled"]
        ctx.state = ExecutorState.LEG1_FILLED
        self._log_slippage("CEX leg1", signal.cex_price, leg1["price"], signal.pair)

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
        self._log_slippage("DEX leg2", signal.dex_price, leg2["price"], signal.pair)
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
        self._log_slippage("DEX leg1", signal.dex_price, leg1["price"], signal.pair)

        ctx.state = ExecutorState.LEG2_PENDING
        ctx.leg2_venue = "cex"

        order_id = f"{signal.signal_id}_{uuid.uuid4().hex[:8]}"
        leg2, err = await self._cex_with_retry(signal, size=ctx.leg1_fill_size, order_id=order_id)
        if leg2 is None:
            ctx.state = ExecutorState.UNWINDING
            await self._unwind(ctx)
            ctx.state = ExecutorState.FAILED
            ctx.error = f"CEX failed after DEX - unwound ({err})"
            return ctx

        ctx.leg2_fill_price = leg2["price"]
        ctx.leg2_fill_size = leg2["filled"]
        self._log_slippage("CEX leg2", signal.cex_price, leg2["price"], signal.pair)
        ctx.actual_net_pnl = self._calculate_pnl(ctx)
        ctx.state = ExecutorState.DONE
        return ctx

    async def _cex_with_retry(
        self,
        signal: Signal,
        size: float | None = None,
        order_id: str | None = None,
    ) -> tuple[dict | None, str]:
        """
        Execute a CEX order with exponential backoff retry for transient failures.

        Returns (result_dict, error_str).  result_dict is None on failure.

        Retry logic (Microsoft Retry Pattern):
          - Permanent errors (rejected, insufficient balance): fail immediately, no retry.
          - Transient errors (timeout, network): retry up to leg1_max_retries times
            with exponential delay + ±20% jitter to spread concurrent requests.
          - Idempotency: the same order_id is reused across retries so the exchange
            cannot process it twice even if the first attempt succeeded but timed out.
        """
        delay = self.config.leg1_retry_base_delay
        last_error = "CEX timeout"

        for attempt in range(self.config.leg1_max_retries + 1):
            try:
                result = await asyncio.wait_for(
                    self._execute_cex_leg(signal, size, order_id=order_id),
                    timeout=self.config.leg1_timeout,
                )
                if result["success"]:
                    if attempt > 0:
                        log.info("CEX order succeeded on retry %d", attempt)
                    return result, ""

                error = result.get("error", "CEX rejected")
                # Permanent failure — stop immediately
                if any(p in error.lower() for p in _PERMANENT_CEX_ERRORS):
                    log.warning("CEX permanent failure (no retry): %s", error)
                    return None, error
                last_error = error

            except TimeoutError:
                last_error = "CEX timeout"

            if attempt < self.config.leg1_max_retries:
                jitter = delay * 0.2 * (random.random() - 0.5)
                wait = delay + jitter
                log.warning(
                    "CEX attempt %d/%d failed (%s) — retrying in %.3fs",
                    attempt + 1,
                    self.config.leg1_max_retries + 1,
                    last_error,
                    wait,
                )
                await asyncio.sleep(wait)
                delay *= 2

        return None, last_error

    def _log_slippage(self, label: str, expected: float, actual: float, pair: str) -> None:
        """Log and record execution slippage vs the signal's arrival price."""
        if expected <= 0:
            return
        slippage_bps = abs(actual - expected) / expected * 10_000
        log.info(
            "%s slippage: %.2f bps (expected=%.4f actual=%.4f)",
            label,
            slippage_bps,
            expected,
            actual,
        )
        try:
            from monitoring.metrics import EXECUTION_SLIPPAGE

            EXECUTION_SLIPPAGE.labels(pair=pair, leg=label).observe(slippage_bps)
        except Exception:
            pass

    async def _execute_cex_leg(
        self,
        signal: Signal,
        size: float | None = None,
        order_id: str | None = None,
    ) -> dict:
        actual_size = size or signal.size
        if self.config.simulation_mode:
            await asyncio.sleep(0.01)
            return {
                "success": True,
                "price": signal.cex_price * 1.0001,
                "filled": actual_size,
            }
        side = "buy" if signal.direction == Direction.BUY_CEX_SELL_DEX else "sell"
        # clientOrderId provides idempotency: if this request is retried after a
        # timeout, the exchange recognises the duplicate and returns the original
        # result instead of creating a second order.
        params = {"clientOrderId": order_id} if order_id else {}
        result = self.exchange.create_limit_ioc_order(
            symbol=signal.pair,
            side=side,
            amount=actual_size,
            price=signal.cex_price * 1.001,
            params=params,
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
        Real DEX execution via UniswapDirectPricer or PricingEngine.

        Flow:
          1. Resolve tokens and get a quote.
          2. Approve ERC-20 token_in if needed (uint256_max approval, one-time).
          3. Build calldata using the correct Uniswap V2 function:
               - ETH  → token : swapExactETHForTokens  (no approval, send ETH as value)
               - token → ETH  : swapExactTokensForETH  (ERC-20 approval required)
               - token → token: swapExactTokensForTokens (ERC-20 approval required)
          4. Sign, broadcast, and wait for receipt.
        """
        from chain.builder import TransactionBuilder
        from core.types import Address, TokenAmount
        from pricing.fork_simulator import abi_encode

        wallet = self.config.wallet
        if wallet is None:
            raise RuntimeError("ExecutorConfig.wallet must be set for live DEX execution")

        chain_client = self.config.chain_client or getattr(self.pricing, "client", None)
        if chain_client is None:
            raise RuntimeError("chain_client must be set in ExecutorConfig for DEX execution")

        base, quote = signal.pair.split("/")
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            token_in = self.pricing.get_token(base)
            token_out = self.pricing.get_token(quote)
        else:
            token_in = self.pricing.get_token(quote)
            token_out = self.pricing.get_token(base)

        amount_in = int(size * 10**token_in.decimals)

        try:
            quote_result = self.pricing.get_quote(token_in, token_out, amount_in)
        except Exception as exc:
            return {"success": False, "error": f"Quote failed: {exc}"}

        # PricingEngine quotes carry a validity flag; DirectQuote does not.
        if hasattr(quote_result, "is_valid") and not quote_result.is_valid:
            return {"success": False, "error": "Quote invalid (stale reserves)"}

        min_out = int(quote_result.expected_output * (10_000 - self.config.slippage_bps) / 10_000)
        deadline = int(time.time()) + self.config.tx_deadline_seconds
        router = Address(self.config.dex_router)

        # Build token path — use route from PricingEngine if available, else direct.
        if hasattr(quote_result, "route"):
            path = [t.address.checksum for t in quote_result.route.path]
        else:
            path = [token_in.address, token_out.address]

        token_in_is_eth = token_in.symbol == "ETH"
        token_out_is_eth = token_out.symbol == "ETH"

        if token_in_is_eth:
            # swapExactETHForTokens — no approval, ETH sent as msg.value
            calldata = bytes.fromhex("7ff36ab5") + abi_encode(
                ["uint256", "address[]", "address", "uint256"],
                [min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=amount_in, decimals=18, symbol="ETH")

        elif token_out_is_eth:
            # swapExactTokensForETH — approve token_in first
            self._ensure_erc20_approved_sync(
                token_in.address, wallet, self.config.dex_router, chain_client, amount_in
            )
            calldata = bytes.fromhex("18cbafe5") + abi_encode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                [amount_in, min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=0, decimals=18, symbol="ETH")

        else:
            # swapExactTokensForTokens — approve token_in first
            self._ensure_erc20_approved_sync(
                token_in.address, wallet, self.config.dex_router, chain_client, amount_in
            )
            calldata = bytes.fromhex("38ed1739") + abi_encode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                [amount_in, min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=0, decimals=18, symbol="ETH")

        try:
            # with_gas_price("high") sets EIP-1559 maxFeePerGas/maxPriorityFeePerGas
            # from ChainClient.get_gas_price() — arb txs must land in the next block.
            # with_gas_estimate() adds a 1.2× safety buffer on the gas limit.
            tx = (
                TransactionBuilder(chain_client, wallet)
                .to(router)
                .value(tx_value)
                .data(calldata)
                .with_gas_estimate(buffer=1.2)
                .with_gas_price("high")
                .build()
            )
            signed = wallet.sign_transaction(tx.to_dict())
            tx_hash = chain_client.send_transaction(signed.rawTransaction)
            receipt = chain_client.wait_for_receipt(tx_hash, timeout=self.config.leg2_timeout)
        except Exception as exc:
            return {"success": False, "error": f"DEX tx failed: {exc}"}

        if not receipt or receipt.get("status") != 1:
            return {"success": False, "error": "DEX tx reverted"}

        effective_price = (quote_result.expected_output / 10**token_out.decimals) / size
        return {"success": True, "price": effective_price, "filled": size, "tx_hash": tx_hash}

    def _ensure_erc20_approved_sync(
        self,
        token_address: str,
        wallet,
        spender: str,
        chain_client,
        amount_needed: int,
    ) -> None:
        """
        Check ERC-20 allowance and send an approval tx if the current allowance
        is insufficient.  Uses uint256_max so subsequent trades need no further
        approvals for this token+wallet+spender combination.

        Selectors:
            allowance(address owner, address spender) → 0xdd62ed3e
            approve(address spender, uint256 amount)  → 0x095ea7b3
        """
        from chain.builder import TransactionBuilder
        from core.types import Address, TokenAmount
        from pricing.fork_simulator import abi_encode

        owner = wallet.address.lower().replace("0x", "").zfill(64)
        spender_hex = spender.lower().replace("0x", "").zfill(64)

        result = chain_client._call_with_retry(
            "call",
            {"to": token_address, "data": "0xdd62ed3e" + owner + spender_hex},
            "latest",
        )
        current = int(result, 16) if result and result not in ("0x", "0x0") else 0

        if current >= amount_needed:
            return

        log.info(
            "Approving %s → Uniswap router (allowance=%d < needed=%d)",
            token_address,
            current,
            amount_needed,
        )

        uint256_max = 2**256 - 1
        calldata = bytes.fromhex("095ea7b3") + abi_encode(
            ["address", "uint256"], [spender, uint256_max]
        )

        try:
            tx = (
                TransactionBuilder(chain_client, wallet)
                .to(Address(token_address))
                .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
                .data(calldata)
                .build()
            )
            signed = wallet.sign_transaction(tx.to_dict())
            tx_hash = chain_client.send_transaction(signed.rawTransaction)
            receipt = chain_client.wait_for_receipt(tx_hash, timeout=30)
            if not receipt or receipt.get("status") != 1:
                raise RuntimeError(f"Approval tx reverted for {token_address}")
            log.info("ERC-20 approval confirmed: %s", tx_hash)
        except Exception as exc:
            raise RuntimeError(f"ERC-20 approval failed: {exc}") from exc

    def _unwind_dex_leg_sync(self, ctx: ExecutionContext) -> None:
        """
        Reverse a stuck DEX position by swapping back through Uniswap V2.

        Called when leg1=DEX filled but leg2=CEX failed.  We hold tokens
        received from the original DEX swap and need to swap them back.
        Uses _execute_dex_leg_sync infrastructure with reversed token direction.
        """
        from chain.builder import TransactionBuilder
        from core.types import Address, TokenAmount
        from pricing.fork_simulator import abi_encode

        wallet = self.config.wallet
        if wallet is None:
            raise RuntimeError("ExecutorConfig.wallet must be set for DEX unwind")

        chain_client = self.config.chain_client or getattr(self.pricing, "client", None)
        if chain_client is None:
            raise RuntimeError("chain_client must be set in ExecutorConfig for DEX unwind")

        signal = ctx.signal
        base, quote = signal.pair.split("/")

        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            # Original: sold base → received quote. Unwind: sell quote → get base.
            token_in = self.pricing.get_token(quote)
            token_out = self.pricing.get_token(base)
            amount_in = int(
                (ctx.leg1_fill_size or 0) * (ctx.leg1_fill_price or 0) * 10**token_in.decimals
            )
        else:
            # Original: sold quote → received base. Unwind: sell base → get quote.
            token_in = self.pricing.get_token(base)
            token_out = self.pricing.get_token(quote)
            amount_in = int((ctx.leg1_fill_size or 0) * 10**token_in.decimals)

        if amount_in == 0:
            log.warning("DEX unwind: zero amount — nothing to unwind")
            return

        try:
            quote_result = self.pricing.get_quote(token_in, token_out, amount_in)
        except Exception as exc:
            raise RuntimeError(f"DEX unwind quote failed: {exc}") from exc

        slippage = self.config.unwind_slippage_bps
        min_out = int(quote_result.expected_output * (10_000 - slippage) / 10_000)
        deadline = int(time.time()) + self.config.tx_deadline_seconds

        if hasattr(quote_result, "route"):
            path = [t.address.checksum for t in quote_result.route.path]
        else:
            path = [token_in.address, token_out.address]

        token_in_is_eth = token_in.symbol == "ETH"
        token_out_is_eth = token_out.symbol == "ETH"
        router = Address(self.config.dex_router)

        if token_in_is_eth:
            calldata = bytes.fromhex("7ff36ab5") + abi_encode(
                ["uint256", "address[]", "address", "uint256"],
                [min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=amount_in, decimals=18, symbol="ETH")
        elif token_out_is_eth:
            self._ensure_erc20_approved_sync(
                token_in.address, wallet, self.config.dex_router, chain_client, amount_in
            )
            calldata = bytes.fromhex("18cbafe5") + abi_encode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                [amount_in, min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=0, decimals=18, symbol="ETH")
        else:
            self._ensure_erc20_approved_sync(
                token_in.address, wallet, self.config.dex_router, chain_client, amount_in
            )
            calldata = bytes.fromhex("38ed1739") + abi_encode(
                ["uint256", "uint256", "address[]", "address", "uint256"],
                [amount_in, min_out, path, wallet.address, deadline],
            )
            tx_value = TokenAmount(raw=0, decimals=18, symbol="ETH")

        # Unwind is time-critical — use "high" gas to ensure next-block inclusion.
        tx = (
            TransactionBuilder(chain_client, wallet)
            .to(router)
            .value(tx_value)
            .data(calldata)
            .with_gas_estimate(buffer=1.3)
            .with_gas_price("high")
            .build()
        )
        signed = wallet.sign_transaction(tx.to_dict())
        tx_hash = chain_client.send_transaction(signed.rawTransaction)
        receipt = chain_client.wait_for_receipt(tx_hash, timeout=self.config.leg2_timeout)

        if not receipt or receipt.get("status") != 1:
            raise RuntimeError("DEX unwind transaction reverted")

        log.warning(
            "DEX unwind complete: %s → %s tx=%s",
            token_in.symbol,
            token_out.symbol,
            tx_hash,
        )

    async def _unwind(self, ctx: ExecutionContext) -> None:
        """Market order to flatten the stuck leg-1 position."""
        try:
            from monitoring.metrics import UNWINDS

            UNWINDS.labels(pair=ctx.signal.pair).inc()
        except Exception:
            pass

        if self.config.simulation_mode:
            await asyncio.sleep(0.01)
            return

        if not ctx.leg1_fill_size:
            return

        signal = ctx.signal

        if ctx.leg1_venue == "cex":
            side = "sell" if signal.direction == Direction.BUY_CEX_SELL_DEX else "buy"
            try:
                self.exchange.create_market_order(
                    symbol=signal.pair,
                    side=side,
                    amount=ctx.leg1_fill_size,
                )
                log.warning(
                    "Unwind: %s %s %.6f on CEX",
                    side,
                    signal.pair,
                    ctx.leg1_fill_size,
                )
            except Exception as exc:
                log.critical(
                    "CEX unwind failed for %s: %s — manual intervention required",
                    signal.pair,
                    exc,
                )

        elif ctx.leg1_venue == "dex":
            try:
                await asyncio.get_event_loop().run_in_executor(None, self._unwind_dex_leg_sync, ctx)
            except Exception as exc:
                log.critical(
                    "DEX unwind failed for %s: %s — manual intervention required",
                    signal.pair,
                    exc,
                )

    def _calculate_pnl(self, ctx: ExecutionContext) -> float:
        signal = ctx.signal
        if signal.direction == Direction.BUY_CEX_SELL_DEX:
            gross = (ctx.leg2_fill_price - ctx.leg1_fill_price) * ctx.leg1_fill_size
        else:
            gross = (ctx.leg1_fill_price - ctx.leg2_fill_price) * ctx.leg1_fill_size
        trade_value = ctx.leg1_fill_size * ctx.leg1_fill_price
        if self.config.fee_structure is not None:
            fees = float(self.config.fee_structure.fee_usd(trade_value))
        else:
            fees = trade_value * 0.004  # fallback: 10 bps CEX + 30 bps DEX estimate
        return gross - fees
