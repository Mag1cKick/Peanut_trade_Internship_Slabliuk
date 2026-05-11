"""
db/trades.py — Persistent SQLite trade ledger.

Stores every completed arb trade across sessions/days.
The file lives at data/trades.db (created on first use).

Schema:
  trades(id, ts, pair, direction, size, dex_price, cex_price,
         spread_bps, gross_pnl, net_pnl, gas_usd, portfolio_usd, notes)
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "trades.db"


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    return con


def init() -> None:
    """Create tables if they don't exist."""
    with _connect() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS trades (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                ts            TEXT NOT NULL,
                date          TEXT NOT NULL,
                pair          TEXT NOT NULL,
                direction     TEXT NOT NULL,
                size          REAL NOT NULL,
                dex_price     REAL,
                cex_price     REAL,
                spread_bps    REAL,
                gross_pnl     REAL,
                net_pnl       REAL,
                gas_usd       REAL,
                portfolio_usd REAL,
                notes         TEXT
            )
        """)


@dataclass
class TradeRecord:
    ts: str
    pair: str
    direction: str
    size: float
    dex_price: float
    cex_price: float
    spread_bps: float
    gross_pnl: float
    net_pnl: float
    gas_usd: float = 0.009
    portfolio_usd: float = 0.0
    notes: str = ""

    @property
    def date(self) -> str:
        return self.ts[:10]


def insert(rec: TradeRecord) -> int:
    init()
    with _connect() as con:
        cur = con.execute(
            """
            INSERT INTO trades
              (ts, date, pair, direction, size, dex_price, cex_price,
               spread_bps, gross_pnl, net_pnl, gas_usd, portfolio_usd, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
            (
                rec.ts,
                rec.date,
                rec.pair,
                rec.direction,
                rec.size,
                rec.dex_price,
                rec.cex_price,
                rec.spread_bps,
                rec.gross_pnl,
                rec.net_pnl,
                rec.gas_usd,
                rec.portfolio_usd,
                rec.notes,
            ),
        )
        return cur.lastrowid


def all_trades() -> list[sqlite3.Row]:
    init()
    with _connect() as con:
        return con.execute("SELECT * FROM trades ORDER BY ts").fetchall()


def daily_summary(date: str | None = None) -> dict:
    """Return aggregated stats for a given date (today if None)."""
    init()
    d = date or datetime.now().strftime("%Y-%m-%d")
    with _connect() as con:
        row = con.execute(
            """
            SELECT COUNT(*) as n,
                   SUM(net_pnl) as total_net,
                   SUM(gross_pnl) as total_gross,
                   SUM(gas_usd) as total_gas,
                   AVG(spread_bps) as avg_spread,
                   MAX(spread_bps) as max_spread,
                   MIN(spread_bps) as min_spread,
                   SUM(CASE WHEN net_pnl > 0 THEN 1 ELSE 0 END) as wins
            FROM trades WHERE date = ?
        """,
            (d,),
        ).fetchone()
    return dict(row) if row else {}


def print_ledger(date: str | None = None) -> None:
    """Print a formatted trade ledger, optionally filtered by date."""
    init()
    with _connect() as con:
        if date:
            rows = con.execute(
                "SELECT * FROM trades WHERE date = ? ORDER BY ts", (date,)
            ).fetchall()
        else:
            rows = con.execute("SELECT * FROM trades ORDER BY ts").fetchall()

    if not rows:
        print("No trades found.")
        return

    print(
        f"{'#':>3} {'Time':>8} {'Pair':>12} {'Dir':>20} "
        f"{'Size':>6} {'DEX':>8} {'CEX':>8} {'Sprd':>7} {'Net PnL':>9} {'Port':>8}"
    )
    print("-" * 100)
    total_pnl = 0.0
    for r in rows:
        ts_short = r["ts"][11:19]
        total_pnl += r["net_pnl"] or 0
        print(
            f"{r['id']:>3} {ts_short:>8} {r['pair']:>12} {r['direction']:>20} "
            f"{r['size']:>6.2f} {r['dex_price']:>8.4f} {r['cex_price']:>8.4f} "
            f"{r['spread_bps']:>6.1f}bps {r['net_pnl']:>+9.4f} "
            f"${r['portfolio_usd']:>7.2f}"
        )
    print("-" * 100)
    print(f"{'Total':>68} {total_pnl:>+9.4f}")
