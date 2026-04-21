"""
strategy/fees.py — Fee structure and cost calculation for arbitrage trades.

All costs are modelled in basis points (bps) relative to trade notional,
with gas expressed as a fixed USD amount that converts to bps at a given size.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FeeStructure:
    """
    Fee model for one arb cycle: CEX taker + DEX swap + gas.
    """

    cex_taker_bps: float = 10.0
    dex_swap_bps: float = 30.0
    gas_cost_usd: float = 5.0

    def __post_init__(self) -> None:
        if self.cex_taker_bps < 0:
            raise ValueError(f"cex_taker_bps must be >= 0, got {self.cex_taker_bps}")
        if self.dex_swap_bps < 0:
            raise ValueError(f"dex_swap_bps must be >= 0, got {self.dex_swap_bps}")
        if self.gas_cost_usd < 0:
            raise ValueError(f"gas_cost_usd must be >= 0, got {self.gas_cost_usd}")

    def gas_bps(self, trade_value_usd: float) -> float:
        """Gas cost expressed in basis points of notional."""
        if trade_value_usd <= 0:
            raise ValueError(f"trade_value_usd must be > 0, got {trade_value_usd}")
        return (self.gas_cost_usd / trade_value_usd) * 10_000

    def total_fee_bps(self, trade_value_usd: float) -> float:
        """
        Sum of all costs in basis points for a given notional.
        """
        return self.cex_taker_bps + self.dex_swap_bps + self.gas_bps(trade_value_usd)

    def breakeven_spread_bps(self, trade_value_usd: float) -> float:
        """
        Minimum spread (bps) at which the trade breaks even.
        """
        return self.total_fee_bps(trade_value_usd)

    def net_profit_usd(self, spread_bps: float, trade_value_usd: float) -> float:
        """
        Expected profit in USD after all fees.
        """
        gross = (spread_bps / 10_000) * trade_value_usd
        fees = (self.total_fee_bps(trade_value_usd) / 10_000) * trade_value_usd
        return gross - fees

    def fee_usd(self, trade_value_usd: float) -> float:
        """Total fees in USD (not bps) for a given notional."""
        return (self.total_fee_bps(trade_value_usd) / 10_000) * trade_value_usd
