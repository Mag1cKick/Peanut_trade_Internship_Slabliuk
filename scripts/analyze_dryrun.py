"""
scripts/analyze_dryrun.py — Dry-run log analyzer.

Parses bot log files and answers the four questions from the assignment:
  1. Signal frequency  — how often does the bot detect opportunities?
  2. Cost breakdown    — are fees estimated correctly, what is breakeven spread?
  3. Parameter sensitivity — what if min_spread_bps or trade_size changed?
  4. Risk limit hits   — what blocked signals, were the blocks correct?

Usage:
    python scripts/analyze_dryrun.py                     # latest log file
    python scripts/analyze_dryrun.py logs/bot_20260502.log
"""

from __future__ import annotations

import re
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

# ── Windows UTF-8 fix ────────────────────────────────────────────────────────
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── Regex patterns ────────────────────────────────────────────────────────────
_DRY = re.compile(
    r"DRY RUN \| Would trade: (?P<pair>\S+) \S+ "
    r"size=(?P<size>[\d.]+) "
    r"spread=(?P<spread>[\d.]+)bps "
    r"expected_pnl=\$(?P<pnl>-?[\d.]+)"
)
_RISK = re.compile(r"Risk check failed for (?P<pair>\S+): (?P<reason>.+)")
_VALID = re.compile(r"Validation failed for (?P<pair>\S+): (?P<reason>.+)")
_SKIP = re.compile(r"Skipped (?P<pair>\S+): (?P<reason>.+)")
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# ── Helpers ───────────────────────────────────────────────────────────────────

BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[92m"
YELLOW = "\033[93m"
CYAN = "\033[96m"


def hdr(title: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 56}{RESET}")
    print(f"{BOLD}{CYAN}  {title}{RESET}")
    print(f"{BOLD}{CYAN}{'─' * 56}{RESET}")


def find_latest_log() -> Path | None:
    logs = sorted(Path("logs").glob("bot_*.log"), reverse=True)
    return logs[0] if logs else None


# ── Main analysis ─────────────────────────────────────────────────────────────


def analyze(log_path: Path) -> None:
    print(f"\n{BOLD}Analyzing: {log_path}{RESET}")
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()

    signals: list[dict] = []
    risk_blocks: list[tuple[str, str]] = []
    valid_blocks: list[tuple[str, str]] = []
    skips: list[tuple[str, str]] = []
    timestamps: list[datetime] = []

    for line in lines:
        ts_m = _TS.match(line)
        if ts_m:
            try:
                timestamps.append(datetime.strptime(ts_m.group(1), "%Y-%m-%d %H:%M:%S"))
            except ValueError:
                pass

        m = _DRY.search(line)
        if m:
            signals.append(
                {
                    "pair": m.group("pair"),
                    "size": float(m.group("size")),
                    "spread": float(m.group("spread")),
                    "pnl": float(m.group("pnl")),
                    "ts": timestamps[-1] if timestamps else None,
                }
            )
            continue

        m = _RISK.search(line)
        if m:
            risk_blocks.append((m.group("pair"), m.group("reason")))
            continue

        m = _VALID.search(line)
        if m:
            valid_blocks.append((m.group("pair"), m.group("reason")))
            continue

        m = _SKIP.search(line)
        if m:
            skips.append((m.group("pair"), m.group("reason")))

    # ── 1. Signal Frequency ───────────────────────────────────────────────────
    hdr("1 · Signal Frequency")
    if not signals:
        print("  No DRY RUN signals found in this log.")
        _suggest_no_signals()
    else:
        span_min = 0.0
        if len(timestamps) >= 2:
            span_min = (timestamps[-1] - timestamps[0]).total_seconds() / 60
        rate = len(signals) / span_min if span_min > 0 else 0

        print(f"  Total signals : {len(signals)}")
        print(f"  Log span      : {span_min:.0f} min")
        print(f"  Rate          : {rate:.1f} signals/min  ({rate * 60:.0f}/hr)")

        by_pair = Counter(s["pair"] for s in signals)
        for pair, count in by_pair.most_common():
            print(f"    {pair}: {count} signals")

        # Hourly histogram
        if signals and signals[0]["ts"]:
            by_hour: defaultdict[str, int] = defaultdict(int)
            for s in signals:
                if s["ts"]:
                    by_hour[s["ts"].strftime("%H:00")] += 1
            if len(by_hour) > 1:
                print("\n  Signals per hour:")
                for hour in sorted(by_hour):
                    bar = "█" * by_hour[hour]
                    print(f"    {hour}  {bar} {by_hour[hour]}")

    # ── 2. Cost Breakdown & Breakeven ─────────────────────────────────────────
    hdr("2 · Cost Breakdown & Breakeven")
    if signals:
        spreads = [s["spread"] for s in signals]
        pnls = [s["pnl"] for s in signals]

        print(
            f"  Spread  min={min(spreads):.1f}  avg={sum(spreads)/len(spreads):.1f}  max={max(spreads):.1f}  bps"
        )
        print(
            f"  Exp PnL min=${min(pnls):.2f}  avg=${sum(pnls)/len(pnls):.2f}  max=${max(pnls):.2f}"
        )
        print()

        # Approximate breakeven: where expected_pnl ≈ 0
        # Use the signals closest to pnl=0 to infer breakeven spread
        near_zero = sorted(signals, key=lambda s: abs(s["pnl"]))[:5]
        if near_zero:
            be_spread = sum(s["spread"] for s in near_zero) / len(near_zero)
            print(f"  Estimated breakeven spread : ~{be_spread:.1f} bps")
            print("  (signals with |pnl| < $0.10 cluster around this spread)")
        print()
        print("  Reminder: fee structure = CEX 10bps + DEX 30bps + gas ~$0.10")
        print("  Total round-trip fee on $10 notional ≈ $0.50 + gas ≈ $0.60")
        print("  Minimum profitable spread at $10 notional ≈ 60bps")
    else:
        print("  No signal data to analyze.")

    # ── 3. Parameter Sensitivity ──────────────────────────────────────────────
    hdr("3 · Parameter Sensitivity")
    if signals:
        thresholds = [20, 30, 40, 50, 60, 80, 100]
        print(f"  {'min_spread_bps':>16}  {'signals kept':>12}  {'avg exp_pnl':>12}")
        print(f"  {'─' * 44}")
        for thresh in thresholds:
            kept = [s for s in signals if s["spread"] >= thresh]
            avg_pnl = sum(s["pnl"] for s in kept) / len(kept) if kept else 0
            marker = " ← current" if thresh == 30 else ""
            print(f"  {thresh:>16}  {len(kept):>12}  ${avg_pnl:>10.2f}{marker}")

        print()
        sizes = [5.0, 10.0, 20.0, 50.0]
        if signals:
            # Use avg spread to estimate pnl scaling with size
            avg_spread = sum(s["spread"] for s in signals) / len(signals)
            avg_size = signals[0]["size"] if signals else 20.0
            print(f"  {'trade_size (ARB)':>18}  {'notional (~$)':>14}  {'est avg pnl':>12}")
            print(f"  {'─' * 48}")
            for sz in sizes:
                notional = sz * 0.50  # rough ARB price
                pnl_est = (avg_spread / 10000) * notional
                marker = " ← current" if sz == avg_size else ""
                print(f"  {sz:>18.1f}  ${notional:>13.2f}  ${pnl_est:>10.3f}{marker}")
    else:
        print("  No signal data to analyze.")

    # ── 4. Risk Limit Hits ────────────────────────────────────────────────────
    hdr("4 · Risk Limit Hits")
    total_blocks = len(risk_blocks) + len(valid_blocks) + len(skips)
    print(f"  Total blocked/skipped: {total_blocks}")

    if risk_blocks:
        print(f"\n  Risk blocks ({len(risk_blocks)}):")
        reason_counts = Counter(r for _, r in risk_blocks)
        for reason, count in reason_counts.most_common():
            print(f"    {count:>4}×  {reason[:72]}")

    if valid_blocks:
        print(f"\n  Validation blocks ({len(valid_blocks)}):")
        reason_counts = Counter(r for _, r in valid_blocks)
        for reason, count in reason_counts.most_common():
            print(f"    {count:>4}×  {reason[:72]}")

    if skips:
        print(f"\n  Score/decay skips ({len(skips)}):")
        reason_counts = Counter(r for _, r in skips)
        for reason, count in reason_counts.most_common():
            print(f"    {count:>4}×  {reason[:72]}")

    if total_blocks == 0:
        print("  No blocks recorded — all generated signals passed all checks.")

    # ── Summary ───────────────────────────────────────────────────────────────
    hdr("Summary")
    if signals:
        positive = [s for s in signals if s["pnl"] > 0]
        print(f"  {GREEN}{len(positive)}/{len(signals)} signals had positive expected PnL{RESET}")
        if len(signals) > 0:
            print(f"  Win rate (expected): {len(positive)/len(signals)*100:.0f}%")
    print(f"  Blocks/skips: {total_blocks}")
    if signals and total_blocks:
        total_seen = len(signals) + total_blocks
        print(f"  Opportunity capture rate: {len(signals)/total_seen*100:.0f}%")
    print()


def _suggest_no_signals() -> None:
    print()
    print("  Possible reasons:")
    print("  • min_spread_bps too high — try lowering to 20")
    print("  • min_profit_usd too high for trade_size")
    print("  • ARB/USDC pool not found on Arbitrum V2 — check RPC and factory address")
    print("  • No ARB_RPC_URL set — bot has no DEX prices, spread = 0")
    print()
    print("  Quick check commands:")
    print("  grep 'Signal generator' logs/bot_*.log | tail -5")
    print("  grep 'spread=0' logs/bot_*.log | wc -l")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    if len(sys.argv) > 1:
        path = Path(sys.argv[1])
    else:
        path = find_latest_log()
        if path is None:
            print("No log files found in logs/. Run the bot first.")
            sys.exit(1)

    if not path.exists():
        print(f"File not found: {path}")
        sys.exit(1)

    analyze(path)
