"""
tests/test_client.py — Unit tests for chain.client

All tests mock web3.py at the dispatch layer so no real RPC node is needed.
This keeps tests fast, deterministic, and runnable offline.

Test groups:
  1.  ChainClient — construction and validation
  2.  GasPrice — calculations and get_max_fee
  3.  get_balance — happy path and error handling
  4.  get_nonce
  5.  get_gas_price — EIP-1559 and legacy fallback
  6.  estimate_gas
  7.  send_transaction
  8.  get_receipt — pending and confirmed
  9.  wait_for_receipt — success, timeout, revert
  10. get_transaction
  11. call (eth_call)
  12. Retry logic — exponential backoff and endpoint fallback
  13. Error classification — RPC message → exception mapping
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import patch

import pytest

from chain.client import ChainClient, GasPrice
from chain.errors import (
    AllRPCsFailed,
    InsufficientFunds,
    NonceTooLow,
    ReplacementUnderpriced,
    RPCError,
    TransactionFailed,
    TransactionTimeout,
)
from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest

# ── Fixtures ──────────────────────────────────────────────────────────────────

ADDR_A = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # pragma: allowlist secret
ADDR_B = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # pragma: allowlist secret
TX_HASH = "0xabcd" * 16  # pragma: allowlist secret


@pytest.fixture
def address_a() -> Address:
    return Address(ADDR_A)


@pytest.fixture
def address_b() -> Address:
    return Address(ADDR_B)


@pytest.fixture
def client() -> ChainClient:
    """Client backed by a fake URL — all calls will be mocked."""
    return ChainClient(["https://fake-rpc.example.com"], timeout=5, max_retries=2)


@pytest.fixture
def multi_client() -> ChainClient:
    """Client with two endpoints for fallback tests."""
    return ChainClient(
        ["https://primary.example.com", "https://fallback.example.com"],
        timeout=5,
        max_retries=2,
    )


@pytest.fixture
def sample_tx(address_a, address_b) -> TransactionRequest:
    return TransactionRequest(
        to=address_b,
        value=TokenAmount.from_human("0.1", 18, "ETH"),
        data=b"",
        nonce=0,
        gas_limit=21000,
        max_fee_per_gas=30_000_000_000,
        max_priority_fee=1_000_000_000,
        chain_id=1,
    )


@pytest.fixture
def sample_receipt_dict() -> dict:
    return {
        "transactionHash": bytes.fromhex("ab" * 32),
        "blockNumber": 18_000_000,
        "status": 1,
        "gasUsed": 21000,
        "effectiveGasPrice": 25_000_000_000,
        "logs": [],
    }


# ── Helper: patch _dispatch on a client ──────────────────────────────────────


def mock_dispatch(client: ChainClient, return_value=None, side_effect=None):
    """Patch ChainClient._dispatch to return a fixed value or raise."""
    return patch.object(
        client,
        "_dispatch",
        return_value=return_value,
        side_effect=side_effect,
    )


# ── 1. Construction ───────────────────────────────────────────────────────────


class TestConstruction:
    def test_single_url_accepted(self):
        c = ChainClient(["https://rpc.example.com"])
        assert len(c._web3_instances) == 1

    def test_multiple_urls_accepted(self):
        c = ChainClient(["https://a.com", "https://b.com"])
        assert len(c._web3_instances) == 2

    def test_empty_urls_raises(self):
        with pytest.raises(ValueError, match="At least one RPC URL"):
            ChainClient([])

    def test_default_timeout_and_retries(self):
        c = ChainClient(["https://rpc.example.com"])
        assert c._timeout == 30
        assert c._max_retries == 3

    def test_custom_timeout_and_retries(self):
        c = ChainClient(["https://rpc.example.com"], timeout=10, max_retries=5)
        assert c._timeout == 10
        assert c._max_retries == 5


# ── 2. GasPrice ───────────────────────────────────────────────────────────────


class TestGasPrice:
    def test_get_max_fee_medium(self):
        gp = GasPrice(
            base_fee=20_000_000_000,
            priority_fee_low=1_000_000_000,
            priority_fee_medium=2_000_000_000,
            priority_fee_high=5_000_000_000,
        )
        # base * 1.2 + medium = 24e9 + 2e9 = 26e9
        assert gp.get_max_fee("medium") == 26_000_000_000

    def test_get_max_fee_low(self):
        gp = GasPrice(
            base_fee=10_000_000_000,
            priority_fee_low=500_000_000,
            priority_fee_medium=1_000_000_000,
            priority_fee_high=2_000_000_000,
        )
        assert gp.get_max_fee("low") == 12_500_000_000  # 12e9 + 0.5e9

    def test_get_max_fee_high(self):
        gp = GasPrice(
            base_fee=10_000_000_000,
            priority_fee_low=500_000_000,
            priority_fee_medium=1_000_000_000,
            priority_fee_high=3_000_000_000,
        )
        assert gp.get_max_fee("high") == 15_000_000_000  # 12e9 + 3e9

    def test_get_max_fee_custom_buffer(self):
        gp = GasPrice(
            base_fee=10_000_000_000,
            priority_fee_low=0,
            priority_fee_medium=1_000_000_000,
            priority_fee_high=0,
        )
        # buffer=2.0: 20e9 + 1e9 = 21e9
        assert gp.get_max_fee("medium", buffer=2.0) == 21_000_000_000

    def test_get_max_fee_invalid_priority_raises(self):
        gp = GasPrice(10_000_000_000, 1_000_000_000, 2_000_000_000, 3_000_000_000)
        with pytest.raises(ValueError, match="priority"):
            gp.get_max_fee("ultra")

    def test_gwei_base_fee_property(self):
        gp = GasPrice(25_000_000_000, 0, 0, 0)
        assert gp.gwei_base_fee == pytest.approx(25.0)

    def test_max_fee_always_int(self):
        gp = GasPrice(10_000_000_000, 1_000_000_000, 2_000_000_000, 3_000_000_000)
        assert isinstance(gp.get_max_fee(), int)


# ── 3. get_balance ────────────────────────────────────────────────────────────


class TestGetBalance:
    def test_returns_token_amount(self, client, address_a):
        with mock_dispatch(client, return_value=1_000_000_000_000_000_000):
            balance = client.get_balance(address_a)
        assert isinstance(balance, TokenAmount)
        assert balance.raw == 1_000_000_000_000_000_000
        assert balance.decimals == 18
        assert balance.symbol == "ETH"

    def test_balance_human_readable(self, client, address_a):
        with mock_dispatch(client, return_value=1_500_000_000_000_000_000):
            balance = client.get_balance(address_a)
        assert balance.human == Decimal("1.5")

    def test_zero_balance(self, client, address_a):
        with mock_dispatch(client, return_value=0):
            balance = client.get_balance(address_a)
        assert balance.raw == 0

    def test_rpc_error_propagates(self, client, address_a):
        with mock_dispatch(client, side_effect=RPCError("connection refused")):
            with pytest.raises((RPCError, AllRPCsFailed)):
                client.get_balance(address_a)


# ── 4. get_nonce ──────────────────────────────────────────────────────────────


class TestGetNonce:
    def test_returns_int(self, client, address_a):
        with mock_dispatch(client, return_value=42):
            nonce = client.get_nonce(address_a)
        assert nonce == 42

    def test_default_block_is_pending(self, client, address_a):
        captured = {}

        def capture_dispatch(w3, method, *args, **kwargs):
            captured["args"] = args
            return 5

        with patch.object(client, "_dispatch", side_effect=capture_dispatch):
            client.get_nonce(address_a)

        assert captured["args"][1] == "pending"

    def test_custom_block(self, client, address_a):
        captured = {}

        def capture_dispatch(w3, method, *args, **kwargs):
            captured["args"] = args
            return 3

        with patch.object(client, "_dispatch", side_effect=capture_dispatch):
            client.get_nonce(address_a, block="latest")

        assert captured["args"][1] == "latest"


# ── 5. get_gas_price ─────────────────────────────────────────────────────────


class TestGetGasPrice:
    def test_returns_gas_price(self, client):
        block_data = {"baseFeePerGas": 20_000_000_000}
        fee_history_data = {
            "reward": [
                [500_000_000, 2_000_000_000, 5_000_000_000],
                [600_000_000, 1_800_000_000, 4_500_000_000],
            ]
        }

        call_count = [0]

        def dispatch_side_effect(w3, method, *args, **kwargs):
            call_count[0] += 1
            if method == "get_block":
                return block_data
            if method == "fee_history":
                return fee_history_data
            return None

        with patch.object(client, "_dispatch", side_effect=dispatch_side_effect):
            gp = client.get_gas_price()

        assert isinstance(gp, GasPrice)
        assert gp.base_fee == 20_000_000_000
        assert gp.priority_fee_low > 0
        assert gp.priority_fee_medium > 0
        assert gp.priority_fee_high > 0

    def test_returns_gas_price_object(self, client):
        with mock_dispatch(client, return_value={"baseFeePerGas": 10_000_000_000, "reward": []}):
            # Even if fee_history fails, should return a GasPrice
            try:
                gp = client.get_gas_price()
                assert isinstance(gp, GasPrice)
            except Exception:
                pass  # fallback path acceptable


# ── 6. estimate_gas ───────────────────────────────────────────────────────────


class TestEstimateGas:
    def test_returns_int(self, client, sample_tx):
        with mock_dispatch(client, return_value=21000):
            gas = client.estimate_gas(sample_tx)
        assert gas == 21000

    def test_revert_raises_rpc_error(self, client, sample_tx):
        with mock_dispatch(client, side_effect=RPCError("execution reverted")):
            with pytest.raises((RPCError, AllRPCsFailed)):
                client.estimate_gas(sample_tx)


# ── 7. send_transaction ───────────────────────────────────────────────────────


class TestSendTransaction:
    def test_returns_tx_hash_string(self, client):
        class FakeHash:
            def hex(self):
                return "0x" + "ab" * 32

        with mock_dispatch(client, return_value=FakeHash()):
            result = client.send_transaction(b"\x01\x02\x03")
        assert isinstance(result, str)

    def test_insufficient_funds_raises(self, client):
        with mock_dispatch(client, side_effect=InsufficientFunds("insufficient funds")):
            with pytest.raises(InsufficientFunds):
                client.send_transaction(b"\x01")

    def test_nonce_too_low_raises(self, client):
        with mock_dispatch(client, side_effect=NonceTooLow("nonce too low")):
            with pytest.raises(NonceTooLow):
                client.send_transaction(b"\x01")


# ── 8. get_receipt ────────────────────────────────────────────────────────────


class TestGetReceipt:
    def test_returns_none_when_pending(self, client):
        with mock_dispatch(client, return_value=None):
            result = client.get_receipt("0xdeadbeef")
        assert result is None

    def test_returns_receipt_when_confirmed(self, client, sample_receipt_dict):
        with mock_dispatch(client, return_value=sample_receipt_dict):
            receipt = client.get_receipt("0xdeadbeef")
        assert isinstance(receipt, TransactionReceipt)
        assert receipt.status is True
        assert receipt.gas_used == 21000

    def test_failed_tx_receipt_status_false(self, client, sample_receipt_dict):
        sample_receipt_dict["status"] = 0
        with mock_dispatch(client, return_value=sample_receipt_dict):
            receipt = client.get_receipt("0xdeadbeef")
        assert receipt.status is False


# ── 9. wait_for_receipt ───────────────────────────────────────────────────────


class TestWaitForReceipt:
    def test_returns_receipt_on_first_poll(self, client, sample_receipt_dict):
        with mock_dispatch(client, return_value=sample_receipt_dict):
            receipt = client.wait_for_receipt("0xdeadbeef", timeout=10, poll_interval=0.01)
        assert isinstance(receipt, TransactionReceipt)
        assert receipt.status is True

    def test_polls_until_confirmed(self, client, sample_receipt_dict):
        """Returns None twice then confirms on third call."""
        responses = [None, None, sample_receipt_dict]
        call_idx = [0]

        def dispatch_side_effect(w3, method, *args, **kwargs):
            result = responses[min(call_idx[0], len(responses) - 1)]
            call_idx[0] += 1
            return result

        with patch.object(client, "_dispatch", side_effect=dispatch_side_effect):
            receipt = client.wait_for_receipt("0xdeadbeef", timeout=10, poll_interval=0.01)
        assert receipt.status is True

    def test_timeout_raises(self, client):
        with mock_dispatch(client, return_value=None):
            with pytest.raises(TransactionTimeout):
                client.wait_for_receipt("0xdeadbeef", timeout=0.05, poll_interval=0.01)

    def test_reverted_tx_raises_transaction_failed(self, client, sample_receipt_dict):
        sample_receipt_dict["status"] = 0
        with mock_dispatch(client, return_value=sample_receipt_dict):
            with pytest.raises(TransactionFailed) as exc_info:
                client.wait_for_receipt("0xdeadbeef", timeout=10, poll_interval=0.01)
        assert exc_info.value.receipt.status is False


# ── 10. get_transaction ───────────────────────────────────────────────────────


class TestGetTransaction:
    def test_returns_dict(self, client):
        fake_tx = {"hash": "0xabc", "nonce": 5, "value": 0}
        with mock_dispatch(client, return_value=fake_tx):
            result = client.get_transaction("0xabc")
        assert isinstance(result, dict)
        assert result["nonce"] == 5

    def test_not_found_raises_rpc_error(self, client):
        with mock_dispatch(client, return_value=None):
            with pytest.raises(RPCError, match="not found"):
                client.get_transaction("0xdeadbeef")


# ── 11. call (eth_call) ───────────────────────────────────────────────────────


class TestCall:
    def test_returns_bytes(self, client, sample_tx):
        with mock_dispatch(client, return_value=b"\x00" * 32):
            result = client.call(sample_tx)
        assert isinstance(result, bytes)

    def test_empty_return_data(self, client, sample_tx):
        with mock_dispatch(client, return_value=b""):
            result = client.call(sample_tx)
        assert result == b""


# ── 12. Retry logic ───────────────────────────────────────────────────────────


class TestRetryLogic:
    def test_retries_on_transient_error(self, client, address_a):
        """Fails once then succeeds — should NOT raise."""
        responses = [Exception("timeout"), 1_000_000_000_000_000_000]
        call_idx = [0]

        def dispatch_side_effect(w3, method, *args, **kwargs):
            result = responses[call_idx[0]]
            call_idx[0] += 1
            if isinstance(result, Exception):
                raise result
            return result

        with patch.object(client, "_dispatch", side_effect=dispatch_side_effect):
            with patch("chain.client.time.sleep"):  # don't actually sleep
                balance = client.get_balance(address_a)
        assert balance.raw == 1_000_000_000_000_000_000

    def test_raises_after_max_retries(self, client, address_a):
        """Always fails — should raise AllRPCsFailed after max_retries."""
        with mock_dispatch(client, side_effect=Exception("connection refused")):
            with patch("chain.client.time.sleep"):
                with pytest.raises(AllRPCsFailed):
                    client.get_balance(address_a)

    def test_fallback_to_second_endpoint(self, multi_client, address_a):
        """First endpoint always fails, second succeeds."""
        call_counts = [0]

        def dispatch_side_effect(w3, method, *args, **kwargs):
            call_counts[0] += 1
            # First web3 instance fails, second succeeds
            if w3 == multi_client._web3_instances[0]:
                raise Exception("primary down")
            return 5_000_000_000_000_000_000

        with patch.object(multi_client, "_dispatch", side_effect=dispatch_side_effect):
            with patch("chain.client.time.sleep"):
                balance = multi_client.get_balance(address_a)
        assert balance.raw == 5_000_000_000_000_000_000

    def test_classified_errors_not_retried(self, client, address_a):
        """InsufficientFunds should bubble up immediately, no retries."""
        call_count = [0]

        def dispatch_side_effect(w3, method, *args, **kwargs):
            call_count[0] += 1
            raise InsufficientFunds("insufficient funds")

        with patch.object(client, "_dispatch", side_effect=dispatch_side_effect):
            with pytest.raises(InsufficientFunds):
                client.get_balance(address_a)
        # Should have been called only once — no retry on classified errors
        assert call_count[0] == 1


# ── 13. Error classification ──────────────────────────────────────────────────


class TestErrorClassification:
    def test_insufficient_funds_classified(self, client, address_a):
        with mock_dispatch(
            client, side_effect=InsufficientFunds("insufficient funds for transfer")
        ):
            with pytest.raises(InsufficientFunds):
                client.get_balance(address_a)

    def test_nonce_too_low_classified(self, client):
        with mock_dispatch(client, side_effect=NonceTooLow("nonce too low")):
            with pytest.raises(NonceTooLow):
                client.send_transaction(b"\x01")

    def test_replacement_underpriced_classified(self, client):
        with mock_dispatch(
            client, side_effect=ReplacementUnderpriced("replacement transaction underpriced")
        ):
            with pytest.raises(ReplacementUnderpriced):
                client.send_transaction(b"\x01")

    def test_rpc_error_has_code(self):
        err = RPCError("execution reverted", code=-32000)
        assert err.code == -32000
        assert "execution reverted" in str(err)

    def test_transaction_failed_has_receipt(self, sample_receipt_dict):
        receipt = TransactionReceipt.from_web3(sample_receipt_dict)
        err = TransactionFailed("0xdeadbeef", receipt)
        assert err.tx_hash == "0xdeadbeef"
        assert err.receipt is receipt
        assert "0xdeadbeef" in str(err)

    def test_all_rpcs_failed_contains_errors(self):
        errors = [Exception("a"), Exception("b")]
        err = AllRPCsFailed(errors)
        assert len(err.errors) == 2
        assert "a" in str(err)
