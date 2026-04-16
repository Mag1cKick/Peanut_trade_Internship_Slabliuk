"""
tests/test_charts.py — Unit tests for inventory.charts.PnLCharts

All chart-saving tests use tmp_path and verify the output file is created.
Matplotlib is set to Agg backend (no display required).

Test groups:
  1. Construction — requires matplotlib, stores engine
  2. cumulative_pnl — empty and with data
  3. pnl_by_hour — empty and with data
  4. trade_distribution — empty and with data
  5. drawdown — empty and with data
  6. all — 2x2 grid renders
  7. File output — files are actually written
  8. CLI smoke test
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import patch

import pytest

from inventory.charts import PnLCharts
from inventory.pnl import ArbRecord, PnLEngine, TradeLeg

# ── Helpers ────────────────────────────────────────────────────────────────────


def _leg(side: str, price: float, hour: int = 10) -> TradeLeg:
    return TradeLeg(
        id=f"{side}-{price}",
        timestamp=datetime(2024, 3, 1, hour, 0, 0, tzinfo=UTC),
        venue="binance" if side == "buy" else "wallet",
        symbol="ETH/USDT",
        side=side,
        amount=Decimal("1"),
        price=Decimal(str(price)),
        fee=Decimal("0.002"),
        fee_asset="USDT",
    )


def _arb(buy_price: float, sell_price: float, hour: int = 10) -> ArbRecord:
    return ArbRecord(
        id=f"arb-{buy_price}-{sell_price}",
        timestamp=datetime(2024, 3, 1, hour, 0, 0, tzinfo=UTC),
        buy_leg=_leg("buy", buy_price, hour),
        sell_leg=_leg("sell", sell_price, hour),
        gas_cost_usd=Decimal("0.5"),
    )


def _engine_with_trades(n: int = 5) -> PnLEngine:
    engine = PnLEngine()
    prices = [(1990, 2010), (2000, 2015), (2005, 1995), (1980, 2020), (2010, 2030)]
    for i in range(n):
        buy_p, sell_p = prices[i % len(prices)]
        engine.record(_arb(buy_p, sell_p, hour=(10 + i) % 24))
    return engine


def _empty_engine() -> PnLEngine:
    return PnLEngine()


# ── 1. Construction ────────────────────────────────────────────────────────────


class TestConstruction:
    def test_stores_engine(self):
        e = _empty_engine()
        c = PnLCharts(e)
        assert c._engine is e

    def test_raises_without_matplotlib(self):
        with patch.dict("sys.modules", {"matplotlib": None}):
            with pytest.raises(ImportError, match="matplotlib"):
                PnLCharts(_empty_engine())

    def test_construction_with_trades(self):
        e = _engine_with_trades(3)
        c = PnLCharts(e)
        assert c._engine is e


# ── 2. cumulative_pnl ─────────────────────────────────────────────────────────


class TestCumulativePnl:
    def test_returns_figure_no_save(self):
        import matplotlib.figure

        c = PnLCharts(_engine_with_trades())
        fig = c.cumulative_pnl(output_path=None)
        assert isinstance(fig, matplotlib.figure.Figure)

    def test_empty_engine_no_error(self):
        c = PnLCharts(_empty_engine())
        fig = c.cumulative_pnl(output_path=None)
        assert fig is not None

    def test_saves_file(self, tmp_path):
        path = str(tmp_path / "cumul.png")
        c = PnLCharts(_engine_with_trades())
        c.cumulative_pnl(output_path=path)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0


# ── 3. pnl_by_hour ────────────────────────────────────────────────────────────


class TestPnlByHour:
    def test_returns_figure_no_save(self):
        import matplotlib.figure

        c = PnLCharts(_engine_with_trades())
        fig = c.pnl_by_hour(output_path=None)
        assert isinstance(fig, matplotlib.figure.Figure)

    def test_empty_engine_no_error(self):
        c = PnLCharts(_empty_engine())
        fig = c.pnl_by_hour(output_path=None)
        assert fig is not None

    def test_saves_file(self, tmp_path):
        path = str(tmp_path / "by_hour.png")
        c = PnLCharts(_engine_with_trades())
        c.pnl_by_hour(output_path=path)
        assert os.path.exists(path)


# ── 4. trade_distribution ─────────────────────────────────────────────────────


class TestTradeDistribution:
    def test_returns_figure_no_save(self):
        import matplotlib.figure

        c = PnLCharts(_engine_with_trades())
        fig = c.trade_distribution(output_path=None)
        assert isinstance(fig, matplotlib.figure.Figure)

    def test_empty_engine_no_error(self):
        c = PnLCharts(_empty_engine())
        fig = c.trade_distribution(output_path=None)
        assert fig is not None

    def test_saves_file(self, tmp_path):
        path = str(tmp_path / "dist.png")
        c = PnLCharts(_engine_with_trades())
        c.trade_distribution(output_path=path)
        assert os.path.exists(path)


# ── 5. drawdown ───────────────────────────────────────────────────────────────


class TestDrawdown:
    def test_returns_figure_no_save(self):
        import matplotlib.figure

        c = PnLCharts(_engine_with_trades())
        fig = c.drawdown(output_path=None)
        assert isinstance(fig, matplotlib.figure.Figure)

    def test_empty_engine_no_error(self):
        c = PnLCharts(_empty_engine())
        fig = c.drawdown(output_path=None)
        assert fig is not None

    def test_saves_file(self, tmp_path):
        path = str(tmp_path / "drawdown.png")
        c = PnLCharts(_engine_with_trades())
        c.drawdown(output_path=path)
        assert os.path.exists(path)

    def test_drawdown_never_positive(self):
        """Drawdown from peak must always be ≤ 0."""
        engine = _engine_with_trades(5)
        trades = engine.trades
        running, peak = Decimal("0"), None
        for t in trades:
            running += t.net_pnl
            if peak is None or running > peak:
                peak = running
            dd = float(running - peak)
            assert dd <= 1e-9, f"Drawdown should be ≤ 0, got {dd}"


# ── 6. all() subplot grid ─────────────────────────────────────────────────────


class TestAll:
    def test_returns_figure(self):
        import matplotlib.figure

        c = PnLCharts(_engine_with_trades())
        fig = c.all(output_path=None)
        assert isinstance(fig, matplotlib.figure.Figure)

    def test_figure_has_four_subplots(self):
        c = PnLCharts(_engine_with_trades())
        fig = c.all(output_path=None)
        assert len(fig.axes) == 4

    def test_saves_file(self, tmp_path):
        path = str(tmp_path / "overview.png")
        c = PnLCharts(_engine_with_trades())
        c.all(output_path=path)
        assert os.path.exists(path)
        assert os.path.getsize(path) > 0

    def test_all_empty_engine(self):
        c = PnLCharts(_empty_engine())
        fig = c.all(output_path=None)
        assert fig is not None


# ── 7. File output format ─────────────────────────────────────────────────────


class TestFileOutput:
    def test_png_extension_written(self, tmp_path):
        path = str(tmp_path / "out.png")
        PnLCharts(_engine_with_trades()).cumulative_pnl(output_path=path)
        # PNG magic bytes: \x89PNG
        with open(path, "rb") as f:
            header = f.read(4)
        assert header == b"\x89PNG"

    def test_output_path_none_does_not_create_file(self, tmp_path):
        initial_files = set(os.listdir(tmp_path))
        PnLCharts(_engine_with_trades()).cumulative_pnl(output_path=None)
        assert set(os.listdir(tmp_path)) == initial_files


# ── 8. CLI smoke ───────────────────────────────────────────────────────────────


class TestCLI:
    def test_cli_runs_and_creates_file(self, tmp_path):
        from inventory.charts import _run_cli

        out = str(tmp_path / "cli_out.png")
        rc = _run_cli(["--output", out, "--chart", "all"])
        assert rc == 0
        assert os.path.exists(out)

    def test_cli_cumulative_chart(self, tmp_path):
        from inventory.charts import _run_cli

        out = str(tmp_path / "cumul.png")
        rc = _run_cli(["--output", out, "--chart", "cumulative_pnl"])
        assert rc == 0
        assert os.path.exists(out)

    def test_cli_module_importable(self):
        from inventory.charts import _run_cli

        assert callable(_run_cli)
