# Day 2 — LINK/USDT Live Trading

**Date:** 2026-05-06
**Continued from Day 1** (MAGIC ghost-pool discovery)

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | $100.00 |
| Ending capital | $101.14 |
| Real PnL (check_pnl.py) | +$1.14 (+1.14%) |
| Trades completed | 2 (2W / 0L) — plus 1 orphaned DEX-only |
| Win rate | 100% |
| Best trade | ~+$0.04 gross |
| Wallet LINK acquired | 3 LINK (2 from complete arb + 1 orphaned) |
| Fees paid (CEX) | ~$0.02 total |
| DEX gas (Arbitrum) | ~$0.009/swap actual |

**Risk settings:**
- max_trade_usd: $10 (Day 2)
- trade_size: 1 LINK ≈ $9.87
- max_daily_loss: $15

**Arbiscan wallet:** https://arbiscan.io/address/0x15c7B3c21bB7DeaD50DBAe2036d870653B07d784

---

## What Happened

After discovering MAGIC/USDT V3 pools are ghost pools (correct slot0 price but no in-range liquidity — Quoter showed $0.21 USDT for 100 MAGIC instead of $6.60), we switched to LINK/USDT.

**Pair selection process:**
- Scanned 11 Binance-listed tokens on Arbitrum DEX pools using the V3 Quoter
- LINK/USDT Uniswap V3 fee=3000: persistent 40-47 bps spread, real depth confirmed
- `check_pool_depth.py`: selling 1–500 LINK deviates only 0.3% from slot0 (= pool fee only, zero price impact)

**Trade flow (BUY_DEX_SELL_CEX):**
1. Bot detects: DEX buy price $9.807 vs Binance bid $9.930 — spread 43-46 bps
2. DEX leg: swap USDT → LINK on Uniswap V3 fee=3000 pool
3. CEX leg: sell pre-positioned LINK on Binance at bid
4. Net: ~+$0.015-0.025 per trade after fees and gas

**Confirmed on Arbiscan:** wallet received LINK from V3 swap, USDT balance decreased accordingly.

---

## Problems Encountered

### 1. Execution pipeline had 8 bugs (fixed over course of the day):

| Bug | Symptom | Fix |
|---|---|---|
| Wrong ERC-20 addresses (mainnet not Arbitrum) | MAGIC/LINK balance read as 0 | Replaced with Arbitrum addresses |
| Missing `chain_id=42161` in tx builder | Tx signed for wrong chain | Added to `ExecutorConfig` |
| `rawTransaction` → `raw_transaction` (web3 v6) | CEX order failed | `hasattr` fallback |
| No `from` field in `estimateGas` | "approve from zero address" | Added `sender` to `TransactionRequest` |
| V2 calldata sent to V3 router | Swap reverted every time | Rewrote to V3 `exactInputSingle` |
| `amount_in` wrong for BUY_DEX direction | Tried to spend 100 USDT instead of $9.86 | Used `signal.dex_price` to convert |
| `receipt.get("status")` on dataclass | AttributeError killed trade | Changed to `receipt.status` |
| `HexBytes` not decoded in chain client | Balance fetches returned garbage | Added `.hex()` in `_dispatch` |

### 2. MAGIC ghost pool — the core lesson:
Uniswap V3 `slot0.sqrtPriceX96` shows the last-traded price, not the executable price. A pool with 5 MAGIC in active range shows a "correct" $0.066 price but can only give $0.21 for 100 MAGIC. **Always validate depth with the Quoter before selecting a trading pair.**

### 3. Fee double-counting:
`FeeStructure` was charging 30 bps DEX fee on top of Quoter prices that already include the pool fee. Fixed by setting `dex_swap_bps=0` since Quoter prices are post-fee.

### 4. `_calculate_pnl` sign bug:
For BUY_DEX_SELL_CEX, formula was `(leg1 - leg2)` instead of `(leg2 - leg1)`, showing -$8.92 loss on a winning trade. **The bot's PnL display was wrong; `check_pnl.py` (live balance calculation) is truth.**

### 5. Orphaned LINK (no unwind):
First run had DEX succeed but CEX fail (`params` error). Exception propagated before unwind could trigger → 1 LINK stuck in wallet without matching Binance sell. Fixed `params` bug; unwind path untested.

---

## Changes Made

- **`pricing/uniswap_direct.py`**: Quoter-based depth validation — pools with >10% deviation from slot0 rejected as ghost pools. Pool resolution skips uninitialized pools.
- **`executor/engine.py`**: V3 `exactInputSingle` calldata, correct `effective_price` for both directions, `_calculate_pnl` sign fix, `chain_id` on all tx builders.
- **`scripts/arb_bot.py`**: Arbitrum ERC-20 addresses, `dex_swap_bps=0`, `gas_cost_usd=0.02`, LINK scorer config (excellent=50 bps, liquidity threshold=15 bps), portfolio tracker at start/end.
- **`scripts/check_pnl.py`**: New — fetches live balances from Binance + wallet, calculates true portfolio value in USD.

---

## Lessons Learned

1. **Slot0 ≠ executable price.** Always use the Quoter for real price discovery. Ghost pools have correct prices but zero depth.

2. **Gas economics at $10 trade size.** At $0.009 actual gas on Arbitrum, break-even spread = 10 (CEX fee) + 9 (gas bps) = 19 bps. LINK at 40-47 bps is profitable.

3. **The scorer needs calibration per pair.** Default `excellent_spread_bps=100` and `liquid_spread_threshold_bps=5` were calibrated for ETH/BTC. LINK's 10 bps bid-ask is normal — threshold should be 15 bps.

4. **Use live balances, not bot accounting, for real PnL.** The bot's internal PnL had multiple calculation bugs. `check_pnl.py` gives ground truth.

5. **Pre-position inventory before going live.** BUY_DEX_SELL_CEX requires LINK on Binance (CEX sell leg). After each trade, LINK accumulates in wallet and must be redeployed to Binance.

---

## Tomorrow's Plan (Day 3)

- Transfer 3 LINK from wallet to Binance (restore inventory for 3 more trades)
- Continue running on LINK/USDT fee=3000, 1 LINK trade size
- Watch for spread spikes above 50 bps (more profitable)
- Fix the unwind path — test what happens when CEX leg fails
- Consider adding 2 LINK trade size for Day 3 ($20, higher than $10 day limit — revisit on Day 4)
