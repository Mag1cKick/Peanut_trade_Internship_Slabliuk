# Day 3 — Missed Opportunities (Lecture Connection)

**Date:** 2026-05-08

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | ~$101.49 |
| Ending capital | ~$101.92 |
| PnL | +$0.017 |
| Trades | 1 (1 win / 0 losses) |
| Win rate | 100% |
| Spreads available | 55–65 bps (best of the week) |
| Trades missed | ~50+ signals discarded (inventory_ok=False) |

**Risk settings used:**
- max_trade_usd: $10
- max_daily_loss: $15
- pair: LINK/USDT

---

## What Happened

Started at 06:04 with 61 bps spread — the best conditions of the week. The bot discarded every signal with `inventory_ok=False` because Binance LINK had been depleted from Day 2 and never restocked overnight.

At 08:00 bought 1 LINK on Binance manually. However, the bot only syncs balances at startup — it didn't detect the new balance for two hours. At 09:55, after a manual restart (which re-synced balances), one trade fired at 57.9 bps → net +$0.017.

The rebalance also failed: the bridge script crashed with `UnicodeEncodeError` on the `→` arrow character in subprocess output on Windows (cp1251 console encoding).

After the one trade, inventory_ok=False again. Spreads of 55–65 bps continued for 6+ hours with zero additional trades.

---

## Connecting to Lecture: Inventory Risk in Market Making

This day illustrates a core concept from the market making lecture: **inventory risk is a first-order constraint, not an afterthought.**

Market makers (and arb bots) need balanced inventory on both sides to execute. Our bot required:
- Binance LINK (to sell on CEX)
- Wallet USDT (to buy on DEX)

When Binance LINK ran to 0, the bot had **unidirectional capacity only** — it could theoretically buy LINK on DEX but had nothing to sell on CEX to complete the arb. This is exactly the inventory imbalance problem that causes market makers to widen spreads or pull quotes entirely.

The lecture solution (skewing quotes to attract inventory-restoring flow) doesn't apply directly to arb bots, but the equivalent is: **periodic rebalancing must be part of the strategy design, not an afterthought.** Every 3 trades at 1.43 LINK/trade drains the Binance inventory. The rebalancing cost and mechanism must be planned before going live.

**Estimated missed profit:** 55–65 bps × ~50 signals × $0.033/trade ≈ **$1.65 in missed PnL** from a $0.01 fix (periodic balance sync).

---

## Changes Made

- **Periodic balance sync every 30s** in `_tick()` — bot now detects external balance changes without restart
- **Bridge Unicode fix** — replaced `→` with `->` in bridge scripts
- **Score display fix** — signal log now shows real score after scoring, not 0

---

## Lessons Learned

**The inventory sync gap was the costliest bug of the week.** A one-line fix (`if elapsed > 30: await _sync_balances()`) would have captured $1.65 in PnL that was lost. Small operational bugs have outsized cost when spreads are wide.

**Rebalancing must be reliable before scaling up.** One failed bridge + no auto-withdrawal = trading stopped for the full day. The tooling around the strategy is as important as the strategy itself.

---

## Tomorrow's Plan (Day 4)

- Verify periodic sync works (buy LINK without restarting, confirm bot picks it up within 30s)
- Switch to `--day 4` ($15 max trade, better gas economics)
- Keep 3+ LINK on Binance as buffer
- Test bridge with `--dry-run` before enabling auto-bridge
