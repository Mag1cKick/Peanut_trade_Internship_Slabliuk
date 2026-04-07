"""
pricing/mempool.py — Mempool monitor for pending swap transactions.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from eth_abi import decode as abi_decode
from web3 import AsyncWeb3

from core.types import Address

log = logging.getLogger(__name__)

# ── Selector registry ──────────────────────────────────────────────────────────

_SWAP_SELECTORS: dict[str, tuple[str, str]] = {
    "0x38ed1739": ("UniswapV2", "swapExactTokensForTokens"),
    "0x7ff36ab5": ("UniswapV2", "swapExactETHForTokens"),
    "0x18cbafe5": ("UniswapV2", "swapExactTokensForETH"),
    "0x5ae401dc": ("UniswapV3", "multicall"),
}

# ABI parameter types for each selector (calldata without the 4-byte prefix)
_ABI_SIGNATURES: dict[str, tuple[str, ...]] = {
    "0x38ed1739": ("uint256", "uint256", "address[]", "address", "uint256"),
    "0x7ff36ab5": ("uint256", "address[]", "address", "uint256"),
    "0x18cbafe5": ("uint256", "uint256", "address[]", "address", "uint256"),
    "0x5ae401dc": ("uint256", "bytes[]"),
}


# ── ParsedSwap ─────────────────────────────────────────────────────────────────


@dataclass
class ParsedSwap:
    """
    Parsed swap transaction extracted from the mempool.

    token_in / token_out are None when the swap involves native ETH.
    expected_amount_out must be populated externally (e.g. via AMM query)
    before calling slippage_tolerance.
    """

    tx_hash: str
    router: str
    dex: str
    method: str
    token_in: Address | None
    token_out: Address | None
    amount_in: int
    min_amount_out: int
    deadline: int
    sender: Address
    gas_price: int
    expected_amount_out: int | None = field(default=None)

    @property
    def slippage_tolerance(self) -> Decimal:
        """
        Implied slippage tolerance: (expected_out - min_out) / expected_out.

        Populate expected_amount_out first by querying the relevant AMM pool.

        Raises:
            ValueError: If expected_amount_out is not set or is zero.
        """
        if self.expected_amount_out is None:
            raise ValueError(
                "expected_amount_out must be set before computing slippage_tolerance. "
                "Query the AMM to obtain the expected output first."
            )
        if self.expected_amount_out == 0:
            raise ValueError("expected_amount_out must be non-zero.")
        diff = self.expected_amount_out - self.min_amount_out
        return Decimal(diff) / Decimal(self.expected_amount_out)


# ── MempoolMonitor ─────────────────────────────────────────────────────────────


class MempoolMonitor:
    """
    Monitors pending transactions for swap activity via WebSocket.

    Args:
        ws_url:   WebSocket RPC endpoint (wss://...).
        callback: Invoked with each ParsedSwap detected.
    """

    SWAP_SELECTORS = _SWAP_SELECTORS

    def __init__(self, ws_url: str, callback: Callable[[ParsedSwap], None]) -> None:
        self.ws_url = ws_url
        self.callback = callback

    async def start(self) -> None:
        """
        Connect to WebSocket and stream pending transactions.

        Subscribes to newPendingTransactions, fetches each transaction in a
        non-blocking asyncio task, parses it, and calls self.callback when a
        recognised swap is found.  Runs until the coroutine is cancelled.
        """
        async with AsyncWeb3(AsyncWeb3.WebSocketProvider(self.ws_url)) as w3:
            await w3.eth.subscribe("newPendingTransactions")
            async for message in w3.socket.process_subscriptions():
                result = message.get("result") or (message.get("params", {}).get("result"))
                if result is None:
                    continue
                if isinstance(result, bytes):
                    tx_hash = "0x" + result.hex()
                else:
                    tx_hash = str(result)
                asyncio.create_task(self._fetch_and_process(w3, tx_hash))

    async def _fetch_and_process(self, w3: AsyncWeb3, tx_hash: str) -> None:
        """Fetch the full transaction and invoke callback if it is a swap."""
        try:
            tx = await w3.eth.get_transaction(tx_hash)
            if tx is None:
                return
            parsed = self.parse_transaction(dict(tx))
            if parsed is not None:
                self.callback(parsed)
        except Exception as exc:
            log.debug("Failed to process tx %s: %s", tx_hash, exc)

    def parse_transaction(self, tx: dict) -> ParsedSwap | None:
        """
        Parse a raw transaction dict into a ParsedSwap.

        Returns None if the transaction is not a recognised swap.

        Args:
            tx: Transaction dict as returned by eth_getTransactionByHash
                (may contain 'input' or 'data' for calldata).
        """
        raw_input: str | bytes = tx.get("input") or tx.get("data") or b""

        if isinstance(raw_input, bytes):
            if len(raw_input) < 4:
                return None
            selector = "0x" + raw_input[:4].hex()
            calldata = raw_input[4:]
        else:
            if len(raw_input) < 10:
                return None
            selector = raw_input[:10].lower()
            try:
                calldata = bytes.fromhex(raw_input[10:])
            except ValueError:
                return None

        if selector not in self.SWAP_SELECTORS:
            return None

        dex, method = self.SWAP_SELECTORS[selector]

        try:
            params = self.decode_swap_params(selector, calldata)
        except Exception as exc:
            log.debug("decode_swap_params failed for %s: %s", selector, exc)
            return None

        # For ETH-in swaps the actual input amount lives in tx.value
        if selector == "0x7ff36ab5":
            params["amount_in"] = tx.get("value", 0)

        tx_hash = tx.get("hash", "")
        if isinstance(tx_hash, bytes):
            tx_hash = "0x" + tx_hash.hex()

        sender_raw = tx.get("from") or "0x0000000000000000000000000000000000000000"
        gas_price = tx.get("gasPrice") or tx.get("maxFeePerGas") or 0

        token_in_raw = params.get("token_in")
        token_out_raw = params.get("token_out")

        try:
            return ParsedSwap(
                tx_hash=str(tx_hash),
                router=str(tx.get("to") or ""),
                dex=dex,
                method=method,
                token_in=Address(token_in_raw) if token_in_raw else None,
                token_out=Address(token_out_raw) if token_out_raw else None,
                amount_in=params.get("amount_in", 0),
                min_amount_out=params.get("min_amount_out", 0),
                deadline=params.get("deadline", 0),
                sender=Address(sender_raw),
                gas_price=gas_price,
            )
        except Exception as exc:
            log.debug("Failed to build ParsedSwap: %s", exc)
            return None

    def decode_swap_params(self, selector: str, data: bytes) -> dict:
        """
        ABI-decode swap calldata (the bytes *after* the 4-byte selector).

        Returns a dict with keys:
            amount_in, min_amount_out, token_in, token_out, deadline.
        token_in / token_out are raw checksum address strings or None.

        Raises:
            ValueError: For unsupported selectors.
            Exception:  If ABI decoding fails (malformed calldata).
        """
        types = _ABI_SIGNATURES.get(selector)
        if types is None:
            raise ValueError(f"Unsupported selector: {selector!r}")

        decoded = abi_decode(types, data)

        if selector == "0x38ed1739":  # swapExactTokensForTokens
            amount_in, min_amount_out, path, _to, deadline = decoded
            return {
                "amount_in": amount_in,
                "min_amount_out": min_amount_out,
                "token_in": path[0] if path else None,
                "token_out": path[-1] if path else None,
                "deadline": deadline,
            }

        if selector == "0x7ff36ab5":  # swapExactETHForTokens (ETH in)
            min_amount_out, path, _to, deadline = decoded
            return {
                "amount_in": 0,  # overridden in parse_transaction with tx.value
                "min_amount_out": min_amount_out,
                "token_in": None,  # native ETH has no ERC-20 address
                "token_out": path[-1] if path else None,
                "deadline": deadline,
            }

        if selector == "0x18cbafe5":  # swapExactTokensForETH
            amount_in, min_amount_out, path, _to, deadline = decoded
            return {
                "amount_in": amount_in,
                "min_amount_out": min_amount_out,
                "token_in": path[0] if path else None,
                "token_out": None,  # native ETH out
                "deadline": deadline,
            }

        if selector == "0x5ae401dc":  # UniswapV3 multicall
            deadline, _inner_data = decoded
            return {
                "amount_in": 0,
                "min_amount_out": 0,
                "token_in": None,
                "token_out": None,
                "deadline": deadline,
            }

        raise ValueError(f"Unhandled selector: {selector!r}")
