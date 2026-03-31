"""
chain/client.py — Ethereum RPC client with reliability features.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from web3 import Web3
from web3.exceptions import ContractLogicError

from chain.errors import (
    AllRPCsFailed,
    ChainError,
    InsufficientFunds,
    NonceTooLow,
    ReplacementUnderpriced,
    RPCError,
    TransactionFailed,
    TransactionTimeout,
)
from core.types import Address, TokenAmount, TransactionReceipt, TransactionRequest

log = logging.getLogger(__name__)

_RPC_ERROR_MAP: list[tuple[str, type[ChainError]]] = [
    ("insufficient funds", InsufficientFunds),
    ("nonce too low", NonceTooLow),
    ("replacement transaction underpriced", ReplacementUnderpriced),
    ("already known", ReplacementUnderpriced),
    ("transaction underpriced", ReplacementUnderpriced),
]


def _classify_rpc_error(message: str, code: int | None = None) -> ChainError:
    """Map a raw RPC error message to the most specific ChainError subclass."""
    lower = message.lower()
    for pattern, exc_class in _RPC_ERROR_MAP:
        if pattern in lower:
            return exc_class(message)
    return RPCError(message, code=code)


@dataclass
class GasPrice:
    """
    Current network gas price snapshot.
    """

    base_fee: int
    priority_fee_low: int
    priority_fee_medium: int
    priority_fee_high: int

    def get_max_fee(self, priority: str = "medium", buffer: float = 1.2) -> int:
        """
        Calculate maxFeePerGas = (base_fee * buffer) + priority_fee.
        """
        priority_map = {
            "low": self.priority_fee_low,
            "medium": self.priority_fee_medium,
            "high": self.priority_fee_high,
        }
        if priority not in priority_map:
            raise ValueError(f"priority must be one of {list(priority_map)}, got {priority!r}")
        buffered_base = int(self.base_fee * buffer)
        return buffered_base + priority_map[priority]

    @property
    def gwei_base_fee(self) -> float:
        """Base fee in gwei for display."""
        return self.base_fee / 1e9


class ChainClient:
    """
    Ethereum RPC client with automatic retry and endpoint fallback.
    """

    def __init__(
        self,
        rpc_urls: list[str],
        timeout: int = 30,
        max_retries: int = 3,
    ) -> None:
        if not rpc_urls:
            raise ValueError("At least one RPC URL must be provided.")
        self._rpc_urls = rpc_urls
        self._timeout = timeout
        self._max_retries = max_retries
        self._web3_instances: list[Web3] = [
            Web3(Web3.HTTPProvider(url, request_kwargs={"timeout": timeout})) for url in rpc_urls
        ]

    def _call_with_retry(self, method_name: str, *args, **kwargs):
        """
        Try each RPC endpoint in order, retrying with exponential backoff.
        """
        all_errors: list[Exception] = []

        for i, w3 in enumerate(self._web3_instances):
            url = self._rpc_urls[i]
            for attempt in range(self._max_retries):
                try:
                    t0 = time.monotonic()
                    result = self._dispatch(w3, method_name, *args, **kwargs)
                    elapsed = time.monotonic() - t0
                    log.debug(
                        "%s via %s attempt %d: %.3fs",
                        method_name,
                        url,
                        attempt + 1,
                        elapsed,
                    )
                    return result
                except ChainError:
                    raise
                except Exception as exc:
                    wait = 2**attempt
                    log.warning(
                        "%s via %s attempt %d failed: %s — retrying in %ds",
                        method_name,
                        url,
                        attempt + 1,
                        exc,
                        wait,
                    )
                    all_errors.append(exc)
                    if attempt < self._max_retries - 1:
                        time.sleep(wait)

        raise AllRPCsFailed(all_errors)

    def _dispatch(self, w3: Web3, method_name: str, *args, **kwargs):
        """Route method_name to the correct web3.py call."""
        eth = w3.eth
        try:
            match method_name:
                case "get_balance":
                    return eth.get_balance(args[0])
                case "get_transaction_count":
                    return eth.get_transaction_count(args[0], args[1])
                case "get_block":
                    return eth.get_block(args[0])
                case "fee_history":
                    return eth.fee_history(args[0], args[1], args[2])
                case "estimate_gas":
                    return eth.estimate_gas(args[0])
                case "send_raw_transaction":
                    return eth.send_raw_transaction(args[0])
                case "get_transaction_receipt":
                    return eth.get_transaction_receipt(args[0])
                case "get_transaction":
                    return eth.get_transaction(args[0])
                case "call":
                    return eth.call(args[0], args[1])
                case _:
                    raise ValueError(f"Unknown method: {method_name}")
        except ContractLogicError as exc:
            raise RPCError(str(exc)) from exc
        except Exception as exc:
            msg = str(exc)
            code = None
            if hasattr(exc, "args") and exc.args:
                if isinstance(exc.args[0], dict):
                    code = exc.args[0].get("code")
                    msg = exc.args[0].get("message", msg)
            raise _classify_rpc_error(msg, code) from exc

    def get_balance(self, address: Address) -> TokenAmount:
        """Return ETH balance as a TokenAmount (18 decimals)."""
        raw = self._call_with_retry("get_balance", address.checksum)
        return TokenAmount(raw=raw, decimals=18, symbol="ETH")

    def get_nonce(self, address: Address, block: str = "pending") -> int:
        """Return the next usable nonce for address."""
        return self._call_with_retry("get_transaction_count", address.checksum, block)

    def get_gas_price(self) -> GasPrice:
        """
        Return current gas price info using EIP-1559 fee history.
        """
        try:
            block = self._call_with_retry("get_block", "latest")
            base_fee = block.get("baseFeePerGas", 0)

            history = self._call_with_retry("fee_history", 5, "latest", [10, 50, 90])
            rewards = history.get("reward", [[0, 0, 0]] * 5)

            def avg_percentile(idx: int) -> int:
                vals = [r[idx] for r in rewards if r]
                return int(sum(vals) / len(vals)) if vals else 1_000_000_000

            return GasPrice(
                base_fee=base_fee,
                priority_fee_low=avg_percentile(0),
                priority_fee_medium=avg_percentile(1),
                priority_fee_high=avg_percentile(2),
            )
        except Exception:
            block = self._call_with_retry("get_block", "latest")
            gas_price = block.get("gasPrice", 20_000_000_000)
            return GasPrice(
                base_fee=gas_price,
                priority_fee_low=1_000_000_000,
                priority_fee_medium=2_000_000_000,
                priority_fee_high=3_000_000_000,
            )

    def estimate_gas(self, tx: TransactionRequest) -> int:
        """Estimate gas for a transaction. Raises RPCError on revert."""
        tx_dict = tx.to_dict()
        return self._call_with_retry("estimate_gas", tx_dict)

    def send_transaction(self, signed_tx: bytes) -> str:
        """
        Broadcast a signed transaction. Returns the tx hash immediately.
        """
        tx_hash = self._call_with_retry("send_raw_transaction", signed_tx)
        if hasattr(tx_hash, "hex"):
            return tx_hash.hex()
        return str(tx_hash)

    def wait_for_receipt(
        self,
        tx_hash: str,
        timeout: int = 120,
        poll_interval: float = 1.0,
    ) -> TransactionReceipt:
        """
        Poll until a transaction is confirmed, then return the receipt.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            receipt = self.get_receipt(tx_hash)
            if receipt is not None:
                if not receipt.status:
                    raise TransactionFailed(tx_hash, receipt)
                return receipt
            time.sleep(poll_interval)

        raise TransactionTimeout(tx_hash, timeout)

    def get_transaction(self, tx_hash: str) -> dict:
        """Return raw transaction dict (may be None if pending/unknown)."""
        result = self._call_with_retry("get_transaction", tx_hash)
        if result is None:
            raise RPCError(f"Transaction {tx_hash} not found")
        return dict(result)

    def get_receipt(self, tx_hash: str) -> TransactionReceipt | None:
        """
        Return parsed receipt or None if transaction is still pending.
        """
        raw = self._call_with_retry("get_transaction_receipt", tx_hash)
        if raw is None:
            return None
        return TransactionReceipt.from_web3(dict(raw))

    def call(self, tx: TransactionRequest, block: str = "latest") -> bytes:
        """
        Simulate a transaction via eth_call without broadcasting.
        Useful for reading contract state or checking for reverts.
        """
        tx_dict = tx.to_dict()
        result = self._call_with_retry("call", tx_dict, block)
        return bytes(result)
