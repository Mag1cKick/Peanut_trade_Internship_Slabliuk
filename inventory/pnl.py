"""
inventory/pnl.py — P&L engines for position tracking and arb trade ledger.

Two engines serve different purposes:

PositionPnLEngine  — cost-basis P&L over CostBasisTracker positions
                     (unrealized, realized, return %).  Used for position
                     monitoring.

PnLEngine          — arb-trade ledger.  Records completed TradeLeg pairs
                     (ArbRecord) and produces aggregate summaries, win rates,
                     Sharpe estimates, CSV export, and a CLI dashboard.

All monetary values are Decimal.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from inventory.tracker import CostBasisTracker, Venue

# ══════════════════════════════════════════════════════════════════════════════
# PositionPnLEngine — cost-basis position P&L
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class PnLSnapshot:
    """P&L breakdown for a single asset position."""

    asset: str
    qty: Decimal
    avg_cost: Decimal
    mark_price: Decimal
    unrealized_pnl: Decimal
    realized_pnl: Decimal
    total_fees: Decimal
    total_pnl: Decimal
    return_pct: Decimal


@dataclass
class PortfolioPnL:
    """Aggregate P&L across all positions."""

    snapshots: list[PnLSnapshot]
    total_unrealized: Decimal
    total_realized: Decimal
    total_fees: Decimal
    net_pnl: Decimal


class PositionPnLEngine:
    """
    Computes realized and unrealized P&L for all positions in a
    :class:`~inventory.tracker.CostBasisTracker`.
    """

    def __init__(self, tracker: CostBasisTracker) -> None:
        self._tracker = tracker

    def snapshot(self, mark_prices: dict[str, Decimal]) -> PortfolioPnL:
        """Compute a full P&L snapshot for every position in the tracker."""
        snapshots: list[PnLSnapshot] = []
        total_unrealized = Decimal("0")
        total_realized = Decimal("0")
        total_fees = Decimal("0")

        for asset, pos in self._tracker.all_positions_including_closed().items():
            mark = mark_prices.get(asset, pos.avg_cost)
            unrealized = self._tracker.unrealized_pnl(asset, mark)
            realized = pos.realized_pnl
            fees = pos.total_fees

            cost_basis = pos.avg_cost * pos.qty
            return_pct = unrealized / cost_basis if cost_basis > 0 else Decimal("0")

            total_unrealized += unrealized
            total_realized += realized
            total_fees += fees

            snapshots.append(
                PnLSnapshot(
                    asset=asset,
                    qty=pos.qty,
                    avg_cost=pos.avg_cost,
                    mark_price=mark,
                    unrealized_pnl=unrealized,
                    realized_pnl=realized,
                    total_fees=fees,
                    total_pnl=realized + unrealized,
                    return_pct=return_pct,
                )
            )

        return PortfolioPnL(
            snapshots=snapshots,
            total_unrealized=total_unrealized,
            total_realized=total_realized,
            total_fees=total_fees,
            net_pnl=total_realized + total_unrealized,
        )

    def asset_pnl(self, asset: str, mark_price: Decimal) -> PnLSnapshot:
        """P&L snapshot for a single asset."""
        pos = self._tracker.get_position(asset)
        unrealized = self._tracker.unrealized_pnl(asset, mark_price)
        cost_basis = pos.avg_cost * pos.qty
        return_pct = unrealized / cost_basis if cost_basis > 0 else Decimal("0")

        return PnLSnapshot(
            asset=asset,
            qty=pos.qty,
            avg_cost=pos.avg_cost,
            mark_price=mark_price,
            unrealized_pnl=unrealized,
            realized_pnl=pos.realized_pnl,
            total_fees=pos.total_fees,
            total_pnl=pos.realized_pnl + unrealized,
            return_pct=return_pct,
        )


# ══════════════════════════════════════════════════════════════════════════════
# TradeLeg / ArbRecord — arb trade primitives
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class TradeLeg:
    """Single execution leg of an arbitrage trade."""

    id: str
    timestamp: datetime
    venue: Venue
    symbol: str  # e.g. "ETH/USDT"
    side: str  # "buy" or "sell"
    amount: Decimal  # Base asset quantity
    price: Decimal  # Execution price
    fee: Decimal
    fee_asset: str


@dataclass
class ArbRecord:
    """
    Complete arbitrage trade: one buy leg and one sell leg.

    Gross P&L  = sell revenue − buy cost
    Net P&L    = gross − buy fee − sell fee − gas
    """

    id: str
    timestamp: datetime
    buy_leg: TradeLeg
    sell_leg: TradeLeg
    gas_cost_usd: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def notional(self) -> Decimal:
        """Trade size in quote currency (buy-side cost)."""
        return self.buy_leg.price * self.buy_leg.amount

    @property
    def gross_pnl(self) -> Decimal:
        """Price-difference revenue before fees."""
        sell_revenue = self.sell_leg.price * self.sell_leg.amount
        buy_cost = self.buy_leg.price * self.buy_leg.amount
        return sell_revenue - buy_cost

    @property
    def total_fees(self) -> Decimal:
        """Sum of both leg fees plus gas."""
        return self.buy_leg.fee + self.sell_leg.fee + self.gas_cost_usd

    @property
    def net_pnl(self) -> Decimal:
        """Gross P&L minus all fees."""
        return self.gross_pnl - self.total_fees

    @property
    def net_pnl_bps(self) -> Decimal:
        """Net P&L expressed in basis points of notional."""
        if self.notional == 0:
            return Decimal("0")
        return self.net_pnl / self.notional * Decimal("10000")


# ══════════════════════════════════════════════════════════════════════════════
# PnLEngine — arb trade ledger
# ══════════════════════════════════════════════════════════════════════════════


class PnLEngine:
    """
    Tracks all arbitrage trades and produces P&L reports.

    Usage::

        engine = PnLEngine()
        engine.record(arb_record)
        print(engine.summary())
    """

    def __init__(self) -> None:
        self.trades: list[ArbRecord] = []

    def record(self, trade: ArbRecord) -> None:
        """Append a completed arb trade to the ledger."""
        self.trades.append(trade)

    def summary(self) -> dict:
        """
        Aggregate P&L summary across all recorded trades.

        Returns::

            {
                'total_trades':      int,
                'total_pnl_usd':     Decimal,
                'total_fees_usd':    Decimal,
                'avg_pnl_per_trade': Decimal,
                'avg_pnl_bps':       Decimal,
                'win_rate':          float,     # percentage of profitable trades
                'best_trade_pnl':    Decimal,
                'worst_trade_pnl':   Decimal,
                'total_notional':    Decimal,
                'sharpe_estimate':   float,     # mean(pnl) / stddev(pnl)
                'pnl_by_hour':       dict,      # {hour_int: total_pnl}
            }
        """
        if not self.trades:
            return {
                "total_trades": 0,
                "total_pnl_usd": Decimal("0"),
                "total_fees_usd": Decimal("0"),
                "avg_pnl_per_trade": Decimal("0"),
                "avg_pnl_bps": Decimal("0"),
                "win_rate": 0.0,
                "best_trade_pnl": Decimal("0"),
                "worst_trade_pnl": Decimal("0"),
                "total_notional": Decimal("0"),
                "sharpe_estimate": 0.0,
                "pnl_by_hour": {},
            }

        pnls = [t.net_pnl for t in self.trades]
        n = len(self.trades)
        total_pnl = sum(pnls, Decimal("0"))
        total_fees = sum((t.total_fees for t in self.trades), Decimal("0"))
        total_notional = sum((t.notional for t in self.trades), Decimal("0"))

        wins = sum(1 for p in pnls if p > 0)
        win_rate = wins / n * 100.0

        avg_pnl = total_pnl / n
        avg_bps = sum((t.net_pnl_bps for t in self.trades), Decimal("0")) / n

        # Sharpe: mean / stddev of net PnL values (rough, not annualised).
        if n >= 2:
            pnls_float = [float(p) for p in pnls]
            std_f = statistics.stdev(pnls_float)
            mean_f = sum(pnls_float) / n
            sharpe = mean_f / std_f if std_f != 0.0 else 0.0
        else:
            sharpe = 0.0

        # P&L grouped by UTC hour of the trade timestamp.
        pnl_by_hour: dict[int, Decimal] = {}
        for t in self.trades:
            h = t.timestamp.hour
            pnl_by_hour[h] = pnl_by_hour.get(h, Decimal("0")) + t.net_pnl

        return {
            "total_trades": n,
            "total_pnl_usd": total_pnl,
            "total_fees_usd": total_fees,
            "avg_pnl_per_trade": avg_pnl,
            "avg_pnl_bps": avg_bps,
            "win_rate": win_rate,
            "best_trade_pnl": max(pnls),
            "worst_trade_pnl": min(pnls),
            "total_notional": total_notional,
            "sharpe_estimate": sharpe,
            "pnl_by_hour": pnl_by_hour,
        }

    def recent(self, n: int = 10) -> list[dict]:
        """
        Last ``n`` trades as summary dicts for CLI display.

        Returns most-recent first.  Each dict contains: id, timestamp,
        symbol, buy_venue, sell_venue, gross_pnl, net_pnl, net_pnl_bps,
        total_fees, notional.
        """
        slice_ = self.trades[-n:] if len(self.trades) > n else list(self.trades)
        result = []
        for t in reversed(slice_):
            result.append(
                {
                    "id": t.id,
                    "timestamp": t.timestamp,
                    "symbol": t.buy_leg.symbol,
                    "buy_venue": t.buy_leg.venue,
                    "sell_venue": t.sell_leg.venue,
                    "gross_pnl": t.gross_pnl,
                    "net_pnl": t.net_pnl,
                    "net_pnl_bps": t.net_pnl_bps,
                    "total_fees": t.total_fees,
                    "notional": t.notional,
                }
            )
        return result

    def export_csv(self, filepath: str) -> None:
        """
        Export all trades to CSV.

        Columns: id, timestamp, symbol, buy_venue, sell_venue, buy_price,
        sell_price, amount, gross_pnl, total_fees, net_pnl, net_pnl_bps,
        notional, gas_cost_usd.
        """
        fieldnames = [
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
        ]
        with open(filepath, "w", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            for t in self.trades:
                buy_venue = (
                    t.buy_leg.venue.value
                    if hasattr(t.buy_leg.venue, "value")
                    else str(t.buy_leg.venue)
                )
                sell_venue = (
                    t.sell_leg.venue.value
                    if hasattr(t.sell_leg.venue, "value")
                    else str(t.sell_leg.venue)
                )
                writer.writerow(
                    {
                        "id": t.id,
                        "timestamp": t.timestamp.isoformat(),
                        "symbol": t.buy_leg.symbol,
                        "buy_venue": buy_venue,
                        "sell_venue": sell_venue,
                        "buy_price": str(t.buy_leg.price),
                        "sell_price": str(t.sell_leg.price),
                        "amount": str(t.buy_leg.amount),
                        "gross_pnl": str(t.gross_pnl),
                        "total_fees": str(t.total_fees),
                        "net_pnl": str(t.net_pnl),
                        "net_pnl_bps": str(t.net_pnl_bps),
                        "notional": str(t.notional),
                        "gas_cost_usd": str(t.gas_cost_usd),
                    }
                )


# ══════════════════════════════════════════════════════════════════════════════
# CLI
# ══════════════════════════════════════════════════════════════════════════════


def _make_demo_engine() -> PnLEngine:
    """Build a demo engine with sample trades for CLI display."""
    from inventory.tracker import Venue

    engine = PnLEngine()
    base_ts = datetime(2024, 1, 15, 14, 0, 0, tzinfo=UTC)

    sample = [
        # (buy_price, sell_price, amount, buy_fee, sell_fee, gas, minutes_ago)
        (
            Decimal("2000"),
            Decimal("2001.25"),
            Decimal("1"),
            Decimal("0.40"),
            Decimal("0.40"),
            Decimal("0"),
            8,
        ),
        (
            Decimal("2001"),
            Decimal("2001.90"),
            Decimal("1"),
            Decimal("0.40"),
            Decimal("0.40"),
            Decimal("0"),
            10,
        ),
        (
            Decimal("2002"),
            Decimal("2001.40"),
            Decimal("1"),
            Decimal("0.40"),
            Decimal("0.40"),
            Decimal("0"),
            13,
        ),
        (
            Decimal("2000"),
            Decimal("2001.80"),
            Decimal("1"),
            Decimal("0.40"),
            Decimal("0.40"),
            Decimal("0"),
            16,
        ),
    ]

    from datetime import timedelta

    for i, (bp, sp, amt, bf, sf, gas, mins) in enumerate(sample):
        ts = base_ts - timedelta(minutes=mins)
        buy_leg = TradeLeg(
            id=f"buy-{i}",
            timestamp=ts,
            venue=Venue.WALLET,
            symbol="ETH/USDT",
            side="buy",
            amount=amt,
            price=bp,
            fee=bf,
            fee_asset="USDT",
        )
        sell_leg = TradeLeg(
            id=f"sell-{i}",
            timestamp=ts,
            venue=Venue.BINANCE,
            symbol="ETH/USDT",
            side="sell",
            amount=amt,
            price=sp,
            fee=sf,
            fee_asset="USDT",
        )
        engine.record(
            ArbRecord(
                id=f"arb-{i}", timestamp=ts, buy_leg=buy_leg, sell_leg=sell_leg, gas_cost_usd=gas
            )
        )

    return engine


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="P&L dashboard — arb trade ledger",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python -m inventory.pnl --summary\n  python -m inventory.pnl --recent 5",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--summary", action="store_true", help="Show aggregate PnL summary")
    group.add_argument(
        "--recent", metavar="N", type=int, nargs="?", const=10, help="Show last N trades"
    )
    args = parser.parse_args(argv)

    engine = _make_demo_engine()

    if args.summary:
        s = engine.summary()
        W = 45
        top = "═" * W
        print(f"\nPnL Summary (demo)\n{top}")
        print(f"Total Trades:       {s['total_trades']:>8}")
        print(f"Win Rate:           {s['win_rate']:>7.1f}%")
        print(f"Total PnL:          ${float(s['total_pnl_usd']):>8.2f}")
        print(f"Total Fees:         ${float(s['total_fees_usd']):>8.2f}")
        print(f"Avg PnL/Trade:      ${float(s['avg_pnl_per_trade']):>8.2f}")
        print(f"Avg PnL (bps):      {float(s['avg_pnl_bps']):>7.1f} bps")
        print(f"Best Trade:         ${float(s['best_trade_pnl']):>8.2f}")
        print(f"Worst Trade:        ${float(s['worst_trade_pnl']):>8.2f}")
        print(f"Total Notional:     ${float(s['total_notional']):>10,.2f}")
        print(f"Sharpe (rough):     {s['sharpe_estimate']:>8.2f}")
        print()

        recent = engine.recent(4)
        print("Recent Trades:")
        for r in recent:
            ts = r["timestamp"].strftime("%H:%M")
            sym = r["symbol"].split("/")[0]
            bv = r["buy_venue"].value if hasattr(r["buy_venue"], "value") else str(r["buy_venue"])
            sv = (
                r["sell_venue"].value if hasattr(r["sell_venue"], "value") else str(r["sell_venue"])
            )
            pnl = float(r["net_pnl"])
            bps = float(r["net_pnl_bps"])
            flag = "✅" if pnl >= 0 else "❌"
            sign = "+" if pnl >= 0 else ""
            print(f"  {ts}  {sym}  Buy {bv} / Sell {sv}  {sign}${pnl:.2f} ({bps:.1f} bps) {flag}")
        return 0

    if args.recent is not None:
        recent = engine.recent(args.recent)
        print(f"{'Time':<6}  {'Symbol':<10}  {'Net PnL':>8}  {'bps':>6}  {'Notional':>12}")
        print("-" * 50)
        for r in recent:
            ts = r["timestamp"].strftime("%H:%M")
            sign = "+" if r["net_pnl"] >= 0 else ""
            print(
                f"{ts:<6}  {r['symbol']:<10}  "
                f"{sign}${float(r['net_pnl']):>6.2f}  "
                f"{float(r['net_pnl_bps']):>5.1f}  "
                f"${float(r['notional']):>10,.2f}"
            )
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
