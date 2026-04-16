"""
tests/test_ws_orderbook.py — Unit tests for exchange.ws_orderbook.OrderBookStream

All tests use mocks — no real WebSocket connections or HTTP requests.

Test groups:
  1. OrderBookStream construction
  2. _apply_snapshot — initial state population
  3. _apply_event — incremental updates, stale-event filtering
  4. snapshot() — output format matches ExchangeClient.fetch_order_book
  5. Async iteration — aiter yields updated snapshots
  6. URL helpers — testnet vs mainnet
  7. CLI smoke test
"""

from __future__ import annotations

import json
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from exchange.ws_orderbook import OrderBookStream

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_stream(symbol: str = "ETH/USDT", testnet: bool = True) -> OrderBookStream:
    return OrderBookStream(symbol, testnet=testnet, depth_limit=5)


def _snapshot_data(last_update_id: int = 100) -> dict:
    return {
        "lastUpdateId": last_update_id,
        "bids": [["2000.00", "1.0"], ["1999.00", "2.0"], ["1998.00", "3.0"]],
        "asks": [["2001.00", "1.5"], ["2002.00", "2.5"], ["2003.00", "0.5"]],
    }


def _diff_event(
    first_id: int,
    final_id: int,
    bids=None,
    asks=None,
) -> str:
    return json.dumps(
        {
            "U": first_id,
            "u": final_id,
            "b": bids or [],
            "a": asks or [],
        }
    )


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_symbol_stored(self):
        s = _make_stream("BTC/USDT")
        assert s._symbol == "BTC/USDT"

    def test_ws_symbol_lowercased(self):
        s = _make_stream("ETH/USDT")
        assert s._ws_symbol == "ethusdt"

    def test_rest_symbol_no_slash(self):
        s = _make_stream("ETH/USDT")
        assert s._rest_symbol == "ETHUSDT"

    def test_initial_not_synced(self):
        s = _make_stream()
        assert s._synced is False

    def test_initial_last_update_id_zero(self):
        s = _make_stream()
        assert s._last_update_id == 0

    def test_initial_empty_bids_asks(self):
        s = _make_stream()
        assert s._bids == {}
        assert s._asks == {}

    async def test_not_connected_raises_on_iter(self):
        s = _make_stream()
        with pytest.raises(RuntimeError, match="not connected"):
            async for _ in s:
                pass


# ── 2. _apply_snapshot ─────────────────────────────────────────────────────────


class TestApplySnapshot:
    def test_sets_last_update_id(self):
        s = _make_stream()
        s._apply_snapshot(_snapshot_data(last_update_id=500))
        assert s._last_update_id == 500

    def test_synced_becomes_true(self):
        s = _make_stream()
        s._apply_snapshot(_snapshot_data())
        assert s._synced is True

    def test_bids_populated(self):
        s = _make_stream()
        s._apply_snapshot(_snapshot_data())
        assert Decimal("2000.00") in s._bids
        assert s._bids[Decimal("2000.00")] == Decimal("1.0")

    def test_asks_populated(self):
        s = _make_stream()
        s._apply_snapshot(_snapshot_data())
        assert Decimal("2001.00") in s._asks

    def test_zero_qty_levels_excluded(self):
        data = {
            "lastUpdateId": 1,
            "bids": [["1999.00", "0"], ["2000.00", "1.0"]],
            "asks": [["2001.00", "0.5"]],
        }
        s = _make_stream()
        s._apply_snapshot(data)
        assert Decimal("1999.00") not in s._bids
        assert Decimal("2000.00") in s._bids


# ── 3. _apply_event ────────────────────────────────────────────────────────────


class TestApplyEvent:
    def setup_method(self):
        self.s = _make_stream()
        self.s._apply_snapshot(_snapshot_data(last_update_id=100))

    def test_stale_event_returns_false(self):
        event = json.loads(_diff_event(first_id=90, final_id=100))
        result = self.s._apply_event(event)
        assert result is False

    def test_fresh_event_returns_true(self):
        event = json.loads(_diff_event(first_id=101, final_id=102))
        result = self.s._apply_event(event)
        assert result is True

    def test_updates_last_update_id(self):
        event = json.loads(_diff_event(first_id=101, final_id=105))
        self.s._apply_event(event)
        assert self.s._last_update_id == 105

    def test_bid_upsert(self):
        event = json.loads(_diff_event(101, 102, bids=[["1997.00", "5.0"]]))
        self.s._apply_event(event)
        assert self.s._bids[Decimal("1997.00")] == Decimal("5.0")

    def test_bid_update_existing(self):
        event = json.loads(_diff_event(101, 102, bids=[["2000.00", "9.9"]]))
        self.s._apply_event(event)
        assert self.s._bids[Decimal("2000.00")] == Decimal("9.9")

    def test_bid_removal_on_zero_qty(self):
        event = json.loads(_diff_event(101, 102, bids=[["2000.00", "0"]]))
        self.s._apply_event(event)
        assert Decimal("2000.00") not in self.s._bids

    def test_ask_upsert(self):
        event = json.loads(_diff_event(101, 102, asks=[["2005.00", "3.0"]]))
        self.s._apply_event(event)
        assert self.s._asks[Decimal("2005.00")] == Decimal("3.0")

    def test_ask_removal_on_zero_qty(self):
        event = json.loads(_diff_event(101, 102, asks=[["2001.00", "0"]]))
        self.s._apply_event(event)
        assert Decimal("2001.00") not in self.s._asks

    def test_multiple_levels_in_one_event(self):
        event = json.loads(
            _diff_event(
                101,
                103,
                bids=[["1996.00", "1.0"], ["1995.00", "2.0"]],
                asks=[["2004.00", "1.0"]],
            )
        )
        self.s._apply_event(event)
        assert Decimal("1996.00") in self.s._bids
        assert Decimal("1995.00") in self.s._bids
        assert Decimal("2004.00") in self.s._asks


# ── 4. snapshot() format ───────────────────────────────────────────────────────


class TestSnapshot:
    def setup_method(self):
        self.s = _make_stream()
        self.s._apply_snapshot(_snapshot_data(last_update_id=200))

    def test_required_keys_present(self):
        book = self.s.snapshot()
        for key in (
            "symbol",
            "timestamp",
            "bids",
            "asks",
            "best_bid",
            "best_ask",
            "mid_price",
            "spread_bps",
            "last_update_id",
        ):
            assert key in book

    def test_symbol_matches(self):
        assert self.s.snapshot()["symbol"] == "ETH/USDT"

    def test_last_update_id_correct(self):
        assert self.s.snapshot()["last_update_id"] == 200

    def test_bids_sorted_descending(self):
        book = self.s.snapshot()
        prices = [p for p, _ in book["bids"]]
        assert prices == sorted(prices, reverse=True)

    def test_asks_sorted_ascending(self):
        book = self.s.snapshot()
        prices = [p for p, _ in book["asks"]]
        assert prices == sorted(prices)

    def test_best_bid_is_highest(self):
        book = self.s.snapshot()
        assert book["best_bid"][0] == max(p for p, _ in book["bids"])

    def test_best_ask_is_lowest(self):
        book = self.s.snapshot()
        assert book["best_ask"][0] == min(p for p, _ in book["asks"])

    def test_mid_price_between_bid_and_ask(self):
        book = self.s.snapshot()
        bid_p = book["best_bid"][0]
        ask_p = book["best_ask"][0]
        assert bid_p < book["mid_price"] < ask_p

    def test_spread_bps_positive(self):
        book = self.s.snapshot()
        assert book["spread_bps"] > 0

    def test_prices_are_decimal(self):
        book = self.s.snapshot()
        assert isinstance(book["best_bid"][0], Decimal)
        assert isinstance(book["best_ask"][0], Decimal)
        assert isinstance(book["mid_price"], Decimal)

    def test_depth_limit_respected(self):
        s = OrderBookStream("ETH/USDT", depth_limit=2)
        s._apply_snapshot(_snapshot_data())
        book = s.snapshot()
        assert len(book["bids"]) <= 2
        assert len(book["asks"]) <= 2


# ── 5. Async iteration ─────────────────────────────────────────────────────────


class TestAsyncIteration:
    def _make_ws_mock(self, messages: list[str]) -> MagicMock:
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()

        async def _aiter(self_inner):
            for msg in messages:
                yield msg

        mock_ws.__aiter__ = _aiter
        return mock_ws

    def _make_session_mock(self, snapshot: dict) -> MagicMock:
        mock_session = MagicMock()
        mock_session.close = AsyncMock()

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=snapshot)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session.get = MagicMock(return_value=mock_resp)
        return mock_session

    async def test_yields_snapshot_per_fresh_event(self):
        messages = [
            _diff_event(101, 101, bids=[["1997.00", "1.0"]]),
            _diff_event(102, 102, asks=[["2004.00", "2.0"]]),
        ]
        s = _make_stream()
        s._ws = self._make_ws_mock(messages)
        s._session = self._make_session_mock(_snapshot_data(100))
        s._apply_snapshot(_snapshot_data(100))

        results = []
        async for book in s:
            results.append(book)
        assert len(results) == 2

    async def test_stale_events_not_yielded(self):
        messages = [
            _diff_event(50, 99, bids=[["1990.00", "1.0"]]),  # stale
            _diff_event(101, 101, bids=[["1997.00", "1.0"]]),  # fresh
        ]
        s = _make_stream()
        s._ws = self._make_ws_mock(messages)
        s._session = self._make_session_mock(_snapshot_data(100))
        s._apply_snapshot(_snapshot_data(100))

        results = []
        async for book in s:
            results.append(book)
        assert len(results) == 1

    async def test_yielded_book_has_required_keys(self):
        messages = [_diff_event(101, 101, bids=[["1997.00", "1.0"]])]
        s = _make_stream()
        s._ws = self._make_ws_mock(messages)
        s._apply_snapshot(_snapshot_data(100))

        book = None
        async for book in s:
            break
        assert book is not None
        assert "best_bid" in book
        assert "last_update_id" in book


# ── 6. URL helpers ─────────────────────────────────────────────────────────────


class TestURLHelpers:
    def test_testnet_ws_url(self):
        s = OrderBookStream("ETH/USDT", testnet=True)
        assert "testnet.binance.vision" in s._ws_url

    def test_mainnet_ws_url(self):
        s = OrderBookStream("ETH/USDT", testnet=False)
        assert "stream.binance.com" in s._ws_url

    def test_ws_url_contains_symbol(self):
        s = OrderBookStream("BTC/USDT", testnet=True)
        assert "btcusdt" in s._ws_url

    def test_testnet_rest_base(self):
        s = OrderBookStream("ETH/USDT", testnet=True)
        assert "testnet" in s._rest_base

    def test_mainnet_rest_base(self):
        s = OrderBookStream("ETH/USDT", testnet=False)
        assert "api.binance.com" in s._rest_base


# ── 7. CLI smoke ───────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_module_importable(self):
        from exchange.ws_orderbook import _run_cli

        assert callable(_run_cli)


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestConnect:
    """connect() and close() via mocked websockets + aiohttp."""

    def _make_ws_mock(self):
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()

        async def _aiter(self_inner):
            return
            yield  # make it an async generator

        mock_ws.__aiter__ = _aiter
        return mock_ws

    def _make_session_mock(self, snapshot):
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=snapshot)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        return mock_session

    async def test_connect_sets_synced(self):
        snap = {"lastUpdateId": 200, "bids": [["2000", "1"]], "asks": [["2001", "1"]]}
        mock_ws = self._make_ws_mock()
        mock_session = self._make_session_mock(snap)

        s = OrderBookStream("ETH/USDT", testnet=True)
        s._ws = mock_ws
        s._session = mock_session
        snap_data = await s._fetch_snapshot()
        s._apply_snapshot(snap_data)

        assert s._synced is True
        assert s._last_update_id == 200

    async def test_close_clears_ws(self):
        s = OrderBookStream("ETH/USDT")
        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()
        mock_session = MagicMock()
        mock_session.close = AsyncMock()
        s._ws = mock_ws
        s._session = mock_session
        await s.close()
        assert s._ws is None
        assert s._session is None

    async def test_close_with_none_ws_no_error(self):
        s = OrderBookStream("ETH/USDT")
        await s.close()  # _ws and _session are None — should not raise

    async def test_aenter_returns_self(self):
        s = OrderBookStream("ETH/USDT")
        with patch.object(s, "connect", new=AsyncMock()):
            result = await s.__aenter__()
        assert result is s

    async def test_aexit_calls_close(self):
        s = OrderBookStream("ETH/USDT")
        with patch.object(s, "close", new=AsyncMock()) as mock_close:
            await s.__aexit__(None, None, None)
            mock_close.assert_called_once()

    async def test_fetch_snapshot_calls_rest(self):
        snap = {"lastUpdateId": 50, "bids": [], "asks": []}
        s = OrderBookStream("ETH/USDT", testnet=True)
        mock_session = MagicMock()
        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=snap)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_resp)
        s._session = mock_session
        result = await s._fetch_snapshot()
        assert result["lastUpdateId"] == 50


class TestConnectMethod:
    """Cover connect() — the only remaining gap."""

    async def test_connect_calls_websockets_and_aiohttp(self):
        import sys

        snap = {"lastUpdateId": 10, "bids": [["2000", "1"]], "asks": [["2001", "1"]]}

        mock_ws = MagicMock()
        mock_ws.close = AsyncMock()

        mock_resp = AsyncMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json = AsyncMock(return_value=snap)
        mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
        mock_resp.__aexit__ = AsyncMock(return_value=False)

        mock_session_instance = MagicMock()
        mock_session_instance.close = AsyncMock()
        mock_session_instance.get = MagicMock(return_value=mock_resp)

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect = AsyncMock(return_value=mock_ws)

        mock_aiohttp_mod = MagicMock()
        mock_aiohttp_mod.ClientSession = MagicMock(return_value=mock_session_instance)

        with patch.dict(sys.modules, {"websockets": mock_ws_mod, "aiohttp": mock_aiohttp_mod}):
            s = OrderBookStream("ETH/USDT", testnet=True)
            await s.connect()

        assert s._synced is True
        assert s._last_update_id == 10

    async def test_connect_missing_websockets_raises(self):
        import sys

        with patch.dict(sys.modules, {"websockets": None}):
            s = OrderBookStream("ETH/USDT")
            with pytest.raises(ImportError, match="websockets"):
                await s.connect()

    async def test_connect_missing_aiohttp_raises(self):
        import sys

        mock_ws_mod = MagicMock()
        mock_ws_mod.connect = AsyncMock(return_value=MagicMock())
        with patch.dict(sys.modules, {"websockets": mock_ws_mod, "aiohttp": None}):
            s = OrderBookStream("ETH/USDT")
            with pytest.raises(ImportError, match="aiohttp"):
                await s.connect()


class TestRunCLI:
    def test_run_cli_importable(self):
        from exchange.ws_orderbook import _run_cli

        assert callable(_run_cli)

    def test_run_cli_no_dotenv(self):
        """Cover the `except ImportError: pass` branch for dotenv."""
        import sys

        from exchange.ws_orderbook import _run_cli

        with patch.dict(sys.modules, {"dotenv": None}):
            with patch("exchange.ws_orderbook.asyncio.run", side_effect=KeyboardInterrupt):
                rc = _run_cli(["ETH/USDT"])
        assert rc == 0

    def test_run_cli_exits_zero_with_mock(self):
        from exchange.ws_orderbook import _run_cli

        async def _fake_stream():
            book = {
                "best_bid": (Decimal("2000"), Decimal("1")),
                "best_ask": (Decimal("2001"), Decimal("1")),
                "mid_price": Decimal("2000.5"),
                "spread_bps": Decimal("5"),
                "last_update_id": 100,
            }
            yield book

        class _FakeStream:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                pass

            def __aiter__(self):
                return _fake_stream()

        # _run_cli calls asyncio.run() — patch it to use a fresh loop
        # so it works whether called from sync or async context.
        def _run_sync(coro):
            import asyncio as _asyncio

            loop = _asyncio.new_event_loop()
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()

        with patch("exchange.ws_orderbook.OrderBookStream", return_value=_FakeStream()):
            with patch("exchange.ws_orderbook.asyncio.run", side_effect=_run_sync):
                rc = _run_cli(["ETH/USDT", "--count", "1"])
        assert rc == 0

    def test_run_cli_keyboard_interrupt(self):
        from exchange.ws_orderbook import _run_cli

        with patch("exchange.ws_orderbook.asyncio.run", side_effect=KeyboardInterrupt):
            rc = _run_cli(["ETH/USDT", "--count", "1"])
        assert rc == 0
