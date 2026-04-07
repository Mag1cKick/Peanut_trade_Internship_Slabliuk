"""
tests/test_analyzer.py — Unit tests for chain.analyzer

All web3 / RPC calls are mocked — no real node needed.

Test groups:
  1.  decode_function — known selectors, unknown, edge cases
  2.  parse_logs — Transfer, Swap, Sync events
  3.  format_text — output rendering
  4.  analyze() — orchestration, pending, failed, invalid hash
  5.  CLI — argument parsing, exit codes, output formats
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from chain.analyzer import (
    DEFAULT_RPC,
    ERC20_TRANSFER_TOPIC,
    UNISWAP_V2_SWAP_TOPIC,
    analyze,
    decode_function,
    detect_mev_signals,
    format_text,
    get_trace,
    main,
    parse_logs,
)

# ── Helpers ───────────────────────────────────────────────────────────────────

VALID_HASH = "0x" + "ab" * 32  # pragma: allowlist secret
ADDR_A = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # pragma: allowlist secret
ADDR_B = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # pragma: allowlist secret


def _make_mock_w3(
    tx: dict | None = None,
    receipt: dict | None = None,
    block_timestamp: int = 1705329825,
    call_raises: Exception | None = None,
) -> MagicMock:
    """Build a minimal mock web3 instance."""
    w3 = MagicMock()

    default_tx = {
        "hash": VALID_HASH,
        "from": ADDR_A,
        "to": ADDR_B,
        "value": 0,
        "gas": 21000,
        "nonce": 5,
        "input": b"",
        "blockNumber": 18_000_000,
    }
    w3.eth.get_transaction.return_value = tx if tx is not None else default_tx

    default_receipt = {
        "blockNumber": 18_000_000,
        "status": 1,
        "gasUsed": 21000,
        "effectiveGasPrice": 25_000_000_000,
        "logs": [],
        "transactionHash": bytes.fromhex("ab" * 32),
    }
    w3.eth.get_transaction_receipt.return_value = (
        receipt if receipt is not None else default_receipt
    )

    w3.eth.get_block.return_value = {"timestamp": block_timestamp}

    if call_raises:
        w3.eth.call.side_effect = call_raises
    else:
        w3.eth.call.return_value = b""

    # to_checksum_address passthrough
    from web3 import Web3

    w3.to_checksum_address = Web3.to_checksum_address

    return w3


# ── 1. decode_function ────────────────────────────────────────────────────────


class TestDecodeFunction:
    def test_empty_data_is_eth_transfer(self):
        result = decode_function(b"")
        assert result["name"] == "ETH Transfer"
        assert result["selector"] is None

    def test_short_data_is_eth_transfer(self):
        result = decode_function(b"\x01\x02\x03")
        assert result["name"] == "ETH Transfer"

    def test_known_erc20_transfer_selector(self):
        # transfer(address,uint256) selector = 0xa9059cbb
        selector = bytes.fromhex("a9059cbb")
        to_addr = bytes(12) + bytes.fromhex(ADDR_B[2:])
        amount = (1000 * 10**18).to_bytes(32, "big")
        data = selector + to_addr + amount
        result = decode_function(data)
        assert result["selector"] == "0xa9059cbb"
        assert "transfer" in result["name"]
        assert len(result["args"]) == 2

    def test_known_approve_selector(self):
        selector = bytes.fromhex("095ea7b3")
        spender = bytes(12) + bytes.fromhex(ADDR_B[2:])
        amount = (10**18).to_bytes(32, "big")
        data = selector + spender + amount
        result = decode_function(data)
        assert "approve" in result["name"]

    def test_uniswap_v2_swap_selector(self):
        selector = bytes.fromhex("38ed1739")
        data = selector + b"\x00" * 160  # 5 args * 32 bytes
        result = decode_function(data)
        assert "swapExactTokensForTokens" in result["name"]
        assert result["selector"] == "0x38ed1739"

    def test_unknown_selector_returns_raw(self):
        data = bytes.fromhex("deadbeef") + b"\x00" * 32
        result = decode_function(data)
        assert result["name"] == "Unknown"
        assert result["selector"] == "0xdeadbeef"
        assert len(result["args"]) == 1
        assert "data" in result["args"][0]["name"]

    def test_returns_dict_with_required_keys(self):
        result = decode_function(b"")
        assert "selector" in result
        assert "name" in result
        assert "args" in result

    def test_args_are_list(self):
        result = decode_function(b"")
        assert isinstance(result["args"], list)

    def test_uniswap_v3_multicall(self):
        selector = bytes.fromhex("ac9650d8")
        data = selector + b"\x00" * 32
        result = decode_function(data)
        assert "multicall" in result["name"]


# ── 2. parse_logs ─────────────────────────────────────────────────────────────


class TestParseLogs:
    def _make_transfer_log(self, from_addr: str, to_addr: str, amount: int) -> dict:
        """Build a minimal ERC-20 Transfer log."""
        from_topic = bytes(12) + bytes.fromhex(from_addr[2:])
        to_topic = bytes(12) + bytes.fromhex(to_addr[2:])
        return {
            "address": "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",  # USDC  # pragma: allowlist secret
            "topics": [
                bytes.fromhex(ERC20_TRANSFER_TOPIC[2:]),
                from_topic,
                to_topic,
            ],
            "data": amount.to_bytes(32, "big"),
        }

    def _make_swap_log(self, pair: str) -> dict:
        """Build a minimal Uniswap V2 Swap log."""
        return {
            "address": pair,
            "topics": [bytes.fromhex(UNISWAP_V2_SWAP_TOPIC[2:])],
            "data": (
                (1000 * 10**6).to_bytes(32, "big")  # amount0In
                + (0).to_bytes(32, "big")  # amount1In
                + (0).to_bytes(32, "big")  # amount0Out
                + (5 * 10**17).to_bytes(32, "big")  # amount1Out
            ),
        }

    def test_empty_logs_returns_empty(self):
        w3 = MagicMock()
        result = parse_logs([], w3)
        assert result == {"transfers": [], "swaps": [], "syncs": []}

    def test_parses_erc20_transfer(self):
        w3 = MagicMock()
        w3.eth.contract.return_value.functions.symbol.return_value.call.return_value = "USDC"
        w3.eth.contract.return_value.functions.decimals.return_value.call.return_value = 6
        from web3 import Web3

        w3.to_checksum_address = Web3.to_checksum_address

        log = self._make_transfer_log(ADDR_A, ADDR_B, 1_000_000)
        result = parse_logs([log], w3)

        assert len(result["transfers"]) == 1
        t = result["transfers"][0]
        assert t["amount_raw"] == 1_000_000

    def test_parses_swap_event(self):
        w3 = MagicMock()
        log = self._make_swap_log("0xPairAddress")
        result = parse_logs([log], w3)
        assert len(result["swaps"]) == 1
        swap = result["swaps"][0]
        assert swap["amount0In"] == 1000 * 10**6

    def test_unrecognised_topic_ignored(self):
        w3 = MagicMock()
        log = {
            "address": ADDR_A,
            "topics": [bytes(32)],  # all zeros — unknown topic
            "data": b"",
        }
        result = parse_logs([log], w3)
        assert result == {"transfers": [], "swaps": [], "syncs": []}

    def test_log_without_topics_ignored(self):
        w3 = MagicMock()
        result = parse_logs([{"address": ADDR_A, "topics": [], "data": b""}], w3)
        assert result["transfers"] == []

    def test_multiple_transfers_parsed(self):
        w3 = MagicMock()
        w3.eth.contract.return_value.functions.symbol.return_value.call.return_value = "USDC"
        w3.eth.contract.return_value.functions.decimals.return_value.call.return_value = 6
        from web3 import Web3

        w3.to_checksum_address = Web3.to_checksum_address

        logs = [
            self._make_transfer_log(ADDR_A, ADDR_B, 1_000_000),
            self._make_transfer_log(ADDR_B, ADDR_A, 500_000),
        ]
        result = parse_logs(logs, w3)
        assert len(result["transfers"]) == 2


# ── 3. format_text ────────────────────────────────────────────────────────────


class TestFormatText:
    def _base_analysis(self) -> dict:
        return {
            "transaction": {
                "hash": VALID_HASH,
                "from": ADDR_A,
                "to": ADDR_B,
                "value": 0,
                "gas": 21000,
                "nonce": 5,
                "input": "0x",
            },
            "receipt": {
                "block_number": 18_000_000,
                "status": True,
                "gas_used": 21000,
                "effective_gas_price": 25_000_000_000,
                "logs": [],
            },
            "timestamp": 1705329825,
            "function": {"selector": None, "name": "ETH Transfer", "args": []},
            "events": {"transfers": [], "swaps": [], "syncs": []},
        }

    def test_contains_hash(self):
        text = format_text(self._base_analysis())
        assert VALID_HASH in text

    def test_contains_success_status(self):
        text = format_text(self._base_analysis())
        assert "SUCCESS" in text

    def test_failed_status_shown(self):
        a = self._base_analysis()
        a["receipt"]["status"] = False
        text = format_text(a)
        assert "FAILED" in text

    def test_pending_shown_when_no_receipt(self):
        a = self._base_analysis()
        a["receipt"] = None
        text = format_text(a)
        assert "PENDING" in text

    def test_block_number_shown(self):
        text = format_text(self._base_analysis())
        assert "18,000,000" in text

    def test_timestamp_shown(self):
        text = format_text(self._base_analysis())
        assert "2024" in text

    def test_from_address_shown(self):
        text = format_text(self._base_analysis())
        assert ADDR_A in text

    def test_gas_section_shown(self):
        text = format_text(self._base_analysis())
        assert "Gas Analysis" in text
        assert "Gas Used" in text

    def test_function_section_shown_for_known(self):
        a = self._base_analysis()
        a["function"] = {
            "selector": "0xa9059cbb",
            "name": "transfer(address,uint256)",
            "args": [
                {"name": "to", "type": "address", "value": ADDR_B, "raw": 0},
                {"name": "amount", "type": "uint256", "value": 1000, "raw": 1000},
            ],
        }
        text = format_text(a)
        assert "Function Called" in text
        assert "transfer" in text

    def test_transfers_section_shown(self):
        a = self._base_analysis()
        a["events"]["transfers"] = [
            {
                "token": "USDC",
                "token_address": ADDR_B,
                "from": ADDR_A,
                "to": ADDR_B,
                "amount_raw": 1_000_000,
                "decimals": 6,
                "amount_fmt": "1.000000 USDC",
            }
        ]
        text = format_text(a)
        assert "Token Transfers" in text
        assert "USDC" in text

    def test_revert_reason_shown(self):
        a = self._base_analysis()
        a["receipt"]["status"] = False
        a["revert_reason"] = "execution reverted: insufficient output amount"
        text = format_text(a)
        assert "Revert Reason" in text
        assert "insufficient output amount" in text

    def test_returns_string(self):
        text = format_text(self._base_analysis())
        assert isinstance(text, str)


# ── 4. analyze() ─────────────────────────────────────────────────────────────


class TestAnalyze:
    def test_invalid_hash_too_short_raises(self):
        with pytest.raises(ValueError, match="Invalid transaction hash"):
            analyze("0xabcd", DEFAULT_RPC)

    def test_invalid_hash_no_prefix_raises(self):
        with pytest.raises(ValueError, match="Invalid transaction hash"):
            analyze("ab" * 32, DEFAULT_RPC)

    def test_returns_dict_with_required_keys(self):
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_web3_cls.return_value = _make_mock_w3()
            mock_web3_cls.HTTPProvider = MagicMock()
            mock_web3_cls.to_checksum_address = MagicMock(side_effect=lambda x: x)
            result = analyze(VALID_HASH, DEFAULT_RPC)
        assert "transaction" in result
        assert "function" in result

    def test_pending_tx_receipt_is_none(self):
        w3 = _make_mock_w3(receipt=None)
        w3.eth.get_transaction_receipt.return_value = None
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_web3_cls.return_value = w3
            mock_web3_cls.HTTPProvider = MagicMock()
            mock_web3_cls.to_checksum_address = MagicMock(side_effect=lambda x: x)
            result = analyze(VALID_HASH, DEFAULT_RPC)
        assert result["receipt"] is None

    def test_transaction_not_found_raises_runtime_error(self):
        w3 = MagicMock()
        w3.eth.get_transaction.side_effect = Exception("not found")
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_web3_cls.return_value = w3
            mock_web3_cls.HTTPProvider = MagicMock()
            with pytest.raises(RuntimeError, match="Could not fetch"):
                analyze(VALID_HASH, DEFAULT_RPC)

    def test_function_decoded_for_known_input(self):
        selector = bytes.fromhex("a9059cbb")
        payload = bytes(12) + bytes.fromhex(ADDR_B[2:]) + (10**18).to_bytes(32, "big")
        tx_with_input = {
            "hash": VALID_HASH,
            "from": ADDR_A,
            "to": ADDR_B,
            "value": 0,
            "gas": 50000,
            "nonce": 1,
            "input": selector + payload,
            "blockNumber": 18_000_000,
        }
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_web3_cls.return_value = _make_mock_w3(tx=tx_with_input)
            mock_web3_cls.HTTPProvider = MagicMock()
            mock_web3_cls.to_checksum_address = MagicMock(side_effect=lambda x: x)
            result = analyze(VALID_HASH, DEFAULT_RPC)
        assert "transfer" in result["function"]["name"]


# ── 5. CLI ────────────────────────────────────────────────────────────────────


class TestCLI:
    def test_invalid_hash_exits_with_1(self):
        exit_code = main(["0xinvalid"])
        assert exit_code == 1

    def test_valid_hash_exits_with_0(self):
        with patch("chain.analyzer.analyze") as mock_analyze:
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": {
                    "block_number": 1,
                    "status": True,
                    "gas_used": 21000,
                    "effective_gas_price": 25_000_000_000,
                    "logs": [],
                },
                "timestamp": 1705329825,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            exit_code = main([VALID_HASH])
        assert exit_code == 0

    def test_json_format_outputs_valid_json(self, capsys):
        with patch("chain.analyzer.analyze") as mock_analyze:
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": None,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            main([VALID_HASH, "--format", "json"])

        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "transaction" in parsed

    def test_text_format_is_default(self, capsys):
        with patch("chain.analyzer.analyze") as mock_analyze:
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": None,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            main([VALID_HASH])
        captured = capsys.readouterr()
        assert "Transaction Analysis" in captured.out

    def test_custom_rpc_passed_to_analyze(self):
        custom_rpc = "https://my-custom-rpc.example.com"
        with patch("chain.analyzer.analyze") as mock_analyze:
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": None,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            main([VALID_HASH, "--rpc", custom_rpc])
        mock_analyze.assert_called_once_with(VALID_HASH, custom_rpc)

    def test_rpc_error_exits_with_1(self):
        with patch("chain.analyzer.analyze", side_effect=RuntimeError("RPC failed")):
            exit_code = main([VALID_HASH])
        assert exit_code == 1

    def test_mev_flag_adds_mev_to_analysis(self, capsys):
        with patch("chain.analyzer.analyze") as mock_analyze:
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": {
                    "block_number": 1,
                    "status": True,
                    "gas_used": 21000,
                    "effective_gas_price": 25_000_000_000,
                    "logs": [],
                },
                "timestamp": 1705329825,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            exit_code = main([VALID_HASH, "--mev"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "MEV Analysis" in captured.out

    def test_trace_flag_warns_on_rpc_failure(self, capsys):
        with (
            patch("chain.analyzer.analyze") as mock_analyze,
            patch("chain.analyzer.get_trace", side_effect=RuntimeError("debug API not supported")),
        ):
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": None,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            exit_code = main([VALID_HASH, "--trace"])
        assert exit_code == 0  # trace failure is a warning, not a fatal error
        captured = capsys.readouterr()
        assert "Warning" in captured.err

    def test_trace_flag_includes_trace_in_output(self, capsys):
        trace_data = {
            "type": "CALL",
            "from": ADDR_A,
            "to": ADDR_B,
            "value": "0x0",
            "gasUsed": "0x5208",
            "calls": [],
        }
        with (
            patch("chain.analyzer.analyze") as mock_analyze,
            patch("chain.analyzer.get_trace", return_value=trace_data),
        ):
            mock_analyze.return_value = {
                "transaction": {
                    "hash": VALID_HASH,
                    "from": ADDR_A,
                    "to": ADDR_B,
                    "value": 0,
                    "gas": 21000,
                    "nonce": 0,
                    "input": "0x",
                },
                "receipt": None,
                "function": {"selector": None, "name": "ETH Transfer", "args": []},
                "events": {"transfers": [], "swaps": [], "syncs": []},
            }
            exit_code = main([VALID_HASH, "--trace"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "Call Trace" in captured.out


# ── 6. get_trace (stretch goal) ───────────────────────────────────────────────


class TestGetTrace:
    def test_invalid_hash_raises(self):
        with pytest.raises(ValueError, match="Invalid transaction hash"):
            get_trace("0xinvalid", DEFAULT_RPC)

    def test_invalid_hash_no_prefix_raises(self):
        with pytest.raises(ValueError, match="Invalid transaction hash"):
            get_trace("ab" * 32, DEFAULT_RPC)

    def test_returns_dict_on_success(self):
        trace_result = {
            "type": "CALL",
            "from": ADDR_A,
            "to": ADDR_B,
            "value": "0x0",
            "gasUsed": "0x5208",
        }
        mock_provider = MagicMock()
        mock_provider.make_request.return_value = {"result": trace_result}
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_instance = MagicMock()
            mock_instance.provider = mock_provider
            mock_web3_cls.return_value = mock_instance
            mock_web3_cls.HTTPProvider = MagicMock()
            result = get_trace(VALID_HASH, DEFAULT_RPC)
        assert result["type"] == "CALL"

    def test_rpc_error_response_raises_runtime_error(self):
        mock_provider = MagicMock()
        mock_provider.make_request.return_value = {
            "error": {"message": "Method not supported", "code": -32601}
        }
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_instance = MagicMock()
            mock_instance.provider = mock_provider
            mock_web3_cls.return_value = mock_instance
            mock_web3_cls.HTTPProvider = MagicMock()
            with pytest.raises(RuntimeError, match="Trace unavailable"):
                get_trace(VALID_HASH, DEFAULT_RPC)

    def test_network_exception_raises_runtime_error(self):
        mock_provider = MagicMock()
        mock_provider.make_request.side_effect = ConnectionError("refused")
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_instance = MagicMock()
            mock_instance.provider = mock_provider
            mock_web3_cls.return_value = mock_instance
            mock_web3_cls.HTTPProvider = MagicMock()
            with pytest.raises(RuntimeError, match="Trace request failed"):
                get_trace(VALID_HASH, DEFAULT_RPC)

    def test_empty_result_returns_empty_dict(self):
        mock_provider = MagicMock()
        mock_provider.make_request.return_value = {"result": {}}
        with patch("chain.analyzer.Web3") as mock_web3_cls:
            mock_instance = MagicMock()
            mock_instance.provider = mock_provider
            mock_web3_cls.return_value = mock_instance
            mock_web3_cls.HTTPProvider = MagicMock()
            result = get_trace(VALID_HASH, DEFAULT_RPC)
        assert result == {}


# ── 7. detect_mev_signals (stretch goal) ─────────────────────────────────────


class TestDetectMevSignals:
    def _base_analysis(self) -> dict:
        return {
            "transaction": {
                "hash": VALID_HASH,
                "from": ADDR_A,
                "to": ADDR_B,
                "value": 0,
                "gas": 21000,
                "input": "0x",
            },
            "receipt": {
                "block_number": 1,
                "status": True,
                "gas_used": 21000,
                "effective_gas_price": 25_000_000_000,
            },
            "events": {"transfers": [], "swaps": [], "syncs": []},
        }

    def test_returns_dict_with_required_keys(self):
        result = detect_mev_signals(self._base_analysis())
        assert "signals" in result
        assert "risk_level" in result
        assert "note" in result

    def test_signals_is_list(self):
        result = detect_mev_signals(self._base_analysis())
        assert isinstance(result["signals"], list)

    def test_clean_transaction_has_no_signals(self):
        result = detect_mev_signals(self._base_analysis())
        assert result["risk_level"] == "none"
        assert result["signals"] == []

    def test_high_effective_gas_price_triggers_signal(self):
        a = self._base_analysis()
        a["receipt"]["effective_gas_price"] = 150 * 10**9  # 150 gwei
        result = detect_mev_signals(a)
        assert "high_effective_gas_price" in result["signals"]
        assert result["risk_level"] in ("low", "medium", "high")

    def test_multiple_swaps_triggers_signal(self):
        a = self._base_analysis()
        a["events"]["swaps"] = [{"pair": ADDR_A}, {"pair": ADDR_B}]
        result = detect_mev_signals(a)
        assert "multiple_swaps" in result["signals"]

    def test_zero_value_swap_triggers_signal(self):
        a = self._base_analysis()
        a["transaction"]["value"] = 0
        a["transaction"]["input"] = "0xa9059cbb" + "00" * 60
        a["events"]["swaps"] = [{"pair": ADDR_A}]
        result = detect_mev_signals(a)
        assert "zero_value_swap" in result["signals"]

    def test_high_gas_swap_triggers_signal(self):
        a = self._base_analysis()
        a["transaction"]["gas"] = 600_000
        a["events"]["swaps"] = [{"pair": ADDR_A}]
        result = detect_mev_signals(a)
        assert "high_gas_swap" in result["signals"]

    def test_multiple_signals_raise_risk_level(self):
        a = self._base_analysis()
        a["receipt"]["effective_gas_price"] = 200 * 10**9
        a["events"]["swaps"] = [{"pair": ADDR_A}, {"pair": ADDR_B}]
        result = detect_mev_signals(a)
        assert result["risk_level"] in ("medium", "high")
        assert len(result["signals"]) >= 2

    def test_three_signals_is_high_risk(self):
        a = self._base_analysis()
        a["receipt"]["effective_gas_price"] = 200 * 10**9
        a["events"]["swaps"] = [{"pair": ADDR_A}, {"pair": ADDR_B}]
        a["transaction"]["gas"] = 700_000
        a["transaction"]["input"] = "0x38ed1739" + "00" * 100
        a["transaction"]["value"] = 0
        result = detect_mev_signals(a)
        assert result["risk_level"] == "high"

    def test_note_is_string(self):
        result = detect_mev_signals(self._base_analysis())
        assert isinstance(result["note"], str)
        assert len(result["note"]) > 0

    def test_missing_receipt_does_not_raise(self):
        a = self._base_analysis()
        a["receipt"] = None
        result = detect_mev_signals(a)
        assert "signals" in result

    def test_missing_events_does_not_raise(self):
        a = self._base_analysis()
        del a["events"]
        result = detect_mev_signals(a)
        assert "signals" in result
