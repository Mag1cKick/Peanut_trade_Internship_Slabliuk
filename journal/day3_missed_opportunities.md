# Day 3 — Missed Opportunities

**Date:** 2026-05-07

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | $101.09 (portfolio at 06:04) |
| Ending capital | ~$101.15 (LINK rose ~0.5%) |
| Real PnL (arb) | +$0.02 (1 trade) |
| Trades completed | 1 |
| Win rate | 100% |
| Spreads available | 55–65 bps all day (excellent) |
| Trades missed | ~50+ signals discarded (inventory_ok=False) |

**Risk settings:** Day 2 limits (max $10/trade)

---

## What Happened

Started with spreads at 61 bps at 06:04 — the best conditions yet. Bot ran correctly but Binance LINK had been depleted from Day 2. Every signal was discarded with `inventory_ok=False` (Binance LINK = 0, needed ≥ 0.99).

At 08:00 user bought 1 LINK on Binance. However, the inventory sync only happens at startup and after completed trades — the bot didn't detect the new balance for ~2 hours.

At 09:55, after a manual restart that re-synced balances, **1 trade fired** at 57.9 bps → pnl=$0.02.

Rebalance triggered correctly, but **bridge script failed** with `UnicodeEncodeError` on the `→` arrow character in a subprocess call on Windows (cp1251 console encoding). The USDT withdrawal also couldn't auto-execute (Binance API key needs withdrawal permission).

After the one trade, inventory_ok=False again. Spreads of 55–65 bps available for **6+ hours** but zero trades executed.

---

## Problems Encountered

| Problem | Root Cause | Impact |
|---|---|---|
| `inventory_ok=False` all morning | Balances only synced at startup, not periodically | Missed ~2 hours of 60+ bps spreads |
| Bot didn't detect manual LINK purchase | No periodic sync | Required manual restart to detect |
| Bridge failed | `→` Unicode char (U+2192) not in cp1251 (Windows console) | Auto-rebalance silently failed |
| USDT not auto-withdrawn from Binance | Binance API key missing withdrawal permission | Manual rebalance required |

---

## Changes Made (Day 3 fixes)

- **Periodic balance sync every 30s** in `_tick()` — bot now detects external balance changes without restart
- **Score display fixed** — "Generated signal" now logs AFTER scoring with real score; removed misleading `score=0`
- **Bridge Unicode fix** — replaced all `→` with `->` in bridge scripts
- **Rebalance trigger** — now fires on any tick where inventory is too low, not just after trades

---

## Lessons Learned

**The inventory sync gap was the costliest bug of the week.** 55–65 bps spread for 6+ hours = ~$0.03 net per trade × ~60 possible trades = ~$1.80 missed profit. A $0.01 fix (periodic sync) would have captured it.

**Rebalancing must be reliable before scaling up.** One failed bridge + no auto-withdrawal = trading stopped for the full day. Fix the tooling before Day 4.

**Windows console encoding**: `→`, `✅`, `⚠️` and other Unicode chars need to be avoided in subprocess output on Windows (cp1251). Use ASCII alternatives.

---

## Tomorrow's Plan (Day 4 — $15 max trade)

- Verify periodic sync works (buy LINK on Binance without restarting, confirm bot picks it up within 30s)
- Test bridge with `--dry-run` before enabling auto-bridge
- Switch to `--day 4` (max_trade_usd $15 → ~1.5 LINK per trade, better gas economics)
- Keep 3+ LINK on Binance at all times as buffer
- Withdraw USDT from Binance to wallet manually if needed (API withdrawal requires extra key permission)
