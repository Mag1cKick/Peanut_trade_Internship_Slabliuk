"""
scripts/lab4_demo.py — Lab 4 full pipeline demo.

Demonstrates all Lab 4 modules end-to-end using synthetic data
(no API keys or network required).

Run:
    python scripts/lab4_demo.py
    python scripts/lab4_demo.py --charts-dir /tmp/charts   # also save PNG charts

Exit codes:
    0 — all steps passed
    1 — one or more steps failed
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from decimal import Decimal

# Ensure project root is on sys.path when run as `python scripts/lab4_demo.py`
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

# ── Formatting helpers (same style as pricing_demo.py) ────────────────────────


def _sep(title: str = "") -> None:
    if title:
        print(f"\n{'-' * 56}")
        print(f"  {title}")
        print(f"{'-' * 56}")
    else:
        print()


def _ok(msg: str) -> None:
    print(f"  OK   {msg}")


def _val(label: str, value) -> None:
    print(f"  {label:<34} {value}")


def _fail(msg: str) -> None:
    print(f"  FAIL {msg}", file=sys.stderr)


# ── Synthetic market data ─────────────────────────────────────────────────────

_SYNTHETIC_BOOK = {
    "symbol": "ETH/USDT",
    "timestamp": 1_700_000_000_000,
    "bids": [
        (Decimal("2010.00"), Decimal("5.0")),
        (Decimal("2009.50"), Decimal("8.0")),
        (Decimal("2009.00"), Decimal("12.0")),
    ],
    "asks": [
        (Decimal("2010.50"), Decimal("4.0")),
        (Decimal("2011.00"), Decimal("7.0")),
        (Decimal("2011.50"), Decimal("10.0")),
    ],
    "best_bid": (Decimal("2010.00"), Decimal("5.0")),
    "best_ask": (Decimal("2010.50"), Decimal("4.0")),
    "mid_price": Decimal("2010.25"),
    "spread_bps": Decimal("2.49"),
}


def _make_arb_record(buy_price: float, sell_price: float, hour: int = 10):
    from inventory.pnl import ArbRecord, TradeLeg

    buy = TradeLeg(
        id=f"buy-{buy_price}-h{hour}",
        timestamp=datetime(2024, 3, 1, hour, 0, 0, tzinfo=UTC),
        venue="binance",
        symbol="ETH/USDT",
        side="buy",
        amount=Decimal("1"),
        price=Decimal(str(buy_price)),
        fee=Decimal("0.002"),
        fee_asset="USDT",
    )
    sell = TradeLeg(
        id=f"sell-{sell_price}-h{hour}",
        timestamp=datetime(2024, 3, 1, hour, 0, 0, tzinfo=UTC),
        venue="wallet",
        symbol="ETH/USDT",
        side="sell",
        amount=Decimal("1"),
        price=Decimal(str(sell_price)),
        fee=Decimal("0.002"),
        fee_asset="USDT",
    )
    return ArbRecord(
        id=f"arb-{buy_price}-{sell_price}",
        timestamp=datetime(2024, 3, 1, hour, 0, 0, tzinfo=UTC),
        buy_leg=buy,
        sell_leg=sell,
        gas_cost_usd=Decimal("0.50"),
    )


# ── Demo sections ─────────────────────────────────────────────────────────────


def _demo_orderbook(errors: list[str]) -> None:
    _sep("1. Order Book Analyzer — spread, depth, mid-price")
    try:
        from exchange.orderbook import OrderBookAnalyzer

        ob = OrderBookAnalyzer(_SYNTHETIC_BOOK)

        _val("Symbol:", ob.symbol)
        _val("Best bid:", f"{ob.best_bid[0]} USDT  (qty {ob.best_bid[1]})")
        _val("Best ask:", f"{ob.best_ask[0]} USDT  (qty {ob.best_ask[1]})")
        _val("Mid price:", f"{ob.mid_price} USDT")
        _val("Spread (bps):", f"{float(ob.quoted_spread_bps):.4f}")

        bid_depth = ob.depth_at_bps("bid", 50)
        ask_depth = ob.depth_at_bps("ask", 50)
        _val("Bid depth (within 50 bps):", f"{float(bid_depth):.1f} ETH")
        _val("Ask depth (within 50 bps):", f"{float(ask_depth):.1f} ETH")

        imbalance = ob.imbalance()
        _val("Order imbalance:", f"{float(imbalance):.4f}  (>0 = bid-heavy)")

        _ok("OrderBookAnalyzer parsed synthetic book successfully")
    except Exception as exc:
        _fail(f"OrderBookAnalyzer failed: {exc}")
        errors.append(str(exc))


def _demo_tracker(errors: list[str]):
    _sep("2. Inventory Tracker — multi-venue balances")
    try:
        from inventory.tracker import InventoryTracker, Venue

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(
            Venue.BINANCE,
            {
                "ETH": {"free": "9", "locked": "0"},
                "USDT": {"free": "500", "locked": "0"},
            },
        )
        tracker.update_from_wallet(Venue.WALLET, {"ETH": "1", "USDT": "4500"})

        snap = tracker.snapshot()
        totals = snap["totals"]
        venues = snap["venues"]

        _val("Total ETH across venues:", f"{totals['ETH']}")
        _val("Total USDT across venues:", f"{totals['USDT']}")
        _val("Binance ETH:", f"{venues[Venue.BINANCE]['ETH']['total']}")
        _val("Wallet  ETH:", f"{venues[Venue.WALLET]['ETH']['total']}")
        _val("Binance USDT:", f"{venues[Venue.BINANCE]['USDT']['total']}")
        _val("Wallet  USDT:", f"{venues[Venue.WALLET]['USDT']['total']}")

        skews = tracker.get_skews()
        for s in skews:
            _val(f"Skew {s['asset']}:", f"max deviation {s['max_deviation_pct']:.1f}%")

        _ok("InventoryTracker snapshot and skew computed")
        return tracker
    except Exception as exc:
        _fail(f"InventoryTracker failed: {exc}")
        errors.append(str(exc))
        return None


def _demo_pnl(errors: list[str]):
    _sep("3. PnL Engine — record arb trades & summary")
    try:
        from inventory.pnl import PnLEngine

        engine = PnLEngine()
        trade_data = [
            (1990, 2010, 10),
            (2000, 2015, 11),
            (1980, 2020, 12),
            (2005, 1995, 13),  # losing trade
            (1975, 2025, 14),
        ]
        for bp, sp, h in trade_data:
            engine.record(_make_arb_record(bp, sp, h))

        s = engine.summary()
        _val("Trades recorded:", s["total_trades"])
        _val("Total PnL (USD):", f"${float(s['total_pnl_usd']):.4f}")
        _val("Avg PnL / trade:", f"${float(s['avg_pnl_per_trade']):.4f}")
        _val("Avg PnL (bps):", f"{float(s['avg_pnl_bps']):.2f}")
        _val("Win rate:", f"{s['win_rate']:.1f}%")
        _val("Best trade:", f"${float(s['best_trade_pnl']):.4f}")
        _val("Worst trade:", f"${float(s['worst_trade_pnl']):.4f}")
        _val("Sharpe estimate:", f"{s['sharpe_estimate']:.4f}")

        _ok("PnLEngine recorded 5 trades and computed summary")
        return engine
    except Exception as exc:
        _fail(f"PnLEngine failed: {exc}")
        errors.append(str(exc))
        return None


def _demo_rebalancer(tracker, errors: list[str]) -> None:
    _sep("4. Rebalance Planner — detect skew, generate transfer plan")
    if tracker is None:
        print("  (skipped — tracker not available)")
        return
    try:
        from inventory.rebalancer import RebalancePlanner

        planner = RebalancePlanner(tracker)
        checks = planner.check_all()

        for c in checks:
            flag = "REBALANCE NEEDED" if c["needs_rebalance"] else "balanced"
            _val(
                f"  {c['asset']} skew:",
                f"{c['max_deviation_pct']:.1f}%  [{flag}]",
            )

        plan = planner.plan_all()
        if plan:
            print()
            for asset, transfers in plan.items():
                for t in transfers:
                    _val(
                        f"  Transfer {asset}:",
                        f"{t.amount} {asset}  {t.from_venue.value} -> {t.to_venue.value}"
                        f"  (fee~{t.estimated_fee}, ~{t.estimated_time_min} min)",
                    )
        else:
            _val("  Plan:", "no transfers required")

        all_transfers = [t for transfers in plan.values() for t in transfers]
        cost = planner.estimate_cost(all_transfers)
        _val("  Total transfers planned:", cost["total_transfers"])
        _val("  Estimated total fees:", f"{cost['total_fees_usd']} USD")
        _val("  Estimated max time:", f"~{cost['total_time_min']} min")

        _ok("RebalancePlanner detected skew and generated transfer plan")
    except Exception as exc:
        _fail(f"RebalancePlanner failed: {exc}")
        errors.append(str(exc))


def _demo_arb_checker(tracker, pnl_engine, errors: list[str]):
    _sep("5. Arb Checker — DEX/CEX spread check")
    try:
        from unittest.mock import MagicMock

        from integration.arb_checker import ArbChecker, SimplePricingAdapter

        dex_price = Decimal("2005.00")  # slightly below CEX mid
        pricing = SimplePricingAdapter(
            price=dex_price,
            price_impact_bps=Decimal("1.2"),
            fee_bps=Decimal("30"),
        )

        mock_cex = MagicMock()
        mock_cex.fetch_order_book.return_value = _SYNTHETIC_BOOK
        mock_cex.get_trading_fees.return_value = {
            "taker": Decimal("0.001"),
            "maker": Decimal("0.001"),
        }

        checker = ArbChecker(
            pricing_engine=pricing,
            exchange_client=mock_cex,
            inventory_tracker=tracker,
            pnl_engine=pnl_engine,
        )

        result = checker.check("ETH/USDT", size=1.0)

        _val("Pair:", result["pair"])
        _val("DEX price:", f"{result['dex_price']} USDT")
        _val("CEX bid:", f"{result['cex_bid']} USDT")
        _val("CEX ask:", f"{result['cex_ask']} USDT")
        _val("Gap (bps):", f"{float(result['gap_bps']):.2f}")
        _val("Direction:", result["direction"] or "none")
        _val("Estimated costs (bps):", f"{float(result['estimated_costs_bps']):.2f}")
        _val("Net PnL (bps):", f"{float(result['estimated_net_pnl_bps']):.2f}")
        _val("Inventory OK:", result["inventory_ok"])
        _val("Executable:", result["executable"])

        _ok("ArbChecker computed full DEX/CEX spread analysis")
        return result
    except Exception as exc:
        _fail(f"ArbChecker failed: {exc}")
        errors.append(str(exc))
        return None


def _demo_arb_logger(arb_result, errors: list[str]) -> None:
    _sep("6. Arb Logger — ring buffer + CSV export")
    try:
        from unittest.mock import MagicMock

        from integration.arb_logger import ArbLogger

        mock_checker = MagicMock()
        mock_checker.check.return_value = arb_result or {
            "pair": "ETH/USDT",
            "timestamp": datetime(2024, 3, 1, 12, 0, 0, tzinfo=UTC),
            "dex_price": Decimal("2005"),
            "cex_bid": Decimal("2010"),
            "cex_ask": Decimal("2010.50"),
            "gap_bps": Decimal("25"),
            "direction": "buy_dex_sell_cex",
            "estimated_costs_bps": Decimal("40"),
            "estimated_net_pnl_bps": Decimal("-15"),
            "inventory_ok": True,
            "executable": False,
            "details": {},
        }

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            csv_path = f.name

        logger = ArbLogger(mock_checker, csv_path=csv_path, maxlen=100)

        for i in range(5):
            logger.check("ETH/USDT", size=1.0, note=f"demo-{i}")

        stats = logger.stats()
        _val("Total logged:", stats["total_logged"])
        _val("Buffer size:", stats["buffer_size"])
        _val("Executable count:", stats["executable_count"])
        _val(
            "Avg net PnL (bps):",
            f"{stats['avg_net_pnl_bps']:.4f}",
        )
        _val("Pairs seen:", stats["pairs"])

        recent = logger.recent(3)
        _val("recent(3) entries:", len(recent))

        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="w") as f:
            export_path = f.name
        n = logger.export_csv(export_path)
        _val("export_csv() rows written:", n)
        _val("CSV file:", export_path)

        logger.flush()
        _val("After flush — buffer size:", len(logger._buffer))
        _val("After flush — total_logged:", logger._total_logged)

        _ok("ArbLogger ring buffer, CSV export, and flush all working")
    except Exception as exc:
        _fail(f"ArbLogger failed: {exc}")
        errors.append(str(exc))


def _demo_dashboard(tracker, pnl_engine, errors: list[str]) -> None:
    _sep("7. Inventory Dashboard — Rich terminal render")
    if tracker is None:
        print("  (skipped — tracker not available)")
        return
    try:
        from inventory.dashboard import InventoryDashboard

        dash = InventoryDashboard(tracker, pnl_engine=pnl_engine, title="Lab 4 Demo Dashboard")

        _val("Title:", dash._title)

        balance_table = dash._build_balance_table()
        _val("Balance table columns:", len(balance_table.columns))
        _val("Balance table rows:", balance_table.row_count)

        skew_table = dash._build_skew_table()
        _val("Skew table rows:", skew_table.row_count)

        pnl_panel = dash._build_pnl_panel()
        _val("PnL panel type:", type(pnl_panel).__name__)

        print()
        dash.print_once()

        _ok("InventoryDashboard rendered to terminal without error")
    except Exception as exc:
        _fail(f"InventoryDashboard failed: {exc}")
        errors.append(str(exc))


def _demo_bybit_client(errors: list[str]) -> None:
    _sep("9. Bybit Client — REST adapter config (stretch goal)")
    try:
        import sys
        from unittest.mock import MagicMock, patch

        # BybitClient wraps ccxt.bybit and calls fetch_time in __init__.
        # Mock ccxt so no network call is made — we verify the adapter layer.
        mock_exchange = MagicMock()
        mock_exchange.id = "bybit"
        mock_exchange.fetch_time.return_value = 1_700_000_000_000
        mock_ccxt = MagicMock()
        mock_ccxt.bybit.return_value = mock_exchange

        with patch.dict(sys.modules, {"ccxt": mock_ccxt}):
            from exchange.bybit_client import BybitClient

            cfg = {
                "apiKey": "demo_key",  # pragma: allowlist secret
                "secret": "demo_secret",  # pragma: allowlist secret
                "sandbox": True,
            }
            client = BybitClient(cfg)

        _val("Exchange id:", client._exchange.id)
        _val("Sandbox set:", mock_exchange.set_sandbox_mode.called)
        _val("Rate-limit weight start:", client._weight_used)
        _val("fetch_time called on init:", mock_exchange.fetch_time.called)

        _ok("BybitClient constructed with mocked ccxt — adapter layer verified without network")
    except Exception as exc:
        _fail(f"BybitClient failed: {exc}")
        errors.append(str(exc))


def _demo_ws_orderbook(errors: list[str]) -> None:
    _sep("10. WebSocket Order Book Stream — config + state (stretch goal)")
    try:
        from exchange.ws_orderbook import OrderBookStream

        # Construct without connecting — no network required
        stream = OrderBookStream("ETH/USDT", testnet=True, depth_limit=20)

        _val("Symbol:", stream._symbol)
        _val("Depth limit:", stream._depth_limit)
        _val("Testnet:", stream._testnet)
        _val("WS URL:", stream._ws_url)
        _val("REST base:", stream._rest_base)
        _val("Connected:", stream._ws is not None)
        _val("Synced:", stream._synced)

        # Apply a REST snapshot offline — exercises the core sync logic
        snap = {
            "lastUpdateId": 999,
            "bids": [["2010.00", "5.0"], ["2009.50", "3.0"]],
            "asks": [["2010.50", "4.0"], ["2011.00", "2.0"]],
        }
        stream._apply_snapshot(snap)
        _val("After snapshot — lastUpdateId:", stream._last_update_id)
        _val("After snapshot — synced:", stream._synced)
        _val("After snapshot — bid levels:", len(stream._bids))
        _val("After snapshot — ask levels:", len(stream._asks))

        _ok("OrderBookStream constructed and snapshot applied — offline sync logic verified")
    except Exception as exc:
        _fail(f"OrderBookStream failed: {exc}")
        errors.append(str(exc))


def _demo_charts(pnl_engine, charts_dir: str | None, errors: list[str]) -> None:
    _sep("8. PnL Charts — matplotlib chart export")
    if pnl_engine is None:
        print("  (skipped — PnL engine not available)")
        return
    try:
        from inventory.charts import PnLCharts

        charts = PnLCharts(pnl_engine)

        out_dir = charts_dir or tempfile.mkdtemp(prefix="lab4_charts_")
        os.makedirs(out_dir, exist_ok=True)

        chart_specs = [
            ("cumulative_pnl", "Cumulative PnL"),
            ("pnl_by_hour", "PnL by Hour"),
            ("trade_distribution", "Trade Distribution"),
            ("drawdown", "Drawdown"),
            ("all", "2x2 Overview"),
        ]

        for method_name, label in chart_specs:
            path = os.path.join(out_dir, f"{method_name}.png")
            fn = getattr(charts, method_name)
            fn(output_path=path)
            size_kb = os.path.getsize(path) // 1024
            _val(f"  {label}:", f"{path}  ({size_kb} KB)")

        _ok(f"All 5 charts saved to {out_dir}")
    except Exception as exc:
        _fail(f"PnLCharts failed: {exc}")
        errors.append(str(exc))


# ── Entry point ───────────────────────────────────────────────────────────────


def run(charts_dir: str | None = None) -> int:
    errors: list[str] = []

    print("=" * 56)
    print("  Peanut Trade — Lab 4 Full Pipeline Demo")
    print("=" * 56)

    _demo_orderbook(errors)
    tracker = _demo_tracker(errors)
    pnl_engine = _demo_pnl(errors)
    _demo_rebalancer(tracker, errors)

    arb_result = _demo_arb_checker(tracker, pnl_engine, errors)
    _demo_arb_logger(arb_result, errors)
    _demo_dashboard(tracker, pnl_engine, errors)
    _demo_charts(pnl_engine, charts_dir, errors)
    _demo_bybit_client(errors)
    _demo_ws_orderbook(errors)

    print("\n" + "=" * 56)
    if errors:
        print(f"  Demo FAILED ({len(errors)} error(s))")
        for e in errors:
            print(f"    - {e}")
        return 1

    print("  Demo PASSED — all Lab 4 modules working")
    print()
    print("  Modules demonstrated:")
    print("    exchange/orderbook.py        OrderBookAnalyzer")
    print("    inventory/tracker.py         InventoryTracker")
    print("    inventory/pnl.py             PnLEngine")
    print("    inventory/rebalancer.py      RebalancePlanner")
    print("    integration/arb_checker.py   ArbChecker")
    print("    integration/arb_logger.py    ArbLogger")
    print("    inventory/dashboard.py       InventoryDashboard  [stretch]")
    print("    inventory/charts.py          PnLCharts           [stretch]")
    print("    exchange/bybit_client.py     BybitClient         [stretch]")
    print("    exchange/ws_orderbook.py     OrderBookStream     [stretch]")
    print("=" * 56)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Peanut Trade Lab 4 pipeline demo — runs fully offline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python scripts/lab4_demo.py
  python scripts/lab4_demo.py --charts-dir /tmp/lab4_charts
        """,
    )
    parser.add_argument(
        "--charts-dir",
        default=None,
        help="Directory to save PNG chart files (default: system temp dir)",
    )
    args = parser.parse_args()
    return run(charts_dir=args.charts_dir)


if __name__ == "__main__":
    sys.exit(main())
