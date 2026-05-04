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
import tempfile
import time
from collections import deque
from datetime import datetime
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from dotenv import load_dotenv

load_dotenv()

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
from monitoring.telegram import make_alerter

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
from config.settings import Config
from safety import (
    ABSOLUTE_MIN_CAPITAL,
    PreTradeValidator,
    RiskLimits,
    RiskManager,
    is_kill_switch_active,
    safety_check,
    trigger_kill_switch,
)
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import SignalScorer


def _configure_logging() -> None:
    """Set up structured logging to stdout + daily rotating log file."""
    Path("logs").mkdir(exist_ok=True)
    log_file = Path("logs") / f"bot_{datetime.now():%Y%m%d}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-8s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


_configure_logging()
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
        if is_prod:
            log.warning("⚠️  PRODUCTION MODE — REAL MONEY ⚠️")
        else:
            log.info("Testnet / simulation mode — no real funds at risk")

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
            from pricing.uniswap_direct import (
                ARBITRUM,
                ARBITRUM_SUSHI,
                ARBITRUM_V3,
                ETHEREUM,
                UniswapDirectPricer,
                UniswapV3Pricer,
            )

            _network_map = {
                "arbitrum": ARBITRUM,
                "arbitrum-sushi": ARBITRUM_SUSHI,
                "arbitrum-v3": ARBITRUM_V3,
                "ethereum": ETHEREUM,
            }
            _pricer_cls = (
                UniswapV3Pricer if "v3" in config.get("network", "") else UniswapDirectPricer
            )
            network_cfg = _network_map.get(config.get("network", "arbitrum"), ARBITRUM)
            self.chain_client = ChainClient([rpc_url])
            self.dex_pricer: UniswapDirectPricer | None = _pricer_cls(rpc_url, network=network_cfg)
            log.info("DEX pricer: %s (chain_id=%d)", network_cfg.name, network_cfg.chain_id)
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

        # Fee structure: use Config defaults unless overridden in the run config.
        # Config.GAS_COST_USD defaults to $0.10 for Arbitrum (vs $5 mainnet).
        self.fees = FeeStructure(
            cex_taker_bps=config.get("cex_taker_bps", Config.CEX_TAKER_BPS),
            dex_swap_bps=config.get("dex_swap_bps", Config.DEX_SWAP_BPS),
            gas_cost_usd=config.get("gas_cost_usd", Config.GAS_COST_USD),
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
                dex_router=config.get(
                    "dex_router",
                    self.dex_pricer.router
                    if self.dex_pricer
                    else "0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
                ),
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

        # Risk management — conservative limits by default, tighten for prod
        risk_cfg = config.get("risk_limits", {})
        self.risk_limits = RiskLimits(
            max_trade_usd=risk_cfg.get("max_trade_usd", 5.0),
            max_trade_pct=risk_cfg.get("max_trade_pct", 0.20),
            max_position_per_token=risk_cfg.get("max_position_per_token", 30.0),
            max_open_positions=risk_cfg.get("max_open_positions", 1),
            max_loss_per_trade=risk_cfg.get("max_loss_per_trade", 5.0),
            max_daily_loss=risk_cfg.get("max_daily_loss", 10.0),
            max_drawdown_pct=risk_cfg.get("max_drawdown_pct", 0.20),
            max_trades_per_hour=risk_cfg.get("max_trades_per_hour", 20),
            consecutive_loss_limit=risk_cfg.get("consecutive_loss_limit", 3),
        )
        self.risk_manager = RiskManager(
            self.risk_limits,
            initial_capital=config.get("initial_capital", 100.0),
        )
        self.pre_trade_validator = PreTradeValidator()

        # Mock balances: used when CEX API keys are absent (dry-run / demo mode).
        # Format mirrors ccxt fetch_balance: {"USDC": {"free": "100", "locked": "0"}, ...}
        raw_mock = config.get("mock_balances", {})
        self._mock_balances: dict = (
            {k: {"free": str(v), "locked": "0"} for k, v in raw_mock.items()} if raw_mock else {}
        )
        if self._mock_balances:
            log.info("Mock balances: %s", {k: v["free"] for k, v in self._mock_balances.items()})

        # Session stats — accumulated each tick for the daily summary
        self._session_start: float = time.time()
        self._signals_seen: int = 0  # total signals that passed validation+risk
        self._signals_skipped_reasons: dict[str, int] = {}  # reason → count
        self._dry_run_count: int = 0  # how many would-have-traded
        self._dry_run_spreads: list[float] = []
        self._dry_run_pnls: list[float] = []
        self._dry_run_scores: list[float] = []

        # Telegram alerter — no-op when env vars are absent
        self.alerter = make_alerter()

        # Error-rate tracking: timestamps of tick-level exceptions (rolling 1-hour window)
        self._error_times: deque[float] = deque()
        # Suppress repeated circuit-breaker Telegram alerts (alert at most every 5 min)
        self._cb_last_alerted: float = 0.0

        # Dry-run: generate + validate + risk-check signals but do NOT execute.
        # Default True so the bot is safe out of the box; must be explicitly
        # set to False in prod config after a successful dry-run observation period.
        self.dry_run: bool = config.get("dry_run", True)
        if self.dry_run:
            log.info("DRY RUN mode — signals will be logged but NOT executed")

    async def run(self) -> None:
        self.running = True
        log.info("Bot starting...")

        if self.metrics_port:
            start_metrics_server(self.metrics_port)

        mode_label = "DRY RUN" if self.dry_run else "LIVE"
        await self.alerter.send(
            f"🟢 <b>ArbBot started</b> [{mode_label}]\n"
            f"Pairs: {', '.join(self.pairs)}\n"
            f"Score threshold: {self.score_threshold}"
        )

        await self._sync_balances()

        # Dead man's switch: heartbeat written every 30 s so an external watchdog
        # can detect if the bot hangs (see scripts/watchdog.sh for the counterpart).
        asyncio.create_task(self._heartbeat_loop())

        try:
            while self.running:
                try:
                    await self._tick()
                    await asyncio.sleep(1)
                except Exception as exc:
                    log.error("Tick error: %s", exc)
                    # Track errors for auto-kill if rate exceeds 50/hour
                    now_m = time.monotonic()
                    self._error_times.append(now_m)
                    cutoff = now_m - 3600.0
                    while self._error_times and self._error_times[0] < cutoff:
                        self._error_times.popleft()
                    if len(self._error_times) > 50:
                        msg = f"{len(self._error_times)} errors in last hour"
                        trigger_kill_switch(msg)
                        await self.alerter.send(f"🚨 <b>AUTO KILL SWITCH</b> — error rate: {msg}")
                    await asyncio.sleep(5)
        finally:
            await self._send_daily_summary()
            await self.alerter.send("🔴 <b>ArbBot stopped</b>")

    async def _tick(self) -> None:
        # Kill switch: operator can halt the bot by creating /tmp/arb_bot_kill
        if is_kill_switch_active():
            logging.critical("KILL SWITCH ACTIVE — halting bot")
            await self.alerter.send("🚨 <b>KILL SWITCH ACTIVATED</b> — bot halted")
            self.stop()
            return

        cb = self.executor.circuit_breaker

        # Update circuit-breaker gauge for Prometheus
        CIRCUIT_BREAKER_OPEN.set(1 if cb.is_open() else 0)

        if cb.is_open():
            secs = cb.time_until_reset()
            log.info("Circuit breaker open — %.0fs until reset", secs)
            # Alert at most once per 5 minutes to avoid Telegram spam
            now_m = time.monotonic()
            if now_m - self._cb_last_alerted > 300:
                self._cb_last_alerted = now_m
                await self.alerter.send(f"⚡ <b>Circuit breaker OPEN</b>\nResets in {secs:.0f}s")
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

            # Pre-trade sanity check (prices, TTL, within_limits)
            valid, reason = self.pre_trade_validator.validate_signal(signal)
            if not valid:
                SIGNALS_SKIPPED.labels(pair=pair, reason="validation").inc()
                self._signals_skipped_reasons["validation"] = (
                    self._signals_skipped_reasons.get("validation", 0) + 1
                )
                log.warning("Validation failed for %s: %s", pair, reason)
                return

            # Risk limits check (daily loss, drawdown, size, rate)
            allowed, reason = self.risk_manager.check_pre_trade(signal)
            if not allowed:
                SIGNALS_SKIPPED.labels(pair=pair, reason="risk").inc()
                self._signals_skipped_reasons["risk"] = (
                    self._signals_skipped_reasons.get("risk", 0) + 1
                )
                log.warning("Risk check failed for %s: %s", pair, reason)
                return

            self._signals_seen += 1
            signal.score = self.scorer.score(signal, self.inventory.get_skews())
            SIGNAL_SCORE.labels(pair=pair).observe(signal.score)
            if signal.score < self.score_threshold:
                SIGNALS_SKIPPED.labels(pair=pair, reason="low_score").inc()
                self._signals_skipped_reasons["low_score"] = (
                    self._signals_skipped_reasons.get("low_score", 0) + 1
                )
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

            # Absolute safety gate — final check before any real execution.
            # Skipped in simulation mode: these limits guard real capital only.
            if not self.executor.config.simulation_mode:
                trade_usd = float(signal.size) * float(signal.cex_price)
                safe, reason = safety_check(
                    trade_usd=trade_usd,
                    daily_loss=self.risk_manager.daily_loss,
                    total_capital=self.risk_manager.current_capital,
                    trades_this_hour=self.risk_manager.trades_this_hour,
                )
                if not safe:
                    SIGNALS_SKIPPED.labels(pair=signal.pair, reason="safety").inc()
                    log.critical("SAFETY CHECK BLOCKED trade: %s", reason)
                    continue

            # Dry-run gate: log the would-be trade and skip execution.
            if self.dry_run:
                direction = getattr(signal, "direction", None)
                dir_str = direction.value if direction is not None else "?"
                expected_pnl = float(
                    signal.expected_net_pnl
                    if hasattr(signal, "expected_net_pnl")
                    else signal.expected_gross_pnl
                )
                log.info(
                    "DRY RUN | Would trade: %s %s size=%.4f "
                    "spread=%.1fbps expected_pnl=$%.2f score=%.0f",
                    signal.pair,
                    dir_str,
                    float(signal.size),
                    float(signal.spread_bps),
                    expected_pnl,
                    signal.score,
                )
                self._dry_run_count += 1
                self._dry_run_spreads.append(float(signal.spread_bps))
                self._dry_run_pnls.append(expected_pnl)
                self._dry_run_scores.append(signal.score)
                continue

            t0 = time.monotonic()
            ctx = await self.executor.execute(signal)
            latency = time.monotonic() - t0

            EXECUTION_LATENCY.labels(pair=signal.pair).observe(latency)
            TRADES_EXECUTED.labels(
                pair=signal.pair,
                state="done" if ctx.state == ExecutorState.DONE else "failed",
            ).inc()
            self.scorer.record_result(signal.pair, ctx.state == ExecutorState.DONE)

            self._log_trade(ctx)
            if ctx.state == ExecutorState.DONE and not self.dry_run:
                await self._verify_balances(ctx)
            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                PNL_USD.labels(pair=signal.pair).observe(ctx.actual_net_pnl)
                arb_record = execution_to_arb_record(ctx, self.fees)
                self.pnl_engine.record(arb_record)
                self.risk_manager.record_trade(ctx.actual_net_pnl)
                pnl_emoji = "✅" if ctx.actual_net_pnl >= 0 else "🔻"
                await self.alerter.send(
                    f"{pnl_emoji} <b>Trade completed</b> {signal.pair}\n"
                    f"PnL: ${ctx.actual_net_pnl:.2f}  "
                    f"Daily loss: ${self.risk_manager.daily_loss:.2f}"
                )
                if self.risk_manager.daily_loss <= -self.risk_limits.max_daily_loss:
                    await self.alerter.send(
                        f"🛑 <b>Daily loss limit reached</b>\n"
                        f"Loss: ${self.risk_manager.daily_loss:.2f} / "
                        f"${self.risk_limits.max_daily_loss:.2f}"
                    )
                # Auto-trigger kill switch if capital drops below absolute floor
                if self.risk_manager.current_capital < ABSOLUTE_MIN_CAPITAL:
                    msg = (
                        f"capital ${self.risk_manager.current_capital:.2f} "
                        f"< ABSOLUTE_MIN_CAPITAL ${ABSOLUTE_MIN_CAPITAL:.2f}"
                    )
                    trigger_kill_switch(msg)
                    await self.alerter.send(f"🚨 <b>AUTO KILL SWITCH</b> — {msg}")
            elif ctx.actual_net_pnl is not None:
                self.risk_manager.record_trade(ctx.actual_net_pnl)
                log.warning("FAILED: %s", ctx.error)
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
        if self._mock_balances:
            # Mock mode: inject simulated balances on both venues so inventory
            # checks pass during dry-run without needing real funds.
            self.inventory.update_from_cex(Venue.BINANCE, self._mock_balances)
            wallet_balances = {k: v["free"] for k, v in self._mock_balances.items()}
            self.inventory.update_from_wallet(Venue.WALLET, wallet_balances)
            return

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

    # ------------------------------------------------------------------
    # Monitoring helpers
    # ------------------------------------------------------------------

    async def _verify_balances(self, ctx: ExecutionContext) -> None:
        """
        After a real trade, compare actual exchange balances against the
        inventory tracker's expected values. A mismatch > threshold means
        something went wrong (partial fill, fee accounting error, API bug)
        and the bot should stop immediately.
        """
        pair = ctx.signal.pair
        base, quote = pair.split("/")
        threshold = 0.01  # 1% relative tolerance

        try:
            actual_cex = self.exchange.fetch_balance()
        except Exception as exc:
            log.warning("verify_balances: could not fetch CEX balance: %s", exc)
            return

        for asset in (base, quote):
            actual = float(actual_cex.get(asset, {}).get("free", 0))
            expected = float(self.inventory.get_available(Venue.BINANCE, asset))
            if expected == 0:
                continue
            diff = abs(actual - expected) / expected
            if diff > threshold:
                msg = (
                    f"BALANCE MISMATCH {asset}: "
                    f"actual={actual:.6f} expected={expected:.6f} diff={diff:.1%}"
                )
                log.critical(msg)
                await self.alerter.send(f"🚨 <b>Balance mismatch</b> — {msg}\nStopping bot.")
                self.stop()
                return

        if self.chain_client is not None:
            try:
                actual_dex = self._fetch_wallet_balances()
                expected_dex = float(self.inventory.get_available(Venue.WALLET, base))
                actual_dex_base = float(actual_dex.get(base, 0))
                if expected_dex > 0:
                    diff = abs(actual_dex_base - expected_dex) / expected_dex
                    if diff > threshold:
                        msg = (
                            f"WALLET MISMATCH {base}: "
                            f"actual={actual_dex_base:.6f} expected={expected_dex:.6f} diff={diff:.1%}"
                        )
                        log.critical(msg)
                        await self.alerter.send(f"🚨 <b>Wallet mismatch</b> — {msg}\nStopping bot.")
                        self.stop()
                        return
            except Exception as exc:
                log.warning("verify_balances: wallet check failed: %s", exc)

        log.debug("Balance verification passed for %s", pair)

    def _log_trade(self, ctx: ExecutionContext) -> None:
        """Emit a structured TRADE log line parseable with grep."""
        direction = getattr(ctx.signal, "direction", None)
        dir_str = direction.value if direction is not None else "?"
        pnl = ctx.actual_net_pnl or 0.0
        log.info(
            "TRADE | pair=%s | direction=%s | size=%.4f | " "spread=%.1fbps | pnl=%.2f | state=%s",
            ctx.signal.pair,
            dir_str,
            float(ctx.signal.size),
            float(ctx.signal.spread_bps),
            pnl,
            ctx.state.name,
        )

    async def _heartbeat_loop(self) -> None:
        """Write a monotonic timestamp every 30 s for an external watchdog."""
        heartbeat = Path(tempfile.gettempdir()) / "arb_bot_heartbeat"
        while self.running:
            try:
                heartbeat.write_text(str(time.time()), encoding="utf-8")
            except OSError:
                pass
            await asyncio.sleep(30)

    async def _send_daily_summary(self) -> None:
        """Send rich end-of-session Telegram summary covering dry-run and live stats."""
        duration_min = (time.time() - self._session_start) / 60
        mode_label = "DRY RUN" if self.dry_run else "LIVE"
        trades = self.pnl_engine.trades
        skipped_total = sum(self._signals_skipped_reasons.values())

        lines: list[str] = [
            f"📊 <b>Session Summary [{mode_label}]</b>",
            "",
            f"⏱ Duration: {duration_min:.0f} min",
            f"🔍 Signals: {self._signals_seen} passed  {skipped_total} blocked",
        ]
        if skipped_total:
            reasons = "  ".join(
                f"{r}:{n}" for r, n in sorted(self._signals_skipped_reasons.items())
            )
            lines.append(f"   blocked by: {reasons}")

        # Dry-run breakdown
        if self.dry_run and self._dry_run_count:
            sp = self._dry_run_spreads
            pn = self._dry_run_pnls
            sc = self._dry_run_scores
            lines += [
                "",
                f"🧪 <b>Dry-run: {self._dry_run_count} would-be trades</b>",
                f"   Spread:  avg={sum(sp)/len(sp):.0f}  min={min(sp):.0f}  max={max(sp):.0f} bps",
                f"   Exp PnL: avg=${sum(pn)/len(pn):.3f}  total=${sum(pn):.2f}",
                f"   Score:   avg={sum(sc)/len(sc):.0f}  min={min(sc):.0f}  max={max(sc):.0f}",
            ]
        elif self.dry_run:
            lines.append("\n🧪 Dry-run: no signals passed all checks")

        # Live trade breakdown
        if trades:
            total_pnl = sum(float(t.net_pnl) for t in trades)
            wins = sum(1 for t in trades if float(t.net_pnl) > 0)
            losses = len(trades) - wins
            best = max(trades, key=lambda t: float(t.net_pnl))
            worst = min(trades, key=lambda t: float(t.net_pnl))
            pnl_emoji = "✅" if total_pnl >= 0 else "🔻"
            lines += [
                "",
                f"{pnl_emoji} <b>Trades: {len(trades)}  ({wins}W/{losses}L)  "
                f"Win: {wins/len(trades)*100:.0f}%</b>",
                f"   Net PnL: <b>${total_pnl:+.2f}</b>",
                f"   Best: ${float(best.net_pnl):+.2f}   Worst: ${float(worst.net_pnl):+.2f}",
            ]

        # Capital & risk
        peak = self.risk_manager._peak_capital
        cap = self.risk_manager.current_capital
        drawdown_pct = (peak - cap) / peak * 100 if peak > 0 else 0.0
        lines += [
            "",
            f"💰 Capital: ${cap:.2f}  (drawdown {drawdown_pct:.1f}%)",
            f"📉 Daily loss: ${self.risk_manager.daily_loss:.2f} / ${self.risk_limits.max_daily_loss:.2f}",
            f"⚠️ Errors: {len(self._error_times)}",
        ]

        await self.alerter.send("\n".join(lines))

    async def emergency_flatten(self) -> None:
        """
        Market-sell all non-stablecoin CEX positions immediately.

        Accepts any price — use only when you need to exit NOW.
        Follow up with manual verification on Binance web/app.
        """
        log.critical("EMERGENCY FLATTEN INITIATED")
        await self.alerter.send("🔴 <b>Emergency flatten initiated</b>")

        stablecoins = {"USDT", "USDC", "BUSD", "DAI", "FDUSD", "USD"}
        try:
            balances = self.exchange.fetch_balance()
        except Exception as exc:
            log.error("Emergency flatten: fetch_balance failed: %s", exc)
            await self.alerter.send(
                "⚠️ Emergency flatten: could not fetch balances — <b>MANUAL ACTION NEEDED</b>"
            )
            return

        for token, info in balances.items():
            if token in stablecoins or not isinstance(info, dict):
                continue
            free = float(info.get("free", 0))
            if free <= 0:
                continue
            for quote in ("USDT", "USDC"):
                symbol = f"{token}/{quote}"
                try:
                    order = self.exchange._exchange.create_market_sell_order(symbol, free)
                    fill = order.get("average") or order.get("price") or "?"
                    log.info("Flattened %.6f %s via %s @ %s", free, token, symbol, fill)
                    await self.alerter.send(f"✅ Flattened {free:.4f} {token} via {symbol}")
                    break
                except Exception:
                    pass
            else:
                log.warning("Could not flatten %s — manual intervention required", token)
                await self.alerter.send(
                    f"⚠️ Could not flatten {token} — <b>MANUAL ACTION NEEDED</b>"
                )

        await self.alerter.send("🔴 <b>Emergency flatten complete</b> — verify positions manually")


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
        "rpc_url": os.getenv("ARB_RPC_URL", os.getenv("ETH_RPC_URL", "")),
        "network": "arbitrum-v3",
        "pairs": ["MAGIC/USDC"],
        "trade_size": 100.0,
        "score_threshold": 60.0,
        "signal_config": {
            "min_spread_bps": 20,
            "min_profit_usd": 0.01,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        "risk_limits": {
            "max_trade_usd": 25.0,
            "max_daily_loss": 20.0,
            "max_position_per_token": 500.0,
        },
        "dry_run": True,
        # PENDLE in wallet needed so inventory_ok=True for the DEX sell leg
        "mock_balances": {"USDC": 100.0, "MAGIC": 500.0},
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    # --- PROD mode config ---
    # Binance mainnet + Arbitrum DEX execution.
    # Requires env vars: PRIVATE_KEY, BINANCE_API_KEY, BINANCE_SECRET, ARB_RPC_URL  # pragma: allowlist secret
    _PROD_CONFIG: dict = {
        "mode": MODE_PROD,
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET"),  # pragma: allowlist secret
        "rpc_url": os.getenv("ARB_RPC_URL", ""),
        "network": "arbitrum-v3",
        "private_key_env": "PRIVATE_KEY",  # pragma: allowlist secret
        # MAGIC/USDC V3: 242bps spread, ~$0.03 net per trade, real liquidity (1T).
        # USDC pool has different price than USDT pool — currently better spread.
        "pairs": ["MAGIC/USDC"],
        "trade_size": 100.0,  # 100 MAGIC ≈ $6.60 at ~$0.066/MAGIC
        "score_threshold": 60.0,
        "slippage_bps": 50,
        "unwind_slippage_bps": 150,
        "signal_config": {
            "min_spread_bps": 30,
            "min_profit_usd": 0.02,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        # Risk limits for Day 1 live trading with $100 capital.
        # All values sit well below the absolute hard limits:
        #   ABSOLUTE_MAX_TRADE_USD=25  ABSOLUTE_MAX_DAILY_LOSS=20  ABSOLUTE_MIN_CAPITAL=50
        "risk_limits": {
            "max_trade_usd": 7.0,  # Day 1: ~$6.40/trade, slight headroom
            "max_daily_loss": 10.0,  # stop for the day after -$10
            "max_drawdown_pct": 0.15,  # halt at 15% drawdown ($15 on $100)
            "max_trades_per_hour": 20,
            "consecutive_loss_limit": 3,  # pause after 3 losses in a row
            "max_position_per_token": 500.0,
        },
        # mock_balances: for dry-run — bot needs MAGIC in wallet to pass inventory check.
        # Before going LIVE (dry_run=False): buy actual MAGIC and remove this.
        "mock_balances": {"USDC": 100.0, "MAGIC": 500.0},
        # dry_run=False only after ≥30 min observation confirms healthy signals.
        # Activate live trading: DRY_RUN=false python scripts/arb_bot.py --mode prod
        "dry_run": os.getenv("DRY_RUN", "true").lower() != "false",
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    config = _PROD_CONFIG if args.mode == MODE_PROD else _TEST_CONFIG
    bot = ArbBot(config)
    asyncio.run(bot.run())
