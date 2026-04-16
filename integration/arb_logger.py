"""
integration/arb_logger.py — Arb opportunity logger with CSV export.

Wraps ``ArbChecker`` (or any callable producing the same result dict) and
persistently logs every opportunity assessment to an in-memory ring buffer
and optionally to a CSV file.

Features
────────
* Ring buffer (``maxlen``) — keeps the last N results in RAM.
* Rotating CSV — appends rows to a configurable file path on every check.
* ``recent(n)`` — retrieve the last n log entries as dicts.
* ``export_csv(path)`` — dump the full in-memory buffer to a new CSV file.
* ``stats()`` — quick summary (total logged, executable count, avg net PnL).
* ``flush()`` — clear the in-memory buffer (does not affect the rotating file).

CSV columns (21 columns)
────────────────────────
logged_at, pair, timestamp, direction, dex_price, cex_bid, cex_ask,
gap_bps, estimated_costs_bps, estimated_net_pnl_bps,
inventory_ok, executable,
dex_fee_bps, dex_price_impact_bps, cex_fee_bps, cex_slippage_bps,
gas_cost_usd, size, gas_price_gwei, eth_price_usd, note

Usage::

    from integration.arb_checker import ArbChecker, SimplePricingAdapter
    from integration.arb_logger import ArbLogger

    checker = ArbChecker(pricing, cex_client, tracker, pnl)
    logger  = ArbLogger(checker, csv_path="arb_log.csv", maxlen=1000)

    result = logger.check("ETH/USDT", size=1.0)
    print(logger.stats())
    logger.export_csv("snapshot.csv")

CLI::

    python -m integration.arb_logger ETH/USDT --size 2.0 --export log.csv
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from collections import deque
from datetime import UTC, datetime
from decimal import Decimal

_CSV_FIELDS = [
    "logged_at",
    "pair",
    "timestamp",
    "direction",
    "dex_price",
    "cex_bid",
    "cex_ask",
    "gap_bps",
    "estimated_costs_bps",
    "estimated_net_pnl_bps",
    "inventory_ok",
    "executable",
    "dex_fee_bps",
    "dex_price_impact_bps",
    "cex_fee_bps",
    "cex_slippage_bps",
    "gas_cost_usd",
    "size",
    "gas_price_gwei",
    "eth_price_usd",
    "note",
]


def _result_to_row(
    result: dict,
    *,
    size: float = 1.0,
    gas_price_gwei: int = 20,
    eth_price_usd: Decimal | None = None,
    note: str = "",
) -> dict:
    """Convert an ArbChecker result dict to a flat CSV row dict."""
    d = result.get("details", {})
    return {
        "logged_at": datetime.now(tz=UTC).isoformat(),
        "pair": result["pair"],
        "timestamp": result["timestamp"].isoformat(),
        "direction": result.get("direction") or "",
        "dex_price": str(result["dex_price"]),
        "cex_bid": str(result["cex_bid"]),
        "cex_ask": str(result["cex_ask"]),
        "gap_bps": str(result["gap_bps"]),
        "estimated_costs_bps": str(result["estimated_costs_bps"]),
        "estimated_net_pnl_bps": str(result["estimated_net_pnl_bps"]),
        "inventory_ok": str(result["inventory_ok"]),
        "executable": str(result["executable"]),
        "dex_fee_bps": str(d.get("dex_fee_bps", "")),
        "dex_price_impact_bps": str(d.get("dex_price_impact_bps", "")),
        "cex_fee_bps": str(d.get("cex_fee_bps", "")),
        "cex_slippage_bps": str(d.get("cex_slippage_bps", "")),
        "gas_cost_usd": str(d.get("gas_cost_usd", "")),
        "size": str(size),
        "gas_price_gwei": str(gas_price_gwei),
        "eth_price_usd": str(eth_price_usd) if eth_price_usd is not None else "",
        "note": note,
    }


class ArbLogger:
    """
    Logs arb opportunity check results to memory and optionally to a CSV file.

    Parameters
    ----------
    checker:
        Any object with a ``check(pair, size, gas_price_gwei, eth_price_usd)``
        method that returns the ArbChecker result dict.
    csv_path:
        If provided, each call to ``check()`` appends a row to this CSV file.
        The file is created (with header) if it doesn't exist.
    maxlen:
        Maximum number of entries kept in the in-memory ring buffer.
    """

    def __init__(
        self,
        checker,
        csv_path: str | None = None,
        maxlen: int = 500,
    ) -> None:
        self._checker = checker
        self._csv_path = csv_path
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._total_logged: int = 0

        if csv_path is not None:
            self._ensure_csv_header(csv_path)

    # ── Core ───────────────────────────────────────────────────────────────────

    def check(
        self,
        pair: str,
        size: float = 1.0,
        gas_price_gwei: int = 20,
        eth_price_usd: Decimal | None = None,
        note: str = "",
    ) -> dict:
        """
        Run an arb check, log the result, and return it unchanged.
        """
        result = self._checker.check(
            pair,
            size=size,
            gas_price_gwei=gas_price_gwei,
            eth_price_usd=eth_price_usd,
        )
        self._log(
            result, size=size, gas_price_gwei=gas_price_gwei, eth_price_usd=eth_price_usd, note=note
        )
        return result

    def log_result(
        self,
        result: dict,
        *,
        size: float = 1.0,
        gas_price_gwei: int = 20,
        eth_price_usd: Decimal | None = None,
        note: str = "",
    ) -> None:
        """
        Log a pre-computed ArbChecker result dict (without running a check).
        Useful when you already hold the result from a direct ``ArbChecker.check()`` call.
        """
        self._log(
            result, size=size, gas_price_gwei=gas_price_gwei, eth_price_usd=eth_price_usd, note=note
        )

    # ── Query ──────────────────────────────────────────────────────────────────

    def recent(self, n: int = 10) -> list[dict]:
        """Return the last ``n`` log entries (newest last)."""
        entries = list(self._buffer)
        return entries[-n:] if n < len(entries) else entries

    def stats(self) -> dict:
        """
        Quick aggregate over the in-memory buffer.

        Returns
        -------
        dict with keys:
            total_logged     int     — all-time entries logged (survives flush)
            buffer_size      int     — current in-memory entries
            executable_count int     — entries where executable=True
            executable_pct   float   — executable / total in buffer
            avg_net_pnl_bps  float   — mean estimated_net_pnl_bps (buffer)
            pairs            list    — unique pairs seen in buffer
        """
        buf = list(self._buffer)
        if not buf:
            return {
                "total_logged": self._total_logged,
                "buffer_size": 0,
                "executable_count": 0,
                "executable_pct": 0.0,
                "avg_net_pnl_bps": 0.0,
                "pairs": [],
            }

        executable = [e for e in buf if e.get("executable") == "True"]
        net_pnl_values = []
        for e in buf:
            try:
                net_pnl_values.append(float(e["estimated_net_pnl_bps"]))
            except (KeyError, ValueError):
                pass

        avg_net = sum(net_pnl_values) / len(net_pnl_values) if net_pnl_values else 0.0
        pairs = sorted({e["pair"] for e in buf})

        return {
            "total_logged": self._total_logged,
            "buffer_size": len(buf),
            "executable_count": len(executable),
            "executable_pct": len(executable) / len(buf),
            "avg_net_pnl_bps": avg_net,
            "pairs": pairs,
        }

    def export_csv(self, path: str) -> int:
        """
        Write the current in-memory buffer to ``path`` as a CSV file.

        Returns the number of rows written.
        """
        rows = list(self._buffer)
        with open(path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        return len(rows)

    def flush(self) -> None:
        """Clear the in-memory buffer (does not affect ``csv_path``)."""
        self._buffer.clear()

    # ── Internal ───────────────────────────────────────────────────────────────

    def _log(
        self,
        result: dict,
        *,
        size: float,
        gas_price_gwei: int,
        eth_price_usd: Decimal | None,
        note: str,
    ) -> None:
        row = _result_to_row(
            result,
            size=size,
            gas_price_gwei=gas_price_gwei,
            eth_price_usd=eth_price_usd,
            note=note,
        )
        self._buffer.append(row)
        self._total_logged += 1

        if self._csv_path is not None:
            self._append_csv_row(self._csv_path, row)

    @staticmethod
    def _ensure_csv_header(path: str) -> None:
        if not os.path.exists(path):
            with open(path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
                writer.writeheader()

    @staticmethod
    def _append_csv_row(path: str, row: dict) -> None:
        with open(path, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS, extrasaction="ignore")
            writer.writerow(row)


# ── CLI ─────────────────────────────────────────────────────────────────────────


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Arb opportunity logger — runs a check and logs to CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python -m integration.arb_logger ETH/USDT --size 2.0\n"
            "  python -m integration.arb_logger ETH/USDT --export log.csv"
        ),
    )
    parser.add_argument("pair", help="Trading pair, e.g. ETH/USDT")
    parser.add_argument("--size", type=float, default=1.0)
    parser.add_argument("--gas-gwei", type=int, default=20)
    parser.add_argument("--dex-price", type=float, default=None)
    parser.add_argument("--export", default=None, help="CSV file to append logs to")
    parser.add_argument("--note", default="", help="Optional note to attach to log entry")
    args = parser.parse_args(argv)

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    try:
        from dotenv import load_dotenv

        load_dotenv(os.path.join(_root, ".env"))
    except ImportError:
        pass

    api_key = os.environ.get("BINANCE_API_KEY", "")
    api_secret = os.environ.get("BINANCE_API_SECRET", "")

    try:
        from exchange.client import ExchangeClient

        cex_client = ExchangeClient(
            {
                "apiKey": api_key,
                "secret": api_secret,
                "sandbox": True,
                "enableRateLimit": True,
            }
        )
    except Exception as exc:
        print(f"Error connecting to exchange: {exc}", file=sys.stderr)
        return 1

    try:
        from exchange.orderbook import OrderBookAnalyzer

        raw_book = cex_client.fetch_order_book(args.pair, limit=20)
        mid = float(OrderBookAnalyzer(raw_book).mid_price)
    except Exception as exc:
        print(f"Error fetching order book: {exc}", file=sys.stderr)
        return 1

    dex_price_val = (
        Decimal(str(args.dex_price))
        if args.dex_price is not None
        else Decimal(str(mid)) * Decimal("0.998")
    )

    from integration.arb_checker import ArbChecker, SimplePricingAdapter
    from inventory.pnl import PnLEngine
    from inventory.tracker import InventoryTracker, Venue

    pricing = SimplePricingAdapter(
        price=dex_price_val,
        price_impact_bps=Decimal("1.2"),
        fee_bps=Decimal("30"),
    )

    base, quote = args.pair.split("/")
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    tracker.update_from_cex(
        Venue.BINANCE,
        {base: {"free": "100", "locked": "0"}, quote: {"free": "500000", "locked": "0"}},
    )
    tracker.update_from_wallet(Venue.WALLET, {base: "100", quote: "500000"})

    checker = ArbChecker(
        pricing_engine=pricing,
        exchange_client=cex_client,
        inventory_tracker=tracker,
        pnl_engine=PnLEngine(),
    )

    logger = ArbLogger(checker, csv_path=args.export)

    try:
        result = logger.check(
            args.pair,
            size=args.size,
            gas_price_gwei=args.gas_gwei,
            note=args.note,
        )
    except Exception as exc:
        print(f"Error running arb check: {exc}", file=sys.stderr)
        return 1

    s = logger.stats()
    print(f"\nPair:           {result['pair']}")
    print(f"Direction:      {result['direction'] or 'none'}")
    print(f"Net PnL (bps):  {float(result['estimated_net_pnl_bps']):.2f}")
    print(f"Executable:     {result['executable']}")
    print(f"\nLogger stats:   {s['total_logged']} logged, " f"{s['executable_count']} executable")

    if args.export:
        print(f"CSV appended to: {args.export}")

    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
