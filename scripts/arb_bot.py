"""
scripts/arb_bot.py — Main arbitrage bot loop integrating all weeks.

Week 1: ChainClient (optional, gated by rpc_url config)
Week 2: PricingEngine (optional, gated by rpc_url config)
Week 3: ExchangeClient, InventoryTracker, PnLEngine
Week 4: SignalGenerator, SignalScorer, Executor, FeeStructure
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from exchange.client import ExchangeClient
from executor.engine import ExecutionContext, Executor, ExecutorConfig, ExecutorState
from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.tracker import InventoryTracker, Venue
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import SignalScorer

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


class ArbBot:
    def __init__(self, config: dict) -> None:
        self.exchange = ExchangeClient(config)

        if config.get("rpc_url"):
            from chain.client import ChainClient
            from pricing.engine import PricingEngine

            self.chain_client = ChainClient([config["rpc_url"]])
            self.pricing_engine = PricingEngine(
                self.chain_client,
                config.get("fork_url", ""),
                config.get("ws_url", ""),
            )
        else:
            self.chain_client = None
            self.pricing_engine = None

        self.inventory = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        self.pnl_engine = PnLEngine()

        self.fees = FeeStructure()
        self.generator = SignalGenerator(
            self.exchange,
            self.pricing_engine,
            self.inventory,
            self.fees,
            config.get("signal_config", {}),
        )
        self.scorer = SignalScorer()
        self.executor = Executor(
            self.exchange,
            self.pricing_engine,
            self.inventory,
            ExecutorConfig(simulation_mode=config.get("simulation", True)),
        )

        self.pairs: list[str] = config.get("pairs", ["ETH/USDT"])
        self.trade_size: float = config.get("trade_size", 0.1)
        self.score_threshold: float = config.get("score_threshold", 60.0)
        self.running = False

    async def run(self) -> None:
        self.running = True
        log.info("Bot starting...")
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
        if cb.is_open():
            log.info("Circuit breaker open — %.0fs until reset", cb.time_until_reset())
            return

        for pair in self.pairs:
            signal = self.generator.generate(pair, self.trade_size)
            if signal is None:
                continue

            signal.score = self.scorer.score(signal, self.inventory.get_skews())

            if signal.score < self.score_threshold:
                log.info(
                    "Skipped: score below threshold (%.1f < %.1f)",
                    signal.score,
                    self.score_threshold,
                )
                continue

            log.info(
                "Signal: %s spread=%.1fbps score=%.0f",
                pair,
                signal.spread_bps,
                signal.score,
            )

            ctx = await self.executor.execute(signal)
            self.scorer.record_result(pair, ctx.state == ExecutorState.DONE)

            if ctx.state == ExecutorState.DONE and ctx.actual_net_pnl is not None:
                arb_record = execution_to_arb_record(ctx)
                self.pnl_engine.record(arb_record)
                log.info("SUCCESS: PnL=$%.2f", ctx.actual_net_pnl)
            else:
                failures = len([t for t in cb.failures]) if hasattr(cb, "failures") else "?"
                log.warning("FAILED: %s", ctx.error)
                log.warning(
                    "Circuit breaker: %s/%s failures",
                    failures,
                    cb.config.failure_threshold if hasattr(cb, "config") else "?",
                )

            await self._sync_balances()

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

    def _fetch_wallet_balances(self) -> dict:
        """Query on-chain balances via ChainClient.get_balance()."""
        raise NotImplementedError("Implement using ChainClient.get_balance()")

    def stop(self) -> None:
        self.running = False


def execution_to_arb_record(ctx: ExecutionContext) -> ArbRecord:
    """
    Bridge Week 4's ExecutionContext into Week 3's ArbRecord for PnL tracking.
    """
    signal = ctx.signal

    buy_venue = Venue.BINANCE if ctx.leg1_venue == "cex" else Venue.WALLET
    sell_venue = Venue.WALLET if ctx.leg2_venue == "dex" else Venue.BINANCE

    buy_leg = TradeLeg(
        id=f"{signal.signal_id}_buy",
        timestamp=datetime.fromtimestamp(ctx.started_at),
        venue=buy_venue,
        symbol=signal.pair,
        side="buy",
        amount=Decimal(str(ctx.leg1_fill_size or 0)),
        price=Decimal(str(ctx.leg1_fill_price or 0)),
        fee=Decimal("0"),
        fee_asset=signal.pair.split("/")[1],
    )
    sell_leg = TradeLeg(
        id=f"{signal.signal_id}_sell",
        timestamp=datetime.fromtimestamp(ctx.finished_at or ctx.started_at),
        venue=sell_venue,
        symbol=signal.pair,
        side="sell",
        amount=Decimal(str(ctx.leg2_fill_size or 0)),
        price=Decimal(str(ctx.leg2_fill_price or 0)),
        fee=Decimal("0"),
        fee_asset=signal.pair.split("/")[1],
    )
    return ArbRecord(
        id=signal.signal_id,
        timestamp=datetime.fromtimestamp(ctx.started_at),
        buy_leg=buy_leg,
        sell_leg=sell_leg,
    )


if __name__ == "__main__":
    config = {
        "apiKey": os.getenv("BINANCE_TESTNET_API_KEY"),
        "secret": os.getenv("BINANCE_TESTNET_SECRET"),
        "sandbox": True,
        "rpc_url": os.getenv("ETH_RPC_URL", ""),
        "pairs": ["ETH/USDT"],
        "trade_size": 0.1,
        "simulation": True,
    }
    bot = ArbBot(config)
    asyncio.run(bot.run())
