"""
inventory/dashboard.py — Real-time terminal inventory dashboard using Rich.

Renders a live view of multi-venue balances, cross-venue skew, and PnL
summary in the terminal.  Refreshes every ``refresh_interval`` seconds.

Usage::

    from inventory.dashboard import InventoryDashboard
    from inventory.tracker import InventoryTracker, Venue
    from inventory.pnl import PnLEngine

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    pnl     = PnLEngine()
    dash    = InventoryDashboard(tracker, pnl_engine=pnl)

    # One-shot render (returns Rich renderable):
    panel = dash.render()

    # Live loop (blocks until Ctrl-C):
    dash.run(refresh_interval=5)

CLI::

    python -m inventory.dashboard
    python -m inventory.dashboard --interval 3 --once
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inventory.pnl import PnLEngine
    from inventory.tracker import InventoryTracker


def _require_rich():
    try:
        import rich

        return rich
    except ImportError as exc:
        raise ImportError("rich is required for the dashboard: pip install rich") from exc


class InventoryDashboard:
    """
    Terminal dashboard that renders inventory state using Rich.

    Parameters
    ----------
    tracker:
        ``InventoryTracker`` pre-loaded with venue balances.
    pnl_engine:
        Optional ``PnLEngine`` for the PnL summary panel.
        Pass ``None`` to omit the PnL section.
    title:
        Dashboard title shown in the header panel.
    """

    def __init__(
        self,
        tracker: InventoryTracker,
        pnl_engine: PnLEngine | None = None,
        title: str = "Inventory Dashboard",
    ) -> None:
        _require_rich()
        self._tracker = tracker
        self._pnl = pnl_engine
        self._title = title

    # ── Rendering ──────────────────────────────────────────────────────────────

    def render(self):
        """
        Build and return a Rich ``Layout`` (or ``Group``) representing the
        current dashboard state.  Suitable for embedding in ``rich.live.Live``.
        """
        from rich.panel import Panel

        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
        header = Panel(
            f"[bold cyan]{self._title}[/bold cyan]  [dim]{now}[/dim]",
            expand=True,
        )

        balance_table = self._build_balance_table()
        skew_table = self._build_skew_table()

        sections = [header, balance_table, skew_table]

        if self._pnl is not None:
            sections.append(self._build_pnl_panel())

        from rich.console import Group

        return Group(*sections)

    def _build_balance_table(self):
        from rich.table import Table

        snap = self._tracker.snapshot()
        venues_data = snap["venues"]  # {venue_name: {asset: {free, locked, total}}}
        totals = snap["totals"]  # {asset: Decimal}
        venue_names = list(venues_data.keys())

        table = Table(title="Balances by Venue", show_lines=True, expand=True)
        table.add_column("Asset", style="bold")
        for v in venue_names:
            table.add_column(str(v).upper(), justify="right")
        table.add_column("Total", justify="right", style="bold yellow")

        all_assets: set[str] = set(totals.keys())

        for asset in sorted(all_assets):
            row = [asset]
            for v in venue_names:
                info = venues_data.get(v, {}).get(asset, {})
                bal = info.get("total", Decimal("0"))
                row.append(f"{float(bal):,.4f}")
            row.append(f"{float(totals.get(asset, Decimal('0'))):,.4f}")
            table.add_row(*row)

        return table

    def _build_skew_table(self):
        from rich.table import Table
        from rich.text import Text

        table = Table(title="Cross-Venue Skew", show_lines=True, expand=True)
        table.add_column("Asset", style="bold")
        table.add_column("Max Deviation", justify="right")
        table.add_column("Needs Rebalance", justify="center")
        table.add_column("Distribution", justify="right")

        skew_list = self._tracker.get_skews()

        for skew in sorted(skew_list, key=lambda s: s["asset"]):
            asset = skew["asset"]
            dev = float(skew.get("max_deviation_pct", 0))
            needs = skew.get("needs_rebalance", False)

            dev_text = Text(f"{dev:.1f}%", style="red bold" if needs else "green")
            flag = Text("YES", style="red bold") if needs else Text("no", style="dim")

            dist_parts = []
            for venue, info in sorted(skew.get("venues", {}).items()):
                pct = info.get("pct", 0)
                dist_parts.append(f"{venue}={float(pct):.0f}%")
            dist = "  ".join(dist_parts)

            table.add_row(asset, dev_text, flag, dist)

        if not skew_list:
            table.add_row("[dim]no data[/dim]", "", "", "")

        return table

    def _build_pnl_panel(self):
        from rich.panel import Panel
        from rich.table import Table

        summary = self._pnl.summary()

        table = Table.grid(padding=(0, 2))
        table.add_column(style="dim")
        table.add_column(justify="right", style="bold")

        net = float(summary["total_pnl_usd"])
        net_style = "green" if net >= 0 else "red"
        sign = "+" if net >= 0 else ""

        table.add_row("Trades:", str(summary["total_trades"]))
        table.add_row("Win rate:", f"{float(summary['win_rate']) * 100:.1f}%")
        table.add_row(
            "Net PnL:",
            f"[{net_style}]{sign}{net:,.4f} USDT[/{net_style}]",
        )
        table.add_row(
            "Avg PnL/trade:",
            f"{float(summary['avg_pnl_per_trade']):,.4f} USDT",
        )
        table.add_row("Sharpe:", f"{summary['sharpe_estimate']:.3f}")

        return Panel(table, title="PnL Summary", expand=True)

    # ── Live loop ──────────────────────────────────────────────────────────────

    def run(self, refresh_interval: float = 5.0) -> None:
        """
        Block and render the dashboard in a ``rich.live.Live`` loop.
        Exits cleanly on KeyboardInterrupt.
        """
        from rich.live import Live

        try:
            with Live(self.render(), refresh_per_second=1, screen=False) as live:
                while True:
                    time.sleep(refresh_interval)
                    live.update(self.render())
        except KeyboardInterrupt:
            pass

    def print_once(self) -> None:
        """Print a single static snapshot to stdout."""
        from rich.console import Console

        console = Console()
        console.print(self.render())


# ── CLI ─────────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Real-time inventory dashboard (Rich terminal UI)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m inventory.dashboard\n"
            "  python -m inventory.dashboard --interval 3\n"
            "  python -m inventory.dashboard --once"
        ),
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Refresh interval in seconds (default: 5)",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print a single snapshot and exit",
    )
    args = parser.parse_args(argv)

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(_root, ".env"))
    except ImportError:
        pass

    from inventory.pnl import PnLEngine
    from inventory.tracker import InventoryTracker, Venue

    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_cex(
        Venue.BINANCE,
        {
            "ETH": {"free": "10", "locked": "0"},
            "USDT": {"free": "20000", "locked": "0"},
            "BTC": {"free": "0.5", "locked": "0"},
        },
    )
    tracker.update_from_wallet(Venue.WALLET, {"ETH": "2", "USDT": "5000"})

    pnl = PnLEngine()
    dash = InventoryDashboard(tracker, pnl_engine=pnl)

    if args.once:
        dash.print_once()
    else:
        print("Press Ctrl-C to exit.\n")
        try:
            dash.run(refresh_interval=args.interval)
        except KeyboardInterrupt:
            pass

    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
