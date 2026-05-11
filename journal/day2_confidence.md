# Day 2 — First Real Trades (Confidence Check)

**Date:** 2026-05-07

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | ~$100.00 |
| Ending capital | ~$101.49 |
| PnL | +$0.059 |
| Trades | 2 (2 wins / 0 losses) |
| Win rate | 100% |
| Best trade | +$0.044 (Trade #1, 42.7 bps) |
| Worst trade | +$0.015 (Trade #2, 45.6 bps) |
| Fees paid (CEX) | ~$0.020 |
| Fees paid (DEX gas) | ~$0.004 |

**Risk settings used:**
- max_trade_usd: $10
- max_daily_loss: $15
- max_drawdown_pct: 20%
- pair: LINK/USDT (switched from MAGIC/USDC)

---

## What Happened

Day 1 ended with zero trades because MAGIC/USDC was a ghost pool. Day 2 started with pair research: found LINK/USDT on Uniswap V3 (fee=3000) with 1e18 in-range liquidity and a real, persistent 40–50 bps spread between Binance bid and Uniswap ask.

**Trade #1 (11:05):** First confirmed real-money round-trip. Bought 1.0 LINK at $9.807 on DEX, sold at $9.930 on Binance. Net +$0.044. Confirmed on Arbiscan. The feeling of watching a real transaction confirm on-chain and then seeing the Binance fill was significant — the system worked end-to-end.

**Trade #2 (11:25):** Second trade, slightly tighter spread (45.6 bps). Net +$0.015. Slower because the USDT approval from Trade #1 was already set, so only one on-chain transaction needed.

After two trades, Binance LINK inventory dropped below threshold and bot paused with `inventory_ok=False`.

**Confidence level after Day 2:** High. The core arbitrage loop — signal → DEX buy → CEX sell → PnL — works exactly as designed. The spread is real and persistent. The main risk going forward is inventory management, not signal quality.

---

## Problems Encountered

| Problem | Root Cause | Fix |
|---|---|---|
| Fee double-counting | `dex_swap_bps` was charging the pool fee ON TOP of Quoter price (which already includes it) | Set `dex_swap_bps=0` — Quoter prices are the real executable prices including fees |
| `score=0` in log | "Generated signal" log was before scoring step | Moved log to after `scorer.score()` call |
| Inventory depleted after 2 trades | Binance LINK goes to 0 fast at 1 LINK/trade | Need rebalancing plan |

---

## Changes Made

- `config/settings.py`: `dex_swap_bps=0` in PROD_CONFIG
- `strategy/generator.py`: Moved signal log to after scoring (shows real score)
- `scripts/arb_bot.py`: Added `ERC20_ADDRESSES` for Arbitrum LINK token

---

## Lessons Learned

**The spread was real all along — the pair was wrong.** MAGIC/USDC had a phantom spread caused by no liquidity. LINK/USDT has the same spread profile but with real depth. Pair selection is the most important decision before going live.

**Quoter prices already include the pool fee.** Don't charge it again in the fee structure. This would have made every trade appear unprofitable and suppressed signals.

---

## Tomorrow's Plan

- Buy more LINK on Binance to restore inventory
- Run with `--day 3` (max $10/trade)
- Keep 3+ LINK on Binance at all times as buffer
- Monitor for inventory_ok=False and restart quickly
