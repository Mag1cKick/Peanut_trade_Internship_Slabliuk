"""
tests/test_multi_venue_tracker.py — Unit tests for InventoryTracker (multi-venue)

Covers all methods in the new InventoryTracker:
  1. Construction
  2. update_from_cex / update_from_wallet
  3. snapshot — aggregates across venues
  4. get_available
  5. can_execute — pass/fail pre-flight checks
  6. record_trade — buy/sell balance updates, fees
  7. skew — balanced / imbalanced distributions
  8. get_skews — all assets, alphabetical order
  9. Balance dataclass
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from inventory.tracker import Balance, InventoryTracker, Venue

# ── Helpers ────────────────────────────────────────────────────────────────────


def _tracker() -> InventoryTracker:
    return InventoryTracker([Venue.BINANCE, Venue.WALLET])


def _cex_balances() -> dict:
    """Simulates ExchangeClient.fetch_balance() output."""
    return {
        "ETH": {"free": Decimal("10"), "locked": Decimal("2"), "total": Decimal("12")},
        "USDT": {"free": Decimal("20000"), "locked": Decimal("0"), "total": Decimal("20000")},
    }


def _wallet_balances() -> dict:
    """Simulates on-chain wallet query output."""
    return {
        "ETH": Decimal("8"),
        "WETH": Decimal("3"),
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. Construction
# ══════════════════════════════════════════════════════════════════════════════


class TestConstruction:
    def test_creates_with_venues(self):
        t = _tracker()
        assert t is not None

    def test_single_venue(self):
        t = InventoryTracker([Venue.BINANCE])
        assert t is not None

    def test_empty_venues(self):
        t = InventoryTracker([])
        assert t.snapshot()["totals"] == {}


# ══════════════════════════════════════════════════════════════════════════════
# 2. update_from_cex / update_from_wallet
# ══════════════════════════════════════════════════════════════════════════════


class TestUpdateFromCex:
    def setup_method(self):
        self.t = _tracker()

    def test_update_stores_balances(self):
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("10")

    def test_update_replaces_previous_snapshot(self):
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())
        self.t.update_from_cex(
            Venue.BINANCE,
            {"BTC": {"free": Decimal("1"), "locked": Decimal("0"), "total": Decimal("1")}},
        )
        # Old assets gone, new ones present
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("0")
        assert self.t.get_available(Venue.BINANCE, "BTC") == Decimal("1")

    def test_non_dict_values_skipped(self):
        raw = {
            "ETH": {"free": Decimal("5"), "locked": Decimal("0"), "total": Decimal("5")},
            "info": "metadata",
        }
        self.t.update_from_cex(Venue.BINANCE, raw)
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("5")

    def test_locked_stored_separately(self):
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())
        snap = self.t.snapshot()
        eth = snap["venues"]["binance"]["ETH"]
        assert eth["locked"] == Decimal("2")
        assert eth["total"] == Decimal("12")


class TestUpdateFromWallet:
    def setup_method(self):
        self.t = _tracker()

    def test_update_stores_balances(self):
        self.t.update_from_wallet(Venue.WALLET, _wallet_balances())
        assert self.t.get_available(Venue.WALLET, "ETH") == Decimal("8")

    def test_all_wallet_funds_are_free(self):
        self.t.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
        snap = self.t.snapshot()
        eth = snap["venues"]["wallet"]["ETH"]
        assert eth["free"] == Decimal("5")
        assert eth["locked"] == Decimal("0")

    def test_update_replaces_previous(self):
        self.t.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5")})
        self.t.update_from_wallet(Venue.WALLET, {"WETH": Decimal("3")})
        assert self.t.get_available(Venue.WALLET, "ETH") == Decimal("0")
        assert self.t.get_available(Venue.WALLET, "WETH") == Decimal("3")


# ══════════════════════════════════════════════════════════════════════════════
# 3. snapshot
# ══════════════════════════════════════════════════════════════════════════════


class TestSnapshot:
    def setup_method(self):
        self.t = _tracker()
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())
        self.t.update_from_wallet(Venue.WALLET, _wallet_balances())

    def test_snapshot_aggregates_across_venues(self):
        """Total ETH = Binance ETH (free+locked=12) + Wallet ETH (8)."""
        snap = self.t.snapshot()
        assert snap["totals"]["ETH"] == Decimal("20")

    def test_snapshot_includes_all_venues(self):
        snap = self.t.snapshot()
        assert "binance" in snap["venues"]
        assert "wallet" in snap["venues"]

    def test_snapshot_includes_timestamp(self):
        snap = self.t.snapshot()
        assert isinstance(snap["timestamp"], datetime)

    def test_snapshot_totals_wallet_only_asset(self):
        snap = self.t.snapshot()
        assert snap["totals"]["WETH"] == Decimal("3")

    def test_snapshot_totals_cex_only_asset(self):
        snap = self.t.snapshot()
        assert snap["totals"]["USDT"] == Decimal("20000")

    def test_empty_tracker_snapshot(self):
        t = InventoryTracker([Venue.BINANCE])
        snap = t.snapshot()
        assert snap["totals"] == {}
        assert snap["venues"]["binance"] == {}


# ══════════════════════════════════════════════════════════════════════════════
# 4. get_available
# ══════════════════════════════════════════════════════════════════════════════


class TestGetAvailable:
    def setup_method(self):
        self.t = _tracker()
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())

    def test_returns_free_balance(self):
        # ETH: free=10, locked=2 → available=10
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("10")

    def test_unknown_asset_returns_zero(self):
        assert self.t.get_available(Venue.BINANCE, "BTC") == Decimal("0")

    def test_unknown_venue_returns_zero(self):
        assert self.t.get_available(Venue.WALLET, "ETH") == Decimal("0")

    def test_returns_decimal(self):
        assert isinstance(self.t.get_available(Venue.BINANCE, "ETH"), Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 5. can_execute
# ══════════════════════════════════════════════════════════════════════════════


class TestCanExecute:
    def setup_method(self):
        self.t = _tracker()
        self.t.update_from_cex(Venue.BINANCE, _cex_balances())  # ETH free=10, USDT free=20000
        self.t.update_from_wallet(Venue.WALLET, {"ETH": Decimal("8")})

    def test_passes_when_sufficient(self):
        """Returns can_execute=True with enough balance on both sides."""
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("4000"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("2"),
        )
        assert result["can_execute"] is True
        assert result["reason"] is None

    def test_fails_insufficient_buy(self):
        """Returns can_execute=False when buy venue lacks funds."""
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("99999"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("1"),
        )
        assert result["can_execute"] is False
        assert "USDT" in result["reason"]

    def test_fails_insufficient_sell(self):
        """Returns can_execute=False when sell venue lacks asset."""
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("100"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("100"),
        )
        assert result["can_execute"] is False
        assert "ETH" in result["reason"]

    def test_fails_both_legs(self):
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("999999"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("999"),
        )
        assert result["can_execute"] is False

    def test_returns_available_amounts(self):
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("100"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("1"),
        )
        assert result["buy_venue_available"] == Decimal("20000")
        assert result["sell_venue_available"] == Decimal("8")

    def test_needed_amounts_in_result(self):
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("500"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("0.25"),
        )
        assert result["buy_venue_needed"] == Decimal("500")
        assert result["sell_venue_needed"] == Decimal("0.25")

    def test_exact_balance_passes(self):
        result = self.t.can_execute(
            buy_venue=Venue.BINANCE,
            buy_asset="USDT",
            buy_amount=Decimal("20000"),
            sell_venue=Venue.WALLET,
            sell_asset="ETH",
            sell_amount=Decimal("8"),
        )
        assert result["can_execute"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 6. record_trade
# ══════════════════════════════════════════════════════════════════════════════


class TestRecordTrade:
    def setup_method(self):
        self.t = _tracker()
        self.t.update_from_cex(
            Venue.BINANCE,
            {
                "ETH": {"free": Decimal("10"), "locked": Decimal("0"), "total": Decimal("10")},
                "USDT": {
                    "free": Decimal("20000"),
                    "locked": Decimal("0"),
                    "total": Decimal("20000"),
                },
            },
        )

    def test_buy_increases_base_decreases_quote(self):
        """After buy trade: base increases, quote decreases, fee deducted."""
        self.t.record_trade(
            venue=Venue.BINANCE,
            side="buy",
            base_asset="ETH",
            quote_asset="USDT",
            base_amount=Decimal("1"),
            quote_amount=Decimal("2000"),
            fee=Decimal("2"),
            fee_asset="USDT",
        )
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("11")
        # USDT: 20000 - 2000 (quote) - 2 (fee) = 17998
        assert self.t.get_available(Venue.BINANCE, "USDT") == Decimal("17998")

    def test_sell_decreases_base_increases_quote(self):
        self.t.record_trade(
            venue=Venue.BINANCE,
            side="sell",
            base_asset="ETH",
            quote_asset="USDT",
            base_amount=Decimal("2"),
            quote_amount=Decimal("4000"),
            fee=Decimal("4"),
            fee_asset="USDT",
        )
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("8")
        # USDT: 20000 + 4000 (quote) - 4 (fee) = 23996
        assert self.t.get_available(Venue.BINANCE, "USDT") == Decimal("23996")

    def test_fee_deducted_from_fee_asset(self):
        self.t.record_trade(
            venue=Venue.BINANCE,
            side="buy",
            base_asset="ETH",
            quote_asset="USDT",
            base_amount=Decimal("1"),
            quote_amount=Decimal("2000"),
            fee=Decimal("0.001"),
            fee_asset="ETH",  # fee in base asset
        )
        # ETH: 10 + 1 (buy) - 0.001 (fee) = 10.999
        assert self.t.get_available(Venue.BINANCE, "ETH") == Decimal("10.999")

    def test_record_trade_on_unknown_venue_creates_balances(self):
        t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        t.record_trade(
            venue=Venue.WALLET,
            side="buy",
            base_asset="ETH",
            quote_asset="USDT",
            base_amount=Decimal("1"),
            quote_amount=Decimal("2000"),
            fee=Decimal("0"),
            fee_asset="USDT",
        )
        assert t.get_available(Venue.WALLET, "ETH") == Decimal("1")

    def test_zero_fee_does_not_change_balance(self):
        self.t.record_trade(
            venue=Venue.BINANCE,
            side="buy",
            base_asset="ETH",
            quote_asset="USDT",
            base_amount=Decimal("1"),
            quote_amount=Decimal("2000"),
            fee=Decimal("0"),
            fee_asset="USDT",
        )
        assert self.t.get_available(Venue.BINANCE, "USDT") == Decimal("18000")


# ══════════════════════════════════════════════════════════════════════════════
# 7. skew
# ══════════════════════════════════════════════════════════════════════════════


class TestSkew:
    def setup_method(self):
        self.t = _tracker()

    def _load(self, binance_eth: str, wallet_eth: str) -> None:
        self.t.update_from_cex(
            Venue.BINANCE,
            {
                "ETH": {
                    "free": Decimal(binance_eth),
                    "locked": Decimal("0"),
                    "total": Decimal(binance_eth),
                }
            },
        )
        self.t.update_from_wallet(Venue.WALLET, {"ETH": Decimal(wallet_eth)})

    def test_skew_detects_imbalance(self):
        """90/10 split shows >30% deviation (40% from equal 50%)."""
        self._load("9", "1")  # 90% on Binance, 10% on Wallet
        result = self.t.skew("ETH")
        assert result["needs_rebalance"] is True
        assert result["max_deviation_pct"] > 30.0

    def test_skew_balanced(self):
        """50/50 split shows ~0% deviation."""
        self._load("5", "5")
        result = self.t.skew("ETH")
        assert result["needs_rebalance"] is False
        assert result["max_deviation_pct"] < 1.0

    def test_skew_asset_field(self):
        self._load("5", "5")
        assert self.t.skew("ETH")["asset"] == "ETH"

    def test_skew_total(self):
        self._load("8", "2")
        assert self.t.skew("ETH")["total"] == Decimal("10")

    def test_skew_returns_all_venues(self):
        self._load("5", "5")
        result = self.t.skew("ETH")
        assert "binance" in result["venues"]
        assert "wallet" in result["venues"]

    def test_skew_pct_sums_to_100(self):
        self._load("7", "3")
        result = self.t.skew("ETH")
        total_pct = sum(v["pct"] for v in result["venues"].values())
        assert abs(total_pct - 100.0) < 0.001

    def test_skew_unknown_asset_total_zero(self):
        result = self.t.skew("BTC")
        assert result["total"] == Decimal("0")
        assert result["needs_rebalance"] is False

    def test_skew_100_percent_one_venue(self):
        """All on one venue → 50% deviation for 2-venue tracker."""
        self._load("10", "0")
        result = self.t.skew("ETH")
        assert result["needs_rebalance"] is True

    def test_skew_amounts_are_decimal(self):
        self._load("5", "5")
        for v in self.t.skew("ETH")["venues"].values():
            assert isinstance(v["amount"], Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 8. get_skews
# ══════════════════════════════════════════════════════════════════════════════


class TestGetSkews:
    def setup_method(self):
        self.t = _tracker()
        self.t.update_from_cex(
            Venue.BINANCE,
            {
                "ETH": {"free": Decimal("5"), "locked": Decimal("0"), "total": Decimal("5")},
                "USDT": {"free": Decimal("1000"), "locked": Decimal("0"), "total": Decimal("1000")},
            },
        )
        self.t.update_from_wallet(Venue.WALLET, {"ETH": Decimal("3"), "WETH": Decimal("2")})

    def test_get_skews_returns_all_assets(self):
        """get_skews() returns one entry per tracked asset."""
        skews = self.t.get_skews()
        assets = {s["asset"] for s in skews}
        assert assets == {"ETH", "USDT", "WETH"}

    def test_get_skews_sorted_alphabetically(self):
        skews = self.t.get_skews()
        names = [s["asset"] for s in skews]
        assert names == sorted(names)

    def test_get_skews_schema_matches_skew(self):
        skews = self.t.get_skews()
        for s in skews:
            assert "asset" in s
            assert "total" in s
            assert "venues" in s
            assert "max_deviation_pct" in s
            assert "needs_rebalance" in s

    def test_get_skews_empty_tracker(self):
        t = InventoryTracker([Venue.BINANCE])
        assert t.get_skews() == []

    def test_get_skews_weth_only_on_wallet(self):
        skews = self.t.get_skews()
        weth = next(s for s in skews if s["asset"] == "WETH")
        # WETH only on WALLET → 100% on one side → needs rebalance
        assert weth["needs_rebalance"] is True


# ══════════════════════════════════════════════════════════════════════════════
# 9. Balance dataclass
# ══════════════════════════════════════════════════════════════════════════════


class TestBalanceDataclass:
    def test_total_property(self):
        b = Balance(venue=Venue.BINANCE, asset="ETH", free=Decimal("10"), locked=Decimal("2"))
        assert b.total == Decimal("12")

    def test_default_locked_is_zero(self):
        b = Balance(venue=Venue.BINANCE, asset="ETH", free=Decimal("5"))
        assert b.locked == Decimal("0")
        assert b.total == Decimal("5")

    def test_venue_enum_value(self):
        assert Venue.BINANCE.value == "binance"
        assert Venue.WALLET.value == "wallet"

    def test_balance_fields_are_decimal(self):
        b = Balance(venue=Venue.WALLET, asset="ETH", free=Decimal("3"))
        assert isinstance(b.free, Decimal)
        assert isinstance(b.total, Decimal)
