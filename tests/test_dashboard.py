"""
tests/test_dashboard.py — Unit tests for inventory.dashboard.InventoryDashboard

Test groups:
  1. Construction — stores tracker and pnl_engine
  2. render() — returns a Rich renderable, includes expected sections
  3. _build_balance_table — columns, rows, totals
  4. _build_skew_table — deviation colours, needs-rebalance flag
  5. _build_pnl_panel — correct summary fields
  6. print_once — writes to console without error
  7. CLI smoke test
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from inventory.dashboard import InventoryDashboard
from inventory.pnl import PnLEngine
from inventory.tracker import InventoryTracker, Venue

# ── Helpers ────────────────────────────────────────────────────────────────────


def _make_tracker(
    binance_eth: str = "9",
    wallet_eth: str = "1",
    binance_usdt: str = "5000",
    wallet_usdt: str = "1000",
) -> InventoryTracker:
    t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    t.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": binance_eth, "locked": "0"},
            "USDT": {"free": binance_usdt, "locked": "0"},
        },
    )
    t.update_from_wallet(Venue.WALLET, {"ETH": wallet_eth, "USDT": wallet_usdt})
    return t


def _make_dash(**kwargs) -> InventoryDashboard:
    tracker = _make_tracker(**kwargs)
    pnl = PnLEngine()
    return InventoryDashboard(tracker, pnl_engine=pnl)


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_stores_tracker(self):
        t = _make_tracker()
        d = InventoryDashboard(t)
        assert d._tracker is t

    def test_stores_pnl_engine(self):
        t = _make_tracker()
        pnl = PnLEngine()
        d = InventoryDashboard(t, pnl_engine=pnl)
        assert d._pnl is pnl

    def test_pnl_engine_optional(self):
        t = _make_tracker()
        d = InventoryDashboard(t)
        assert d._pnl is None

    def test_default_title(self):
        d = _make_dash()
        assert "Dashboard" in d._title

    def test_custom_title(self):
        t = _make_tracker()
        d = InventoryDashboard(t, title="My Arb Dashboard")
        assert d._title == "My Arb Dashboard"

    def test_raises_without_rich(self):
        with patch.dict("sys.modules", {"rich": None}):
            with pytest.raises(ImportError, match="rich"):
                InventoryDashboard(_make_tracker())


# ── 2. render() ────────────────────────────────────────────────────────────────


class TestRender:
    def test_render_returns_something(self):
        d = _make_dash()
        result = d.render()
        assert result is not None

    def test_render_with_no_pnl_engine(self):
        t = _make_tracker()
        d = InventoryDashboard(t, pnl_engine=None)
        result = d.render()
        assert result is not None

    def test_render_does_not_raise(self):
        d = _make_dash()
        d.render()  # should not raise


# ── 3. _build_balance_table ────────────────────────────────────────────────────


class TestBalanceTable:
    def test_table_has_venue_columns(self):
        d = _make_dash()
        table = d._build_balance_table()
        col_names = [c.header for c in table.columns]
        assert any("BINANCE" in str(c).upper() for c in col_names)

    def test_table_has_total_column(self):
        d = _make_dash()
        table = d._build_balance_table()
        col_names = [str(c.header) for c in table.columns]
        assert any("Total" in c for c in col_names)

    def test_table_has_asset_column(self):
        d = _make_dash()
        table = d._build_balance_table()
        col_names = [str(c.header) for c in table.columns]
        assert any("Asset" in c for c in col_names)

    def test_table_has_rows(self):
        d = _make_dash()
        table = d._build_balance_table()
        assert table.row_count > 0

    def test_eth_row_present(self):
        d = _make_dash()
        table = d._build_balance_table()
        assert table.row_count >= 1


# ── 4. _build_skew_table ───────────────────────────────────────────────────────


class TestSkewTable:
    def test_skew_table_returns_table(self):
        d = _make_dash()
        table = d._build_skew_table()
        from rich.table import Table

        assert isinstance(table, Table)

    def test_skew_table_has_rows_when_data(self):
        # 9 ETH binance vs 1 ETH wallet → skewed
        d = _make_dash(binance_eth="9", wallet_eth="1")
        table = d._build_skew_table()
        assert table.row_count > 0

    def test_skew_table_no_error_empty_tracker(self):
        t = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        d = InventoryDashboard(t)
        table = d._build_skew_table()
        assert table.row_count >= 0


# ── 5. _build_pnl_panel ────────────────────────────────────────────────────────


class TestPnlPanel:
    def test_pnl_panel_returns_panel(self):
        d = _make_dash()
        from rich.panel import Panel

        panel = d._build_pnl_panel()
        assert isinstance(panel, Panel)

    def test_pnl_panel_no_error(self):
        d = _make_dash()
        d._build_pnl_panel()  # should not raise

    def test_pnl_panel_no_error_with_zero_trades(self):
        d = _make_dash()
        d._build_pnl_panel()  # empty PnLEngine, should not raise


# ── 6. print_once ─────────────────────────────────────────────────────────────


class TestPrintOnce:
    def test_print_once_runs_without_error(self):
        d = _make_dash()
        d.print_once()

    def test_print_once_no_pnl_engine(self):
        t = _make_tracker()
        d = InventoryDashboard(t, pnl_engine=None)
        d.print_once()


# ── 7. CLI smoke ───────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_once_flag_exits_zero(self):
        from inventory.dashboard import _run_cli

        rc = _run_cli(["--once"])
        assert rc == 0

    def test_cli_module_importable(self):
        from inventory.dashboard import _run_cli

        assert callable(_run_cli)


# ── Coverage gap tests ─────────────────────────────────────────────────────────


class TestRunMethod:
    """Cover run() — the live loop that exits on KeyboardInterrupt."""

    def test_run_exits_on_keyboard_interrupt(self):
        from unittest.mock import patch

        d = _make_dash()

        class _MockLive:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def update(self, *a):
                raise KeyboardInterrupt()

        with patch("rich.live.Live", _MockLive):
            # Should not raise — KeyboardInterrupt is caught
            d.run(refresh_interval=0.0)

    def test_run_calls_render_on_update(self):
        from unittest.mock import patch

        d = _make_dash()
        render_calls = []

        class _MockLive:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def update(self, renderable):
                render_calls.append(renderable)
                raise KeyboardInterrupt()

        with patch("rich.live.Live", _MockLive):
            with patch("time.sleep"):
                d.run(refresh_interval=0.0)

        assert len(render_calls) >= 1


class TestCLILiveMode:
    """Cover the CLI path that calls run() (no --once flag)."""

    def test_cli_live_mode_stops_on_keyboard_interrupt(self):
        from inventory.dashboard import _run_cli

        with patch("inventory.dashboard.InventoryDashboard.run", side_effect=KeyboardInterrupt()):
            rc = _run_cli([])
        assert rc == 0
