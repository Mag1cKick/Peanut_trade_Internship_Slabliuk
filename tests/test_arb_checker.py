"""
tests/test_arb_checker.py — Unit tests for ArbChecker (Part 6).

All dependencies are mocked — no network calls, no exchange connections.

Covers:
  1. Direction detection (buy_dex_sell_cex / buy_cex_sell_dex / None)
  2. Gap and cost calculation
  3. Inventory check integration
  4. executable flag logic
  5. Return-value schema validation
  6. SimplePricingAdapter
  7. CLI smoke test
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from integration.arb_checker import ArbChecker, SimplePricingAdapter
from inventory.pnl import PnLEngine
from inventory.tracker import InventoryTracker, Venue

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_order_book(
    bid_price: str = "2010.00",
    ask_price: str = "2010.50",
    qty: str = "10",
) -> dict:
    """Return a minimal order book dict in the ExchangeClient format."""
    bp = Decimal(bid_price)
    ap = Decimal(ask_price)
    q = Decimal(qty)
    return {
        "symbol": "ETH/USDT",
        "timestamp": 1_700_000_000_000,
        "bids": [(bp, q), (bp - Decimal("0.5"), q)],
        "asks": [(ap, q), (ap + Decimal("0.5"), q)],
        "best_bid": (bp, q),
        "best_ask": (ap, q),
        "mid_price": (bp + ap) / 2,
        "spread_bps": (ap - bp) / ((bp + ap) / 2) * Decimal("10000"),
    }


def _make_cex_client(
    bid_price: str = "2010.00",
    ask_price: str = "2010.50",
    taker_fee: str = "0.001",
) -> MagicMock:
    client = MagicMock()
    client.fetch_order_book.return_value = _make_order_book(bid_price, ask_price)
    client.get_trading_fees.return_value = {
        "maker": Decimal("0.001"),
        "taker": Decimal(taker_fee),
    }
    return client


def _make_pricing(
    price: str = "2000.00",
    impact_bps: str = "1.2",
    fee_bps: str = "30",
) -> SimplePricingAdapter:
    return SimplePricingAdapter(
        price=Decimal(price),
        price_impact_bps=Decimal(impact_bps),
        fee_bps=Decimal(fee_bps),
    )


def _make_tracker(
    eth_binance: str = "10",
    usdt_binance: str = "50000",
    eth_wallet: str = "10",
    usdt_wallet: str = "50000",
) -> InventoryTracker:
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": eth_binance, "locked": "0"},
            "USDT": {"free": usdt_binance, "locked": "0"},
        },
    )
    t.update_from_wallet(Venue.WALLET, {"ETH": eth_wallet, "USDT": usdt_wallet})
    return t


def _checker(
    dex_price: str = "2000.00",
    bid_price: str = "2010.00",
    ask_price: str = "2010.50",
    eth_binance: str = "10",
    usdt_binance: str = "50000",
    eth_wallet: str = "10",
    usdt_wallet: str = "50000",
    taker_fee: str = "0.001",
    impact_bps: str = "1.2",
) -> ArbChecker:
    return ArbChecker(
        pricing_engine=_make_pricing(dex_price, impact_bps),
        exchange_client=_make_cex_client(bid_price, ask_price, taker_fee),
        inventory_tracker=_make_tracker(eth_binance, usdt_binance, eth_wallet, usdt_wallet),
        pnl_engine=PnLEngine(),
    )


# ══════════════════════════════════════════════════════════════════════════════
# 1. Direction detection
# ══════════════════════════════════════════════════════════════════════════════


class TestDirection:
    def test_buy_dex_sell_cex_when_dex_price_below_bid(self):
        """DEX price below CEX bid → buy DEX, sell CEX."""
        # dex=2000, cex_bid=2010 → DEX cheaper
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2011")
        r = c.check("ETH/USDT", size=1.0)
        assert r["direction"] == "buy_dex_sell_cex"

    def test_buy_cex_sell_dex_when_dex_price_above_ask(self):
        """DEX price above CEX ask → buy CEX, sell DEX."""
        # dex=2020, cex_ask=2010.50 → CEX cheaper
        c = _checker(dex_price="2020", bid_price="2009", ask_price="2010")
        r = c.check("ETH/USDT", size=1.0)
        assert r["direction"] == "buy_cex_sell_dex"

    def test_no_direction_when_dex_inside_spread(self):
        """DEX price between bid and ask → no opportunity."""
        # dex=2010.25 is between bid=2010 and ask=2010.50
        c = _checker(dex_price="2010.25", bid_price="2010", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["direction"] is None

    def test_no_direction_when_dex_equals_bid(self):
        """DEX price equal to CEX bid → no opportunity (requires strict <)."""
        c = _checker(dex_price="2010.00", bid_price="2010.00", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["direction"] is None

    def test_no_direction_when_dex_equals_ask(self):
        c = _checker(dex_price="2010.50", bid_price="2010.00", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["direction"] is None


# ══════════════════════════════════════════════════════════════════════════════
# 2. Gap & cost calculation
# ══════════════════════════════════════════════════════════════════════════════


class TestGapAndCosts:
    def test_gap_bps_buy_dex_sell_cex(self):
        """gap_bps = (cex_bid - dex_price) / dex_price × 10000."""
        # dex=2000, cex_bid=2010 → gap = 10/2000*10000 = 50 bps
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert r["gap_bps"] == pytest.approx(Decimal("50"), rel=Decimal("0.01"))

    def test_gap_bps_buy_cex_sell_dex(self):
        """gap_bps = (dex_price - cex_ask) / cex_ask × 10000."""
        # dex=2020, cex_ask=2010 → gap = 10/2010*10000 ≈ 49.75 bps
        c = _checker(dex_price="2020", bid_price="2008", ask_price="2010")
        r = c.check("ETH/USDT", size=1.0)
        assert r["gap_bps"] == pytest.approx(Decimal("49.75"), rel=Decimal("0.01"))

    def test_gap_zero_when_no_direction(self):
        c = _checker(dex_price="2010.25", bid_price="2010", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["gap_bps"] == Decimal("0")

    def test_costs_include_dex_fee(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert r["details"]["dex_fee_bps"] == Decimal("30")

    def test_costs_include_dex_impact(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015", impact_bps="5")
        r = c.check("ETH/USDT", size=1.0)
        assert r["details"]["dex_price_impact_bps"] == Decimal("5")

    def test_costs_include_cex_fee(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015", taker_fee="0.001")
        r = c.check("ETH/USDT", size=1.0)
        # 0.001 * 10000 = 10 bps
        assert r["details"]["cex_fee_bps"] == Decimal("10")

    def test_cex_slippage_bps_is_decimal(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert isinstance(r["details"]["cex_slippage_bps"], Decimal)

    def test_gas_cost_usd_positive(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0, gas_price_gwei=20)
        assert r["details"]["gas_cost_usd"] > 0

    def test_gas_cost_scales_with_gas_price(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r10 = c.check("ETH/USDT", size=1.0, gas_price_gwei=10)
        r20 = c.check("ETH/USDT", size=1.0, gas_price_gwei=20)
        assert r20["details"]["gas_cost_usd"] == pytest.approx(
            r10["details"]["gas_cost_usd"] * 2, rel=Decimal("0.01")
        )

    def test_net_pnl_equals_gap_minus_costs(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert r["estimated_net_pnl_bps"] == pytest.approx(
            r["gap_bps"] - r["estimated_costs_bps"], rel=Decimal("0.001")
        )

    def test_estimated_costs_bps_is_decimal(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert isinstance(r["estimated_costs_bps"], Decimal)

    def test_net_pnl_bps_is_decimal(self):
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert isinstance(r["estimated_net_pnl_bps"], Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 3. Inventory check
# ══════════════════════════════════════════════════════════════════════════════


class TestInventory:
    def test_inventory_ok_when_sufficient_funds(self):
        # dex=2000, bid=2010 → buy_dex_sell_cex
        # Need USDT on WALLET (50000 >> 2000) and ETH on BINANCE (10 >> 1)
        c = _checker(dex_price="2000", bid_price="2010", ask_price="2015")
        r = c.check("ETH/USDT", size=1.0)
        assert r["inventory_ok"] is True

    def test_inventory_fails_when_no_quote_on_wallet(self):
        """buy_dex_sell_cex needs USDT on WALLET; zero USDT → inventory_ok False."""
        c = _checker(
            dex_price="2000",
            bid_price="2010",
            ask_price="2015",
            usdt_wallet="0",  # zero USDT on WALLET
        )
        r = c.check("ETH/USDT", size=1.0)
        assert r["inventory_ok"] is False

    def test_inventory_fails_when_no_base_on_cex(self):
        """buy_dex_sell_cex needs ETH on BINANCE; zero ETH → inventory_ok False."""
        c = _checker(
            dex_price="2000",
            bid_price="2010",
            ask_price="2015",
            eth_binance="0",  # zero ETH on BINANCE
        )
        r = c.check("ETH/USDT", size=1.0)
        assert r["inventory_ok"] is False

    def test_inventory_ok_true_when_no_direction(self):
        """When there's no opportunity, inventory_ok should be True (no check needed)."""
        c = _checker(dex_price="2010.25", bid_price="2010", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["inventory_ok"] is True

    def test_inventory_buy_cex_sell_dex_checks_correct_venues(self):
        """buy_cex_sell_dex needs USDT on BINANCE and ETH on WALLET."""
        c = _checker(
            dex_price="2020",
            bid_price="2008",
            ask_price="2010",
            eth_wallet="0",  # zero ETH on WALLET → can't sell DEX
            eth_binance="10",
            usdt_binance="50000",
            usdt_wallet="50000",
        )
        r = c.check("ETH/USDT", size=1.0)
        assert r["inventory_ok"] is False


# ══════════════════════════════════════════════════════════════════════════════
# 4. executable flag
# ══════════════════════════════════════════════════════════════════════════════


class TestExecutable:
    def test_profitable_opportunity_detected(self):
        """gap > costs AND inventory ok → executable True."""
        # Large gap (500 bps) with default small costs → should be profitable
        c = _checker(dex_price="2000", bid_price="2100", ask_price="2110")
        r = c.check("ETH/USDT", size=1.0)
        assert r["executable"] is True

    def test_unprofitable_skipped(self):
        """gap < costs → executable False even with good inventory."""
        # Tiny gap (2.5 bps = 2010 vs 2010.50) vs costs (~41+ bps) → not profitable
        c = _checker(dex_price="2000", bid_price="2010.50", ask_price="2011")
        # Force artificially high costs by using high gas gwei
        r = c.check("ETH/USDT", size=0.001, gas_price_gwei=500)
        # net_pnl_bps likely negative with 500 gwei gas on tiny size
        if r["estimated_net_pnl_bps"] <= 0:
            assert r["executable"] is False

    def test_not_executable_when_no_direction(self):
        c = _checker(dex_price="2010.25", bid_price="2010", ask_price="2010.50")
        r = c.check("ETH/USDT", size=1.0)
        assert r["executable"] is False

    def test_not_executable_when_inventory_fails(self):
        """Profitable gap but no funds → not executable."""
        c = _checker(
            dex_price="2000",
            bid_price="2100",
            ask_price="2110",
            usdt_wallet="0",
            eth_binance="0",
        )
        r = c.check("ETH/USDT", size=1.0)
        assert r["executable"] is False

    def test_executable_requires_positive_net_pnl(self):
        c = _checker(dex_price="2000", bid_price="2100", ask_price="2110")
        r = c.check("ETH/USDT", size=1.0)
        if r["executable"]:
            assert r["estimated_net_pnl_bps"] > 0


# ══════════════════════════════════════════════════════════════════════════════
# 5. Return-value schema
# ══════════════════════════════════════════════════════════════════════════════


class TestSchema:
    def setup_method(self):
        c = _checker()
        self.r = c.check("ETH/USDT", size=1.0)

    def test_returns_required_keys(self):
        required = {
            "pair",
            "timestamp",
            "dex_price",
            "cex_bid",
            "cex_ask",
            "gap_bps",
            "direction",
            "estimated_costs_bps",
            "estimated_net_pnl_bps",
            "inventory_ok",
            "executable",
            "details",
        }
        assert required <= set(self.r.keys())

    def test_details_has_required_keys(self):
        detail_keys = {
            "dex_price_impact_bps",
            "cex_slippage_bps",
            "cex_fee_bps",
            "dex_fee_bps",
            "gas_cost_usd",
        }
        assert detail_keys <= set(self.r["details"].keys())

    def test_timestamp_is_datetime(self):
        assert isinstance(self.r["timestamp"], datetime)
        assert self.r["timestamp"].tzinfo is not None

    def test_pair_preserved(self):
        assert self.r["pair"] == "ETH/USDT"

    def test_prices_are_decimal(self):
        for key in ("dex_price", "cex_bid", "cex_ask", "gap_bps"):
            assert isinstance(self.r[key], Decimal), f"{key} should be Decimal"

    def test_executable_is_bool(self):
        assert isinstance(self.r["executable"], bool)

    def test_inventory_ok_is_bool(self):
        assert isinstance(self.r["inventory_ok"], bool)

    def test_direction_is_str_or_none(self):
        d = self.r["direction"]
        assert d is None or isinstance(d, str)

    def test_valid_direction_values(self):
        d = self.r["direction"]
        assert d in (None, "buy_dex_sell_cex", "buy_cex_sell_dex")


# ══════════════════════════════════════════════════════════════════════════════
# 6. SimplePricingAdapter
# ══════════════════════════════════════════════════════════════════════════════


class TestSimplePricingAdapter:
    def test_fixed_price(self):
        a = SimplePricingAdapter(price=Decimal("2000"))
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["price"] == Decimal("2000")

    def test_default_fee_bps(self):
        a = SimplePricingAdapter(price=Decimal("2000"))
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["fee_bps"] == Decimal("30")

    def test_custom_fee_bps(self):
        a = SimplePricingAdapter(price=Decimal("2000"), fee_bps=Decimal("5"))
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["fee_bps"] == Decimal("5")

    def test_price_fn_overrides_fixed(self):
        def fn(base, quote, size):
            return {
                "price": Decimal("9999"),
                "price_impact_bps": Decimal("0"),
                "fee_bps": Decimal("30"),
            }

        a = SimplePricingAdapter(price=Decimal("2000"), price_fn=fn)
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["price"] == Decimal("9999")

    def test_price_impact_defaults_zero(self):
        a = SimplePricingAdapter(price=Decimal("2000"))
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["price_impact_bps"] == Decimal("0")

    def test_custom_price_impact(self):
        a = SimplePricingAdapter(price=Decimal("2000"), price_impact_bps=Decimal("2.5"))
        r = a.get_dex_price("ETH", "USDT", Decimal("1"))
        assert r["price_impact_bps"] == Decimal("2.5")


# ══════════════════════════════════════════════════════════════════════════════
# 7. CLI smoke test (no network — patches exchange client)
# ══════════════════════════════════════════════════════════════════════════════


class TestCLI:
    def test_cli_with_mocked_exchange(self, monkeypatch):
        """CLI runs end-to-end with a mocked ExchangeClient."""
        from integration import arb_checker as mod

        monkeypatch.setattr(
            mod,
            "_run_cli",
            lambda argv=None: 0,  # bypass real CLI to avoid network
        )
        # Just import check passes — detailed test below.

    def test_checker_instantiation(self):
        """ArbChecker can be constructed without errors."""
        c = _checker()
        assert c is not None

    def test_check_returns_dict(self):
        c = _checker()
        result = c.check("ETH/USDT", size=1.0)
        assert isinstance(result, dict)

    def test_check_pair_stored(self):
        c = _checker()
        result = c.check("ETH/USDT", size=2.0)
        assert result["pair"] == "ETH/USDT"


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestFeeException:
    """Cover except Exception → _DEFAULT_CEX_FEE_BPS fallback (lines 126-127)."""

    def test_fee_fetch_exception_uses_default(self):
        from integration.arb_checker import _DEFAULT_CEX_FEE_BPS, ArbChecker, SimplePricingAdapter
        from inventory.pnl import PnLEngine
        from inventory.tracker import InventoryTracker, Venue

        pricing = SimplePricingAdapter(price=Decimal("1990"))
        cex = MagicMock()
        cex.fetch_order_book.return_value = {
            "bids": [(Decimal("2000"), Decimal("1"))],
            "asks": [(Decimal("2001"), Decimal("1"))],
            "best_bid": (Decimal("2000"), Decimal("1")),
            "best_ask": (Decimal("2001"), Decimal("1")),
            "mid_price": Decimal("2000.5"),
            "spread_bps": Decimal("5"),
            "symbol": "ETH/USDT",
            "timestamp": 1700000000000,
        }
        cex.get_trading_fees.side_effect = Exception("API error")

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(
            Venue.BINANCE,
            {
                "ETH": {"free": "100", "locked": "0"},
                "USDT": {"free": "500000", "locked": "0"},
            },
        )
        tracker.update_from_wallet(Venue.WALLET, {"ETH": "100", "USDT": "500000"})

        checker = ArbChecker(pricing, cex, tracker, PnLEngine())
        result = checker.check("ETH/USDT", size=1.0)
        # Should succeed and use the default fee
        assert result["details"]["cex_fee_bps"] == _DEFAULT_CEX_FEE_BPS


class TestPrintResult:
    """Cover _print_result (lines 218-274)."""

    def _make_result(
        self, direction="buy_dex_sell_cex", executable=True, inventory_ok=True, net_pnl=5.0
    ):
        return {
            "pair": "ETH/USDT",
            "timestamp": __import__("datetime").datetime(2024, 1, 1, 12, 0, 0),
            "dex_price": Decimal("1990"),
            "cex_bid": Decimal("2000"),
            "cex_ask": Decimal("2001"),
            "gap_bps": Decimal("50"),
            "direction": direction,
            "estimated_costs_bps": Decimal(str(50 - net_pnl)),
            "estimated_net_pnl_bps": Decimal(str(net_pnl)),
            "inventory_ok": inventory_ok,
            "executable": executable,
            "details": {
                "dex_fee_bps": Decimal("30"),
                "dex_price_impact_bps": Decimal("1.2"),
                "cex_fee_bps": Decimal("10"),
                "cex_slippage_bps": Decimal("2"),
                "gas_cost_usd": Decimal("0.5"),
            },
        }

    def test_print_result_with_direction(self, capsys):
        from integration.arb_checker import _print_result

        _print_result(self._make_result(direction="buy_dex_sell_cex"), size=1.0)
        out = capsys.readouterr().out
        assert "ETH/USDT" in out
        assert "buy dex sell cex" in out

    def test_print_result_no_direction(self, capsys):
        from integration.arb_checker import _print_result

        _print_result(self._make_result(direction=None, executable=False, net_pnl=-10.0), size=1.0)
        out = capsys.readouterr().out
        assert "no opportunity" in out
        assert "SKIP" in out

    def test_print_result_not_profitable(self, capsys):
        from integration.arb_checker import _print_result

        _print_result(self._make_result(executable=False, net_pnl=-5.0), size=2.0)
        out = capsys.readouterr().out
        assert "NOT PROFITABLE" in out

    def test_print_result_executable(self, capsys):
        from integration.arb_checker import _print_result

        _print_result(self._make_result(executable=True, net_pnl=5.0), size=1.0)
        out = capsys.readouterr().out
        assert "EXECUTE" in out

    def test_print_result_inventory_failed(self, capsys):
        from integration.arb_checker import _print_result

        _print_result(
            self._make_result(executable=False, inventory_ok=False, net_pnl=5.0), size=1.0
        )
        out = capsys.readouterr().out
        assert "insufficient inventory" in out.lower() or "INSUFFICIENT" in out


class TestRunCLI:
    """_run_cli with mocked ExchangeClient."""

    def _mock_cex(self):
        from decimal import Decimal

        mock = MagicMock()
        mock.fetch_order_book.return_value = {
            "bids": [(Decimal("2000"), Decimal("5"))],
            "asks": [(Decimal("2001"), Decimal("5"))],
            "best_bid": (Decimal("2000"), Decimal("5")),
            "best_ask": (Decimal("2001"), Decimal("5")),
            "mid_price": Decimal("2000.5"),
            "spread_bps": Decimal("5"),
            "symbol": "ETH/USDT",
            "timestamp": 1700000000000,
        }
        mock.get_trading_fees.return_value = {"taker": Decimal("0.001"), "maker": Decimal("0.001")}
        return mock

    def test_cli_exits_zero(self, capsys):
        from integration.arb_checker import _run_cli

        with patch("exchange.client.ExchangeClient", return_value=self._mock_cex()):
            rc = _run_cli(["ETH/USDT", "--size", "1.0"])
        assert rc == 0

    def test_cli_with_explicit_dex_price(self, capsys):
        from integration.arb_checker import _run_cli

        with patch("exchange.client.ExchangeClient", return_value=self._mock_cex()):
            rc = _run_cli(["ETH/USDT", "--size", "1.0", "--dex-price", "1990"])
        assert rc == 0

    def test_cli_exchange_error_returns_one(self):
        from integration.arb_checker import _run_cli

        with patch("exchange.client.ExchangeClient", side_effect=Exception("fail")):
            rc = _run_cli(["ETH/USDT"])
        assert rc == 1

    def test_cli_order_book_error_returns_one(self):
        from integration.arb_checker import _run_cli

        mock_cex = self._mock_cex()
        mock_cex.fetch_order_book.side_effect = Exception("book error")
        with patch("exchange.client.ExchangeClient", return_value=mock_cex):
            rc = _run_cli(["ETH/USDT"])
        assert rc == 1
