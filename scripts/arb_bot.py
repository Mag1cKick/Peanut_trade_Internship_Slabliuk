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

_ERC20_ADDRESSES: dict[str, str] = {
    "USDT": "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",
    "USDC": "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",
    "LINK": "0xf97f4df75117a78c1A5a0DBb814Af92458539FB4",
    "MAGIC": "0x539bdE0d7Dbd336b79148AA742883198BBF60342",
    "WBTC": "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f",
    "DAI": "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",
    "WETH": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
}
_ERC20_DECIMALS: dict[str, int] = {
    "USDT": 6,
    "USDC": 6,
    "LINK": 18,
    "MAGIC": 18,
    "WBTC": 8,
    "DAI": 18,
    "WETH": 18,
}
from config.settings import Config
from safety import (
    ABSOLUTE_MIN_CAPITAL,
    KILL_SWITCH_FILE,
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

        self.exchange = ExchangeClient({**config, "sandbox": not is_prod})

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
                if config.get("pool_addresses"):
                    from core.types import Address

                    self.pricing_engine.load_pools([Address(a) for a in config["pool_addresses"]])
                log.info("PricingEngine ready (%d pool(s))", len(self.pricing_engine.pools))
            except Exception as exc:
                log.warning(
                    "PricingEngine setup failed (%s) — using direct pricer for execution", exc
                )

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

        _cex_bps = config.get("cex_taker_bps", Config.CEX_TAKER_BPS)
        _dex_bps = config.get("dex_swap_bps", Config.DEX_SWAP_BPS)
        _gas = config.get("gas_cost_usd", Config.GAS_COST_USD)
        log.info("FeeStructure: cex=%sbps dex=%sbps gas=$%s", _cex_bps, _dex_bps, _gas)
        self.fees = FeeStructure(
            cex_taker_bps=_cex_bps,
            dex_swap_bps=_dex_bps,
            gas_cost_usd=_gas,
        )
        self.generator = SignalGenerator(
            self.exchange,
            self.dex_pricer,
            self.inventory,
            self.fees,
            config.get("signal_config", {}),
        )
        from strategy.scorer import ScorerConfig

        _sc = config.get("scorer_config", {})
        self.scorer = SignalScorer(ScorerConfig(**_sc) if _sc else None)
        self.executor = Executor(
            self.exchange,
            self.pricing_engine or self.dex_pricer,
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
        self._trade_usd: float = config.get("trade_usd", 0.0)
        self.score_threshold: float = config.get("score_threshold", 50.0)
        self.min_profit_usd: float = config.get("min_profit_usd", 0.03)
        self._max_dex_buy_trades: int = int(
            os.getenv("MAX_DEX_BUY_TRADES", config.get("max_dex_buy_trades", 0))
        )
        self._dex_buy_trade_count: int = 0
        self._bep20_link_address: str = os.getenv("BINANCE_BEP20_LINK_ADDRESS", "")
        self.metrics_port: int = config.get("metrics_port", 0)
        self._wallet_address: str | None = (
            wallet.address if wallet else config.get("wallet_address")
        )
        self.running = False

        self._queue: SignalQueue = SignalQueue(maxsize=config.get("queue_maxsize", 50))

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

        raw_mock = config.get("mock_balances", {})
        self._mock_balances: dict = (
            {k: {"free": str(v), "locked": "0"} for k, v in raw_mock.items()} if raw_mock else {}
        )
        if self._mock_balances:
            log.info("Mock balances: %s", {k: v["free"] for k, v in self._mock_balances.items()})

        self._session_start: float = time.time()
        self._last_status_log: float = 0.0
        self._signals_seen: int = 0
        self._signals_skipped_reasons: dict[str, int] = {}
        self._dry_run_count: int = 0
        self._dry_run_spreads: list[float] = []
        self._dry_run_pnls: list[float] = []
        self._dry_run_scores: list[float] = []

        self.alerter = make_alerter()

        self._error_times: deque[float] = deque()
        self._cb_last_alerted: float = 0.0
        self._last_balance_sync: float = 0.0
        self._balance_sync_interval: float = 30.0

        self.dry_run: bool = config.get("dry_run", True)
        self.one_shot: bool = config.get("one_shot", False)
        if self.dry_run:
            log.info("DRY RUN mode — signals will be logged but NOT executed")

        self._portfolio_start: float | None = None

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
        self._portfolio_start = await self._fetch_portfolio_value()
        if self._portfolio_start:
            log.info("Portfolio value at start: $%.4f", self._portfolio_start)
            self.risk_manager.current_capital = self._portfolio_start

        asyncio.create_task(self._heartbeat_loop())

        try:
            while self.running:
                try:
                    await self._tick()
                    await asyncio.sleep(1)
                except Exception as exc:
                    log.error("Tick error: %s", exc)
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
            now_m = time.monotonic()
            if now_m - self._cb_last_alerted > 300:
                self._cb_last_alerted = now_m
                logging.critical(
                    "KILL SWITCH ACTIVE — trading paused, remove %s to resume", KILL_SWITCH_FILE
                )
                await self.alerter.send(
                    f"🚨 <b>KILL SWITCH ACTIVE</b> — trading paused\n"
                    f"Remove <code>{KILL_SWITCH_FILE}</code> to resume"
                )
            return

        cb = self.executor.circuit_breaker

        now_t = time.monotonic()
        if now_t - self._last_balance_sync >= self._balance_sync_interval:
            await self._sync_balances()
            self._last_balance_sync = now_t

        if now_t - self._last_status_log >= 60:
            self._last_status_log = now_t
            try:
                session_trades = len(self.pnl_engine.trades)
                session_pnl = sum(float(t.net_pnl) for t in self.pnl_engine.trades)
                inv = self.inventory
                from inventory.tracker import Venue

                _bl = inv._balances.get(Venue.BINANCE, {}).get("LINK")
                _wu = inv._balances.get(Venue.WALLET, {}).get("USDT")
                b_link = float(_bl.free) if _bl else 0.0
                w_usdt = float(_wu.free) if _wu else 0.0
                log.info(
                    "STATUS | session: %d trades  pnl=%+.4f | "
                    "binance LINK=%.2f  wallet USDT=$%.2f | "
                    "signals seen=%d  skipped=%d",
                    session_trades,
                    session_pnl,
                    b_link,
                    w_usdt,
                    self._signals_seen,
                    sum(self._signals_skipped_reasons.values()),
                )
            except Exception:
                pass

        CIRCUIT_BREAKER_OPEN.set(1 if cb.is_open() else 0)

        if cb.is_open():
            secs = cb.time_until_reset()
            log.info("Circuit breaker open — %.0fs until reset", secs)
            now_m = time.monotonic()
            if now_m - self._cb_last_alerted > 300:
                self._cb_last_alerted = now_m
                await self.alerter.send(f"⚡ <b>Circuit breaker OPEN</b>\nResets in {secs:.0f}s")
            return

        async def _generate_one(pair: str) -> None:
            if self._trade_usd > 0:
                try:
                    ob = self.exchange.fetch_order_book(pair)
                    mid = (float(ob["best_bid"][0]) + float(ob["best_ask"][0])) / 2
                    # Floor to 2 decimal places to stay strictly under limit
                    import math

                    dyn_size = (
                        math.floor(self._trade_usd / mid * 100) / 100
                        if mid > 0
                        else self.trade_size
                    )
                except Exception:
                    dyn_size = self.trade_size
            else:
                dyn_size = self.trade_size

            signal = await asyncio.get_event_loop().run_in_executor(
                None, self.generator.generate, pair, dyn_size
            )
            if signal is None:
                return
            SIGNALS_GENERATED.labels(pair=pair).inc()
            SPREAD_BPS.labels(pair=pair).observe(signal.spread_bps)

            valid, reason = self.pre_trade_validator.validate_signal(signal)
            if not valid:
                SIGNALS_SKIPPED.labels(pair=pair, reason="validation").inc()
                self._signals_skipped_reasons["validation"] = (
                    self._signals_skipped_reasons.get("validation", 0) + 1
                )
                log.warning("Validation failed for %s: %s", pair, reason)
                return

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
            log.info(
                "Signal: %s spread=%.1fbps score=%.0f | cex_bid=%.5f dex_buy=%.5f net=$%.4f",
                pair,
                signal.spread_bps,
                signal.score,
                signal.cex_price,
                signal.dex_price,
                float(signal.expected_net_pnl),
            )
            if float(signal.expected_net_pnl) < self.min_profit_usd:
                SIGNALS_SKIPPED.labels(pair=pair, reason="low_profit").inc()
                self._signals_skipped_reasons["low_profit"] = (
                    self._signals_skipped_reasons.get("low_profit", 0) + 1
                )
                log.debug(
                    "Skipped %s: net_pnl $%.4f below min $%.2f",
                    pair,
                    float(signal.expected_net_pnl),
                    self.min_profit_usd,
                )
                return
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

        while True:
            signal = self._queue.get()
            if signal is None:
                break

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
            if self.one_shot:
                log.info("ONE-SHOT mode — stopping after first trade")
                self.stop()
            if ctx.state == ExecutorState.DONE and not self.dry_run:
                await self._sync_balances()
                await self._verify_balances(ctx)
                from strategy.signal import Direction

                if ctx.signal.direction == Direction.BUY_DEX_SELL_CEX:
                    self._dex_buy_trade_count += 1
                    if (
                        self._max_dex_buy_trades > 0
                        and self._dex_buy_trade_count >= self._max_dex_buy_trades
                    ):
                        await self.alerter.send(
                            f"Max DEX-buy trades reached ({self._dex_buy_trade_count}/{self._max_dex_buy_trades}). "
                            f"Pausing — buy more LINK on Binance then restart."
                        )
                        log.warning(
                            "Max DEX buy trades (%d) reached — stopping", self._max_dex_buy_trades
                        )
                        self.stop()
            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                PNL_USD.labels(pair=signal.pair).observe(ctx.actual_net_pnl)
                arb_record = execution_to_arb_record(ctx, self.fees)
                self.pnl_engine.record(arb_record)
                self.risk_manager.record_trade(ctx.actual_net_pnl)
                await self._db_record_trade(ctx)
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

                # Arm kill switch after N consecutive losses — pause until reviewed
                if (
                    ctx.actual_net_pnl < 0
                    and self.risk_manager._consecutive_losses
                    >= self.risk_limits.consecutive_loss_limit
                ):
                    msg = f"{self.risk_manager._consecutive_losses} consecutive losses"
                    trigger_kill_switch(msg)
                    await self.alerter.send(
                        f"⚠️ <b>Trading paused</b> — {msg}\n"
                        f"Review logs, then: <code>rm {KILL_SWITCH_FILE}</code> to resume"
                    )
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

            if cb.is_open():
                CIRCUIT_BREAKER_OPEN.set(1)
                break

    async def _sync_balances(self) -> None:
        if self._mock_balances:
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
        if not result:
            return 0
        if isinstance(result, bytes | bytearray):
            return int.from_bytes(result, "big")
        if result in ("0x", "0x0"):
            return 0
        return int(result, 16)

    def stop(self) -> None:
        self.running = False

    async def _db_record_trade(self, ctx: ExecutionContext) -> None:
        """Persist a completed trade to the SQLite ledger."""
        try:
            from db.trades import TradeRecord, all_trades, insert

            signal = ctx.signal
            gross = (
                (ctx.leg2_fill_price - ctx.leg1_fill_price) * ctx.leg1_fill_size
                if ctx.leg1_fill_price and ctx.leg2_fill_price
                else 0.0
            )
            portfolio = await self._fetch_portfolio_value() or 0.0
            rec = TradeRecord(
                ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                pair=signal.pair,
                direction=signal.direction.value,
                size=float(signal.size),
                dex_price=ctx.leg1_fill_price or float(signal.dex_price),
                cex_price=ctx.leg2_fill_price or float(signal.cex_price),
                spread_bps=float(signal.spread_bps),
                gross_pnl=gross,
                net_pnl=ctx.actual_net_pnl or 0.0,
                gas_usd=float(self.fees.gas_cost_usd),
                portfolio_usd=portfolio,
                notes="",
            )
            insert(rec)
            log.info(
                "Trade saved to DB (net=%.4f  total_trades=%d)", rec.net_pnl, len(all_trades())
            )
        except Exception as exc:
            log.warning("DB record failed: %s", exc)

    async def _rebalance_if_needed(self) -> None:
        """
        Rebalance when either venue can't fund the next trade.

        Calculates live 50/50 targets from total holdings across both venues,
        then moves only the delta needed to reach that split.

        Automated:
          wallet LINK surplus → bridge to BSC via Synapse   (BINANCE_BEP20_LINK_ADDRESS)
          wallet USDT surplus → ERC20 transfer to Binance   (BINANCE_USDT_DEPOSIT_ADDRESS)
          Binance USDT surplus → Binance API withdrawal      (API key needs withdrawal perm)
          Binance LINK surplus → alert only (no direct Arbitrum deposit on Binance)
        """
        if self.dry_run:
            return

        base, quote = self.pairs[0].split("/")
        from inventory.tracker import Venue

        w_link = float(self.inventory.get_available(Venue.WALLET, base))
        w_usdt = float(self.inventory.get_available(Venue.WALLET, quote))
        b_link = float(self.inventory.get_available(Venue.BINANCE, base))
        b_usdt = float(self.inventory.get_available(Venue.BINANCE, quote))

        trade_usd = self._trade_usd or (self.trade_size * 10)
        if b_link >= self.trade_size and w_usdt >= trade_usd:
            return

        target_link = (w_link + b_link) / 2
        target_usdt = (w_usdt + b_usdt) / 2

        log.info(
            "Rebalance triggered. Totals: %.3f %s  $%.2f USDT  →  target %.3f/%.3f  $%.2f/$%.2f",
            w_link + b_link,
            base,
            w_usdt + b_usdt,
            target_link,
            target_link,
            target_usdt,
            target_usdt,
        )

        actions: list[str] = []
        MIN_LINK_MOVE = 0.5
        MIN_USDT_MOVE = 3.0

        link_delta = w_link - target_link

        if link_delta >= MIN_LINK_MOVE and self._bep20_link_address:
            log.info("Bridging %.4f %s wallet -> BSC", link_delta, base)
            try:
                import subprocess

                res = subprocess.run(
                    [
                        sys.executable,
                        "scripts/bridge_link_bsc.py",
                        "--to",
                        self._bep20_link_address,
                        "--amount",
                        f"{link_delta:.4f}",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                    cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                )
                if res.returncode == 0:
                    actions.append(
                        f"Bridged {link_delta:.4f} {base} wallet -> BSC (then buy on Binance)"
                    )
                else:
                    actions.append(f"Bridge failed: {res.stderr[-120:]}")
            except Exception as exc:
                actions.append(f"Bridge error: {exc}")
        elif link_delta >= MIN_LINK_MOVE:
            actions.append(
                f"Bridge {link_delta:.4f} {base} wallet->BSC: set BINANCE_BEP20_LINK_ADDRESS in .env"
            )
        elif link_delta < -MIN_LINK_MOVE:
            actions.append(
                f"Binance has {-link_delta:.4f} {base} surplus. "
                f"Sell excess on Binance or manually send to wallet."
            )
        elif b_link < self.trade_size:
            actions.append(
                f"Buy {target_link:.2f} {base} on Binance (have {b_link:.4f}, need {self.trade_size:.2f})"
            )

        usdt_delta = w_usdt - target_usdt

        usdt_dep_addr = os.getenv("BINANCE_USDT_DEPOSIT_ADDRESS", "")

        if usdt_delta >= MIN_USDT_MOVE and usdt_dep_addr and self.chain_client:
            log.info("Sending $%.2f USDT wallet -> Binance (Arbitrum)", usdt_delta)
            try:
                from eth_abi import encode as abi_encode

                from chain.builder import TransactionBuilder
                from core.types import Address, TokenAmount

                wallet_mgr = self.executor.config.wallet
                amount_wei = int(usdt_delta * 1e6)
                data = bytes.fromhex("a9059cbb") + abi_encode(
                    ["address", "uint256"], [usdt_dep_addr, amount_wei]
                )
                tx = (
                    TransactionBuilder(self.chain_client, wallet_mgr)
                    .to(Address(_ERC20_ADDRESSES["USDT"]))
                    .value(TokenAmount(raw=0, decimals=18, symbol="ETH"))
                    .data(data)
                    .chain_id(42161)
                    .with_gas_estimate(buffer=1.2)
                    .with_gas_price("medium")
                    .build()
                )
                signed = wallet_mgr.sign_transaction(tx.to_dict())
                raw = (
                    signed.raw_transaction
                    if hasattr(signed, "raw_transaction")
                    else signed.rawTransaction
                )
                txh = self.chain_client.send_transaction(raw)
                self.chain_client.wait_for_receipt(txh, timeout=60)
                actions.append(f"Sent ${usdt_delta:.2f} USDT wallet -> Binance")
            except Exception as exc:
                actions.append(f"USDT transfer failed: {exc}")
        elif usdt_delta >= MIN_USDT_MOVE:
            actions.append(
                f"Send ${usdt_delta:.2f} USDT wallet->Binance: set BINANCE_USDT_DEPOSIT_ADDRESS in .env"
            )

        elif usdt_delta <= -MIN_USDT_MOVE:
            actions.append(
                f"Manual: withdraw ${-usdt_delta:.2f} USDT from Binance to wallet via Arbitrum One"
            )

        if actions:
            summary = "\n".join(f"• {a}" for a in actions)
            log.info("Rebalance actions: %s", summary.replace("\n", " | "))
            await self.alerter.send(
                f"Rebalance\n"
                f"Totals: {w_link+b_link:.3f} {base}  ${w_usdt+b_usdt:.2f} USDT\n"
                f"Target: {target_link:.3f}/{target_link:.3f} {base}  ${target_usdt:.2f}/${target_usdt:.2f} USDT\n"
                f"\n{summary}"
            )

    async def _verify_balances(self, ctx: ExecutionContext) -> None:
        """
        After a real trade, compare actual exchange balances against the
        inventory tracker's expected values. A mismatch > threshold means
        something went wrong (partial fill, fee accounting error, API bug)
        and the bot should stop immediately.
        """
        pair = ctx.signal.pair
        base, quote = pair.split("/")
        threshold = 0.01

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

    async def _fetch_portfolio_value(self) -> float | None:
        """
        Fetch live portfolio value in USD: wallet USDT + wallet LINK + Binance USDT + Binance LINK.
        Returns None on failure.
        """
        import json
        import urllib.request as _ureq

        try:
            url = f"https://api.binance.com/api/v3/ticker/price?symbol={self.pairs[0].replace('/', '')}"
            price = float(json.loads(_ureq.urlopen(url, timeout=4).read())["price"])
        except Exception:
            return None

        base, quote = self.pairs[0].split("/")
        try:
            from inventory.tracker import Venue

            w_quote = float(self.inventory.get_available(Venue.WALLET, quote))
            w_base = float(self.inventory.get_available(Venue.WALLET, base))
            b_quote = float(self.inventory.get_available(Venue.BINANCE, quote))
            b_base = float(self.inventory.get_available(Venue.BINANCE, base))
            total = w_quote + w_base * price + b_quote + b_base * price
            return total
        except Exception:
            return None

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
        await self._sync_balances()
        portfolio_end = await self._fetch_portfolio_value()
        trades = self.pnl_engine.trades
        skipped_total = sum(self._signals_skipped_reasons.values())

        if portfolio_end is not None and self._portfolio_start is not None:
            session_pnl = portfolio_end - self._portfolio_start
            sign = "+" if session_pnl >= 0 else ""
            portfolio_line = f"💼 Portfolio: ${self._portfolio_start:.2f} → ${portfolio_end:.2f}  ({sign}${session_pnl:.4f})"
        else:
            portfolio_line = ""

        lines: list[str] = [
            f"📊 <b>Session Summary [{mode_label}]</b>",
            "",
            f"⏱ Duration: {duration_min:.0f} min",
            f"🔍 Signals: {self._signals_seen} passed  {skipped_total} blocked",
        ]
        if portfolio_line:
            lines.append(portfolio_line)
        if skipped_total:
            reasons = "  ".join(
                f"{r}:{n}" for r, n in sorted(self._signals_skipped_reasons.items())
            )
            lines.append(f"   blocked by: {reasons}")

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
        import re

        plain = re.sub(r"<[^>]+>", "", "\n".join(lines))
        log.info("\n" + "=" * 60 + "\n" + plain + "\n" + "=" * 60)

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
    parser.add_argument(
        "--day",
        type=int,
        choices=[1, 2, 3, 4, 5],
        default=None,
        help=(
            "Week 6 trading day (1-5). Automatically sets risk limits per assignment schedule: "
            "Day 1=$5 max, Day 2-3=$10, Day 4-5=$15. Overrides risk_limits in config."
        ),
    )
    parser.add_argument(
        "--one-shot",
        action="store_true",
        help="Execute exactly one trade then stop. Useful for pipeline testing.",
    )
    args = parser.parse_args()

    _DAY_RISK_LIMITS: dict[int, dict] = {
        1: {
            "max_trade_usd": 7.0,
            "max_daily_loss": 10.0,
            "max_drawdown_pct": 0.15,
            "trade_usd": 6.90,
        },
        2: {
            "max_trade_usd": 10.0,
            "max_daily_loss": 15.0,
            "max_drawdown_pct": 0.20,
            "trade_usd": 9.90,
        },
        3: {
            "max_trade_usd": 10.0,
            "max_daily_loss": 15.0,
            "max_drawdown_pct": 0.20,
            "trade_usd": 9.90,
        },
        4: {
            "max_trade_usd": 15.0,
            "max_daily_loss": 20.0,
            "max_drawdown_pct": 0.20,
            "trade_usd": 14.90,
        },
        5: {
            "max_trade_usd": 15.0,
            "max_daily_loss": 20.0,
            "max_drawdown_pct": 0.20,
            "trade_usd": 14.90,
        },
    }

    _TEST_CONFIG: dict = {
        "mode": MODE_TEST,
        "apiKey": os.getenv("BINANCE_TESTNET_API_KEY"),
        "secret": os.getenv("BINANCE_TESTNET_SECRET"),
        "rpc_url": os.getenv("ARB_RPC_URL", os.getenv("ETH_RPC_URL", "")),
        "network": "arbitrum-v3",
        "pairs": ["MAGIC/USDT"],
        "trade_size": 100.0,
        "score_threshold": 50.0,
        "signal_config": {
            "min_spread_bps": 20,
            "min_profit_usd": 0.025,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        "risk_limits": {
            "max_trade_usd": 25.0,
            "max_daily_loss": 20.0,
            "max_position_per_token": 500.0,
        },
        "dry_run": True,
        "mock_balances": {"USDT": 100.0, "MAGIC": 500.0},
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    _PROD_CONFIG: dict = {
        "mode": MODE_PROD,
        "apiKey": os.getenv("BINANCE_API_KEY"),
        "secret": os.getenv("BINANCE_SECRET"),
        "rpc_url": os.getenv("ARB_RPC_URL", ""),
        "network": "arbitrum-v3",
        "private_key_env": "PRIVATE_KEY",  # pragma: allowlist secret
        # Quoter-verified: 1 LINK trade deviates only 0.3% from slot0 (just pool fee).
        "pairs": ["LINK/USDT"],
        "trade_size": 1.0,
        "trade_usd": 9.90,
        "score_threshold": 50.0,
        "slippage_bps": 200,
        "unwind_slippage_bps": 150,
        "dex_swap_bps": 0,
        "gas_cost_usd": 0.02,
        "signal_config": {
            "min_spread_bps": 15,
            "min_profit_usd": 0.025,
            "cooldown_seconds": 2,
            "signal_ttl_seconds": 5,
        },
        "scorer_config": {
            "excellent_spread_bps": 50.0,
            "min_spread_bps": 15.0,
            "liquid_spread_threshold_bps": 15.0,
        },
        "risk_limits": {
            "max_trade_usd": 7.0,
            "max_daily_loss": 10.0,
            "max_drawdown_pct": 0.15,
            "max_trades_per_hour": 20,
            "consecutive_loss_limit": 3,
            "max_position_per_token": 500.0,
        },
        "dry_run": os.getenv("DRY_RUN", "true").lower() != "false",
        "metrics_port": int(os.getenv("METRICS_PORT", "8000")),
    }

    config = _PROD_CONFIG if args.mode == MODE_PROD else _TEST_CONFIG

    if args.one_shot:
        config = {**config, "one_shot": True}

    if args.day is not None:
        day_limits = _DAY_RISK_LIMITS[args.day]
        risk_keys = {"max_trade_usd", "max_daily_loss", "max_drawdown_pct"}
        config = {
            **config,
            "risk_limits": {
                **config.get("risk_limits", {}),
                **{k: v for k, v in day_limits.items() if k in risk_keys},
            },
            "trade_usd": day_limits["trade_usd"],
        }
        log.info(
            "Week 6 Day %d limits: max_trade=$%.0f  trade_usd=$%.2f  max_daily_loss=$%.0f  max_drawdown=%.0f%%",
            args.day,
            day_limits["max_trade_usd"],
            day_limits["trade_usd"],
            day_limits["max_daily_loss"],
            day_limits["max_drawdown_pct"] * 100,
        )

    bot = ArbBot(config)
    asyncio.run(bot.run())
