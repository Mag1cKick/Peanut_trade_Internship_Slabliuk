"""
pricing/price_feed.py — Real-time price feed via WebSocket block subscriptions.

Subscribes to ``newHeads`` (new block headers) over a WebSocket connection.
On each new block, it fetches the current reserves for all registered pools
via ``eth_call`` (getReserves), recomputes spot prices, and fires a callback
with a ``PriceUpdate`` dataclass.

Usage::

    feed = PriceFeed(ws_url="wss://...", pools=[pair1, pair2], on_update=my_cb)
    await feed.start()
    # ... later:
    await feed.stop()
    latest = feed.get_latest(pair1.address)
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

from web3 import AsyncWeb3

from core.types import Address
from pricing.amm import UniswapV2Pair

log = logging.getLogger(__name__)

# getReserves() selector + ABI for raw eth_call
_GET_RESERVES_SELECTOR = bytes.fromhex("0902f1ac")


# ── PriceUpdate ───────────────────────────────────────────────────────────────


@dataclass
class PriceUpdate:
    """
    A snapshot of a pool's price at a particular block.

    Attributes:
        pool_address: On-chain address of the pair contract.
        block_number: Block at which this snapshot was taken.
        reserve0:     token0 reserve (raw uint112).
        reserve1:     token1 reserve (raw uint112).
        spot_price_0: Spot price expressed as token1 per token0.
        spot_price_1: Spot price expressed as token0 per token1.
        timestamp:    Wall-clock time when the update was received.
    """

    pool_address: Address
    block_number: int
    reserve0: int
    reserve1: int
    spot_price_0: Decimal  # token1 / token0
    spot_price_1: Decimal  # token0 / token1
    timestamp: float = field(default_factory=time.time)

    @property
    def price_changed(self) -> bool:
        """Always True in this dataclass (comparison requires prior snapshot)."""
        return True


# ── PriceFeed ─────────────────────────────────────────────────────────────────


class PriceFeed:
    """
    Real-time price feed that listens for new Ethereum blocks over WebSocket
    and refreshes pool prices on every block.

    Args:
        ws_url:    WebSocket endpoint (e.g. ``wss://mainnet.infura.io/ws/v3/...``).
        pools:     List of UniswapV2Pair objects to track.
        on_update: Callback invoked with each ``PriceUpdate``.  May be a
                   plain function or an async coroutine function.
    """

    def __init__(
        self,
        ws_url: str,
        pools: list[UniswapV2Pair],
        on_update: Callable[[PriceUpdate], None],
    ) -> None:
        self._ws_url = ws_url
        self.pools = pools
        self._on_update = on_update
        self._latest: dict[Address, PriceUpdate] = {}
        self._task: asyncio.Task | None = None
        self._running = False

    # ── public API ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        """
        Connect to the WebSocket endpoint and begin listening for new blocks.

        Spawns a background asyncio task; returns immediately.
        """
        self._running = True
        self._task = asyncio.create_task(self._listen())
        log.info("PriceFeed started for %d pool(s).", len(self.pools))

    async def stop(self) -> None:
        """
        Stop the feed and cancel the background task.
        """
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        log.info("PriceFeed stopped.")

    def get_latest(self, pool_address: Address) -> PriceUpdate | None:
        """
        Return the most recent PriceUpdate for *pool_address*, or None if no
        update has been received yet.

        Args:
            pool_address: Address of the pool to query.
        """
        return self._latest.get(pool_address)

    def get_all_latest(self) -> dict[Address, PriceUpdate]:
        """Return a snapshot of all latest price updates, keyed by pool address."""
        return dict(self._latest)

    # ── internal ──────────────────────────────────────────────────────────────

    async def _listen(self) -> None:
        """Main loop: subscribe to newHeads, fetch prices on each block."""
        try:
            async with AsyncWeb3(AsyncWeb3.AsyncWebsocketProvider(self._ws_url)) as w3:
                subscription_id = await w3.eth.subscribe("newHeads")
                log.debug("Subscribed to newHeads: %s", subscription_id)
                async for header in w3.socket.process_subscriptions():
                    if not self._running:
                        break
                    block_number: int = int(header["result"].get("number", 0), 16)
                    await self._refresh_all(w3, block_number)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.error("PriceFeed error: %s", exc, exc_info=True)

    async def _refresh_all(self, w3: AsyncWeb3, block_number: int) -> None:
        """Fetch reserves for all tracked pools and emit PriceUpdate callbacks."""
        for pair in self.pools:
            try:
                r0, r1 = await self._get_reserves(w3, pair.address)
                if r0 <= 0 or r1 <= 0:
                    continue
                spot0 = Decimal(r1) / Decimal(r0)
                spot1 = Decimal(r0) / Decimal(r1)
                update = PriceUpdate(
                    pool_address=pair.address,
                    block_number=block_number,
                    reserve0=r0,
                    reserve1=r1,
                    spot_price_0=spot0,
                    spot_price_1=spot1,
                )
                self._latest[pair.address] = update
                if asyncio.iscoroutinefunction(self._on_update):
                    await self._on_update(update)
                else:
                    self._on_update(update)
            except Exception as exc:
                log.warning("Failed to refresh pool %s: %s", pair.address, exc)

    async def _get_reserves(self, w3: AsyncWeb3, pair_address: Address) -> tuple[int, int]:
        """
        Call ``getReserves()`` on a pair contract and return (reserve0, reserve1).

        Uses raw ``eth_call`` with the 4-byte selector to avoid ABI overhead.
        Result layout: [reserve0 uint112 (32B)] [reserve1 uint112 (32B)] [ts uint32 (32B)]
        """
        checksum = AsyncWeb3.to_checksum_address(pair_address.checksum)
        result: bytes = await w3.eth.call(
            {"to": checksum, "data": "0x" + _GET_RESERVES_SELECTOR.hex()},
        )
        # Each value is ABI-encoded as a 32-byte word
        reserve0 = int.from_bytes(result[0:32], "big")
        reserve1 = int.from_bytes(result[32:64], "big")
        return reserve0, reserve1
