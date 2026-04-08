"""
tests/test_price_feed.py — Unit tests for pricing.price_feed.PriceFeed

No live WebSocket connection required — all async WebSocket behaviour is
mocked via AsyncMock / MagicMock.

Test groups:
  1. PriceUpdate — dataclass construction and properties
  2. PriceFeed.get_latest — returns None before any update, value after
  3. PriceFeed.get_all_latest — returns dict of all pools
  4. PriceFeed._get_reserves — raw eth_call decoding
  5. PriceFeed._refresh_all — invokes callback with correct PriceUpdate
  6. PriceFeed._refresh_all — handles pool errors gracefully
  7. PriceFeed.start / stop — task lifecycle
  8. PriceFeed callback — async callback is awaited, sync callback is called
"""

from __future__ import annotations

import asyncio
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.price_feed import PriceFeed, PriceUpdate

# ── Shared fixtures ───────────────────────────────────────────────────────────

WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)
USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6
)

PAIR_ADDR = Address("0x0000000000000000000000000000000000000001")
USDC_ADDR = Address("0x0000000000000000000000000000000000000002")

R0 = 10**18
R1 = 2000 * 10**18


def _pair(addr=PAIR_ADDR, t0=WETH, t1=DAI, r0=R0, r1=R1) -> UniswapV2Pair:
    return UniswapV2Pair(address=addr, token0=t0, token1=t1, reserve0=r0, reserve1=r1)


def _encode_reserves(r0: int, r1: int) -> bytes:
    """ABI-encode two uint112 values as 32-byte words (like getReserves returns)."""
    return r0.to_bytes(32, "big") + r1.to_bytes(32, "big") + b"\x00" * 32


# ── 1. PriceUpdate ────────────────────────────────────────────────────────────


class TestPriceUpdate:
    def test_construction(self):
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=100,
            reserve0=R0,
            reserve1=R1,
            spot_price_0=Decimal(R1) / Decimal(R0),
            spot_price_1=Decimal(R0) / Decimal(R1),
        )
        assert upd.pool_address == PAIR_ADDR
        assert upd.block_number == 100
        assert upd.reserve0 == R0
        assert upd.reserve1 == R1

    def test_spot_price_0_is_token1_per_token0(self):
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            spot_price_0=Decimal(2000),
            spot_price_1=Decimal("0.0005"),
        )
        assert upd.spot_price_0 == Decimal(2000)

    def test_spot_prices_are_inverse(self):
        r0, r1 = 10**18, 2000 * 10**18
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=r0,
            reserve1=r1,
            spot_price_0=Decimal(r1) / Decimal(r0),
            spot_price_1=Decimal(r0) / Decimal(r1),
        )
        assert pytest.approx(float(upd.spot_price_0 * upd.spot_price_1), rel=1e-9) == 1.0

    def test_timestamp_auto_populated(self):
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=R0,
            reserve1=R1,
            spot_price_0=Decimal(2000),
            spot_price_1=Decimal("0.0005"),
        )
        assert upd.timestamp > 0

    def test_price_changed_always_true(self):
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=R0,
            reserve1=R1,
            spot_price_0=Decimal(1),
            spot_price_1=Decimal(1),
        )
        assert upd.price_changed is True


# ── 2. PriceFeed.get_latest ───────────────────────────────────────────────────


class TestGetLatest:
    def test_returns_none_before_any_update(self):
        feed = PriceFeed(ws_url="wss://x", pools=[_pair()], on_update=lambda u: None)
        assert feed.get_latest(PAIR_ADDR) is None

    def test_returns_update_after_manual_inject(self):
        feed = PriceFeed(ws_url="wss://x", pools=[_pair()], on_update=lambda u: None)
        upd = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=R0,
            reserve1=R1,
            spot_price_0=Decimal(2000),
            spot_price_1=Decimal("0.0005"),
        )
        feed._latest[PAIR_ADDR] = upd
        assert feed.get_latest(PAIR_ADDR) is upd

    def test_unknown_address_returns_none(self):
        feed = PriceFeed(ws_url="wss://x", pools=[_pair()], on_update=lambda u: None)
        other = Address("0x000000000000000000000000000000000000dead")
        assert feed.get_latest(other) is None


# ── 3. PriceFeed.get_all_latest ───────────────────────────────────────────────


class TestGetAllLatest:
    def test_returns_empty_dict_initially(self):
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
        assert feed.get_all_latest() == {}

    def test_returns_all_injected_updates(self):
        pair_a = _pair(PAIR_ADDR, WETH, DAI)
        pair_b = _pair(USDC_ADDR, WETH, USDC, r0=10**18, r1=2000 * 10**6)
        feed = PriceFeed(ws_url="wss://x", pools=[pair_a, pair_b], on_update=lambda u: None)
        upd_a = PriceUpdate(
            pool_address=PAIR_ADDR,
            block_number=1,
            reserve0=R0,
            reserve1=R1,
            spot_price_0=Decimal(2000),
            spot_price_1=Decimal("0.0005"),
        )
        upd_b = PriceUpdate(
            pool_address=USDC_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**6,
            spot_price_0=Decimal("0.000002"),
            spot_price_1=Decimal(500000),
        )
        feed._latest[PAIR_ADDR] = upd_a
        feed._latest[USDC_ADDR] = upd_b
        result = feed.get_all_latest()
        assert PAIR_ADDR in result
        assert USDC_ADDR in result

    def test_returns_copy(self):
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
        result = feed.get_all_latest()
        result["injected"] = "something"
        assert "injected" not in feed._latest


# ── 4. PriceFeed._get_reserves ────────────────────────────────────────────────


class TestGetReserves:
    @pytest.mark.asyncio
    async def test_decodes_reserves_correctly(self):
        encoded = _encode_reserves(R0, R1)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=encoded)

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
            r0, r1 = await feed._get_reserves(mock_w3, PAIR_ADDR)

        assert r0 == R0
        assert r1 == R1

    @pytest.mark.asyncio
    async def test_get_reserves_passes_correct_selector(self):
        encoded = _encode_reserves(1, 2)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=encoded)

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
            await feed._get_reserves(mock_w3, PAIR_ADDR)

        call_args = mock_w3.eth.call.call_args[0][0]
        assert call_args["data"] == "0x0902f1ac"


# ── 5. PriceFeed._refresh_all — callback invocation ──────────────────────────


class TestRefreshAll:
    @pytest.mark.asyncio
    async def test_sync_callback_called_with_price_update(self):
        pair = _pair()
        received: list[PriceUpdate] = []

        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=received.append)

        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(R0, R1))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=42)

        assert len(received) == 1
        upd = received[0]
        assert upd.pool_address == PAIR_ADDR
        assert upd.block_number == 42
        assert upd.reserve0 == R0
        assert upd.reserve1 == R1

    @pytest.mark.asyncio
    async def test_spot_prices_computed_correctly(self):
        pair = _pair()
        received: list[PriceUpdate] = []

        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=received.append)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(R0, R1))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=1)

        upd = received[0]
        assert pytest.approx(float(upd.spot_price_0), rel=1e-9) == R1 / R0
        assert pytest.approx(float(upd.spot_price_1), rel=1e-9) == R0 / R1

    @pytest.mark.asyncio
    async def test_latest_dict_updated(self):
        pair = _pair()
        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=lambda u: None)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(R0, R1))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=5)

        assert feed.get_latest(PAIR_ADDR) is not None
        assert feed.get_latest(PAIR_ADDR).block_number == 5

    @pytest.mark.asyncio
    async def test_multiple_pools_all_updated(self):
        pair_a = _pair(PAIR_ADDR, WETH, DAI)
        pair_b = _pair(USDC_ADDR, WETH, USDC, r0=10**18, r1=2000 * 10**6)
        received: list[PriceUpdate] = []

        feed = PriceFeed(ws_url="wss://x", pools=[pair_a, pair_b], on_update=received.append)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(R0, R1))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=10)

        assert len(received) == 2


# ── 6. _refresh_all — error handling ─────────────────────────────────────────


class TestRefreshAllErrorHandling:
    @pytest.mark.asyncio
    async def test_failed_pool_does_not_stop_others(self):
        pair_a = _pair(PAIR_ADDR, WETH, DAI)
        pair_b = _pair(USDC_ADDR, WETH, USDC, r0=10**18, r1=2000 * 10**6)
        received: list[PriceUpdate] = []

        feed = PriceFeed(ws_url="wss://x", pools=[pair_a, pair_b], on_update=received.append)
        mock_w3 = AsyncMock()

        call_count = 0

        async def side_effect(tx):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("RPC error")
            return _encode_reserves(R0, R1)

        mock_w3.eth.call = AsyncMock(side_effect=side_effect)

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=1)

        # Second pool should still succeed
        assert len(received) == 1

    @pytest.mark.asyncio
    async def test_zero_reserves_skipped(self):
        pair = _pair()
        received: list[PriceUpdate] = []

        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=received.append)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(0, 0))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=1)

        # Zero reserves should be silently skipped
        assert len(received) == 0


# ── 7. start / stop lifecycle ─────────────────────────────────────────────────


class TestStartStop:
    @pytest.mark.asyncio
    async def test_start_creates_task(self):
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)

        async def fake_listen():
            await asyncio.sleep(10)

        feed._listen = fake_listen
        await feed.start()
        assert feed._task is not None
        assert not feed._task.done()
        await feed.stop()

    @pytest.mark.asyncio
    async def test_stop_cancels_task(self):
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)

        async def fake_listen():
            await asyncio.sleep(10)

        feed._listen = fake_listen
        await feed.start()
        await feed.stop()
        assert feed._task is None

    @pytest.mark.asyncio
    async def test_stop_before_start_is_safe(self):
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
        await feed.stop()  # Should not raise


# ── 8. Async callback ─────────────────────────────────────────────────────────


class TestAsyncCallback:
    @pytest.mark.asyncio
    async def test_async_callback_is_awaited(self):
        pair = _pair()
        received: list[PriceUpdate] = []

        async def async_cb(upd: PriceUpdate) -> None:
            received.append(upd)

        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=async_cb)
        mock_w3 = AsyncMock()
        mock_w3.eth.call = AsyncMock(return_value=_encode_reserves(R0, R1))

        with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
            await feed._refresh_all(mock_w3, block_number=7)

        assert len(received) == 1
        assert received[0].block_number == 7


# ── 9. _listen method ─────────────────────────────────────────────────────────


def _build_mock_ws(headers, call_return=None):
    """Return a mock AsyncWeb3 context-manager that yields *headers*."""

    async def _subscriptions():
        for h in headers:
            yield h

    mock_w3 = AsyncMock()
    mock_w3.eth.subscribe = AsyncMock(return_value="sub-id")
    mock_w3.socket.process_subscriptions = _subscriptions
    if call_return is not None:
        mock_w3.eth.call = AsyncMock(return_value=call_return)
    mock_w3.__aenter__ = AsyncMock(return_value=mock_w3)
    mock_w3.__aexit__ = AsyncMock(return_value=None)

    mock_cls = MagicMock()
    mock_cls.return_value = mock_w3
    mock_cls.AsyncWebsocketProvider = MagicMock()
    return mock_cls, mock_w3


class TestListen:
    @pytest.mark.asyncio
    async def test_listen_calls_refresh_on_each_block(self):
        """Each newHeads header triggers _refresh_all with the parsed block number."""
        pair = _pair()
        received: list[PriceUpdate] = []
        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=received.append)
        feed._running = True

        headers = [
            {"result": {"number": "0x64"}},  # block 100
            {"result": {"number": "0x65"}},  # block 101
        ]
        mock_cls, _ = _build_mock_ws(headers, call_return=_encode_reserves(R0, R1))

        with patch("pricing.price_feed.AsyncWeb3", mock_cls):
            with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
                await feed._listen()

        assert len(received) == 2
        assert received[0].block_number == 100
        assert received[1].block_number == 101

    @pytest.mark.asyncio
    async def test_listen_stops_when_running_false(self):
        """If _running is False on entry, loop body breaks immediately."""
        pair = _pair()
        received: list[PriceUpdate] = []
        feed = PriceFeed(ws_url="wss://x", pools=[pair], on_update=received.append)
        feed._running = False  # stopped before listen

        headers = [{"result": {"number": "0x1"}}]
        mock_cls, _ = _build_mock_ws(headers, call_return=_encode_reserves(R0, R1))

        with patch("pricing.price_feed.AsyncWeb3", mock_cls):
            with patch("web3.AsyncWeb3.to_checksum_address", side_effect=lambda x: x):
                await feed._listen()

        # Loop entered but broke immediately before _refresh_all
        assert len(received) == 0

    @pytest.mark.asyncio
    async def test_listen_reraises_cancelled_error(self):
        """asyncio.CancelledError must propagate so task cancellation works."""
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)
        feed._running = True

        async def _raise_cancelled():
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        mock_w3 = AsyncMock()
        mock_w3.eth.subscribe = AsyncMock(return_value="sub-id")
        mock_w3.socket.process_subscriptions = _raise_cancelled
        mock_w3.__aenter__ = AsyncMock(return_value=mock_w3)
        mock_w3.__aexit__ = AsyncMock(return_value=None)

        mock_cls = MagicMock()
        mock_cls.return_value = mock_w3
        mock_cls.AsyncWebsocketProvider = MagicMock()

        with patch("pricing.price_feed.AsyncWeb3", mock_cls):
            with pytest.raises(asyncio.CancelledError):
                await feed._listen()

    @pytest.mark.asyncio
    async def test_listen_swallows_generic_exception(self):
        """Non-CancelledError exceptions are caught and logged — not propagated."""
        feed = PriceFeed(ws_url="wss://x", pools=[], on_update=lambda u: None)

        mock_cls = MagicMock()
        mock_cls.side_effect = RuntimeError("WebSocket connection failed")
        mock_cls.AsyncWebsocketProvider = MagicMock()

        with patch("pricing.price_feed.AsyncWeb3", mock_cls):
            # Should complete without raising
            await feed._listen()
