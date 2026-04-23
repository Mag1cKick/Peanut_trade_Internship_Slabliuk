"""Tests for strategy/fees.py"""

from __future__ import annotations

from decimal import Decimal

import pytest

from strategy.fees import FeeStructure


class TestFeeStructureDefaults:
    def test_default_values(self):
        f = FeeStructure()
        assert f.cex_taker_bps == Decimal("10")
        assert f.dex_swap_bps == Decimal("30")
        assert f.gas_cost_usd == Decimal("5")

    def test_custom_values(self):
        f = FeeStructure(cex_taker_bps=7.0, dex_swap_bps=25.0, gas_cost_usd=3.0)
        assert f.cex_taker_bps == Decimal("7")
        assert f.dex_swap_bps == Decimal("25")
        assert f.gas_cost_usd == Decimal("3")

    def test_decimal_inputs_accepted(self):
        f = FeeStructure(
            cex_taker_bps=Decimal("7.5"),
            dex_swap_bps=Decimal("25"),
            gas_cost_usd=Decimal("3"),
        )
        assert f.cex_taker_bps == Decimal("7.5")


class TestFeeStructureValidation:
    def test_negative_cex_fee_raises(self):
        with pytest.raises(ValueError, match="cex_taker_bps"):
            FeeStructure(cex_taker_bps=-1.0)

    def test_negative_dex_fee_raises(self):
        with pytest.raises(ValueError, match="dex_swap_bps"):
            FeeStructure(dex_swap_bps=-0.1)

    def test_negative_gas_raises(self):
        with pytest.raises(ValueError, match="gas_cost_usd"):
            FeeStructure(gas_cost_usd=-5.0)

    def test_zero_fees_allowed(self):
        f = FeeStructure(cex_taker_bps=0.0, dex_swap_bps=0.0, gas_cost_usd=0.0)
        assert f.total_fee_bps(1000.0) == Decimal("0")

    def test_zero_trade_value_raises_gas_bps(self):
        f = FeeStructure()
        with pytest.raises(ValueError, match="trade_value_usd"):
            f.gas_bps(0.0)

    def test_negative_trade_value_raises_gas_bps(self):
        f = FeeStructure()
        with pytest.raises(ValueError, match="trade_value_usd"):
            f.gas_bps(-100.0)

    def test_zero_trade_value_raises_total_fee_bps(self):
        f = FeeStructure()
        with pytest.raises(ValueError):
            f.total_fee_bps(0.0)

    def test_zero_trade_value_raises_breakeven(self):
        f = FeeStructure()
        with pytest.raises(ValueError):
            f.breakeven_spread_bps(0.0)

    def test_zero_trade_value_raises_net_profit(self):
        f = FeeStructure()
        with pytest.raises(ValueError):
            f.net_profit_usd(50.0, 0.0)

    def test_zero_trade_value_raises_fee_usd(self):
        f = FeeStructure()
        with pytest.raises(ValueError):
            f.fee_usd(0.0)


class TestGasBps:
    def test_gas_bps_at_1000(self):
        # gas=$5, notional=$1000 → 50 bps
        f = FeeStructure(gas_cost_usd=5.0)
        assert f.gas_bps(1_000.0) == Decimal("50")

    def test_gas_bps_at_2000(self):
        # gas=$5, notional=$2000 → 25 bps
        f = FeeStructure(gas_cost_usd=5.0)
        assert f.gas_bps(2_000.0) == Decimal("25")

    def test_gas_bps_at_10000(self):
        # gas=$5, notional=$10000 → 5 bps
        f = FeeStructure(gas_cost_usd=5.0)
        assert f.gas_bps(10_000.0) == Decimal("5")

    def test_gas_bps_zero_gas(self):
        f = FeeStructure(gas_cost_usd=0.0)
        assert f.gas_bps(2_000.0) == Decimal("0")

    def test_gas_bps_decreases_with_size(self):
        f = FeeStructure(gas_cost_usd=5.0)
        assert f.gas_bps(1_000.0) > f.gas_bps(5_000.0) > f.gas_bps(10_000.0)

    def test_returns_decimal(self):
        f = FeeStructure()
        assert isinstance(f.gas_bps(2_000.0), Decimal)


class TestTotalFeeBps:
    def test_known_values(self):
        # cex=10, dex=30, gas=$5 on $2000 = 25 bps → total = 65 bps
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.total_fee_bps(2_000.0) == Decimal("65")

    def test_total_at_1000(self):
        # gas = 50 bps → total = 90 bps
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.total_fee_bps(1_000.0) == Decimal("90")

    def test_total_at_10000(self):
        # gas = 5 bps → total = 45 bps
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.total_fee_bps(10_000.0) == Decimal("45")

    def test_total_decreases_with_size(self):
        f = FeeStructure()
        assert f.total_fee_bps(1_000.0) > f.total_fee_bps(5_000.0)

    def test_no_gas_total(self):
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=0.0)
        assert f.total_fee_bps(999_999.0) == Decimal("40")

    def test_returns_decimal(self):
        f = FeeStructure()
        assert isinstance(f.total_fee_bps(2_000.0), Decimal)


class TestBreakevenSpread:
    def test_breakeven_equals_total_fee_bps(self):
        f = FeeStructure()
        for notional in [500.0, 1_000.0, 5_000.0, 10_000.0]:
            assert f.breakeven_spread_bps(notional) == f.total_fee_bps(notional)

    def test_breakeven_decreases_with_size(self):
        f = FeeStructure()
        assert f.breakeven_spread_bps(1_000.0) > f.breakeven_spread_bps(10_000.0)


class TestNetProfitUsd:
    def test_profitable_trade(self):
        # spread=100 bps, notional=$2000, fees=65 bps → gross=$20, fees=$13, net=$7
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.net_profit_usd(100.0, 2_000.0) == Decimal("7")

    def test_exactly_at_breakeven(self):
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        breakeven = f.breakeven_spread_bps(2_000.0)
        assert f.net_profit_usd(breakeven, 2_000.0) == Decimal("0")

    def test_below_breakeven_is_negative(self):
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.net_profit_usd(10.0, 2_000.0) < 0

    def test_zero_spread_is_loss(self):
        f = FeeStructure()
        assert f.net_profit_usd(0.0, 2_000.0) < 0

    def test_large_spread_profitable(self):
        f = FeeStructure()
        assert f.net_profit_usd(200.0, 5_000.0) > 0

    def test_net_scales_with_size(self):
        f = FeeStructure(gas_cost_usd=0.0)  # no gas to keep math clean
        # spread 100 bps fixed, fees 40 bps fixed → net = 60 bps of notional
        net_small = f.net_profit_usd(100.0, 1_000.0)
        net_large = f.net_profit_usd(100.0, 2_000.0)
        assert net_large == net_small * 2

    def test_returns_decimal(self):
        f = FeeStructure()
        assert isinstance(f.net_profit_usd(100.0, 2_000.0), Decimal)


class TestFeeUsd:
    def test_fee_usd_known(self):
        # total_fee = 65 bps on $2000 → $13
        f = FeeStructure(cex_taker_bps=10.0, dex_swap_bps=30.0, gas_cost_usd=5.0)
        assert f.fee_usd(2_000.0) == Decimal("13")

    def test_fee_usd_zero_fees(self):
        f = FeeStructure(cex_taker_bps=0.0, dex_swap_bps=0.0, gas_cost_usd=0.0)
        assert f.fee_usd(5_000.0) == Decimal("0")

    def test_fee_usd_consistency_with_net_profit(self):
        f = FeeStructure()
        notional = Decimal("3000")
        spread_bps = Decimal("80")
        gross = (spread_bps / Decimal("10000")) * notional
        expected_net = gross - f.fee_usd(notional)
        assert f.net_profit_usd(spread_bps, notional) == expected_net

    def test_returns_decimal(self):
        f = FeeStructure()
        assert isinstance(f.fee_usd(2_000.0), Decimal)
