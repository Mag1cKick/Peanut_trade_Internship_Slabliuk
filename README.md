# Peanut Trade — Weeks 1–4: CEX-DEX Arbitrage Bot

> End-to-end arbitrage trading system — wallet management, blockchain interaction,
> on-chain DEX pricing, CEX order book analysis, multi-venue inventory tracking,
> signal generation & scoring, executor state machine, circuit breaker,
> and a live two-mode trading bot (Binance testnet + Uniswap V2 mainnet).

---

## Quick Start

```bash
# 1. Clone & install
git clone <your-repo-url>
cd Peanut_trade_Internship_Slabliuk
make install

# 2. Configure
cp .env.example .env
# Edit .env — PRIVATE_KEY, ETH_RPC_URL, BINANCE_TESTNET_API_KEY, BINANCE_TESTNET_SECRET

# 3. Run all 1773 tests
make test

# ── Week 4 ─────────────────────────────────────────────────────────────────

# 4. Live arb demo — stub DEX (no API keys, always works)
python scripts/demo_week4.py

# 5. Live arb demo — real Uniswap V2 prices (needs ETH_RPC_URL)
python scripts/demo_week4.py --rpc-url https://ethereum.publicnode.com

# 6. Run the bot in TEST mode (Binance testnet + real DEX prices, no execution)
python scripts/arb_bot.py --mode test

# 7. Run the bot in PROD mode (Binance mainnet + real execution, needs PRIVATE_KEY)
python scripts/arb_bot.py --mode prod

# 8. Week 4 integration tests — mocked (CI-safe, no network)
pytest tests/test_integration_week4.py -m "not network" -v

# 9. Week 4 integration tests — real Binance prices (needs internet)
pytest tests/test_integration_week4.py -m network -v -s

# ── Earlier weeks ──────────────────────────────────────────────────────────

# 10. CEX order book live snapshot
python -m exchange.orderbook ETH/USDT --depth 20 --qty 2

# 11. Arb opportunity check (mocked DEX + live CEX)
python -m integration.arb_checker ETH/USDT --size 2.0 --dex-price 2007.21

# 12. Live WebSocket order book stream
python -m exchange.ws_orderbook ETH/USDT --count 5

# 13. Venue rebalance check
python -m inventory.rebalancer --check

# 14. P&L dashboard (demo data)
python -m inventory.dashboard --once
```

---

## Architecture

```
                        ┌─────────────────────────────────────────────────────┐
                        │                 ArbChecker (integration/)            │
                        │                                                       │
                        │  check(pair, size) → {gap_bps, executable, ...}      │
                        └──────────┬──────────────┬──────────────┬─────────────┘
                                   │              │              │
                     ┌─────────────▼──┐  ┌────────▼──────┐  ┌──▼───────────────┐
                     │ PricingEngine  │  │ ExchangeClient │  │ InventoryTracker │
                     │  (pricing/)    │  │  (exchange/)   │  │  (inventory/)    │
                     │                │  │                │  │                  │
                     │ get_quote()    │  │fetch_order_book│  │ can_execute()    │
                     │ RouteFinder    │  │create_*_order  │  │ update_from_cex  │
                     │ UniswapV2Pair  │  │get_trading_fees│  │ update_from_wallet│
                     └──────┬─────────┘  └───────┬────────┘  └──────┬───────────┘
                            │                    │                   │
                   ┌────────▼──────┐    ┌────────▼────────┐  ┌──────▼───────────┐
                   │  ForkSimulator │    │ OrderBookAnalyzer│  │  RebalancePlanner│
                   │  (Anvil fork)  │    │  walk_the_book() │  │  plan() / check()│
                   │  simulate_swap │    │  imbalance()     │  │  TransferPlan    │
                   │  validate quote│    │  depth_at_bps()  │  │  estimate_cost() │
                   └────────┬───────┘    └─────────────────┘  └──────────────────┘
                            │
                   ┌────────▼───────┐    ┌──────────────────┐
                   │  ChainClient   │    │    PnLEngine       │
                   │  (chain/)      │    │  (inventory/)      │
                   │  get_balance() │    │  record(ArbRecord) │
                   │  send_tx()     │    │  summary()         │
                   └────────┬───────┘    │  export_csv()      │
                            │            └──────────────────┘
                   ┌────────▼───────┐
                   │  WalletManager │
                   │  (core/)       │
                   │  sign_message()│
                   │  sign_tx()     │
                   └────────────────┘
```

### Data Flow: Arb Check

```
DEX side                              CEX side
────────                              ────────
PricingEngine.get_quote()             ExchangeClient.fetch_order_book()
  └─ RouteFinder.find_best_route()      └─ OrderBookAnalyzer.walk_the_book()
       └─ UniswapV2Pair.get_amount_out()     └─ slippage_bps, avg_fill_price
            └─ ForkSimulator.validate()
                                      ExchangeClient.get_trading_fees()

              ┌──────────────────────────────────────────┐
              │            ArbChecker.check()             │
              │                                           │
              │  gap_bps    = (cex_price - dex_price)     │
              │               / dex_price × 10000         │
              │                                           │
              │  costs_bps  = dex_fee + dex_impact        │
              │             + cex_fee + cex_slippage       │
              │             + gas_bps                     │
              │                                           │
              │  net_pnl    = gap_bps - costs_bps         │
              │  executable = net_pnl > 0 AND inventory   │
              └──────────────────────────────────────────┘
                                    │
                            InventoryTracker.can_execute()
                              buy_venue, buy_asset, buy_amount
                              sell_venue, sell_asset, sell_amount
```

---

## Project Structure

```
.
├── core/                        # Week 1 — wallet, types, serialization
│   ├── wallet.py                # WalletManager — key loading, signing, security
│   ├── types.py                 # Address, TokenAmount, Token, TransactionRequest/Receipt
│   └── serializer.py            # CanonicalSerializer — deterministic JSON + keccak256
│
├── chain/                       # Week 1 — blockchain interaction
│   ├── client.py                # ChainClient — RPC with retry, fallback, error classification
│   ├── builder.py               # TransactionBuilder — fluent transaction construction
│   ├── analyzer.py              # CLI: python -m chain.analyzer <tx_hash>
│   └── errors.py                # ChainError hierarchy
│
├── pricing/                     # Week 2 — AMM math, routing, simulation, monitoring
│   ├── amm.py                   # UniswapV2Pair — exact integer AMM formula
│   ├── amm_v3.py                # UniswapV3Pool — concentrated liquidity (Q96 math)
│   ├── router.py                # Route + RouteFinder — multi-hop DFS, gas-adjusted
│   ├── arbitrage.py             # ArbitrageDetector — circular + cross-pool detection
│   ├── mempool.py               # MempoolMonitor + ParsedSwap — pending swap decoding
│   ├── fork_simulator.py        # ForkSimulator — eth_call against local Anvil fork
│   ├── historical.py            # HistoricalAnalyzer — reserve snapshots + trend analysis
│   ├── price_feed.py            # PriceFeed — real-time price stream over WebSocket
│   ├── impact_analyzer.py       # PriceImpactAnalyzer — slippage tables, max trade size
│   └── engine.py                # PricingEngine — unified interface + Quote validation
│
├── exchange/                    # Weeks 3–4 — CEX interaction & live streaming
│   ├── client.py                # ExchangeClient — Binance testnet, rate limiting, orders
│   ├── orderbook.py             # OrderBookAnalyzer — walk_the_book, depth, imbalance
│   │                            #   CLI: python -m exchange.orderbook ETH/USDT
│   ├── bybit_client.py          # BybitClient — ccxt-backed Bybit adapter [stretch]
│   └── ws_orderbook.py          # OrderBookStream — Binance WebSocket depth stream [stretch]
│                                #   Binance sync protocol: WS open → REST snapshot → diffs
│                                #   CLI: python -m exchange.ws_orderbook ETH/USDT --count 10
│
├── inventory/                   # Weeks 3–4 — position tracking, PnL, dashboard
│   ├── tracker.py               # CostBasisTracker (fills P&L) + InventoryTracker (venues)
│   │                            #   Venue enum, Balance dataclass, skew analysis
│   ├── pnl.py                   # PnLEngine — TradeLeg, ArbRecord, summary(), export_csv()
│   │                            #   CLI: python -m inventory.pnl --summary
│   ├── rebalancer.py            # RebalancePlanner — venue-aware transfer plans
│   │                            #   TransferPlan, TRANSFER_FEES, MIN_OPERATING_BALANCE
│   │                            #   CLI: python -m inventory.rebalancer --check
│   ├── dashboard.py             # InventoryDashboard — Rich live terminal UI [stretch]
│   │                            #   CLI: python -m inventory.dashboard --once
│   └── charts.py                # PnLCharts — matplotlib cumulative PnL, drawdown [stretch]
│                                #   CLI: python -m inventory.charts --output charts/
│
├── integration/                 # Weeks 3–4 — full pipeline
│   ├── arb_checker.py           # ArbChecker — DEX + CEX + inventory → opportunity dict
│   │                            #   PricingEngineAdapter — wires Week 2 PricingEngine
│   │                            #   SimplePricingAdapter — lightweight shim for testing
│   │                            #   CLI: python -m integration.arb_checker ETH/USDT
│   └── arb_logger.py            # ArbLogger — ring buffer + CSV export of arb results
│                                #   CLI: python -m integration.arb_logger ETH/USDT
│
├── safety/                      # Week 5 placeholder
├── config/                      # Week 5 placeholder
│
├── tests/                       # 1522 unit tests, all passing, 99% coverage
│   ├── test_wallet.py           # 37  — key loading, security, signing
│   ├── test_serializer.py       # 55  — determinism, unicode, edge cases
│   ├── test_types.py            # 68  — validation, arithmetic, equality
│   ├── test_client.py           # 44  — retry logic, error classification
│   ├── test_builder.py          # 55  — fluent API, validation
│   ├── test_analyzer.py         # 47  — decoding, parsing, CLI
│   ├── test_amm.py              # 65  — AMM math, Solidity test vector, precision
│   ├── test_amm_v3.py           # 30  — V3 Q96 math, concentrated liquidity
│   ├── test_impact_analyzer.py  # 50  — slippage tables, binary search, CLI
│   ├── test_router.py           # 38  — DFS routing, gas flip, sequential match
│   ├── test_arbitrage.py        # 27  — circular + cross-pool detection
│   ├── test_mempool.py          # 36  — calldata decoding, async monitor
│   ├── test_fork_simulator.py   # 27  — mocked eth_call, reserve fetch
│   ├── test_historical.py       # 20  — snapshot fetch, trend analysis
│   ├── test_price_feed.py       # 18  — WebSocket price stream mock
│   ├── test_pricing_engine.py   # 37  — integration, quote validity
│   ├── test_exchange_client.py  # 95  — orders, balance, rate limiter, errors
│   ├── test_orderbook.py        # 53  — walk_the_book, depth, imbalance, CLI
│   ├── test_order_book.py       # 24  — legacy OrderBookAnalyzer
│   ├── test_inventory.py        # 78  — CostBasisTracker, WeightRebalancePlanner, PnL
│   ├── test_multi_venue_tracker.py # 52 — InventoryTracker, can_execute, skew
│   ├── test_rebalancer.py       # 67  — RebalancePlanner, TransferPlan, CLI
│   ├── test_pnl.py              # 65  — ArbRecord, PnLEngine, CSV export, CLI
│   ├── test_arb_checker.py      # 58  — ArbChecker, direction, costs, inventory, CLI
│   ├── test_arb_logger.py       # 58  — ArbLogger, ring buffer, CSV, stats
│   ├── test_bybit_client.py     # 52  — BybitClient, order book, balance, rate limiter
│   ├── test_dashboard.py        # 26  — InventoryDashboard, Rich tables, live loop
│   ├── test_charts.py           # 25  — PnLCharts, all chart types, file output
│   └── test_ws_orderbook.py     # 54  — OrderBookStream, sync protocol, async iter
│
├── scripts/
│   ├── lab4_demo.py             # Lab 4 full pipeline demo (offline, no API keys)
│   ├── pricing_demo.py          # Week 2 pricing pipeline demo
│   ├── integration_test.py      # End-to-end Sepolia test
│   └── check_secrets_baseline.py
│
├── configs/settings.yaml
├── .env.example
├── .pre-commit-config.yaml
├── Makefile
├── pyproject.toml
├── requirements.txt
└── requirements-dev.txt
```

---

## Setup

### Prerequisites

- Python 3.13+
- A Sepolia RPC URL ([Alchemy](https://www.alchemy.com/) or [Infura](https://infura.io/) — free tier)
- Sepolia ETH ([faucet](https://sepoliafaucet.com/))

### Install

```bash
make install
```

### Configure

```bash
cp .env.example .env
```

Edit `.env`:

```env
PRIVATE_KEY=0x...          # your wallet private key — never commit this
RPC_URL=https://...        # your Alchemy/Infura Sepolia RPC URL
ENVIRONMENT=development
```

---

## Usage Examples

### Transaction Analyzer

Analyze any Ethereum transaction — mainnet or testnet:

```bash
# Simple ETH transfer
python -m chain.analyzer 0xb5c8bd9430b6cc87a0e2fe110ece6bf527fa4f170a4bc8cd032f768fc5219838 \
  --rpc https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# Uniswap V2 swap
python -m chain.analyzer 0xaf6e8e358b9d93ead36b5852c4ebb9127fa88e3f7753f73d8a3f74a552601742 \
  --rpc https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# Failed transaction — shows revert reason
python -m chain.analyzer 0xc5178498b5c226d9f7e2f5086f72bf0e4f4d87e097c4e517f1bec128580fd537 \
  --rpc https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# JSON output
python -m chain.analyzer 0xb5c8bd9... --rpc https://... --format json
```

**Example output:**
```
Transaction Analysis
====================
Hash:           0xb5c8bd94...
Block:          14,000,000
Timestamp:      2022-03-19 12:34:56 UTC
Status:         SUCCESS

From:           0xSender...
To:             0xRecipient...
Value:          1.000000 ETH

Gas Analysis
------------
Gas Limit:      21,000
Gas Used:       21,000 (100.00%)
Effective Price: 55.00 gwei
Transaction Fee: 0.001155 ETH
```

### WalletManager

```python
from core.wallet import WalletManager

# Load from environment
wallet = WalletManager.from_env("PRIVATE_KEY")
print(wallet)  # WalletManager(address=0x...) — key never shown

# Sign a message
signed = wallet.sign_message("hello world")

# Verify
assert wallet.verify_message("hello world", signed.signature.hex())

# Generate new wallet (prints key once)
new_wallet = WalletManager.generate()
```

### ChainClient

```python
from chain.client import ChainClient
from core.types import Address

client = ChainClient(
    rpc_urls=[
        "https://sepolia.infura.io/v3/KEY",    # primary
        "https://eth-sepolia.g.alchemy.com/v2/KEY",  # fallback
    ]
)

address = Address("0x...")
balance = client.get_balance(address)
print(f"Balance: {balance.human} ETH")

gas_price = client.get_gas_price()
print(f"Base fee: {gas_price.gwei_base_fee:.1f} gwei")
```

### TransactionBuilder

```python
from chain.builder import TransactionBuilder

receipt = (
    TransactionBuilder(client, wallet)
    .to(Address("0xRecipient..."))
    .value(TokenAmount.from_human("0.01", 18, "ETH"))
    .data(b"")
    .chain_id(11155111)          # Sepolia
    .with_gas_estimate(buffer=1.2)
    .with_gas_price("medium")
    .send_and_wait(timeout=120)
)
print(f"Confirmed in block {receipt.block_number}")
```

### CanonicalSerializer

```python
from core.serializer import CanonicalSerializer

# Deterministic JSON — key order doesn't matter
data = {"b": 2, "a": 1, "nested": {"z": 9, "x": 0}}
canonical = CanonicalSerializer.serialize(data)
# b'{"a":1,"b":2,"nested":{"x":0,"z":9}}'

# keccak256 hash for signing
digest = CanonicalSerializer.hash(data)  # 32 bytes

# Verify determinism
assert CanonicalSerializer.verify_determinism(data, iterations=1000)
```

---

## Week 2: Pricing Module

### Architecture

```
                          ┌─────────────────────────────────┐
                          │         PricingEngine            │
                          │  load_pools() · refresh_pool()  │
                          │  get_quote()  · pending_swaps   │
                          └────────┬──────────┬─────────────┘
                                   │          │
               ┌───────────────────┘          └───────────────────┐
               ▼                                                   ▼
  ┌────────────────────────┐                        ┌─────────────────────────┐
  │      RouteFinder        │                        │     ForkSimulator        │
  │  _build_graph() (DFS)  │                        │  simulate_route()        │
  │  find_best_route()     │                        │  compare_vs_calculation()│
  │  compare_routes()      │                        └───────────┬─────────────┘
  └────────────┬───────────┘                                    │ eth_call
               │ Route                                          │ getAmountsOut
               ▼                                                │ getReserves
  ┌────────────────────────┐                                    ▼
  │     UniswapV2Pair       │◄──────────────────────────── Anvil Fork
  │  get_amount_out()       │    (integer AMM math,          (local RPC)
  │  get_spot_price()       │     matches Solidity)
  │  get_price_impact()     │
  └────────────────────────┘

  ┌────────────────────────┐
  │    MempoolMonitor       │  wss:// subscription
  │  start() (async)       │──────────────────────► pending txs
  │  parse_transaction()   │  decodes V2 calldata
  │  decode_swap_params()  │  calls engine._on_mempool_swap()
  └────────────────────────┘
```

### AMM Math

Our `get_amount_out` implements the **exact** Uniswap V2 Solidity formula using integer-only arithmetic:

```
amountOut = (amountIn × 9970 × reserveOut)
            ─────────────────────────────────────
            (reserveIn × 10000 + amountIn × 9970)
```

`9970/10000` is algebraically identical to Solidity's `997/1000` — and produces the **same integer-division result** for all inputs (verified in `test_fee_matches_uniswap_997_multiplier`).

Test vector from the [Uniswap V2 core test suite](https://github.com/Uniswap/v2-core):

| reserve0 | reserve1 | amountIn | expected amountOut |
|---|---|---|---|
| 5 × 10¹⁸ | 10 × 10¹⁸ | 10¹⁸ | **1,662,497,915,624,478,906** |

### Quick Usage

```python
from chain.client import ChainClient
from core.types import Address
from pricing.engine import PricingEngine, QuoteError
from pricing.amm import UniswapV2Pair
from pricing.router import RouteFinder

# ── 1. AMM math (no node required) ──────────────────────────────────────
from core.types import Token
from pricing.amm import UniswapV2Pair

USDC = Token(address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"), symbol="USDC", decimals=6)
WETH = Token(address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18)

pair = UniswapV2Pair(
    address=Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc"),
    token0=USDC, token1=WETH,
    reserve0=100_000_000 * 10**6,   # 100M USDC
    reserve1=50_000 * 10**18,        # 50k WETH
    fee_bps=30,
)

amount_out = pair.get_amount_out(1_000 * 10**6, USDC)   # 1000 USDC → WETH
impact     = pair.get_price_impact(1_000 * 10**6, USDC) # e.g. Decimal('0.00001')
print(f"Out: {amount_out / 10**18:.6f} WETH  impact: {impact:.4%}")

# ── 2. Multi-hop routing ─────────────────────────────────────────────────
from pricing.router import RouteFinder

dai_weth = UniswapV2Pair(...)   # second pool
finder = RouteFinder(pools=[pair, dai_weth])

best_route, net_output = finder.find_best_route(
    token_in=USDC, token_out=WETH,
    amount_in=1_000 * 10**6,
    gas_price_gwei=20,
)
print(f"Best: {best_route}  net output: {net_output / 10**18:.6f} WETH")

# ── 3. Full engine ───────────────────────────────────────────────────────
client  = ChainClient(rpc_urls=["https://eth-mainnet.g.alchemy.com/v2/KEY"])
engine  = PricingEngine(
    chain_client=client,
    fork_url="http://127.0.0.1:8545",   # anvil --fork-url $ETH_RPC_URL
    ws_url="wss://eth-mainnet.g.alchemy.com/v2/KEY",
)

USDC_WETH_ADDR = Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")
engine.load_pools([USDC_WETH_ADDR])

try:
    quote = engine.get_quote(USDC, WETH, amount_in=1_000 * 10**6, gas_price_gwei=20)
    print(f"Route:     {quote.route}")
    print(f"Expected:  {quote.expected_output / 10**18:.6f} WETH (net of gas)")
    print(f"Simulated: {quote.simulated_output / 10**18:.6f} WETH (fork)")
    print(f"Valid:     {quote.is_valid}")        # True if sim within 0.1% of expected
except QuoteError as e:
    print(f"Quote failed: {e}")
```

### Price Impact Analyzer CLI

```bash
# Trade size impact table — 1k, 10k, 100k USDC
python -m pricing.impact_analyzer \
  --pair 0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc \
  --token-in 0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48 \
  --sizes 1000,10000,100000 \
  --rpc https://eth-mainnet.g.alchemy.com/v2/KEY
```

### Local Fork Setup

```bash
export ETH_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
bash scripts/start_fork.sh          # starts Anvil on http://127.0.0.1:8545
```

---

---

## Week 3: Exchange, Inventory & Integration

### ExchangeClient — Binance Testnet

```python
from exchange.client import ExchangeClient

client = ExchangeClient({
    "apiKey": "YOUR_BINANCE_TESTNET_KEY",  # pragma: allowlist secret
    "secret": "YOUR_BINANCE_TESTNET_SECRET",  # pragma: allowlist secret
    "sandbox": True,
    "enableRateLimit": True,
})

# Live order book
book = client.fetch_order_book("ETH/USDT", limit=20)
# {symbol, timestamp, bids, asks, best_bid, best_ask, mid_price, spread_bps}

# Balances
balances = client.fetch_balance()
# {"ETH": {"free": Decimal, "locked": Decimal, "total": Decimal}, ...}

# Orders
order = client.create_limit_ioc_order("ETH/USDT", "buy", amount=0.1, price=2000.0)
client.cancel_order(order["id"], "ETH/USDT")

# Fees
fees = client.get_trading_fees("ETH/USDT")
# {"maker": Decimal("0.001"), "taker": Decimal("0.001")}
```

### OrderBookAnalyzer CLI

```bash
python -m exchange.orderbook ETH/USDT --depth 20 --qty 2
```

```
╔══════════════════════════════════════════════════════╗
║  ETH/USDT Order Book Analysis                        ║
║  Timestamp: 2024-01-15 14:30:00 UTC                  ║
╠══════════════════════════════════════════════════════╣
║  Best Bid:    $2,010.00 × 5.2000 ETH                 ║
║  Best Ask:    $2,010.50 × 3.8000 ETH                 ║
║  Mid Price:   $2,010.25                               ║
║  Spread:      $0.50 (2.49 bps)                        ║
╠══════════════════════════════════════════════════════╣
║  Depth (within 10 bps):                               ║
║    Bids: 47.2000 ETH ($94,923.84)                    ║
║    Asks: 38.1000 ETH ($76,609.05)                    ║
║  Imbalance: +0.11 (buy pressure)                     ║
╠══════════════════════════════════════════════════════╣
║  Walk-the-book (2 ETH buy):                          ║
║    Avg price:  $2,010.52                              ║
║    Slippage:   0.10 bps                               ║
║    Levels:     1                                      ║
╚══════════════════════════════════════════════════════╝
```

### InventoryTracker

```python
from inventory.tracker import InventoryTracker, Venue
from decimal import Decimal

tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])

# Update from CEX
tracker.update_from_cex(Venue.BINANCE, client.fetch_balance())

# Update from on-chain wallet
tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5.0"), "USDT": Decimal("10000")})

# Pre-flight arb check
result = tracker.can_execute(
    buy_venue=Venue.WALLET,  buy_asset="USDT",  buy_amount=Decimal("4000"),
    sell_venue=Venue.BINANCE, sell_asset="ETH", sell_amount=Decimal("2"),
)
# {"can_execute": True, "buy_venue_available": ..., "sell_venue_available": ..., "reason": None}

# Cross-venue skew
skew = tracker.skew("ETH")
# {"asset": "ETH", "total": Decimal("15"), "max_deviation_pct": 40.0, "needs_rebalance": True, ...}

# Portfolio snapshot
snap = tracker.snapshot()
# {"timestamp": datetime, "venues": {"binance": {...}, "wallet": {...}}, "totals": {...}}
```

### RebalancePlanner

```python
from inventory.rebalancer import RebalancePlanner, TRANSFER_FEES, MIN_OPERATING_BALANCE

planner = RebalancePlanner(tracker)         # threshold_pct=30.0 by default

# Check skew across all assets
for s in planner.check_all():
    print(f"{s['asset']}: {s['max_deviation_pct']:.1f}% deviation — "
          f"{'REBALANCE' if s['needs_rebalance'] else 'ok'}")

# Plan transfers for a specific asset
plans = planner.plan("ETH")
# [TransferPlan(from_venue=BINANCE, to_venue=WALLET, amount=4.0, estimated_fee=0.005, ...)]
print(plans[0].net_amount)   # 3.995 ETH arrives at destination

# Estimate total cost
cost = planner.estimate_cost(plans)
# {"total_transfers": 1, "total_fees_usd": Decimal("0.005"), "total_time_min": 15, ...}

# Plan all unbalanced assets at once
all_plans = planner.plan_all()   # {asset: [TransferPlan, ...]}
```

```bash
python -m inventory.rebalancer --check
# Asset     Total          Max Dev %   Needs Rebal
# --------------------------------------------------
# ETH       10.0000             40.0           YES
# USDT   10000.0000             40.0           YES

python -m inventory.rebalancer --plan ETH
# Transfer plans for ETH:
#   [1] binance → wallet: 4.0 ETH  (fee=0.005, net=3.995, ~15min)
```

### PnLEngine — Arb Trade Ledger

```python
from inventory.pnl import PnLEngine, ArbRecord, TradeLeg
from inventory.tracker import Venue
from datetime import UTC, datetime
from decimal import Decimal

engine = PnLEngine()

buy_leg = TradeLeg(
    id="buy-1", timestamp=datetime.now(UTC), venue=Venue.WALLET,
    symbol="ETH/USDT", side="buy", amount=Decimal("1"),
    price=Decimal("2000"), fee=Decimal("0.40"), fee_asset="USDT",
)
sell_leg = TradeLeg(
    id="sell-1", timestamp=datetime.now(UTC), venue=Venue.BINANCE,
    symbol="ETH/USDT", side="sell", amount=Decimal("1"),
    price=Decimal("2002"), fee=Decimal("0.40"), fee_asset="USDT",
)
record = ArbRecord(id="arb-1", timestamp=datetime.now(UTC),
                   buy_leg=buy_leg, sell_leg=sell_leg, gas_cost_usd=Decimal("0.20"))

print(record.gross_pnl)    # 2.00
print(record.total_fees)   # 1.00
print(record.net_pnl)      # 1.00
print(record.net_pnl_bps)  # 5.0 bps

engine.record(record)
s = engine.summary()
# {total_trades, total_pnl_usd, win_rate, sharpe_estimate, pnl_by_hour, ...}

engine.export_csv("trades.csv")
```

```bash
python -m inventory.pnl --summary
```

```
PnL Summary (demo)
═════════════════════════════════════════════
Total Trades:             4
Win Rate:            75.0%
Total PnL:           $1.15
Total Fees:          $3.20
Avg PnL/Trade:       $0.29
Avg PnL (bps):        1.4 bps
Best Trade:          $1.10
Worst Trade:         -$1.40
Total Notional:   $8,004.00
Sharpe (rough):        1.23
```

### ArbChecker — Full Pipeline

```python
from integration.arb_checker import ArbChecker, SimplePricingAdapter
from inventory.tracker import InventoryTracker, Venue
from inventory.pnl import PnLEngine
from exchange.client import ExchangeClient
from decimal import Decimal

# Wire up components
pricing = SimplePricingAdapter(
    price=Decimal("2007.21"),
    price_impact_bps=Decimal("1.2"),
    fee_bps=Decimal("30"),
)
cex_client = ExchangeClient(config)
tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
tracker.update_from_cex(Venue.BINANCE, cex_client.fetch_balance())
tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("10"), "USDT": Decimal("20000")})

checker = ArbChecker(
    pricing_engine=pricing,
    exchange_client=cex_client,
    inventory_tracker=tracker,
    pnl_engine=PnLEngine(),
)

result = checker.check("ETH/USDT", size=2.0, gas_price_gwei=20)
# {
#   "pair": "ETH/USDT",
#   "direction": "buy_dex_sell_cex",   # or "buy_cex_sell_dex" or None
#   "gap_bps": Decimal("38.8"),
#   "estimated_costs_bps": Decimal("44.1"),
#   "estimated_net_pnl_bps": Decimal("-5.3"),
#   "inventory_ok": True,
#   "executable": False,               # gap < costs
#   "details": {
#       "dex_fee_bps": Decimal("30"),
#       "dex_price_impact_bps": Decimal("1.2"),
#       "cex_fee_bps": Decimal("10"),
#       "cex_slippage_bps": Decimal("0.4"),
#       "gas_cost_usd": Decimal("6.00"),
#   }
# }
```

```bash
python -m integration.arb_checker ETH/USDT --size 2.0

═══════════════════════════════════════════
  ARB CHECK: ETH/USDT (size: 2.0 ETH)
═══════════════════════════════════════════

Prices:
  DEX (execution):      $2,007.21
  CEX best bid:         $2,015.00
  CEX best ask:         $2,015.50

Gap: 38.8 bps  [buy dex sell cex]

Costs:
  DEX fee:              30.0 bps
  DEX price impact:      1.2 bps
  CEX fee:              10.0 bps
  CEX slippage:          0.4 bps
  Gas:               $6.00
  ──────────────────────────────
  Total costs:          44.1 bps

Net PnL estimate: -5.3 bps  ❌ NOT PROFITABLE

Inventory:
  Pre-flight check:  ✅

Verdict: SKIP — costs exceed gap
═══════════════════════════════════════════
```

---

## Week 4: Live Trading Infrastructure

### BybitClient — ccxt-backed Bybit Adapter

```python
from exchange.bybit_client import BybitClient

client = BybitClient({
    "apiKey": "YOUR_BYBIT_KEY",  # pragma: allowlist secret
    "secret": "YOUR_BYBIT_SECRET",  # pragma: allowlist secret
    "sandbox": True,
})

# Order book
book = client.fetch_order_book("ETH/USDT", limit=25)
# {symbol, bids: [(price, qty), ...], asks: [...], timestamp}

# Balances
balances = client.fetch_balance()
# {"ETH": {"free": Decimal, "locked": Decimal, "total": Decimal}, ...}

# Place / cancel
order = client.create_limit_order("ETH/USDT", "buy", qty=0.1, price=2000.0)
client.cancel_order(order["id"], "ETH/USDT")

# Trading fees
fees = client.get_trading_fees("ETH/USDT")
# {"maker": Decimal("0.001"), "taker": Decimal("0.001")}
```

### OrderBookStream — Binance WebSocket Depth

Implements the [Binance depth stream sync protocol](https://developers.binance.com/docs/binance-spot-api-docs/web-socket-streams#how-to-manage-a-local-order-book-correctly):
1. Open WebSocket stream `wss://.../ws/<symbol>@depth`
2. Fetch REST snapshot via `GET /api/v3/depth`
3. Discard diffs with `U ≤ lastUpdateId`; apply the first diff where `U ≤ lastUpdateId+1 ≤ u`

```python
import asyncio
from exchange.ws_orderbook import OrderBookStream

async def main():
    stream = OrderBookStream("ETH/USDT", testnet=True, depth_limit=20)
    await stream.connect()          # opens WS + fetches REST snapshot

    async for book in stream:       # yields on each depth update
        print(f"mid={book['mid_price']:.2f}  spread={book['spread_bps']:.1f} bps")

asyncio.run(main())
```

```bash
# Stream 10 updates then exit
python -m exchange.ws_orderbook ETH/USDT --count 10
```

```
ETH/USDT — live depth stream (testnet)
update #1  mid=2,010.25  spread=2.5 bps  bids=20  asks=20
update #2  mid=2,010.30  spread=2.4 bps  bids=20  asks=20
...
```

### InventoryDashboard — Rich Terminal UI

```python
from inventory.dashboard import InventoryDashboard
from inventory.tracker import InventoryTracker, Venue

tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])
dash = InventoryDashboard(tracker)

dash.render()           # print one snapshot
dash.run(refresh_interval=5)   # live loop, Ctrl-C to exit
```

```bash
python -m inventory.dashboard --once         # single snapshot
python -m inventory.dashboard --interval 3   # refresh every 3 s
```

```
┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓
┃  Inventory Dashboard  —  2024-01-15 14:30:00 UTC                    ┃
┡━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┩
│ Asset    │  Binance (free) │  Wallet         │  Total           │
│ ETH      │          3.0000 │         7.0000  │        10.0000   │
│ USDT     │       6000.0000 │      4000.0000  │     10000.0000   │
├──────────┴─────────────────┴─────────────────┴──────────────────┤
│ Skew: ETH  40.0% deviation  ⚠ REBALANCE NEEDED                  │
│ Skew: USDT 20.0% deviation  ✓                                    │
└──────────────────────────────────────────────────────────────────┘
```

### PnLCharts — Matplotlib Visualizations

```python
from inventory.charts import PnLCharts
from inventory.pnl import PnLEngine

engine = PnLEngine()
# ... record trades ...

charts = PnLCharts(engine)
charts.plot_cumulative_pnl(output_path="charts/cumulative_pnl.png")
charts.plot_drawdown(output_path="charts/drawdown.png")
charts.plot_pnl_by_hour(output_path="charts/hourly.png")
charts.plot_all(output_dir="charts/")
```

```bash
python -m inventory.charts --output charts/
# Saved: charts/cumulative_pnl.png
# Saved: charts/drawdown.png
# Saved: charts/pnl_by_hour.png
```

### ArbLogger — Ring Buffer + CSV Export

```python
from integration.arb_logger import ArbLogger

logger = ArbLogger(max_records=1000)

# Log arb check results
logger.log(result)          # result from ArbChecker.check()
logger.log(result2)

# Stats
stats = logger.stats()
# {total, executable_count, executable_pct, avg_gap_bps, avg_net_pnl_bps,
#  best_opportunity, worst_opportunity}

# Export
logger.export_csv("arb_log.csv")

# Recent (last N)
recent = logger.recent(10)
```

```bash
# Continuously poll and log arb opportunities
python -m integration.arb_logger ETH/USDT --interval 5 --count 20
```

### PricingEngineAdapter — Wiring Week 2 into ArbChecker

`PricingEngineAdapter` bridges the Week 2 `PricingEngine` (raw int AMM math) to the
`{price, price_impact_bps, fee_bps}` dict that `ArbChecker` expects:

```python
from integration.arb_checker import ArbChecker, PricingEngineAdapter
from pricing.engine import PricingEngine
from core.types import Token, Address

# Set up the real on-chain pricing engine
engine = PricingEngine(chain_client=client, fork_url="http://127.0.0.1:8545")
engine.load_pools([Address("0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc")])

WETH = Token(address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),
             symbol="WETH", decimals=18)
USDC = Token(address=Address("0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
             symbol="USDC", decimals=6)

# Wrap it
adapter = PricingEngineAdapter(
    engine=engine,
    token_in=WETH,
    token_out=USDC,
    decimals_in=18,
    decimals_out=6,
    dex_fee_bps=30,
    gas_price_gwei=20,
)

# Wire into ArbChecker — now uses real AMM quotes
checker = ArbChecker(
    pricing_engine=adapter,
    exchange_client=cex_client,
    inventory_tracker=tracker,
)

result = checker.check("ETH/USDT", size=2.0)
# execution_price comes from PricingEngine.get_quote() → Quote.expected_output
# price_impact_bps = (simulated_output - expected_output) / simulated_output × 10000
```

### Lab 4 Full Pipeline Demo

```bash
python scripts/lab4_demo.py
```

Runs entirely offline (no API keys needed) — mocks CEX and WebSocket calls:

```
════════════════════════════════════════════════════
  Lab 4 Full Pipeline Demo
════════════════════════════════════════════════════

[1] InventoryTracker — multi-venue balances ...
    ETH: binance=3.00, wallet=7.00, total=10.00
    USDT: binance=6000.00, wallet=4000.00, total=10000.00

[2] RebalancePlanner — skew check ...
    ETH — 40.0% deviation — REBALANCE NEEDED
    Plan: binance→wallet 2.0 ETH  fee=0.005 ETH  net=1.995 ETH

[3] PnLEngine — arb trade ledger ...
    Net PnL: $1.00  (5.0 bps)  win_rate=75.0%

[4] ArbChecker — full pipeline ...
    gap=38.8 bps  costs=44.1 bps  net=-5.3 bps  executable=False

[5] OrderBookAnalyzer — walk the book ...
    mid=2010.25  spread=2.49 bps  depth_bid=47.2 ETH

[6] ArbLogger — ring buffer ...
    Logged 3 results. executable=1 (33.3%)  avg_gap=35.0 bps

[7] InventoryDashboard — terminal UI ...
    [Rich table printed]

[8] PnLCharts — matplotlib (skipped — no display) ...

[9] BybitClient — ccxt adapter ...
    Fetched mock order book: best_bid=2009.50, best_ask=2010.00

[10] OrderBookStream — WebSocket depth ...
     Snapshot applied: 5 bids, 5 asks, last_update_id=100

════════════════════════════════════════════════════
  All 10 sections completed successfully
════════════════════════════════════════════════════
```

---

## Week 4: Strategy & Execution (Arbitrage Bot)

### Architecture

```
Live prices (Binance REST/WS)
        │
        ▼
┌───────────────────┐     ┌──────────────────┐
│  SignalGenerator  │────▶│  SignalScorer     │
│  strategy/        │     │  strategy/        │
│  _fetch_prices()  │     │  spread · liq     │
│  spread calc      │     │  inventory · hist │
└───────────────────┘     └────────┬─────────┘
                                   │ score 0-100
                                   ▼
                          ┌──────────────────┐
                          │   SignalQueue     │  max-heap, thread-safe
                          │   executor/       │  evicts lowest score
                          └────────┬─────────┘
                                   │ highest score first
                                   ▼
                          ┌──────────────────────────────────────┐
                          │           Executor                    │
                          │  IDLE → VALIDATING → LEG1_PENDING    │
                          │       → LEG1_FILLED → LEG2_PENDING   │
                          │       → DONE / FAILED / UNWINDING    │
                          │                                       │
                          │  CEX-first  OR  DEX-first (Flashbots)│
                          │  retry + backoff + idempotency key    │
                          │  ERC-20 approve → swap → receipt      │
                          └────────┬──────────────────────────────┘
                                   │
                   ┌───────────────┼────────────────┐
                   ▼               ▼                ▼
          CircuitBreaker    ReplayProtection    PnLEngine
          sliding window    TTL-keyed dict      ArbRecord
          half-open probe   60s expiry          summary()
          webhook alert
```

### Signal → Score → Execute flow

| Stage | Code | What happens |
|---|---|---|
| Price fetch | `generator._fetch_prices()` | CEX order book (Binance) + DEX reserves (Uniswap V2 via `UniswapDirectPricer`) |
| Signal | `generator.generate()` | Spread calc, fee check, inventory pre-flight; emits `Signal` with `Decimal` economics |
| Score | `scorer.score()` | 4-factor 0–100: spread (40%), liquidity bid-ask (20%), inventory (20%), win-rate history (20%) |
| Queue | `SignalQueue.put()` | Max-heap by score; evicts lowest when full; skips expired on dequeue |
| Execute | `executor.execute()` | State machine; retry with 50ms→100ms→200ms backoff; idempotency key; slippage tracking |
| Unwind | `executor._unwind()` | CEX market order or DEX reverse swap if leg 2 fails after leg 1 filled |
| Safety | `CircuitBreaker` | Trips after N failures in window; half-open probe after cooldown; webhook alert |
| Record | `pnl_engine.record()` | `execution_to_arb_record()` bridges `ExecutionContext` → `ArbRecord` |

### Operating Modes

#### TEST (default) — safe development

```bash
python scripts/arb_bot.py --mode test
```

- Binance **testnet** (`sandbox=True`) — real order book data, no real orders
- `UniswapDirectPricer` queries live **mainnet** Uniswap V2 pool reserves for accurate DEX prices
- `simulation_mode=True` — execution always returns a mock fill, no real transactions
- No `PRIVATE_KEY` required

#### PROD — real execution

```bash
# Prerequisites
export PRIVATE_KEY=0x...
export BINANCE_API_KEY=...
export BINANCE_SECRET=...
export ETH_RPC_URL=https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY
# Optional: local Anvil fork for execution quote validation
anvil --fork-url $ETH_RPC_URL &
export FORK_URL=http://127.0.0.1:8545

python scripts/arb_bot.py --mode prod
```

- Binance **mainnet** — real orders placed
- Real DEX execution via Uniswap V2 (`swapExactETHForTokens` / `swapExactTokensForETH`)
- ERC-20 token approval sent automatically on first trade per token
- `PRIVATE_KEY` required; wallet loaded via `WalletManager.from_env()`

### Demo

```bash
# Stub DEX prices (always works, no keys needed)
python scripts/demo_week4.py
python scripts/demo_week4.py --ticks 15 --interval 2
python scripts/demo_week4.py --ticks 0                   # until Ctrl+C

# Real Uniswap V2 DEX prices (needs ETH_RPC_URL)
python scripts/demo_week4.py --rpc-url https://ethereum.publicnode.com
python scripts/demo_week4.py --rpc-url https://ethereum.publicnode.com --pairs ETH/USDT --ticks 10
```

Live terminal shows: bid/ask prices, signal score breakdown (spread · liquidity · inventory · history), priority queue ordering, execution fills, latency, running PnL, circuit breaker status.

### Key components

```
strategy/
  signal.py       — Signal dataclass; is_valid(); bid_ask_spread_bps for liquidity scoring
  fees.py         — FeeStructure; all Decimal arithmetic; breakeven_spread_bps()
  generator.py    — SignalGenerator; _fetch_prices → spread calc → inventory check
  scorer.py       — SignalScorer; 4-factor weighted score; apply_decay(); record_result()

executor/
  engine.py       — Executor state machine; CEX retry+backoff; DEX swap routing;
                    ERC-20 approve; slippage tracking; _unwind_dex_leg_sync()
  recovery.py     — CircuitBreaker (sliding window + half-open); ReplayProtection (TTL)
  queue.py        — SignalQueue (thread-safe max-heap; evict-lowest on full)

pricing/
  uniswap_direct.py — UniswapDirectPricer; getReserves via eth_call; AMM formula;
                      get_token() / get_quote() interface for SignalGenerator

monitoring/
  metrics.py      — Prometheus counters/histograms: signals, PnL, slippage, retries,
                    circuit breaker, inventory balances, execution latency

scripts/
  arb_bot.py      — ArbBot; MODE_TEST / MODE_PROD; concurrent price fetch (asyncio.gather);
                    apply_decay in drain loop; INVENTORY_BALANCE gauge; wallet balance sync
  demo_week4.py   — Rich live terminal demo; stub or real Uniswap DEX prices
```

### Prometheus metrics

| Metric | Type | What it measures |
|---|---|---|
| `arb_signals_generated_total` | Counter | Signals produced per pair |
| `arb_signals_skipped_total` | Counter | Skipped (low score / decayed) |
| `arb_trades_executed_total` | Counter | Executions by outcome (done/failed) |
| `arb_execution_slippage_bps` | Histogram | Actual vs expected fill price per leg |
| `arb_execution_latency_seconds` | Histogram | Wall-clock time per execution |
| `arb_pnl_usd` | Histogram | Net PnL distribution per trade |
| `arb_signal_score` | Histogram | Score distribution at execution |
| `arb_circuit_breaker_open` | Gauge | 1 if open, 0 if closed |
| `arb_circuit_breaker_trips_total` | Counter | Total trips |
| `arb_replay_blocks_total` | Counter | Duplicate signals blocked |
| `arb_unwinds_total` | Counter | Unwind operations triggered |
| `arb_inventory_balance` | Gauge | Balance per venue per asset |
| `arb_cex_retry_total` | Counter | CEX order retries (transient failures) |

Start scraping: `python scripts/arb_bot.py --mode test` with `metrics_port: 8000` in config, then `curl http://localhost:8000/metrics`.

---

## Running Tests

```bash
make test                                            # run all 1773 tests (97% coverage)

# Week 1 — core, chain
python -m pytest tests/test_wallet.py tests/test_types.py tests/test_serializer.py -v
python -m pytest tests/test_client.py tests/test_builder.py tests/test_analyzer.py -v

# Week 2 — pricing
python -m pytest tests/test_amm.py tests/test_router.py tests/test_pricing_engine.py -v
python -m pytest tests/test_fork_simulator.py tests/test_arbitrage.py -v

# Week 3 — exchange, inventory, integration
python -m pytest tests/test_exchange_client.py -v   # CEX client (95 tests)
python -m pytest tests/test_orderbook.py -v         # order book analysis
python -m pytest tests/test_multi_venue_tracker.py  # inventory tracking
python -m pytest tests/test_rebalancer.py -v        # rebalance planner
python -m pytest tests/test_pnl.py -v               # P&L engine
python -m pytest tests/test_arb_checker.py -v       # integration pipeline

# Week 4 — strategy & execution
pytest tests/test_signal.py tests/test_fees.py tests/test_generator.py -v
pytest tests/test_scorer.py tests/test_executor.py tests/test_recovery.py -v
pytest tests/test_stretch_goals.py -v               # queue + metrics + webhook
pytest tests/test_integration_week4.py -m "not network" -v  # full pipeline (mocked)
pytest tests/test_integration_week4.py -m network -v -s     # real Binance prices

# Week 4 — live trading infrastructure (stretch)
python -m pytest tests/test_bybit_client.py -v      # BybitClient (52 tests)
python -m pytest tests/test_ws_orderbook.py -v      # WebSocket stream (54 tests)
python -m pytest tests/test_dashboard.py -v         # Rich terminal UI (26 tests)
python -m pytest tests/test_charts.py -v            # PnL charts (25 tests)
python -m pytest tests/test_arb_logger.py -v        # ArbLogger (58 tests)

# Filter
python -m pytest -k "test_security" -v              # by name pattern
python -m pytest --tb=short --cov=. --cov-report=term-missing  # with coverage
```

---

## Integration Test

Tests the full pipeline on Sepolia testnet:

```bash
# Full test (sends a real transaction)
PRIVATE_KEY=0x... python scripts/integration_test.py \
  --rpc https://sepolia.infura.io/v3/YOUR_KEY

# Dry run — build and sign but don't send
PRIVATE_KEY=0x... python scripts/integration_test.py --dry-run
```

**What it tests:**
1. Loads wallet from `PRIVATE_KEY` env var
2. Connects to Sepolia and checks balance
3. Builds a `0.001 ETH` transfer to the burn address
4. Estimates gas with 1.2x buffer
5. Signs and verifies the signature locally
6. Broadcasts to Sepolia
7. Polls until confirmed (up to 120s)
8. Runs the transaction analyzer on the receipt

---

## Make Commands

| Command | What it does |
|---|---|
| `make install` | Install all dependencies |
| `make test` | Run all 1773 tests (97% coverage) |
| `make lint` | Lint with ruff |
| `make format` | Auto-format with ruff |
| `make pre-commit-install` | Wire up git hooks |
| `make clean` | Remove cache files |

---

## Security

| Rule | Enforcement |
|---|---|
| Private key never in code | `_SecretStr` wrapper — `repr`/`str`/`format` return `***` |
| Private key never in git | `.gitignore` blocks `.env`; `detect-secrets` scans every commit |
| Secrets scanned on commit | pre-commit hook runs `check_secrets_baseline.py` |
| Tests use public test keys | Hardhat account #0 — known public, zero funds |
| CI repeats all checks | GitHub Actions runs on every push |

---

## Limitations & Assumptions

- Function argument decoding uses simplified 32-byte chunk parsing — complex types (dynamic arrays, tuples) show raw hex
- `ChainClient` uses HTTP polling; WebSocket support is a stretch goal
- Token metadata in the analyzer is fetched lazily and cached per process run (not persisted)
- Integration test sends to the Sepolia burn address (`0x000...dEaD`) — safe for testing
- Python 3.13+ required (uses `match` statements and `X | Y` type union syntax)
