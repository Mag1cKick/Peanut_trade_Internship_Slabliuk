"""
tests/test_wallet.py — Unit tests for core.wallet.WalletManager

Test groups:
  1. Key loading (from_env, from_key, generate)
  2. Security — private key must never leak
  3. sign_message — happy path + negative cases
  4. sign_typed_data — happy path + negative cases
  5. sign_transaction — happy path + negative cases
  6. verify_message — round-trip verification
"""

import pytest

from core.wallet import WalletManager, _SecretStr

# ── Fixtures ──────────────────────────────────────────────────────────────────

# A well-known test private key (NOT for use with real funds)
TEST_PRIVATE_KEY = (
    "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"  # pragma: allowlist secret
)
TEST_ADDRESS = (
    "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"  # checksummed  # pragma: allowlist secret
)


@pytest.fixture
def wallet() -> WalletManager:
    return WalletManager.from_key(TEST_PRIVATE_KEY)


# ── 1. Key loading ────────────────────────────────────────────────────────────


class TestKeyLoading:
    def test_from_key_returns_wallet_manager(self, wallet):
        assert isinstance(wallet, WalletManager)

    def test_from_key_correct_address(self, wallet):
        assert wallet.address == TEST_ADDRESS

    def test_from_key_without_0x_prefix(self):
        key_no_prefix = TEST_PRIVATE_KEY.lstrip("0x")
        w = WalletManager.from_key(key_no_prefix)
        assert w.address == TEST_ADDRESS

    def test_from_key_invalid_hex_raises(self):
        with pytest.raises(ValueError, match="Invalid private key"):
            WalletManager.from_key("not-a-valid-key")

    def test_from_key_wrong_type_raises(self):
        with pytest.raises(TypeError):
            WalletManager.from_key(12345)  # type: ignore

    def test_from_env_reads_variable(self, wallet, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY", TEST_PRIVATE_KEY)
        loaded = WalletManager.from_env("PRIVATE_KEY")
        assert loaded.address == wallet.address

    def test_from_env_missing_variable_raises(self, monkeypatch):
        monkeypatch.delenv("PRIVATE_KEY", raising=False)
        with pytest.raises(EnvironmentError, match="PRIVATE_KEY"):
            WalletManager.from_env("PRIVATE_KEY")

    def test_from_env_empty_variable_raises(self, monkeypatch):
        monkeypatch.setenv("PRIVATE_KEY", "")
        with pytest.raises(EnvironmentError):
            WalletManager.from_env("PRIVATE_KEY")

    def test_from_env_custom_var_name(self, monkeypatch):
        monkeypatch.setenv("MY_BOT_KEY", TEST_PRIVATE_KEY)
        w = WalletManager.from_env("MY_BOT_KEY")
        assert w.address == TEST_ADDRESS

    def test_generate_returns_wallet_manager(self, capsys):
        w = WalletManager.generate()
        assert isinstance(w, WalletManager)
        assert w.address.startswith("0x")
        assert len(w.address) == 42

    def test_generate_different_keys_each_time(self, capsys):
        w1 = WalletManager.generate()
        w2 = WalletManager.generate()
        assert w1.address != w2.address


# ── 2. Security — key must NEVER leak ────────────────────────────────────────


class TestKeySecurity:
    def test_repr_does_not_contain_private_key(self, wallet):
        key_fragment = TEST_PRIVATE_KEY[2:10]  # strip 0x, take first 8 chars
        assert key_fragment not in repr(wallet)

    def test_str_does_not_contain_private_key(self, wallet):
        key_fragment = TEST_PRIVATE_KEY[2:10]
        assert key_fragment not in str(wallet)

    def test_repr_shows_address(self, wallet):
        assert TEST_ADDRESS in repr(wallet)

    def test_str_shows_address(self, wallet):
        assert TEST_ADDRESS in str(wallet)

    def test_format_does_not_contain_private_key(self, wallet):
        key_fragment = TEST_PRIVATE_KEY[2:10]
        assert key_fragment not in f"{wallet}"

    def test_secret_str_repr_is_masked(self):
        s = _SecretStr("super-secret-value")
        assert "super-secret-value" not in repr(s)
        assert repr(s) == "_SecretStr(***)"

    def test_secret_str_str_is_masked(self):
        s = _SecretStr("super-secret-value")
        assert str(s) == "***"

    def test_secret_str_format_is_masked(self):
        s = _SecretStr("super-secret-value")
        assert f"{s}" == "***"

    def test_secret_str_is_immutable(self):
        s = _SecretStr("value")
        with pytest.raises(AttributeError):
            s._value = "hacked"  # type: ignore

    def test_secret_str_reveal_returns_value(self):
        s = _SecretStr("my-secret")
        assert s.reveal() == "my-secret"

    def test_invalid_key_error_message_does_not_contain_key(self):
        bad_key = "0xdeadbeef_not_a_real_key"
        try:
            WalletManager.from_key(bad_key)
        except ValueError as e:
            assert "deadbeef" not in str(e)
            assert "not_a_real_key" not in str(e)


# ── 3. sign_message ───────────────────────────────────────────────────────────


class TestSignMessage:
    def test_sign_message_returns_signed_message(self, wallet):
        signed = wallet.sign_message("hello world")
        assert signed is not None
        assert hasattr(signed, "signature")

    def test_sign_message_deterministic(self, wallet):
        """Same message + key must always produce the same signature."""
        sig1 = wallet.sign_message("determinism test")
        sig2 = wallet.sign_message("determinism test")
        assert sig1.signature == sig2.signature

    def test_sign_message_different_messages_produce_different_sigs(self, wallet):
        sig1 = wallet.sign_message("message A")
        sig2 = wallet.sign_message("message B")
        assert sig1.signature != sig2.signature

    def test_sign_empty_message_raises(self, wallet):
        with pytest.raises(ValueError, match="empty"):
            wallet.sign_message("")

    def test_sign_message_wrong_type_raises(self, wallet):
        with pytest.raises(TypeError):
            wallet.sign_message(123)  # type: ignore

    def test_sign_message_bytes_type_raises(self, wallet):
        with pytest.raises(TypeError):
            wallet.sign_message(b"bytes not allowed")  # type: ignore

    def test_sign_message_none_raises(self, wallet):
        with pytest.raises(TypeError):
            wallet.sign_message(None)  # type: ignore


# ── 4. sign_typed_data ────────────────────────────────────────────────────────


class TestSignTypedData:
    # Minimal valid EIP-712 domain + type + value
    DOMAIN = {"name": "TestApp", "version": "1", "chainId": 1}
    TYPES = {"Mail": [{"name": "contents", "type": "string"}]}
    VALUE = {"contents": "Hello EIP-712"}

    def test_sign_typed_data_returns_signed_message(self, wallet):
        signed = wallet.sign_typed_data(self.DOMAIN, self.TYPES, self.VALUE)
        assert signed is not None
        assert hasattr(signed, "signature")

    def test_sign_typed_data_domain_not_dict_raises(self, wallet):
        with pytest.raises(TypeError, match="domain"):
            wallet.sign_typed_data("bad", self.TYPES, self.VALUE)  # type: ignore

    def test_sign_typed_data_types_not_dict_raises(self, wallet):
        with pytest.raises(TypeError, match="types"):
            wallet.sign_typed_data(self.DOMAIN, ["bad"], self.VALUE)  # type: ignore

    def test_sign_typed_data_value_not_dict_raises(self, wallet):
        with pytest.raises(TypeError, match="value"):
            wallet.sign_typed_data(self.DOMAIN, self.TYPES, "bad")  # type: ignore


# ── 5. sign_transaction ───────────────────────────────────────────────────────


class TestSignTransaction:
    # Minimal valid EIP-1559 transaction dict
    VALID_TX = {
        "to": "0x70997970C51812dc3A010C7d01b50e0d17dc79C8",
        "nonce": 0,
        "gas": 21000,
        "chainId": 1,
        "maxFeePerGas": 30_000_000_000,
        "maxPriorityFeePerGas": 1_000_000_000,
        "value": 0,
        "data": b"",
    }

    def test_sign_transaction_returns_signed_tx(self, wallet):
        signed = wallet.sign_transaction(self.VALID_TX)
        assert signed is not None
        # eth-account >= 0.9 uses raw_transaction; older uses rawTransaction
        assert hasattr(signed, "raw_transaction") or hasattr(signed, "rawTransaction")

    def test_sign_transaction_not_dict_raises(self, wallet):
        with pytest.raises(TypeError):
            wallet.sign_transaction("not a dict")  # type: ignore

    def test_sign_transaction_missing_to_raises(self, wallet):
        bad_tx = {k: v for k, v in self.VALID_TX.items() if k != "to"}
        with pytest.raises(ValueError, match="to"):
            wallet.sign_transaction(bad_tx)

    def test_sign_transaction_missing_nonce_raises(self, wallet):
        bad_tx = {k: v for k, v in self.VALID_TX.items() if k != "nonce"}
        with pytest.raises(ValueError, match="nonce"):
            wallet.sign_transaction(bad_tx)

    def test_sign_transaction_missing_gas_raises(self, wallet):
        bad_tx = {k: v for k, v in self.VALID_TX.items() if k != "gas"}
        with pytest.raises(ValueError, match="gas"):
            wallet.sign_transaction(bad_tx)

    def test_sign_transaction_missing_chain_id_raises(self, wallet):
        bad_tx = {k: v for k, v in self.VALID_TX.items() if k != "chainId"}
        with pytest.raises(ValueError, match="chainId"):
            wallet.sign_transaction(bad_tx)


# ── 6. verify_message ────────────────────────────────────────────────────────


class TestVerifyMessage:
    def test_verify_own_signature_returns_true(self, wallet):
        message = "verify me"
        signed = wallet.sign_message(message)
        assert wallet.verify_message(message, signed.signature.hex()) is True

    def test_verify_wrong_message_returns_false(self, wallet):
        signed = wallet.sign_message("original message")
        assert wallet.verify_message("tampered message", signed.signature.hex()) is False

    def test_verify_signature_from_different_wallet_returns_false(self, wallet, capsys):
        other = WalletManager.generate()
        message = "cross-wallet test"
        signed = other.sign_message(message)
        assert wallet.verify_message(message, signed.signature.hex()) is False
