"""
tests/test_builder.py — Unit tests for chain.builder.TransactionBuilder

All RPC calls and wallet signing are mocked — no real node or key needed.

Test groups:
  1.  Construction
  2.  Fluent setters — happy path
  3.  Fluent setters — validation / negative cases
  4.  with_gas_estimate — happy path and edge cases
  5.  with_gas_price — happy path and edge cases
  6.  build() — validation, auto-nonce, field assembly
  7.  build_and_sign()
  8.  send()
  9.  send_and_wait()
  10. Chaining — full fluent usage end-to-end
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock

import pytest

from chain.builder import NonceManager, TransactionBuilder
from chain.client import GasPrice
from chain.errors import TransactionFailed, TransactionTimeout
from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest

# ── Test addresses ────────────────────────────────────────────────────────────

ADDR_A = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # pragma: allowlist secret
ADDR_B = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # pragma: allowlist secret

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def address_a() -> Address:
    return Address(ADDR_A)


@pytest.fixture
def address_b() -> Address:
    return Address(ADDR_B)


@pytest.fixture
def mock_client() -> MagicMock:
    client = MagicMock()
    client.get_nonce.return_value = 5
    client.estimate_gas.return_value = 21000
    client.get_gas_price.return_value = GasPrice(
        base_fee=20_000_000_000,
        priority_fee_low=1_000_000_000,
        priority_fee_medium=2_000_000_000,
        priority_fee_high=5_000_000_000,
    )
    client.send_transaction.return_value = "0x" + "ab" * 32
    client.wait_for_receipt.return_value = TransactionReceipt(
        tx_hash="0x" + "ab" * 32,
        block_number=18_000_000,
        status=True,
        gas_used=21000,
        effective_gas_price=25_000_000_000,
        logs=[],
    )
    return client


@pytest.fixture
def mock_wallet(address_a) -> MagicMock:
    wallet = MagicMock()
    wallet.address = ADDR_A
    signed = MagicMock()
    signed.raw_transaction = b"\x02" + b"\x00" * 100
    wallet.sign_transaction.return_value = signed
    return wallet


@pytest.fixture
def builder(mock_client, mock_wallet) -> TransactionBuilder:
    return TransactionBuilder(mock_client, mock_wallet)


@pytest.fixture
def eth_value() -> TokenAmount:
    return TokenAmount.from_human("0.1", 18, "ETH")


@pytest.fixture
def full_builder(builder, address_b, eth_value) -> TransactionBuilder:
    """Builder with all required fields set."""
    return builder.to(address_b).value(eth_value).data(b"")


# ── 1. Construction ───────────────────────────────────────────────────────────


class TestConstruction:
    def test_creates_builder(self, mock_client, mock_wallet):
        b = TransactionBuilder(mock_client, mock_wallet)
        assert isinstance(b, TransactionBuilder)

    def test_default_chain_id_is_1(self, builder):
        assert builder._chain_id == 1

    def test_all_fields_start_as_none(self, builder):
        assert builder._to is None
        assert builder._value is None
        assert builder._data is None
        assert builder._nonce is None
        assert builder._gas_limit is None
        assert builder._max_fee_per_gas is None
        assert builder._max_priority_fee is None


# ── 2. Fluent setters — happy path ────────────────────────────────────────────


class TestFluentSetters:
    def test_to_returns_self(self, builder, address_b):
        result = builder.to(address_b)
        assert result is builder

    def test_to_sets_address(self, builder, address_b):
        builder.to(address_b)
        assert builder._to == address_b

    def test_value_returns_self(self, builder, eth_value):
        result = builder.value(eth_value)
        assert result is builder

    def test_value_sets_amount(self, builder, eth_value):
        builder.value(eth_value)
        assert builder._value == eth_value

    def test_data_returns_self(self, builder):
        result = builder.data(b"\x12\x34")
        assert result is builder

    def test_data_sets_calldata(self, builder):
        builder.data(b"\x12\x34")
        assert builder._data == b"\x12\x34"

    def test_nonce_returns_self(self, builder):
        result = builder.nonce(7)
        assert result is builder

    def test_nonce_sets_value(self, builder):
        builder.nonce(7)
        assert builder._nonce == 7

    def test_gas_limit_returns_self(self, builder):
        result = builder.gas_limit(50000)
        assert result is builder

    def test_gas_limit_sets_value(self, builder):
        builder.gas_limit(50000)
        assert builder._gas_limit == 50000

    def test_chain_id_sets_value(self, builder):
        builder.chain_id(11155111)  # Sepolia
        assert builder._chain_id == 11155111

    def test_chaining_works(self, builder, address_b, eth_value):
        result = builder.to(address_b).value(eth_value).data(b"").nonce(3)
        assert result is builder
        assert builder._to == address_b
        assert builder._nonce == 3


# ── 3. Fluent setters — negative cases ───────────────────────────────────────


class TestFluentSettersValidation:
    def test_to_non_address_raises(self, builder):
        with pytest.raises(TypeError, match="Address instance"):
            builder.to("0xnotanaddress")  # type: ignore

    def test_to_string_raises(self, builder):
        with pytest.raises(TypeError):
            builder.to(ADDR_B)  # type: ignore

    def test_value_non_token_amount_raises(self, builder):
        with pytest.raises(TypeError, match="TokenAmount"):
            builder.value(100)  # type: ignore

    def test_value_wrong_decimals_raises(self, builder):
        usdc_amount = TokenAmount(raw=1_000_000, decimals=6, symbol="USDC")
        with pytest.raises(ValueError, match="18 decimals"):
            builder.value(usdc_amount)

    def test_data_non_bytes_raises(self, builder):
        with pytest.raises(TypeError, match="bytes"):
            builder.data("not bytes")  # type: ignore

    def test_nonce_negative_raises(self, builder):
        with pytest.raises(ValueError, match="nonce"):
            builder.nonce(-1)

    def test_nonce_float_raises(self, builder):
        with pytest.raises(ValueError):
            builder.nonce(1.5)  # type: ignore

    def test_gas_limit_zero_raises(self, builder):
        with pytest.raises(ValueError, match="gas_limit"):
            builder.gas_limit(0)

    def test_gas_limit_negative_raises(self, builder):
        with pytest.raises(ValueError):
            builder.gas_limit(-100)


# ── 4. with_gas_estimate ─────────────────────────────────────────────────────


class TestWithGasEstimate:
    def test_sets_gas_limit_with_buffer(self, full_builder, mock_client):
        mock_client.estimate_gas.return_value = 21000
        full_builder.with_gas_estimate(buffer=1.2)
        assert full_builder._gas_limit == int(21000 * 1.2)

    def test_returns_self(self, full_builder):
        result = full_builder.with_gas_estimate()
        assert result is full_builder

    def test_calls_estimate_gas_on_client(self, full_builder, mock_client):
        full_builder.with_gas_estimate()
        mock_client.estimate_gas.assert_called_once()

    def test_buffer_below_1_raises(self, full_builder):
        with pytest.raises(ValueError, match="buffer"):
            full_builder.with_gas_estimate(buffer=0.9)

    def test_buffer_exactly_1_raises(self, full_builder):
        with pytest.raises(ValueError, match="buffer"):
            full_builder.with_gas_estimate(buffer=1.0)

    def test_missing_to_raises(self, builder, eth_value):
        builder.value(eth_value).data(b"")
        with pytest.raises(ValueError, match="to"):
            builder.with_gas_estimate()

    def test_missing_value_raises(self, builder, address_b):
        builder.to(address_b).data(b"")
        with pytest.raises(ValueError, match="value"):
            builder.with_gas_estimate()

    def test_missing_data_raises(self, builder, address_b, eth_value):
        builder.to(address_b).value(eth_value)
        with pytest.raises(ValueError, match="data"):
            builder.with_gas_estimate()

    def test_higher_buffer_gives_higher_limit(self, full_builder, mock_client):
        mock_client.estimate_gas.return_value = 21000
        full_builder.with_gas_estimate(buffer=1.5)
        assert full_builder._gas_limit == int(21000 * 1.5)


# ── 5. with_gas_price ────────────────────────────────────────────────────────


class TestWithGasPrice:
    def test_sets_max_fee_and_priority_fee(self, full_builder, mock_client):
        full_builder.with_gas_price("medium")
        assert full_builder._max_fee_per_gas is not None
        assert full_builder._max_priority_fee == 2_000_000_000

    def test_returns_self(self, full_builder):
        result = full_builder.with_gas_price()
        assert result is full_builder

    def test_calls_get_gas_price_on_client(self, full_builder, mock_client):
        full_builder.with_gas_price()
        mock_client.get_gas_price.assert_called_once()

    def test_low_priority(self, full_builder, mock_client):
        full_builder.with_gas_price("low")
        assert full_builder._max_priority_fee == 1_000_000_000

    def test_high_priority(self, full_builder, mock_client):
        full_builder.with_gas_price("high")
        assert full_builder._max_priority_fee == 5_000_000_000

    def test_invalid_priority_raises(self, full_builder):
        with pytest.raises(ValueError, match="priority"):
            full_builder.with_gas_price("ultra")


# ── 6. build() ────────────────────────────────────────────────────────────────


class TestBuild:
    def test_returns_transaction_request(self, full_builder):
        tx = full_builder.build()
        assert isinstance(tx, TransactionRequest)

    def test_auto_fetches_nonce_when_not_set(self, full_builder, mock_client):
        mock_client.get_nonce.return_value = 7
        tx = full_builder.build()
        assert tx.nonce == 7
        mock_client.get_nonce.assert_called_once()

    def test_uses_explicit_nonce_over_chain(self, full_builder, mock_client):
        full_builder.nonce(99)
        tx = full_builder.build()
        assert tx.nonce == 99
        mock_client.get_nonce.assert_not_called()

    def test_missing_to_raises(self, builder, eth_value):
        builder.value(eth_value).data(b"")
        with pytest.raises(ValueError, match="to"):
            builder.build()

    def test_missing_value_raises(self, builder, address_b):
        builder.to(address_b).data(b"")
        with pytest.raises(ValueError, match="value"):
            builder.build()

    def test_missing_data_raises(self, builder, address_b, eth_value):
        builder.to(address_b).value(eth_value)
        with pytest.raises(ValueError, match="data"):
            builder.build()

    def test_gas_limit_passed_through(self, full_builder):
        full_builder.gas_limit(42000)
        tx = full_builder.build()
        assert tx.gas_limit == 42000

    def test_chain_id_passed_through(self, full_builder):
        full_builder.chain_id(11155111)
        tx = full_builder.build()
        assert tx.chain_id == 11155111

    def test_to_address_in_result(self, full_builder, address_b):
        tx = full_builder.build()
        assert tx.to == address_b

    def test_value_in_result(self, full_builder, eth_value):
        tx = full_builder.build()
        assert tx.value == eth_value

    def test_data_in_result(self, full_builder):
        tx = full_builder.build()
        assert tx.data == b""


# ── 7. build_and_sign() ───────────────────────────────────────────────────────


class TestBuildAndSign:
    def test_calls_wallet_sign_transaction(self, full_builder, mock_wallet):
        full_builder.build_and_sign()
        mock_wallet.sign_transaction.assert_called_once()

    def test_returns_signed_transaction(self, full_builder, mock_wallet):
        result = full_builder.build_and_sign()
        assert result is mock_wallet.sign_transaction.return_value

    def test_sign_receives_dict(self, full_builder, mock_wallet):
        full_builder.build_and_sign()
        call_args = mock_wallet.sign_transaction.call_args[0][0]
        assert isinstance(call_args, dict)
        assert "to" in call_args

    def test_missing_fields_raises_before_signing(self, builder, mock_wallet):
        with pytest.raises(ValueError):
            builder.build_and_sign()
        mock_wallet.sign_transaction.assert_not_called()


# ── 8. send() ────────────────────────────────────────────────────────────────


class TestSend:
    def test_returns_tx_hash_string(self, full_builder, mock_client):
        result = full_builder.send()
        assert isinstance(result, str)
        assert result == "0x" + "ab" * 32

    def test_calls_send_transaction_on_client(self, full_builder, mock_client):
        full_builder.send()
        mock_client.send_transaction.assert_called_once()

    def test_sends_raw_transaction_bytes(self, full_builder, mock_client, mock_wallet):
        full_builder.send()
        raw_sent = mock_client.send_transaction.call_args[0][0]
        assert isinstance(raw_sent, bytes)


# ── 9. send_and_wait() ───────────────────────────────────────────────────────


class TestSendAndWait:
    def test_returns_receipt(self, full_builder, mock_client):
        receipt = full_builder.send_and_wait()
        assert isinstance(receipt, TransactionReceipt)
        assert receipt.status is True

    def test_calls_wait_for_receipt(self, full_builder, mock_client):
        full_builder.send_and_wait(timeout=60)
        mock_client.wait_for_receipt.assert_called_once()
        call_kwargs = mock_client.wait_for_receipt.call_args
        assert call_kwargs[1]["timeout"] == 60 or call_kwargs[0][1] == 60

    def test_propagates_timeout_error(self, full_builder, mock_client):
        mock_client.wait_for_receipt.side_effect = TransactionTimeout("0xabc", 120)
        with pytest.raises(TransactionTimeout):
            full_builder.send_and_wait()

    def test_propagates_transaction_failed(self, full_builder, mock_client):
        receipt = TransactionReceipt(
            tx_hash="0xabc",
            block_number=1,
            status=False,
            gas_used=21000,
            effective_gas_price=25_000_000_000,
            logs=[],
        )
        mock_client.wait_for_receipt.side_effect = TransactionFailed("0xabc", receipt)
        with pytest.raises(TransactionFailed):
            full_builder.send_and_wait()


# ── 10. Chaining — full end-to-end ────────────────────────────────────────────


class TestFullChain:
    def test_full_fluent_chain_build(self, builder, address_b, eth_value, mock_client):
        tx = (
            builder.to(address_b)
            .value(eth_value)
            .data(b"")
            .nonce(3)
            .gas_limit(21000)
            .with_gas_price("medium")
            .build()
        )
        assert isinstance(tx, TransactionRequest)
        assert tx.to == address_b
        assert tx.nonce == 3
        assert tx.gas_limit == 21000

    def test_full_fluent_chain_send_and_wait(self, builder, address_b, eth_value, mock_client):
        receipt = (
            builder.to(address_b)
            .value(eth_value)
            .data(b"")
            .with_gas_estimate()
            .with_gas_price("high")
            .send_and_wait(timeout=60)
        )
        assert receipt.status is True

    def test_builder_can_be_reused_for_different_nonces(
        self, mock_client, mock_wallet, address_b, eth_value
    ):
        """Building twice should not mutate shared state."""
        b1 = (
            TransactionBuilder(mock_client, mock_wallet)
            .to(address_b)
            .value(eth_value)
            .data(b"")
            .nonce(1)
        )
        b2 = (
            TransactionBuilder(mock_client, mock_wallet)
            .to(address_b)
            .value(eth_value)
            .data(b"")
            .nonce(2)
        )
        tx1 = b1.build()
        tx2 = b2.build()
        assert tx1.nonce == 1
        assert tx2.nonce == 2

    def test_with_gas_estimate_then_explicit_gas_limit_overrides(self, full_builder, mock_client):
        """Calling gas_limit() after with_gas_estimate() should override."""
        mock_client.estimate_gas.return_value = 21000
        full_builder.with_gas_estimate(buffer=1.2).gas_limit(99999)
        tx = full_builder.build()
        assert tx.gas_limit == 99999


# ── 11. NonceManager (stretch goal) ──────────────────────────────────────────


class TestNonceManager:
    def test_get_next_initializes_from_chain(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 5
        nm = NonceManager(mock_client, address_a)
        assert nm.get_next() == 5

    def test_get_next_increments_locally(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 10
        nm = NonceManager(mock_client, address_a)
        assert nm.get_next() == 10
        assert nm.get_next() == 11
        assert nm.get_next() == 12

    def test_get_next_fetches_chain_only_once(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 3
        nm = NonceManager(mock_client, address_a)
        nm.get_next()
        nm.get_next()
        nm.get_next()
        mock_client.get_nonce.assert_called_once()

    def test_peek_returns_current_nonce(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 7
        nm = NonceManager(mock_client, address_a)
        assert nm.peek() == 7
        assert nm.peek() == 7  # does not increment

    def test_peek_does_not_increment(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 7
        nm = NonceManager(mock_client, address_a)
        nm.peek()
        nm.peek()
        assert nm.get_next() == 7  # still 7

    def test_sync_refetches_from_chain(self, mock_client, address_a):
        mock_client.get_nonce.side_effect = [5, 20]
        nm = NonceManager(mock_client, address_a)
        nm.get_next()  # initializes to 5, now at 6
        nm.sync()  # re-fetches, gets 20
        assert nm.get_next() == 20

    def test_sync_resets_local_counter(self, mock_client, address_a):
        mock_client.get_nonce.side_effect = [0, 50]
        nm = NonceManager(mock_client, address_a)
        for _ in range(5):
            nm.get_next()  # burns 0–4
        nm.sync()
        assert nm.peek() == 50

    def test_reset_overrides_nonce(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 5
        nm = NonceManager(mock_client, address_a)
        nm.reset(100)
        assert nm.get_next() == 100

    def test_reset_does_not_call_chain(self, mock_client, address_a):
        nm = NonceManager(mock_client, address_a)
        nm.reset(42)
        mock_client.get_nonce.assert_not_called()

    def test_reset_negative_raises(self, mock_client, address_a):
        nm = NonceManager(mock_client, address_a)
        with pytest.raises(ValueError):
            nm.reset(-1)

    def test_reset_non_int_raises(self, mock_client, address_a):
        nm = NonceManager(mock_client, address_a)
        with pytest.raises(ValueError):
            nm.reset("five")  # type: ignore

    def test_thread_safety_no_duplicate_nonces(self, mock_client, address_a):
        mock_client.get_nonce.return_value = 0
        nm = NonceManager(mock_client, address_a)
        nonces: list[int] = []
        lock = threading.Lock()

        def grab():
            n = nm.get_next()
            with lock:
                nonces.append(n)

        threads = [threading.Thread(target=grab) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(nonces) == 50
        assert sorted(nonces) == list(range(50))  # all unique, sequential
