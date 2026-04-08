"""
pricing/arbitrage.py — Arbitrage opportunity detection across Uniswap V2 pools.

Two strategies are implemented:

1. **Circular arbitrage** — start with *token*, traverse N pools, return to
   *token* and check if output > input (after gas).

2. **Cross-pool arbitrage** — two different pools share the same token pair;
   buy on the cheaper pool and sell on the more expensive one.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass

from core.types import Token
from pricing.amm import UniswapV2Pair
from pricing.router import _GAS_BASE, _GAS_PER_HOP, Route

log = logging.getLogger(__name__)

_GAS_PRICE_WEI_DEFAULT = 20 * 10**9  # 20 gwei fallback


# ── ArbitrageOpportunity ──────────────────────────────────────────────────────


@dataclass
class ArbitrageOpportunity:
    """
    Describes a detected arbitrage path.

    Attributes:
        route:          The sequence of pools and tokens traversed.
        amount_in:      Input amount (in raw *token* units).
        gross_profit:   amount_out − amount_in (before gas).
        gas_cost:       Estimated gas cost in the same raw units as amount_in.
        token:          The token in which profit is denominated.
        strategy:       ``"circular"`` or ``"cross_pool"``.
    """

    route: Route
    amount_in: int
    gross_profit: int
    gas_cost: int
    token: Token
    strategy: str

    @property
    def net_profit(self) -> int:
        """Gross profit minus estimated gas cost."""
        return self.gross_profit - self.gas_cost

    @property
    def is_profitable(self) -> bool:
        """True if gross_profit > 0 (pre-gas positive return)."""
        return self.gross_profit > 0

    @property
    def is_net_profitable(self) -> bool:
        """True if net_profit > 0 (after gas)."""
        return self.net_profit > 0

    def __repr__(self) -> str:
        return (
            f"ArbitrageOpportunity(strategy={self.strategy!r}, "
            f"gross={self.gross_profit}, net={self.net_profit}, "
            f"route={self.route!r})"
        )


# ── ArbitrageDetector ─────────────────────────────────────────────────────────


class ArbitrageDetector:
    """
    Detects circular and cross-pool arbitrage opportunities across a set of
    Uniswap V2 pools.

    Args:
        pools: List of UniswapV2Pair objects to scan.
    """

    def __init__(self, pools: list[UniswapV2Pair]) -> None:
        self.pools = pools
        self._graph: dict[Token, list[tuple[UniswapV2Pair, Token]]] = self._build_graph()

    # ── public API ────────────────────────────────────────────────────────────

    def find_circular_arbitrage(
        self,
        token: Token,
        amount_in: int,
        gas_price_gwei: int,
        max_hops: int = 3,
    ) -> list[ArbitrageOpportunity]:
        """
        Find all circular paths that start and end at *token* and yield
        gross_profit > 0.

        A circular route visits *max_hops* distinct pools and returns to the
        origin token.  The search uses a cycle-free DFS; each pool may be
        used only once.

        Args:
            token:          Starting / ending token.
            amount_in:      Amount to trade (raw, in *token* units).
            gas_price_gwei: Gas price for net-profit calculation.
            max_hops:       Maximum number of pool hops (default 3).

        Returns:
            List of ArbitrageOpportunity sorted by gross_profit descending.
        """
        gas_price_wei = gas_price_gwei * 10**9
        opportunities: list[ArbitrageOpportunity] = []
        self._dfs_circular(
            start_token=token,
            current=token,
            amount_in=amount_in,
            current_amount=amount_in,
            gas_price_wei=gas_price_wei,
            max_hops=max_hops,
            visited_pools=set(),
            pools_so_far=[],
            path_so_far=[token],
            opportunities=opportunities,
        )
        opportunities.sort(key=lambda o: o.gross_profit, reverse=True)
        return opportunities

    def find_cross_pool_arbitrage(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
    ) -> list[ArbitrageOpportunity]:
        """
        Find arbitrage opportunities across two pools that share the same
        token pair.

        For each pair of pools that both contain (token_in, token_out), we
        compare buying token_out on the cheaper pool then selling back on the
        more expensive one.

        Args:
            token_in:       Token to spend.
            token_out:      Token to receive.
            amount_in:      Amount to trade (raw).
            gas_price_gwei: Gas price for gas cost estimation.

        Returns:
            List of ArbitrageOpportunity sorted by gross_profit descending.
        """
        # Find all pools that contain both tokens
        candidate_pools = [
            p
            for p in self.pools
            if (p.token0 == token_in and p.token1 == token_out)
            or (p.token0 == token_out and p.token1 == token_in)
            or (p.token0 == token_in and p.token1 == token_out)
            or (p.token1 == token_in and p.token0 == token_out)
        ]
        # Deduplicate by id
        seen: set[int] = set()
        unique_pools: list[UniswapV2Pair] = []
        for p in candidate_pools:
            if id(p) not in seen:
                seen.add(id(p))
                unique_pools.append(p)

        gas_price_wei = gas_price_gwei * 10**9
        # Gas: 2 hops (buy + sell back)
        gas_cost = gas_price_wei * (_GAS_BASE + _GAS_PER_HOP * 2)

        opportunities: list[ArbitrageOpportunity] = []
        for i, pool_a in enumerate(unique_pools):
            for pool_b in unique_pools[i + 1 :]:
                # Strategy A: buy on pool_a, sell back on pool_b
                opp = self._cross_pool_pair(
                    pool_buy=pool_a,
                    pool_sell=pool_b,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=amount_in,
                    gas_cost=gas_cost,
                )
                if opp is not None:
                    opportunities.append(opp)

                # Strategy B: buy on pool_b, sell back on pool_a
                opp = self._cross_pool_pair(
                    pool_buy=pool_b,
                    pool_sell=pool_a,
                    token_in=token_in,
                    token_out=token_out,
                    amount_in=amount_in,
                    gas_cost=gas_cost,
                )
                if opp is not None:
                    opportunities.append(opp)

        opportunities.sort(key=lambda o: o.gross_profit, reverse=True)
        return opportunities

    def find_best_circular_arbitrage(
        self,
        token: Token,
        amount_in: int,
        gas_price_gwei: int,
        max_hops: int = 3,
    ) -> ArbitrageOpportunity | None:
        """
        Return the single most profitable circular opportunity, or None if
        none is found with gross_profit > 0.

        Args:
            token:          Starting / ending token.
            amount_in:      Amount to trade.
            gas_price_gwei: Gas price in gwei.
            max_hops:       Maximum hop count.
        """
        opps = self.find_circular_arbitrage(token, amount_in, gas_price_gwei, max_hops)
        return opps[0] if opps else None

    # ── internal helpers ──────────────────────────────────────────────────────

    def _build_graph(self) -> dict[Token, list[tuple[UniswapV2Pair, Token]]]:
        graph: dict[Token, list[tuple[UniswapV2Pair, Token]]] = defaultdict(list)
        for pool in self.pools:
            graph[pool.token0].append((pool, pool.token1))
            graph[pool.token1].append((pool, pool.token0))
        return graph

    def _dfs_circular(
        self,
        start_token: Token,
        current: Token,
        amount_in: int,
        current_amount: int,
        gas_price_wei: int,
        max_hops: int,
        visited_pools: set[int],
        pools_so_far: list[UniswapV2Pair],
        path_so_far: list[Token],
        opportunities: list[ArbitrageOpportunity],
    ) -> None:
        """
        DFS that tracks the running output amount.  A valid cycle is recorded
        when we return to start_token with at least one hop.
        """
        for pool, neighbour in self._graph.get(current, []):
            pool_id = id(pool)
            if pool_id in visited_pools:
                continue

            out = pool.get_amount_out(current_amount, current)

            # Close the cycle
            if neighbour == start_token and pools_so_far:
                gross_profit = out - amount_in
                if gross_profit > 0:
                    route = Route(
                        pools=list(pools_so_far) + [pool],
                        path=list(path_so_far) + [neighbour],
                    )
                    num_hops = len(route.pools)
                    gas_cost = gas_price_wei * (_GAS_BASE + _GAS_PER_HOP * num_hops)
                    opportunities.append(
                        ArbitrageOpportunity(
                            route=route,
                            amount_in=amount_in,
                            gross_profit=gross_profit,
                            gas_cost=gas_cost,
                            token=start_token,
                            strategy="circular",
                        )
                    )
                continue  # don't recurse further on a closed cycle

            # Don't revisit non-origin tokens (avoid non-simple paths)
            if neighbour in path_so_far:
                continue

            if len(pools_so_far) + 1 >= max_hops:
                continue

            visited_pools.add(pool_id)
            pools_so_far.append(pool)
            path_so_far.append(neighbour)

            self._dfs_circular(
                start_token=start_token,
                current=neighbour,
                amount_in=amount_in,
                current_amount=out,
                gas_price_wei=gas_price_wei,
                max_hops=max_hops,
                visited_pools=visited_pools,
                pools_so_far=pools_so_far,
                path_so_far=path_so_far,
                opportunities=opportunities,
            )

            pools_so_far.pop()
            path_so_far.pop()
            visited_pools.discard(pool_id)

    def _cross_pool_pair(
        self,
        pool_buy: UniswapV2Pair,
        pool_sell: UniswapV2Pair,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_cost: int,
    ) -> ArbitrageOpportunity | None:
        """
        Simulate: buy token_out on pool_buy with amount_in of token_in,
        then sell the received token_out back to token_in on pool_sell.
        """
        try:
            mid_amount = pool_buy.get_amount_out(amount_in, token_in)
            final_amount = pool_sell.get_amount_out(mid_amount, token_out)
        except ValueError:
            return None

        gross_profit = final_amount - amount_in
        if gross_profit <= 0:
            return None

        route = Route(
            pools=[pool_buy, pool_sell],
            path=[token_in, token_out, token_in],
        )
        return ArbitrageOpportunity(
            route=route,
            amount_in=amount_in,
            gross_profit=gross_profit,
            gas_cost=gas_cost,
            token=token_in,
            strategy="cross_pool",
        )
