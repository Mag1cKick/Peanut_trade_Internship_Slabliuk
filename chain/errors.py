"""
chain/errors.py — Exception hierarchy for chain interactions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from core.types import TransactionReceipt


class ChainError(Exception):
    """Base class for all chain-related errors."""


class RPCError(ChainError):
    """
    RPC request failed — network error, invalid response, or node error.
    """

    def __init__(self, message: str, code: int | None = None) -> None:
        self.code = code
        super().__init__(message)


class TransactionFailed(ChainError):
    """
    Transaction was mined but reverted on-chain.
    """

    def __init__(self, tx_hash: str, receipt: TransactionReceipt) -> None:
        self.tx_hash = tx_hash
        self.receipt = receipt
        super().__init__(f"Transaction {tx_hash} reverted")


class TransactionTimeout(ChainError):
    """Transaction was not confirmed within the timeout period."""

    def __init__(self, tx_hash: str, timeout: int) -> None:
        self.tx_hash = tx_hash
        self.timeout = timeout
        super().__init__(f"Transaction {tx_hash} not confirmed after {timeout}s")


class InsufficientFunds(ChainError):
    """Account balance is too low to cover value + gas."""


class NonceTooLow(ChainError):
    """Nonce has already been used — likely a resubmit of a confirmed tx."""


class ReplacementUnderpriced(ChainError):
    """
    Replacement transaction was rejected because gas price is too low.
    Must be at least 10 % higher than the pending tx being replaced.
    """


class AllRPCsFailed(ChainError):
    """All configured RPC endpoints failed after retries."""

    def __init__(self, errors: list[Exception]) -> None:
        self.errors = errors
        summary = "; ".join(str(e) for e in errors)
        super().__init__(f"All RPC endpoints failed: {summary}")
