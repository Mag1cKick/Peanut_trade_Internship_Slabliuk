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

_ETH_PAIR = "ETH/USDT"
_BTC_PAIR = "BTC/USDT"
_QUOTE = "USDT"


def _ascii(s: str) -> str:
    """Return a safe ASCII representation — testnet has assets with non-ASCII names."""
    return s.encode("ascii", "replace").decode("ascii")


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
    """Fetch live snapshots for ETH/USDT and BTC/USDT and validate the analysis."""

    @pytest.mark.parametrize("pair", [_ETH_PAIR, _BTC_PAIR])
    def test_fetch_order_book_and_analysis(self, client, pair):
        from exchange.orderbook import OrderBookAnalyzer

        base = pair.split("/")[0]
        book = client.fetch_order_book(pair, limit=20)

        # --- structural checks ---
        assert book["symbol"] == pair
        assert len(book["bids"]) > 0, "Expected bids in live order book"
        assert len(book["asks"]) > 0, "Expected asks in live order book"
        assert book["mid_price"] > 0, "Mid price should be positive"
        assert book["spread_bps"] >= 0, "Spread should be non-negative"

        analyzer = OrderBookAnalyzer(book)

        best_bid_price, best_bid_qty = analyzer.best_bid
        best_ask_price, best_ask_qty = analyzer.best_ask

        assert best_bid_price > 0
        assert best_ask_price > best_bid_price, "Ask must be above bid"

        # --- depth (returns Decimal of qty within band) ---
        bid_depth = analyzer.depth_at_bps("bid", 10)
        ask_depth = analyzer.depth_at_bps("ask", 10)
        assert bid_depth >= 0
        assert ask_depth >= 0

        # --- imbalance in [-1, +1] ---
        imbalance = analyzer.imbalance()
        assert -1.0 <= float(imbalance) <= 1.0

        # --- walk the book ---
        walk_qty = 0.5 if pair == _ETH_PAIR else 0.01
        walk = analyzer.walk_the_book("buy", walk_qty)
        assert walk["avg_price"] > 0
        assert walk["slippage_bps"] >= 0
        assert walk["levels_consumed"] >= 0

        print(
            f"\n[Order Book] {pair}"
            f"\n  best bid:  ${float(best_bid_price):,.2f} x {float(best_bid_qty):.4f}"
            f"\n  best ask:  ${float(best_ask_price):,.2f} x {float(best_ask_qty):.4f}"
            f"\n  mid price: ${float(analyzer.mid_price):,.2f}"
            f"\n  spread:    {float(book['spread_bps']):.2f} bps"
            f"\n  imbalance: {float(imbalance):+.3f}  "
            f"({'buy' if imbalance > 0 else 'sell'} pressure)"
            f"\n  depth +/-10bps: bids={float(bid_depth):.4f} {base}"
            f"  asks={float(ask_depth):.4f} {base}"
            f"\n  walk buy {walk_qty} {base}:"
            f" avg=${float(walk['avg_price']):,.2f}"
            f"  slippage={float(walk['slippage_bps']):.2f} bps"
            f"  levels={walk['levels_consumed']}"
            f"  fully_filled={walk['fully_filled']}"
        )


# ---------------------------------------------------------------------------
# Test 2 — Place and cancel a LIMIT IOC order on testnet
# ---------------------------------------------------------------------------
class TestLiveOrderPlacement:
    """
    Place a LIMIT IOC buy order at 10 % below market — guaranteed not to fill.
    IOC semantics: if not immediately filled the exchange cancels the order.
    Also demonstrates explicit cancel_order on a resting LIMIT order.
    """

    def test_place_limit_ioc_order_self_cancels(self, client):
        """IOC at 10% below market must self-cancel (status expired)."""
        book = client.fetch_order_book(_ETH_PAIR, limit=5)
        best_ask = float(book["best_ask"][0])
        assert best_ask > 0, "Need a live ask price"

        limit_price = round(best_ask * 0.90, 2)
        qty = 0.01

        print(
            f"\n[IOC Order] Placing LIMIT IOC buy {qty} ETH @ ${limit_price:,.2f}"
            f" (market ask: ${best_ask:,.2f})"
        )

        order = client.create_limit_ioc_order(_ETH_PAIR, "buy", amount=qty, price=limit_price)

        assert order["id"], "Order should have an id"
        assert order["symbol"] == _ETH_PAIR
        assert order["side"] == "buy"
        assert order["type"] == "limit"

        # Handle exchange processing lag
        if order["status"] == "open":
            time.sleep(2)
            order = client.fetch_order_status(order["id"], _ETH_PAIR)

        # IOC at 10% below market must not fill
        assert (
            float(order["amount_filled"]) == 0.0
        ), f"IOC order 10% below market should not fill, got {order['amount_filled']}"
        assert order["status"] in (
            "expired",
            "canceled",
            "cancelled",
        ), f"Expected IOC to self-cancel, got status={order['status']}"

        print(
            f"  order id:  {order['id']}"
            f"\n  TIF:       {order['time_in_force']}"
            f"\n  status:    {order['status']}"
            f"\n  filled:    {float(order['amount_filled']):.6f} ETH"
            f"\n  -> IOC self-cancelled as expected"
        )

    def test_place_and_explicitly_cancel_limit_order(self, client):
        """Place a resting LIMIT buy well below market, then cancel it manually."""
        book = client.fetch_order_book(_BTC_PAIR, limit=5)
        best_ask = float(book["best_ask"][0])
        assert best_ask > 0

        # Place at 20% below market — well outside any realistic fill
        limit_price = round(best_ask * 0.80, 0)
        qty = 0.001  # 0.001 BTC

        print(
            f"\n[LIMIT Order] Placing LIMIT buy {qty} BTC @ ${limit_price:,.0f}"
            f" (market ask: ${best_ask:,.2f})"
        )

        # Use create_limit_ioc_order as regular LIMIT by checking our client supports it
        # For a resting LIMIT we use the underlying ccxt directly via the client fixture

        raw = client._exchange.create_order(
            _BTC_PAIR,
            "limit",
            "buy",
            qty,
            limit_price,
            {"timeInForce": "GTC"},
        )
        order_id = str(raw["id"])
        assert order_id, "Order must have an id"
        print(f"  placed order id: {order_id}  status: {raw.get('status')}")

        # Explicitly cancel
        cancelled = client.cancel_order(order_id, _BTC_PAIR)
        assert cancelled["id"] == order_id
        assert cancelled["status"] in (
            "canceled",
            "cancelled",
            "expired",
        ), f"Expected cancelled, got {cancelled['status']}"

        print(f"  cancelled: id={cancelled['id']}  status={cancelled['status']}")


# ---------------------------------------------------------------------------
# Test 3 — Portfolio snapshot across Binance testnet + wallet
# ---------------------------------------------------------------------------
class TestLivePortfolioSnapshot:
    """Fetch live CEX balances and build a multi-venue InventoryTracker snapshot."""

    def test_portfolio_snapshot(self, client):
        from inventory.tracker import InventoryTracker, Venue

        cex_balances = client.fetch_balance()

        assert isinstance(cex_balances, dict)
        # Filter to printable ASCII asset names (testnet has coins with CJK names)
        printable_assets = [_ascii(k) for k in cex_balances.keys()]
        print(
            f"\n[Portfolio] Binance testnet assets ({len(cex_balances)}): {printable_assets[:10]}..."
        )

        # Simulate on-chain wallet alongside CEX
        wallet_balances: dict = {
            "ETH": Decimal("5.0"),
            "BTC": Decimal("0.1"),
            "USDT": Decimal("10000.0"),
        }

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
        tracker.update_from_cex(Venue.BINANCE, cex_balances)
        tracker.update_from_wallet(Venue.WALLET, wallet_balances)

        snapshot = tracker.snapshot()

        assert "venues" in snapshot
        assert "totals" in snapshot
        assert "timestamp" in snapshot

        print(f"  Snapshot at: {snapshot['timestamp']}")
        print(f"  {'Asset':<8}  {'Binance':>14}  {'Wallet':>14}  {'Total':>14}")
        print(f"  {'-'*8}  {'-'*14}  {'-'*14}  {'-'*14}")
        for asset, total in snapshot["totals"].items():
            binance_free = (
                snapshot["venues"].get("binance", {}).get(asset, {}).get("free", Decimal("0"))
            )
            wallet_free = (
                snapshot["venues"].get("wallet", {}).get(asset, {}).get("free", Decimal("0"))
            )
            safe_asset = _ascii(asset)
            print(
                f"  {safe_asset:<8}  {float(binance_free):>14,.4f}"
                f"  {float(wallet_free):>14,.4f}"
                f"  {float(total):>14,.4f}"
            )

        # ETH, BTC, USDT must appear (wallet always has them)
        for asset in ("ETH", "BTC", "USDT"):
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
            f"\n  can_execute(buy 100 USDT @ WALLET, sell 0.01 ETH @ BINANCE):"
            f" {result['can_execute']}  reason={result['reason']}"
        )
        assert isinstance(result["can_execute"], bool)


# ---------------------------------------------------------------------------
# Test 4 — Run arb checker against real CEX prices
# ---------------------------------------------------------------------------
class TestLiveArbChecker:
    """
    Wire the full ArbChecker pipeline with live Binance testnet prices.
    Covers both ETH/USDT and BTC/USDT.
    DEX price is simulated 20 bps below mid to represent a typical on-chain gap.
    """

    @pytest.mark.parametrize("pair", [_ETH_PAIR, _BTC_PAIR])
    def test_arb_checker_live_prices(self, client, pair):
        from integration.arb_checker import ArbChecker, SimplePricingAdapter
        from inventory.pnl import PnLEngine
        from inventory.tracker import InventoryTracker, Venue

        book = client.fetch_order_book(pair, limit=20)
        mid = float(book["mid_price"])
        assert mid > 0

        base = pair.split("/")[0]

        # DEX price 20 bps below mid
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
            {base: Decimal("10"), _QUOTE: Decimal("50000")},
        )

        checker = ArbChecker(
            pricing_engine=pricing,
            exchange_client=client,
            inventory_tracker=tracker,
            pnl_engine=PnLEngine(),
        )

        size = 1.0 if pair == _ETH_PAIR else 0.01
        result = checker.check(pair, size=size, gas_price_gwei=20)

        # Structural assertions
        assert result["pair"] == pair
        assert result["dex_price"] == dex_price
        assert result["cex_bid"] > 0
        assert result["cex_ask"] > 0
        assert "gap_bps" in result
        assert "estimated_costs_bps" in result
        assert "estimated_net_pnl_bps" in result
        assert isinstance(result["executable"], bool)

        print(
            f"\n[Arb Check] {pair} (size={size} {base})"
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
# Test 5 — PnL report with 5+ recorded arb trades
# ---------------------------------------------------------------------------
class TestLivePnLReport:
    """
    Record 5 synthetic arb trades using real live market prices, then print
    a full PnL summary. Covers both ETH/USDT and BTC/USDT notional.
    """

    def test_pnl_report_with_5_trades(self, client):
        from inventory.pnl import ArbRecord, PnLEngine, TradeLeg
        from inventory.tracker import Venue

        # Get live prices for realistic notional
        eth_book = client.fetch_order_book(_ETH_PAIR, limit=5)
        btc_book = client.fetch_order_book(_BTC_PAIR, limit=5)
        eth_mid = float(eth_book["mid_price"])
        btc_mid = float(btc_book["mid_price"])
        assert eth_mid > 0 and btc_mid > 0

        engine = PnLEngine()

        # Five trades: mix of ETH/USDT and BTC/USDT, mix of win/loss
        scenarios = [
            # (pair, mid, dex_off, cex_off, qty,  gas,  label)
            (_ETH_PAIR, eth_mid, -0.0020, 0.0000, 1.0, 3.50, "ETH profitable 20bps"),
            (_ETH_PAIR, eth_mid, -0.0015, 0.0005, 0.5, 2.00, "ETH marginal 20bps"),
            (_BTC_PAIR, btc_mid, 0.0010, 0.0000, 0.01, 7.00, "BTC loss: DEX above CEX"),
            (_BTC_PAIR, btc_mid, -0.0025, 0.0010, 0.02, 4.50, "BTC profitable 35bps"),
            (_ETH_PAIR, eth_mid, -0.0005, 0.0005, 0.8, 2.50, "ETH break-even 10bps"),
        ]

        print("\n[PnL] Recording 5 arb trades:")
        for i, (pair, mid, dex_off, cex_off, qty, gas_usd, label) in enumerate(scenarios, 1):
            dex_p = Decimal(str(round(mid * (1 + dex_off), 2)))
            cex_p = Decimal(str(round(mid * (1 + cex_off), 2)))
            q = Decimal(str(qty))
            fee = cex_p * q * Decimal("0.001")  # 10 bps taker fee

            ts = datetime.now(UTC)
            buy_leg = TradeLeg(
                id=f"buy-{i}",
                timestamp=ts,
                venue=Venue.WALLET,
                symbol=pair,
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
                symbol=pair,
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
                f"  [{i}] {label:<30}"
                f"  gross={float(record.gross_pnl):+7.2f}"
                f"  fees={float(record.total_fees):6.2f}"
                f"  gas={gas_usd:.2f}"
                f"  net={float(record.net_pnl):+7.2f} USD"
                f"  ({float(record.net_pnl_bps):+.1f} bps)"
            )

        assert len(engine.trades) == 5

        summary = engine.summary()

        assert summary["total_trades"] == 5
        assert "total_pnl_usd" in summary
        assert "win_rate" in summary
        assert 0.0 <= float(summary["win_rate"]) <= 100.0

        wins = sum(1 for r in engine.trades if r.net_pnl > 0)
        expected_win_rate = wins / 5 * 100
        assert abs(float(summary["win_rate"]) - expected_win_rate) < 0.01

        print(
            f"\n[PnL Summary]"
            f"\n  trades:          {summary['total_trades']}"
            f"\n  win rate:        {float(summary['win_rate']):.1f}%"
            f"\n  total PnL:       ${float(summary['total_pnl_usd']):+.4f}"
            f"\n  total fees:      ${float(summary['total_fees_usd']):.4f}"
            f"\n  avg PnL/trade:   ${float(summary['avg_pnl_per_trade']):+.4f}"
            f"\n  avg PnL (bps):   {float(summary['avg_pnl_bps']):+.2f}"
            f"\n  best trade:      ${float(summary['best_trade_pnl']):+.4f}"
            f"\n  worst trade:     ${float(summary['worst_trade_pnl']):+.4f}"
            f"\n  total notional:  ${float(summary['total_notional']):,.2f}"
            f"\n  sharpe (rough):  {float(summary.get('sharpe_estimate', 0)):.2f}"
        )
