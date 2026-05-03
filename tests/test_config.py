"""Tests for config/settings.py — TradingRules and Config."""

from __future__ import annotations

import pytest

from config.settings import Config, TradingRules, get_trading_rules


class TestTradingRulesRounding:
    def setup_method(self):
        self.rules = TradingRules(
            symbol="ETH/USDC",
            min_qty=0.0001,
            max_qty=9000.0,
            step_size=0.0001,
            min_price=0.01,
            max_price=1_000_000.0,
            tick_size=0.01,
            min_notional=5.0,
        )

    def test_round_quantity_floors_to_step(self):
        # 0.12345 ETH → floor to 0.1234 (step=0.0001)
        assert self.rules.round_quantity(0.12345) == pytest.approx(0.1234)

    def test_round_quantity_exact_multiple_unchanged(self):
        assert self.rules.round_quantity(0.1000) == pytest.approx(0.1000)

    def test_round_quantity_never_exceeds_input(self):
        qty = 1.99999
        assert self.rules.round_quantity(qty) <= qty

    def test_round_price_rounds_to_tick(self):
        # $2314.256 → $2314.26 (tick=0.01)
        assert self.rules.round_price(2314.256) == pytest.approx(2314.26)

    def test_round_price_exact_tick_unchanged(self):
        assert self.rules.round_price(2314.50) == pytest.approx(2314.50)


class TestTradingRulesValidation:
    def setup_method(self):
        self.rules = TradingRules(
            symbol="ETH/USDC",
            min_qty=0.0001,
            max_qty=9000.0,
            step_size=0.0001,
            min_price=0.01,
            max_price=1_000_000.0,
            tick_size=0.01,
            min_notional=5.0,
        )

    def test_valid_order_passes(self):
        ok, reason = self.rules.validate(qty=0.01, price=2000.0)
        assert ok
        assert reason == ""

    def test_qty_below_min_fails(self):
        ok, reason = self.rules.validate(qty=0.00001, price=2000.0)
        assert not ok
        assert "min_qty" in reason

    def test_notional_below_min_fails(self):
        # 0.001 ETH × $2 = $0.002 < $5 min notional
        ok, reason = self.rules.validate(qty=0.001, price=2.0)
        assert not ok
        assert "notional" in reason

    def test_price_below_min_fails(self):
        ok, reason = self.rules.validate(qty=1.0, price=0.001)
        assert not ok
        assert "min_price" in reason

    def test_qty_above_max_fails(self):
        ok, reason = self.rules.validate(qty=10_000.0, price=2000.0)
        assert not ok
        assert "max_qty" in reason


class TestGetTradingRules:
    def test_returns_fallback_when_no_client(self):
        rules = get_trading_rules("ETH/USDC")
        assert isinstance(rules, TradingRules)
        assert rules.symbol == "ETH/USDC"
        assert rules.step_size > 0
        assert rules.tick_size > 0
        assert rules.min_notional > 0

    def test_returns_fallback_for_unknown_symbol(self):
        rules = get_trading_rules("UNKNOWN/PAIR")
        assert isinstance(rules, TradingRules)
        assert rules.symbol == "UNKNOWN/PAIR"

    def test_caches_result(self):
        r1 = get_trading_rules("ETH/USDT")
        r2 = get_trading_rules("ETH/USDT")
        assert r1 is r2  # same object from cache

    def test_returns_btc_fallback(self):
        rules = get_trading_rules("BTC/USDT")
        assert rules.step_size == pytest.approx(0.00001)


class TestConfig:
    def test_gas_cost_is_arbitrum_rate(self):
        assert Config.GAS_COST_USD <= 0.20  # Arbitrum: max $0.20

    def test_fee_structure_uses_config_defaults(self):
        fees = Config.to_fee_structure()
        assert float(fees.cex_taker_bps) == pytest.approx(Config.CEX_TAKER_BPS)
        assert float(fees.dex_swap_bps) == pytest.approx(Config.DEX_SWAP_BPS)
        assert float(fees.gas_cost_usd) == pytest.approx(Config.GAS_COST_USD)

    def test_signal_config_has_required_keys(self):
        cfg = Config.to_signal_config()
        assert "min_spread_bps" in cfg
        assert "min_profit_usd" in cfg
        assert "max_position_usd" in cfg

    def test_arbitrum_addresses_are_checksummed_format(self):
        # Basic sanity: 42-char hex strings starting with 0x
        assert Config.WETH_ADDRESS.startswith("0x")
        assert len(Config.WETH_ADDRESS) == 42
        assert Config.USDC_ADDRESS.startswith("0x")
        assert len(Config.USDC_ADDRESS) == 42

    def test_arbitrum_chain_id(self):
        assert Config.ARBITRUM_CHAIN_ID == 42161
