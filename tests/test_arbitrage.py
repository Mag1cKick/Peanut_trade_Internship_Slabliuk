"""
tests/test_arbitrage.py — Unit tests for pricing.arbitrage.ArbitrageDetector

Test groups:
  1. ArbitrageOpportunity — properties (is_profitable, net_profit, etc.)
  2. find_circular_arbitrage — profitable cycles found, no false positives
  3. find_circular_arbitrage — edge cases (no route, single pool, max_hops)
  4. find_best_circular_arbitrage — returns best or None
  5. find_cross_pool_arbitrage — imbalanced pools create opportunity
  6. find_cross_pool_arbitrage — balanced pools yield no opportunity
  7. find_cross_pool_arbitrage — direction symmetry
"""

from __future__ import annotations

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.arbitrage import ArbitrageDetector, ArbitrageOpportunity
from pricing.router import Route

# ── Shared tokens ─────────────────────────────────────────────────────────────

WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)
USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6
)

ADDR_AB = Address("0x0000000000000000000000000000000000000001")
ADDR_BC = Address("0x0000000000000000000000000000000000000002")
ADDR_CA = Address("0x0000000000000000000000000000000000000003")
ADDR_ALT = Address("0x0000000000000000000000000000000000000004")


def _pair(addr, t0, t1, r0, r1, fee=30) -> UniswapV2Pair:
    return UniswapV2Pair(address=addr, token0=t0, token1=t1, reserve0=r0, reserve1=r1, fee_bps=fee)


# ── 1. ArbitrageOpportunity properties ───────────────────────────────────────


class TestArbitrageOpportunity:
    def _make_opp(self, gross=1000, gas_cost=200) -> ArbitrageOpportunity:
        pool = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        route = Route(pools=[pool], path=[WETH, DAI])
        return ArbitrageOpportunity(
            route=route,
            amount_in=10**18,
            gross_profit=gross,
            gas_cost=gas_cost,
            token=WETH,
            strategy="circular",
        )

    def test_is_profitable_true_when_gross_positive(self):
        assert self._make_opp(gross=1).is_profitable

    def test_is_profitable_false_when_gross_zero(self):
        opp = self._make_opp(gross=0)
        assert not opp.is_profitable

    def test_is_profitable_false_when_gross_negative(self):
        assert not self._make_opp(gross=-1).is_profitable

    def test_net_profit_subtracts_gas(self):
        opp = self._make_opp(gross=1000, gas_cost=200)
        assert opp.net_profit == 800

    def test_is_net_profitable_true(self):
        assert self._make_opp(gross=1000, gas_cost=200).is_net_profitable

    def test_is_net_profitable_false_when_gas_exceeds_gross(self):
        assert not self._make_opp(gross=100, gas_cost=500).is_net_profitable

    def test_repr_contains_strategy(self):
        opp = self._make_opp()
        assert "circular" in repr(opp)


# ── 2. find_circular_arbitrage — profitable cycle ─────────────────────────────


class TestFindCircularArbitrage:
    """
    Triangle: WETH→DAI→USDC→WETH.
    We intentionally imbalance the USDC/WETH pool so the arbitrage pays.
    """

    def _triangle_detector(self, weth_usdc_r0=1, weth_usdc_r1=5000) -> ArbitrageDetector:
        """
        Pool AB: WETH/DAI   (balanced 1:2000)
        Pool BC: DAI/USDC   (balanced 1:1)
        Pool CA: USDC/WETH  (imbalanced: cheap WETH → arb opportunity)
        """
        pool_ab = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_bc = _pair(ADDR_BC, DAI, USDC, 2000 * 10**18, 2000 * 10**18)
        # Very cheap WETH on this pool: 1 WETH costs only 5000 DAI (should be 2000)
        # Buying WETH here is profitable after the DAI→USDC→here route
        pool_ca = _pair(ADDR_CA, USDC, WETH, weth_usdc_r0 * 10**18, weth_usdc_r1 * 10**18)
        return ArbitrageDetector([pool_ab, pool_bc, pool_ca])

    def test_finds_opportunity_in_imbalanced_triangle(self):
        """
        Strongly imbalanced: pool_ca has USDC:WETH = 1:100 (very cheap WETH).
        A circular arb WETH→DAI→USDC→WETH should yield gross_profit > 0.
        """
        detector = self._triangle_detector(weth_usdc_r0=1, weth_usdc_r1=100)
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        assert len(opps) > 0
        assert all(o.gross_profit > 0 for o in opps)

    def test_sorted_by_gross_profit_descending(self):
        detector = self._triangle_detector(weth_usdc_r0=1, weth_usdc_r1=100)
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        if len(opps) > 1:
            profits = [o.gross_profit for o in opps]
            assert profits == sorted(profits, reverse=True)

    def test_strategy_label_is_circular(self):
        detector = self._triangle_detector(weth_usdc_r0=1, weth_usdc_r1=100)
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        for opp in opps:
            assert opp.strategy == "circular"

    def test_route_starts_and_ends_at_token(self):
        detector = self._triangle_detector(weth_usdc_r0=1, weth_usdc_r1=100)
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        for opp in opps:
            assert opp.route.path[0] == WETH
            assert opp.route.path[-1] == WETH

    def test_amount_in_stored_correctly(self):
        detector = self._triangle_detector(weth_usdc_r0=1, weth_usdc_r1=100)
        amount = 10**17
        opps = detector.find_circular_arbitrage(WETH, amount, gas_price_gwei=0)
        for opp in opps:
            assert opp.amount_in == amount


# ── 3. Circular arbitrage edge cases ──────────────────────────────────────────


class TestFindCircularArbitrageEdgeCases:
    def test_no_opportunity_when_balanced(self):
        """Perfectly balanced pools → no circular arb."""
        pool_ab = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_bc = _pair(ADDR_BC, DAI, USDC, 2000 * 10**18, 2000 * 10**18)
        pool_ca = _pair(ADDR_CA, USDC, WETH, 2000 * 10**18, 10**18)
        detector = ArbitrageDetector([pool_ab, pool_bc, pool_ca])
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        assert opps == []

    def test_no_opportunity_with_single_pool(self):
        """One pool cannot form a cycle back to origin."""
        pool = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        detector = ArbitrageDetector([pool])
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        assert opps == []

    def test_max_hops_limits_search(self):
        """With max_hops=1, a 3-hop cycle is not found."""
        pool_ab = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_bc = _pair(ADDR_BC, DAI, USDC, 2000 * 10**18, 2000 * 10**18)
        pool_ca = _pair(ADDR_CA, USDC, WETH, 1 * 10**18, 100 * 10**18)
        detector = ArbitrageDetector([pool_ab, pool_bc, pool_ca])
        opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0, max_hops=1)
        assert opps == []

    def test_unknown_token_returns_empty(self):
        other = Token(
            address=Address("0xdAC17F958D2ee523a2206206994597C13D831ec7"), symbol="USDT", decimals=6
        )
        pool = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        detector = ArbitrageDetector([pool])
        opps = detector.find_circular_arbitrage(other, 10**18, gas_price_gwei=0)
        assert opps == []


# ── 4. find_best_circular_arbitrage ──────────────────────────────────────────


class TestFindBestCircularArbitrage:
    def test_returns_none_when_no_opportunity(self):
        pool = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        detector = ArbitrageDetector([pool])
        result = detector.find_best_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        assert result is None

    def test_returns_highest_profit_opportunity(self):
        pool_ab = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_bc = _pair(ADDR_BC, DAI, USDC, 2000 * 10**18, 2000 * 10**18)
        pool_ca = _pair(ADDR_CA, USDC, WETH, 1 * 10**18, 100 * 10**18)
        detector = ArbitrageDetector([pool_ab, pool_bc, pool_ca])
        best = detector.find_best_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        all_opps = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0)
        if all_opps:
            assert best is not None
            assert best.gross_profit == max(o.gross_profit for o in all_opps)


# ── 5. find_cross_pool_arbitrage — profitable ────────────────────────────────


class TestFindCrossPoolArbitrageProfitable:
    """
    Two WETH/DAI pools with different prices:
      pool_cheap: 1 WETH = 1000 DAI  (WETH is cheap here)
      pool_dear:  1 WETH = 3000 DAI  (WETH is expensive here → sell DAI, buy WETH)
    """

    def _detector(self) -> ArbitrageDetector:
        pool_cheap = _pair(ADDR_AB, WETH, DAI, 10 * 10**18, 10_000 * 10**18)
        pool_dear = _pair(ADDR_ALT, WETH, DAI, 10 * 10**18, 30_000 * 10**18)
        return ArbitrageDetector([pool_cheap, pool_dear])

    def test_finds_opportunity(self):
        detector = self._detector()
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        assert len(opps) > 0

    def test_all_gross_positive(self):
        detector = self._detector()
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        assert all(o.gross_profit > 0 for o in opps)

    def test_strategy_label_is_cross_pool(self):
        detector = self._detector()
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        for opp in opps:
            assert opp.strategy == "cross_pool"

    def test_sorted_by_gross_profit_descending(self):
        detector = self._detector()
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        profits = [o.gross_profit for o in opps]
        assert profits == sorted(profits, reverse=True)

    def test_route_has_two_hops(self):
        detector = self._detector()
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        for opp in opps:
            assert opp.route.num_hops == 2


# ── 6. find_cross_pool_arbitrage — no opportunity ────────────────────────────


class TestFindCrossPoolArbitrageNoOpportunity:
    def test_balanced_pools_no_opportunity(self):
        """Identical pools → no arb (after fees, output < input)."""
        pool_a = _pair(ADDR_AB, WETH, DAI, 10 * 10**18, 20_000 * 10**18)
        pool_b = _pair(ADDR_ALT, WETH, DAI, 10 * 10**18, 20_000 * 10**18)
        detector = ArbitrageDetector([pool_a, pool_b])
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        assert opps == []

    def test_single_pool_no_opportunity(self):
        pool = _pair(ADDR_AB, WETH, DAI, 10 * 10**18, 20_000 * 10**18)
        detector = ArbitrageDetector([pool])
        opps = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        assert opps == []

    def test_unrelated_pools_no_opportunity(self):
        pool_weth_dai = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_weth_usdc = _pair(ADDR_ALT, WETH, USDC, 10**18, 2000 * 10**18)
        detector = ArbitrageDetector([pool_weth_dai, pool_weth_usdc])
        # Asking for DAI↔USDC cross-pool — neither pool has this pair
        opps = detector.find_cross_pool_arbitrage(DAI, USDC, 10**18, gas_price_gwei=0)
        assert opps == []


# ── 7. Direction symmetry ─────────────────────────────────────────────────────


class TestCrossPoolDirectionSymmetry:
    def test_both_directions_explored(self):
        """
        pool_cheap has cheap WETH (1 WETH = 1000 DAI).
        pool_dear  has expensive WETH (1 WETH = 3000 DAI).
        Buying WETH on pool_cheap and selling on pool_dear must appear.
        """
        pool_cheap = _pair(ADDR_AB, WETH, DAI, 10 * 10**18, 10_000 * 10**18)
        pool_dear = _pair(ADDR_ALT, WETH, DAI, 10 * 10**18, 30_000 * 10**18)
        detector = ArbitrageDetector([pool_cheap, pool_dear])

        # token_in=WETH: spend WETH, get DAI on one pool, sell DAI back to WETH
        opps_weth = detector.find_cross_pool_arbitrage(WETH, DAI, 10**18, gas_price_gwei=0)
        # token_in=DAI: spend DAI, get WETH on one pool, sell WETH back to DAI
        opps_dai = detector.find_cross_pool_arbitrage(DAI, WETH, 2000 * 10**18, gas_price_gwei=0)

        # At least one direction should yield an opportunity given the imbalance
        assert len(opps_weth) > 0 or len(opps_dai) > 0


# ── 8. Additional edge cases ──────────────────────────────────────────────────


class TestAdditionalEdgeCases:
    def test_cross_pool_pair_value_error_returns_none(self):
        """
        _cross_pool_pair catches ValueError from get_amount_out (wrong token) and returns None.
        """
        pool_buy = _pair(ADDR_AB, WETH, DAI, 10 * 10**18, 20_000 * 10**18)
        # pool_sell only has WETH/USDC — asking for DAI as token_out raises ValueError
        pool_sell = _pair(ADDR_ALT, WETH, USDC, 10 * 10**18, 20_000 * 10**18)
        detector = ArbitrageDetector([pool_buy, pool_sell])

        result = detector._cross_pool_pair(
            pool_buy=pool_buy,
            pool_sell=pool_sell,
            token_in=WETH,
            token_out=DAI,
            amount_in=10**18,
            gas_cost=0,
        )
        assert result is None

    def test_dfs_circular_skips_revisited_non_origin_token(self):
        """
        DFS skips a neighbour that is already in the current path (non-origin).
        Exercises the `if neighbour in path_so_far: continue` guard.
        """
        pool_ab = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_ba = _pair(ADDR_BC, DAI, WETH, 2000 * 10**18, 10**18)
        detector = ArbitrageDetector([pool_ab, pool_ba])

        # With max_hops=3, DFS must handle cycle detection; just verify no crash.
        result = detector.find_circular_arbitrage(WETH, 10**17, gas_price_gwei=0, max_hops=3)
        assert isinstance(result, list)

    def test_dfs_circular_non_origin_cycle_skipped(self):
        """
        Explicitly triggers arbitrage.py:286 — 'if neighbour in path_so_far: continue'.

        Path WETH→DAI→USDC is built; from USDC there is a pool back to DAI (non-origin),
        so DFS must skip DAI (already in path_so_far) without crashing.
        """
        ADDR_D = Address("0x0000000000000000000000000000000000000005")
        pool_weth_dai = _pair(ADDR_AB, WETH, DAI, 10**18, 2000 * 10**18)
        pool_dai_usdc = _pair(ADDR_BC, DAI, USDC, 2000 * 10**18, 2000 * 10**6)
        # This pool connects USDC back to DAI — triggers line 286
        pool_usdc_dai = _pair(ADDR_CA, USDC, DAI, 2000 * 10**6, 2000 * 10**18)
        # Also add pool back to WETH so a circular arb could complete
        pool_usdc_weth = _pair(ADDR_D, USDC, WETH, 2000 * 10**6, 10**18)
        detector = ArbitrageDetector([pool_weth_dai, pool_dai_usdc, pool_usdc_dai, pool_usdc_weth])

        # max_hops=4 allows WETH→DAI→USDC→DAI which triggers line 286
        result = detector.find_circular_arbitrage(WETH, 10**16, gas_price_gwei=0, max_hops=4)
        assert isinstance(result, list)
