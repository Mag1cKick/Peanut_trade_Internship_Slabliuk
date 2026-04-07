"""
core/wallet.py — Wallet key management, signing, and verification.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from eth_account import Account
from eth_account.messages import encode_defunct
from eth_account.signers.local import LocalAccount

if TYPE_CHECKING:
    from eth_account.datastructures import SignedMessage, SignedTransaction

log = logging.getLogger(__name__)


class _SecretStr:
    """
    Wraps a sensitive string so it can never leak via repr/str/format.

    >>> s = _SecretStr("super-secret")
    >>> str(s)
    '***'
    >>> repr(s)
    '_SecretStr(***)'
    >>> f"{s}"
    '***'
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        object.__setattr__(self, "_value", value)

    def reveal(self) -> str:
        """The ONLY way to access the underlying value."""
        return object.__getattribute__(self, "_value")

    def __str__(self) -> str:
        return "***"

    def __repr__(self) -> str:
        return "_SecretStr(***)"

    def __format__(self, spec: str) -> str:
        return "***"

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("_SecretStr is immutable")


class WalletManager:
    """
    Manages wallet operations: key loading, signing, and verification.
    """

    def __init__(self, account: LocalAccount) -> None:
        self._account: LocalAccount = account
        self._private_key: _SecretStr = _SecretStr(account.key.hex())

    @classmethod
    def from_env(cls, env_var: str = "PRIVATE_KEY") -> WalletManager:
        """
        Load private key from an environment variable.
        """
        raw = os.environ.get(env_var)
        if not raw:
            raise OSError(
                f"Environment variable '{env_var}' is not set or empty. "
                "Add it to your .env file — never hardcode keys in source."
            )
        return cls.from_key(raw.strip())

    @classmethod
    def from_key(cls, private_key: str) -> WalletManager:
        """
        Load from a raw hex private key string.
        """
        if not isinstance(private_key, str):
            raise TypeError(
                f"private_key must be a str, got {type(private_key).__name__}. "
                "Never pass key material as non-string types."
            )
        try:
            account: LocalAccount = Account.from_key(private_key)
        except Exception as exc:
            raise ValueError(
                "Invalid private key format. "
                "Expected a 32-byte hex string (with or without 0x prefix)."
            ) from exc
        return cls(account)

    @classmethod
    def generate(cls) -> WalletManager:
        """
        Generate a new random wallet.
        """
        account: LocalAccount = Account.create()
        instance = cls(account)
        print("[WalletManager] New wallet generated.")
        print(f"  Address:     {instance.address}")
        print(f"  Private key: {instance._private_key.reveal()}")
        print("  ⚠ Save this key securely. It will NOT be shown again.")
        return instance

    @classmethod
    def from_keyfile(cls, path: str, password: str) -> WalletManager:
        """
        Load private key from an encrypted JSON keyfile (geth/clef format).

        Args:
            path: Path to the encrypted keyfile.
            password: Decryption password.

        Raises:
            TypeError: If password is not a string.
            FileNotFoundError: If the keyfile does not exist.
            ValueError: If the file is not valid JSON or decryption fails.
        """
        import json as _json

        if not isinstance(password, str):
            raise TypeError(f"password must be str, got {type(password).__name__}.")
        try:
            with open(path) as f:
                keyfile_json = _json.load(f)
        except FileNotFoundError as exc:
            raise FileNotFoundError(f"Keyfile not found: {path}") from exc
        except _json.JSONDecodeError as exc:
            raise ValueError(f"Invalid keyfile — {path} is not valid JSON.") from exc
        try:
            private_key_bytes = Account.decrypt(keyfile_json, password)
        except Exception as exc:
            raise ValueError(
                "Failed to decrypt keyfile. Check the password and file integrity."
            ) from exc
        return cls(Account.from_key(private_key_bytes))

    def to_keyfile(self, path: str, password: str) -> None:
        """
        Export wallet to an encrypted JSON keyfile (geth/clef format).

        Args:
            path: Destination file path.
            password: Encryption password (must not be empty).

        Raises:
            TypeError: If password is not a string.
            ValueError: If password is empty.
        """
        import json as _json

        if not isinstance(password, str):
            raise TypeError(f"password must be str, got {type(password).__name__}.")
        if not password:
            raise ValueError("Keyfile password must not be empty.")
        encrypted = Account.encrypt(self._private_key.reveal(), password)
        with open(path, "w") as f:
            _json.dump(encrypted, f)

    @property
    def address(self) -> str:
        """Returns the EIP-55 checksummed Ethereum address."""
        return self._account.address

    def sign_message(self, message: str) -> SignedMessage:
        """
        Sign an arbitrary text message using EIP-191 prefix.
        """
        if not isinstance(message, str):
            raise TypeError(f"message must be str, got {type(message).__name__}.")
        if not message:
            raise ValueError("Cannot sign an empty message. " "Provide a non-empty string.")
        encoded = encode_defunct(text=message)
        return self._account.sign_message(encoded)

    def sign_typed_data(
        self,
        domain: dict,
        types: dict,
        value: dict,
    ) -> SignedMessage:
        """
        Sign EIP-712 typed structured data.
        """
        for name, arg in (("domain", domain), ("types", types), ("value", value)):
            if not isinstance(arg, dict):
                raise TypeError(f"'{name}' must be a dict, got {type(arg).__name__}.")
        return Account.sign_typed_data(
            self._private_key.reveal(),
            domain_data=domain,
            message_types=types,
            message_data=value,
        )

    def sign_transaction(self, tx: dict) -> SignedTransaction:
        """
        Sign a transaction dict (EIP-1559 or legacy).
        """
        if not isinstance(tx, dict):
            raise TypeError(f"tx must be a dict, got {type(tx).__name__}.")
        required = {"to", "nonce", "gas", "chainId"}
        missing = required - tx.keys()
        if missing:
            raise ValueError(
                f"Transaction dict is missing required fields: {sorted(missing)}. "
                "All of {to, nonce, gas, chainId} are required."
            )
        return self._account.sign_transaction(tx)

    def verify_message(self, message: str, signature: str) -> bool:
        """
        Return True if the signature was produced by this wallet's key.
        """
        encoded = encode_defunct(text=message)
        recovered = Account.recover_message(encoded, signature=signature)
        return recovered.lower() == self.address.lower()

    def __repr__(self) -> str:
        return f"WalletManager(address={self.address})"

    def __str__(self) -> str:
        return f"WalletManager(address={self.address})"
