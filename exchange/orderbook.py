"""
exchange/orderbook.py — Order book depth analysis for trading decisions.

Provides OrderBookAnalyzer which wraps the dict returned by
ExchangeClient.fetch_order_book() and exposes:

  walk_the_book   — simulate market impact at a given order size
  depth_at_bps    — available liquidity within a basis-point band
  imbalance       — bid/ask pressure indicator  [-1.0, +1.0]
  effective_spread — true cost of immediacy for a round-trip trade

CLI (live data from Binance testnet):
    python -m exchange.orderbook ETH/USDT
    python -m exchange.orderbook ETH/USDT --depth 20 --qty 5
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import UTC, datetime
from decimal import Decimal


class OrderBookAnalyzer:
    """
    Analyze order book snapshots for trading decisions.
    """

    def __init__(self, orderbook: dict) -> None:
        self._bids: list[tuple[Decimal, Decimal]] = orderbook["bids"]
        self._asks: list[tuple[Decimal, Decimal]] = orderbook["asks"]
        self._symbol: str = orderbook.get("symbol", "")
        self._timestamp: int = orderbook.get("timestamp") or 0
        self._mid: Decimal = orderbook.get("mid_price", Decimal("0"))
        best_bid = orderbook.get("best_bid") or (Decimal("0"), Decimal("0"))
        best_ask = orderbook.get("best_ask") or (Decimal("0"), Decimal("0"))
        self._best_bid: tuple[Decimal, Decimal] = best_bid
        self._best_ask: tuple[Decimal, Decimal] = best_ask

    def walk_the_book(self, side: str, qty: float) -> dict:
        """
        Simulate filling ``qty`` against the live order book.
        """
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        qty_d = Decimal(str(qty))
        if qty_d <= 0:
            raise ValueError(f"qty must be positive, got {qty}")

        levels = self._asks if side == "buy" else self._bids
        best_price = levels[0][0] if levels else Decimal("0")

        remaining = qty_d
        fills: list[dict] = []
        total_cost = Decimal("0")

        for price, avail_qty in levels:
            if remaining <= 0:
                break
            take = min(remaining, avail_qty)
            cost = price * take
            fills.append({"price": price, "qty": take, "cost": cost})
            total_cost += cost
            remaining -= take

        filled_qty = qty_d - remaining
        avg_price = total_cost / filled_qty if filled_qty > 0 else Decimal("0")
        fully_filled = remaining <= 0

        if best_price > 0 and filled_qty > 0:
            if side == "buy":
                slippage_bps = (avg_price - best_price) / best_price * Decimal("10000")
            else:
                slippage_bps = (best_price - avg_price) / best_price * Decimal("10000")
            slippage_bps = max(slippage_bps, Decimal("0"))
        else:
            slippage_bps = Decimal("0")

        return {
            "avg_price": avg_price,
            "total_cost": total_cost,
            "slippage_bps": slippage_bps,
            "levels_consumed": len(fills),
            "fully_filled": fully_filled,
            "fills": fills,
        }

    def depth_at_bps(self, side: str, bps: float) -> Decimal:
        """
        Total quantity available within ``bps`` basis points of best price.
        """
        if side not in ("bid", "ask"):
            raise ValueError(f"side must be 'bid' or 'ask', got {side!r}")

        bps_d = Decimal(str(bps))
        factor = bps_d / Decimal("10000")

        if side == "bid":
            if not self._bids:
                return Decimal("0")
            best = self._bids[0][0]
            threshold = best * (1 - factor)
            return sum((q for p, q in self._bids if p >= threshold), Decimal("0"))
        else:
            if not self._asks:
                return Decimal("0")
            best = self._asks[0][0]
            threshold = best * (1 + factor)
            return sum((q for p, q in self._asks if p <= threshold), Decimal("0"))

    def imbalance(self, levels: int = 10) -> float:
        """
        Order book imbalance ratio in ``[-1.0, +1.0]``.
        """
        bid_qty = float(sum(q for _, q in self._bids[:levels]))
        ask_qty = float(sum(q for _, q in self._asks[:levels]))
        total = bid_qty + ask_qty
        if total == 0.0:
            return 0.0
        return (bid_qty - ask_qty) / total

    def effective_spread(self, qty: float) -> Decimal:
        """
        Effective spread for a round-trip of size ``qty`` (in basis points).
        """
        buy_result = self.walk_the_book("buy", qty)
        sell_result = self.walk_the_book("sell", qty)

        avg_ask = buy_result["avg_price"]
        avg_bid = sell_result["avg_price"]

        if self._mid == 0 or avg_ask == 0 or avg_bid == 0:
            return Decimal("0")

        return (avg_ask - avg_bid) / self._mid * Decimal("10000")

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timestamp(self) -> int:
        return self._timestamp

    @property
    def mid_price(self) -> Decimal:
        return self._mid

    @property
    def best_bid(self) -> tuple[Decimal, Decimal]:
        return self._best_bid

    @property
    def best_ask(self) -> tuple[Decimal, Decimal]:
        return self._best_ask

    @property
    def quoted_spread_bps(self) -> Decimal:
        """Best-bid / best-ask spread in basis points."""
        bid_p, ask_p = self._best_bid[0], self._best_ask[0]
        if self._mid == 0 or bid_p == 0 or ask_p == 0:
            return Decimal("0")
        return (ask_p - bid_p) / self._mid * Decimal("10000")


def _fmt_bps(bps: Decimal) -> str:
    return f"{bps:.2f} bps"


def _fmt_price(price: Decimal) -> str:
    return f"${price:,.2f}"


def _fmt_qty(qty: Decimal, symbol: str = "") -> str:
    base = symbol.split("/")[0] if "/" in symbol else ""
    suffix = f" {base}" if base else ""
    return f"{qty:.4f}{suffix}"


def _box_line(content: str, width: int = 54) -> str:
    return f"║  {content:<{width - 4}}║"


def _print_analysis(analyzer: OrderBookAnalyzer, qty_small: float, qty_large: float) -> None:
    W = 56

    ts = datetime.fromtimestamp(analyzer.timestamp / 1000, tz=UTC)
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S UTC") if analyzer.timestamp else "N/A"

    bid_p, bid_q = analyzer.best_bid
    ask_p, ask_q = analyzer.best_ask
    spread_abs = ask_p - bid_p
    imbal = analyzer.imbalance()
    imbal_label = (
        "buy pressure" if imbal > 0.05 else "sell pressure" if imbal < -0.05 else "balanced"
    )
    imbal_sign = "+" if imbal >= 0 else ""

    depth_bid = analyzer.depth_at_bps("bid", 10)
    depth_ask = analyzer.depth_at_bps("ask", 10)
    bid_value = depth_bid * bid_p
    ask_value = depth_ask * ask_p

    sym = analyzer.symbol

    def _row(text: str) -> str:
        inner = W - 4
        return f"║  {text:<{inner}}║"

    sep = "╠" + "═" * (W - 2) + "╣"
    top = "╔" + "═" * (W - 2) + "╗"
    bot = "╚" + "═" * (W - 2) + "╝"

    lines = [
        top,
        _row(f"{sym} Order Book Analysis"),
        _row(f"Timestamp: {ts_str}"),
        sep,
        _row(f"Best Bid:    {_fmt_price(bid_p)} × {_fmt_qty(bid_q, sym)}"),
        _row(f"Best Ask:    {_fmt_price(ask_p)} × {_fmt_qty(ask_q, sym)}"),
        _row(f"Mid Price:   {_fmt_price(analyzer.mid_price)}"),
        _row(f"Spread:      {_fmt_price(spread_abs)} ({_fmt_bps(analyzer.quoted_spread_bps)})"),
        sep,
        _row("Depth (within 10 bps):"),
        _row(f"  Bids: {_fmt_qty(depth_bid, sym)} ({_fmt_price(bid_value)})"),
        _row(f"  Asks: {_fmt_qty(depth_ask, sym)} ({_fmt_price(ask_value)})"),
        _row(f"Imbalance: {imbal_sign}{imbal:.2f} ({imbal_label})"),
    ]

    for label, qty in [(f"{qty_small}", qty_small), (f"{qty_large}", qty_large)]:
        base = sym.split("/")[0] if "/" in sym else ""
        lines.append(sep)
        lines.append(_row(f"Walk-the-book ({label} {base} buy):"))
        result = analyzer.walk_the_book("buy", qty)
        if result["fully_filled"]:
            lines.append(_row(f"  Avg price:  {_fmt_price(result['avg_price'])}"))
            lines.append(_row(f"  Slippage:   {_fmt_bps(result['slippage_bps'])}"))
            lines.append(_row(f"  Levels:     {result['levels_consumed']}"))
        else:
            lines.append(_row("  INSUFFICIENT LIQUIDITY (partial fill)"))
            lines.append(_row(f"  Filled: {_fmt_qty(sum(f['qty'] for f in result['fills']), sym)}"))

    eff = analyzer.effective_spread(qty_small)
    lines += [
        sep,
        _row(
            f"Effective spread ({qty_small} {sym.split('/')[0] if '/' in sym else ''} round-trip): {_fmt_bps(eff)}"
        ),
        bot,
    ]

    print("\n".join(lines))


def _run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Order book analysis — connects to Binance testnet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Examples:\n  python -m exchange.orderbook ETH/USDT\n  python -m exchange.orderbook BTC/USDT --depth 20 --qty 5",
    )
    parser.add_argument("symbol", help="Trading pair, e.g. ETH/USDT")
    parser.add_argument("--depth", type=int, default=20, help="Order book depth (default: 20)")
    parser.add_argument(
        "--qty", type=float, default=2.0, help="Small walk-the-book qty (default: 2)"
    )
    parser.add_argument(
        "--qty-large", type=float, default=10.0, help="Large walk-the-book qty (default: 10)"
    )
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

        config = {
            "apiKey": api_key,
            "secret": api_secret,
            "sandbox": True,
            "enableRateLimit": True,
        }
        client = ExchangeClient(config)
        raw_book = client.fetch_order_book(args.symbol, limit=args.depth)
    except Exception as exc:
        print(f"Error fetching order book: {exc}", file=sys.stderr)
        return 1

    analyzer = OrderBookAnalyzer(raw_book)
    _print_analysis(analyzer, qty_small=args.qty, qty_large=args.qty_large)
    return 0


if __name__ == "__main__":
    sys.exit(_run_cli())
