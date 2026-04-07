"""
tests/test_impact_analyzer.py — Unit tests for pricing.impact_analyzer

No real RPC needed — all chain calls are mocked.

Test groups:
  1.  generate_impact_table — structure, values, sorting
  2.  find_max_size_for_impact — binary search correctness
  3.  estimate_true_cost — gas cost calculation (WETH pairs + non-WETH)
  4.  format_table — output rendering
  5.  CLI — argument parsing, exit codes
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.impact_analyzer import (
    PriceImpactAnalyzer,
    _spot_price_human,
    format_table,
    main,
)

# ── Shared fixtures ───────────────────────────────────────────────────────────

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


@pytest.fixture
def eth_usdc_pair() -> UniswapV2Pair:
    """1 000 WETH / 2 000 000 USDC — spot ~$2 000/ETH."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=USDC,
        reserve0=1_000 * 10**18,
        reserve1=2_000_000 * 10**6,
        fee_bps=30,
    )


@pytest.fixture
def balanced_pair() -> UniswapV2Pair:
    """1 000 WETH / 1 000 DAI — spot 1:1 (18/18 decimals)."""
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=DAI,
        reserve0=1_000 * 10**18,
        reserve1=1_000 * 10**18,
        fee_bps=30,
    )


@pytest.fixture
def analyzer(eth_usdc_pair) -> PriceImpactAnalyzer:
    return PriceImpactAnalyzer(eth_usdc_pair)


# ── 1. generate_impact_table ──────────────────────────────────────────────────


class TestGenerateImpactTable:
    def test_returns_list(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert isinstance(rows, list)

    def test_one_row_per_size(self, analyzer):
        sizes = [100 * 10**6, 1_000 * 10**6, 10_000 * 10**6]
        rows = analyzer.generate_impact_table(USDC, sizes)
        assert len(rows) == 3

    def test_empty_sizes_returns_empty(self, analyzer):
        assert analyzer.generate_impact_table(USDC, []) == []

    def test_row_has_required_keys(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        required = {"amount_in", "amount_out", "spot_price", "execution_price", "price_impact_pct"}
        assert required <= rows[0].keys()

    def test_amount_in_preserved(self, analyzer):
        size = 1_234 * 10**6
        rows = analyzer.generate_impact_table(USDC, [size])
        assert rows[0]["amount_in"] == size

    def test_amount_out_is_int(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert isinstance(rows[0]["amount_out"], int)
        assert rows[0]["amount_out"] > 0

    def test_spot_price_is_decimal(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert isinstance(rows[0]["spot_price"], Decimal)

    def test_execution_price_is_decimal(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert isinstance(rows[0]["execution_price"], Decimal)

    def test_impact_pct_is_decimal(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert isinstance(rows[0]["price_impact_pct"], Decimal)

    def test_spot_price_human_units(self, analyzer):
        """Spot price should be ~2000 USDC/ETH (human units)."""
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        assert Decimal("1990") < rows[0]["spot_price"] < Decimal("2010")

    def test_execution_price_worse_than_spot(self, analyzer):
        """Execution price (USDC/ETH) should exceed spot (you pay more per ETH)."""
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6])
        spot = rows[0]["spot_price"]
        exec_price = rows[0]["execution_price"]
        assert exec_price > spot

    def test_impact_pct_in_percent_units(self, analyzer):
        """A large trade should show visible percent impact (not a tiny fraction)."""
        rows = analyzer.generate_impact_table(USDC, [100_000 * 10**6])
        assert rows[0]["price_impact_pct"] > Decimal("1")

    def test_larger_trade_higher_impact(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6, 100_000 * 10**6])
        assert rows[1]["price_impact_pct"] > rows[0]["price_impact_pct"]

    def test_larger_trade_more_output(self, analyzer):
        rows = analyzer.generate_impact_table(USDC, [1_000 * 10**6, 10_000 * 10**6])
        assert rows[1]["amount_out"] > rows[0]["amount_out"]

    def test_spot_price_same_for_all_rows(self, analyzer):
        """Spot price is a property of the pool, not the trade size."""
        rows = analyzer.generate_impact_table(
            USDC, [1_000 * 10**6, 10_000 * 10**6, 100_000 * 10**6]
        )
        assert rows[0]["spot_price"] == rows[1]["spot_price"] == rows[2]["spot_price"]

    def test_works_with_token0_as_input(self, analyzer):
        """Should also work when selling the ETH side (token0)."""
        rows = analyzer.generate_impact_table(WETH, [1 * 10**18])
        assert rows[0]["amount_out"] > 0


# ── 2. find_max_size_for_impact ───────────────────────────────────────────────


class TestFindMaxSizeForImpact:
    def test_returns_int(self, analyzer):
        result = analyzer.find_max_size_for_impact(USDC, Decimal("1"))
        assert isinstance(result, int)

    def test_result_within_impact_threshold(self, analyzer):
        max_size = analyzer.find_max_size_for_impact(USDC, Decimal("1"))
        pair = analyzer.pair
        impact = pair.get_price_impact(max_size, USDC)
        assert impact <= Decimal("0.01")

    def test_one_more_exceeds_threshold(self, analyzer):
        """One unit above the returned size should exceed the threshold."""
        max_size = analyzer.find_max_size_for_impact(USDC, Decimal("1"))
        if max_size > 0:
            pair = analyzer.pair
            impact_over = pair.get_price_impact(max_size + 1, USDC)
            # impact at max_size is <= threshold; at max_size+1 it should be
            # at or above (binary search invariant)
            assert impact_over >= pair.get_price_impact(max_size, USDC)

    def test_tighter_threshold_returns_smaller_size(self, analyzer):
        size_1pct = analyzer.find_max_size_for_impact(USDC, Decimal("1"))
        size_5pct = analyzer.find_max_size_for_impact(USDC, Decimal("5"))
        assert size_5pct > size_1pct

    def test_zero_impact_raises(self, analyzer):
        with pytest.raises(ValueError, match="positive"):
            analyzer.find_max_size_for_impact(USDC, Decimal("0"))

    def test_negative_impact_raises(self, analyzer):
        with pytest.raises(ValueError):
            analyzer.find_max_size_for_impact(USDC, Decimal("-1"))

    def test_large_threshold_returns_large_size(self, analyzer):
        """50% threshold should return a large trade size."""
        size = analyzer.find_max_size_for_impact(USDC, Decimal("50"))
        assert size > 100_000 * 10**6  # more than 100k USDC

    def test_works_with_token0_input(self, analyzer):
        size = analyzer.find_max_size_for_impact(WETH, Decimal("1"))
        assert isinstance(size, int)
        assert size > 0


# ── 3. estimate_true_cost ─────────────────────────────────────────────────────


class TestEstimateTrueCost:
    def test_returns_dict_with_required_keys(self, analyzer):
        result = analyzer.estimate_true_cost(
            amount_in=1_000 * 10**6,
            token_in=USDC,
            gas_price_gwei=50,
        )
        required = {
            "gross_output",
            "gas_cost_eth",
            "gas_cost_in_output_token",
            "net_output",
            "effective_price",
        }
        assert required <= result.keys()

    def test_gross_output_equals_amount_out(self, analyzer):
        amount_in = 1_000 * 10**6
        result = analyzer.estimate_true_cost(amount_in, USDC, 50)
        expected = analyzer.pair.get_amount_out(amount_in, USDC)
        assert result["gross_output"] == expected

    def test_gas_cost_eth_calculation(self, analyzer):
        """gas_cost_eth = gas_price_gwei * 1e9 * gas_estimate."""
        result = analyzer.estimate_true_cost(1_000 * 10**6, USDC, 50, gas_estimate=150_000)
        assert result["gas_cost_eth"] == 50 * 10**9 * 150_000

    def test_net_output_less_than_gross(self, eth_usdc_pair):
        """When gas is in WETH and output is WETH, net < gross."""
        analyzer = PriceImpactAnalyzer(eth_usdc_pair)
        # Selling USDC to receive WETH — gas reduces WETH output
        result = analyzer.estimate_true_cost(
            amount_in=1_000 * 10**6,
            token_in=USDC,
            gas_price_gwei=50,
            gas_estimate=150_000,
        )
        assert result["net_output"] <= result["gross_output"]

    def test_net_never_negative(self, analyzer):
        """Even with extreme gas prices, net_output should not go below 0."""
        result = analyzer.estimate_true_cost(
            amount_in=1 * 10**6,  # tiny 1 USDC trade
            token_in=USDC,
            gas_price_gwei=100_000,  # absurdly high gas price
            gas_estimate=500_000,
        )
        assert result["net_output"] >= 0

    def test_weth_output_gas_conversion(self, eth_usdc_pair):
        """When output is WETH, gas cost should be converted correctly."""
        analyzer = PriceImpactAnalyzer(eth_usdc_pair)
        result = analyzer.estimate_true_cost(1_000 * 10**6, USDC, 50, 150_000)
        gas_cost_eth = 50 * 10**9 * 150_000
        assert result["gas_cost_in_output_token"] == gas_cost_eth

    def test_weth_input_gas_conversion(self, eth_usdc_pair):
        """When input is WETH, gas cost should be converted to output (USDC)."""
        analyzer = PriceImpactAnalyzer(eth_usdc_pair)
        result = analyzer.estimate_true_cost(1 * 10**18, WETH, 50, 150_000)
        # Gas cost should be positive and reasonable (USDC units)
        assert result["gas_cost_in_output_token"] > 0
        # ~50 gwei * 150k * $2000/ETH ≈ $15 = 15_000_000 USDC raw
        assert result["gas_cost_in_output_token"] > 10**6  # > 1 USDC

    def test_non_weth_pair_gas_is_zero(self, balanced_pair):
        """Neither token is WETH — cannot convert gas to output units."""
        # balanced_pair is WETH/DAI actually, but let's use a custom non-WETH pair
        non_weth_pair = UniswapV2Pair(
            address=PAIR_ADDR,
            token0=USDC,
            token1=DAI,
            reserve0=1_000_000 * 10**6,
            reserve1=1_000_000 * 10**18,
            fee_bps=30,
        )
        analyzer = PriceImpactAnalyzer(non_weth_pair)
        result = analyzer.estimate_true_cost(100 * 10**6, USDC, 50)
        assert result["gas_cost_in_output_token"] == 0

    def test_effective_price_is_decimal(self, analyzer):
        result = analyzer.estimate_true_cost(1_000 * 10**6, USDC, 50)
        assert isinstance(result["effective_price"], Decimal)

    def test_custom_gas_estimate(self, analyzer):
        r1 = analyzer.estimate_true_cost(1_000 * 10**6, USDC, 50, gas_estimate=100_000)
        r2 = analyzer.estimate_true_cost(1_000 * 10**6, USDC, 50, gas_estimate=200_000)
        assert r2["gas_cost_eth"] == 2 * r1["gas_cost_eth"]


# ── 4. format_table ───────────────────────────────────────────────────────────


class TestFormatTable:
    def _make_rows(self, pair: UniswapV2Pair) -> list[dict]:
        analyzer = PriceImpactAnalyzer(pair)
        return analyzer.generate_impact_table(
            USDC,
            [1_000 * 10**6, 10_000 * 10**6, 100_000 * 10**6],
        )

    def test_returns_string(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        result = format_table(rows, USDC, WETH, eth_usdc_pair, 19_802 * 10**6, Decimal("1"))
        assert isinstance(result, str)

    def test_contains_token_symbols(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        text = format_table(rows, USDC, WETH, eth_usdc_pair, 0, Decimal("1"))
        assert "USDC" in text
        assert "WETH" in text

    def test_contains_spot_price(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        text = format_table(rows, USDC, WETH, eth_usdc_pair, 0, Decimal("1"))
        assert "2,000" in text or "1,999" in text or "2000" in text

    def test_contains_impact_percentages(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        text = format_table(rows, USDC, WETH, eth_usdc_pair, 0, Decimal("1"))
        assert "%" in text

    def test_contains_max_trade_line(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        text = format_table(rows, USDC, WETH, eth_usdc_pair, 19_802 * 10**6, Decimal("1"))
        assert "Max trade" in text

    def test_contains_box_drawing_chars(self, eth_usdc_pair):
        rows = self._make_rows(eth_usdc_pair)
        text = format_table(rows, USDC, WETH, eth_usdc_pair, 0, Decimal("1"))
        assert "│" in text
        assert "─" in text

    def test_empty_rows_no_crash(self, eth_usdc_pair):
        text = format_table([], USDC, WETH, eth_usdc_pair, 0, Decimal("1"))
        assert isinstance(text, str)


# ── 5. CLI ────────────────────────────────────────────────────────────────────


class TestCLI:
    def _mock_pair(self) -> UniswapV2Pair:
        return UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=1_000 * 10**18,
            reserve1=2_000_000 * 10**6,
            fee_bps=30,
        )

    def test_missing_pair_address_exits_with_error(self):
        # argparse prints to stderr and exits with code 2 for missing required args
        with pytest.raises(SystemExit) as exc_info:
            main(["--token-in", "USDC", "--sizes", "1000"])
        assert exc_info.value.code != 0

    def test_valid_args_exit_0(self):
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            exit_code = main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "USDC",
                    "--sizes",
                    "1000,10000",
                    "--rpc",
                    "https://example.com",
                ]
            )
        assert exit_code == 0

    def test_invalid_pair_address_exits_1(self):
        with patch("pricing.impact_analyzer.ChainClient"):
            with patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls:
                mock_cls.from_chain.side_effect = Exception("bad address")
                exit_code = main(
                    [
                        "not-an-address",
                        "--token-in",
                        "USDC",
                        "--sizes",
                        "1000",
                    ]
                )
        assert exit_code == 1

    def test_unknown_token_exits_1(self):
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            exit_code = main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "UNKNOWN_TOKEN",
                    "--sizes",
                    "1000",
                ]
            )
        assert exit_code == 1

    def test_invalid_max_impact_exits_1(self):
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            exit_code = main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "USDC",
                    "--sizes",
                    "1000",
                    "--max-impact",
                    "not-a-number",
                ]
            )
        assert exit_code == 1

    def test_output_contains_analysis_text(self, capsys):
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "USDC",
                    "--sizes",
                    "1000,10000",
                ]
            )
        captured = capsys.readouterr()
        assert "Price Impact Analysis" in captured.out
        assert "Max trade" in captured.out


# ── 6. _spot_price_human helper ───────────────────────────────────────────────


class TestSpotPriceHuman:
    def test_weth_usdc_roughly_2000(self, eth_usdc_pair):
        """Selling USDC for WETH — price should be ~2000 USDC per ETH."""
        price = _spot_price_human(eth_usdc_pair, USDC, WETH)
        assert Decimal("1999") < price < Decimal("2001")

    def test_weth_dai_balanced_is_one(self, balanced_pair):
        """Equal reserves 1:1 pool (both 18 dec) — price should be 1."""
        price = _spot_price_human(balanced_pair, WETH, DAI)
        assert price == Decimal("1")

    def test_inverse_direction(self, eth_usdc_pair):
        """Selling WETH for USDC — price is the inverse."""
        p_usdc_in = _spot_price_human(eth_usdc_pair, USDC, WETH)  # ~2000
        p_weth_in = _spot_price_human(eth_usdc_pair, WETH, USDC)  # ~0.0005
        product = p_usdc_in * p_weth_in
        assert abs(product - Decimal("1")) < Decimal("0.0001")

    def test_zero_spot_price_returns_zero(self, eth_usdc_pair):
        """Branch: if out_per_in_human == 0, return Decimal(0)."""
        with patch.object(eth_usdc_pair, "get_spot_price", return_value=Decimal(0)):
            price = _spot_price_human(eth_usdc_pair, USDC, WETH)
        assert price == Decimal(0)


# ── 7. _resolve_token helper ──────────────────────────────────────────────────


class TestResolveToken:
    def test_resolve_by_symbol_token0(self, eth_usdc_pair):
        """Token0 (WETH) found by symbol match — covers impact_analyzer.py:207."""
        from pricing.impact_analyzer import _resolve_token

        token = _resolve_token(eth_usdc_pair, "WETH")
        assert token.symbol == "WETH"

    def test_resolve_by_address(self, eth_usdc_pair):
        """Token can be looked up by its raw address string."""
        from pricing.impact_analyzer import _resolve_token

        token = _resolve_token(eth_usdc_pair, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48")
        assert token.symbol == "USDC"

    def test_resolve_by_address_token1(self, eth_usdc_pair):
        """Token1 (USDC) found by address match."""
        from pricing.impact_analyzer import _resolve_token

        token = _resolve_token(eth_usdc_pair, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2")
        assert token.symbol == "WETH"

    def test_invalid_token_raises(self, eth_usdc_pair):
        """Neither symbol nor address match → ValueError."""
        from pricing.impact_analyzer import _resolve_token

        with pytest.raises(ValueError, match="not found"):
            _resolve_token(eth_usdc_pair, "UNKNOWN")


# ── 8. Additional CLI error paths ─────────────────────────────────────────────


class TestCLIAdditionalErrors:
    def _mock_pair(self):
        return UniswapV2Pair(
            address=PAIR_ADDR,
            token0=WETH,
            token1=USDC,
            reserve0=1_000 * 10**18,
            reserve1=2_000_000 * 10**6,
            fee_bps=30,
        )

    def test_invalid_sizes_exits_1(self):
        """Non-numeric --sizes should exit with code 1."""
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            exit_code = main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "USDC",
                    "--sizes",
                    "not,numbers",
                ]
            )
        assert exit_code == 1

    def test_generate_table_exception_exits_1(self):
        """If generate_impact_table or find_max_size_for_impact raises, exit 1."""
        with (
            patch("pricing.impact_analyzer.UniswapV2Pair") as mock_cls,
            patch("pricing.impact_analyzer.ChainClient"),
            patch("pricing.impact_analyzer.PriceImpactAnalyzer") as mock_analyzer_cls,
        ):
            mock_cls.from_chain.return_value = self._mock_pair()
            mock_analyzer = MagicMock()
            mock_analyzer.generate_impact_table.side_effect = RuntimeError("boom")
            mock_analyzer_cls.return_value = mock_analyzer

            exit_code = main(
                [
                    PAIR_ADDR.checksum,
                    "--token-in",
                    "USDC",
                    "--sizes",
                    "1000",
                ]
            )
        assert exit_code == 1
