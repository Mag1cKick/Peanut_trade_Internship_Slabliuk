"""
scripts/arb_bot.py — Main arbitrage bot loop integrating all weeks.

Week 1: ChainClient (optional, gated by rpc_url config)
Week 2: PricingEngine (optional, gated by rpc_url config)
Week 3: ExchangeClient, InventoryTracker, PnLEngine
Week 4: SignalGenerator, SignalScorer, Executor, FeeStructure
Stretch: SignalQueue (priority queue), Prometheus metrics, webhook alerts
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from exchange.client import ExchangeClient
from executor.engine import ExecutionContext, Executor, ExecutorConfig, ExecutorState
from executor.queue import SignalQueue
from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.tracker import InventoryTracker, Venue
from monitoring.metrics import (
    CIRCUIT_BREAKER_OPEN,
    EXECUTION_LATENCY,
    INVENTORY_BALANCE,
    PNL_USD,
    SIGNAL_SCORE,
    SIGNALS_GENERATED,
    SIGNALS_SKIPPED,
    SPREAD_BPS,
    TRADES_EXECUTED,
    start_metrics_server,
)

# Well-known mainnet ERC-20 token addresses and decimals.
# Used by _fetch_wallet_balances to query on-chain balances for trading pairs.
_ERC20_ADDRESSES: dict[str, str] = {
    "USDT": "0xdAC17F958D2ee523a2206206994597C13D831ec7",
    "USDC": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
    "WBTC": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",
    "DAI": "0x6B175474E89094C44Da98b954EedeAC495271d0F",
    "BNB": "0xB8c77482e45F1F44dE1745F52C74426C631bDD52",
}
_ERC20_DECIMALS: dict[str, int] = {
    "USDT": 6,
    "USDC": 6,
    "WBTC": 8,
    "DAI": 18,
    "BNB": 18,
}
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import SignalScorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Operating modes
# ---------------------------------------------------------------------------
# TEST: Binance testnet + real DEX prices from mainnet RPC, no execution.
#       Safe for development — no real funds at risk.
# PROD: Binance mainnet + DEX mainnet with real transaction execution.
#       Requires PRIVATE_KEY env var and a funded wallet.
MODE_TEST = "test"
MODE_PROD = "prod"


class ArbBot:
    def __init__(self, config: dict) -> None:
        mode = config.get("mode", MODE_TEST)
        is_prod = mode == MODE_PROD
        log.info("Starting in %s mode", mode.upper())

        # --- CEX: testnet in TEST, mainnet in PROD ---
        self.exchange = ExchangeClient({**config, "sandbox": not is_prod})

        # --- Chain + DEX pricing ---
        # Both modes use real mainnet RPC for accurate DEX prices.
        # UniswapDirectPricer queries Uniswap V2 pool reserves directly — no
        # fork simulator needed, works for price data in both TEST and PROD.
        # PricingEngine (with ForkSimulator) is additionally wired in PROD mode
        # for execution-time quote validation via local Anvil fork.
        rpc_url = config.get("rpc_url", "")
        if rpc_url:
            from chain.client import ChainClient
            from pricing.uniswap_direct import UniswapDirectPricer

            self.chain_client = ChainClient([rpc_url])
            self.dex_pricer: UniswapDirectPricer | None = UniswapDirectPricer(rpc_url)
        else:
            self.chain_client = None
            self.dex_pricer = None

        # Full PricingEngine for execution validation — requires a local Anvil
        # fork (fork_url) and is only set up in PROD mode.
        self.pricing_engine = None
        if is_prod and rpc_url and config.get("fork_url"):
            try:
                from pricing.engine import PricingEngine
                from pricing.fork_simulator import ForkSimulator

                fork_sim = ForkSimulator.from_url(config["fork_url"])
                self.pricing_engine = PricingEngine(
                    self.chain_client,
                    fork_sim,
                    config.get("ws_url", ""),
                )
                # Load configured pools so the router can build quotes
                if config.get("pool_addresses"):
                    from core.types import Address

                    self.pricing_engine.load_pools([Address(a) for a in config["pool_addresses"]])
                log.info("PricingEngine ready (%d pool(s))", len(self.pricing_engine.pools))
            except Exception as exc:
                log.warning(
                    "PricingEngine setup failed (%s) — using direct pricer for execution", exc
                )

        # --- Wallet: required for PROD, optional for TEST ---
        wallet = None
        if is_prod:
            from core.wallet import WalletManager

            wallet = WalletManager.from_env(config.get("private_key_env", "PRIVATE_KEY"))
            log.info("Wallet loaded: %s", wallet.address)
        elif config.get("private_key_env"):
            try:
                from core.wallet import WalletManager

                wallet = WalletManager.from_env(config["private_key_env"])
            except OSError:
                log.debug("No wallet configured for test mode — simulation only")

        self.inventory = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        self.pnl_engine = PnLEngine()

        self.fees = FeeStructure(
            cex_taker_bps=config.get("cex_taker_bps", 10.0),
            dex_swap_bps=config.get("dex_swap_bps", 30.0),
            gas_cost_usd=config.get("gas_cost_usd", 5.0),
        )
        # Generator uses UniswapDirectPricer for live pool reserve quotes.
        # Executor uses PricingEngine (with fork sim) in PROD for execution
        # validation; falls back to UniswapDirectPricer when no fork available.
        self.generator = SignalGenerator(
            self.exchange,
            self.dex_pricer,  # direct pool queries — works without fork
            self.inventory,
            self.fees,
            config.get("signal_config", {}),
        )
        self.scorer = SignalScorer()
        self.executor = Executor(
            self.exchange,
            self.pricing_engine or self.dex_pricer,  # PricingEngine if available
            self.inventory,
            ExecutorConfig(
                simulation_mode=not is_prod,
                fee_structure=self.fees,
                wallet=wallet,
                chain_client=self.chain_client,
                slippage_bps=config.get("slippage_bps", 50),
                unwind_slippage_bps=config.get("unwind_slippage_bps", 150),
                dex_router=config.get("dex_router", "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"),
                tx_deadline_seconds=config.get("tx_deadline_seconds", 120),
            ),
        )

        self.pairs: list[str] = config.get("pairs", ["ETH/USDT"])
        self.trade_size: float = config.get("trade_size", 0.1)
        self.score_threshold: float = config.get("score_threshold", 60.0)
        self.metrics_port: int = config.get("metrics_port", 0)
        self._wallet_address: str | None = (
            wallet.address if wallet else config.get("wallet_address")
        )
        self.running = False

        # Priority queue: holds scored signals waiting for execution
        self._queue: SignalQueue = SignalQueue(maxsize=config.get("queue_maxsize", 50))

    async def run(self) -> None:
        self.running = True
        log.info("Bot starting...")

        if self.metrics_port:
            start_metrics_server(self.metrics_port)

        await self._sync_balances()

        while self.running:
            try:
                await self._tick()
                await asyncio.sleep(1)
            except Exception as exc:
                log.error("Tick error: %s", exc)
                await asyncio.sleep(5)

    async def _tick(self) -> None:
        cb = self.executor.circuit_breaker

        # Update circuit-breaker gauge for Prometheus
        CIRCUIT_BREAKER_OPEN.set(1 if cb.is_open() else 0)

        if cb.is_open():
            log.info("Circuit breaker open — %.0fs until reset", cb.time_until_reset())
            return

        # Phase 1: generate + score all pairs concurrently.
        # Each pair's price fetch is independent — run them in parallel so a
        # slow exchange response on one pair doesn't delay the others.
        # (AsyncIO docs: use gather for independent concurrent coroutines.)
        async def _generate_one(pair: str) -> None:
            signal = await asyncio.get_event_loop().run_in_executor(
                None, self.generator.generate, pair, self.trade_size
            )
            if signal is None:
                return
            SIGNALS_GENERATED.labels(pair=pair).inc()
            SPREAD_BPS.labels(pair=pair).observe(signal.spread_bps)
            signal.score = self.scorer.score(signal, self.inventory.get_skews())
            SIGNAL_SCORE.labels(pair=pair).observe(signal.score)
            if signal.score < self.score_threshold:
                SIGNALS_SKIPPED.labels(pair=pair, reason="low_score").inc()
                log.info(
                    "Skipped %s: score %.1f below threshold %.1f",
                    pair,
                    signal.score,
                    self.score_threshold,
                )
                return
            self._queue.put(signal)

        await asyncio.gather(*[_generate_one(p) for p in self.pairs])

        # Phase 2: drain the queue highest-score-first
        while True:
            signal = self._queue.get()
            if signal is None:
                break

            # Reject signals whose score has decayed below half the threshold
            # while waiting in the queue — market conditions have moved on.
            decayed = self.scorer.apply_decay(signal)
            if decayed < self.score_threshold * 0.5:
                SIGNALS_SKIPPED.labels(pair=signal.pair, reason="decayed").inc()
                log.info(
                    "Skipped %s: score decayed %.1f → %.1f",
                    signal.pair,
                    signal.score,
                    decayed,
                )
                continue

            log.info(
                "Signal: %s spread=%.1fbps score=%.0f (decayed=%.1f)",
                signal.pair,
                signal.spread_bps,
                signal.score,
                decayed,
            )

            t0 = time.monotonic()
            ctx = await self.executor.execute(signal)
            latency = time.monotonic() - t0

            EXECUTION_LATENCY.labels(pair=signal.pair).observe(latency)
            TRADES_EXECUTED.labels(
                pair=signal.pair,
                state="done" if ctx.state == ExecutorState.DONE else "failed",
            ).inc()
            self.scorer.record_result(signal.pair, ctx.state == ExecutorState.DONE)

            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                PNL_USD.labels(pair=signal.pair).observe(ctx.actual_net_pnl)
                arb_record = execution_to_arb_record(ctx, self.fees)
                self.pnl_engine.record(arb_record)
                log.info("SUCCESS: PnL=$%.2f", ctx.actual_net_pnl)
            else:
                log.warning("FAILED: %s", ctx.error)
                log.warning(
                    "Circuit breaker: %d/%d failures",
                    len(cb.failures),
                    cb.config.failure_threshold,
                )

            await self._sync_balances()

            # Stop draining if breaker just tripped
            if cb.is_open():
                CIRCUIT_BREAKER_OPEN.set(1)
                break

    async def _sync_balances(self) -> None:
        try:
            cex_balances = self.exchange.fetch_balance()
            self.inventory.update_from_cex(Venue.BINANCE, cex_balances)
        except Exception as exc:
            log.warning("CEX balance sync failed: %s", exc)

        if self.chain_client is not None:
            try:
                wallet_balances = self._fetch_wallet_balances()
                self.inventory.update_from_wallet(Venue.WALLET, wallet_balances)
            except Exception as exc:
                log.warning("Wallet balance sync failed: %s", exc)

        self._update_inventory_metrics()

    def _update_inventory_metrics(self) -> None:
        """Push current inventory balances into the INVENTORY_BALANCE Prometheus gauge."""
        assets: set[str] = set()
        for pair in self.pairs:
            base, quote = pair.split("/")
            assets.update([base, quote])
        for asset in assets:
            for venue, label in [(Venue.BINANCE, "binance"), (Venue.WALLET, "wallet")]:
                try:
                    bal = float(self.inventory.get_available(venue, asset))
                    INVENTORY_BALANCE.labels(venue=label, asset=asset).set(bal)
                except Exception:
                    pass

    def _fetch_wallet_balances(self) -> dict:
        """
        Query on-chain balances via ChainClient for ETH and ERC-20 tokens.

        Requires config['wallet_address'] to be set.
        ERC-20 tokens are resolved from the configured trading pairs using
        well-known mainnet contract addresses in _ERC20_ADDRESSES.
        """
        from decimal import Decimal as _D

        from core.types import Address

        if not self._wallet_address:
            log.warning("wallet_address not configured — skipping on-chain balance sync")
            return {}

        address = Address(self._wallet_address)
        result: dict[str, str] = {}

        try:
            eth_amount = self.chain_client.get_balance(address)
            result["ETH"] = str(_D(str(eth_amount.raw)) / _D(str(10**18)))
        except Exception as exc:
            log.warning("ETH balance fetch failed: %s", exc)

        needed: set[str] = set()
        for pair in self.pairs:
            base, quote = pair.split("/")
            needed.update([base, quote])
        needed.discard("ETH")

        for symbol in needed:
            token_addr = _ERC20_ADDRESSES.get(symbol)
            decimals = _ERC20_DECIMALS.get(symbol, 18)
            if token_addr is None:
                log.debug("No mainnet address known for %s — skipping", symbol)
                continue
            try:
                raw = self._call_balanceof(token_addr, self._wallet_address)
                result[symbol] = str(_D(str(raw)) / _D(str(10**decimals)))
            except Exception as exc:
                log.warning("ERC-20 balance fetch failed for %s: %s", symbol, exc)

        return result

    def _call_balanceof(self, token_address: str, wallet_address: str) -> int:
        """
        Raw eth_call for ERC-20 balanceOf(address) → uint256.
        Selector: keccak256("balanceOf(address)")[:4] = 0x70a08231
        """
        padded = wallet_address.lower().replace("0x", "").zfill(64)
        data = "0x70a08231" + padded
        result = self.chain_client._call_with_retry(
            "call", {"to": token_address, "data": data}, "latest"
        )
        return int(result, 16) if result and result not in ("0x", "0x0") else 0

    def stop(self) -> None:
        self.running = False


def execution_to_arb_record(
    ctx: ExecutionContext,
    fee_structure=None,
) -> ArbRecord:
    """
    Bridge Week 4's ExecutionContext into Week 3's ArbRecord for PnL tracking.
    Actual fees are split evenly across both legs when fee_structure is provided.
    """
    signal = ctx.signal

    buy_venue = Venue.BINANCE if ctx.leg1_venue == "cex" else Venue.WALLET
    sell_venue = Venue.WALLET if ctx.leg2_venue == "dex" else Venue.BINANCE

    fill_size = Decimal(str(ctx.leg1_fill_size or 0))
    fill_price = Decimal(str(ctx.leg1_fill_price or 0))
    trade_value = fill_size * fill_price
    if fee_structure is not None and trade_value > 0:
        total_fee = fee_structure.fee_usd(trade_value)
        leg_fee = total_fee / 2
    else:
        leg_fee = Decimal("0")

    fee_asset = signal.pair.split("/")[1]

    buy_leg = TradeLeg(
        id=f"{signal.signal_id}_buy",
        timestamp=datetime.fromtimestamp(ctx.started_at),
        venue=buy_venue,
        symbol=signal.pair,
        side="buy",
        amount=fill_size,
        price=fill_price,
        fee=leg_fee,
        fee_asset=fee_asset,
    )
    sell_leg = TradeLeg(
        id=f"{signal.signal_id}_sell",
        timestamp=datetime.fromtimestamp(ctx.finished_at or ctx.started_at),
        venue=sell_venue,
        symbol=signal.pair,
        side="sell",
        amount=Decimal(str(ctx.leg2_fill_size or 0)),
        price=Decimal(str(ctx.leg2_fill_price or 0)),
        fee=leg_fee,
        fee_asset=fee_asset,
    )
    return ArbRecord(
        id=signal.signal_id,
        timestamp=datetime.fromtimestamp(ctx.started_at),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
    )


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="PeanutTrade arbitrage bot")
    parser.add_argument(
        "--mode",
        choices=[MODE_TEST, MODE_PROD],
        default=MODE_TEST,
        help=(
            "test: Binance testnet + real DEX prices, no execution. "
            "prod: Binance mainnet + real DEX execution (requires PRIVATE_KEY env var)."
        ),
    )
    args = parser.parse_args()

    # --- TEST mode config ---
    # Binance testnet (sandbox=True set automatically by ArbBot based on mode).
    # Real mainnet RPC for accurate DEX prices.
    # No execution — simulation_mode=True is set automatically.
    _TEST_CONFIG: dict = {
        "mode": MODE_TEST,
        "apiKey": os.getenv("BINANCE_TESTNET_API_KEY"),
        "secret": os.getenv("BINANCE_TESTNET_SECRET"),  # pragma: allowlist secret
        "rpc_url": os.getenv("ETH_RPC_URL", ""),  # mainnet RPC for real DEX prices
        "pairs": ["ETH/USDT"],
        "trade_size": 0.1,
        "score_threshold": 60.0,
        "signal_config": {
            "min_spread_bps": 50,
            "min_profit_usd": 5.0,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    # --- PROD mode config ---
    # Binance mainnet, real DEX execution.
    # Requires env vars: PRIVATE_KEY, BINANCE_API_KEY, BINANCE_SECRET, ETH_RPC_URL  # pragma: allowlist secret
    _PROD_CONFIG: dict = {
        "mode": MODE_PROD,
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET"),  # pragma: allowlist secret
        "rpc_url": os.getenv("ETH_RPC_URL", ""),
        "private_key_env": "PRIVATE_KEY",  # pragma: allowlist secret
        "pairs": ["ETH/USDT"],
        "trade_size": 0.05,  # smaller size for prod caution
        "score_threshold": 70.0,  # higher bar in prod
        "slippage_bps": 50,
        "unwind_slippage_bps": 150,
        "signal_config": {
            "min_spread_bps": 60,
            "min_profit_usd": 10.0,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    config = _PROD_CONFIG if args.mode == MODE_PROD else _TEST_CONFIG
    bot = ArbBot(config)
    asyncio.run(bot.run())
