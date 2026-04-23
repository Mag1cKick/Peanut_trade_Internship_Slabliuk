"""
strategy/fees.py — Fee structure and cost calculation for arbitrage trades.

All costs are modelled in basis points (bps) relative to trade notional,
with gas expressed as a fixed USD amount that converts to bps at a given size.
All monetary values use Decimal to avoid floating-point rounding errors.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

_TEN_THOUSAND = Decimal("10000")


def _d(value: Decimal | float | int | str) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


@dataclass
class FeeStructure:
    """
    Fee model for one arb cycle: CEX taker + DEX swap + gas.
    """

    cex_taker_bps: Decimal = Decimal("10")
    dex_swap_bps: Decimal = Decimal("30")
    gas_cost_usd: Decimal = Decimal("5")

    def __post_init__(self) -> None:
        self.cex_taker_bps = _d(self.cex_taker_bps)
        self.dex_swap_bps = _d(self.dex_swap_bps)
        self.gas_cost_usd = _d(self.gas_cost_usd)

        if self.cex_taker_bps < 0:
            raise ValueError(f"cex_taker_bps must be >= 0, got {self.cex_taker_bps}")
        if self.dex_swap_bps < 0:
            raise ValueError(f"dex_swap_bps must be >= 0, got {self.dex_swap_bps}")
        if self.gas_cost_usd < 0:
            raise ValueError(f"gas_cost_usd must be >= 0, got {self.gas_cost_usd}")

    def gas_bps(self, trade_value_usd: Decimal | float) -> Decimal:
        """Gas cost expressed in basis points of notional."""
        trade_value_usd = _d(trade_value_usd)
        if trade_value_usd <= 0:
            raise ValueError(f"trade_value_usd must be > 0, got {trade_value_usd}")
        return (self.gas_cost_usd / trade_value_usd) * _TEN_THOUSAND

    def total_fee_bps(self, trade_value_usd: Decimal | float) -> Decimal:
        """Sum of all costs in basis points for a given notional."""
        return self.cex_taker_bps + self.dex_swap_bps + self.gas_bps(trade_value_usd)

    def breakeven_spread_bps(self, trade_value_usd: Decimal | float) -> Decimal:
        """Minimum spread (bps) at which the trade breaks even."""
        return self.total_fee_bps(trade_value_usd)

    def net_profit_usd(
        self, spread_bps: Decimal | float, trade_value_usd: Decimal | float
    ) -> Decimal:
        """Expected profit in USD after all fees."""
        spread_bps = _d(spread_bps)
        trade_value_usd = _d(trade_value_usd)
        gross = (spread_bps / _TEN_THOUSAND) * trade_value_usd
        fees = (self.total_fee_bps(trade_value_usd) / _TEN_THOUSAND) * trade_value_usd
        return gross - fees

    def fee_usd(self, trade_value_usd: Decimal | float) -> Decimal:
        """Total fees in USD (not bps) for a given notional."""
        trade_value_usd = _d(trade_value_usd)
        return (self.total_fee_bps(trade_value_usd) / _TEN_THOUSAND) * trade_value_usd
