"""
tests/test_pnl.py — Unit tests for PnLEngine (Part 5).

Covers:
  1. TradeLeg / ArbRecord properties
  2. PnLEngine.record() and .trades
  3. PnLEngine.summary() — all fields including edge cases
  4. PnLEngine.recent()
  5. PnLEngine.export_csv()
  6. CLI smoke tests

No network calls, no external dependencies.
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
from inventory.tracker import Venue

# ── Helpers ────────────────────────────────────────────────────────────────────

_TS = datetime(2024, 1, 15, 14, 30, 0, tzinfo=UTC)


def _leg(
    side: str,
    price: str,
    amount: str = "1",
    fee: str = "0.40",
    venue: Venue = Venue.BINANCE,
    ts: datetime | None = None,
) -> TradeLeg:
    return TradeLeg(
        id=f"{side}-leg",
        timestamp=ts or _TS,
        venue=venue,
        symbol="ETH/USDT",
        side=side,
        amount=Decimal(amount),
        price=Decimal(price),
        fee=Decimal(fee),
        fee_asset="USDT",
    )


def _arb(
    buy_price: str = "2000",
    sell_price: str = "2001.25",
    amount: str = "1",
    buy_fee: str = "0.40",
    sell_fee: str = "0.40",
    gas: str = "0",
    ts: datetime | None = None,
) -> ArbRecord:
    t = ts or _TS
    return ArbRecord(
        id="arb-1",
        timestamp=t,
        buy_leg=_leg("buy", buy_price, amount, buy_fee, Venue.WALLET, t),
        sell_leg=_leg("sell", sell_price, amount, sell_fee, Venue.BINANCE, t),
        gas_cost_usd=Decimal(gas),
    )


def _engine_with_trades(*args: ArbRecord) -> PnLEngine:
    engine = PnLEngine()
    for a in args:
        engine.record(a)
    return engine


# ══════════════════════════════════════════════════════════════════════════════
# 1. TradeLeg / ArbRecord
# ══════════════════════════════════════════════════════════════════════════════


class TestTradeLeg:
    def test_fields_stored(self):
        leg = _leg("buy", "2000")
        assert leg.side == "buy"
        assert leg.price == Decimal("2000")
        assert leg.amount == Decimal("1")
        assert leg.symbol == "ETH/USDT"
        assert leg.venue == Venue.BINANCE

    def test_fee_stored(self):
        leg = _leg("sell", "2001", fee="0.50")
        assert leg.fee == Decimal("0.50")

    def test_fee_asset_stored(self):
        leg = _leg("buy", "2000")
        assert leg.fee_asset == "USDT"


class TestArbRecordProperties:
    def test_gross_pnl_calculation(self):
        """Gross PnL = sell revenue − buy cost."""
        a = _arb(buy_price="2000", sell_price="2001.25", amount="1")
        # sell: 2001.25 × 1 = 2001.25; buy: 2000 × 1 = 2000
        assert a.gross_pnl == Decimal("1.25")

    def test_gross_pnl_multi_amount(self):
        a = _arb(buy_price="2000", sell_price="2002", amount="2")
        # (2002 - 2000) × 2 = 4
        assert a.gross_pnl == Decimal("4")

    def test_gross_pnl_negative_when_buy_above_sell(self):
        a = _arb(buy_price="2001", sell_price="2000", amount="1")
        assert a.gross_pnl == Decimal("-1")

    def test_net_pnl_includes_all_fees(self):
        """Net PnL = gross − buy fee − sell fee − gas."""
        a = _arb(buy_price="2000", sell_price="2002", buy_fee="0.40", sell_fee="0.40", gas="0.20")
        # gross = 2; fees = 0.40 + 0.40 + 0.20 = 1.00
        assert a.net_pnl == Decimal("1.00")

    def test_net_pnl_no_gas(self):
        a = _arb(buy_price="2000", sell_price="2001.25", buy_fee="0.40", sell_fee="0.40", gas="0")
        # gross 1.25 - 0.80 fees = 0.45
        assert a.net_pnl == Decimal("0.45")

    def test_total_fees_sum(self):
        a = _arb(buy_fee="0.40", sell_fee="0.60", gas="0.10")
        assert a.total_fees == Decimal("1.10")

    def test_notional(self):
        a = _arb(buy_price="2000", amount="2")
        assert a.notional == Decimal("4000")

    def test_net_pnl_bps_calculation(self):
        """PnL bps = net_pnl / notional × 10000."""
        a = _arb(
            buy_price="2000",
            sell_price="2002",
            amount="1",
            buy_fee="0.40",
            sell_fee="0.40",
            gas="0",
        )
        # gross=2, fees=0.80, net=1.20, notional=2000
        # bps = 1.20 / 2000 * 10000 = 6.0
        assert a.net_pnl_bps == Decimal("6.0")

    def test_net_pnl_bps_zero_notional(self):
        """No division by zero when notional is 0."""
        a = _arb(buy_price="0", sell_price="1", amount="1")
        assert a.net_pnl_bps == Decimal("0")

    def test_gas_cost_default_zero(self):
        t = _TS
        arb = ArbRecord(
            id="x",
            timestamp=t,
            buy_leg=_leg("buy", "2000", ts=t),
            sell_leg=_leg("sell", "2001", ts=t),
        )
        assert arb.gas_cost_usd == Decimal("0")

    def test_net_pnl_is_decimal(self):
        a = _arb()
        assert isinstance(a.net_pnl, Decimal)

    def test_net_pnl_bps_is_decimal(self):
        a = _arb()
        assert isinstance(a.net_pnl_bps, Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 2. PnLEngine.record() / .trades
# ══════════════════════════════════════════════════════════════════════════════


class TestPnLEngineRecord:
    def test_starts_empty(self):
        assert PnLEngine().trades == []

    def test_record_appends(self):
        engine = PnLEngine()
        engine.record(_arb())
        assert len(engine.trades) == 1

    def test_record_multiple(self):
        engine = PnLEngine()
        engine.record(_arb())
        engine.record(_arb())
        assert len(engine.trades) == 2

    def test_recorded_trade_is_same_object(self):
        engine = PnLEngine()
        a = _arb()
        engine.record(a)
        assert engine.trades[0] is a


# ══════════════════════════════════════════════════════════════════════════════
# 3. PnLEngine.summary()
# ══════════════════════════════════════════════════════════════════════════════


class TestSummaryEmpty:
    def test_summary_with_no_trades(self):
        """Summary returns zeros, no division errors."""
        s = PnLEngine().summary()
        assert s["total_trades"] == 0
        assert s["total_pnl_usd"] == Decimal("0")
        assert s["total_fees_usd"] == Decimal("0")
        assert s["avg_pnl_per_trade"] == Decimal("0")
        assert s["avg_pnl_bps"] == Decimal("0")
        assert s["win_rate"] == 0.0
        assert s["best_trade_pnl"] == Decimal("0")
        assert s["worst_trade_pnl"] == Decimal("0")
        assert s["total_notional"] == Decimal("0")
        assert s["sharpe_estimate"] == 0.0
        assert s["pnl_by_hour"] == {}


class TestSummaryWithTrades:
    def setup_method(self):
        # 3 wins, 1 loss
        self.engine = _engine_with_trades(
            _arb("2000", "2001.25", buy_fee="0.40", sell_fee="0.40"),  # net +0.45
            _arb("2000", "2001.90", buy_fee="0.40", sell_fee="0.40"),  # net +1.10
            _arb("2002", "2001.40", buy_fee="0.40", sell_fee="0.40"),  # net -1.40
            _arb("2000", "2001.80", buy_fee="0.40", sell_fee="0.40"),  # net +1.00
        )
        self.s = self.engine.summary()

    def test_total_trades(self):
        assert self.s["total_trades"] == 4

    def test_total_pnl_correct(self):
        expected = Decimal("0.45") + Decimal("1.10") + Decimal("-1.40") + Decimal("1.00")
        assert self.s["total_pnl_usd"] == expected

    def test_total_fees_correct(self):
        # 4 trades × (0.40 + 0.40) = 3.20
        assert self.s["total_fees_usd"] == Decimal("3.20")

    def test_avg_pnl_per_trade(self):
        assert self.s["avg_pnl_per_trade"] == self.s["total_pnl_usd"] / 4

    def test_summary_win_rate(self):
        """Win rate = profitable trades / total trades × 100."""
        assert self.s["win_rate"] == pytest.approx(75.0)

    def test_best_trade_pnl(self):
        assert self.s["best_trade_pnl"] == Decimal("1.10")

    def test_worst_trade_pnl(self):
        assert self.s["worst_trade_pnl"] == Decimal("-1.40")

    def test_total_notional(self):
        # 4 trades × 2000–2002 × 1 ETH ≈ ~8004
        assert self.s["total_notional"] > Decimal("8000")

    def test_sharpe_nonzero(self):
        assert isinstance(self.s["sharpe_estimate"], float)
        # With mixed PnL the Sharpe should be defined but we just check type/no error

    def test_avg_pnl_bps_is_decimal(self):
        assert isinstance(self.s["avg_pnl_bps"], Decimal)

    def test_pnl_by_hour_keyed_by_int(self):
        for k in self.s["pnl_by_hour"]:
            assert isinstance(k, int)
            assert 0 <= k <= 23

    def test_pnl_by_hour_sum_equals_total(self):
        hour_sum = sum(self.s["pnl_by_hour"].values(), Decimal("0"))
        assert hour_sum == self.s["total_pnl_usd"]


class TestSummarySingleTrade:
    def test_sharpe_single_trade_is_zero(self):
        """No stddev with one sample — Sharpe defaults to 0."""
        s = _engine_with_trades(_arb()).summary()
        assert s["sharpe_estimate"] == 0.0

    def test_win_rate_100_when_profitable(self):
        s = _engine_with_trades(_arb("2000", "2002")).summary()
        assert s["win_rate"] == 100.0

    def test_win_rate_0_when_losing(self):
        s = _engine_with_trades(_arb("2002", "2000")).summary()
        assert s["win_rate"] == 0.0


class TestSummaryPnLByHour:
    def test_trades_in_different_hours(self):
        t1 = datetime(2024, 1, 15, 10, 0, tzinfo=UTC)
        t2 = datetime(2024, 1, 15, 14, 0, tzinfo=UTC)
        engine = _engine_with_trades(
            _arb("2000", "2002", ts=t1),
            _arb("2000", "2003", ts=t2),
        )
        s = engine.summary()
        assert 10 in s["pnl_by_hour"]
        assert 14 in s["pnl_by_hour"]

    def test_multiple_trades_same_hour_aggregated(self):
        engine = _engine_with_trades(
            _arb("2000", "2002", ts=_TS),
            _arb("2000", "2003", ts=_TS),
        )
        s = engine.summary()
        hour_pnl = s["pnl_by_hour"][_TS.hour]
        expected = _arb("2000", "2002").net_pnl + _arb("2000", "2003").net_pnl
        assert hour_pnl == expected


# ══════════════════════════════════════════════════════════════════════════════
# 4. PnLEngine.recent()
# ══════════════════════════════════════════════════════════════════════════════


class TestRecent:
    def setup_method(self):
        # 5 trades at different times
        self.engine = PnLEngine()
        for i in range(5):
            ts = _TS + timedelta(minutes=i)
            self.engine.record(
                ArbRecord(
                    id=f"arb-{i}",
                    timestamp=ts,
                    buy_leg=_leg("buy", "2000", ts=ts),
                    sell_leg=_leg("sell", "2001", ts=ts),
                )
            )

    def test_returns_list(self):
        assert isinstance(self.engine.recent(), list)

    def test_default_returns_up_to_10(self):
        r = self.engine.recent()
        assert len(r) <= 10

    def test_returns_n_items(self):
        assert len(self.engine.recent(3)) == 3

    def test_most_recent_first(self):
        r = self.engine.recent(5)
        timestamps = [item["timestamp"] for item in r]
        assert timestamps == sorted(timestamps, reverse=True)

    def test_fewer_than_n_returns_all(self):
        assert len(self.engine.recent(100)) == 5

    def test_dict_has_required_keys(self):
        r = self.engine.recent(1)[0]
        for key in (
            "id",
            "timestamp",
            "symbol",
            "buy_venue",
            "sell_venue",
            "gross_pnl",
            "net_pnl",
            "net_pnl_bps",
            "total_fees",
            "notional",
        ):
            assert key in r

    def test_empty_engine_returns_empty(self):
        assert PnLEngine().recent() == []

    def test_recent_values_are_decimal(self):
        r = self.engine.recent(1)[0]
        assert isinstance(r["net_pnl"], Decimal)
        assert isinstance(r["notional"], Decimal)


# ══════════════════════════════════════════════════════════════════════════════
# 5. PnLEngine.export_csv()
# ══════════════════════════════════════════════════════════════════════════════


class TestExportCSV:
    def setup_method(self):
        self.engine = _engine_with_trades(
            _arb("2000", "2001.25", buy_fee="0.40", sell_fee="0.40", gas="0.10"),
            _arb("2001", "2002.00", buy_fee="0.40", sell_fee="0.40"),
        )
        self.tmp = tempfile.NamedTemporaryFile(suffix=".csv", delete=False)
        self.tmp.close()
        self.path = self.tmp.name

    def teardown_method(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_export_csv_format(self):
        """CSV has expected columns and correct values."""
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        assert len(rows) == 2
        expected_cols = {
            "id",
            "timestamp",
            "symbol",
            "buy_venue",
            "sell_venue",
            "buy_price",
            "sell_price",
            "amount",
            "gross_pnl",
            "total_fees",
            "net_pnl",
            "net_pnl_bps",
            "notional",
            "gas_cost_usd",
        }
        assert expected_cols <= set(rows[0].keys())

    def test_csv_values_correct(self):
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            reader = csv.DictReader(fh)
            rows = list(reader)

        row = rows[0]
        assert row["buy_price"] == "2000"
        assert row["sell_price"] == "2001.25"
        assert row["amount"] == "1"
        assert Decimal(row["gross_pnl"]) == Decimal("1.25")

    def test_csv_buy_venue_string(self):
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows[0]["buy_venue"] in ("wallet", "binance")

    def test_csv_row_count_matches_trades(self):
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert len(rows) == len(self.engine.trades)

    def test_csv_empty_engine(self):
        PnLEngine().export_csv(self.path)
        with open(self.path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert rows == []

    def test_csv_net_pnl_correct(self):
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        # First trade: gross=1.25, fees=0.40+0.40+0.10=0.90, net=0.35
        assert Decimal(rows[0]["net_pnl"]) == Decimal("0.35")

    def test_csv_gas_cost_exported(self):
        self.engine.export_csv(self.path)
        with open(self.path, newline="") as fh:
            rows = list(csv.DictReader(fh))
        assert Decimal(rows[0]["gas_cost_usd"]) == Decimal("0.10")
        assert Decimal(rows[1]["gas_cost_usd"]) == Decimal("0")


# ══════════════════════════════════════════════════════════════════════════════
# 6. CLI smoke tests
# ══════════════════════════════════════════════════════════════════════════════


class TestCLI:
    def test_summary_exits_zero(self):
        from inventory.pnl import _run_cli

        assert _run_cli(["--summary"]) == 0

    def test_recent_exits_zero(self):
        from inventory.pnl import _run_cli

        assert _run_cli(["--recent", "3"]) == 0

    def test_recent_default_exits_zero(self):
        from inventory.pnl import _run_cli

        assert _run_cli(["--recent"]) == 0

    def test_summary_prints_output(self, capsys):
        from inventory.pnl import _run_cli

        _run_cli(["--summary"])
        out = capsys.readouterr().out
        assert "Total Trades" in out
        assert "Win Rate" in out

    def test_recent_prints_output(self, capsys):
        from inventory.pnl import _run_cli

        _run_cli(["--recent", "2"])
        out = capsys.readouterr().out
        assert "ETH" in out

    def test_missing_args_exits_nonzero(self):
        import pytest

        from inventory.pnl import _run_cli

        with pytest.raises(SystemExit) as exc:
            _run_cli([])
        assert exc.value.code != 0
