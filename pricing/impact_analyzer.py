"""
pricing/impact_analyzer.py — Price impact analysis and CLI.
"""

from __future__ import annotations

import argparse
import sys
from decimal import Decimal

from chain.client import ChainClient
from core.types import Address, Token
from pricing.amm import UniswapV2Pair

_WETH_ADDR = "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2"


class PriceImpactAnalyzer:
    """
    Analyzes how trade size affects execution price for a Uniswap V2 pair.
    """

    def __init__(self, pair: UniswapV2Pair) -> None:
        self.pair = pair

    def generate_impact_table(
        self,
        token_in: Token,
        sizes: list[int],
    ) -> list[dict]:
        """
        Generate impact data for a list of raw input sizes.
        """
        if not sizes:
            return []

        token_out = self.pair.token1 if token_in == self.pair.token0 else self.pair.token0
        spot_human = _spot_price_human(self.pair, token_in, token_out)

        rows = []
        for amount_in in sizes:
            amount_out = self.pair.get_amount_out(amount_in, token_in)
            amount_in_h = Decimal(amount_in) / Decimal(10**token_in.decimals)
            amount_out_h = Decimal(amount_out) / Decimal(10**token_out.decimals)
            exec_price = amount_in_h / amount_out_h if amount_out_h else Decimal(0)
            impact_pct = self.pair.get_price_impact(amount_in, token_in) * 100
            rows.append(
                {
                    "amount_in": amount_in,
                    "amount_out": amount_out,
                    "spot_price": spot_human,
                    "execution_price": exec_price,
                    "price_impact_pct": impact_pct,
                }
            )
        return rows

    def find_max_size_for_impact(
        self,
        token_in: Token,
        max_impact_pct: Decimal,
    ) -> int:
        """
        Binary search for the largest trade whose impact stays at or below
        max_impact_pct.
        """
        if max_impact_pct <= 0:
            raise ValueError(f"max_impact_pct must be positive, got {max_impact_pct}.")
        max_impact_frac = max_impact_pct / 100
        reserve_in, _ = self.pair._reserves_for_token_in(token_in)
        lo, hi = 1, reserve_in // 2
        best = 0
        while lo <= hi:
            mid = (lo + hi) // 2
            if self.pair.get_price_impact(mid, token_in) <= max_impact_frac:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    def estimate_true_cost(
        self,
        amount_in: int,
        token_in: Token,
        gas_price_gwei: int,
        gas_estimate: int = 150_000,
    ) -> dict:
        """
        Estimate total cost of a swap including gas.
        """
        gross_output = self.pair.get_amount_out(amount_in, token_in)
        gas_cost_eth = gas_price_gwei * 10**9 * gas_estimate

        token_out = self.pair.token1 if token_in == self.pair.token0 else self.pair.token0

        if token_out.address.lower == _WETH_ADDR:
            gas_cost_output = gas_cost_eth
        elif token_in.address.lower == _WETH_ADDR:
            spot_raw = self.pair.get_spot_price(token_in)
            gas_cost_output = int(Decimal(gas_cost_eth) * spot_raw)
        else:
            gas_cost_output = 0

        net_output = max(0, gross_output - gas_cost_output)
        token_out_dec = token_out.decimals
        token_in_dec = token_in.decimals
        net_output_h = Decimal(net_output) / Decimal(10**token_out_dec)
        amount_in_h = Decimal(amount_in) / Decimal(10**token_in_dec)
        effective_price = net_output_h / amount_in_h if amount_in_h else Decimal(0)

        return {
            "gross_output": gross_output,
            "gas_cost_eth": gas_cost_eth,
            "gas_cost_in_output_token": gas_cost_output,
            "net_output": net_output,
            "effective_price": effective_price,
        }


def _spot_price_human(pair: UniswapV2Pair, token_in: Token, token_out: Token) -> Decimal:
    """
    Return spot price as 'how many token_in per token_out' in human units.
    E.g. for USDC→ETH returns 2000 (USDC per ETH).
    """
    spot_raw = pair.get_spot_price(token_in)
    out_per_in_human = spot_raw * Decimal(10**token_in.decimals) / Decimal(10**token_out.decimals)
    if out_per_in_human == 0:
        return Decimal(0)
    return Decimal(1) / out_per_in_human


_COL_W = [14, 14, 14, 10]


def _hline(left: str, mid: str, right: str) -> str:
    segs = ["─" * (w + 2) for w in _COL_W]
    return left + mid.join(segs) + right


def _row(cols: list[str]) -> str:
    cells = [f" {c:>{_COL_W[i]}} " for i, c in enumerate(cols)]
    return "│" + "│".join(cells) + "│"


def _fmt_amount(raw: int, decimals: int) -> str:
    value = Decimal(raw) / Decimal(10**decimals)
    return f"{value:,.4f}".rstrip("0").rstrip(".")


def format_table(
    rows: list[dict],
    token_in: Token,
    token_out: Token,
    pair: UniswapV2Pair,
    max_size: int,
    max_impact_pct: Decimal,
) -> str:
    """Render the impact analysis as a human-readable table."""
    lines: list[str] = []

    reserve_in, reserve_out = pair._reserves_for_token_in(token_in)
    spot = _spot_price_human(pair, token_in, token_out)

    res_in_h = _fmt_amount(reserve_in, token_in.decimals)
    res_out_h = _fmt_amount(reserve_out, token_out.decimals)

    lines.append(f"\nPrice Impact Analysis for {token_in.symbol} → {token_out.symbol}")
    lines.append(f"Reserves: {res_out_h} {token_out.symbol} / {res_in_h} {token_in.symbol}")
    lines.append(f"Spot Price: {spot:,.2f} {token_in.symbol}/{token_out.symbol}")
    lines.append("")

    headers = [
        f"{token_in.symbol} In",
        f"{token_out.symbol} Out",
        "Exec Price",
        "Impact",
    ]
    lines.append(_hline("┌", "┬", "┐"))
    lines.append(_row(headers))
    lines.append(_hline("├", "┼", "┤"))

    for r in rows:
        exec_fmt = f"{r['execution_price']:,.2f}"
        impact_fmt = f"{r['price_impact_pct']:.2f}%"
        lines.append(
            _row(
                [
                    _fmt_amount(r["amount_in"], token_in.decimals),
                    _fmt_amount(r["amount_out"], token_out.decimals),
                    exec_fmt,
                    impact_fmt,
                ]
            )
        )

    lines.append(_hline("└", "┴", "┘"))
    max_h = _fmt_amount(max_size, token_in.decimals)
    lines.append(f"\nMax trade for {max_impact_pct}% impact: {max_h} {token_in.symbol}")
    return "\n".join(lines)


def _resolve_token(pair: UniswapV2Pair, symbol_or_addr: str) -> Token:
    """Find a token in the pair by symbol or address."""
    s = symbol_or_addr.strip().upper()
    if s == pair.token0.symbol.upper():
        return pair.token0
    if s == pair.token1.symbol.upper():
        return pair.token1
    try:
        addr = Address(symbol_or_addr)
        if addr == pair.token0.address:
            return pair.token0
        if addr == pair.token1.address:
            return pair.token1
    except Exception:
        pass
    raise ValueError(
        f"Token '{symbol_or_addr}' not found in pair "
        f"({pair.token0.symbol}/{pair.token1.symbol})."
    )


DEFAULT_RPC = "https://eth-mainnet.g.alchemy.com/v2/demo"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Analyse price impact for a Uniswap V2 pair",
        prog="python -m pricing.impact_analyzer",
    )
    parser.add_argument("pair_address", help="Pair contract address (0x...)")
    parser.add_argument(
        "--token-in",
        required=True,
        help="Symbol or address of the input token (e.g. USDC)",
    )
    parser.add_argument(
        "--sizes",
        required=True,
        help="Comma-separated input amounts in human units (e.g. 1000,10000)",
    )
    parser.add_argument("--rpc", default=DEFAULT_RPC, help="RPC URL")
    parser.add_argument(
        "--max-impact",
        default="1",
        help="Max impact %% for the max-trade-size line (default: 1)",
    )
    args = parser.parse_args(argv)

    try:
        max_impact_pct = Decimal(args.max_impact)
    except Exception:
        print(f"Error: invalid --max-impact: {args.max_impact!r}", file=sys.stderr)
        return 1

    try:
        client = ChainClient(rpc_urls=[args.rpc], max_retries=1)
        pair = UniswapV2Pair.from_chain(Address(args.pair_address), client)
    except Exception as exc:
        print(f"Error: failed to load pair — {exc}", file=sys.stderr)
        return 1

    try:
        token_in = _resolve_token(pair, args.token_in)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    token_out = pair.token1 if token_in == pair.token0 else pair.token0

    try:
        raw_sizes = [
            int(Decimal(s.strip()) * Decimal(10**token_in.decimals)) for s in args.sizes.split(",")
        ]
    except Exception as exc:
        print(f"Error: invalid --sizes — {exc}", file=sys.stderr)
        return 1

    analyzer = PriceImpactAnalyzer(pair)
    try:
        rows = analyzer.generate_impact_table(token_in, raw_sizes)
        max_size = analyzer.find_max_size_for_impact(token_in, max_impact_pct)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(format_table(rows, token_in, token_out, pair, max_size, max_impact_pct))
    return 0


if __name__ == "__main__":
    sys.exit(main())
