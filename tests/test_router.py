"""
tests/test_router.py — Tests for pricing/router.py (Route and RouteFinder).
"""

from __future__ import annotations

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.router import Route, RouteFinder

USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6
)
WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)
PEPE = Token(
    address=Address("0x6982508145454Ce325dDbE47a25d4ec3d2311933"), symbol="PEPE", decimals=18
)


def make_shallow_usdc_weth() -> UniswapV2Pair:
    """100k USDC / 50 WETH — sparse liquidity, higher slippage."""
    return UniswapV2Pair(
        address=Address("0x0000000000000000000000000000000000000001"),
        token0=USDC,
        token1=WETH,
        reserve0=100_000 * 10**6,
        reserve1=50 * 10**18,
        fee_bps=30,
    )


def make_deep_usdc_dai() -> UniswapV2Pair:
    """10M USDC / 10M DAI — deep stable pool, fee=5bps."""
    return UniswapV2Pair(
        address=Address("0x0000000000000000000000000000000000000002"),
        token0=USDC,
        token1=DAI,
        reserve0=10_000_000 * 10**6,
        reserve1=10_000_000 * 10**18,
        fee_bps=5,
    )


def make_deep_dai_weth() -> UniswapV2Pair:
    """2M DAI / 1000 WETH — deep pair, low slippage on small trades."""
    return UniswapV2Pair(
        address=Address("0x0000000000000000000000000000000000000003"),
        token0=DAI,
        token1=WETH,
        reserve0=2_000_000 * 10**18,
        reserve1=1_000 * 10**18,
        fee_bps=30,
    )


class TestRoute:
    def test_single_hop_construction(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        assert route.num_hops == 1
        assert route.path[0] == USDC
        assert route.path[-1] == WETH

    def test_two_hop_construction(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        assert route.num_hops == 2

    def test_path_length_mismatch_raises(self):
        pool = make_shallow_usdc_weth()
        with pytest.raises(ValueError, match="path length must be len\\(pools\\)"):
            Route(pools=[pool], path=[USDC, DAI, WETH])

    def test_empty_pools_raises(self):
        with pytest.raises(ValueError, match="at least one pool"):
            Route(pools=[], path=[USDC])

    def test_get_output_single_hop(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        amount_in = 1000 * 10**6  # 1000 USDC
        out = route.get_output(amount_in)
        assert out == pool.get_amount_out(amount_in, USDC)

    def test_get_output_two_hop_matches_sequential(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amount_in = 1000 * 10**6
        mid = p1.get_amount_out(amount_in, USDC)
        expected = p2.get_amount_out(mid, DAI)
        assert route.get_output(amount_in) == expected

    def test_get_intermediate_amounts_length(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amounts = route.get_intermediate_amounts(1000 * 10**6)
        assert len(amounts) == route.num_hops + 1

    def test_get_intermediate_amounts_first_is_input(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        amount_in = 500 * 10**6
        amounts = route.get_intermediate_amounts(amount_in)
        assert amounts[0] == amount_in

    def test_get_intermediate_amounts_last_matches_get_output(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amount_in = 1000 * 10**6
        amounts = route.get_intermediate_amounts(amount_in)
        assert amounts[-1] == route.get_output(amount_in)

    def test_estimate_gas_single_hop(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        assert route.estimate_gas() == 150_000 + 100_000

    def test_estimate_gas_two_hop(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        assert route.estimate_gas() == 150_000 + 200_000

    def test_repr_contains_symbols(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        assert "USDC" in repr(route)
        assert "WETH" in repr(route)


# ── TestRouteFinder ────────────────────────────────────────────────────────────


class TestRouteFinder:
    def test_graph_built_bidirectionally(self):
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        assert USDC in finder.graph
        assert WETH in finder.graph

    def test_find_all_routes_direct(self):
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        routes = finder.find_all_routes(USDC, WETH)
        assert len(routes) == 1
        assert routes[0].num_hops == 1

    def test_find_all_routes_two_hop(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        finder = RouteFinder(pools=[p1, p2])
        routes = finder.find_all_routes(USDC, WETH)
        assert len(routes) == 1
        assert routes[0].num_hops == 2

    def test_find_all_routes_direct_and_multihop(self):
        p_direct = make_shallow_usdc_weth()
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        finder = RouteFinder(pools=[p_direct, p1, p2])
        routes = finder.find_all_routes(USDC, WETH)
        hops = sorted(r.num_hops for r in routes)
        assert hops == [1, 2]

    def test_find_all_routes_none_if_disconnected(self):
        pool = make_shallow_usdc_weth()  # USDC/WETH only
        finder = RouteFinder(pools=[pool])
        routes = finder.find_all_routes(USDC, DAI)
        assert routes == []

    def test_find_all_routes_respects_max_hops(self):
        p_direct = make_shallow_usdc_weth()
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        finder = RouteFinder(pools=[p_direct, p1, p2])
        routes_1 = finder.find_all_routes(USDC, WETH, max_hops=1)
        assert all(r.num_hops <= 1 for r in routes_1)
        assert len(routes_1) == 1

    def test_find_all_routes_no_cycles(self):
        """Each pool should appear at most once per route."""
        p_direct = make_shallow_usdc_weth()
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        finder = RouteFinder(pools=[p_direct, p1, p2])
        for route in finder.find_all_routes(USDC, WETH):
            pool_ids = [id(p) for p in route.pools]
            assert len(pool_ids) == len(set(pool_ids))


# ── TestDirectVsMultihop ───────────────────────────────────────────────────────


class TestDirectVsMultihop:
    """
    At low gas cost the deeper 2-hop route yields more net output.
    At high gas cost the cheaper direct route wins.
    """

    def setup_method(self):
        self.p_direct = make_shallow_usdc_weth()
        self.p1 = make_deep_usdc_dai()
        self.p2 = make_deep_dai_weth()
        self.finder = RouteFinder(pools=[self.p_direct, self.p1, self.p2])
        self.amount_in = 1000 * 10**6  # 1000 USDC

    def test_multihop_gross_better_than_direct(self):
        """2-hop route gets more WETH before gas."""
        direct_gross = self.p_direct.get_amount_out(self.amount_in, USDC)
        mid = self.p1.get_amount_out(self.amount_in, USDC)
        twohop_gross = self.p2.get_amount_out(mid, DAI)
        assert twohop_gross > direct_gross

    def test_low_gas_favours_multihop(self):
        best_route, net = self.finder.find_best_route(USDC, WETH, self.amount_in, gas_price_gwei=1)
        assert best_route.num_hops == 2

    def test_high_gas_favours_direct(self):
        best_route, net = self.finder.find_best_route(
            USDC, WETH, self.amount_in, gas_price_gwei=100
        )
        assert best_route.num_hops == 1

    def test_compare_routes_returns_all(self):
        results = self.finder.compare_routes(USDC, WETH, self.amount_in, gas_price_gwei=1)
        assert len(results) == 2

    def test_compare_routes_sorted_best_first(self):
        results = self.finder.compare_routes(USDC, WETH, self.amount_in, gas_price_gwei=1)
        nets = [r["net_output"] for r in results]
        assert nets == sorted(nets, reverse=True)

    def test_compare_routes_contains_expected_keys(self):
        results = self.finder.compare_routes(USDC, WETH, self.amount_in, gas_price_gwei=1)
        for r in results:
            assert set(r.keys()) == {
                "route",
                "gross_output",
                "gas_estimate",
                "gas_cost",
                "net_output",
            }

    def test_compare_routes_gas_cost_correct(self):
        gas_price_gwei = 5
        results = self.finder.compare_routes(
            USDC, WETH, self.amount_in, gas_price_gwei=gas_price_gwei
        )
        for r in results:
            expected_cost = gas_price_gwei * 10**9 * r["route"].estimate_gas()
            assert r["gas_cost"] == expected_cost

    def test_net_output_is_gross_minus_gas(self):
        results = self.finder.compare_routes(USDC, WETH, self.amount_in, gas_price_gwei=1)
        for r in results:
            expected_net = max(0, r["gross_output"] - r["gas_cost"])
            assert r["net_output"] == expected_net


# ── TestNoRouteExists ──────────────────────────────────────────────────────────


class TestNoRouteExists:
    def test_find_all_routes_empty(self):
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        routes = finder.find_all_routes(USDC, DAI)
        assert routes == []

    def test_find_best_route_raises_value_error(self):
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        with pytest.raises(ValueError, match="No route found"):
            finder.find_best_route(USDC, DAI, 1000 * 10**6, gas_price_gwei=1)

    def test_compare_routes_returns_empty_list(self):
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        results = finder.compare_routes(USDC, DAI, 1000 * 10**6, gas_price_gwei=1)
        assert results == []

    def test_isolated_token_no_route(self):
        """A pool that doesn't connect to the target."""
        pool = make_shallow_usdc_weth()
        finder = RouteFinder(pools=[pool])
        routes = finder.find_all_routes(USDC, PEPE)
        assert routes == []

    def test_exceeds_max_hops_returns_empty(self):
        """Route exists but requires 2 hops; max_hops=1 should return empty."""
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        finder = RouteFinder(pools=[p1, p2])
        routes = finder.find_all_routes(USDC, WETH, max_hops=1)
        assert routes == []


# ── TestRouteOutputMatchesSequentialSwaps ─────────────────────────────────────


class TestRouteOutputMatchesSequentialSwaps:
    """Route.get_output must match manually chained get_amount_out calls."""

    def test_single_hop_matches(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        amount_in = 500 * 10**6
        expected = pool.get_amount_out(amount_in, USDC)
        assert route.get_output(amount_in) == expected

    def test_two_hop_matches(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amount_in = 1000 * 10**6
        mid = p1.get_amount_out(amount_in, USDC)
        expected = p2.get_amount_out(mid, DAI)
        assert route.get_output(amount_in) == expected

    def test_reverse_direction_single_hop(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[WETH, USDC])
        amount_in = 1 * 10**18  # 1 WETH
        expected = pool.get_amount_out(amount_in, WETH)
        assert route.get_output(amount_in) == expected

    def test_intermediate_amounts_match_step_by_step(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amount_in = 1000 * 10**6
        amounts = route.get_intermediate_amounts(amount_in)
        step1 = p1.get_amount_out(amount_in, USDC)
        step2 = p2.get_amount_out(step1, DAI)
        assert amounts[0] == amount_in
        assert amounts[1] == step1
        assert amounts[2] == step2

    def test_different_input_sizes_consistent(self):
        pool = make_shallow_usdc_weth()
        route = Route(pools=[pool], path=[USDC, WETH])
        for amount_human in [100, 1_000, 10_000]:
            amount_in = amount_human * 10**6
            assert route.get_output(amount_in) == pool.get_amount_out(amount_in, USDC)

    def test_route_output_is_deterministic(self):
        p1 = make_deep_usdc_dai()
        p2 = make_deep_dai_weth()
        route = Route(pools=[p1, p2], path=[USDC, DAI, WETH])
        amount_in = 1000 * 10**6
        out1 = route.get_output(amount_in)
        out2 = route.get_output(amount_in)
        assert out1 == out2
