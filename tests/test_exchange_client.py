"""
tests/test_exchange_client.py — Unit tests for exchange.client.ExchangeClient

All tests use MagicMock — no real network calls, no API keys required.

Test groups:
  1. Construction — health check on init, auth failure, import error
  2. fetch_order_book — structure, bid/ask sort order, spread calculation
  3. fetch_balance — normalisation, zero-balance filtering
  4. create_limit_ioc_order — fill info, partial fill, expired
  5. create_market_order — delegates correctly
  6. cancel_order / fetch_order_status — normalisation
  7. get_trading_fees — Decimal conversion
  8. Rate limiter — weight tracking, sleep when budget exhausted
  9. _normalise_order — status mapping edge cases
"""

from __future__ import annotations

import time
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from exchange.client import ExchangeClient, _to_decimal

# ── Helpers ────────────────────────────────────────────────────────────────────


_TEST_CONFIG = {"apiKey": "k", "secret": "s", "sandbox": True}  # pragma: allowlist secret
_BAD_CONFIG = {"apiKey": "bad", "secret": "bad", "sandbox": True}  # pragma: allowlist secret


def _make_client() -> tuple[ExchangeClient, MagicMock]:
    """Return a client with a fully mocked ccxt.binance instance."""
    mock_exchange = MagicMock()
    mock_exchange.id = "binance"
    mock_exchange.fetch_time.return_value = {"serverTime": 1700000000000}

    with patch("ccxt.binance", return_value=mock_exchange):
        client = ExchangeClient(_TEST_CONFIG)

    return client, mock_exchange


def _raw_order(
    status="closed",
    filled=1.0,
    amount=1.0,
    price=2000.0,
    side="buy",
    tif="IOC",
) -> dict:
    return {
        "id": "123",
        "symbol": "ETH/USDT",
        "side": side,
        "type": "limit",
        "timeInForce": tif,
        "status": status,
        "filled": filled,
        "amount": amount,
        "average": price,
        "price": price,
        "fee": {"cost": 0.002, "currency": "USDT"},
        "timestamp": 1700000000000,
        "info": {"timeInForce": tif},
    }


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_health_check_called_on_init(self):
        mock_exchange = MagicMock()
        mock_exchange.fetch_time.return_value = {}
        with patch("ccxt.binance", return_value=mock_exchange):
            ExchangeClient(_TEST_CONFIG)
        mock_exchange.fetch_time.assert_called_once()

    def test_auth_error_on_init_propagates(self):
        import ccxt

        mock_exchange = MagicMock()
        mock_exchange.fetch_time.side_effect = ccxt.AuthenticationError("bad key")
        with patch("ccxt.binance", return_value=mock_exchange):
            with pytest.raises(ccxt.AuthenticationError):
                ExchangeClient(_BAD_CONFIG)

    def test_network_error_on_init_propagates(self):
        import ccxt

        mock_exchange = MagicMock()
        mock_exchange.fetch_time.side_effect = ccxt.NetworkError("timeout")
        with patch("ccxt.binance", return_value=mock_exchange):
            with pytest.raises(ccxt.NetworkError):
                ExchangeClient(_TEST_CONFIG)

    def test_missing_ccxt_raises_import_error(self):
        with patch.dict("sys.modules", {"ccxt": None}):
            with pytest.raises(ImportError, match="ccxt"):
                ExchangeClient({})


# ── 2. fetch_order_book ────────────────────────────────────────────────────────


class TestFetchOrderBook:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 1700000000000,
            "bids": [[2001.0, 1.5], [2000.0, 2.0], [1999.0, 0.5]],
            "asks": [[2002.0, 1.0], [2003.0, 0.8], [2004.0, 2.0]],
        }

    def test_returns_required_fields(self):
        book = self.client.fetch_order_book("ETH/USDT")
        for key in (
            "symbol",
            "timestamp",
            "bids",
            "asks",
            "best_bid",
            "best_ask",
            "mid_price",
            "spread_bps",
        ):
            assert key in book

    def test_bids_sorted_descending(self):
        book = self.client.fetch_order_book("ETH/USDT")
        prices = [b[0] for b in book["bids"]]
        assert prices == sorted(prices, reverse=True)

    def test_asks_sorted_ascending(self):
        book = self.client.fetch_order_book("ETH/USDT")
        prices = [a[0] for a in book["asks"]]
        assert prices == sorted(prices)

    def test_best_bid_is_highest_bid(self):
        book = self.client.fetch_order_book("ETH/USDT")
        assert book["best_bid"][0] == Decimal("2001.0")

    def test_best_ask_is_lowest_ask(self):
        book = self.client.fetch_order_book("ETH/USDT")
        assert book["best_ask"][0] == Decimal("2002.0")

    def test_mid_price_correct(self):
        book = self.client.fetch_order_book("ETH/USDT")
        expected = (Decimal("2001") + Decimal("2002")) / Decimal("2")
        assert book["mid_price"] == expected

    def test_spread_bps_correct(self):
        book = self.client.fetch_order_book("ETH/USDT")
        spread = (Decimal("2002") - Decimal("2001")) / Decimal("2001.5") * Decimal("10000")
        assert abs(book["spread_bps"] - spread) < Decimal("0.01")

    def test_spread_bps_is_decimal(self):
        book = self.client.fetch_order_book("ETH/USDT")
        assert isinstance(book["spread_bps"], Decimal)

    def test_all_prices_are_decimal(self):
        book = self.client.fetch_order_book("ETH/USDT")
        for price, qty in book["bids"] + book["asks"]:
            assert isinstance(price, Decimal)
            assert isinstance(qty, Decimal)

    def test_empty_book_does_not_raise(self):
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 1700000000000,
            "bids": [],
            "asks": [],
        }
        book = self.client.fetch_order_book("ETH/USDT")
        assert book["spread_bps"] == Decimal("0")
        assert book["mid_price"] == Decimal("0")

    def test_passes_limit_to_exchange(self):
        self.client.fetch_order_book("ETH/USDT", limit=50)
        self.mock_ex.fetch_order_book.assert_called_with("ETH/USDT", 50)


# ── 3. fetch_balance ───────────────────────────────────────────────────────────


class TestFetchBalance:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()
        self.mock_ex.fetch_balance.return_value = {
            "ETH": {"free": "10.5", "used": "0", "total": "10.5"},
            "USDT": {"free": "20000", "used": "500", "total": "20500"},
            "BNB": {"free": "0", "used": "0", "total": "0"},
            "info": {"some": "metadata"},
            "total": {"ETH": "10.5"},
        }

    def test_returns_non_zero_assets(self):
        bal = self.client.fetch_balance()
        assert "ETH" in bal
        assert "USDT" in bal

    def test_filters_zero_balance_assets(self):
        bal = self.client.fetch_balance()
        assert "BNB" not in bal

    def test_filters_non_dict_keys(self):
        bal = self.client.fetch_balance()
        assert "info" not in bal
        assert "total" not in bal

    def test_values_are_decimal(self):
        bal = self.client.fetch_balance()
        for asset, info in bal.items():
            assert isinstance(info["free"], Decimal)
            assert isinstance(info["locked"], Decimal)
            assert isinstance(info["total"], Decimal)

    def test_locked_maps_from_used(self):
        bal = self.client.fetch_balance()
        assert bal["USDT"]["locked"] == Decimal("500")

    def test_free_plus_locked_equals_total(self):
        bal = self.client.fetch_balance()
        assert bal["USDT"]["free"] + bal["USDT"]["locked"] == bal["USDT"]["total"]


# ── 4. create_limit_ioc_order ──────────────────────────────────────────────────


class TestCreateLimitIocOrder:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_places_limit_ioc_order(self):
        self.mock_ex.create_order.return_value = _raw_order()
        self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        self.mock_ex.create_order.assert_called_once_with(
            "ETH/USDT", "limit", "buy", 1.0, 2000.0, {"timeInForce": "IOC"}
        )

    def test_returns_required_fields(self):
        self.mock_ex.create_order.return_value = _raw_order()
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        for key in (
            "id",
            "symbol",
            "side",
            "type",
            "amount_requested",
            "amount_filled",
            "avg_fill_price",
            "fee",
            "fee_asset",
            "status",
            "timestamp",
        ):
            assert key in result

    def test_filled_order_status(self):
        self.mock_ex.create_order.return_value = _raw_order(status="closed", filled=1.0, amount=1.0)
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "filled"

    def test_partial_fill_status(self):
        self.mock_ex.create_order.return_value = _raw_order(status="closed", filled=0.5, amount=1.0)
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "partially_filled"

    def test_expired_status(self):
        self.mock_ex.create_order.return_value = _raw_order(
            status="canceled", filled=0.0, amount=1.0
        )
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert result["status"] == "expired"

    def test_amount_filled_is_decimal(self):
        self.mock_ex.create_order.return_value = _raw_order(filled=0.75)
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert isinstance(result["amount_filled"], Decimal)
        assert result["amount_filled"] == Decimal("0.75")

    def test_avg_fill_price_is_decimal(self):
        self.mock_ex.create_order.return_value = _raw_order(price=1999.5)
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert isinstance(result["avg_fill_price"], Decimal)

    def test_fee_is_decimal(self):
        self.mock_ex.create_order.return_value = _raw_order()
        result = self.client.create_limit_ioc_order("ETH/USDT", "buy", 1.0, 2000.0)
        assert isinstance(result["fee"], Decimal)


# ── 5. create_market_order ─────────────────────────────────────────────────────


class TestCreateMarketOrder:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_places_market_order(self):
        self.mock_ex.create_order.return_value = _raw_order()
        self.client.create_market_order("ETH/USDT", "sell", 0.5)
        self.mock_ex.create_order.assert_called_once_with("ETH/USDT", "market", "sell", 0.5)

    def test_returns_normalised_dict(self):
        self.mock_ex.create_order.return_value = _raw_order(side="sell")
        result = self.client.create_market_order("ETH/USDT", "sell", 0.5)
        assert result["side"] == "sell"
        assert isinstance(result["amount_filled"], Decimal)


# ── 6. cancel_order / fetch_order_status ──────────────────────────────────────


class TestCancelAndStatus:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_cancel_calls_exchange(self):
        self.mock_ex.cancel_order.return_value = _raw_order(status="canceled")
        self.client.cancel_order("123", "ETH/USDT")
        self.mock_ex.cancel_order.assert_called_once_with("123", "ETH/USDT")

    def test_cancel_returns_normalised(self):
        self.mock_ex.cancel_order.return_value = _raw_order(status="canceled", filled=0.0)
        result = self.client.cancel_order("123", "ETH/USDT")
        assert result["status"] == "expired"
        assert result["id"] == "123"

    def test_fetch_order_status_calls_exchange(self):
        self.mock_ex.fetch_order.return_value = _raw_order()
        self.client.fetch_order_status("123", "ETH/USDT")
        self.mock_ex.fetch_order.assert_called_once_with("123", "ETH/USDT")

    def test_fetch_order_status_returns_normalised(self):
        self.mock_ex.fetch_order.return_value = _raw_order(status="closed", filled=1.0)
        result = self.client.fetch_order_status("123", "ETH/USDT")
        assert result["status"] == "filled"


# ── 7. get_trading_fees ────────────────────────────────────────────────────────


class TestGetTradingFees:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_returns_maker_and_taker(self):
        self.mock_ex.fetch_trading_fee.return_value = {"maker": 0.001, "taker": 0.001}
        fees = self.client.get_trading_fees("ETH/USDT")
        assert "maker" in fees
        assert "taker" in fees

    def test_fees_are_decimal(self):
        self.mock_ex.fetch_trading_fee.return_value = {"maker": 0.001, "taker": 0.001}
        fees = self.client.get_trading_fees("ETH/USDT")
        assert isinstance(fees["maker"], Decimal)
        assert isinstance(fees["taker"], Decimal)

    def test_fee_values_correct(self):
        self.mock_ex.fetch_trading_fee.return_value = {"maker": "0.0005", "taker": "0.001"}
        fees = self.client.get_trading_fees("ETH/USDT")
        assert fees["maker"] == Decimal("0.0005")
        assert fees["taker"] == Decimal("0.001")


# ── 8. Rate limiter ────────────────────────────────────────────────────────────


class TestRateLimiter:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_weight_accumulates(self):
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 0,
            "bids": [],
            "asks": [],
        }
        initial = self.client._weight_used
        self.client.fetch_order_book("ETH/USDT")
        assert self.client._weight_used > initial

    def test_weight_resets_after_window(self):
        self.client._weight_used = 500
        self.client._weight_reset_at = time.monotonic() - 1  # expired
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 0,
            "bids": [],
            "asks": [],
        }
        self.client.fetch_order_book("ETH/USDT")
        # After reset, weight should be just the cost of this one call
        assert self.client._weight_used < 500

    def test_blocks_when_budget_exhausted(self):
        # Fill weight to just below safety threshold
        self.client._weight_used = 1079  # _WEIGHT_SAFETY = 1080
        self.client._weight_reset_at = time.monotonic() + 0.05  # expires in 50ms

        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 0,
            "bids": [],
            "asks": [],
        }
        with patch("time.sleep") as mock_sleep:
            self.client.fetch_order_book("ETH/USDT")
            mock_sleep.assert_called()


# ── 9. _normalise_order edge cases ────────────────────────────────────────────


class TestNormaliseOrder:
    def setup_method(self):
        self.client, _ = _make_client()

    def test_none_fee_handled(self):
        raw = _raw_order()
        raw["fee"] = None
        result = self.client._normalise_order(raw)
        assert result["fee"] == Decimal("0")
        assert result["fee_asset"] == ""

    def test_missing_average_falls_back_to_price(self):
        raw = _raw_order(price=1800.0)
        raw["average"] = None
        result = self.client._normalise_order(raw)
        assert result["avg_fill_price"] == Decimal("1800.0")

    def test_cancelled_spelling_variant(self):
        raw = _raw_order(status="cancelled", filled=0.0)
        result = self.client._normalise_order(raw)
        assert result["status"] == "expired"

    def test_open_status_preserved(self):
        raw = _raw_order(status="open", filled=0.0)
        result = self.client._normalise_order(raw)
        assert result["status"] == "open"

    def test_id_converted_to_string(self):
        raw = _raw_order()
        raw["id"] = 99999
        result = self.client._normalise_order(raw)
        assert isinstance(result["id"], str)
        assert result["id"] == "99999"


# ── 10. _call error handling ──────────────────────────────────────────────────


class TestCallErrorHandling:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_rate_limit_exceeded_sleeps_and_reraises(self):
        import ccxt

        self.mock_ex.fetch_order_book.side_effect = ccxt.RateLimitExceeded("too fast")
        with patch("time.sleep") as mock_sleep:
            with pytest.raises(ccxt.RateLimitExceeded):
                self.client.fetch_order_book("ETH/USDT")
            mock_sleep.assert_called_with(60)

    def test_base_error_reraises(self):
        import ccxt

        self.mock_ex.fetch_order_book.side_effect = ccxt.BaseError("unknown error")
        with pytest.raises(ccxt.BaseError):
            self.client.fetch_order_book("ETH/USDT")

    def test_network_error_reraises(self):
        import ccxt

        self.mock_ex.fetch_order_book.side_effect = ccxt.NetworkError("timeout")
        with pytest.raises(ccxt.NetworkError):
            self.client.fetch_order_book("ETH/USDT")

    def test_unknown_method_weight_defaults_to_one(self):
        # Method not in _ENDPOINT_WEIGHTS — weight defaults to 1
        self.mock_ex.some_unknown_method = MagicMock(return_value={})
        before = self.client._weight_used
        self.client._call("some_unknown_method")
        assert self.client._weight_used == before + 1


# ── 11. _consume_weight edge cases ────────────────────────────────────────────


class TestConsumeWeightEdgeCases:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_no_sleep_when_reset_already_passed(self):
        # Budget exhausted but reset time already passed — no sleep needed
        self.client._weight_used = 1079
        self.client._weight_reset_at = time.monotonic() - 1  # already expired

        with patch("time.sleep") as mock_sleep:
            self.client._consume_weight("fetch_order_book")
            mock_sleep.assert_not_called()

    def test_weight_reset_resets_used_counter(self):
        self.client._weight_used = 999
        self.client._weight_reset_at = time.monotonic() - 1  # expired
        self.client._consume_weight("fetch_time")
        # After reset + one call, weight should equal just this call's weight
        assert self.client._weight_used == 1  # fetch_time weight = 1

    def test_sleep_for_positive_remaining_window(self):
        self.client._weight_used = 1079  # one below safety
        self.client._weight_reset_at = time.monotonic() + 10.0  # 10s left

        with patch("time.sleep") as mock_sleep:
            self.client._consume_weight("fetch_order_book")  # weight 5, pushes over
            mock_sleep.assert_called_once()
            sleep_arg = mock_sleep.call_args[0][0]
            assert sleep_arg > 0


# ── 12. fetch_order_book timestamp fallback ───────────────────────────────────


class TestOrderBookTimestampFallback:
    def setup_method(self):
        self.client, self.mock_ex = _make_client()

    def test_uses_current_time_when_timestamp_none(self):
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": None,
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
        }
        before = int(time.time() * 1000)
        book = self.client.fetch_order_book("ETH/USDT")
        assert book["timestamp"] >= before

    def test_uses_current_time_when_timestamp_zero(self):
        self.mock_ex.fetch_order_book.return_value = {
            "symbol": "ETH/USDT",
            "timestamp": 0,
            "bids": [[2000.0, 1.0]],
            "asks": [[2001.0, 1.0]],
        }
        before = int(time.time() * 1000)
        book = self.client.fetch_order_book("ETH/USDT")
        assert book["timestamp"] >= before


# ── 13. _normalise_order — remaining branches ─────────────────────────────────


class TestNormaliseOrderRemainingBranches:
    def setup_method(self):
        self.client, _ = _make_client()

    def test_time_in_force_from_info_when_top_level_missing(self):
        raw = _raw_order()
        del raw["timeInForce"]
        raw["info"] = {"timeInForce": "GTC"}
        result = self.client._normalise_order(raw)
        assert result["time_in_force"] == "GTC"

    def test_timestamp_fallback_when_none(self):
        raw = _raw_order()
        raw["timestamp"] = None
        before = int(time.time() * 1000)
        result = self.client._normalise_order(raw)
        assert result["timestamp"] >= before

    def test_timestamp_fallback_when_zero(self):
        raw = _raw_order()
        raw["timestamp"] = 0
        before = int(time.time() * 1000)
        result = self.client._normalise_order(raw)
        assert result["timestamp"] >= before

    def test_status_unknown_when_empty(self):
        raw = _raw_order()
        raw["status"] = ""
        result = self.client._normalise_order(raw)
        assert result["status"] == "unknown"

    def test_status_unknown_when_none(self):
        raw = _raw_order()
        raw["status"] = None
        result = self.client._normalise_order(raw)
        assert result["status"] == "unknown"

    def test_expired_status_from_expired_string(self):
        raw = _raw_order(status="expired", filled=0.0)
        result = self.client._normalise_order(raw)
        assert result["status"] == "expired"

    def test_average_price_none_and_price_none_gives_zero(self):
        raw = _raw_order()
        raw["average"] = None
        raw["price"] = None
        result = self.client._normalise_order(raw)
        assert result["avg_fill_price"] == Decimal("0")

    def test_missing_id_gives_empty_string(self):
        raw = _raw_order()
        del raw["id"]
        result = self.client._normalise_order(raw)
        assert result["id"] == ""


# ── _to_decimal helper ─────────────────────────────────────────────────────────


class TestToDecimal:
    def test_float_input(self):
        assert _to_decimal(1.5) == Decimal("1.5")

    def test_string_input(self):
        assert _to_decimal("2000.5") == Decimal("2000.5")

    def test_none_returns_zero(self):
        assert _to_decimal(None) == Decimal("0")

    def test_invalid_string_returns_zero(self):
        assert _to_decimal("not_a_number") == Decimal("0")

    def test_integer_input(self):
        assert _to_decimal(42) == Decimal("42")
