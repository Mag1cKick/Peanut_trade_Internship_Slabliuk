"""
tests/test_pricing_engine.py — Tests for pricing/engine.py.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.engine import PricingEngine, Quote, QuoteError
from pricing.fork_simulator import SimulationResult
from pricing.mempool import ParsedSwap
from pricing.router import Route

# ── Shared fixtures ────────────────────────────────────────────────────────────

USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6
)
WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)

PAIR_ADDR = Address("0x0000000000000000000000000000000000000011")
PAIR_ADDR2 = Address("0x0000000000000000000000000000000000000022")
SENDER = Address("0x0000000000000000000000000000000000000001")

USDC_WETH_PAIR = UniswapV2Pair(
    address=PAIR_ADDR,
    token0=USDC,
    token1=WETH,
    reserve0=100_000 * 10**6,
    reserve1=50 * 10**18,
    fee_bps=30,
)

DAI_WETH_PAIR = UniswapV2Pair(
    address=PAIR_ADDR2,
    token0=DAI,
    token1=WETH,
    reserve0=2_000_000 * 10**18,
    reserve1=1_000 * 10**18,
    fee_bps=30,
)


def _make_engine() -> PricingEngine:
    """Return a PricingEngine with mocked sub-components."""
    mock_client = MagicMock()
    engine = PricingEngine.__new__(PricingEngine)
    engine.client = mock_client
    engine.simulator = MagicMock()
    engine.monitor = MagicMock()
    engine.pools = {}
    engine.router = None
    engine.pending_swaps = []
    return engine


def _make_route(pool=USDC_WETH_PAIR) -> Route:
    return Route(pools=[pool], path=[USDC, WETH])


def _make_parsed_swap(token_in=USDC.address, token_out=WETH.address) -> ParsedSwap:
    return ParsedSwap(
        tx_hash="0xabc",
        router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
        dex="UniswapV2",
        method="swapExactTokensForTokens",
        token_in=token_in,
        token_out=token_out,
        amount_in=1000 * 10**6,
        min_amount_out=490 * 10**15,
        deadline=9_999_999_999,
        sender=SENDER,
        gas_price=20 * 10**9,
    )


# ── TestQuote ──────────────────────────────────────────────────────────────────


class TestQuote:
    def _make_quote(self, expected=500, simulated=500) -> Quote:
        return Quote(
            route=_make_route(),
            amount_in=1000 * 10**6,
            expected_output=expected,
            simulated_output=simulated,
            gas_estimate=250_000,
            timestamp=1_700_000_000.0,
        )

    def test_is_valid_exact_match(self):
        assert self._make_quote(500, 500).is_valid is True

    def test_is_valid_within_tolerance(self):
        # 0.05% difference — under 0.1% threshold
        assert self._make_quote(1_000_000, 999_500).is_valid is True

    def test_is_invalid_over_tolerance(self):
        # 0.2% difference — over threshold
        assert self._make_quote(1_000_000, 998_000).is_valid is False

    def test_is_valid_exactly_at_boundary(self):
        # exactly 0.1% — NOT strictly less than, so invalid
        expected = 1_000_000
        simulated = expected - 1_000  # exactly 0.1%
        assert self._make_quote(expected, simulated).is_valid is False

    def test_is_valid_zero_expected_zero_simulated(self):
        assert self._make_quote(0, 0).is_valid is True

    def test_is_invalid_zero_expected_nonzero_simulated(self):
        assert self._make_quote(0, 1).is_valid is False

    def test_fields_accessible(self):
        q = self._make_quote(500, 499)
        assert q.amount_in == 1000 * 10**6
        assert q.gas_estimate == 250_000
        assert isinstance(q.timestamp, float)
        assert isinstance(q.route, Route)


# ── TestQuoteError ─────────────────────────────────────────────────────────────


class TestQuoteError:
    def test_is_exception(self):
        err = QuoteError("no route")
        assert isinstance(err, Exception)

    def test_message_preserved(self):
        with pytest.raises(QuoteError, match="no route"):
            raise QuoteError("no route")


# ── TestPricingEngineInit ──────────────────────────────────────────────────────


class TestPricingEngineInit:
    def test_init_stores_injected_simulator(self):
        """ForkSimulator is now injected (DIP) — PricingEngine stores it as-is."""
        mock_client = MagicMock()
        mock_sim = MagicMock()
        with patch("pricing.engine.MempoolMonitor"):
            engine = PricingEngine(mock_client, mock_sim, "wss://fake")
        assert engine.simulator is mock_sim

    def test_monitor_callback_is_on_mempool_swap(self):
        mock_client = MagicMock()
        with patch("pricing.engine.MempoolMonitor") as MockMonitor:
            PricingEngine(mock_client, MagicMock(), "wss://fake")
        callback = MockMonitor.call_args[0][1]
        assert callable(callback)

    def test_pools_initially_empty(self):
        mock_client = MagicMock()
        with patch("pricing.engine.MempoolMonitor"):
            engine = PricingEngine(mock_client, MagicMock(), "wss://fake")
        assert engine.pools == {}
        assert engine.router is None

    def test_pending_swaps_initially_empty(self):
        mock_client = MagicMock()
        with patch("pricing.engine.MempoolMonitor"):
            engine = PricingEngine(mock_client, MagicMock(), "wss://fake")
        assert engine.pending_swaps == []


# ── TestLoadPools ──────────────────────────────────────────────────────────────


class TestLoadPools:
    def test_pools_populated_after_load(self):
        engine = _make_engine()
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=USDC_WETH_PAIR):
            engine.load_pools([PAIR_ADDR])
        assert PAIR_ADDR in engine.pools

    def test_router_built_after_load(self):
        engine = _make_engine()
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=USDC_WETH_PAIR):
            engine.load_pools([PAIR_ADDR])
        assert engine.router is not None

    def test_multiple_pools_loaded(self):
        engine = _make_engine()
        side_effects = [USDC_WETH_PAIR, DAI_WETH_PAIR]
        with patch("pricing.engine.UniswapV2Pair.from_chain", side_effect=side_effects):
            engine.load_pools([PAIR_ADDR, PAIR_ADDR2])
        assert len(engine.pools) == 2

    def test_from_chain_called_per_address(self):
        engine = _make_engine()
        with patch(
            "pricing.engine.UniswapV2Pair.from_chain", return_value=USDC_WETH_PAIR
        ) as mock_fc:
            engine.load_pools([PAIR_ADDR, PAIR_ADDR2])
        assert mock_fc.call_count == 2

    def test_router_receives_all_pools(self):
        engine = _make_engine()
        side_effects = [USDC_WETH_PAIR, DAI_WETH_PAIR]
        with patch("pricing.engine.UniswapV2Pair.from_chain", side_effect=side_effects):
            engine.load_pools([PAIR_ADDR, PAIR_ADDR2])
        assert len(engine.router.pools) == 2


# ── TestRefreshPool ────────────────────────────────────────────────────────────


def _fresh_usdc_weth() -> UniswapV2Pair:
    """Create a new USDC/WETH pair instance to avoid shared-state mutations."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=USDC,
        token1=WETH,
        reserve0=100_000 * 10**6,
        reserve1=50 * 10**18,
        fee_bps=30,
    )


class TestRefreshPool:
    def test_raises_if_pool_not_loaded(self):
        engine = _make_engine()
        with pytest.raises(KeyError):
            engine.refresh_pool(PAIR_ADDR)

    def test_reserves_updated_in_place(self):
        engine = _make_engine()
        initial = _fresh_usdc_weth()
        original_r0 = initial.reserve0
        original_r1 = initial.reserve1

        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=initial):
            engine.load_pools([PAIR_ADDR])

        fresh = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=USDC,
            token1=WETH,
            reserve0=original_r0 * 2,
            reserve1=original_r1 * 2,
            fee_bps=30,
        )
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=fresh):
            engine.refresh_pool(PAIR_ADDR)

        assert engine.pools[PAIR_ADDR].reserve0 == original_r0 * 2
        assert engine.pools[PAIR_ADDR].reserve1 == original_r1 * 2

    def test_same_object_identity_preserved(self):
        """Refresh updates in place — same object referenced by router."""
        engine = _make_engine()
        initial = _fresh_usdc_weth()
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=initial):
            engine.load_pools([PAIR_ADDR])

        pool_before = engine.pools[PAIR_ADDR]

        fresh = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=USDC,
            token1=WETH,
            reserve0=200,
            reserve1=200,
            fee_bps=30,
        )
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=fresh):
            engine.refresh_pool(PAIR_ADDR)

        # The SAME object in pools dict (identity preserved)
        assert engine.pools[PAIR_ADDR] is pool_before

    def test_fee_bps_updated(self):
        engine = _make_engine()
        initial = _fresh_usdc_weth()
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=initial):
            engine.load_pools([PAIR_ADDR])

        fresh = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=USDC,
            token1=WETH,
            reserve0=100,
            reserve1=100,
            fee_bps=5,
        )
        with patch("pricing.engine.UniswapV2Pair.from_chain", return_value=fresh):
            engine.refresh_pool(PAIR_ADDR)

        assert engine.pools[PAIR_ADDR].fee_bps == 5


# ── TestGetQuote ───────────────────────────────────────────────────────────────


class TestGetQuote:
    def _engine_with_pool(self) -> PricingEngine:
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR
        engine.router = MagicMock()
        return engine

    def test_returns_quote_on_success(self):
        engine = self._engine_with_pool()
        amount_in = 1000 * 10**6
        gross_out = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        net_out = gross_out - 250_000 * 10**9  # after gas

        engine.router.find_best_route.return_value = (_make_route(), net_out)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=True, amount_out=gross_out, gas_used=250_000, error=None
        )

        quote = engine.get_quote(USDC, WETH, amount_in, gas_price_gwei=1)

        assert isinstance(quote, Quote)
        assert quote.amount_in == amount_in
        assert quote.expected_output == net_out
        assert quote.simulated_output == gross_out
        assert quote.gas_estimate == 250_000

    def test_raises_quote_error_when_no_pools_loaded(self):
        engine = _make_engine()
        with pytest.raises(QuoteError, match="No pools loaded"):
            engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)

    def test_raises_quote_error_when_no_route(self):
        engine = self._engine_with_pool()
        engine.router.find_best_route.side_effect = ValueError("No route found")
        with pytest.raises(QuoteError, match="No route found"):
            engine.get_quote(USDC, DAI, 1000 * 10**6, gas_price_gwei=1)

    def test_raises_quote_error_on_simulation_failure(self):
        engine = self._engine_with_pool()
        engine.router.find_best_route.return_value = (_make_route(), 490 * 10**15)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=False, amount_out=0, gas_used=0, error="execution reverted"
        )
        with pytest.raises(QuoteError, match="Simulation failed"):
            engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)

    def test_simulation_called_with_correct_route(self):
        engine = self._engine_with_pool()
        route = _make_route()
        engine.router.find_best_route.return_value = (route, 490 * 10**15)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=True, amount_out=493 * 10**15, gas_used=250_000, error=None
        )
        engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)
        engine.simulator.simulate_route.assert_called_once()
        call_route = engine.simulator.simulate_route.call_args[0][0]
        assert call_route is route

    def test_quote_timestamp_recent(self):
        import time as _time

        engine = self._engine_with_pool()
        engine.router.find_best_route.return_value = (_make_route(), 490 * 10**15)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=True, amount_out=493 * 10**15, gas_used=250_000, error=None
        )
        before = _time.time()
        quote = engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)
        after = _time.time()
        assert before <= quote.timestamp <= after

    def test_quote_is_valid_when_outputs_close(self):
        engine = self._engine_with_pool()
        expected = 490_000_000_000_000_000
        simulated = 490_100_000_000_000_000  # ~0.02% diff
        engine.router.find_best_route.return_value = (_make_route(), expected)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=True, amount_out=simulated, gas_used=250_000, error=None
        )
        quote = engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)
        assert quote.is_valid is True

    def test_quote_invalid_when_outputs_diverge(self):
        engine = self._engine_with_pool()
        expected = 490_000_000_000_000_000
        simulated = 480_000_000_000_000_000  # ~2% diff
        engine.router.find_best_route.return_value = (_make_route(), expected)
        engine.simulator.simulate_route.return_value = SimulationResult(
            success=True, amount_out=simulated, gas_used=250_000, error=None
        )
        quote = engine.get_quote(USDC, WETH, 1000 * 10**6, gas_price_gwei=1)
        assert quote.is_valid is False


# ── TestOnMempoolSwap ──────────────────────────────────────────────────────────


class TestOnMempoolSwap:
    def test_relevant_swap_added_to_pending(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR

        swap = _make_parsed_swap(token_in=USDC.address, token_out=WETH.address)
        engine._on_mempool_swap(swap)

        assert len(engine.pending_swaps) == 1
        assert engine.pending_swaps[0] is swap

    def test_irrelevant_swap_not_added(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR  # only USDC/WETH pool

        # Swap between two tokens we don't track
        dai_addr = Address("0x6B175474E89094C44Da98b954EedeAC495271d0F")
        pepe_addr = Address("0x6982508145454Ce325dDbE47a25d4ec3d2311933")
        swap = _make_parsed_swap(token_in=dai_addr, token_out=pepe_addr)
        engine._on_mempool_swap(swap)

        assert engine.pending_swaps == []

    def test_swap_with_no_tokens_ignored(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR

        swap = _make_parsed_swap(token_in=None, token_out=None)
        engine._on_mempool_swap(swap)

        assert engine.pending_swaps == []

    def test_multiple_swaps_accumulated(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR

        for _ in range(3):
            engine._on_mempool_swap(
                _make_parsed_swap(token_in=USDC.address, token_out=WETH.address)
            )

        assert len(engine.pending_swaps) == 3

    def test_swap_affecting_token_in_only(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR

        # token_in is USDC (in our pool), token_out is something else
        pepe_addr = Address("0x6982508145454Ce325dDbE47a25d4ec3d2311933")
        swap = _make_parsed_swap(token_in=USDC.address, token_out=pepe_addr)
        engine._on_mempool_swap(swap)

        assert len(engine.pending_swaps) == 1

    def test_pools_affected_by_returns_matching_pairs(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR
        engine.pools[PAIR_ADDR2] = DAI_WETH_PAIR

        # Swap involving WETH — affects BOTH pools
        swap = _make_parsed_swap(token_in=USDC.address, token_out=WETH.address)
        affected = engine._pools_affected_by(swap)
        addresses = {p.address for p in affected}
        assert PAIR_ADDR in addresses  # USDC/WETH
        assert PAIR_ADDR2 in addresses  # DAI/WETH (WETH appears here too)

    def test_pools_affected_by_empty_when_no_match(self):
        engine = _make_engine()
        engine.pools[PAIR_ADDR] = USDC_WETH_PAIR

        pepe = Address("0x6982508145454Ce325dDbE47a25d4ec3d2311933")
        link = Address("0x514910771AF9Ca656af840dff83E8264EcF986CA")
        swap = _make_parsed_swap(token_in=pepe, token_out=link)
        assert engine._pools_affected_by(swap) == []
