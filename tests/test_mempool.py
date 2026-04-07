"""
tests/test_mempool.py — Tests for pricing/mempool.py (ParsedSwap and MempoolMonitor).
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode

from core.types import Address
from pricing.mempool import MempoolMonitor, ParsedSwap

# ── Shared addresses ───────────────────────────────────────────────────────────

USDC_ADDR = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
WETH_ADDR = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
DAI_ADDR = "0x6B175474E89094C44Da98b954EedeAC495271d0F"
TO_ADDR = "0x0000000000000000000000000000000000000099"
ROUTER_ADDR = "0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D"
SENDER_ADDR = "0x0000000000000000000000000000000000000001"
DEADLINE = 9_999_999_999


# ── Calldata builders ──────────────────────────────────────────────────────────


def _make_input_hex(selector_hex: str, types: list, values: list) -> str:
    encoded = abi_encode(types, values)
    return "0x" + selector_hex + encoded.hex()


def _make_input_bytes(selector_hex: str, types: list, values: list) -> bytes:
    return bytes.fromhex(selector_hex) + abi_encode(types, values)


def _tokens_for_tokens_hex(amount_in=1000 * 10**6, min_out=490 * 10**15) -> str:
    return _make_input_hex(
        "38ed1739",
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, min_out, [USDC_ADDR, WETH_ADDR], TO_ADDR, DEADLINE],
    )


def _eth_for_tokens_hex(min_out=480 * 10**15) -> str:
    return _make_input_hex(
        "7ff36ab5",
        ["uint256", "address[]", "address", "uint256"],
        [min_out, [WETH_ADDR, DAI_ADDR], TO_ADDR, DEADLINE],
    )


def _tokens_for_eth_hex(amount_in=2000 * 10**6, min_out=950 * 10**15) -> str:
    return _make_input_hex(
        "18cbafe5",
        ["uint256", "uint256", "address[]", "address", "uint256"],
        [amount_in, min_out, [USDC_ADDR, WETH_ADDR], TO_ADDR, DEADLINE],
    )


def _multicall_hex() -> str:
    return _make_input_hex(
        "5ae401dc",
        ["uint256", "bytes[]"],
        [DEADLINE, [b"\xde\xad\xbe\xef"]],
    )


def _make_tx(input_data, value=0, sender=SENDER_ADDR, router=ROUTER_ADDR, gas_price=20 * 10**9):
    return {
        "hash": "0xabcd",
        "from": sender,
        "to": router,
        "input": input_data,
        "value": value,
        "gasPrice": gas_price,
    }


# ── TestParsedSwap ─────────────────────────────────────────────────────────────


class TestParsedSwap:
    def _make_swap(self, **kwargs) -> ParsedSwap:
        defaults = dict(
            tx_hash="0xabc",
            router=ROUTER_ADDR,
            dex="UniswapV2",
            method="swapExactTokensForTokens",
            token_in=Address(USDC_ADDR),
            token_out=Address(WETH_ADDR),
            amount_in=1000 * 10**6,
            min_amount_out=490 * 10**15,
            deadline=DEADLINE,
            sender=Address(SENDER_ADDR),
            gas_price=20 * 10**9,
        )
        defaults.update(kwargs)
        return ParsedSwap(**defaults)

    def test_construction(self):
        swap = self._make_swap()
        assert swap.dex == "UniswapV2"
        assert swap.method == "swapExactTokensForTokens"
        assert swap.amount_in == 1000 * 10**6
        assert swap.min_amount_out == 490 * 10**15

    def test_token_in_is_address(self):
        swap = self._make_swap()
        assert isinstance(swap.token_in, Address)

    def test_token_in_none_for_eth_in(self):
        swap = self._make_swap(token_in=None)
        assert swap.token_in is None

    def test_token_out_none_for_eth_out(self):
        swap = self._make_swap(token_out=None)
        assert swap.token_out is None

    def test_slippage_tolerance_correct(self):
        swap = self._make_swap(
            amount_in=1000 * 10**6,
            min_amount_out=490 * 10**15,
            expected_amount_out=500 * 10**15,
        )
        # (500 - 490) / 500 = 2%
        assert swap.slippage_tolerance == Decimal("10") / Decimal("500")

    def test_slippage_tolerance_zero_when_min_equals_expected(self):
        swap = self._make_swap(
            min_amount_out=500 * 10**15,
            expected_amount_out=500 * 10**15,
        )
        assert swap.slippage_tolerance == Decimal(0)

    def test_slippage_tolerance_raises_without_expected(self):
        swap = self._make_swap()
        with pytest.raises(ValueError, match="expected_amount_out must be set"):
            _ = swap.slippage_tolerance

    def test_slippage_tolerance_raises_zero_expected(self):
        swap = self._make_swap(expected_amount_out=0)
        with pytest.raises(ValueError, match="non-zero"):
            _ = swap.slippage_tolerance

    def test_expected_amount_out_default_none(self):
        swap = self._make_swap()
        assert swap.expected_amount_out is None

    def test_expected_amount_out_settable(self):
        swap = self._make_swap()
        swap.expected_amount_out = 500 * 10**15
        assert swap.expected_amount_out == 500 * 10**15


# ── TestDecodeSwapParams ───────────────────────────────────────────────────────


class TestDecodeSwapParams:
    def setup_method(self):
        self.monitor = MempoolMonitor("wss://fake", callback=lambda s: None)

    def _calldata(self, selector_hex: str, types: list, values: list) -> bytes:
        return abi_encode(types, values)

    def test_swap_exact_tokens_for_tokens(self):
        data = self._calldata(
            "38ed1739",
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [1000 * 10**6, 490 * 10**15, [USDC_ADDR, WETH_ADDR], TO_ADDR, DEADLINE],
        )
        params = self.monitor.decode_swap_params("0x38ed1739", data)
        assert params["amount_in"] == 1000 * 10**6
        assert params["min_amount_out"] == 490 * 10**15
        assert params["token_in"].lower() == USDC_ADDR.lower()
        assert params["token_out"].lower() == WETH_ADDR.lower()
        assert params["deadline"] == DEADLINE

    def test_swap_exact_eth_for_tokens(self):
        data = self._calldata(
            "7ff36ab5",
            ["uint256", "address[]", "address", "uint256"],
            [480 * 10**15, [WETH_ADDR, DAI_ADDR], TO_ADDR, DEADLINE],
        )
        params = self.monitor.decode_swap_params("0x7ff36ab5", data)
        assert params["min_amount_out"] == 480 * 10**15
        assert params["token_in"] is None  # ETH in — no ERC-20 address
        assert params["token_out"].lower() == DAI_ADDR.lower()
        assert params["deadline"] == DEADLINE

    def test_swap_exact_tokens_for_eth(self):
        data = self._calldata(
            "18cbafe5",
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [2000 * 10**6, 950 * 10**15, [USDC_ADDR, WETH_ADDR], TO_ADDR, DEADLINE],
        )
        params = self.monitor.decode_swap_params("0x18cbafe5", data)
        assert params["amount_in"] == 2000 * 10**6
        assert params["min_amount_out"] == 950 * 10**15
        assert params["token_in"].lower() == USDC_ADDR.lower()
        assert params["token_out"] is None  # ETH out

    def test_multicall(self):
        data = self._calldata(
            "5ae401dc",
            ["uint256", "bytes[]"],
            [DEADLINE, [b"\xde\xad\xbe\xef"]],
        )
        params = self.monitor.decode_swap_params("0x5ae401dc", data)
        assert params["deadline"] == DEADLINE
        assert params["amount_in"] == 0
        assert params["min_amount_out"] == 0
        assert params["token_in"] is None
        assert params["token_out"] is None

    def test_unsupported_selector_raises(self):
        with pytest.raises(ValueError, match="Unsupported selector"):
            self.monitor.decode_swap_params("0xdeadbeef", b"\x00" * 32)

    def test_malformed_calldata_raises(self):
        with pytest.raises(Exception):
            self.monitor.decode_swap_params("0x38ed1739", b"\x00" * 10)


# ── TestParseTransaction ───────────────────────────────────────────────────────


class TestParseTransaction:
    def setup_method(self):
        self.monitor = MempoolMonitor("wss://fake", callback=lambda s: None)

    def test_returns_none_for_empty_input(self):
        tx = _make_tx(input_data="")
        assert self.monitor.parse_transaction(tx) is None

    def test_returns_none_for_short_input(self):
        tx = _make_tx(input_data="0x1234")
        assert self.monitor.parse_transaction(tx) is None

    def test_returns_none_for_unknown_selector(self):
        tx = _make_tx(input_data="0xdeadbeef" + "00" * 32)
        assert self.monitor.parse_transaction(tx) is None

    def test_parses_tokens_for_tokens_hex_input(self):
        tx = _make_tx(input_data=_tokens_for_tokens_hex())
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.dex == "UniswapV2"
        assert result.method == "swapExactTokensForTokens"
        assert result.amount_in == 1000 * 10**6
        assert result.min_amount_out == 490 * 10**15

    def test_parses_tokens_for_tokens_bytes_input(self):
        raw = _make_input_bytes(
            "38ed1739",
            ["uint256", "uint256", "address[]", "address", "uint256"],
            [1000 * 10**6, 490 * 10**15, [USDC_ADDR, WETH_ADDR], TO_ADDR, DEADLINE],
        )
        tx = _make_tx(input_data=raw)
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.amount_in == 1000 * 10**6

    def test_token_addresses_extracted_correctly(self):
        tx = _make_tx(input_data=_tokens_for_tokens_hex())
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.token_in == Address(USDC_ADDR)
        assert result.token_out == Address(WETH_ADDR)

    def test_eth_for_tokens_uses_tx_value(self):
        eth_value = 2 * 10**18
        tx = _make_tx(input_data=_eth_for_tokens_hex(), value=eth_value)
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.method == "swapExactETHForTokens"
        assert result.amount_in == eth_value
        assert result.token_in is None
        assert result.token_out == Address(DAI_ADDR)

    def test_tokens_for_eth_has_no_token_out(self):
        tx = _make_tx(input_data=_tokens_for_eth_hex())
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.method == "swapExactTokensForETH"
        assert result.token_out is None

    def test_multicall_parsed(self):
        tx = _make_tx(input_data=_multicall_hex())
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.dex == "UniswapV3"
        assert result.method == "multicall"
        assert result.deadline == DEADLINE

    def test_sender_set_correctly(self):
        tx = _make_tx(input_data=_tokens_for_tokens_hex())
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.sender == Address(SENDER_ADDR)

    def test_gas_price_set_from_gasPrice(self):
        gas = 50 * 10**9
        tx = _make_tx(input_data=_tokens_for_tokens_hex(), gas_price=gas)
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.gas_price == gas

    def test_gas_price_fallback_to_maxFeePerGas(self):
        tx = {
            "hash": "0xabc",
            "from": SENDER_ADDR,
            "to": ROUTER_ADDR,
            "input": _tokens_for_tokens_hex(),
            "value": 0,
            "maxFeePerGas": 30 * 10**9,
        }
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.gas_price == 30 * 10**9

    def test_malformed_calldata_returns_none(self):
        # Correct selector but garbage ABI data
        tx = _make_tx(input_data="0x38ed1739" + "de" * 10)
        assert self.monitor.parse_transaction(tx) is None

    def test_data_field_used_if_no_input(self):
        tx = {
            "hash": "0xabc",
            "from": SENDER_ADDR,
            "to": ROUTER_ADDR,
            "data": _tokens_for_tokens_hex(),
            "value": 0,
            "gasPrice": 20 * 10**9,
        }
        result = self.monitor.parse_transaction(tx)
        assert result is not None
        assert result.amount_in == 1000 * 10**6


# ── TestMempoolMonitorStart ────────────────────────────────────────────────────


class TestMempoolMonitorStart:
    def _build_message(self, tx_hash: str) -> dict:
        return {"params": {"result": tx_hash}}

    async def _run_start_with_messages(self, monitor, messages, tx_by_hash):
        """
        Drive monitor.start() with a mocked AsyncWeb3 that yields the given
        messages and resolves get_transaction via tx_by_hash dict.
        """

        async def fake_subscriptions():
            for msg in messages:
                yield msg

        mock_w3 = MagicMock()
        mock_w3.eth.subscribe = AsyncMock()
        mock_w3.eth.get_transaction = AsyncMock(side_effect=lambda h: tx_by_hash.get(h))
        mock_w3.socket.process_subscriptions = fake_subscriptions

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_w3)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("pricing.mempool.AsyncWeb3", return_value=mock_ctx):
            await monitor.start()
            # Let spawned tasks complete
            await asyncio.gather(
                *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            )

    @pytest.mark.asyncio
    async def test_callback_called_on_swap(self):
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        tx_hash = "0xdeadbeef"
        tx = _make_tx(input_data=_tokens_for_tokens_hex())
        tx["hash"] = tx_hash

        await self._run_start_with_messages(
            monitor,
            messages=[self._build_message(tx_hash)],
            tx_by_hash={tx_hash: tx},
        )

        assert len(received) == 1
        assert received[0].method == "swapExactTokensForTokens"

    @pytest.mark.asyncio
    async def test_no_callback_on_non_swap(self):
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        tx_hash = "0xdeadbeef"
        tx = _make_tx(input_data="0xabcdef12" + "00" * 32)  # unknown selector
        tx["hash"] = tx_hash

        await self._run_start_with_messages(
            monitor,
            messages=[self._build_message(tx_hash)],
            tx_by_hash={tx_hash: tx},
        )

        assert received == []

    @pytest.mark.asyncio
    async def test_fetch_error_does_not_stop_monitor(self):
        """A failed get_transaction should be silently swallowed."""
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        async def fake_subscriptions():
            yield self._build_message("0xbad")
            yield self._build_message("0xgood")

        good_tx = _make_tx(input_data=_tokens_for_tokens_hex())
        good_tx["hash"] = "0xgood"

        mock_w3 = MagicMock()
        mock_w3.eth.subscribe = AsyncMock()
        mock_w3.eth.get_transaction = AsyncMock(
            side_effect=lambda h: (_ for _ in ()).throw(RuntimeError("rpc error"))
            if h == "0xbad"
            else good_tx
        )
        mock_w3.socket.process_subscriptions = fake_subscriptions

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_w3)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)

        with patch("pricing.mempool.AsyncWeb3", return_value=mock_ctx):
            await monitor.start()
            await asyncio.gather(
                *[t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
            )

        assert len(received) == 1
        assert received[0].tx_hash == "0xgood"

    @pytest.mark.asyncio
    async def test_fetch_and_process_calls_callback(self):
        """Unit-test _fetch_and_process directly, bypassing start()."""
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        tx = _make_tx(input_data=_tokens_for_tokens_hex())
        tx["hash"] = "0xtest"

        mock_w3 = AsyncMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=tx)

        await monitor._fetch_and_process(mock_w3, "0xtest")

        assert len(received) == 1
        assert received[0].amount_in == 1000 * 10**6

    @pytest.mark.asyncio
    async def test_fetch_and_process_none_tx_skipped(self):
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        mock_w3 = AsyncMock()
        mock_w3.eth.get_transaction = AsyncMock(return_value=None)

        await monitor._fetch_and_process(mock_w3, "0xunknown")
        assert received == []

    @pytest.mark.asyncio
    async def test_multiple_swaps_all_received(self):
        received = []
        monitor = MempoolMonitor("wss://fake", callback=received.append)

        hashes = ["0x0001", "0x0002", "0x0003"]
        txs = {h: {**_make_tx(input_data=_tokens_for_tokens_hex()), "hash": h} for h in hashes}
        messages = [self._build_message(h) for h in hashes]

        await self._run_start_with_messages(monitor, messages, txs)

        assert len(received) == 3
