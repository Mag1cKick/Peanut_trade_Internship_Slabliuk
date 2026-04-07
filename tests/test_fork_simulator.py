"""
tests/test_fork_simulator.py — Tests for pricing/fork_simulator.py.

No live Anvil node required — AnvilClient is injected and mocked throughout.

Test groups:
  1.  SimulationResult construction
  2.  AnvilClient — cheatcode methods (snapshot/revert/set_balance/impersonate/mine)
  3.  ForkSimulator.simulate_swap — read-only getAmountsOut path
  4.  ForkSimulator.simulate_route — live-reserve AMM math path
  5.  ForkSimulator.execute_swap — state-changing transaction path
  6.  ForkSimulator.compare_simulation_vs_calculation
  7.  ForkSimulator._get_reserves internal helper
"""

from __future__ import annotations

from unittest.mock import MagicMock, call

import pytest
from eth_abi import encode as abi_encode

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.fork_simulator import AnvilClient, ForkSimulator, SimulationResult
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


# ── Mock helpers ───────────────────────────────────────────────────────────────


def _mock_simulator() -> tuple[ForkSimulator, MagicMock]:
    """Return a ForkSimulator with a mocked AnvilClient.

    Use mock_client.call.return_value / side_effect to control eth_call results.
    """
    mock_client = MagicMock(spec=AnvilClient)
    sim = ForkSimulator(mock_client)
    return sim, mock_client


def _encode_amounts_out(amounts: list[int]) -> bytes:
    return abi_encode(["uint256[]"], [amounts])


def _encode_reserves(r0: int, r1: int, ts: int = 1_700_000_000) -> bytes:
    return abi_encode(["uint112", "uint112", "uint32"], [r0, r1, ts])


# ── 1. TestSimulationResult ────────────────────────────────────────────────────


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


# ── 2. TestAnvilClient ─────────────────────────────────────────────────────────


class TestAnvilClient:
    """Unit-test each Anvil cheatcode wrapper without a live node."""

    def _make_client(self) -> tuple[AnvilClient, MagicMock]:
        mock_w3 = MagicMock()
        return AnvilClient(mock_w3), mock_w3

    def test_snapshot_returns_int(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {"result": "0x2a"}
        snap = client.snapshot()
        assert snap == 42
        mock_w3.provider.make_request.assert_called_once_with("evm_snapshot", [])

    def test_revert_calls_evm_revert(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.revert(42)
        mock_w3.provider.make_request.assert_called_once_with("evm_revert", [hex(42)])

    def test_set_balance_calls_anvil_setBalance(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.set_balance(SENDER, 10**18)
        mock_w3.provider.make_request.assert_called_once_with(
            "anvil_setBalance", [SENDER.checksum, hex(10**18)]
        )

    def test_impersonate_calls_anvil_impersonateAccount(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.impersonate(SENDER)
        mock_w3.provider.make_request.assert_called_once_with(
            "anvil_impersonateAccount", [SENDER.checksum]
        )

    def test_stop_impersonating_calls_correct_method(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.stop_impersonating(SENDER)
        mock_w3.provider.make_request.assert_called_once_with(
            "anvil_stopImpersonatingAccount", [SENDER.checksum]
        )

    def test_mine_default_one_block(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.mine()
        mock_w3.provider.make_request.assert_called_once_with("evm_mine", [])

    def test_mine_multiple_blocks(self):
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.mine(3)
        assert mock_w3.provider.make_request.call_count == 3
        mock_w3.provider.make_request.assert_called_with("evm_mine", [])

    def test_call_delegates_to_eth_call(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.call.return_value = b"\x00" * 32
        tx = {"to": ROUTER.checksum, "data": "0xd06ca61f"}
        result = client.call(tx)
        mock_w3.eth.call.assert_called_once_with(tx)
        assert result == b"\x00" * 32

    def test_send_transaction_returns_hex_string(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.send_transaction.return_value = bytes.fromhex("ab" * 32)
        tx_hash = client.send_transaction({"to": ROUTER.checksum})
        assert tx_hash == "0x" + "ab" * 32

    def test_send_transaction_string_passthrough(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.send_transaction.return_value = "0xdeadbeef"
        tx_hash = client.send_transaction({"to": ROUTER.checksum})
        assert tx_hash == "0xdeadbeef"

    def test_get_transaction_receipt_returns_dict(self):
        client, mock_w3 = self._make_client()
        mock_receipt = MagicMock()
        mock_receipt.__iter__ = lambda s: iter({"gasUsed": 150_000}.items())
        mock_w3.eth.get_transaction_receipt.return_value = {"gasUsed": 150_000}
        receipt = client.get_transaction_receipt("0xabc")
        assert receipt is not None

    def test_get_transaction_receipt_none_returns_none(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.get_transaction_receipt.return_value = None
        assert client.get_transaction_receipt("0xabc") is None

    def test_warp_sets_timestamp_and_mines(self):
        """warp() sets timestamp then mines a block — equivalent to vm.warp()."""
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        client.warp(1_800_000_000)
        calls = mock_w3.provider.make_request.call_args_list
        assert calls[0] == call("evm_setNextBlockTimestamp", [1_800_000_000])
        assert calls[1] == call("evm_mine", [])

    def test_roll_mines_correct_number_of_blocks(self):
        """roll(n) mines (n - current) blocks — equivalent to vm.roll(n)."""
        client, mock_w3 = self._make_client()
        mock_w3.eth.block_number = 100
        mock_w3.provider.make_request.return_value = {}
        client.roll(105)
        # Should mine exactly 5 blocks
        assert mock_w3.provider.make_request.call_count == 5

    def test_roll_at_current_block_is_noop(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.block_number = 50
        mock_w3.provider.make_request.return_value = {}
        client.roll(50)  # already at target — no mining needed
        assert mock_w3.provider.make_request.call_count == 0

    def test_roll_backwards_raises_value_error(self):
        client, mock_w3 = self._make_client()
        mock_w3.eth.block_number = 200
        with pytest.raises(ValueError, match="backwards"):
            client.roll(199)

    def test_deal_erc20_calls_hardhat_setStorageAt(self):
        """deal_erc20() writes to the token's balance slot — like Forge's deal()."""
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        token = Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")  # USDC
        client.deal_erc20(token, SENDER, 1000 * 10**6, balance_slot=9)
        args = mock_w3.provider.make_request.call_args[0]
        assert args[0] == "hardhat_setStorageAt"
        assert args[1][0] == token.checksum
        # storage value must be the 32-byte big-endian encoding of 1000 USDC
        expected_value = "0x" + format(1000 * 10**6, "064x")
        assert args[1][2] == expected_value

    def test_deal_erc20_default_slot_zero(self):
        """Default balance_slot=0 is used when not specified."""
        client, mock_w3 = self._make_client()
        mock_w3.provider.make_request.return_value = {}
        token = Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
        client.deal_erc20(token, SENDER, 10**18)  # no slot → default 0
        assert mock_w3.provider.make_request.called

    def test_from_url_factory(self):
        """from_url should construct an AnvilClient wrapping a Web3 instance."""
        from unittest.mock import patch

        with patch("pricing.fork_simulator.Web3") as mock_web3_cls:
            mock_web3_cls.HTTPProvider.return_value = MagicMock()
            client = AnvilClient.from_url("http://127.0.0.1:8545")
        assert isinstance(client, AnvilClient)


# ── 3. TestSimulateSwap ────────────────────────────────────────────────────────


class TestSimulateSwap:
    def setup_method(self):
        self.sim, self.mock_client = _mock_simulator()

    def test_returns_simulation_result(self):
        amount_out = 493_579_017_198_530_649
        self.mock_client.call.return_value = _encode_amounts_out([1000 * 10**6, amount_out])
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.success is True
        assert result.amount_out == amount_out

    def test_gas_estimate_single_hop(self):
        self.mock_client.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.gas_used == 250_000  # 150k + 100k * 1 hop

    def test_gas_estimate_two_hop(self):
        self.mock_client.call.return_value = _encode_amounts_out(
            [1000 * 10**6, 999 * 10**18, 490 * 10**15]
        )
        path = [USDC.address.checksum, DAI.address.checksum, WETH.address.checksum]
        result = self.sim.simulate_swap(ROUTER, {"amount_in": 1000 * 10**6, "path": path}, SENDER)
        assert result.gas_used == 350_000  # 150k + 100k * 2 hops

    def test_uses_last_amount_as_output(self):
        self.mock_client.call.return_value = _encode_amounts_out(
            [500 * 10**6, 499 * 10**18, 248 * 10**15]
        )
        path = [USDC.address.checksum, DAI.address.checksum, WETH.address.checksum]
        result = self.sim.simulate_swap(ROUTER, {"amount_in": 500 * 10**6, "path": path}, SENDER)
        assert result.amount_out == 248 * 10**15

    def test_returns_failure_on_revert(self):
        self.mock_client.call.side_effect = Exception("execution reverted")
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
        self.mock_client.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["to"] == ROUTER.checksum

    def test_eth_call_receives_correct_from(self):
        self.mock_client.call.return_value = _encode_amounts_out([1000 * 10**6, 490 * 10**15])
        self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["from"] == SENDER.checksum

    def test_error_message_stored_in_result(self):
        self.mock_client.call.side_effect = RuntimeError("insufficient liquidity")
        result = self.sim.simulate_swap(
            ROUTER,
            {"amount_in": 1000 * 10**6, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert "insufficient liquidity" in result.error


# ── 4. TestSimulateRoute ───────────────────────────────────────────────────────


class TestSimulateRoute:
    def setup_method(self):
        self.sim, self.mock_client = _mock_simulator()

    def _make_single_hop_route(self):
        return Route(pools=[USDC_WETH_PAIR], path=[USDC, WETH])

    def _make_two_hop_route(self):
        usdc_dai = UniswapV2Pair(
            address=Address("0x0000000000000000000000000000000000000033"),
            token0=USDC,
            token1=DAI,
            reserve0=10_000_000 * 10**6,
            reserve1=10_000_000 * 10**18,
            fee_bps=5,
        )
        return Route(pools=[usdc_dai, DAI_WETH_PAIR], path=[USDC, DAI, WETH])

    def test_single_hop_returns_correct_output(self):
        live_r0, live_r1 = USDC_WETH_PAIR.reserve0, USDC_WETH_PAIR.reserve1
        self.mock_client.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        amount_in = 1000 * 10**6
        result = self.sim.simulate_route(route, amount_in, SENDER)

        expected = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        assert result.success is True
        assert result.amount_out == expected

    def test_two_hop_uses_both_pairs(self):
        route = self._make_two_hop_route()
        pools = route.pools
        self.mock_client.call.side_effect = [
            _encode_reserves(pools[0].reserve0, pools[0].reserve1),
            _encode_reserves(pools[1].reserve0, pools[1].reserve1),
        ]
        amount_in = 1000 * 10**6
        result = self.sim.simulate_route(route, amount_in, SENDER)

        mid = pools[0].get_amount_out(amount_in, USDC)
        expected = pools[1].get_amount_out(mid, DAI)
        assert result.success is True
        assert result.amount_out == expected

    def test_gas_used_matches_route_estimate(self):
        self.mock_client.call.return_value = _encode_reserves(
            USDC_WETH_PAIR.reserve0, USDC_WETH_PAIR.reserve1
        )
        route = self._make_single_hop_route()
        result = self.sim.simulate_route(route, 1000 * 10**6, SENDER)
        assert result.gas_used == route.estimate_gas()

    def test_stale_reserves_produce_different_output(self):
        live_r0 = USDC_WETH_PAIR.reserve0 * 2
        live_r1 = USDC_WETH_PAIR.reserve1
        self.mock_client.call.return_value = _encode_reserves(live_r0, live_r1)

        route = self._make_single_hop_route()
        amount_in = 1000 * 10**6
        result = self.sim.simulate_route(route, amount_in, SENDER)
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        assert result.success is True
        assert result.amount_out != calculated

    def test_returns_failure_on_rpc_error(self):
        self.mock_client.call.side_effect = Exception("connection refused")
        route = self._make_single_hop_route()
        result = self.sim.simulate_route(route, 1000 * 10**6, SENDER)
        assert result.success is False
        assert result.amount_out == 0

    def test_get_reserves_calls_correct_pair_address(self):
        self.mock_client.call.return_value = _encode_reserves(
            USDC_WETH_PAIR.reserve0, USDC_WETH_PAIR.reserve1
        )
        route = self._make_single_hop_route()
        self.sim.simulate_route(route, 1000 * 10**6, SENDER)
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["to"] == PAIR_ADDR.checksum


# ── 5. TestExecuteSwap ─────────────────────────────────────────────────────────


class TestExecuteSwap:
    """
    Tests for the state-changing execute_swap path.

    This mirrors Foundry fork tests: set up state via AnvilClient cheatcodes,
    call execute_swap, then verify the receipt/logs.
    """

    def setup_method(self):
        self.sim, self.mock_client = _mock_simulator()

    def test_execute_swap_success(self):
        self.mock_client.send_transaction.return_value = "0x" + "ab" * 32
        self.mock_client.get_transaction_receipt.return_value = {
            "gasUsed": 180_000,
            "logs": [],
        }
        result = self.sim.execute_swap(
            ROUTER,
            {
                "amount_in": 1000 * 10**6,
                "min_amount_out": 490 * 10**15,
                "path": [USDC.address.checksum, WETH.address.checksum],
                "deadline": 9_999_999_999,
            },
            SENDER,
        )
        assert result.success is True
        assert result.gas_used == 180_000

    def test_execute_swap_uses_sender_as_from(self):
        self.mock_client.send_transaction.return_value = "0xdeadbeef"
        self.mock_client.get_transaction_receipt.return_value = {"gasUsed": 150_000, "logs": []}
        self.sim.execute_swap(
            ROUTER,
            {"amount_in": 10**18, "path": [WETH.address.checksum, USDC.address.checksum]},
            SENDER,
        )
        call_args = self.mock_client.send_transaction.call_args[0][0]
        assert call_args["from"] == SENDER.checksum

    def test_execute_swap_failure_stored_in_error(self):
        self.mock_client.send_transaction.side_effect = RuntimeError("insufficient balance")
        result = self.sim.execute_swap(
            ROUTER,
            {"amount_in": 10**30, "path": [USDC.address.checksum, WETH.address.checksum]},
            SENDER,
        )
        assert result.success is False
        assert "insufficient balance" in result.error

    def test_execute_swap_logs_in_result(self):
        logs = [{"topics": ["0xTransfer"], "data": "0x"}]
        self.mock_client.send_transaction.return_value = "0xabc"
        self.mock_client.get_transaction_receipt.return_value = {"gasUsed": 200_000, "logs": logs}
        result = self.sim.execute_swap(
            ROUTER,
            {"amount_in": 10**18, "path": [WETH.address.checksum, USDC.address.checksum]},
            SENDER,
        )
        assert result.logs == logs

    def test_execute_swap_default_deadline_used_when_omitted(self):
        """If swap_params omits 'deadline', the default (2**32-1) is used."""
        self.mock_client.send_transaction.return_value = "0xabc"
        self.mock_client.get_transaction_receipt.return_value = {"gasUsed": 0, "logs": []}
        # Should not raise even without deadline
        result = self.sim.execute_swap(
            ROUTER,
            {"amount_in": 10**18, "path": [WETH.address.checksum, USDC.address.checksum]},
            SENDER,
        )
        assert result.success is True


# ── 6. TestCompareSimulationVsCalculation ─────────────────────────────────────


class TestCompareSimulationVsCalculation:
    def setup_method(self):
        self.sim, self.mock_client = _mock_simulator()

    def test_match_when_reserves_identical(self):
        amount_in = 1000 * 10**6
        expected_out = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        self.mock_client.call.return_value = _encode_amounts_out([amount_in, expected_out])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)
        assert result["calculated"] == expected_out
        assert result["simulated"] == expected_out
        assert result["difference"] == 0
        assert result["match"] is True

    def test_mismatch_when_fork_differs(self):
        amount_in = 1000 * 10**6
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        simulated = calculated - 100
        self.mock_client.call.return_value = _encode_amounts_out([amount_in, simulated])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)
        assert result["difference"] == 100
        assert result["match"] is False

    def test_result_keys_present(self):
        amount_in = 1000 * 10**6
        out = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)
        self.mock_client.call.return_value = _encode_amounts_out([amount_in, out])

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)
        assert set(result.keys()) == {"calculated", "simulated", "difference", "match"}

    def test_token_in_token1_uses_correct_path(self):
        amount_in = 1 * 10**18
        out = USDC_WETH_PAIR.get_amount_out(amount_in, WETH)
        self.mock_client.call.return_value = _encode_amounts_out([amount_in, out])

        self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, WETH)
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["data"].startswith("0xd06ca61f")

    def test_simulation_failure_treated_as_zero_out(self):
        self.mock_client.call.side_effect = Exception("revert")
        amount_in = 1000 * 10**6
        calculated = USDC_WETH_PAIR.get_amount_out(amount_in, USDC)

        result = self.sim.compare_simulation_vs_calculation(USDC_WETH_PAIR, amount_in, USDC)
        assert result["calculated"] == calculated
        assert result["simulated"] == 0
        assert result["difference"] == calculated
        assert result["match"] is False


# ── 7. TestGetReserves ─────────────────────────────────────────────────────────


class TestGetReserves:
    def setup_method(self):
        self.sim, self.mock_client = _mock_simulator()

    def test_decodes_correctly(self):
        r0, r1 = 100_000 * 10**6, 50 * 10**18
        self.mock_client.call.return_value = _encode_reserves(r0, r1)
        result_r0, result_r1 = self.sim._get_reserves(PAIR_ADDR)
        assert result_r0 == r0
        assert result_r1 == r1

    def test_calls_correct_address(self):
        self.mock_client.call.return_value = _encode_reserves(1, 1)
        self.sim._get_reserves(PAIR_ADDR)
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["to"] == PAIR_ADDR.checksum

    def test_uses_get_reserves_selector(self):
        self.mock_client.call.return_value = _encode_reserves(1, 1)
        self.sim._get_reserves(PAIR_ADDR)
        call_args = self.mock_client.call.call_args[0][0]
        assert call_args["data"] == "0x0902f1ac"

    def test_propagates_rpc_error(self):
        self.mock_client.call.side_effect = RuntimeError("timeout")
        with pytest.raises(RuntimeError, match="timeout"):
            self.sim._get_reserves(PAIR_ADDR)


# ── 8. TestForkSimulatorClient ─────────────────────────────────────────────────


class TestForkSimulatorClient:
    def test_client_property_returns_anvil_client(self):
        mock_client = MagicMock(spec=AnvilClient)
        sim = ForkSimulator(mock_client)
        assert sim.client is mock_client

    def test_from_url_factory(self):
        from unittest.mock import patch

        with patch("pricing.fork_simulator.Web3") as mock_web3_cls:
            mock_web3_cls.HTTPProvider.return_value = MagicMock()
            sim = ForkSimulator.from_url("http://127.0.0.1:8545")
        assert isinstance(sim, ForkSimulator)
        assert isinstance(sim.client, AnvilClient)
