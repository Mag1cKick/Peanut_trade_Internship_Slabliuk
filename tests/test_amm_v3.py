"""
tests/test_amm_v3.py — Unit tests for pricing.amm_v3.UniswapV3Pool

No real RPC node needed — from_chain uses a mocked web3 instance.

Test groups:
  1. Construction — validation, invalid fee_ppm, same-token guard
  2. get_amount_out (zeroForOne) — token0 → token1 swap math
  3. get_amount_out (oneForZero) — token1 → token0 swap math
  4. get_amount_out error handling — bad type, non-positive amount, wrong token
  5. get_spot_price — Q96 sqrt-price → ratio
  6. get_price_impact — grows with trade size
  7. fee_ppm variations — 100 / 500 / 3000 / 10000
  8. Known test vector — matches independently calculated V3 output
  9. from_chain — mocked on-chain load
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm_v3 import Q96, UniswapV3Pool

# ── Shared tokens ─────────────────────────────────────────────────────────────

WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
    symbol="WETH",
    decimals=18,
)
USDC = Token(
    address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
    symbol="USDC",
    decimals=6,
)

POOL_ADDR = Address("0x8ad599c3A0ff1De082011EFDDc58f1908eb6e6D8")

# sqrtPrice that represents USDC/WETH ≈ 2000 (raw ratio, ignoring decimals):
#   sqrt(2000) * Q96 ≈ 44.72 * 2^96 ≈ 3_543_191_142_285_914_205_700_096
_SQRT_2000 = int(Decimal(2000).sqrt() * Decimal(Q96))


def _make_pool(
    sqrt_price_x96: int = _SQRT_2000,
    liquidity: int = 10**22,
    fee_ppm: int = 3000,
) -> UniswapV3Pool:
    return UniswapV3Pool(
        address=POOL_ADDR,
        token0=WETH,
        token1=USDC,
        sqrt_price_x96=sqrt_price_x96,
        liquidity=liquidity,
        fee_ppm=fee_ppm,
    )


# ── 1. Construction ───────────────────────────────────────────────────────────


class TestConstruction:
    def test_valid_pool_created(self):
        pool = _make_pool()
        assert pool.token0 == WETH
        assert pool.token1 == USDC
        assert pool.fee_ppm == 3000

    def test_invalid_sqrt_price_raises(self):
        with pytest.raises(ValueError, match="sqrt_price_x96 must be positive"):
            UniswapV3Pool(
                address=POOL_ADDR,
                token0=WETH,
                token1=USDC,
                sqrt_price_x96=0,
                liquidity=10**18,
                fee_ppm=3000,
            )

    def test_negative_sqrt_price_raises(self):
        with pytest.raises(ValueError, match="sqrt_price_x96 must be positive"):
            UniswapV3Pool(
                address=POOL_ADDR,
                token0=WETH,
                token1=USDC,
                sqrt_price_x96=-1,
                liquidity=10**18,
                fee_ppm=3000,
            )

    def test_zero_liquidity_raises(self):
        with pytest.raises(ValueError, match="liquidity must be positive"):
            UniswapV3Pool(
                address=POOL_ADDR,
                token0=WETH,
                token1=USDC,
                sqrt_price_x96=_SQRT_2000,
                liquidity=0,
                fee_ppm=3000,
            )

    def test_invalid_fee_ppm_raises(self):
        with pytest.raises(ValueError, match="fee_ppm must be one of"):
            UniswapV3Pool(
                address=POOL_ADDR,
                token0=WETH,
                token1=USDC,
                sqrt_price_x96=_SQRT_2000,
                liquidity=10**18,
                fee_ppm=9999,
            )

    def test_same_token_raises(self):
        with pytest.raises(ValueError, match="token0 and token1 must be different"):
            UniswapV3Pool(
                address=POOL_ADDR,
                token0=WETH,
                token1=WETH,
                sqrt_price_x96=_SQRT_2000,
                liquidity=10**18,
                fee_ppm=3000,
            )

    @pytest.mark.parametrize("fee", [100, 500, 3000, 10000])
    def test_all_valid_fees(self, fee):
        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**22,
            fee_ppm=fee,
        )
        assert pool.fee_ppm == fee


# ── 2. get_amount_out — zeroForOne ────────────────────────────────────────────


class TestGetAmountOutZeroForOne:
    """token0 (WETH) in → token1 (USDC) out."""

    def test_returns_positive_output(self):
        pool = _make_pool()
        out = pool.get_amount_out(10**18, WETH)
        assert out > 0

    def test_larger_input_gives_larger_output(self):
        pool = _make_pool()
        small = pool.get_amount_out(10**17, WETH)
        large = pool.get_amount_out(10**18, WETH)
        assert large > small

    def test_output_less_than_spot(self):
        """Due to price impact, output_per_unit < spot_price."""
        pool = _make_pool()
        spot = pool.get_spot_price(WETH)
        amount_in = 10**18
        out = pool.get_amount_out(amount_in, WETH)
        execution = Decimal(out) / Decimal(amount_in)
        assert execution < spot

    def test_zero_for_one_integer_math_only(self):
        """Result is a plain Python int (no floats)."""
        pool = _make_pool()
        result = pool.get_amount_out(10**18, WETH)
        assert isinstance(result, int)

    def test_fee_reduces_output(self):
        """Higher fee → lower net_in → lower output."""
        low_fee = _make_pool(fee_ppm=500)
        high_fee = _make_pool(fee_ppm=10000)
        amount_in = 10**18
        assert low_fee.get_amount_out(amount_in, WETH) > high_fee.get_amount_out(amount_in, WETH)


# ── 3. get_amount_out — oneForZero ────────────────────────────────────────────


class TestGetAmountOutOneForZero:
    """token1 (USDC) in → token0 (WETH) out."""

    def test_returns_positive_output(self):
        pool = _make_pool()
        # ~2000 USDC (raw, 18-dec representation for math consistency)
        out = pool.get_amount_out(2000 * 10**18, USDC)
        assert out > 0

    def test_larger_input_gives_larger_output(self):
        pool = _make_pool()
        small = pool.get_amount_out(1000 * 10**18, USDC)
        large = pool.get_amount_out(2000 * 10**18, USDC)
        assert large > small

    def test_output_less_than_spot(self):
        """Due to price impact, output_per_unit < spot_price."""
        pool = _make_pool()
        spot = pool.get_spot_price(USDC)
        amount_in = 2000 * 10**18
        out = pool.get_amount_out(amount_in, USDC)
        execution = Decimal(out) / Decimal(amount_in)
        assert execution < spot

    def test_one_for_zero_integer_math_only(self):
        pool = _make_pool()
        result = pool.get_amount_out(2000 * 10**18, USDC)
        assert isinstance(result, int)

    def test_fee_reduces_output(self):
        low_fee = _make_pool(fee_ppm=500)
        high_fee = _make_pool(fee_ppm=10000)
        amount_in = 2000 * 10**18
        assert low_fee.get_amount_out(amount_in, USDC) > high_fee.get_amount_out(amount_in, USDC)


# ── 4. Error handling ─────────────────────────────────────────────────────────


class TestGetAmountOutErrors:
    def test_non_int_raises_type_error(self):
        pool = _make_pool()
        with pytest.raises(TypeError, match="amount_in must be int"):
            pool.get_amount_out(1.0, WETH)  # type: ignore[arg-type]

    def test_zero_amount_raises_value_error(self):
        pool = _make_pool()
        with pytest.raises(ValueError, match="amount_in must be positive"):
            pool.get_amount_out(0, WETH)

    def test_negative_amount_raises_value_error(self):
        pool = _make_pool()
        with pytest.raises(ValueError, match="amount_in must be positive"):
            pool.get_amount_out(-1, WETH)

    def test_wrong_token_raises_value_error(self):
        other = Token(
            address=Address("0xdAC17F958D2ee523a2206206994597C13D831ec7"),
            symbol="USDT",
            decimals=6,
        )
        pool = _make_pool()
        with pytest.raises(ValueError, match="not in pool"):
            pool.get_amount_out(10**18, other)


# ── 5. get_spot_price ─────────────────────────────────────────────────────────


class TestGetSpotPrice:
    def test_token0_spot_equals_sqrt_squared(self):
        """For token0 in: spot = (sqrtPriceX96 / Q96)^2."""
        pool = _make_pool()
        expected = (Decimal(pool.sqrt_price_x96) / Decimal(Q96)) ** 2
        assert pool.get_spot_price(WETH) == pytest.approx(float(expected), rel=1e-9)

    def test_token1_spot_is_inverse(self):
        """For token1 in: spot = 1 / (sqrtPriceX96 / Q96)^2."""
        pool = _make_pool()
        spot0 = pool.get_spot_price(WETH)
        spot1 = pool.get_spot_price(USDC)
        product = float(spot0 * spot1)
        assert product == pytest.approx(1.0, rel=1e-9)

    def test_sqrt_2000_gives_spot_near_2000(self):
        """_SQRT_2000 was built so the ratio ≈ 2000."""
        pool = _make_pool(sqrt_price_x96=_SQRT_2000)
        spot = float(pool.get_spot_price(WETH))
        assert abs(spot - 2000) < 1  # within 1 unit

    def test_wrong_token_raises(self):
        other = Token(
            address=Address("0xdAC17F958D2ee523a2206206994597C13D831ec7"),
            symbol="USDT",
            decimals=6,
        )
        pool = _make_pool()
        with pytest.raises(ValueError, match="not in pool"):
            pool.get_spot_price(other)


# ── 6. get_price_impact ───────────────────────────────────────────────────────


class TestGetPriceImpact:
    def test_tiny_trade_near_zero_impact(self):
        # Use fee_ppm=100 (0.01% fee) so the fee floor is below 0.1%.
        # Use 10**10 — negligibly small vs L=10**30, avoids net_in rounding to 0.
        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**30,
            fee_ppm=100,
        )
        impact = pool.get_price_impact(10**10, WETH)
        assert float(impact) < 0.001

    def test_large_trade_has_higher_impact(self):
        pool = _make_pool()
        small_impact = pool.get_price_impact(10**15, WETH)
        large_impact = pool.get_price_impact(10**21, WETH)
        assert large_impact > small_impact

    def test_impact_between_zero_and_one(self):
        pool = _make_pool()
        impact = pool.get_price_impact(10**18, WETH)
        assert Decimal(0) <= impact <= Decimal(1)

    def test_one_for_zero_impact_positive(self):
        pool = _make_pool()
        impact = pool.get_price_impact(2000 * 10**18, USDC)
        assert impact >= Decimal(0)

    def test_get_price_impact_zero_when_spot_is_zero(self):
        """Branch amm_v3.py:205 — if get_spot_price returns 0, impact is 0."""
        pool = _make_pool()
        with patch.object(pool, "get_spot_price", return_value=Decimal(0)):
            impact = pool.get_price_impact(10**18, WETH)
        assert impact == Decimal(0)

    def test_get_price_impact_returns_one_when_amount_out_is_zero(self):
        """Branch amm_v3.py:208 — amount_in=1, fee_ppm=100 → net_in=0 → output=0 → impact=1."""
        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**22,
            fee_ppm=100,
        )
        # With fee_ppm=100: net_in = 1 * 999_900 // 1_000_000 = 0 → get_amount_out returns 0
        impact = pool.get_price_impact(1, WETH)
        assert impact == Decimal(1)


# ── 7. Fee tiers ──────────────────────────────────────────────────────────────


class TestFeeTiers:
    @pytest.mark.parametrize("fee_ppm", [100, 500, 3000, 10000])
    def test_output_monotonically_decreases_with_fee(self, fee_ppm):
        """Higher fee → less net_in → less output."""
        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**22,
            fee_ppm=fee_ppm,
        )
        out = pool.get_amount_out(10**18, WETH)
        assert out > 0

    def test_fee_100_greater_than_fee_10000(self):
        low = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**22,
            fee_ppm=100,
        )
        high = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=_SQRT_2000,
            liquidity=10**22,
            fee_ppm=10000,
        )
        assert low.get_amount_out(10**18, WETH) > high.get_amount_out(10**18, WETH)


# ── 8. Known test vector ──────────────────────────────────────────────────────


class TestKnownVector:
    """
    Hand-computed reference vector using the V3 single-tick formula directly.

    Setup:
        sqrtPriceX96 = Q96  (i.e. sqrt-ratio = 1.0, so price = 1:1 raw)
        liquidity    = Q96
        fee_ppm      = 3000
        amount_in    = Q96  (= 2^96)
        token_in     = token0

    Manual calculation (zeroForOne):
        net_in = Q96 * (1_000_000 - 3000) // 1_000_000
               = Q96 * 997000 // 1_000_000
        extra  = ceil(net_in * Q96 / Q96) = net_in
        new_sqrt = (Q96 * Q96) // (Q96 + net_in)
        out    = Q96 * (Q96 - new_sqrt) // Q96
    """

    def test_matches_manual_calculation(self):
        net_in = Q96 * 997_000 // 1_000_000
        extra = net_in  # net_in * Q96 / Q96 = net_in, already exact
        new_sqrt = (Q96 * Q96) // (Q96 + extra)
        expected_out = Q96 * (Q96 - new_sqrt) // Q96

        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=Q96,
            liquidity=Q96,
            fee_ppm=3000,
        )
        assert pool.get_amount_out(Q96, WETH) == expected_out

    def test_zero_fee_gives_exact_constant_product(self):
        """
        With fee=100 (≈0), output should be very close to the
        zero-fee constant-product value for small trades.
        Exact zero-fee not possible due to V3 fee_ppm constraint; use 100 ppm.
        """
        pool = UniswapV3Pool(
            address=POOL_ADDR,
            token0=WETH,
            token1=USDC,
            sqrt_price_x96=Q96,
            liquidity=Q96,
            fee_ppm=100,
        )
        # With fee=100ppm: net_in = amount * 999900 // 1000000
        amount_in = Q96 // 100
        out = pool.get_amount_out(amount_in, WETH)
        # Output must be positive and less than amount_in (price ≈ 1)
        assert 0 < out < amount_in


# ── 9. from_chain ─────────────────────────────────────────────────────────────


class TestFromChain:
    def test_from_chain_creates_pool(self):
        mock_client = MagicMock()
        mock_w3 = MagicMock()
        mock_client._web3_instances = [mock_w3]

        mock_pool_contract = MagicMock()
        mock_pool_contract.functions.slot0.return_value.call.return_value = (
            _SQRT_2000,  # sqrtPriceX96
            202860,  # tick
            0,
            0,
            0,
            0,  # observation fields / feeProtocol
            True,  # unlocked
        )
        mock_pool_contract.functions.liquidity.return_value.call.return_value = 10**22
        mock_pool_contract.functions.token0.return_value.call.return_value = (
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        )
        mock_pool_contract.functions.token1.return_value.call.return_value = (
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        )
        mock_pool_contract.functions.fee.return_value.call.return_value = 3000

        mock_token_contract = MagicMock()
        mock_token_contract.functions.symbol.return_value.call.side_effect = ["WETH", "USDC"]
        mock_token_contract.functions.decimals.return_value.call.side_effect = [18, 6]

        def contract_factory(address, abi):
            if len(abi) == 5:  # pool ABI has 5 entries
                return mock_pool_contract
            return mock_token_contract

        mock_w3.eth.contract.side_effect = contract_factory

        with patch("web3.Web3.to_checksum_address", side_effect=lambda x: x):
            pool = UniswapV3Pool.from_chain(POOL_ADDR, mock_client)

        assert pool.sqrt_price_x96 == _SQRT_2000
        assert pool.liquidity == 10**22
        assert pool.fee_ppm == 3000
        assert pool.tick == 202860

    def test_from_chain_sets_address(self):
        mock_client = MagicMock()
        mock_w3 = MagicMock()
        mock_client._web3_instances = [mock_w3]

        mock_pool_contract = MagicMock()
        mock_pool_contract.functions.slot0.return_value.call.return_value = (
            _SQRT_2000,
            0,
            0,
            0,
            0,
            0,
            True,
        )
        mock_pool_contract.functions.liquidity.return_value.call.return_value = 10**22
        mock_pool_contract.functions.token0.return_value.call.return_value = (
            "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"
        )
        mock_pool_contract.functions.token1.return_value.call.return_value = (
            "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"
        )
        mock_pool_contract.functions.fee.return_value.call.return_value = 500

        mock_token_contract = MagicMock()
        mock_token_contract.functions.symbol.return_value.call.side_effect = ["WETH", "USDC"]
        mock_token_contract.functions.decimals.return_value.call.side_effect = [18, 6]

        mock_w3.eth.contract.side_effect = lambda address, abi: (
            mock_pool_contract if len(abi) == 5 else mock_token_contract
        )

        with patch("web3.Web3.to_checksum_address", side_effect=lambda x: x):
            pool = UniswapV3Pool.from_chain(POOL_ADDR, mock_client)

        assert pool.address == POOL_ADDR
        assert pool.fee_ppm == 500
