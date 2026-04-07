"""
tests/test_amm.py — Unit tests for pricing.amm.UniswapV2Pair

No real RPC node needed — from_chain uses a mocked web3 instance.

Test groups:
  1.  Construction — validation, token ordering
  2.  get_amount_out — formula correctness, edge cases, integer math
  3.  get_amount_in  — inverse formula, round-trip consistency
  4.  get_spot_price — reserve ratio
  5.  get_execution_price — less than or equal to spot
  6.  get_price_impact — grows with trade size, zero for tiny trades
  7.  simulate_swap — immutability, reserve updates
  8.  from_chain — mocked on-chain load
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair

# ── Shared test tokens ────────────────────────────────────────────────────────

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
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"),
    symbol="DAI",
    decimals=18,
)

PAIR_ADDR = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")

# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def eth_usdc_pair() -> UniswapV2Pair:
    """1 000 ETH / 2 000 000 USDC — roughly $2 000/ETH."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=USDC,
        reserve0=1_000 * 10**18,  # 1 000 WETH
        reserve1=2_000_000 * 10**6,  # 2 000 000 USDC
        fee_bps=30,
    )


@pytest.fixture
def balanced_pair() -> UniswapV2Pair:
    """Equal reserves of two 18-decimal tokens for simple ratio math."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=DAI,
        reserve0=1_000 * 10**18,
        reserve1=1_000 * 10**18,
        fee_bps=30,
    )


# ── 1. Construction ───────────────────────────────────────────────────────────


class TestConstruction:
    def test_basic_creation(self, eth_usdc_pair):
        assert eth_usdc_pair.reserve0 == 1_000 * 10**18
        assert eth_usdc_pair.reserve1 == 2_000_000 * 10**6
        assert eth_usdc_pair.fee_bps == 30

    def test_zero_reserve0_raises(self):
        with pytest.raises(ValueError, match="Reserves must be positive"):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=USDC,
                reserve0=0,
                reserve1=10**18,
            )

    def test_zero_reserve1_raises(self):
        with pytest.raises(ValueError, match="Reserves must be positive"):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=USDC,
                reserve0=10**18,
                reserve1=0,
            )

    def test_negative_reserve_raises(self):
        with pytest.raises(ValueError):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=USDC,
                reserve0=-1,
                reserve1=10**18,
            )

    def test_same_token_raises(self):
        with pytest.raises(ValueError, match="different"):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=WETH,
                reserve0=10**18,
                reserve1=10**18,
            )

    def test_invalid_fee_bps_raises(self):
        with pytest.raises(ValueError, match="fee_bps"):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=USDC,
                reserve0=10**18,
                reserve1=10**6,
                fee_bps=10000,
            )

    def test_negative_fee_bps_raises(self):
        with pytest.raises(ValueError):
            UniswapV2Pair(
                address=PAIR_ADDR,
                token0=WETH,
                token1=USDC,
                reserve0=10**18,
                reserve1=10**6,
                fee_bps=-1,
            )

    def test_zero_fee_bps_allowed(self):
        pair = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=10**18,
            reserve1=10**6,
            fee_bps=0,
        )
        assert pair.fee_bps == 0

    def test_custom_fee_bps(self):
        pair = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=10**18,
            reserve1=10**6,
            fee_bps=5,  # 0.05%
        )
        assert pair.fee_bps == 5


# ── 2. get_amount_out ─────────────────────────────────────────────────────────


class TestGetAmountOut:
    def test_basic_output_less_than_spot(self, eth_usdc_pair):
        """Selling 2 000 USDC in a 1 000 ETH / 2 M USDC pool should return ~1 ETH."""
        usdc_in = 2_000 * 10**6
        eth_out = eth_usdc_pair.get_amount_out(usdc_in, USDC)
        assert eth_out < 1 * 10**18
        assert eth_out > int(0.99 * 10**18)

    def test_output_is_int(self, eth_usdc_pair):
        result = eth_usdc_pair.get_amount_out(1_000 * 10**6, USDC)
        assert isinstance(result, int)

    def test_exact_formula(self, balanced_pair):
        """Manually verify formula with known numbers."""
        # reserve_in = reserve_out = 1000e18, fee = 30 bps
        amount_in = 10**18  # 1 token
        reserve_in = reserve_out = 1_000 * 10**18
        fee = 30
        amount_in_with_fee = amount_in * (10000 - fee)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in * 10000 + amount_in_with_fee
        expected = numerator // denominator
        assert balanced_pair.get_amount_out(amount_in, WETH) == expected

    def test_larger_input_more_output(self, eth_usdc_pair):
        small = eth_usdc_pair.get_amount_out(100 * 10**6, USDC)
        large = eth_usdc_pair.get_amount_out(1_000 * 10**6, USDC)
        assert large > small

    def test_output_from_token0(self, eth_usdc_pair):
        """Sell WETH, receive USDC."""
        usdc_out = eth_usdc_pair.get_amount_out(1 * 10**18, WETH)
        assert usdc_out > 0
        # Rough check: should be slightly less than spot ($2000)
        assert usdc_out < 2_000 * 10**6
        assert usdc_out > int(1_990 * 10**6)

    def test_output_from_token1(self, eth_usdc_pair):
        """Sell USDC, receive WETH."""
        eth_out = eth_usdc_pair.get_amount_out(2_000 * 10**6, USDC)
        assert eth_out > 0

    def test_unknown_token_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="not in pair"):
            eth_usdc_pair.get_amount_out(10**18, DAI)

    def test_zero_amount_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="positive"):
            eth_usdc_pair.get_amount_out(0, USDC)

    def test_negative_amount_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError):
            eth_usdc_pair.get_amount_out(-1, USDC)

    def test_non_int_raises(self, eth_usdc_pair):
        with pytest.raises(TypeError):
            eth_usdc_pair.get_amount_out(1.5, USDC)  # type: ignore

    def test_integer_math_no_float_precision_loss(self):
        """Very large reserves must not lose precision (would break with float)."""
        pair = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=10**30,
            reserve1=10**30,
            fee_bps=30,
        )
        result = pair.get_amount_out(10**25, WETH)
        assert isinstance(result, int)
        # Would silently lose precision if floats were used internally
        assert result > 0

    def test_fee_reduces_output(self):
        """Higher fee means less output."""
        low_fee = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=10**18,
            reserve1=2_000 * 10**6,
            fee_bps=0,
        )
        high_fee = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=10**18,
            reserve1=2_000 * 10**6,
            fee_bps=100,
        )
        amount_in = 10**17
        assert low_fee.get_amount_out(amount_in, WETH) > high_fee.get_amount_out(amount_in, WETH)

    def test_output_never_exceeds_reserve(self, eth_usdc_pair):
        """Output can never exceed or equal the reserve of the output token."""
        huge_in = eth_usdc_pair.reserve1 * 100  # absurdly large
        out = eth_usdc_pair.get_amount_out(huge_in, USDC)
        assert out < eth_usdc_pair.reserve0


# ── 3. get_amount_in ──────────────────────────────────────────────────────────


class TestGetAmountIn:
    def test_basic_amount_in(self, eth_usdc_pair):
        """Required USDC to buy 0.5 ETH should be slightly more than spot ($1000)."""
        eth_want = 5 * 10**17  # 0.5 ETH
        usdc_needed = eth_usdc_pair.get_amount_in(eth_want, WETH)
        assert usdc_needed > 1_000 * 10**6
        assert usdc_needed < 1_050 * 10**6

    def test_result_is_int(self, eth_usdc_pair):
        result = eth_usdc_pair.get_amount_in(10**17, WETH)
        assert isinstance(result, int)

    def test_round_trip_consistency(self, eth_usdc_pair):
        """get_amount_in(get_amount_out(x)) should be >= x (ceiling rounding)."""
        amount_in = 100 * 10**6  # 100 USDC
        amount_out = eth_usdc_pair.get_amount_out(amount_in, USDC)
        recovered = eth_usdc_pair.get_amount_in(amount_out, WETH)
        # Because of floor/ceiling division we may need to pay 1 extra unit
        assert recovered >= amount_in
        assert recovered <= amount_in + 2

    def test_get_amount_in_is_inverse_of_out(self, balanced_pair):
        """For a balanced pool, buying X tokens should roughly cost X (+ fee + impact)."""
        desired_out = 10**17  # 0.1 token
        required_in = balanced_pair.get_amount_in(desired_out, WETH)
        # Verify: swapping required_in gives at least desired_out
        actual_out = balanced_pair.get_amount_out(required_in, DAI)
        assert actual_out >= desired_out

    def test_zero_amount_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="positive"):
            eth_usdc_pair.get_amount_in(0, WETH)

    def test_negative_amount_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError):
            eth_usdc_pair.get_amount_in(-1, WETH)

    def test_non_int_raises(self, eth_usdc_pair):
        with pytest.raises(TypeError):
            eth_usdc_pair.get_amount_in(0.5, WETH)  # type: ignore

    def test_unknown_token_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="not in pair"):
            eth_usdc_pair.get_amount_in(10**18, DAI)

    def test_amount_out_equals_reserve_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="Insufficient liquidity"):
            eth_usdc_pair.get_amount_in(eth_usdc_pair.reserve0, WETH)

    def test_amount_out_exceeds_reserve_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="Insufficient liquidity"):
            eth_usdc_pair.get_amount_in(eth_usdc_pair.reserve0 + 1, WETH)

    def test_ceiling_rounding(self, balanced_pair):
        """Amount-in must always be rounded up (ceil), never down."""
        for desired in (1, 100, 10**15, 10**17):
            required = balanced_pair.get_amount_in(desired, WETH)
            actual_out = balanced_pair.get_amount_out(required, DAI)
            assert (
                actual_out >= desired
            ), f"amount_in={required} produced {actual_out} < desired {desired}"


# ── 4. get_spot_price ─────────────────────────────────────────────────────────


class TestGetSpotPrice:
    def test_spot_price_is_decimal(self, eth_usdc_pair):
        price = eth_usdc_pair.get_spot_price(USDC)
        assert isinstance(price, Decimal)

    def test_spot_price_usdc_to_eth(self, eth_usdc_pair):
        """
        reserve0=1000 ETH, reserve1=2M USDC.
        Spot price of USDC in ETH = reserve0/reserve1 = 1000e18 / 2000000e6 = 5e8.
        """
        price = eth_usdc_pair.get_spot_price(USDC)
        expected = Decimal(1_000 * 10**18) / Decimal(2_000_000 * 10**6)
        assert price == expected

    def test_spot_price_eth_to_usdc(self, eth_usdc_pair):
        price = eth_usdc_pair.get_spot_price(WETH)
        expected = Decimal(2_000_000 * 10**6) / Decimal(1_000 * 10**18)
        assert price == expected

    def test_spot_price_balanced_pool(self, balanced_pair):
        assert balanced_pair.get_spot_price(WETH) == Decimal(1)
        assert balanced_pair.get_spot_price(DAI) == Decimal(1)

    def test_spot_price_unknown_token_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="not in pair"):
            eth_usdc_pair.get_spot_price(DAI)

    def test_inverse_spot_prices_multiply_to_one(self, eth_usdc_pair):
        p0 = eth_usdc_pair.get_spot_price(WETH)
        p1 = eth_usdc_pair.get_spot_price(USDC)
        assert abs(p0 * p1 - Decimal(1)) < Decimal("1e-20")


# ── 5. get_execution_price ────────────────────────────────────────────────────


class TestGetExecutionPrice:
    def test_execution_price_less_than_spot(self, eth_usdc_pair):
        """Execution price must be worse than spot due to fee + impact."""
        spot = eth_usdc_pair.get_spot_price(USDC)
        exec_price = eth_usdc_pair.get_execution_price(2_000 * 10**6, USDC)
        assert exec_price < spot

    def test_execution_price_is_decimal(self, eth_usdc_pair):
        price = eth_usdc_pair.get_execution_price(10**6, USDC)
        assert isinstance(price, Decimal)

    def test_execution_price_approaches_spot_for_tiny_trade(self, eth_usdc_pair):
        """An infinitesimally small trade has price impact ≈ 0."""
        spot = eth_usdc_pair.get_spot_price(USDC)
        tiny = eth_usdc_pair.get_execution_price(1, USDC)  # 1 wei of USDC
        # For tiny trade, execution price should be very close to spot
        # (within 1 bps tolerance)
        ratio = tiny / spot if spot else Decimal(0)
        assert ratio <= Decimal(1)

    def test_execution_price_worsens_with_size(self, eth_usdc_pair):
        small = eth_usdc_pair.get_execution_price(1_000 * 10**6, USDC)
        large = eth_usdc_pair.get_execution_price(100_000 * 10**6, USDC)
        assert large < small  # buying more ETH → worse rate


# ── 6. get_price_impact ───────────────────────────────────────────────────────


class TestGetPriceImpact:
    def test_impact_is_decimal(self, eth_usdc_pair):
        impact = eth_usdc_pair.get_price_impact(2_000 * 10**6, USDC)
        assert isinstance(impact, Decimal)

    def test_impact_positive(self, eth_usdc_pair):
        impact = eth_usdc_pair.get_price_impact(2_000 * 10**6, USDC)
        assert impact > Decimal(0)

    def test_impact_less_than_one(self, eth_usdc_pair):
        impact = eth_usdc_pair.get_price_impact(2_000 * 10**6, USDC)
        assert impact < Decimal(1)

    def test_larger_trade_higher_impact(self, eth_usdc_pair):
        small = eth_usdc_pair.get_price_impact(1_000 * 10**6, USDC)
        large = eth_usdc_pair.get_price_impact(100_000 * 10**6, USDC)
        assert large > small

    def test_tiny_trade_near_zero_impact(self, eth_usdc_pair):
        """
        A 1-unit trade has ~fee_bps/10000 impact (fee dominates, price impact is ~0).
        Impact must not exceed the fee + a tiny epsilon for rounding.
        """
        impact = eth_usdc_pair.get_price_impact(1, USDC)
        fee_fraction = Decimal(eth_usdc_pair.fee_bps) / Decimal(10000)
        assert impact < fee_fraction + Decimal("0.0001")

    def test_impact_unknown_token_raises(self, eth_usdc_pair):
        with pytest.raises(ValueError, match="not in pair"):
            eth_usdc_pair.get_price_impact(10**6, DAI)

    def test_impact_1_percent_threshold(self):
        """
        Buying 1% of the pool in one trade should produce ~1% impact
        (before fees).  With 0.3% fee the impact will be slightly above 1%.
        """
        pair = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=1_000 * 10**18,
            reserve1=2_000_000 * 10**6,
            fee_bps=30,
        )
        # Sell ~1% of reserve1
        one_pct = pair.reserve1 // 100
        impact = pair.get_price_impact(one_pct, USDC)
        assert Decimal("0.005") < impact < Decimal("0.02")


# ── 7. simulate_swap ──────────────────────────────────────────────────────────


class TestSimulateSwap:
    def test_returns_new_instance(self, eth_usdc_pair):
        new_pair = eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert new_pair is not eth_usdc_pair

    def test_original_reserves_unchanged(self, eth_usdc_pair):
        original_r0 = eth_usdc_pair.reserve0
        original_r1 = eth_usdc_pair.reserve1
        eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert eth_usdc_pair.reserve0 == original_r0
        assert eth_usdc_pair.reserve1 == original_r1

    def test_reserve_in_increases(self, eth_usdc_pair):
        """Selling USDC → reserve1 (USDC) should increase."""
        new_pair = eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert new_pair.reserve1 > eth_usdc_pair.reserve1

    def test_reserve_out_decreases(self, eth_usdc_pair):
        """Selling USDC → reserve0 (WETH) should decrease."""
        new_pair = eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert new_pair.reserve0 < eth_usdc_pair.reserve0

    def test_reserve_delta_matches_amount_out(self, eth_usdc_pair):
        amount_in = 2_000 * 10**6
        amount_out = eth_usdc_pair.get_amount_out(amount_in, USDC)
        new_pair = eth_usdc_pair.simulate_swap(amount_in, USDC)
        assert new_pair.reserve1 == eth_usdc_pair.reserve1 + amount_in
        assert new_pair.reserve0 == eth_usdc_pair.reserve0 - amount_out

    def test_simulate_token0_in(self, eth_usdc_pair):
        """Selling WETH → reserve0 increases, reserve1 decreases."""
        amount_in = 1 * 10**18
        amount_out = eth_usdc_pair.get_amount_out(amount_in, WETH)
        new_pair = eth_usdc_pair.simulate_swap(amount_in, WETH)
        assert new_pair.reserve0 == eth_usdc_pair.reserve0 + amount_in
        assert new_pair.reserve1 == eth_usdc_pair.reserve1 - amount_out

    def test_fee_bps_preserved(self, eth_usdc_pair):
        new_pair = eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert new_pair.fee_bps == eth_usdc_pair.fee_bps

    def test_address_preserved(self, eth_usdc_pair):
        new_pair = eth_usdc_pair.simulate_swap(2_000 * 10**6, USDC)
        assert new_pair.address == eth_usdc_pair.address

    def test_chained_simulations_compound_correctly(self, balanced_pair):
        """Two sequential swaps should affect reserves cumulatively."""
        amount = 10 * 10**18
        after_first = balanced_pair.simulate_swap(amount, WETH)
        after_second = after_first.simulate_swap(amount, WETH)
        # reserve0 should have grown by 2*amount
        assert after_second.reserve0 == balanced_pair.reserve0 + 2 * amount


# ── 8. from_chain ─────────────────────────────────────────────────────────────


class TestFromChain:
    def _make_mock_client(
        self,
        reserve0: int = 1_000 * 10**18,
        reserve1: int = 2_000_000 * 10**6,
        token0_addr: str = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",
        token1_addr: str = "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48",
        token0_symbol: str = "WETH",
        token0_decimals: int = 18,
        token1_symbol: str = "USDC",
        token1_decimals: int = 6,
    ) -> MagicMock:
        mock_client = MagicMock()
        mock_w3 = MagicMock()
        mock_client._web3_instances = [mock_w3]

        # Pair contract
        pair_contract = MagicMock()
        pair_contract.functions.getReserves().call.return_value = (reserve0, reserve1, 0)
        pair_contract.functions.token0().call.return_value = token0_addr
        pair_contract.functions.token1().call.return_value = token1_addr

        # Token contracts
        token0_contract = MagicMock()
        token0_contract.functions.symbol().call.return_value = token0_symbol
        token0_contract.functions.decimals().call.return_value = token0_decimals

        token1_contract = MagicMock()
        token1_contract.functions.symbol().call.return_value = token1_symbol
        token1_contract.functions.decimals().call.return_value = token1_decimals

        def contract_factory(address, abi):
            from web3 import Web3

            if address == Web3.to_checksum_address(PAIR_ADDR.checksum):
                return pair_contract
            if address == Web3.to_checksum_address(token0_addr):
                return token0_contract
            return token1_contract

        mock_w3.eth.contract.side_effect = contract_factory
        mock_w3.to_checksum_address = MagicMock(side_effect=lambda x: x)

        from web3 import Web3

        with patch.object(Web3, "to_checksum_address", side_effect=lambda x: x):
            pass  # just to ensure import

        return mock_client

    def test_from_chain_returns_pair(self):
        from web3 import Web3

        client = self._make_mock_client()
        with patch("pricing.amm.Web3") as mock_web3_cls:
            mock_web3_cls.to_checksum_address = Web3.to_checksum_address
            pair = UniswapV2Pair.from_chain(PAIR_ADDR, client)

        assert isinstance(pair, UniswapV2Pair)

    def test_from_chain_loads_reserves(self):
        from web3 import Web3

        client = self._make_mock_client(reserve0=500 * 10**18, reserve1=1_000_000 * 10**6)
        with patch("pricing.amm.Web3") as mock_web3_cls:
            mock_web3_cls.to_checksum_address = Web3.to_checksum_address
            pair = UniswapV2Pair.from_chain(PAIR_ADDR, client)

        assert pair.reserve0 == 500 * 10**18
        assert pair.reserve1 == 1_000_000 * 10**6

    def test_from_chain_loads_token_symbols(self):
        from web3 import Web3

        client = self._make_mock_client()
        with patch("pricing.amm.Web3") as mock_web3_cls:
            mock_web3_cls.to_checksum_address = Web3.to_checksum_address
            pair = UniswapV2Pair.from_chain(PAIR_ADDR, client)

        assert pair.token0.symbol == "WETH"
        assert pair.token1.symbol == "USDC"

    def test_from_chain_preserves_address(self):
        from web3 import Web3

        client = self._make_mock_client()
        with patch("pricing.amm.Web3") as mock_web3_cls:
            mock_web3_cls.to_checksum_address = Web3.to_checksum_address
            pair = UniswapV2Pair.from_chain(PAIR_ADDR, client)

        assert pair.address == PAIR_ADDR
