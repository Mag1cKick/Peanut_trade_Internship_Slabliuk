"""
chain/builder.py — Fluent builder for constructing and sending transactions.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest

if TYPE_CHECKING:
    from eth_account.datastructures import SignedTransaction

    from chain.client import ChainClient
    from core.wallet import WalletManager

log = logging.getLogger(__name__)


class TransactionBuilder:
    """
    Fluent builder for Ethereum transactions.
    """

    def __init__(self, client: ChainClient, wallet: WalletManager) -> None:
        self._client = client
        self._wallet = wallet
        # Required
        self._to: Address | None = None
        self._value: TokenAmount | None = None
        self._data: bytes | None = None
        # Optional
        self._nonce: int | None = None
        self._gas_limit: int | None = None
        self._max_fee_per_gas: int | None = None
        self._max_priority_fee: int | None = None
        self._chain_id: int = 1

    # ── Fluent setters ────────────────────────────────────────────────────────

    def to(self, address: Address) -> TransactionBuilder:
        """Set the recipient address."""
        if not isinstance(address, Address):
            raise TypeError(
                f"address must be an Address instance, got {type(address).__name__}. "
                "Use Address('0x...') to construct one."
            )
        self._to = address
        return self

    def value(self, amount: TokenAmount) -> TransactionBuilder:
        """Set the ETH value to send."""
        if not isinstance(amount, TokenAmount):
            raise TypeError(f"amount must be a TokenAmount instance, got {type(amount).__name__}.")
        if amount.decimals != 18:
            raise ValueError(
                f"ETH value must have 18 decimals, got {amount.decimals}. "
                "Use TokenAmount.from_human('0.1', 18) for ETH amounts."
            )
        self._value = amount
        return self

    def data(self, calldata: bytes) -> TransactionBuilder:
        """Set the calldata (ABI-encoded function call or b'' for plain transfer)."""
        if not isinstance(calldata, bytes):
            raise TypeError(f"calldata must be bytes, got {type(calldata).__name__}.")
        self._data = calldata
        return self

    def nonce(self, nonce: int) -> TransactionBuilder:
        """
        Set an explicit nonce.
        """
        if not isinstance(nonce, int) or nonce < 0:
            raise ValueError(f"nonce must be a non-negative int, got {nonce!r}.")
        self._nonce = nonce
        return self

    def gas_limit(self, limit: int) -> TransactionBuilder:
        """Set an explicit gas limit (overrides with_gas_estimate)."""
        if not isinstance(limit, int) or limit <= 0:
            raise ValueError(f"gas_limit must be a positive int, got {limit!r}.")
        self._gas_limit = limit
        return self

    def chain_id(self, chain_id: int) -> TransactionBuilder:
        """Set chain ID (default: 1 = Ethereum mainnet)."""
        self._chain_id = chain_id
        return self

    def with_gas_estimate(self, buffer: float = 1.2) -> TransactionBuilder:
        """
        Estimate gas via eth_estimateGas and apply a safety buffer.

        The buffer guards against gas estimation being slightly too low
        for transactions with variable-cost state reads.

        Args:
            buffer: Multiplier applied to the estimate (default 1.2 = 20 % headroom).

        Requires: to, value, and data must already be set.
        """
        if buffer <= 1.0:
            raise ValueError(
                f"buffer must be > 1.0 to add headroom, got {buffer}. " "Use 1.2 for 20 % headroom."
            )
        self._ensure_required_for_estimate()
        tx = self._build_partial()
        estimated = self._client.estimate_gas(tx)
        self._gas_limit = int(estimated * buffer)
        log.debug("Gas estimated: %d → with buffer: %d", estimated, self._gas_limit)
        return self

    def with_gas_price(self, priority: str = "medium") -> TransactionBuilder:
        """
        Fetch current network gas price and set EIP-1559 fee fields.

        Args:
            priority: "low" | "medium" | "high" — maps to priority fee tier.
        """
        gas_price = self._client.get_gas_price()
        self._max_fee_per_gas = gas_price.get_max_fee(priority)
        priority_map = {
            "low": gas_price.priority_fee_low,
            "medium": gas_price.priority_fee_medium,
            "high": gas_price.priority_fee_high,
        }
        if priority not in priority_map:
            raise ValueError(f"priority must be 'low', 'medium', or 'high', got {priority!r}.")
        self._max_priority_fee = priority_map[priority]
        log.debug(
            "Gas price: maxFee=%d, priorityFee=%d",
            self._max_fee_per_gas,
            self._max_priority_fee,
        )
        return self

    # ── Terminal methods ──────────────────────────────────────────────────────

    def build(self) -> TransactionRequest:
        """
        Validate all fields and return a TransactionRequest.

        Auto-fills nonce from chain if not explicitly set.

        Raises:
            ValueError: if required fields (to, value, data) are missing.
        """
        self._validate_required()
        nonce = self._nonce
        if nonce is None:
            nonce = self._client.get_nonce(Address(self._wallet.address))
            log.debug("Nonce fetched from chain: %d", nonce)

        return TransactionRequest(
            to=self._to,
            value=self._value,
            data=self._data,
            nonce=nonce,
            gas_limit=self._gas_limit,
            max_fee_per_gas=self._max_fee_per_gas,
            max_priority_fee=self._max_priority_fee,
            chain_id=self._chain_id,
        )

    def build_and_sign(self) -> SignedTransaction:
        """Build the transaction and sign it with the wallet's key."""
        tx = self.build()
        tx_dict = tx.to_dict()
        log.debug("Signing transaction to=%s nonce=%d", tx.to, tx.nonce)
        return self._wallet.sign_transaction(tx_dict)

    def send(self) -> str:
        """
        Build, sign, and broadcast the transaction.

        Returns:
            Transaction hash as a hex string.
        """
        signed = self.build_and_sign()
        raw = (
            signed.raw_transaction if hasattr(signed, "raw_transaction") else signed.rawTransaction
        )
        tx_hash = self._client.send_transaction(raw)
        log.info("Transaction sent: %s", tx_hash)
        return tx_hash

    def send_and_wait(self, timeout: int = 120) -> TransactionReceipt:
        """
        Build, sign, broadcast, and wait for confirmation.

        Args:
            timeout: Maximum seconds to wait for mining (default 120).

        Returns:
            Parsed TransactionReceipt.

        Raises:
            TransactionTimeout: if not mined within timeout.
            TransactionFailed:  if the transaction reverts.
        """
        tx_hash = self.send()
        log.info("Waiting for confirmation: %s (timeout=%ds)", tx_hash, timeout)
        return self._client.wait_for_receipt(tx_hash, timeout=timeout)

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _validate_required(self) -> None:
        """Raise ValueError listing all missing required fields."""
        missing = []
        if self._to is None:
            missing.append("to")
        if self._value is None:
            missing.append("value")
        if self._data is None:
            missing.append("data")
        if missing:
            raise ValueError(
                f"TransactionBuilder is missing required fields: {missing}. "
                "Call .to(), .value(), and .data() before .build()."
            )

    def _ensure_required_for_estimate(self) -> None:
        """Gas estimation needs to/value/data to construct the eth_estimateGas call."""
        missing = []
        if self._to is None:
            missing.append("to")
        if self._value is None:
            missing.append("value")
        if self._data is None:
            missing.append("data")
        if missing:
            raise ValueError(
                f"Cannot estimate gas — missing fields: {missing}. "
                "Call .to(), .value(), and .data() before .with_gas_estimate()."
            )

    def _build_partial(self) -> TransactionRequest:
        """Build a partial TransactionRequest for gas estimation (no nonce needed)."""
        return TransactionRequest(
            to=self._to,
            value=self._value,
            data=self._data,
            nonce=self._nonce or 0,
            gas_limit=self._gas_limit,
            max_fee_per_gas=self._max_fee_per_gas,
            max_priority_fee=self._max_priority_fee,
            chain_id=self._chain_id,
        )
