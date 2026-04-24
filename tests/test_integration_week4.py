"""
tests/test_integration_week4.py — Week 4 integration tests.

Two test classes:
  - TestPipelineMocked   : full pipeline with mocked exchange, always runs in CI
  - TestPipelineRealData : full pipeline with live Binance public order book,
                           requires internet (pytest -m network to run explicitly)

Real-data tests fetch from api.binance.com/api/v3/depth — no API key needed.
DEX prices use the generator's stub (mid ± 0.8%) since no RPC is available,
but all CEX prices are live market data.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from executor.engine import Executor, ExecutorConfig, ExecutorState
from executor.queue import SignalQueue
from executor.recovery import CircuitBreaker, CircuitBreakerConfig
from inventory.tracker import InventoryTracker, Venue
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import SignalScorer

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_ob_dict(bid: float, ask: float) -> dict:
    """Format bid/ask into the dict SignalGenerator._fetch_prices() expects."""
    bid_d, ask_d = Decimal(str(bid)), Decimal(str(ask))
    mid = (bid_d + ask_d) / Decimal("2")
    return {
        "bids": [(bid_d, Decimal("10"))],
        "asks": [(ask_d, Decimal("10"))],
        "best_bid": (bid_d, Decimal("10")),
        "best_ask": (ask_d, Decimal("10")),
        "mid_price": mid,
        "spread_bps": (ask_d - bid_d) / mid * Decimal("10000"),
        "timestamp": int(time.time() * 1000),
    }


def _fetch_binance_orderbook(symbol: str = "ETHUSDT") -> dict:
    """
    Fetch top-of-book from Binance public REST API (no auth required).
    Returns formatted dict ready for use as exchange.fetch_order_book return value.
    Raises urllib.error.URLError on network failure.
    """
    url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=5"
    with urllib.request.urlopen(url, timeout=5) as resp:
        data = json.loads(resp.read())
    best_bid = float(data["bids"][0][0])
    best_ask = float(data["asks"][0][0])
    return _make_ob_dict(best_bid, best_ask)


def _make_tracker() -> InventoryTracker:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": "100", "locked": "0"},
            "USDT": {"free": "500000", "locked": "0"},
        },
    )
    tracker.update_from_wallet(Venue.WALLET, {"ETH": "100", "USDT": "500000"})
    return tracker


def _make_generator(ob: dict, tracker: InventoryTracker | None = None) -> SignalGenerator:
    exchange = MagicMock()
    exchange.fetch_order_book.return_value = ob
    return SignalGenerator(
        exchange_client=exchange,
        pricing_module=None,
        inventory_tracker=tracker or _make_tracker(),
        fee_structure=FeeStructure(cex_taker_bps=10, dex_swap_bps=30, gas_cost_usd=2),
        config={
            "min_spread_bps": 50,
            "min_profit_usd": 1.0,
            "cooldown_seconds": 0,
            "signal_ttl_seconds": 30,
        },
    )


def _generate_scored(ob: dict, pair: str = "ETH/USDT", size: float = 1.0) -> object:
    """Generate a signal and score it so is_valid() returns True."""
    gen = _make_generator(ob)
    scorer = SignalScorer()
    sig = gen.generate(pair, size)
    assert sig is not None, "No signal generated — check OB spread"
    scorer.score(sig, [])
    return sig


def _make_executor(use_flashbots: bool = False) -> Executor:
    return Executor(
        exchange_client=None,
        pricing_module=None,
        inventory_tracker=None,
        config=ExecutorConfig(simulation_mode=True, use_flashbots=use_flashbots),
    )


def _make_bot(ob: dict):
    """Instantiate ArbBot with a real-data (or test) order book."""
    from scripts.arb_bot import ArbBot

    bot = ArbBot(
        {
            "apiKey": "fake",  # pragma: allowlist secret
            "secret": "fake",  # pragma: allowlist secret
            "sandbox": True,
            "simulation": True,
            "pairs": ["ETH/USDT"],
            "trade_size": 1.0,
            "score_threshold": 0.0,
            "signal_config": {
                "min_spread_bps": 50,
                "min_profit_usd": 1.0,
                "cooldown_seconds": 0,
            },
        }
    )
    bot.exchange.fetch_order_book = MagicMock(return_value=ob)
    bot.exchange.fetch_balance = MagicMock(
        return_value={
            "ETH": {"free": "100", "locked": "0"},
            "USDT": {"free": "500000", "locked": "0"},
        }
    )
    bot.inventory.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": "100", "locked": "0"},
            "USDT": {"free": "500000", "locked": "0"},
        },
    )
    bot.inventory.update_from_wallet(Venue.WALLET, {"ETH": "100", "USDT": "500000"})
    return bot


# ---------------------------------------------------------------------------
# Mocked pipeline — always runs in CI
# ---------------------------------------------------------------------------


class TestPipelineMocked:
    """Full Week 4 pipeline with a controlled mocked order book."""

    OB = _make_ob_dict(bid=2000.0, ask=2001.0)

    # --- signal generation ------------------------------------------------

    def test_generator_produces_signal(self):
        gen = _make_generator(self.OB)
        sig = gen.generate("ETH/USDT", 1.0)
        assert sig is not None
        assert sig.spread_bps > 0
        assert sig.expected_net_pnl > 0

    def test_signal_economics_use_decimal(self):
        gen = _make_generator(self.OB)
        sig = gen.generate("ETH/USDT", 1.0)
        assert isinstance(sig.expected_fees, Decimal)
        assert isinstance(sig.expected_net_pnl, Decimal)
        assert sig.expected_net_pnl == sig.expected_gross_pnl - sig.expected_fees

    def test_bid_ask_spread_bps_populated(self):
        gen = _make_generator(self.OB)
        sig = gen.generate("ETH/USDT", 1.0)
        assert sig.bid_ask_spread_bps > 0

    # --- scoring ----------------------------------------------------------

    def test_scorer_produces_score_in_range(self):
        gen = _make_generator(self.OB)
        scorer = SignalScorer()
        sig = gen.generate("ETH/USDT", 1.0)
        score = scorer.score(sig, [])
        assert 0.0 <= score <= 100.0

    def test_liquidity_score_uses_bid_ask_spread(self):
        scorer = SignalScorer()
        tight = _make_ob_dict(2000.0, 2000.1)  # ~0.5 bps spread
        wide = _make_ob_dict(2000.0, 2004.0)  # ~20 bps spread
        gen_t = _make_generator(tight)
        gen_w = _make_generator(wide)
        sig_t = gen_t.generate("ETH/USDT", 1.0)
        sig_w = gen_w.generate("ETH/USDT", 1.0)
        if sig_t is None or sig_w is None:
            pytest.skip("Spread too small to generate signal")
        assert scorer._score_liquidity(sig_t.bid_ask_spread_bps) > scorer._score_liquidity(
            sig_w.bid_ask_spread_bps
        )

    def test_history_score_degrades_after_failures(self):
        gen = _make_generator(self.OB)
        scorer = SignalScorer()
        sig1 = gen.generate("ETH/USDT", 1.0)
        score_before = scorer.score(sig1, [])
        for _ in range(5):
            scorer.record_result("ETH/USDT", False)
        sig2 = gen.generate("ETH/USDT", 1.0)
        score_after = scorer.score(sig2, [])
        assert score_after < score_before

    def test_history_score_improves_after_wins(self):
        gen = _make_generator(self.OB)
        scorer = SignalScorer()
        sig1 = gen.generate("ETH/USDT", 1.0)
        score_neutral = scorer.score(sig1, [])
        for _ in range(5):
            scorer.record_result("ETH/USDT", True)
        sig2 = gen.generate("ETH/USDT", 1.0)
        score_after = scorer.score(sig2, [])
        assert score_after > score_neutral

    # --- queue ------------------------------------------------------------

    def test_higher_score_dequeues_first(self):
        gen = _make_generator(self.OB)
        q = SignalQueue()
        sig_low = gen.generate("ETH/USDT", 1.0)
        sig_low.signal_id = "low"
        sig_low.score = 40.0
        sig_high = gen.generate("ETH/USDT", 1.0)
        sig_high.signal_id = "high"
        sig_high.score = 90.0
        q.put(sig_low)
        q.put(sig_high)
        assert q.get().signal_id == "high"

    def test_expired_signal_skipped_in_queue(self):
        gen = _make_generator(self.OB)
        q = SignalQueue()
        expired = gen.generate("ETH/USDT", 1.0)
        expired.signal_id = "expired"
        expired.score = 99.0
        expired.expiry = time.time() - 1.0
        valid = gen.generate("ETH/USDT", 1.0)
        valid.signal_id = "valid"
        valid.score = 50.0
        q.put(expired)
        q.put(valid)
        assert q.get().signal_id == "valid"

    # --- execution --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_execute_reaches_done(self):
        sig = _generate_scored(self.OB)
        ctx = await _make_executor().execute(sig)
        assert ctx.state == ExecutorState.DONE
        assert ctx.actual_net_pnl is not None

    @pytest.mark.asyncio
    async def test_execute_cex_first_venues(self):
        sig = _generate_scored(self.OB)
        ctx = await _make_executor(use_flashbots=False).execute(sig)
        assert ctx.leg1_venue == "cex"
        assert ctx.leg2_venue == "dex"

    @pytest.mark.asyncio
    async def test_execute_dex_first_venues(self):
        sig = _generate_scored(self.OB)
        ctx = await _make_executor(use_flashbots=True).execute(sig)
        assert ctx.leg1_venue == "dex"
        assert ctx.leg2_venue == "cex"

    # --- circuit breaker --------------------------------------------------

    @pytest.mark.asyncio
    async def test_failures_trip_circuit_breaker(self):
        executor = _make_executor()
        executor.circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=2))
        scorer = SignalScorer()
        gen = _make_generator(self.OB)

        async def fail_cex(*_):
            return {"success": False, "error": "rejected"}

        with patch.object(executor, "_execute_cex_leg", fail_cex):
            for i in range(3):
                sig = gen.generate("ETH/USDT", 1.0)
                sig.signal_id = f"sig_{i}"
                scorer.score(sig, [])
                await executor.execute(sig)

        assert executor.circuit_breaker.is_open()

    @pytest.mark.asyncio
    async def test_open_breaker_blocks_execution(self):
        executor = _make_executor()
        executor.circuit_breaker.trip()
        sig = _generate_scored(self.OB)
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.FAILED
        assert "Circuit breaker" in ctx.error

    @pytest.mark.asyncio
    async def test_circuit_breaker_trips_counter_increments(self):
        from monitoring.metrics import CIRCUIT_BREAKER_TRIPS

        before = CIRCUIT_BREAKER_TRIPS._value.get()
        cb = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        cb.record_failure()
        assert CIRCUIT_BREAKER_TRIPS._value.get() == before + 1

    # --- replay protection ------------------------------------------------

    @pytest.mark.asyncio
    async def test_duplicate_signal_blocked(self):
        sig = _generate_scored(self.OB)
        executor = _make_executor()
        ctx1 = await executor.execute(sig)
        ctx2 = await executor.execute(sig)
        assert ctx1.state == ExecutorState.DONE
        assert ctx2.state == ExecutorState.FAILED
        assert "Duplicate" in ctx2.error

    @pytest.mark.asyncio
    async def test_replay_blocks_counter_increments(self):
        from monitoring.metrics import REPLAY_BLOCKS

        before = REPLAY_BLOCKS._value.get()
        sig = _generate_scored(self.OB)
        executor = _make_executor()
        await executor.execute(sig)
        await executor.execute(sig)
        assert REPLAY_BLOCKS._value.get() == before + 1

    # --- unwind path ------------------------------------------------------

    @pytest.mark.asyncio
    async def test_dex_failure_unwinds_and_increments_counter(self):
        from monitoring.metrics import UNWINDS

        before = UNWINDS.labels(pair="ETH/USDT")._value.get()
        sig = _generate_scored(self.OB)
        executor = _make_executor(use_flashbots=False)

        async def fail_dex(*_):
            return {"success": False, "error": "slippage"}

        with patch.object(executor, "_execute_dex_leg", fail_dex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "unwound" in ctx.error.lower()
        assert UNWINDS.labels(pair="ETH/USDT")._value.get() == before + 1

    @pytest.mark.asyncio
    async def test_cex_failure_leg1_no_unwind(self):
        """CEX failure before any fill → FAILED immediately, no unwind."""
        sig = _generate_scored(self.OB)
        executor = _make_executor(use_flashbots=False)

        async def fail_cex(*_):
            return {"success": False, "error": "rejected"}

        with patch.object(executor, "_execute_cex_leg", fail_cex):
            ctx = await executor.execute(sig)

        assert ctx.state == ExecutorState.FAILED
        assert "unwound" not in (ctx.error or "").lower()

    # --- full bot tick ----------------------------------------------------

    @pytest.mark.asyncio
    async def test_tick_executes_and_records_pnl(self):
        bot = _make_bot(self.OB)
        await bot._tick()
        assert len(bot.pnl_engine.trades) >= 1

    @pytest.mark.asyncio
    async def test_tick_increments_signals_generated(self):
        from monitoring.metrics import SIGNALS_GENERATED

        before = SIGNALS_GENERATED.labels(pair="ETH/USDT")._value.get()
        bot = _make_bot(self.OB)
        await bot._tick()
        assert SIGNALS_GENERATED.labels(pair="ETH/USDT")._value.get() > before

    @pytest.mark.asyncio
    async def test_tick_increments_trades_executed(self):
        from monitoring.metrics import TRADES_EXECUTED

        before = TRADES_EXECUTED.labels(pair="ETH/USDT", state="done")._value.get()
        bot = _make_bot(self.OB)
        await bot._tick()
        assert TRADES_EXECUTED.labels(pair="ETH/USDT", state="done")._value.get() > before

    @pytest.mark.asyncio
    async def test_tick_stops_draining_when_breaker_trips(self):
        bot = _make_bot(self.OB)
        bot.executor.circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=1))
        bot.pairs = ["ETH/USDT", "ETH/USDT"]
        executions = []
        original = bot.executor.execute

        async def counting_execute(sig):
            ctx = await original(sig)
            bot.executor.circuit_breaker.trip()
            executions.append(sig.signal_id)
            return ctx

        bot.executor.execute = counting_execute
        await bot._tick()
        assert len(executions) == 1

    @pytest.mark.asyncio
    async def test_pnl_engine_bridge_produces_valid_record(self):
        from scripts.arb_bot import execution_to_arb_record

        sig = _generate_scored(self.OB)
        ctx = await _make_executor().execute(sig)
        assert ctx.state == ExecutorState.DONE
        record = execution_to_arb_record(ctx)
        assert record.id == sig.signal_id
        assert record.buy_leg.price > 0
        assert record.sell_leg.price > 0


# ---------------------------------------------------------------------------
# Real-data pipeline — requires internet, skipped in CI by default
# Run with: pytest -m network tests/test_integration_week4.py
# ---------------------------------------------------------------------------


@pytest.mark.network
class TestPipelineRealData:
    """
    Same pipeline tests but driven by live Binance public order book data.
    CEX prices are real market values. DEX prices use the stub (mid ± 0.8%).
    No API keys required — Binance public endpoint only.
    """

    @pytest.fixture(autouse=True)
    def real_ob(self):
        try:
            self._ob = _fetch_binance_orderbook("ETHUSDT")
            bid = float(self._ob["best_bid"][0])
            ask = float(self._ob["best_ask"][0])
            print(f"\n  Live ETH/USDT: bid={bid:.2f} ask={ask:.2f}")
        except (urllib.error.URLError, Exception) as exc:
            pytest.skip(f"Binance unreachable: {exc}")

    def test_real_prices_generate_signal(self):
        gen = _make_generator(self._ob)
        sig = gen.generate("ETH/USDT", 1.0)
        assert sig is not None, (
            "No signal generated from live prices — "
            "spread may be below min_spread_bps=50. "
            "This is expected if the market is efficient right now."
        )
        assert float(sig.cex_price) > 100  # sanity: ETH > $100
        assert float(sig.cex_price) < 1_000_000  # sanity: ETH < $1M
        assert sig.expected_net_pnl > 0
        assert sig.bid_ask_spread_bps >= 0

    def test_real_prices_score_in_range(self):
        gen = _make_generator(self._ob)
        scorer = SignalScorer()
        sig = gen.generate("ETH/USDT", 1.0)
        if sig is None:
            pytest.skip("No signal at current market spread")
        score = scorer.score(sig, [])
        assert 0.0 <= score <= 100.0

    @pytest.mark.asyncio
    async def test_real_prices_full_pipeline(self):
        gen = _make_generator(self._ob)
        scorer = SignalScorer()
        executor = _make_executor()
        q = SignalQueue()

        sig = gen.generate("ETH/USDT", 1.0)
        if sig is None:
            pytest.skip("No signal at current market spread")

        scorer.score(sig, [])
        q.put(sig)
        dequeued = q.get()
        assert dequeued is not None

        ctx = await executor.execute(dequeued)
        assert ctx.state == ExecutorState.DONE
        assert ctx.actual_net_pnl is not None
        print(
            f"\n  Score={sig.score:.1f}  "
            f"Expected PnL=${float(sig.expected_net_pnl):.2f}  "
            f"Actual PnL=${ctx.actual_net_pnl:.2f}"
        )

    @pytest.mark.asyncio
    async def test_real_prices_full_bot_tick(self):
        bot = _make_bot(self._ob)
        await bot._tick()

        if not bot.pnl_engine.trades:
            pytest.skip("No signal generated at current market spread")

        summary = bot.pnl_engine.summary()
        print(
            f"\n  Trades={summary['total_trades']}  "
            f"PnL=${float(summary['total_pnl_usd']):.2f}  "
            f"Win rate={summary['win_rate']:.0f}%"
        )
        assert summary["total_trades"] >= 1

    @pytest.mark.asyncio
    async def test_real_prices_btcusdt(self):
        """Same test for BTC/USDT to verify pair-agnostic behaviour."""
        try:
            btc_ob = _fetch_binance_orderbook("BTCUSDT")
        except urllib.error.URLError as exc:
            pytest.skip(f"Binance unreachable: {exc}")

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(
            Venue.BINANCE,
            {
                "BTC": {"free": "10", "locked": "0"},
                "USDT": {"free": "500000", "locked": "0"},
            },
        )
        tracker.update_from_wallet(Venue.WALLET, {"BTC": "10", "USDT": "500000"})

        exchange = MagicMock()
        exchange.fetch_order_book.return_value = btc_ob
        gen = SignalGenerator(
            exchange_client=exchange,
            pricing_module=None,
            inventory_tracker=tracker,
            fee_structure=FeeStructure(cex_taker_bps=10, dex_swap_bps=30, gas_cost_usd=2),
            config={"min_spread_bps": 50, "min_profit_usd": 1.0, "cooldown_seconds": 0},
        )
        sig = gen.generate("BTC/USDT", 0.01)
        if sig is None:
            pytest.skip("No BTC signal at current spread")
        SignalScorer().score(sig, [])

        executor = _make_executor()
        ctx = await executor.execute(sig)
        assert ctx.state == ExecutorState.DONE
        bid = float(btc_ob["best_bid"][0])
        print(f"\n  Live BTC/USDT: bid={bid:.2f}  " f"PnL=${ctx.actual_net_pnl:.2f}")
