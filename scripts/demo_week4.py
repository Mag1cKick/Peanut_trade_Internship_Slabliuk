"""
scripts/demo_week4.py — Week 4 live arbitrage pipeline demo.

Two modes:
  Stub DEX  (default)  — CEX prices real, DEX prices = mid × 1.008 stub
  Real DEX  (--rpc-url) — CEX prices real, DEX prices from live Uniswap V2 pool

Run:
    python scripts/demo_week4.py                                # stub DEX
    python scripts/demo_week4.py --rpc-url https://eth.llamarpc.com   # real DEX
    python scripts/demo_week4.py --ticks 20 --interval 2
    python scripts/demo_week4.py --ticks 0                      # run until Ctrl+C

Free public RPC endpoints (no API key needed):
    https://eth.llamarpc.com
    https://ethereum.publicnode.com
    https://cloudflare-eth.com
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
# Lightweight Uniswap V2 pricer — queries pool reserves via raw JSON-RPC.
# Implements the same get_token() / get_quote() interface the generator uses
# so it can be dropped in as pricing_module without the full PricingEngine.
# ---------------------------------------------------------------------------


@dataclass
class _Token:
    symbol: str
    decimals: int


@dataclass
class _Quote:
    expected_output: int


class SimpleUniswapPricer:
    """
    Minimal Uniswap V2 pricing adapter backed by direct eth_call RPC calls.

    Supported pairs (direct pools, mainnet):
      ETH/USDT  — WETH/USDT pool 0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852
      ETH/USDC  — WETH/USDC pool 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc

    Other pairs fall back to stub pricing automatically.
    """

    _TOKENS: dict[str, _Token] = {
        "ETH": _Token("ETH", 18),
        "BTC": _Token("BTC", 8),
        "USDT": _Token("USDT", 6),
        "USDC": _Token("USDC", 6),
        "BNB": _Token("BNB", 18),
    }

    # (token0_symbol, token1_symbol) → pool address
    # token0 is always the lower address in Uniswap V2
    _POOLS: dict[tuple[str, str], str] = {
        # WETH(0xC02...) / USDT(0xdAC...) — WETH < USDT → WETH=token0
        ("ETH", "USDT"): "0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
        # USDC(0xA0b...) / WETH(0xC02...) — USDC < WETH → USDC=token0
        ("ETH", "USDC"): "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
    }

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url

    def get_token(self, symbol: str) -> _Token:
        if symbol not in self._TOKENS:
            raise ValueError(f"Token {symbol} not supported in DEX mode")
        return self._TOKENS[symbol]

    def get_quote(
        self, token_in: _Token, token_out: _Token, amount_in: int, gas_price: int = 1
    ) -> _Quote:
        base = "ETH" if token_in.symbol == "ETH" or token_out.symbol == "ETH" else None
        quote_sym = token_out.symbol if token_in.symbol == "ETH" else token_in.symbol
        pool_addr = self._POOLS.get(("ETH", quote_sym)) if base else None

        if pool_addr is None:
            raise ValueError(f"No Uniswap V2 pool for {token_in.symbol}/{token_out.symbol}")

        r0, r1 = self._get_reserves(pool_addr)

        # Pool token ordering depends on the pair
        # ETH/USDT pool: token0=WETH(r0), token1=USDT(r1)
        # ETH/USDC pool: token0=USDC(r0), token1=WETH(r1)
        if quote_sym == "USDT":
            # WETH=token0=r0, USDT=token1=r1
            if token_in.symbol == "ETH":
                reserve_in, reserve_out = r0, r1
            else:
                reserve_in, reserve_out = r1, r0
        else:
            # USDC=token0=r0, WETH=token1=r1
            if token_in.symbol == "ETH":
                reserve_in, reserve_out = r1, r0
            else:
                reserve_in, reserve_out = r0, r1

        amount_out = (reserve_out * amount_in * 997) // (reserve_in * 1000 + amount_in * 997)
        return _Quote(expected_output=amount_out)

    def get_prices_for_pair(self, pair: str, size: float) -> tuple[float, float]:
        """
        Return (dex_buy, dex_sell) in quote-currency per base-currency.

        dex_sell = effective price when selling `size` base on Uniswap
        dex_buy  = effective cost  when buying  `size` base on Uniswap
        Both in USDT (or quote token) per 1 base token.
        """
        base, quote = pair.split("/")
        pool_addr = self._POOLS.get((base, quote))
        if pool_addr is None:
            raise ValueError(f"No Uniswap V2 pool for {pair}")

        r0, r1 = self._get_reserves(pool_addr)
        base_dec = self._TOKENS[base].decimals
        quote_dec = self._TOKENS[quote].decimals

        # WETH/USDT pool: token0=WETH(r0), token1=USDT(r1)
        # USDC/WETH pool: token0=USDC(r0), token1=WETH(r1)  ← reversed!
        if (base, quote) == ("ETH", "USDC"):
            reserve_base, reserve_quote = r1, r0
        else:
            reserve_base, reserve_quote = r0, r1

        size_raw = int(size * 10**base_dec)

        # Sell base for quote (e.g. sell ETH → receive USDT)
        out_raw = (reserve_quote * size_raw * 997) // (reserve_base * 1000 + size_raw * 997)
        dex_sell = out_raw / (10**quote_dec * size)

        # Buy base with quote (AMM getAmountIn: how much USDT to pay for size ETH)
        num = reserve_quote * size_raw * 1000
        den = (reserve_base - size_raw) * 997
        if den <= 0:
            dex_buy = dex_sell * 1.003
        else:
            in_raw = num // den + 1
            dex_buy = in_raw / (10**quote_dec * size)

        return dex_buy, dex_sell

    def _get_reserves(self, pool_addr: str) -> tuple[int, int]:
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": pool_addr, "data": "0x0902f1ac"}, "latest"],
            "id": 1,
        }
        req = urllib.request.Request(
            self.rpc_url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PeanutTrade-Demo/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        if "error" in result or not result.get("result"):
            raise ValueError(f"RPC error: {result.get('error')}")
        data = result["result"][2:]
        return int(data[0:64], 16), int(data[64:128], 16)


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
    dex_mode: str = "Stub (mid×1.008)"
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
    dex_colour = "green" if "Uniswap" in state.dex_mode else "yellow"
    title = (
        f"[bold cyan]Week 4 — Arbitrage Pipeline Demo[/bold cyan]   "
        f"[dim]Tick {state.tick}  ·  {time.strftime('%H:%M:%S')}  ·  "
        f"Pairs: {', '.join(pairs)}[/dim]   "
        f"DEX: [{dex_colour}]{state.dex_mode}[/{dex_colour}]"
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
    pricer: SimpleUniswapPricer | None = None,
) -> tuple[dict, list, list]:
    from unittest.mock import MagicMock

    state.tick += 1
    obs: dict[str, dict | None] = {}

    # Fetch CEX prices
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

        base, quote = pair.split("/")
        use_real_dex = pricer is not None and (base, quote) in SimpleUniswapPricer._POOLS

        if pricer is not None and not use_real_dex:
            state.add_log(f"[dim]ℹ {pair} — no Uniswap pool, using stub DEX[/dim]")

        gen = SignalGenerator(
            exchange_client=exchange,
            pricing_module=None,  # real DEX prices injected below via patch
            inventory_tracker=tracker,
            fee_structure=fees,
            config={
                "min_spread_bps": 1,
                "min_profit_usd": 0.01,
                "cooldown_seconds": 0,
                "signal_ttl_seconds": 30,
                "max_position_usd": 10_000_000,
            },
        )

        # Build real Uniswap prices dict and inject via _fetch_prices patch
        real_prices = None
        if use_real_dex:
            try:
                dex_buy, dex_sell = pricer.get_prices_for_pair(pair, size)
                bid = float(ob["best_bid"][0])
                ask = float(ob["best_ask"][0])
                mid = (bid + ask) / 2
                real_prices = {
                    "cex_bid": bid,
                    "cex_ask": ask,
                    "dex_buy": dex_buy,
                    "dex_sell": dex_sell,
                    "bid_ask_spread_bps": (ask - bid) / mid * 10_000 if mid > 0 else 0.0,
                }
            except Exception as exc:
                state.add_log(
                    f"[yellow]⚠[/yellow] {pair} — Uniswap fetch failed ({exc}), using stub"
                )
                use_real_dex = False

        try:
            from unittest.mock import patch as _patch

            if real_prices is not None:
                with _patch.object(gen, "_fetch_prices", return_value=real_prices):
                    sig = gen.generate(pair, size)
            else:
                sig = gen.generate(pair, size)
        except Exception as exc:
            state.add_log(f"[red]✗[/red] {pair} — generator error: {exc}")
            sig = None

        if sig is None:
            state.signals_skipped += 1
            if real_prices is not None:
                # Log actual spreads so demo shows why no opportunity
                dex_s = real_prices["dex_sell"]
                dex_b = real_prices["dex_buy"]
                cex_a = real_prices["cex_ask"]
                cex_b = real_prices["cex_bid"]
                sp_a = (dex_s - cex_a) / cex_a * 10_000 if cex_a > 0 else 0
                sp_b = (cex_b - dex_b) / dex_b * 10_000 if dex_b > 0 else 0
                state.add_log(
                    f"[dim]~ {pair} DEX:real — no arb  "
                    f"sell_spread={sp_a:+.2f}bps  buy_spread={sp_b:+.2f}bps  "
                    f"(efficient market)[/dim]"
                )
            else:
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
        dex_tag = "[green]DEX:real[/green]" if use_real_dex else "[yellow]DEX:stub[/yellow]"
        state.add_log(
            f"[cyan]↑[/cyan] {pair} {dex_tag} — score [bold]{sig.score:.1f}[/bold]  "
            f"spread {float(sig.spread_bps):.1f}bps  net ${float(sig.expected_net_pnl):.4f}"
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


async def run_demo(
    pairs: list[str],
    size: float,
    ticks: int,
    interval: float,
    rpc_url: str | None = None,
) -> None:
    tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
    assets = {"USDT": "500000"}
    for pair in pairs:
        assets[pair.split("/")[0]] = "100"
    tracker.update_from_cex(
        Venue.BINANCE, {k: {"free": v, "locked": "0"} for k, v in assets.items()}
    )
    tracker.update_from_wallet(Venue.WALLET, assets)

    pricer: SimpleUniswapPricer | None = None
    if rpc_url:
        pricer = SimpleUniswapPricer(rpc_url)

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
    state.dex_mode = f"Uniswap V2 ({rpc_url.split('/')[2]})" if rpc_url else "Stub (mid×1.008)"

    obs: dict = {p: None for p in pairs}
    scored: list = []
    results: list = []

    if rpc_url:
        state.add_log(f"[green]DEX mode: real Uniswap V2 via {rpc_url.split('/')[2]}[/green]")
    else:
        state.add_log("[yellow]DEX mode: stub — pass --rpc-url for real Uniswap prices[/yellow]")

    with Live(
        _build_layout(state, pairs, obs, scored, results),
        console=console,
        refresh_per_second=4,
        screen=True,
    ) as live:
        tick_count = 0
        while ticks == 0 or tick_count < ticks:
            obs, scored, results = await _run_tick(
                pairs,
                size,
                tracker,
                scorer,
                executor,
                queue,
                state,
                score_threshold=0.0,
                pricer=pricer,
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
    parser = argparse.ArgumentParser(
        description="Week 4 live pipeline demo",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python demo_week4.py                                    # stub DEX\n"
            "  python demo_week4.py --rpc-url https://eth.llamarpc.com # real Uniswap V2\n"
            "  python demo_week4.py --pairs ETH/USDT --ticks 20\n"
        ),
    )
    parser.add_argument("--pairs", nargs="+", default=["ETH/USDT", "BTC/USDT"])
    parser.add_argument("--size", type=float, default=1.0)
    parser.add_argument("--ticks", type=int, default=10, help="0 = run until Ctrl+C")
    parser.add_argument("--interval", type=float, default=3.0, help="Seconds between ticks")
    parser.add_argument(
        "--rpc-url",
        default=None,
        help=(
            "Ethereum RPC URL for real Uniswap V2 DEX prices. "
            "Free options: https://eth.llamarpc.com  https://ethereum.publicnode.com"
        ),
    )
    args = parser.parse_args()

    try:
        asyncio.run(run_demo(args.pairs, args.size, args.ticks, args.interval, args.rpc_url))
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/yellow]")
