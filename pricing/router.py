"""
pricing/router.py — Multi-hop route discovery and comparison for Uniswap V2 pairs.
"""

from __future__ import annotations

from collections import defaultdict

from core.types import Token
from pricing.amm import UniswapV2Pair

_GAS_BASE = 150_000
_GAS_PER_HOP = 100_000


class Route:
    """
    A sequence of Uniswap V2 pools that routes token_in → token_out.
    """

    def __init__(self, pools: list[UniswapV2Pair], path: list[Token]) -> None:
        if len(path) != len(pools) + 1:
            raise ValueError(
                f"path length must be len(pools) + 1, "
                f"got len(pools)={len(pools)}, len(path)={len(path)}."
            )
        if len(pools) == 0:
            raise ValueError("A route must have at least one pool.")
        self.pools = pools
        self.path = path

    @property
    def num_hops(self) -> int:
        """Number of swaps in this route."""
        return len(self.pools)

    def get_output(self, amount_in: int) -> int:
        """
        Simulate the full route and return the final raw output amount.
        """
        amount = amount_in
        for pool, token_in in zip(self.pools, self.path):
            amount = pool.get_amount_out(amount, token_in)
        return amount

    def get_intermediate_amounts(self, amount_in: int) -> list[int]:
        """
        Return all amounts at each step, including the initial input.
        """
        amounts = [amount_in]
        amount = amount_in
        for pool, token_in in zip(self.pools, self.path):
            amount = pool.get_amount_out(amount, token_in)
            amounts.append(amount)
        return amounts

    def estimate_gas(self) -> int:
        """
        Estimate gas cost: ~150k base + ~100k per hop.
        """
        return _GAS_BASE + _GAS_PER_HOP * self.num_hops

    def __repr__(self) -> str:
        symbols = " → ".join(t.symbol for t in self.path)
        return f"Route({symbols}, {self.num_hops} hop(s))"


class RouteFinder:
    """
    Discovers and compares all routes between two tokens across a pool set.
    """

    def __init__(self, pools: list[UniswapV2Pair]) -> None:
        self.pools = pools
        self.graph = self._build_graph()

    def _build_graph(self) -> dict[Token, list[tuple[UniswapV2Pair, Token]]]:
        """Build adjacency map: token → [(pool, other_token), ...]."""
        graph: dict[Token, list[tuple[UniswapV2Pair, Token]]] = defaultdict(list)
        for pool in self.pools:
            graph[pool.token0].append((pool, pool.token1))
            graph[pool.token1].append((pool, pool.token0))
        return graph

    def find_all_routes(
        self,
        token_in: Token,
        token_out: Token,
        max_hops: int = 3,
    ) -> list[Route]:
        """
        DFS to find all simple routes from token_in to token_out.
        """
        routes: list[Route] = []
        self._dfs(
            current=token_in,
            token_out=token_out,
            max_hops=max_hops,
            visited_tokens={token_in},
            visited_pools=set(),
            pools_so_far=[],
            path_so_far=[token_in],
            routes=routes,
        )
        return routes

    def _dfs(
        self,
        current: Token,
        token_out: Token,
        max_hops: int,
        visited_tokens: set[Token],
        visited_pools: set[int],
        pools_so_far: list[UniswapV2Pair],
        path_so_far: list[Token],
        routes: list[Route],
    ) -> None:
        if current == token_out:
            routes.append(Route(list(pools_so_far), list(path_so_far)))
            return
        if len(pools_so_far) >= max_hops:
            return
        for pool, neighbour in self.graph.get(current, []):
            pool_id = id(pool)
            if pool_id in visited_pools:
                continue
            if neighbour in visited_tokens and neighbour != token_out:
                continue
            pools_so_far.append(pool)
            path_so_far.append(neighbour)
            visited_tokens.add(neighbour)
            visited_pools.add(pool_id)
            self._dfs(
                current=neighbour,
                token_out=token_out,
                max_hops=max_hops,
                visited_tokens=visited_tokens,
                visited_pools=visited_pools,
                pools_so_far=pools_so_far,
                path_so_far=path_so_far,
                routes=routes,
            )
            pools_so_far.pop()
            path_so_far.pop()
            visited_tokens.discard(neighbour)
            visited_pools.discard(pool_id)

    def find_best_route(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
        max_hops: int = 3,
    ) -> tuple[Route, int]:
        """
        Find the route with the highest net output after gas costs.
        """
        routes = self.find_all_routes(token_in, token_out, max_hops)
        if not routes:
            raise ValueError(
                f"No route found from {token_in.symbol} to {token_out.symbol} "
                f"with max_hops={max_hops}."
            )
        gas_price_wei = gas_price_gwei * 10**9
        best_route: Route | None = None
        best_net = -1
        for route in routes:
            gross = route.get_output(amount_in)
            gas_cost = gas_price_wei * route.estimate_gas()
            net = max(0, gross - gas_cost)
            if best_route is None or net > best_net:
                best_route = route
                best_net = net
        assert best_route is not None
        return best_route, best_net

    def compare_routes(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
    ) -> list[dict]:
        """
        Compare all routes and return a list of result dicts, sorted by
        net_output descending.
        """
        routes = self.find_all_routes(token_in, token_out)
        gas_price_wei = gas_price_gwei * 10**9
        results = []
        for route in routes:
            gross = route.get_output(amount_in)
            gas_estimate = route.estimate_gas()
            gas_cost = gas_price_wei * gas_estimate
            net = max(0, gross - gas_cost)
            results.append(
                {
                    "route": route,
                    "gross_output": gross,
                    "gas_estimate": gas_estimate,
                    "gas_cost": gas_cost,
                    "net_output": net,
                }
            )
        results.sort(key=lambda r: r["net_output"], reverse=True)
        return results
