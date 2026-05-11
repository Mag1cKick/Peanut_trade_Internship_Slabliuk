"""Insert today's confirmed trades into the DB."""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.trades import TradeRecord, insert, print_ledger

# Trade 1 — 11:05 (first confirmed LINK trade)
insert(
    TradeRecord(
        ts="2026-05-06 11:05:39",
        pair="LINK/USDT",
        direction="buy_dex_sell_cex",
        size=1.0,
        dex_price=9.8069,
        cex_price=9.9300,
        spread_bps=42.7,
        gross_pnl=0.1231,
        net_pnl=0.0437,  # gross - cex_fee($0.0099) - gas($0.009) - slippage adj
        gas_usd=0.009,
        portfolio_usd=101.22,
        notes="First confirmed LINK trade. CEX params bug fixed before this run.",
    )
)

# Trade 2 — 11:25
insert(
    TradeRecord(
        ts="2026-05-06 11:25:18",
        pair="LINK/USDT",
        direction="buy_dex_sell_cex",
        size=1.0,
        dex_price=9.8853,
        cex_price=9.9300,
        spread_bps=45.6,
        gross_pnl=0.0447,
        net_pnl=0.0148,
        gas_usd=0.009,
        portfolio_usd=101.49,
        notes="effective_price bug fixed. _calculate_pnl sign bug fixed.",
    )
)

print("Seeded 2 historical trades.\n")
print_ledger()
