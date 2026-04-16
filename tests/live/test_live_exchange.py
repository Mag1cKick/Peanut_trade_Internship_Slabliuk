"""
tests/live/test_live_exchange.py — Live integration tests against Binance testnet.

Run with real API keys:
    pytest tests/live/ -v -s

Skipped automatically when BINANCE_API_KEY / BINANCE_API_SECRET are not set.

Required env vars:
    BINANCE_API_KEY      — Binance testnet API key
    BINANCE_API_SECRET   — Binance testnet API secret

Get testnet keys at: https://testnet.binance.vision/
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from decimal import Decimal

import pytest

# ---------------------------------------------------------------------------
# Key detection — all tests skip if credentials not present
# ---------------------------------------------------------------------------
_API_KEY = os.environ.get("BINANCE_API_KEY", "")
_API_SECRET = os.environ.get("BINANCE_API_SECRET", "")
_HAVE_KEYS = bool(_API_KEY and _API_SECRET)

pytestmark = pytest.mark.skipif(
    not _HAVE_KEYS,
    reason="BINANCE_API_KEY / BINANCE_API_SECRET not set — skipping live tests",
)

_PAIR = "ETH/USDT"
_BASE = "ETH"
_QUOTE = "USDT"


# ---------------------------------------------------------------------------
# Shared client fixture (module scope = one connection for all tests)
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def client():
    from exchange.client import ExchangeClient

    return ExchangeClient(
        {
            "apiKey": _API_KEY,  # pragma: allowlist secret
            "secret": _API_SECRET,  # pragma: allowlist secret
            "sandbox": True,
            "enableRateLimit": True,
        }
    )


# ---------------------------------------------------------------------------
# Test 1 — Fetch live order book and show spread / depth / imbalance
# ---------------------------------------------------------------------------
class TestLiveOrderBook:
    """Fetch a live snapshot from Binance testnet and validate the analysis."""

    def test_fetch_order_book_and_analysis(self, client):
        from exchange.orderbook import OrderBookAnalyzer

        book = client.fetch_order_book(_PAIR, limit=20)

        # --- structural checks ---
        assert book["symbol"] == _PAIR
        assert len(book["bids"]) > 0, "Expected bids in live order book"
        assert len(book["asks"]) > 0, "Expected asks in live order book"
        assert book["mid_price"] > 0, "Mid price should be positive"
        assert book["spread_bps"] >= 0, "Spread should be non-negative"

        analyzer = OrderBookAnalyzer(book)

        best_bid_price, best_bid_qty = analyzer.best_bid
        best_ask_price, best_ask_qty = analyzer.best_ask

        assert best_bid_price > 0
        assert best_ask_price > best_bid_price, "Ask must be above bid"

        # --- depth ---
        depth = analyzer.depth_at_bps(10)
        assert depth["bid_depth_base"] >= 0
        assert depth["ask_depth_base"] >= 0

        # --- imbalance in [-1, +1] ---
        imbalance = analyzer.imbalance()
        assert -1.0 <= float(imbalance) <= 1.0

        # --- walk the book: buy 0.5 ETH ---
        walk = analyzer.walk_the_book("buy", 0.5)
        assert walk["filled"] > 0
        assert walk["avg_price"] > 0
        assert walk["slippage_bps"] >= 0

        print(
            f"\n[Order Book] {_PAIR}"
            f"\n  best bid:  ${float(best_bid_price):,.2f} × {float(best_bid_qty):.4f}"
            f"\n  best ask:  ${float(best_ask_price):,.2f} × {float(best_ask_qty):.4f}"
            f"\n  mid price: ${float(analyzer.mid_price):,.2f}"
            f"\n  spread:    {float(book['spread_bps']):.2f} bps"
            f"\n  imbalance: {float(imbalance):+.3f}  "
            f"({'buy' if imbalance > 0 else 'sell'} pressure)"
            f"\n  depth ±10bps — bids: {float(depth['bid_depth_base']):.4f} {_BASE}"
            f"  asks: {float(depth['ask_depth_base']):.4f} {_BASE}"
            f"\n  walk buy 0.5 {_BASE}: avg=${float(walk['avg_price']):,.2f}"
            f"  slippage={float(walk['slippage_bps']):.2f} bps"
        )


# ---------------------------------------------------------------------------
# Test 2 — Place and cancel a LIMIT IOC order on testnet
# ---------------------------------------------------------------------------
class TestLiveOrderPlacement:
    """
    Place a LIMIT IOC buy order at 10 % below market.
    IOC semantics: if not immediately filled, the exchange cancels it.
    We verify the order round-trip and confirm the final status is 'expired'.
    """

    def test_place_and_cancel_limit_ioc_order(self, client):
        # Fetch current market price
        book = client.fetch_order_book(_PAIR, limit=5)
        best_ask = float(book["best_ask"][0])
        assert best_ask > 0, "Need a live ask price"

        # Place a buy IOC at 10% below market — guaranteed not to fill
        limit_price = round(best_ask * 0.90, 2)
        qty = 0.01  # 0.01 ETH — small but above testnet minimums

        print(
            f"\n[Order] Placing LIMIT IOC buy {qty} {_BASE} @ ${limit_price:,.2f} "
            f"(market ask: ${best_ask:,.2f})"
        )

        order = client.create_limit_ioc_order(_PAIR, "buy", amount=qty, price=limit_price)

        assert order["id"], "Order should have an id"
        assert order["symbol"] == _PAIR
        assert order["side"] == "buy"
        assert order["type"] == "limit"

        print(
            f"  → order id:      {order['id']}"
            f"\n  → time_in_force: {order['time_in_force']}"
            f"\n  → status:        {order['status']}"
            f"\n  → filled:        {float(order['amount_filled']):.6f} {_BASE}"
        )

        # IOC orders must be either 'expired' (0 fill) or 'filled' (lucky fill)
        assert order["status"] in (
            "expired",
            "filled",
            "partially_filled",
            "canceled",
            "cancelled",
            "open",
        ), f"Unexpected order status: {order['status']}"

        # If somehow still open (exchange lag), fetch latest status
        if order["status"] == "open":
            time.sleep(2)
            order = client.fetch_order_status(order["id"], _PAIR)
            print(f"  → status after 2s: {order['status']}")

        # IOC at 10% below market must not be filled
        assert (
            float(order["amount_filled"]) == 0.0
        ), f"IOC order 10% below market should not fill, got {order['amount_filled']}"

        print(f"  ✓ Order {order['id']} completed with status '{order['status']}'")


# ---------------------------------------------------------------------------
# Test 3 — Portfolio snapshot across Binance testnet + wallet
# ---------------------------------------------------------------------------
class TestLivePortfolioSnapshot:
    """Fetch live CEX balances and build a multi-venue InventoryTracker snapshot."""

    def test_portfolio_snapshot(self, client):
        from inventory.tracker import InventoryTracker, Venue

        # Fetch live Binance testnet balances
        cex_balances = client.fetch_balance()

        assert isinstance(cex_balances, dict)
        # Binance testnet provides seeded funds; there should be assets
        print(f"\n[Portfolio] Binance testnet assets: {list(cex_balances.keys())}")

        # Simulate on-chain wallet alongside CEX
        wallet_balances = {
            "ETH": Decimal("5.0"),
            "USDT": Decimal("10000.0"),
        }

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(Venue.BINANCE, cex_balances)
        tracker.update_from_wallet(Venue.WALLET, wallet_balances)

        snapshot = tracker.snapshot()

        assert "venues" in snapshot
        assert "totals" in snapshot
        assert "timestamp" in snapshot

        print(f"  Snapshot timestamp: {snapshot['timestamp']}")
        for asset, total in snapshot["totals"].items():
            binance_free = (
                snapshot["venues"].get("binance", {}).get(asset, {}).get("free", Decimal("0"))
            )
            wallet_free = (
                snapshot["venues"].get("wallet", {}).get(asset, {}).get("free", Decimal("0"))
            )
            print(
                f"  {asset:6s}  binance={float(binance_free):>14,.4f}"
                f"  wallet={float(wallet_free):>14,.4f}"
                f"  total={float(total):>14,.4f}"
            )

        # ETH and USDT must appear in totals (wallet always has them)
        for asset in ("ETH", "USDT"):
            assert asset in snapshot["totals"], f"{asset} missing from totals"

        # can_execute sanity check
        result = tracker.can_execute(
            buy_venue=Venue.WALLET,
            buy_asset="USDT",
            buy_amount=Decimal("100"),
            sell_venue=Venue.BINANCE,
            sell_asset="ETH",
            sell_amount=Decimal("0.01"),
        )
        print(
            f"\n  can_execute(buy 100 USDT @ WALLET, sell 0.01 ETH @ BINANCE): "
            f"{result['can_execute']}  reason={result['reason']}"
        )


# ---------------------------------------------------------------------------
# Test 4 — Run arb checker against real CEX prices
# ---------------------------------------------------------------------------
class TestLiveArbChecker:
    """
    Wire the full ArbChecker pipeline with live Binance testnet prices.
    DEX price is simulated 0.2 % below mid to represent a typical small gap.
    """

    def test_arb_checker_live_prices(self, client):
        from integration.arb_checker import ArbChecker, SimplePricingAdapter
        from inventory.pnl import PnLEngine
        from inventory.tracker import InventoryTracker, Venue

        # Get live market mid to anchor DEX simulation
        book = client.fetch_order_book(_PAIR, limit=20)
        mid = float(book["mid_price"])
        assert mid > 0

        # DEX price 20 bps below mid (typical small on-chain discount)
        dex_price = Decimal(str(round(mid * 0.9980, 2)))

        pricing = SimplePricingAdapter(
            price=dex_price,
            price_impact_bps=Decimal("1.5"),
            fee_bps=Decimal("30"),
        )

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(Venue.BINANCE, client.fetch_balance())
        tracker.update_from_wallet(
            Venue.WALLET,
            {"ETH": Decimal("10"), "USDT": Decimal("30000")},
        )

        checker = ArbChecker(
            pricing_engine=pricing,
            exchange_client=client,
            inventory_tracker=tracker,
            pnl_engine=PnLEngine(),
        )

        result = checker.check(_PAIR, size=1.0, gas_price_gwei=20)

        # Structural assertions
        assert result["pair"] == _PAIR
        assert result["dex_price"] == dex_price
        assert result["cex_bid"] > 0
        assert result["cex_ask"] > 0
        assert "gap_bps" in result
        assert "estimated_costs_bps" in result
        assert "estimated_net_pnl_bps" in result
        assert isinstance(result["executable"], bool)

        print(
            f"\n[Arb Check] {_PAIR}"
            f"\n  DEX price:   ${float(result['dex_price']):,.2f}"
            f"\n  CEX bid:     ${float(result['cex_bid']):,.2f}"
            f"\n  CEX ask:     ${float(result['cex_ask']):,.2f}"
            f"\n  direction:   {result['direction'] or 'none'}"
            f"\n  gap:         {float(result['gap_bps']):.1f} bps"
            f"\n  costs:       {float(result['estimated_costs_bps']):.1f} bps"
            f"\n  net PnL:     {float(result['estimated_net_pnl_bps']):+.1f} bps"
            f"\n  inventory ok:{result['inventory_ok']}"
            f"\n  executable:  {result['executable']}"
        )


# ---------------------------------------------------------------------------
# Test 5 — PnL report with 5 recorded arb trades
# ---------------------------------------------------------------------------
class TestLivePnLReport:
    """
    Record 5 synthetic arb trades into PnLEngine and print a full PnL summary.
    Uses real market prices fetched from testnet for realistic notional values.
    """

    def test_pnl_report_with_5_trades(self, client):
        from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
        from inventory.tracker import Venue

        book = client.fetch_order_book(_PAIR, limit=5)
        mid = float(book["mid_price"])
        assert mid > 0

        engine = PnLEngine()

        # Five synthetic arb trades with varying outcomes
        scenarios = [
            # (dex_price_offset, cex_price_offset, qty, gas_usd, label)
            (-0.0020, 0.0000, 1.0, 3.50, "profitable: 20bps gap"),
            (-0.0015, 0.0005, 0.5, 2.00, "marginal: 20bps gap, small size"),
            (0.0010, 0.0000, 2.0, 7.00, "loss: DEX above CEX"),
            (-0.0025, 0.0010, 1.5, 4.50, "profitable: 35bps gap"),
            (-0.0005, 0.0005, 0.8, 2.50, "break-even: 10bps gap"),
        ]

        for i, (dex_offset, cex_offset, qty, gas_usd, label) in enumerate(scenarios, 1):
            dex_p = Decimal(str(round(mid * (1 + dex_offset), 2)))
            cex_p = Decimal(str(round(mid * (1 + cex_offset), 2)))
            q = Decimal(str(qty))
            fee = cex_p * q * Decimal("0.001")  # 10bps fee

            ts = datetime.now(UTC)

            buy_leg = TradeLeg(
                id=f"buy-{i}",
                timestamp=ts,
                venue=Venue.WALLET,
                symbol=_PAIR,
                side="buy",
                amount=q,
                price=dex_p,
                fee=fee,
                fee_asset=_QUOTE,
            )
            sell_leg = TradeLeg(
                id=f"sell-{i}",
                timestamp=ts,
                venue=Venue.BINANCE,
                symbol=_PAIR,
                side="sell",
                amount=q,
                price=cex_p,
                fee=fee,
                fee_asset=_QUOTE,
            )
            record = ArbRecord(
                id=f"arb-{i}",
                timestamp=ts,
                buy_leg=buy_leg,
                sell_leg=sell_leg,
                gas_cost_usd=Decimal(str(gas_usd)),
            )
            engine.record(record)
            print(
                f"\n  trade {i} [{label}]"
                f"  gross={float(record.gross_pnl):+.2f}"
                f"  fees={float(record.total_fees):.2f}"
                f"  gas={gas_usd:.2f}"
                f"  net={float(record.net_pnl):+.2f} USD"
                f"  ({float(record.net_pnl_bps):+.1f} bps)"
            )

        assert engine.total_trades == 5

        summary = engine.summary()

        assert summary["total_trades"] == 5
        assert "total_pnl_usd" in summary
        assert "win_rate" in summary
        assert 0.0 <= float(summary["win_rate"]) <= 1.0

        print(
            f"\n[PnL Summary]"
            f"\n  trades:        {summary['total_trades']}"
            f"\n  win rate:      {float(summary['win_rate']) * 100:.1f}%"
            f"\n  total PnL:     ${float(summary['total_pnl_usd']):+.4f}"
            f"\n  total fees:    ${float(summary['total_fees_usd']):.4f}"
            f"\n  avg PnL/trade: ${float(summary['avg_pnl_per_trade']):+.4f}"
            f"\n  avg PnL (bps): {float(summary['avg_pnl_bps']):+.2f}"
            f"\n  best trade:    ${float(summary['best_trade']):+.4f}"
            f"\n  worst trade:   ${float(summary['worst_trade']):+.4f}"
            f"\n  total notional:${float(summary['total_notional_usd']):,.2f}"
            f"\n  sharpe (rough):{float(summary.get('sharpe_estimate', 0)):.2f}"
        )
