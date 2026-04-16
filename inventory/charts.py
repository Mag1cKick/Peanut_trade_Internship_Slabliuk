"""
inventory/charts.py — Historical PnL chart export using matplotlib.

Generates publication-quality charts from a ``PnLEngine`` trade ledger and
saves them as PNG (or any matplotlib-supported format).

Available charts
────────────────
cumulative_pnl    Running cumulative net PnL over time (line chart).
pnl_by_hour       Average net PnL grouped by UTC hour (bar chart).
trade_distribution Histogram of individual trade net PnL values.
drawdown          Running drawdown from peak cumulative PnL.
all               Render all four charts as a 2×2 subplot grid.

Usage::

    from inventory.charts import PnLCharts
    from inventory.pnl import PnLEngine

    engine = PnLEngine()
    # ... record trades ...

    charts = PnLCharts(engine)
    charts.cumulative_pnl("pnl_cumulative.png")
    charts.pnl_by_hour("pnl_by_hour.png")
    charts.trade_distribution("trade_dist.png")
    charts.all("pnl_overview.png")

CLI::

    python -m inventory.charts --output pnl_overview.png
    python -m inventory.charts --chart cumulative --output cumul.png
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inventory.pnl import PnLEngine


def _require_matplotlib():
    try:
        import matplotlib

        matplotlib.use("Agg")  # non-interactive backend safe for tests/CI
        return matplotlib
    except ImportError as exc:
        raise ImportError("matplotlib is required for charts: pip install matplotlib") from exc


class PnLCharts:
    """
    Generates charts from a ``PnLEngine`` trade ledger.

    All chart methods accept an ``output_path`` and save the figure there.
    Pass ``output_path=None`` to return the ``Figure`` without saving.
    """

    def __init__(self, engine: PnLEngine) -> None:
        _require_matplotlib()
        self._engine = engine

    # ── Chart methods ──────────────────────────────────────────────────────────

    def cumulative_pnl(self, output_path: str | None = None):
        """
        Line chart of cumulative net PnL over time.

        X-axis: trade timestamp
        Y-axis: cumulative net PnL (USD)
        """
        import matplotlib.pyplot as plt

        trades = self._engine.trades
        fig, ax = plt.subplots(figsize=(10, 5))

        if not trades:
            ax.text(
                0.5,
                0.5,
                "No trades recorded",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=14,
                color="gray",
            )
        else:
            timestamps = [t.buy_leg.timestamp for t in trades]
            cumulative = []
            running = Decimal("0")
            for t in trades:
                running += t.net_pnl
                cumulative.append(float(running))

            ax.plot(timestamps, cumulative, linewidth=2, color="#2196F3", label="Cumulative PnL")
            ax.axhline(y=0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
            ax.fill_between(
                timestamps,
                cumulative,
                0,
                where=[c >= 0 for c in cumulative],
                alpha=0.2,
                color="green",
                label="Profit",
            )
            ax.fill_between(
                timestamps,
                cumulative,
                0,
                where=[c < 0 for c in cumulative],
                alpha=0.2,
                color="red",
                label="Loss",
            )
            ax.legend()

        ax.set_title("Cumulative Net PnL", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time")
        ax.set_ylabel("Net PnL (USD)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        return self._save_or_return(fig, output_path)

    def pnl_by_hour(self, output_path: str | None = None):
        """
        Bar chart of total net PnL grouped by UTC hour of day.
        """
        import matplotlib.pyplot as plt

        summary = self._engine.summary()
        by_hour: dict[int, float] = {h: float(v) for h, v in summary["pnl_by_hour"].items()}

        fig, ax = plt.subplots(figsize=(12, 5))
        hours = list(range(24))
        values = [by_hour.get(h, 0.0) for h in hours]
        colors = ["#4CAF50" if v >= 0 else "#F44336" for v in values]

        ax.bar(hours, values, color=colors, edgecolor="white", linewidth=0.5)
        ax.axhline(y=0, color="black", linewidth=0.8)
        ax.set_xticks(hours)
        ax.set_xticklabels([f"{h:02d}:00" for h in hours], rotation=45, ha="right", fontsize=8)
        ax.set_title("Net PnL by Hour of Day (UTC)", fontsize=14, fontweight="bold")
        ax.set_xlabel("Hour (UTC)")
        ax.set_ylabel("Total Net PnL (USD)")
        ax.grid(True, axis="y", alpha=0.3)
        fig.tight_layout()

        return self._save_or_return(fig, output_path)

    def trade_distribution(self, output_path: str | None = None):
        """
        Histogram of individual trade net PnL values.
        """
        import matplotlib.pyplot as plt

        trades = self._engine.trades
        fig, ax = plt.subplots(figsize=(10, 5))

        if not trades:
            ax.text(
                0.5,
                0.5,
                "No trades recorded",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=14,
                color="gray",
            )
        else:
            values = [float(t.net_pnl) for t in trades]
            n_bins = min(max(len(values) // 5, 10), 50)
            ax.hist(
                values, bins=n_bins, edgecolor="white", linewidth=0.5, color="#2196F3", alpha=0.8
            )
            ax.axvline(x=0, color="black", linewidth=1.2, linestyle="--", label="Break-even")
            mean_pnl = sum(values) / len(values)
            ax.axvline(
                x=mean_pnl,
                color="#FF9800",
                linewidth=1.5,
                linestyle="--",
                label=f"Mean: {mean_pnl:.4f}",
            )
            ax.legend()

        ax.set_title("Trade PnL Distribution", fontsize=14, fontweight="bold")
        ax.set_xlabel("Net PnL per Trade (USD)")
        ax.set_ylabel("Frequency")
        ax.grid(True, axis="y", alpha=0.3)
        fig.tight_layout()

        return self._save_or_return(fig, output_path)

    def drawdown(self, output_path: str | None = None):
        """
        Running drawdown from peak cumulative PnL.
        """
        import matplotlib.pyplot as plt

        trades = self._engine.trades
        fig, ax = plt.subplots(figsize=(10, 5))

        if not trades:
            ax.text(
                0.5,
                0.5,
                "No trades recorded",
                ha="center",
                va="center",
                transform=ax.transAxes,
                fontsize=14,
                color="gray",
            )
        else:
            timestamps = [t.buy_leg.timestamp for t in trades]
            cumulative: list[float] = []
            running = Decimal("0")
            for t in trades:
                running += t.net_pnl
                cumulative.append(float(running))

            peak = cumulative[0]
            drawdowns = []
            for c in cumulative:
                if c > peak:
                    peak = c
                drawdowns.append(c - peak)

            ax.fill_between(timestamps, drawdowns, 0, alpha=0.4, color="red", label="Drawdown")
            ax.plot(timestamps, drawdowns, linewidth=1.5, color="#F44336")
            ax.axhline(y=0, color="black", linewidth=0.8)
            ax.legend()

        ax.set_title("Running Drawdown", fontsize=14, fontweight="bold")
        ax.set_xlabel("Time")
        ax.set_ylabel("Drawdown from Peak (USD)")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()

        return self._save_or_return(fig, output_path)

    def all(self, output_path: str | None = None):
        """
        2×2 subplot grid with all four charts.
        """
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 2, figsize=(16, 10))
        fig.suptitle("PnL Dashboard", fontsize=16, fontweight="bold")

        # Temporarily capture each sub-figure's axes
        self._plot_cumulative_on(axes[0][0])
        self._plot_by_hour_on(axes[0][1])
        self._plot_distribution_on(axes[1][0])
        self._plot_drawdown_on(axes[1][1])

        fig.tight_layout()
        return self._save_or_return(fig, output_path)

    # ── Internal plot helpers (axes-level) ─────────────────────────────────────

    def _plot_cumulative_on(self, ax) -> None:
        trades = self._engine.trades
        if not trades:
            ax.text(
                0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="gray"
            )
        else:
            ts = [t.buy_leg.timestamp for t in trades]
            cum, running = [], Decimal("0")
            for t in trades:
                running += t.net_pnl
                cum.append(float(running))
            ax.plot(ts, cum, linewidth=2, color="#2196F3")
            ax.axhline(y=0, color="black", linewidth=0.8, linestyle="--", alpha=0.5)
            ax.fill_between(ts, cum, 0, where=[c >= 0 for c in cum], alpha=0.2, color="green")
            ax.fill_between(ts, cum, 0, where=[c < 0 for c in cum], alpha=0.2, color="red")
        ax.set_title("Cumulative PnL", fontsize=11)
        ax.set_ylabel("USD")
        ax.grid(True, alpha=0.3)

    def _plot_by_hour_on(self, ax) -> None:
        by_hour = {h: float(v) for h, v in self._engine.summary()["pnl_by_hour"].items()}
        hours = list(range(24))
        values = [by_hour.get(h, 0.0) for h in hours]
        colors = ["#4CAF50" if v >= 0 else "#F44336" for v in values]
        ax.bar(hours, values, color=colors, edgecolor="white", linewidth=0.5)
        ax.axhline(y=0, color="black", linewidth=0.8)
        ax.set_title("PnL by Hour (UTC)", fontsize=11)
        ax.set_xlabel("Hour")
        ax.set_ylabel("USD")
        ax.grid(True, axis="y", alpha=0.3)

    def _plot_distribution_on(self, ax) -> None:
        trades = self._engine.trades
        if not trades:
            ax.text(
                0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="gray"
            )
        else:
            vals = [float(t.net_pnl) for t in trades]
            ax.hist(
                vals,
                bins=min(max(len(vals) // 5, 10), 50),
                color="#2196F3",
                alpha=0.8,
                edgecolor="white",
            )
            ax.axvline(x=0, color="black", linewidth=1.2, linestyle="--")
        ax.set_title("PnL Distribution", fontsize=11)
        ax.set_xlabel("Net PnL (USD)")
        ax.set_ylabel("Frequency")
        ax.grid(True, axis="y", alpha=0.3)

    def _plot_drawdown_on(self, ax) -> None:
        trades = self._engine.trades
        if not trades:
            ax.text(
                0.5, 0.5, "No data", ha="center", va="center", transform=ax.transAxes, color="gray"
            )
        else:
            ts = [t.buy_leg.timestamp for t in trades]
            cum, running = [], Decimal("0")
            for t in trades:
                running += t.net_pnl
                cum.append(float(running))
            peak = cum[0]
            dds = []
            for c in cum:
                if c > peak:
                    peak = c
                dds.append(c - peak)
            ax.fill_between(ts, dds, 0, alpha=0.4, color="red")
            ax.plot(ts, dds, linewidth=1.5, color="#F44336")
            ax.axhline(y=0, color="black", linewidth=0.8)
        ax.set_title("Drawdown", fontsize=11)
        ax.set_ylabel("USD")
        ax.grid(True, alpha=0.3)

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _save_or_return(fig, output_path: str | None):
        if output_path is not None:
            fig.savefig(output_path, dpi=150, bbox_inches="tight")
        import matplotlib.pyplot as plt

        plt.close(fig)
        return fig


# ── CLI ─────────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export historical PnL charts from the trade ledger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m inventory.charts --output pnl_overview.png\n"
            "  python -m inventory.charts --chart cumulative --output cumul.png"
        ),
    )
    parser.add_argument(
        "--output",
        default="pnl_chart.png",
        help="Output file path (default: pnl_chart.png)",
    )
    parser.add_argument(
        "--chart",
        choices=["cumulative_pnl", "pnl_by_hour", "trade_distribution", "drawdown", "all"],
        default="all",
        help="Which chart to render (default: all)",
    )
    args = parser.parse_args(argv)

    from inventory.pnl import ArbRecord, PnLEngine, TradeLeg

    engine = PnLEngine()

    # Seed with sample data so the CLI always produces a visible chart
    def _leg(side, price, ts_offset=0):
        return TradeLeg(
            id=f"{side}-demo",
            timestamp=datetime(2024, 1, 15, 10 + ts_offset, 0, 0, tzinfo=UTC),
            venue="binance" if side == "buy" else "wallet",
            symbol="ETH/USDT",
            side=side,
            amount=Decimal("1"),
            price=Decimal(str(price)),
            fee=Decimal("0.002"),
            fee_asset="USDT",
        )

    for i, (buy_p, sell_p) in enumerate(
        [(1990, 2010), (2000, 2015), (2005, 1995), (1980, 2020), (2010, 2030)]
    ):
        engine.record(
            ArbRecord(
                id=f"arb-{i}",
                timestamp=datetime(2024, 1, 15, 10 + i, 0, 0, tzinfo=UTC),
                buy_leg=_leg("buy", buy_p, i),
                sell_leg=_leg("sell", sell_p, i),
                gas_cost_usd=Decimal("0.5"),
            )
        )

    charts = PnLCharts(engine)
    fn = getattr(charts, args.chart)
    fn(args.output)

    print(f"Chart saved to: {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
