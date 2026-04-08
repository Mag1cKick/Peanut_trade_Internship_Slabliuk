# Peanut Trade — Weeks 1 & 2: Core, Chain & Pricing Modules

> Arbitrage trading system foundation — wallet management, blockchain interaction,
> transaction building, analysis, and on-chain pricing with routing and simulation.

---

## Quick Start

```bash
# 1. Clone
git clone <your-repo-url>
cd Peanut_trade_Internship_Slabliuk

# 2. Install dependencies
make install

# 3. Configure secrets
cp .env.example .env
# Edit .env — add your PRIVATE_KEY and RPC_URL

# 4. Run tests
make test

# 5. Analyze a real mainnet transaction
python -m chain.analyzer 0xb5c8bd9430b6cc87a0e2fe110ece6bf527fa4f170a4bc8cd032f768fc5219838 \
  --rpc https://eth-mainnet.g.alchemy.com/v2/YOUR_KEY

# 6. Run integration test on Sepolia
PRIVATE_KEY=0x... python scripts/integration_test.py \
  --rpc https://sepolia.infura.io/v3/YOUR_KEY
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
│   ├── impact_analyzer.py       # PriceImpactAnalyzer — slippage tables, max trade size
│   ├── router.py                # Route + RouteFinder — multi-hop DFS with gas-adjusted best route
│   ├── mempool.py               # MempoolMonitor + ParsedSwap — live pending swap decoding
│   ├── fork_simulator.py        # ForkSimulator — eth_call against local Anvil fork
│   └── engine.py                # PricingEngine — unified interface + Quote + QuoteError
│
├── exchange/                    # Week 3 placeholder
├── inventory/                   # Week 3 placeholder
├── strategy/                    # Week 4 placeholder
├── executor/                    # Week 4 placeholder
├── safety/                      # Week 5 placeholder
├── config/                      # Week 5 placeholder
│
├── tests/                       # 643 unit tests, all passing
│   ├── test_wallet.py           # 37 tests — key loading, security, signing
│   ├── test_serializer.py       # 55 tests — determinism, unicode, edge cases
│   ├── test_types.py            # 68 tests — validation, arithmetic, equality
│   ├── test_client.py           # 44 tests — retry logic, error classification
│   ├── test_builder.py          # 55 tests — fluent API, validation
│   ├── test_analyzer.py         # 47 tests — decoding, parsing, CLI
│   ├── test_amm.py              # 65 tests — AMM math, Solidity vector, precision
│   ├── test_impact_analyzer.py  # 50 tests — slippage tables, binary search, CLI
│   ├── test_router.py           # 38 tests — DFS routing, gas flip, sequential match
│   ├── test_mempool.py          # 36 tests — calldata decoding, async monitor
│   ├── test_fork_simulator.py   # 27 tests — mocked eth_call, reserve fetch
│   └── test_pricing_engine.py   # 37 tests — integration, quote validity
│
├── scripts/
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

## Running Tests

```bash
make test                          # run all 643 tests
python -m pytest tests/test_wallet.py -v     # one module
python -m pytest -k "test_security" -v       # by name pattern
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
| `make test` | Run all 342 tests |
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
