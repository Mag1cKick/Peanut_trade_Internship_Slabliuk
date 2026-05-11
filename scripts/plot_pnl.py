"""
scripts/plot_pnl.py — Cumulative PnL and drawdown charts from bot logs.

Parses TRADE | log lines to extract actual net PnL, then plots:
  1. Cumulative PnL over time
  2. Drawdown from peak (%)
  3. Per-trade PnL bar chart

Usage:
    python scripts/plot_pnl.py
    python scripts/plot_pnl.py logs/bot_20260601.log
"""

from __future__ import annotations

import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

_TRADE = re.compile(r"TRADE \| pair=(\S+) \|.*\| pnl=([-\d.]+) \| state=(\w+)")
_DRY = re.compile(
    r"DRY RUN \| Would trade: \S+ \S+ size=[\d.]+ spread=[\d.]+bps expected_pnl=\$([-\d.]+)"
)
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def parse_logs(paths: list[Path]) -> list[dict]:
    trades: list[dict] = []
    for path in sorted(paths):
        ts = None
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            m = _TS.match(line)
            if m:
                try:
                    ts = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                except ValueError:
                    pass
            m = _TRADE.search(line)
            if m and m.group(3) == "DONE":
                trades.append(
                    {"ts": ts, "pnl": float(m.group(2)), "pair": m.group(1), "type": "live"}
                )
            m = _DRY.search(line)
            if m:
                trades.append(
                    {"ts": ts, "pnl": float(m.group(1)), "pair": "dry-run", "type": "dry"}
                )
    return trades


def plot(trades: list[dict], title: str = "PnL Analysis") -> None:
    try:
        import matplotlib.dates as mdates
        import matplotlib.pyplot as plt
    except ImportError:
        print("matplotlib not installed — run: pip install matplotlib")
        return

    if not trades:
        print("No trades found in logs.")
        return

    timestamps = [t["ts"] for t in trades if t["ts"]]
    pnls = [t["pnl"] for t in trades if t["ts"]]

    if not pnls:
        print("No timestamped trades found.")
        return

    cum_pnl = [sum(pnls[: i + 1]) for i in range(len(pnls))]

    peak = cum_pnl[0]
    drawdowns = []
    for val in cum_pnl:
        peak = max(peak, val)
        dd = (val - peak) / (100 + peak) * 100 if (100 + peak) > 0 else 0
        drawdowns.append(dd)

    fig, axes = plt.subplots(3, 1, figsize=(12, 10))
    fig.suptitle(title, fontsize=14, fontweight="bold")

    ax1 = axes[0]
    color = "green" if cum_pnl[-1] >= 0 else "red"
    ax1.plot(timestamps, cum_pnl, color=color, linewidth=2)
    ax1.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax1.fill_between(
        timestamps, cum_pnl, 0, where=[p >= 0 for p in cum_pnl], alpha=0.2, color="green"
    )
    ax1.fill_between(timestamps, cum_pnl, 0, where=[p < 0 for p in cum_pnl], alpha=0.2, color="red")
    ax1.set_ylabel("Cumulative PnL ($)")
    ax1.set_title(f"Cumulative PnL  (total: ${cum_pnl[-1]:+.2f})")
    ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    ax1.grid(True, alpha=0.3)

    ax2 = axes[1]
    ax2.fill_between(timestamps, drawdowns, 0, color="red", alpha=0.4)
    ax2.plot(timestamps, drawdowns, color="darkred", linewidth=1)
    ax2.set_ylabel("Drawdown (%)")
    ax2.set_title(f"Drawdown from Peak  (max: {min(drawdowns):.1f}%)")
    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d %H:%M"))
    ax2.grid(True, alpha=0.3)

    ax3 = axes[2]
    colors = ["green" if p >= 0 else "red" for p in pnls]
    ax3.bar(range(len(pnls)), pnls, color=colors, alpha=0.7)
    ax3.axhline(0, color="gray", linestyle="--", alpha=0.5)
    ax3.set_xlabel("Trade #")
    ax3.set_ylabel("PnL ($)")
    wins = sum(1 for p in pnls if p > 0)
    ax3.set_title(f"Per-Trade PnL  ({wins}/{len(pnls)} wins, {wins/len(pnls)*100:.0f}% win rate)")
    ax3.grid(True, alpha=0.3, axis="y")

    plt.tight_layout()
    out = Path(__file__).parent.parent / "reports" / "pnl_chart.png"
    plt.savefig(out, dpi=150, bbox_inches="tight")
    print(f"Chart saved to {out}")
    plt.show()

    print(
        f"\nSummary: {len(pnls)} trades  |  "
        f"Net PnL: ${cum_pnl[-1]:+.2f}  |  "
        f"Win rate: {wins/len(pnls)*100:.0f}%  |  "
        f"Max drawdown: {min(drawdowns):.1f}%"
    )


if __name__ == "__main__":
    try:
        from db.trades import all_trades as _db_trades

        db_rows = _db_trades()
        if db_rows:
            live = []
            for r in db_rows:
                try:
                    ts = datetime.strptime(r["ts"], "%Y-%m-%d %H:%M:%S")
                except Exception:
                    ts = None
                live.append(
                    {"ts": ts, "pnl": float(r["net_pnl"]), "pair": r["pair"], "type": "live"}
                )
            print(f"Using DB: {len(live)} trades")
            plot(live, title="Live Trading PnL — Week 6  (LINK/USDT)")
            sys.exit(0)
    except Exception as e:
        print(f"DB read failed ({e}), falling back to logs")

    if len(sys.argv) > 1:
        paths = [Path(p) for p in sys.argv[1:]]
    else:
        paths = sorted(Path("logs").glob("bot_day[2-5].log"))

    if not paths:
        print("No log files found.")
        sys.exit(1)

    trades = parse_logs(paths)
    live = [t for t in trades if t["type"] == "live"]
    dry = [t for t in trades if t["type"] == "dry"]

    print(f"Found: {len(live)} live trades  +  {len(dry)} dry-run signals")

    if live:
        plot(live, title="Live Trading PnL — Week 6")
    elif dry:
        print("No live trades yet — plotting dry-run expected PnL")
        plot(dry, title="Dry-Run Expected PnL")
    else:
        print("No trades found in logs.")
