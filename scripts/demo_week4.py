"""
scripts/demo_week4.py — Week 4 live arbitrage pipeline demo.

Fetches real Binance prices every tick and drives the full pipeline:
  Generator → Scorer → Priority Queue → Executor → PnL Engine → Metrics

Run:
    python scripts/demo_week4.py                          # 10 ticks, ETH+BTC
    python scripts/demo_week4.py --ticks 20              # more ticks
    python scripts/demo_week4.py --pairs ETH/USDT --size 2.0
    python scripts/demo_week4.py --ticks 0               # run until Ctrl+C
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.rule import Rule
from rich.table import Table

from executor.engine import Executor, ExecutorConfig, ExecutorState
from executor.queue import SignalQueue
from executor.recovery import CircuitBreaker, CircuitBreakerConfig
from inventory.tracker import InventoryTracker, Venue
from strategy.fees import FeeStructure
from strategy.generator import SignalGenerator
from strategy.scorer import SignalScorer

console = Console()

BINANCE_SYMBOLS = {
    "ETH/USDT": "ETHUSDT",
    "BTC/USDT": "BTCUSDT",
    "BNB/USDT": "BNBUSDT",
    "SOL/USDT": "SOLUSDT",
}


# ---------------------------------------------------------------------------
# Session state — accumulates across ticks
# ---------------------------------------------------------------------------


@dataclass
class SessionState:
    tick: int = 0
    signals_generated: int = 0
    signals_skipped: int = 0
    executions_done: int = 0
    executions_failed: int = 0
    total_pnl: float = 0.0
    replay_blocks: int = 0
    cb_trips: int = 0
    log: list[str] = field(default_factory=list)

    def add_log(self, msg: str) -> None:
        ts = time.strftime("%H:%M:%S")
        self.log.append(f"[dim]{ts}[/dim]  {msg}")
        if len(self.log) > 12:
            self.log.pop(0)


# ---------------------------------------------------------------------------
# Price fetching
# ---------------------------------------------------------------------------


def _fetch_ob(symbol: str) -> dict | None:
    url = f"https://api.binance.com/api/v3/depth?symbol={symbol}&limit=5"
    try:
        with urllib.request.urlopen(url, timeout=4) as resp:
            data = json.loads(resp.read())
        bid = float(data["bids"][0][0])
        ask = float(data["asks"][0][0])
        bid_d, ask_d = Decimal(str(bid)), Decimal(str(ask))
        mid = (bid_d + ask_d) / Decimal("2")
        return {
            "bids": [(bid_d, Decimal("10"))],
            "asks": [(ask_d, Decimal("10"))],
            "best_bid": (bid_d, Decimal("10")),
            "best_ask": (ask_d, Decimal("10")),
            "mid_price": mid,
            "spread_bps": (ask_d - bid_d) / mid * Decimal("10000"),
            "timestamp": int(time.time() * 1000),
        }
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Rich layout builders
# ---------------------------------------------------------------------------


def _header(state: SessionState, pairs: list[str]) -> Panel:
    cb_status = (
        "[red]OPEN[/red]" if state.cb_trips > state.executions_done else "[green]CLOSED[/green]"
    )
    title = (
        f"[bold cyan]Week 4 — Arbitrage Pipeline Demo[/bold cyan]   "
        f"[dim]Tick {state.tick}  ·  {time.strftime('%H:%M:%S')}  ·  "
        f"Pairs: {', '.join(pairs)}[/dim]"
    )
    stats = (
        f"Signals: [green]{state.signals_generated}[/green] generated  "
        f"[yellow]{state.signals_skipped}[/yellow] skipped   "
        f"Executions: [green]{state.executions_done}[/green] done  "
        f"[red]{state.executions_failed}[/red] failed   "
        f"PnL: [{'green' if state.total_pnl >= 0 else 'red'}]"
        f"${state.total_pnl:+.4f}[/{'green' if state.total_pnl >= 0 else 'red'}]   "
        f"Circuit: {cb_status}   "
        f"Replay blocks: [yellow]{state.replay_blocks}[/yellow]"
    )
    return Panel(f"{title}\n{stats}", border_style="cyan")


def _prices_table(obs: dict[str, dict | None]) -> Table:
    t = Table(box=box.SIMPLE_HEAVY, show_header=True, header_style="bold blue", expand=True)
    t.add_column("Pair", style="bold cyan", width=12)
    t.add_column("Bid", justify="right", width=14)
    t.add_column("Ask", justify="right", width=14)
    t.add_column("CEX Spread", justify="right", width=13)
    t.add_column("Bid-Ask Quality", width=22)

    for pair, ob in obs.items():
        if ob is None:
            t.add_row(pair, "[dim]—[/dim]", "[dim]—[/dim]", "[dim]—[/dim]", "[red]offline[/red]")
            continue
        bid = float(ob["best_bid"][0])
        ask = float(ob["best_ask"][0])
        spread = float(ob["spread_bps"])
        # liquidity quality bar based on bid-ask spread
        quality = max(0.0, 1.0 - spread / 5.0)
        bars = int(quality * 10)
        bar = f"[green]{'█' * bars}[/green][dim]{'░' * (10 - bars)}[/dim] {quality * 100:.0f}%"
        t.add_row(
            pair,
            f"[green]${bid:,.2f}[/green]",
            f"[red]${ask:,.2f}[/red]",
            f"{spread:.4f} bps",
            bar,
        )
    return t


def _scoring_table(scored: list[tuple]) -> Table:
    """scored = list of (signal, score_breakdown_dict)"""
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold", expand=True)
    t.add_column("Pair", style="cyan", width=12)
    t.add_column("Direction", width=20)
    t.add_column("Spread", justify="right", width=10)
    t.add_column("Score", justify="right", width=8)
    t.add_column("  Spread  Liq  Inv  Hist", width=26)
    t.add_column("Net PnL", justify="right", width=10)

    for sig, breakdown in scored:
        direction = (
            "[cyan]CEX→DEX[/cyan]"
            if "BUY_CEX" in sig.direction.value
            else "[magenta]DEX→CEX[/magenta]"
        )
        score_colour = "green" if sig.score >= 70 else "yellow" if sig.score >= 50 else "red"

        def mini_bar(v: float) -> str:
            b = int(v / 20)
            return "█" * b + "░" * (5 - b)

        components = (
            f"[green]{mini_bar(breakdown['spread'])}[/green] "
            f"[cyan]{mini_bar(breakdown['liquidity'])}[/cyan] "
            f"[yellow]{mini_bar(breakdown['inventory'])}[/yellow] "
            f"[dim]{mini_bar(breakdown['history'])}[/dim]"
        )
        t.add_row(
            sig.pair,
            direction,
            f"{float(sig.spread_bps):.1f} bps",
            f"[{score_colour}][bold]{sig.score:.1f}[/bold][/{score_colour}]",
            components,
            f"[green]${float(sig.expected_net_pnl):.2f}[/green]",
        )
    return t


def _execution_table(results: list[tuple]) -> Table:
    """results = list of (signal, ExecutionContext)"""
    t = Table(box=box.SIMPLE, show_header=True, header_style="bold", expand=True)
    t.add_column("Pair", style="cyan", width=12)
    t.add_column("State", width=8)
    t.add_column("Leg 1 Fill", justify="right", width=14)
    t.add_column("Leg 2 Fill", justify="right", width=14)
    t.add_column("Net PnL", justify="right", width=10)
    t.add_column("Latency", justify="right", width=9)
    t.add_column("Error", width=22)

    for sig, ctx, latency_ms in results:
        if ctx.state == ExecutorState.DONE:
            t.add_row(
                sig.pair,
                "[green]DONE[/green]",
                f"${ctx.leg1_fill_price:,.4f}",
                f"${ctx.leg2_fill_price:,.4f}",
                f"[green]${ctx.actual_net_pnl:+.4f}[/green]",
                f"{latency_ms:.0f}ms",
                "[dim]—[/dim]",
            )
        else:
            t.add_row(
                sig.pair,
                "[red]FAILED[/red]",
                "[dim]—[/dim]",
                "[dim]—[/dim]",
                "[dim]—[/dim]",
                f"{latency_ms:.0f}ms",
                f"[dim]{(ctx.error or '')[:22]}[/dim]",
            )
    return t


def _log_panel(state: SessionState) -> Panel:
    body = "\n".join(state.log) if state.log else "[dim]No events yet[/dim]"
    return Panel(body, title="[bold]Event Log[/bold]", border_style="dim", expand=True)


def _build_layout(
    state: SessionState,
    pairs: list[str],
    obs: dict,
    scored: list,
    results: list,
) -> Layout:
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="body"),
        Layout(name="log", size=14),
    )
    layout["body"].split_row(
        Layout(name="left"),
        Layout(name="right"),
    )

    layout["header"].update(_header(state, pairs))

    prices_panel = Panel(
        _prices_table(obs),
        title="[bold blue]Live Prices[/bold blue]",
        border_style="blue",
    )
    layout["left"].update(prices_panel)

    score_panel = Panel(
        _scoring_table(scored) if scored else "[dim]Waiting for signals...[/dim]",
        title="[bold yellow]Signals & Scores[/bold yellow]",
        border_style="yellow",
    )
    exec_panel = Panel(
        _execution_table(results) if results else "[dim]No executions this tick[/dim]",
        title="[bold green]Executions[/bold green]",
        border_style="green",
    )

    layout["right"].split_column(
        Layout(score_panel, name="scores"),
        Layout(exec_panel, name="execs"),
    )
    layout["log"].update(_log_panel(state))
    return layout


# ---------------------------------------------------------------------------
# One tick of the pipeline
# ---------------------------------------------------------------------------


async def _run_tick(
    pairs: list[str],
    size: float,
    tracker: InventoryTracker,
    scorer: SignalScorer,
    executor: Executor,
    queue: SignalQueue,
    state: SessionState,
    score_threshold: float,
) -> tuple[dict, list, list]:
    from unittest.mock import MagicMock

    state.tick += 1
    obs: dict[str, dict | None] = {}

    # Fetch prices
    for pair in pairs:
        symbol = BINANCE_SYMBOLS.get(pair, pair.replace("/", ""))
        obs[pair] = _fetch_ob(symbol)

    # Generate and score signals
    scored = []
    fees = FeeStructure(cex_taker_bps=10, dex_swap_bps=30, gas_cost_usd=2)

    for pair in pairs:
        ob = obs.get(pair)
        if ob is None:
            state.add_log(f"[red]✗[/red] {pair} — price fetch failed")
            continue

        exchange = MagicMock()
        exchange.fetch_order_book.return_value = ob
        gen = SignalGenerator(
            exchange_client=exchange,
            pricing_module=None,
            inventory_tracker=tracker,
            fee_structure=fees,
            config={
                "min_spread_bps": 50,
                "min_profit_usd": 1.0,
                "cooldown_seconds": 0,
                "signal_ttl_seconds": 30,
            },
        )
        sig = gen.generate(pair, size)
        if sig is None:
            state.signals_skipped += 1
            state.add_log(f"[yellow]~[/yellow] {pair} — no signal (spread below threshold)")
            continue

        state.signals_generated += 1
        breakdown = {
            "spread": scorer._score_spread(sig.spread_bps),
            "liquidity": scorer._score_liquidity(sig.bid_ask_spread_bps),
            "inventory": scorer._score_inventory(sig, []),
            "history": scorer._score_history(sig.pair),
        }
        scorer.score(sig, [])

        if sig.score < score_threshold:
            state.signals_skipped += 1
            state.add_log(
                f"[yellow]~[/yellow] {pair} — score {sig.score:.1f} below threshold {score_threshold:.0f}"
            )
            continue

        queue.put(sig)
        scored.append((sig, breakdown))
        state.add_log(
            f"[cyan]↑[/cyan] {pair} — score [bold]{sig.score:.1f}[/bold]  "
            f"spread {float(sig.spread_bps):.1f}bps  net ${float(sig.expected_net_pnl):.2f}"
        )

    # Execute top signal from queue
    results = []
    cb = executor.circuit_breaker

    if cb.is_open():
        state.add_log(
            f"[red]⊘[/red] Circuit breaker OPEN — {cb.time_until_reset():.0f}s until reset"
        )
    else:
        sig = queue.get()
        if sig:
            t0 = time.monotonic()
            ctx = await executor.execute(sig)
            latency_ms = (time.monotonic() - t0) * 1000
            results.append((sig, ctx, latency_ms))

            if ctx.state == ExecutorState.DONE:
                state.executions_done += 1
                state.total_pnl += ctx.actual_net_pnl or 0.0
                scorer.record_result(sig.pair, True)
                state.add_log(
                    f"[green]✓[/green] {sig.pair} DONE — "
                    f"leg1=${ctx.leg1_fill_price:,.4f} "
                    f"leg2=${ctx.leg2_fill_price:,.4f} "
                    f"pnl=[bold green]${ctx.actual_net_pnl:+.4f}[/bold green]"
                )
            elif ctx.error and "Duplicate" in ctx.error:
                state.replay_blocks += 1
                state.add_log(f"[yellow]⟳[/yellow] {sig.pair} — replay blocked")
            else:
                state.executions_failed += 1
                scorer.record_result(sig.pair, False)
                state.add_log(f"[red]✗[/red] {sig.pair} FAILED — {ctx.error}")

    return obs, scored, results


# ---------------------------------------------------------------------------
# Main demo loop
# ---------------------------------------------------------------------------


async def run_demo(pairs: list[str], size: float, ticks: int, interval: float) -> None:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    assets = {"USDT": "500000"}
    for pair in pairs:
        assets[pair.split("/")[0]] = "100"
    tracker.update_from_cex(
        Venue.BINANCE, {k: {"free": v, "locked": "0"} for k, v in assets.items()}
    )
    tracker.update_from_wallet(Venue.WALLET, assets)

    scorer = SignalScorer()
    executor = Executor(
        exchange_client=None,
        pricing_module=None,
        inventory_tracker=None,
        config=ExecutorConfig(simulation_mode=True, use_flashbots=False),
    )
    executor.circuit_breaker = CircuitBreaker(CircuitBreakerConfig(failure_threshold=5))
    queue = SignalQueue(maxsize=50)
    state = SessionState()

    # Initial empty state
    obs: dict = {p: None for p in pairs}
    scored: list = []
    results: list = []

    state.add_log("[dim]Starting demo — fetching live Binance prices...[/dim]")

    with Live(
        _build_layout(state, pairs, obs, scored, results),
        console=console,
        refresh_per_second=4,
        screen=True,
    ) as live:
        tick_count = 0
        while ticks == 0 or tick_count < ticks:
            obs, scored, results = await _run_tick(
                pairs, size, tracker, scorer, executor, queue, state, score_threshold=0.0
            )
            live.update(_build_layout(state, pairs, obs, scored, results))
            tick_count += 1

            if ticks > 0 and tick_count >= ticks:
                state.add_log(f"[bold green]Demo complete — {ticks} ticks finished.[/bold green]")
                live.update(_build_layout(state, pairs, obs, scored, results))
                await asyncio.sleep(3)
                break

            await asyncio.sleep(interval)

    # Final summary outside live view
    console.print()
    console.print(Rule("[bold]Final Summary[/bold]", style="green"))
    console.print()

    t = Table(box=box.SIMPLE, show_header=False)
    t.add_column("", style="dim", width=24)
    t.add_column("", style="bold", width=16)
    pnl_col = "green" if state.total_pnl >= 0 else "red"
    t.add_row("Ticks run", str(state.tick))
    t.add_row("Signals generated", str(state.signals_generated))
    t.add_row("Signals skipped", str(state.signals_skipped))
    t.add_row(
        "Executions",
        f"[green]{state.executions_done}[/green] done  [red]{state.executions_failed}[/red] failed",
    )
    win_rate = (
        state.executions_done / max(state.executions_done + state.executions_failed, 1)
    ) * 100
    t.add_row("Win rate", f"{win_rate:.0f}%")
    t.add_row("Total PnL", f"[{pnl_col}]${state.total_pnl:+.4f}[/{pnl_col}]")
    t.add_row("Replay blocks", str(state.replay_blocks))
    t.add_row("Circuit breaker trips", str(state.cb_trips))
    console.print(t)
    console.print()
    console.print(
        Panel.fit(
            "[bold green]Week 4 pipeline demonstrated on live market data[/bold green]\n"
            "[dim]Generator → Scorer → Priority Queue → Executor → PnL Tracking → Metrics[/dim]",
            border_style="green",
        )
    )
    console.print()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Week 4 live pipeline demo")
    parser.add_argument("--pairs", nargs="+", default=["ETH/USDT", "BTC/USDT"])
    parser.add_argument("--size", type=float, default=1.0)
    parser.add_argument("--ticks", type=int, default=10, help="0 = run until Ctrl+C")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between ticks")
    args = parser.parse_args()

    try:
        asyncio.run(run_demo(args.pairs, args.size, args.ticks, args.interval))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/yellow]")
