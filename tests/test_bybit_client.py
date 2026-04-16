"""
tests/test_bybit_client.py — Unit tests for exchange.bybit_client.BybitClient

All tests use MagicMock — no real network calls or API keys required.

Test groups:
  1. Construction — sandbox mode, health check, import error
  2. fetch_order_book — structure, sort order, Decimal types
  3. fetch_balance — normalisation, zero filtering
  4. create_limit_ioc_order — IOC flag, normalised return
  5. create_market_order — market order delegation
  6. cancel_order / fetch_order_status — delegation
  7. get_trading_fees — Decimal conversion
  8. Rate limiter — weight tracking, sleep behaviour
  9. Interface parity with ExchangeClient
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from exchange.bybit_client import BybitClient, _to_decimal

# ── Helpers ────────────────────────────────────────────────────────────────────

_TEST_CONFIG = {"apiKey": "k", "secret": "s", "sandbox": True}  # pragma: allowlist secret


def _make_client() -> tuple[BybitClient, MagicMock]:
    mock_exchange = MagicMock()
    mock_exchange.id = "bybit"
    mock_exchange.fetch_time.return_value = {"time": 1700000000000}
    mock_exchange.set_sandbox_mode = MagicMock()

    with patch("ccxt.bybit", return_value=mock_exchange):
        client = BybitClient(_TEST_CONFIG)

    return client, mock_exchange


def _raw_order(
    status="closed",
    filled=1.0,
    amount=1.0,
    price=2000.0,
    side="buy",
) -> dict:
    return {
        "id": "99",
        "symbol": "ETH/USDT",
        "side": side,
        "type": "limit",
        "timeInForce": "IOC",
        "amount": amount,
        "filled": filled,
        "average": price,
        "status": status,
        "fee": {"cost": 0.002, "currency": "USDT"},
        "timestamp": 1700000000000,
    }


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_sandbox_mode_enabled(self):
        mock_exchange = MagicMock()
        mock_exchange.id = "bybit"
        mock_exchange.fetch_time.return_value = {}

        with patch("ccxt.bybit", return_value=mock_exchange):
            BybitClient({"apiKey": "k", "secret": "s", "sandbox": True})

        mock_exchange.set_sandbox_mode.assert_called_once_with(True)

    def test_sandbox_false_not_set(self):
        mock_exchange = MagicMock()
        mock_exchange.id = "bybit"
        mock_exchange.fetch_time.return_value = {}

        with patch("ccxt.bybit", return_value=mock_exchange):
            BybitClient({"apiKey": "k", "secret": "s", "sandbox": False})

        mock_exchange.set_sandbox_mode.assert_not_called()

    def test_health_check_called_on_init(self):
        mock_exchange = MagicMock()
        mock_exchange.id = "bybit"
        mock_exchange.fetch_time.return_value = {}

        with patch("ccxt.bybit", return_value=mock_exchange):
            BybitClient(_TEST_CONFIG)

        mock_exchange.fetch_time.assert_called_once()

    def test_missing_ccxt_raises_import_error(self):
        with patch.dict("sys.modules", {"ccxt": None}):
            with pytest.raises(ImportError, match="ccxt"):
                BybitClient(_TEST_CONFIG)

    def test_auth_error_propagates(self):
        import ccxt

        mock_exchange = MagicMock()
        mock_exchange.fetch_time.side_effect = ccxt.AuthenticationError("bad key")

        with patch("ccxt.bybit", return_value=mock_exchange):
            with pytest.raises(ccxt.AuthenticationError):
                BybitClient(_TEST_CONFIG)


# ── 2. fetch_order_book ────────────────────────────────────────────────────────


class TestFetchOrderBook:
    def test_returns_required_fields(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
            "timestamp": 1700000000000,
        }
        book = client.fetch_order_book("ETH/USDT")
        for key in ("symbol", "bids", "asks", "best_bid", "best_ask", "mid_price", "spread_bps"):
            assert key in book

    def test_bids_sorted_descending(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {
            "bids": [[1999.0, 1.0], [2000.0, 2.0], [1998.0, 0.5]],
            "asks": [[2001.0, 1.0]],
            "timestamp": None,
        }
        book = client.fetch_order_book("ETH/USDT")
        prices = [p for p, _ in book["bids"]]
        assert prices == sorted(prices, reverse=True)

    def test_asks_sorted_ascending(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2003.0, 1.0], [2001.0, 2.0], [2002.0, 0.5]],
            "timestamp": None,
        }
        book = client.fetch_order_book("ETH/USDT")
        prices = [p for p, _ in book["asks"]]
        assert prices == sorted(prices)

    def test_mid_price_correct(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2002.0, 1.0]],
            "timestamp": None,
        }
        book = client.fetch_order_book("ETH/USDT")
        assert book["mid_price"] == Decimal("2001")

    def test_all_prices_are_decimal(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
            "timestamp": None,
        }
        book = client.fetch_order_book("ETH/USDT")
        assert isinstance(book["best_bid"][0], Decimal)
        assert isinstance(book["best_ask"][0], Decimal)
        assert isinstance(book["mid_price"], Decimal)
        assert isinstance(book["spread_bps"], Decimal)

    def test_empty_book_does_not_raise(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {"bids": [], "asks": [], "timestamp": None}
        book = client.fetch_order_book("ETH/USDT")
        assert book["best_bid"] == (Decimal("0"), Decimal("0"))
        assert book["spread_bps"] == Decimal("0")


# ── 3. fetch_balance ───────────────────────────────────────────────────────────


class TestFetchBalance:
    def test_filters_zero_balance(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_balance.return_value = {
            "ETH": {"free": 1.0, "used": 0.0, "total": 1.0},
            "BTC": {"free": 0.0, "used": 0.0, "total": 0.0},
        }
        bal = client.fetch_balance()
        assert "ETH" in bal
        assert "BTC" not in bal

    def test_values_are_decimal(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_balance.return_value = {
            "USDT": {"free": 500.0, "used": 10.0, "total": 510.0},
        }
        bal = client.fetch_balance()
        assert isinstance(bal["USDT"]["free"], Decimal)
        assert isinstance(bal["USDT"]["locked"], Decimal)


# ── 4. create_limit_ioc_order ──────────────────────────────────────────────────


class TestCreateLimitIocOrder:
    def test_places_limit_ioc_order(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order()
        client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        pos = mock_ex.create_order.call_args[0]
        # create_order(symbol, type, side, amount, price, params)
        assert pos[1] == "limit"
        assert pos[2] == "buy"
        assert pos[5] == {"timeInForce": "IOC"}

    def test_returns_required_fields(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order()
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        for key in ("id", "symbol", "side", "amount_filled", "avg_fill_price", "status"):
            assert key in result

    def test_filled_order_status(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order(status="closed", filled=1.0, amount=1.0)
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "filled"

    def test_amount_filled_is_decimal(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order()
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert isinstance(result["amount_filled"], Decimal)

    def test_fee_is_decimal(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order()
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert isinstance(result["fee"], Decimal)


# ── 5. create_market_order ─────────────────────────────────────────────────────


class TestCreateMarketOrder:
    def test_places_market_order(self):
        client, mock_ex = _make_client()
        mock_ex.create_order.return_value = _raw_order()
        client.create_market_order("ETH/USDT", "sell", 0.5)
        args = mock_ex.create_order.call_args[0]
        # create_order(symbol, type, side, amount)
        assert args[1] == "market"
        assert args[2] == "sell"


# ── 6. cancel_order / fetch_order_status ───────────────────────────────────────


class TestCancelAndStatus:
    def test_cancel_calls_exchange(self):
        client, mock_ex = _make_client()
        mock_ex.cancel_order.return_value = _raw_order(status="canceled")
        client.cancel_order("99", "ETH/USDT")
        mock_ex.cancel_order.assert_called_once_with("99", "ETH/USDT")

    def test_fetch_order_status_calls_exchange(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order.return_value = _raw_order()
        client.fetch_order_status("99", "ETH/USDT")
        mock_ex.fetch_order.assert_called_once_with("99", "ETH/USDT")


# ── 7. get_trading_fees ────────────────────────────────────────────────────────


class TestGetTradingFees:
    def test_returns_maker_taker(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_trading_fee.return_value = {"maker": 0.001, "taker": 0.001}
        fees = client.get_trading_fees("ETH/USDT")
        assert "maker" in fees
        assert "taker" in fees

    def test_fees_are_decimal(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_trading_fee.return_value = {"maker": 0.0008, "taker": 0.001}
        fees = client.get_trading_fees("ETH/USDT")
        assert isinstance(fees["maker"], Decimal)
        assert isinstance(fees["taker"], Decimal)


# ── 8. Rate limiter ────────────────────────────────────────────────────────────


class TestRateLimiter:
    def test_weight_accumulates(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.return_value = {"bids": [], "asks": [], "timestamp": None}
        initial = client._weight_used
        client.fetch_order_book("ETH/USDT")
        assert client._weight_used > initial

    def test_weight_resets_after_window(self):
        import time as _time

        client, _ = _make_client()
        client._weight_used = 500
        client._weight_reset_at = _time.monotonic() - 1  # already expired
        client._consume_weight("fetch_order_book")
        assert client._weight_used == 1  # reset to 0 then +1


# ── 9. Interface parity with ExchangeClient ────────────────────────────────────


class TestInterfaceParity:
    """BybitClient must expose the same public methods as ExchangeClient."""

    def test_has_fetch_order_book(self):
        assert hasattr(BybitClient, "fetch_order_book")

    def test_has_fetch_balance(self):
        assert hasattr(BybitClient, "fetch_balance")

    def test_has_create_limit_ioc_order(self):
        assert hasattr(BybitClient, "create_limit_ioc_order")

    def test_has_create_market_order(self):
        assert hasattr(BybitClient, "create_market_order")

    def test_has_cancel_order(self):
        assert hasattr(BybitClient, "cancel_order")

    def test_has_fetch_order_status(self):
        assert hasattr(BybitClient, "fetch_order_status")

    def test_has_get_trading_fees(self):
        assert hasattr(BybitClient, "get_trading_fees")

    def test_order_book_format_matches_exchange_client(self):
        """Both clients must return the same keys from fetch_order_book."""
        from exchange.client import ExchangeClient

        bybit_client, mock_bybit = _make_client()
        mock_bybit.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
            "timestamp": 1700000000000,
        }
        bybit_book = bybit_client.fetch_order_book("ETH/USDT")

        mock_binance = MagicMock()
        mock_binance.id = "binance"
        mock_binance.fetch_time.return_value = {}
        mock_binance.fetch_order_book.return_value = {
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
            "timestamp": 1700000000000,
        }
        with patch("ccxt.binance", return_value=mock_binance):
            binance_client = ExchangeClient(_TEST_CONFIG)
        binance_book = binance_client.fetch_order_book("ETH/USDT")

        assert set(bybit_book.keys()) == set(binance_book.keys())


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestToDecimalEdgeCases:
    """Cover _to_decimal(None) and _to_decimal(invalid) branches."""

    def test_none_returns_zero(self):
        assert _to_decimal(None) == Decimal("0")

    def test_invalid_string_returns_zero(self):
        assert _to_decimal("not_a_number") == Decimal("0")

    def test_valid_string_converts(self):
        assert _to_decimal("3.14") == Decimal("3.14")


class TestFetchBalanceNonDictKey:
    """fetch_balance skips non-dict values (line 143 in bybit_client)."""

    def test_non_dict_asset_skipped(self):
        client, mock_ex = _make_client()
        mock_ex.fetch_balance.return_value = {
            "ETH": {"free": 1.0, "used": 0.0, "total": 1.0},
            "info": "some_string_not_a_dict",  # non-dict → skip
            "free": 500.0,  # non-dict → skip
        }
        bal = client.fetch_balance()
        assert "ETH" in bal
        assert "info" not in bal
        assert "free" not in bal


class TestExceptionHandlers:
    """Cover RateLimitExceeded, NetworkError, BaseError handlers."""

    def test_rate_limit_exceeded_reraises(self):
        import ccxt

        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.side_effect = ccxt.RateLimitExceeded("too fast")
        with patch("time.sleep"):
            with pytest.raises(ccxt.RateLimitExceeded):
                client.fetch_order_book("ETH/USDT")

    def test_network_error_reraises(self):
        import ccxt

        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.side_effect = ccxt.NetworkError("timeout")
        with pytest.raises(ccxt.NetworkError):
            client.fetch_order_book("ETH/USDT")

    def test_base_error_reraises(self):
        import ccxt

        client, mock_ex = _make_client()
        mock_ex.fetch_order_book.side_effect = ccxt.BaseError("unknown")
        with pytest.raises(ccxt.BaseError):
            client.fetch_order_book("ETH/USDT")


class TestRateLimiterSleepPath:
    """Cover the branch where budget is exhausted and sleep_for > 0."""

    def test_sleep_called_when_budget_exhausted(self):
        import time as _time

        client, mock_ex = _make_client()
        client._weight_used = 540  # just under _WEIGHT_SAFETY (=540)
        client._weight_reset_at = _time.monotonic() + 30.0  # 30s left in window

        with patch("time.sleep") as mock_sleep:
            client._consume_weight("fetch_order_book")  # weight=1 → 541 >= 540
            mock_sleep.assert_called_once()


class TestNormaliseOrderEdgeCases:
    """Cover partially_filled and unknown status branches."""

    def test_partially_filled_status(self):
        client, mock_ex = _make_client()
        raw = {
            "id": "1",
            "symbol": "ETH/USDT",
            "side": "buy",
            "type": "limit",
            "timeInForce": "IOC",
            "amount": 2.0,
            "filled": 1.0,
            "average": 2000.0,
            "status": "closed",
            "fee": {"cost": 0.001, "currency": "USDT"},
            "timestamp": None,
        }
        mock_ex.create_order.return_value = raw
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 2.0, 2000.0)
        assert result["status"] == "partially_filled"

    def test_unknown_status_passthrough(self):
        client, mock_ex = _make_client()
        raw = {
            "id": "2",
            "symbol": "ETH/USDT",
            "side": "buy",
            "type": "limit",
            "timeInForce": "GTC",
            "amount": 1.0,
            "filled": 0.0,
            "average": 0.0,
            "status": "open",
            "fee": None,
            "timestamp": None,
        }
        mock_ex.create_order.return_value = raw
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "open"

    def test_none_status_becomes_unknown(self):
        client, mock_ex = _make_client()
        raw = {
            "id": "3",
            "symbol": "ETH/USDT",
            "side": "buy",
            "type": "limit",
            "timeInForce": "IOC",
            "amount": 1.0,
            "filled": 0.0,
            "average": 0.0,
            "status": None,
            "fee": None,
            "timestamp": None,
        }
        mock_ex.create_order.return_value = raw
        result = client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "unknown"
