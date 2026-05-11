# Day 5 — Stable Execution, Rebalance Economics, Final State

**Date:** 2026-05-10

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | ~$100.04 (end of Day 4) |
| Ending capital | ~$102.89 |
| Real PnL (arb trades today) | +$0.177 (6 trades) |
| Trades today | 6 (trades #6–11) |
| All-time trades | 11 |
| Win rate today | 100% |
| Spreads available | 44–52 bps |
| Avg net PnL per trade | ~$0.033 |

**Risk settings:** Day 4 limits (max_trade_usd $15, trade_usd $14.90)

---

## What Happened

Bot restarted after Day 4 debugging. First 3 trades (09:41–09:50) executed cleanly with correct PnL display (+$0.03 each). After trade #8, bot stopped on `inventory_ok=False` — Binance LINK depleted again.

Manual rebalance: sent 4.26 LINK from wallet to Binance deposit address on Arbitrum One. Discovered that **Binance does not support LINK deposits on Arbitrum One** — deposit went unconfirmed and required a Binance support ticket. Binance manually credited the LINK after ~1 hour.

After LINK arrived on Binance, bot restarted and executed 3 more trades (10:22–10:23) before inventory depleted again. **10+ trades milestone reached** (11 total across all days).

---

## Rebalance Economics Problem

Each rebalance cycle costs:

| Action | Cost |
|---|---|
| Bridge LINK wallet → BSC (Binance BEP20 deposit) | ~$0.82 |
| Withdraw USDT Binance → wallet (Arbitrum One) | ~$0.10 |
| **Total per cycle** | **~$0.92** |

At ~$0.033 net PnL per trade, we need **28 trades per rebalance cycle just to break even on rebalancing costs**. With Binance LINK inventory supporting ~3 trades before depletion, the rebalance cost kills the arb economics entirely at this trade size.

**Why the portfolio value is up despite rebalancing costs:**

Trade PnL (log-based, USDT in vs USDT out): **+$0.35** across 11 trades. This is the actual arbitrage performance.

Portfolio value also rose by ~$2.54 because LINK appreciated ~5.7% ($9.83 to $10.39). Per instructor guidance, asset price appreciation is not counted as trading PnL.

The honest conclusion: **at $15 trade size, rebalancing costs exceed arb profits per cycle.** The strategy only becomes self-sustaining at larger trade sizes where the fixed $0.92 rebalance cost becomes negligible relative to gross arb profit.

---

## Problems Encountered

| Problem | Root Cause | Resolution |
|---|---|---|
| Binance won't accept LINK on Arbitrum One | Network not supported for LINK deposits | Binance support manual credit (~1hr wait) |
| inventory_ok=False after 3 trades | Binance LINK depletes fast at 1.43 LINK/trade | Manual rebalance each cycle |
| Rebalance costs > arb PnL per cycle | Small trade size vs fixed bridge fees | Accepted for demo; needs 10x trade size in production |

---

## Changes Made

No code changes on Day 5 — bot ran stably with all Day 4 fixes in place:
- DEX-first execution with fresh-price CEX order: working
- PnL calculation (venue-aware formula): correct
- tick_size fix (0.01 not 0.977): no more expired IOC orders

---

## Lessons Learned

**Rebalance cost is the biggest structural problem for small-capital arb.** The 45 bps spread sounds attractive, but after a $0.92 rebalance every 3 trades, each trade needs to earn $0.31 just to cover overhead. At $15 trade size, gross is ~$0.10 — not enough. CEX/DEX arb is only economically viable above a minimum capital threshold where fixed costs become negligible relative to per-trade profit.

**Always verify the exact deposit network for each token on Binance.** USDT on Arbitrum One works. LINK on Arbitrum One does not. Sending to an unsupported network causes delays and requires manual support intervention.

**Price appreciation masked the rebalancing losses.** Total portfolio +$2.89 looks good, but most of it is LINK going up ~5%, not arb profits. In a flat or falling LINK market, rebalance costs would have pushed the portfolio negative despite individually profitable trades.

---

## Final Portfolio State

| Venue | LINK | USDT | USD Value |
|---|---|---|---|
| Wallet | ~1.4 | ~$3 | ~$17.5 |
| Binance | ~3.6 | ~$44 | ~$81.5 |
| **Total** | **~5.0** | **~$47** | **~$99** |

*Excludes 2.24 LINK stuck in Synapse bridge contract (~$23) — recovery pending via Synapse support.*
