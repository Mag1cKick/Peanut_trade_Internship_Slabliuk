# Day 1 — First Live Trades

**Date:** 2026-05-06

---

## Numbers

| Metric | Value |
|---|---|
| Starting capital | $100.00 |
| Ending capital | ~$100.00 |
| PnL | $0.00 |
| Trades | 0 completed (0 wins / 0 losses) |
| Win rate | N/A |
| Best trade | N/A |
| Worst trade | N/A |
| Fees paid (CEX) | $0.00 |
| Fees paid (DEX gas) | ~$0.002 (failed approval + swap attempts) |

**Risk settings used:**
- max_trade_usd: $7 (Day 1 schedule)
- max_daily_loss: $10
- max_drawdown_pct: 15%
- trade_size: 100 MAGIC (~$6.60)

**Funds deployed:**
- Binance: ~380 MAGIC + ~25 USDT
- Arbitrum wallet: ~380 MAGIC + ~24.9 USDT + 0.00058 ETH (gas)

---

## What Happened

No trades completed. The bot saw a persistent 222–267 bps spread on MAGIC/USDT BUY_DEX→SELL_CEX all morning (dex_buy=$0.066, cex_bid=$0.068). Signals generated, passed scoring (62/100), passed risk checks, entered execution — but never completed.

After fixing a chain of execution bugs, the DEX swap consistently reverted with `Too little received`. Integrating the Uniswap V3 Quoter contract revealed the truth: the bot was quoting the slot0 price (last-traded price) but the pool only had **~5 MAGIC in active range**. Selling 100 MAGIC would get $0.21 USDC, not the $6.60 the slot0 price implied.

Checked every MAGIC/USDT and MAGIC/USDC pool on Uniswap V3 — all ghost pools. The real MAGIC liquidity is on **SushiSwap MAGIC/WETH** (1.6M MAGIC + 46 WETH), which is efficiently priced at $0.0676 — exactly Binance bid. No arb opportunity there.

**The 267 bps spread we observed for days was a complete illusion** caused by stale slot0 prices in pools with no real depth.

---

## Problems Encountered

Eight execution bugs found and fixed before the root cause was identified:

| Bug | Fix |
|---|---|
| ERC-20 token addresses were Ethereum mainnet | Replaced with Arbitrum One addresses |
| chain_id defaulted to 1 (Ethereum) in tx builder | Added `chain_id=42161` to `ExecutorConfig` |
| `rawTransaction` renamed in web3.py v6 | Used `hasattr` fallback for both names |
| `estimateGas` called without `from` field → "approve from zero address" | Added `sender` to `TransactionRequest` |
| V2 calldata (`swapExactTokensForTokens`) sent to V3 router | Switched to V3 `exactInputSingle` (selector `414bf389`) |
| `amount_in` for BUY_DEX direction used MAGIC count as USDT units | Multiplied by `signal.dex_price` to convert correctly |
| `receipt.get("status")` on a dataclass → AttributeError | Changed to `receipt.status` |
| `eth.call()` returns HexBytes, not hex string | Added `.hex()` conversion in `_dispatch` |

After all fixes: swap still reverted with "Too little received" — because the pool has no real depth, not because of a code bug.

---

## Changes Made

- **`pricing/uniswap_direct.py`**: replaced slot0-based `get_prices_for_pair` with V3 Quoter-based pricing. Now uses `quoteExactInputSingle` for real executable prices, falls back to slot0 on Quoter failure.
- **`executor/engine.py`**: V3 `exactInputSingle` calldata, correct `amount_in` for both directions, `chain_id` on all tx builders, `receipt.status` checks.
- **`chain/builder.py`**: `sender` field propagated to all transaction dicts.
- **`chain/client.py`**: `eth.call()` now returns hex string, not HexBytes.
- **`scripts/arb_bot.py`**: Arbitrum ERC-20 addresses (MAGIC, USDT), `--one-shot` flag, better signal logging (shows cex_bid/ask/dex_buy/sell).

---

## Lessons Learned

**slot0 price ≠ executable price.** The Uniswap V3 `slot0.sqrtPriceX96` reflects the price of the last trade, not the price you'll actually get. A pool can have a "correct" price with zero in-range liquidity — a ghost pool. The only way to know the real executable price is to call the Quoter contract with your actual trade size. We should have validated pool depth before selecting a trading pair.

What I would do differently: before committing to any pair, run the Quoter for our intended trade size on day 0 and discard any pool where the actual fill price deviates more than 50 bps from slot0.

---

## Tomorrow's Plan

- Find a pair with real DEX depth that also has Binance listing
- Candidates: ARB/USDT (Binance) vs ARB/USDC (Uniswap V3 fee=500), or check Camelot/Ramses for MAGIC pools
- Before any live trading: validate pool with Quoter for intended trade size
- The Quoter is now wired into signal generation — any ghost pool will immediately show 0 spread and be rejected
- Consider smaller trade size (10–20 MAGIC) to see if any pool handles that
