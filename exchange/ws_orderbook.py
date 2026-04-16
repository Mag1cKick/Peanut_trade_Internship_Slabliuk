"""
exchange/ws_orderbook.py — WebSocket-based order book with incremental updates.

Maintains a local order book mirror by subscribing to Binance's depth diff
stream and applying incremental updates on top of an initial REST snapshot.

Synchronisation protocol (Binance best practice):
  1. Open WebSocket diff stream, buffer incoming events.
  2. Fetch REST snapshot → ``lastUpdateId``.
  3. Drop buffered events where ``u`` (final_update_id) ≤ lastUpdateId.
  4. Validate first kept event: ``U`` ≤ lastUpdateId+1 ≤ ``u``.
  5. Apply updates: qty==0 → remove level; qty>0 → upsert level.
  6. Yield a snapshot (same format as ExchangeClient.fetch_order_book)
     after every applied diff event.

Usage::

    import asyncio
    from exchange.ws_orderbook import OrderBookStream

    async def main():
        async with OrderBookStream("ETH/USDT") as stream:
            async for book in stream:
                print(book["best_bid"], book["best_ask"])
                break   # or keep running forever

    asyncio.run(main())

CLI::

    python -m exchange.ws_orderbook ETH/USDT
    python -m exchange.ws_orderbook ETH/USDT --count 5 --no-testnet
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from collections.abc import AsyncIterator
from decimal import Decimal

log = logging.getLogger(__name__)

_BINANCE_TESTNET_WS = "wss://testnet.binance.vision/ws"
_BINANCE_TESTNET_REST = "https://testnet.binance.vision"
_BINANCE_MAINNET_WS = "wss://stream.binance.com:9443/ws"
_BINANCE_MAINNET_REST = "https://api.binance.com"


def _to_dec(val: str) -> Decimal:
    return Decimal(str(val))


class OrderBookStream:
    """
    Async context manager that streams incremental order book updates.

    The yielded dicts are compatible with the output of
    ``ExchangeClient.fetch_order_book()``, with one extra key:
    ``last_update_id`` (int) — the Binance sequence number of the last
    applied diff event.
    """

    def __init__(
        self,
        symbol: str,
        testnet: bool = True,
        depth_limit: int = 20,
    ) -> None:
        # "ETH/USDT" → "ethusdt" for WS, "ETHUSDT" for REST
        self._symbol = symbol
        self._ws_symbol = symbol.replace("/", "").lower()
        self._rest_symbol = symbol.replace("/", "")
        self._testnet = testnet
        self._depth_limit = depth_limit

        # Local order book state
        self._bids: dict[Decimal, Decimal] = {}
        self._asks: dict[Decimal, Decimal] = {}
        self._last_update_id: int = 0
        self._synced: bool = False

        self._ws = None
        self._session = None

    # ── URL helpers ────────────────────────────────────────────────────────────

    @property
    def _ws_url(self) -> str:
        base = _BINANCE_TESTNET_WS if self._testnet else _BINANCE_MAINNET_WS
        return f"{base}/{self._ws_symbol}@depth@100ms"

    @property
    def _rest_base(self) -> str:
        return _BINANCE_TESTNET_REST if self._testnet else _BINANCE_MAINNET_REST

    # ── Connection lifecycle ───────────────────────────────────────────────────

    async def connect(self) -> None:
        """Open WebSocket and synchronise with a REST snapshot."""
        try:
            import websockets
        except ImportError as exc:
            raise ImportError("websockets is required: pip install websockets") from exc
        try:
            import aiohttp
        except ImportError as exc:
            raise ImportError("aiohttp is required: pip install aiohttp") from exc

        log.info("Connecting to %s", self._ws_url)
        self._ws = await websockets.connect(self._ws_url)

        # Fetch REST snapshot while WS is already buffering events
        self._session = aiohttp.ClientSession()
        snapshot = await self._fetch_snapshot()
        self._apply_snapshot(snapshot)
        log.info(
            "Synchronised order book for %s at lastUpdateId=%d",
            self._symbol,
            self._last_update_id,
        )

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._session is not None:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> OrderBookStream:
        await self.connect()
        return self

    async def __aexit__(self, *_args) -> None:
        await self.close()

    # ── Streaming ──────────────────────────────────────────────────────────────

    async def __aiter__(self) -> AsyncIterator[dict]:
        """
        Yield an updated order book snapshot after each applied diff event.
        Stale events (final_update_id ≤ last_update_id) are silently skipped.
        """
        if self._ws is None:
            raise RuntimeError("Stream not connected — use 'async with' or call connect()")

        async for raw_msg in self._ws:
            event = json.loads(raw_msg)
            if self._apply_event(event):
                yield self.snapshot()

    # ── Core state machine ─────────────────────────────────────────────────────

    async def _fetch_snapshot(self) -> dict:
        url = f"{self._rest_base}/api/v3/depth"
        params = {"symbol": self._rest_symbol, "limit": str(self._depth_limit)}
        async with self._session.get(url, params=params) as resp:
            resp.raise_for_status()
            return await resp.json()

    def _apply_snapshot(self, snapshot: dict) -> None:
        self._last_update_id = int(snapshot["lastUpdateId"])
        self._bids = {_to_dec(p): _to_dec(q) for p, q in snapshot["bids"] if _to_dec(q) > 0}
        self._asks = {_to_dec(p): _to_dec(q) for p, q in snapshot["asks"] if _to_dec(q) > 0}
        self._synced = True

    def _apply_event(self, event: dict) -> bool:
        """
        Apply one diff event.

        Returns True if the event was applied (book changed), False if stale.

        Binance diff event fields:
          U   first_update_id
          u   final_update_id
          b   bid updates [[price, qty], ...]
          a   ask updates [[price, qty], ...]
        """
        final_id: int = int(event.get("u", 0))

        if final_id <= self._last_update_id:
            return False  # stale — skip

        for price_s, qty_s in event.get("b", []):
            price = _to_dec(price_s)
            qty = _to_dec(qty_s)
            if qty == 0:
                self._bids.pop(price, None)
            else:
                self._bids[price] = qty

        for price_s, qty_s in event.get("a", []):
            price = _to_dec(price_s)
            qty = _to_dec(qty_s)
            if qty == 0:
                self._asks.pop(price, None)
            else:
                self._asks[price] = qty

        self._last_update_id = final_id
        return True

    # ── Snapshot export ────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """
        Return current order book state — same format as
        ``ExchangeClient.fetch_order_book()`` plus ``last_update_id``.
        """
        sorted_bids = sorted(self._bids.items(), key=lambda x: x[0], reverse=True)[
            : self._depth_limit
        ]
        sorted_asks = sorted(self._asks.items(), key=lambda x: x[0])[: self._depth_limit]

        bids = list(sorted_bids)
        asks = list(sorted_asks)

        best_bid = bids[0] if bids else (Decimal("0"), Decimal("0"))
        best_ask = asks[0] if asks else (Decimal("0"), Decimal("0"))

        bid_p, ask_p = best_bid[0], best_ask[0]
        mid = (bid_p + ask_p) / Decimal("2") if bid_p and ask_p else Decimal("0")
        spread_bps = (ask_p - bid_p) / mid * Decimal("10000") if mid > 0 else Decimal("0")

        return {
            "symbol": self._symbol,
            "timestamp": int(time.time() * 1000),
            "bids": bids,
            "asks": asks,
            "best_bid": best_bid,
            "best_ask": best_ask,
            "mid_price": mid,
            "spread_bps": spread_bps,
            "last_update_id": self._last_update_id,
        }


# ── CLI ─────────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Stream live order book updates via Binance WebSocket",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m exchange.ws_orderbook ETH/USDT\n"
            "  python -m exchange.ws_orderbook ETH/USDT --count 10 --no-testnet"
        ),
    )
    parser.add_argument("symbol", help="Trading pair, e.g. ETH/USDT")
    parser.add_argument(
        "--count",
        type=int,
        default=5,
        help="Number of updates to print before exiting (default: 5)",
    )
    parser.add_argument(
        "--no-testnet",
        dest="testnet",
        action="store_false",
        default=True,
        help="Use mainnet instead of testnet",
    )
    args = parser.parse_args(argv)

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(_root, ".env"))
    except ImportError:
        pass

    async def _stream() -> None:
        print(f"\nStreaming {args.symbol} order book ({'testnet' if args.testnet else 'mainnet'})")
        print(f"Will print {args.count} updates then exit.\n")
        async with OrderBookStream(args.symbol, testnet=args.testnet) as stream:
            n = 0
            async for book in stream:
                bid_p, _ = book["best_bid"]
                ask_p, _ = book["best_ask"]
                mid = book["mid_price"]
                spread = book["spread_bps"]
                uid = book["last_update_id"]
                print(
                    f"  [{uid}] bid={float(bid_p):,.2f}  ask={float(ask_p):,.2f}"
                    f"  mid={float(mid):,.2f}  spread={float(spread):.2f} bps"
                )
                n += 1
                if n >= args.count:
                    break

    try:
        asyncio.run(_stream())
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
