"""
tests/test_types.py — Unit tests for core.types

Test groups:
  1.  Address — construction and validation
  2.  Address — equality and hashing
  3.  Address — properties
  4.  TokenAmount — construction
  5.  TokenAmount — from_human factory
  6.  TokenAmount — arithmetic (add, sub, mul)
  7.  TokenAmount — float rejection
  8.  TokenAmount — equality and ordering
  9.  Token — equality by address only
  10. Token — hashing consistency
  11. TransactionRequest — to_dict
  12. TransactionReceipt — from_web3 and tx_fee
"""

from decimal import Decimal

import pytest

from core.types import (
    USDC,
    WETH,
    Address,
    Token,
    TokenAmount,
    TransactionReceipt,
    TransactionRequest,
)

# ── Fixtures ──────────────────────────────────────────────────────────────────

ADDR_LOWER = "0xf39fd6e51aad88f6f4ce6ab8827279cfffb92266"  # pragma: allowlist secret
ADDR_UPPER = "0xF39FD6E51AAD88F6F4CE6AB8827279CFFFB92266"  # pragma: allowlist secret
ADDR_CHECK = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # EIP-55  # pragma: allowlist secret
ADDR2_CHECK = "0x70997970C51812dc3A010C7d01b50e0d17dc79C8"  # pragma: allowlist secret


@pytest.fixture
def addr() -> Address:
    return Address(ADDR_LOWER)


@pytest.fixture
def addr2() -> Address:
    return Address(ADDR2_CHECK)


@pytest.fixture
def eth_token() -> Token:
    return Token(address=Address(ADDR_CHECK), symbol="ETH", decimals=18)


@pytest.fixture
def usdc_token() -> Token:
    return Token(address=Address(ADDR2_CHECK), symbol="USDC", decimals=6)


# ── 1. Address — construction and validation ──────────────────────────────────


class TestAddressConstruction:
    def test_valid_address_accepted(self):
        a = Address(ADDR_LOWER)
        assert isinstance(a, Address)

    def test_stored_as_checksum(self):
        a = Address(ADDR_LOWER)
        assert a.value == ADDR_CHECK

    def test_uppercase_input_stored_as_checksum(self):
        a = Address(ADDR_UPPER)
        assert a.value == ADDR_CHECK

    def test_from_string_factory(self):
        a = Address.from_string(ADDR_LOWER)
        assert a.value == ADDR_CHECK

    def test_invalid_address_raises_value_error(self):
        with pytest.raises(ValueError, match="not a valid Ethereum address"):
            Address("invalid")

    def test_too_short_hex_raises(self):
        with pytest.raises(ValueError):
            Address("0x1234")

    def test_no_0x_prefix_raises(self):
        with pytest.raises(ValueError):
            Address("f39fd6e51aad88f6f4ce6ab8827279cfffb92266")  # pragma: allowlist secret

    def test_non_string_raises_type_error(self):
        with pytest.raises(TypeError):
            Address(123)  # type: ignore

    def test_none_raises(self):
        with pytest.raises(TypeError):
            Address(None)  # type: ignore

    def test_empty_string_raises(self):
        with pytest.raises(ValueError):
            Address("")

    def test_frozen_immutable(self):
        a = Address(ADDR_LOWER)
        with pytest.raises(Exception):
            a.value = "0x1234"  # type: ignore


# ── 2. Address — equality and hashing ────────────────────────────────────────


class TestAddressEquality:
    def test_same_address_equal(self, addr):
        assert addr == Address(ADDR_LOWER)

    def test_case_insensitive_equality(self):
        assert Address(ADDR_LOWER) == Address(ADDR_UPPER)

    def test_checksum_equals_lower(self):
        assert Address(ADDR_CHECK) == Address(ADDR_LOWER)

    def test_different_address_not_equal(self, addr, addr2):
        assert addr != addr2

    def test_equal_addresses_have_same_hash(self):
        assert hash(Address(ADDR_LOWER)) == hash(Address(ADDR_UPPER))

    def test_different_addresses_have_different_hash(self, addr, addr2):
        assert hash(addr) != hash(addr2)

    def test_usable_as_dict_key(self, addr):
        d = {addr: "value"}
        assert d[Address(ADDR_UPPER)] == "value"

    def test_usable_in_set(self):
        s = {Address(ADDR_LOWER), Address(ADDR_UPPER)}
        assert len(s) == 1


# ── 3. Address — properties ───────────────────────────────────────────────────


class TestAddressProperties:
    def test_checksum_property(self, addr):
        assert addr.checksum == ADDR_CHECK

    def test_lower_property(self, addr):
        assert addr.lower == ADDR_LOWER

    def test_str_returns_checksum(self, addr):
        assert str(addr) == ADDR_CHECK

    def test_repr_contains_checksum(self, addr):
        assert ADDR_CHECK in repr(addr)


# ── 4. TokenAmount — construction ────────────────────────────────────────────


class TestTokenAmountConstruction:
    def test_basic_construction(self):
        amt = TokenAmount(raw=1000, decimals=6)
        assert amt.raw == 1000
        assert amt.decimals == 6

    def test_with_symbol(self):
        amt = TokenAmount(raw=1000, decimals=6, symbol="USDC")
        assert amt.symbol == "USDC"

    def test_float_raw_raises(self):
        with pytest.raises(TypeError, match="int"):
            TokenAmount(raw=1.5, decimals=18)  # type: ignore

    def test_negative_decimals_raises(self):
        with pytest.raises(ValueError, match="decimals"):
            TokenAmount(raw=100, decimals=-1)

    def test_zero_raw_allowed(self):
        amt = TokenAmount(raw=0, decimals=18)
        assert amt.raw == 0

    def test_frozen_immutable(self):
        amt = TokenAmount(raw=100, decimals=18)
        with pytest.raises(Exception):
            amt.raw = 200  # type: ignore


# ── 5. TokenAmount — from_human ───────────────────────────────────────────────


class TestTokenAmountFromHuman:
    def test_1_5_eth_raw(self):
        amt = TokenAmount.from_human("1.5", 18)
        assert amt.raw == 1_500_000_000_000_000_000

    def test_1_eth_raw(self):
        amt = TokenAmount.from_human("1", 18)
        assert amt.raw == 10**18

    def test_1_usdc_raw(self):
        amt = TokenAmount.from_human("1", 6)
        assert amt.raw == 1_000_000

    def test_small_amount(self):
        amt = TokenAmount.from_human("0.000001", 6)
        assert amt.raw == 1

    def test_decimal_input(self):
        amt = TokenAmount.from_human(Decimal("1.5"), 18)
        assert amt.raw == 1_500_000_000_000_000_000

    def test_float_input_raises(self):
        with pytest.raises(TypeError, match="float"):
            TokenAmount.from_human(1.5, 18)  # type: ignore

    def test_invalid_string_raises(self):
        with pytest.raises(ValueError):
            TokenAmount.from_human("not-a-number", 18)

    def test_negative_amount_raises(self):
        with pytest.raises(ValueError, match="negative"):
            TokenAmount.from_human("-1", 18)

    def test_symbol_stored(self):
        amt = TokenAmount.from_human("1", 18, symbol="ETH")
        assert amt.symbol == "ETH"

    def test_human_property_round_trips(self):
        amt = TokenAmount.from_human("1.5", 18)
        assert amt.human == Decimal("1.5")

    def test_no_float_internally(self):
        """human property must return Decimal, not float."""
        amt = TokenAmount.from_human("1.5", 18)
        assert isinstance(amt.human, Decimal)

    def test_str_representation(self):
        amt = TokenAmount.from_human("1.5", 18, symbol="ETH")
        assert "1.5" in str(amt)
        assert "ETH" in str(amt)


# ── 6. TokenAmount — arithmetic ───────────────────────────────────────────────


class TestTokenAmountArithmetic:
    def test_add_same_decimals(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("0.5", 18)
        result = a + b
        assert result.raw == a.raw + b.raw
        assert result.decimals == 18

    def test_add_different_decimals_raises(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("1", 6)
        with pytest.raises(ValueError, match="decimals"):
            _ = a + b

    def test_add_preserves_symbol_when_same(self):
        a = TokenAmount.from_human("1", 18, symbol="ETH")
        b = TokenAmount.from_human("1", 18, symbol="ETH")
        assert (a + b).symbol == "ETH"

    def test_add_clears_symbol_when_different(self):
        a = TokenAmount.from_human("1", 18, symbol="ETH")
        b = TokenAmount.from_human("1", 18, symbol="WETH")
        assert (a + b).symbol is None

    def test_sub_same_decimals(self):
        a = TokenAmount.from_human("1.5", 18)
        b = TokenAmount.from_human("0.5", 18)
        result = a - b
        assert result.raw == a.raw - b.raw

    def test_sub_different_decimals_raises(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("1", 6)
        with pytest.raises(ValueError, match="decimals"):
            _ = a - b

    def test_sub_negative_result_raises(self):
        a = TokenAmount.from_human("0.5", 18)
        b = TokenAmount.from_human("1", 18)
        with pytest.raises(ValueError, match="negative"):
            _ = a - b

    def test_mul_by_int(self):
        a = TokenAmount.from_human("1", 18)
        result = a * 3
        assert result.raw == a.raw * 3

    def test_mul_by_decimal(self):
        a = TokenAmount.from_human("1", 18)
        result = a * Decimal("0.5")
        assert result.raw == a.raw // 2

    def test_mul_by_float_raises(self):
        a = TokenAmount.from_human("1", 18)
        with pytest.raises(TypeError, match="float"):
            _ = a * 1.5  # type: ignore

    def test_arithmetic_never_uses_float(self):
        """Result of add/mul must always be int raw, never float."""
        a = TokenAmount.from_human("1.123456789", 18)
        b = TokenAmount.from_human("0.987654321", 18)
        result = a + b
        assert isinstance(result.raw, int)


# ── 7. TokenAmount — equality and ordering ────────────────────────────────────


class TestTokenAmountEquality:
    def test_equal_amounts(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("1", 18)
        assert a == b

    def test_different_raw_not_equal(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("2", 18)
        assert a != b

    def test_different_decimals_not_equal(self):
        a = TokenAmount(raw=1000, decimals=6)
        b = TokenAmount(raw=1000, decimals=18)
        assert a != b

    def test_less_than(self):
        a = TokenAmount.from_human("0.5", 18)
        b = TokenAmount.from_human("1", 18)
        assert a < b

    def test_less_than_or_equal(self):
        a = TokenAmount.from_human("1", 18)
        assert a <= a

    def test_equal_amounts_same_hash(self):
        a = TokenAmount.from_human("1", 18)
        b = TokenAmount.from_human("1", 18)
        assert hash(a) == hash(b)


# ── 8. Token — equality by address only ──────────────────────────────────────


class TestTokenEquality:
    def test_same_address_equal(self, eth_token):
        same = Token(address=Address(ADDR_CHECK), symbol="ETH", decimals=18)
        assert eth_token == same

    def test_same_address_different_symbol_still_equal(self):
        """Identity is by address — metadata doesn't matter."""
        t1 = Token(address=Address(ADDR_CHECK), symbol="ETH", decimals=18)
        t2 = Token(address=Address(ADDR_CHECK), symbol="WETH", decimals=18)
        assert t1 == t2

    def test_same_address_different_decimals_still_equal(self):
        t1 = Token(address=Address(ADDR_CHECK), symbol="TKN", decimals=18)
        t2 = Token(address=Address(ADDR_CHECK), symbol="TKN", decimals=6)
        assert t1 == t2

    def test_different_addresses_not_equal(self, eth_token, usdc_token):
        assert eth_token != usdc_token

    def test_address_case_insensitive_equality(self):
        t1 = Token(address=Address(ADDR_LOWER), symbol="X", decimals=18)
        t2 = Token(address=Address(ADDR_UPPER), symbol="X", decimals=18)
        assert t1 == t2

    def test_not_equal_to_non_token(self, eth_token):
        assert eth_token != "not a token"
        assert eth_token != 42


# ── 9. Token — hash consistency ───────────────────────────────────────────────


class TestTokenHashing:
    def test_equal_tokens_same_hash(self):
        t1 = Token(address=Address(ADDR_CHECK), symbol="ETH", decimals=18)
        t2 = Token(address=Address(ADDR_LOWER), symbol="WETH", decimals=6)
        assert hash(t1) == hash(t2)

    def test_different_address_different_hash(self, eth_token, usdc_token):
        assert hash(eth_token) != hash(usdc_token)

    def test_usable_as_dict_key(self, eth_token):
        prices = {eth_token: Decimal("3000")}
        same_addr_diff_symbol = Token(address=Address(ADDR_CHECK), symbol="WETH", decimals=18)
        assert prices[same_addr_diff_symbol] == Decimal("3000")

    def test_usable_in_set(self, eth_token):
        same = Token(address=Address(ADDR_CHECK), symbol="ETH", decimals=18)
        s = {eth_token, same}
        assert len(s) == 1

    def test_repr(self, eth_token):
        r = repr(eth_token)
        assert "ETH" in r
        assert ADDR_CHECK in r

    def test_well_known_weth_constant(self):
        assert WETH.symbol == "WETH"
        assert WETH.decimals == 18

    def test_well_known_usdc_constant(self):
        assert USDC.symbol == "USDC"
        assert USDC.decimals == 6


# ── 10. TransactionRequest — to_dict ─────────────────────────────────────────


class TestTransactionRequest:
    @pytest.fixture
    def tx(self, addr, addr2):
        return TransactionRequest(
            to=addr,
            value=TokenAmount.from_human("0.1", 18, "ETH"),
            data=b"\x12\x34",
            nonce=5,
            gas_limit=21000,
            max_fee_per_gas=30_000_000_000,
            max_priority_fee=1_000_000_000,
            chain_id=1,
        )

    def test_to_dict_has_required_fields(self, tx, addr):
        d = tx.to_dict()
        assert d["to"] == addr.checksum
        assert d["chainId"] == 1
        assert d["data"] == b"\x12\x34"

    def test_to_dict_value_is_raw_int(self, tx):
        d = tx.to_dict()
        assert isinstance(d["value"], int)
        assert d["value"] == TokenAmount.from_human("0.1", 18).raw

    def test_to_dict_optional_fields_included_when_set(self, tx):
        d = tx.to_dict()
        assert d["nonce"] == 5
        assert d["gas"] == 21000
        assert d["maxFeePerGas"] == 30_000_000_000
        assert d["maxPriorityFeePerGas"] == 1_000_000_000

    def test_to_dict_optional_fields_omitted_when_none(self, addr):
        tx = TransactionRequest(
            to=addr,
            value=TokenAmount(raw=0, decimals=18),
            data=b"",
        )
        d = tx.to_dict()
        assert "nonce" not in d
        assert "gas" not in d
        assert "maxFeePerGas" not in d

    def test_to_is_checksum_address(self, tx, addr):
        assert tx.to_dict()["to"] == addr.checksum


# ── 11. TransactionReceipt ────────────────────────────────────────────────────


class TestTransactionReceipt:
    @pytest.fixture
    def receipt_dict(self):
        return {
            "transactionHash": bytes.fromhex("abcd" * 16),
            "blockNumber": 18_000_000,
            "status": 1,
            "gasUsed": 21000,
            "effectiveGasPrice": 25_000_000_000,  # 25 gwei
            "logs": [],
        }

    def test_from_web3_parses_correctly(self, receipt_dict):
        r = TransactionReceipt.from_web3(receipt_dict)
        assert r.block_number == 18_000_000
        assert r.status is True
        assert r.gas_used == 21000
        assert r.effective_gas_price == 25_000_000_000

    def test_from_web3_status_false_on_revert(self, receipt_dict):
        receipt_dict["status"] = 0
        r = TransactionReceipt.from_web3(receipt_dict)
        assert r.status is False

    def test_tx_fee_calculation(self, receipt_dict):
        r = TransactionReceipt.from_web3(receipt_dict)
        # fee = 21000 * 25 gwei = 525000 gwei = 0.000525 ETH
        expected_wei = 21000 * 25_000_000_000
        assert r.tx_fee.raw == expected_wei
        assert r.tx_fee.decimals == 18
        assert r.tx_fee.symbol == "ETH"

    def test_tx_fee_is_token_amount(self, receipt_dict):
        r = TransactionReceipt.from_web3(receipt_dict)
        assert isinstance(r.tx_fee, TokenAmount)

    def test_logs_parsed(self, receipt_dict):
        receipt_dict["logs"] = [{"data": "0x"}]
        r = TransactionReceipt.from_web3(receipt_dict)
        assert len(r.logs) == 1
