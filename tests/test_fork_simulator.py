"""
tests/test_fork_simulator.py — Tests for pricing/fork_simulator.py.

All tests mock out web3 calls so no live node is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from eth_abi import encode as abi_encode

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.fork_simulator import ForkSimulator, SimulationResult
from pricing.router import Route

# ── Shared tokens and pools ────────────────────────────────────────────────────

USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6
)
WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)

ROUTER = Address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")
SENDER = Address("0x0000000000000000000000000000000000000001")

PAIR_ADDR = Address("0x0000000000000000000000000000000000000011")
PAIR_ADDR2 = Address("0x0000000000000000000000000000000000000022")

# Pool with 100k USDC / 50 WETH
USDC_WETH_PAIR = UniswapV2Pair(
    address=PAIR_ADDR,
    token0=USDC,
    token1=WETH,
    reserve0=100_000 * 10**6,
    reserve1=50 * 10**18,
    fee_bps=30,
)

# Pool with 2M DAI / 1000 WETH
DAI_WETH_PAIR = UniswapV2Pair(
    address=PAIR_ADDR2,
    token0=DAI,
    token1=WETH,
    reserve0=2_000_000 * 10**18,
    reserve1=1_000 * 10**18,
    fee_bps=30,
)


# ── Mock helpers ───────────────────────────────────────────────────────────────


def _mock_simulator() -> tuple[ForkSimulator, MagicMock]:
    """Return a ForkSimulator whose w3.eth.call is a MagicMock."""
    sim = ForkSimulator.__new__(ForkSimulator)
    mock_w3 = MagicMock()
    sim.w3 = mock_w3
    return sim, mock_w3


def _encode_amounts_out(amounts: list[int]) -> bytes:
    return abi_encode(["uint256[]"], [amounts])


def _encode_reserves(r0: int, r1: int, ts: int = 1_700_000_000) -> bytes:
    return abi_encode(["uint112", "uint112", "uint32"], [r0, r1, ts])


# ── TestSimulationResult ───────────────────────────────────────────────────────


class TestSimulationResult:
    def test_success_construction(self):
        r = SimulationResult(success=True, amount_out=1000, gas_used=250_000, error=None)
        assert r.success is True
        assert r.amount_out == 1000
        assert r.gas_used == 250_000
        assert r.error is None

    def test_failure_construction(self):
        r = SimulationResult(success=False, amount_out=0, gas_used=0, error="revert")
        assert r.success is False
        assert r.error == "revert"

    def test_logs_default_empty(self):
        r = SimulationResult(success=True, amount_out=0, gas_used=0, error=None)
        assert r.logs == []

    def test_logs_populated(self):
        logs = [{"event": "Swap", "data": {}}]
        r = SimulationResult(success=True, amount_out=0, gas_used=0, error=None, logs=logs)
        assert r.logs == logs


# ── TestSimulateSwap ───────────────────────────────────────────────────────────


class TestSimulateSwap:
    def setup_method(self):
        self.sim, self.mock_w3 = _mock_simulator()

    def test_returns_simulation_result(self):
        amount_out = 493_579_017_198_530_649
        self.mock_w3.eth.call.return_value = _encode_amounts_out([1000 * 10**6, amount_out])
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.success is True
        assert result.amount_out == amount_out

    def test_gas_estimate_single_hop(self):
        self.mock_w3.eth.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.gas_used == 250_000  # 150k + 100k * 1 hop

    def test_gas_estimate_two_hop(self):
        self.mock_w3.eth.call.return_value = _encode_amounts_out(
            [1000 * 10**6, 999 * 10**18, 490 * 10**15]
        )
        path = [USDC.address.checksum, DAI.address.checksum, WETH.address.checksum]
        result = self.sim.simulate_swap(ROUTER, {"amount_in": 1000 * 10**6, "path": path}, SENDER)
        assert result.gas_used == 350_000  # 150k + 100k * 2 hops

    def test_uses_last_amount_as_output(self):
        # For a 3-token path, the last element is the final output
        self.mock_w3.eth.call.return_value = _encode_amounts_out(
            [500 * 10**6, 499 * 10**18, 248 * 10**15]
        )
        path = [USDC.address.checksum, DAI.address.checksum, WETH.address.checksum]
        result = self.sim.simulate_swap(ROUTER, {"amount_in": 500 * 10**6, "path": path}, SENDER)
        assert result.amount_out == 248 * 10**15

    def test_returns_failure_on_revert(self):
        self.mock_w3.eth.call.side_effect = Exception("execution reverted")
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 10**30, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.success is False
        assert result.amount_out == 0
        assert result.gas_used == 0
        assert "revert" in result.error.lower()

    def test_eth_call_receives_correct_to(self):
        self.mock_w3.eth.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["to"] == ROUTER.checksum

    def test_eth_call_receives_correct_from(self):
        self.mock_w3.eth.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["from"] == SENDER.checksum

    def test_error_message_stored_in_result(self):
        self.mock_w3.eth.call.side_effect = RuntimeError("insufficient liquidity")
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert "insufficient liquidity" in result.error


# ── TestSimulateRoute ──────────────────────────────────────────────────────────


class TestSimulateRoute:
    def setup_method(self):
        self.sim, self.mock_w3 = _mock_simulator()

    def _make_single_hop_route(self):
        return Route(pools=[USDC_WETH_PAIR], path=[USDC, WETH])

    def _make_two_hop_route(self):
        dai_weth = DAI_WETH_PAIR
        usdc_dai = UniswapV2Pair(
            address=Address("0x0000000000000000000000000000000000000033"),
            token0=USDC,
            token1=DAI,
            reserve0=10_000_000 * 10**6,
            reserve1=10_000_000 * 10**18,
            fee_bps=5,
        )
        return Route(pools=[usdc_dai, dai_weth], path=[USDC, DAI, WETH])

    def test_single_hop_returns_correct_output(self):
        # Fork has same reserves as our stored pair -> outputs should match
        live_r0 = USDC_WETH_PAIR.reserve0
        live_r1 = USDC_WETH_PAIR.reserve1
        self.mock_w3.eth.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        amount_in = 1000 * 10**6
        result = self.sim.simulate_route(route, amount_in, SENDER)

        expected = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        assert result.success is True
        assert result.amount_out == expected

    def test_two_hop_uses_both_pairs(self):
        route = self._make_two_hop_route()
        pools = route.pools

        # Mock returns the reserves of each pair in call order
        call_responses = [
            _encode_reserves(pools[0].reserve0, pools[0].reserve1),
            _encode_reserves(pools[1].reserve0, pools[1].reserve1),
        ]
        self.mock_w3.eth.call.side_effect = call_responses

        amount_in = 1000 * 10**6
        result = self.sim.simulate_route(route, amount_in, SENDER)

        mid = pools[0].get_amount_out(amount_in, USDC)
        expected = pools[1].get_amount_out(mid, DAI)

        assert result.success is True
        assert result.amount_out == expected

    def test_gas_used_matches_route_estimate(self):
        live_r0, live_r1 = USDC_WETH_PAIR.reserve0, USDC_WETH_PAIR.reserve1
        self.mock_w3.eth.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        result = self.sim.simulate_route(route, 1000 * 10**6, SENDER)
        assert result.gas_used == route.estimate_gas()

    def test_stale_reserves_produce_different_output(self):
        """If fork has different reserves than our pair object, results diverge."""
        # Fork has double the USDC reserves (different from stored state)
        live_r0 = USDC_WETH_PAIR.reserve0 * 2
        live_r1 = USDC_WETH_PAIR.reserve1
        self.mock_w3.eth.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        amount_in = 1000 * 10**6

        result = self.sim.simulate_route(route, amount_in, SENDER)
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)

        assert result.success is True
        assert result.amount_out != calculated

    def test_returns_failure_on_rpc_error(self):
        self.mock_w3.eth.call.side_effect = Exception("connection refused")
        route = self._make_single_hop_route()
        result = self.sim.simulate_route(route, 1000 * 10**6, SENDER)
        assert result.success is False
        assert result.amount_out == 0

    def test_get_reserves_calls_correct_pair_address(self):
        live_r0, live_r1 = USDC_WETH_PAIR.reserve0, USDC_WETH_PAIR.reserve1
        self.mock_w3.eth.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        self.sim.simulate_route(route, 1000 * 10**6, SENDER)

        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["to"] == PAIR_ADDR.checksum


# ── TestCompareSimulationVsCalculation ────────────────────────────────────────


class TestCompareSimulationVsCalculation:
    def setup_method(self):
        self.sim, self.mock_w3 = _mock_simulator()

    def test_match_when_reserves_identical(self):
        """Stored reserves == fork reserves → calculated == simulated → match=True."""
        amount_in = 1000 * 10**6
        expected_out = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)

        # Mock getAmountsOut on the router to return the same calculated output
        self.mock_w3.eth.call.return_value = _encode_amounts_out([amount_in, expected_out])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)

        assert result["calculated"] == expected_out
        assert result["simulated"] == expected_out
        assert result["difference"] == 0
        assert result["match"] is True

    def test_mismatch_when_fork_differs(self):
        """Fork state differs → match=False, difference > 0."""
        amount_in = 1000 * 10**6
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        simulated = calculated - 100  # fork gives slightly less

        self.mock_w3.eth.call.return_value = _encode_amounts_out([amount_in, simulated])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)

        assert result["calculated"] == calculated
        assert result["simulated"] == simulated
        assert result["difference"] == 100
        assert result["match"] is False

    def test_result_keys_present(self):
        amount_in = 1000 * 10**6
        out = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        self.mock_w3.eth.call.return_value = _encode_amounts_out([amount_in, out])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)
        assert set(result.keys()) == {"calculated", "simulated", "difference", "match"}

    def test_token_in_token1_uses_correct_path(self):
        """When token_in is token1 (WETH), path should be [WETH, USDC]."""
        amount_in = 1 * 10**18  # 1 WETH
        out = USDC_WETH_PAIR.get_amount_out(amount_in, WETH)
        self.mock_w3.eth.call.return_value = _encode_amounts_out([amount_in, out])

        self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, WETH)

        # Check the call's data starts with the getAmountsOut selector
        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["data"].startswith("0xd06ca61f")

    def test_simulation_failure_treated_as_zero_out(self):
        """If simulate_swap fails, simulated=0 → large difference."""
        self.mock_w3.eth.call.side_effect = Exception("revert")
        amount_in = 1000 * 10**6
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)

        assert result["calculated"] == calculated
        assert result["simulated"] == 0
        assert result["difference"] == calculated
        assert result["match"] is False


# ── TestGetReserves (internal helper) ─────────────────────────────────────────


class TestGetReserves:
    def setup_method(self):
        self.sim, self.mock_w3 = _mock_simulator()

    def test_decodes_correctly(self):
        r0, r1 = 100_000 * 10**6, 50 * 10**18
        self.mock_w3.eth.call.return_value = _encode_reserves(r0, r1)
        result_r0, result_r1 = self.sim._get_reserves(PAIR_ADDR)
        assert result_r0 == r0
        assert result_r1 == r1

    def test_calls_correct_address(self):
        self.mock_w3.eth.call.return_value = _encode_reserves(1, 1)
        self.sim._get_reserves(PAIR_ADDR)
        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["to"] == PAIR_ADDR.checksum

    def test_uses_get_reserves_selector(self):
        self.mock_w3.eth.call.return_value = _encode_reserves(1, 1)
        self.sim._get_reserves(PAIR_ADDR)
        call_args = self.mock_w3.eth.call.call_args[0][0]
        assert call_args["data"] == "0x0902f1ac"

    def test_propagates_rpc_error(self):
        self.mock_w3.eth.call.side_effect = RuntimeError("timeout")
        with pytest.raises(RuntimeError, match="timeout"):
            self.sim._get_reserves(PAIR_ADDR)
