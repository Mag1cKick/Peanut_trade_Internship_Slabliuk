"""
pricing/uniswap_direct.py — Lightweight Uniswap V2 pricer via raw JSON-RPC.

Queries pool reserves directly with a single eth_call (getReserves selector
0x0902f1ac) and applies the standard AMM formula.  No fork simulator, no
load_pools() call, no WebSocket subscription required — just an RPC URL.

Implements get_token() and get_quote() so it is a drop-in replacement for
PricingEngine in SignalGenerator when the full pricing stack is unavailable.

Supported networks (pre-built configs):
  ETHEREUM  — mainnet Uniswap V2 (gas $5-15/swap, use for price data only)
  ARBITRUM  — Arbitrum One Uniswap V2 (gas <$0.01/swap, use for execution)

Usage:
    from pricing.uniswap_direct import UniswapDirectPricer, ARBITRUM

    pricer = UniswapDirectPricer(rpc_url="https://arb1.arbitrum.io/rpc",
                                 network=ARBITRUM)
    generator = SignalGenerator(..., pricing_module=pricer, ...)
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass, field

# ---------------------------------------------------------------------------
# Token and quote descriptors
# ---------------------------------------------------------------------------


@dataclass
class DirectToken:
    """Minimal token descriptor compatible with SignalGenerator._get_token()."""

    symbol: str
    decimals: int
    address: str = ""  # checksummed ERC-20 address; empty = native gas token


@dataclass
class DirectQuote:
    """Minimal quote descriptor compatible with SignalGenerator._dex_prices_from_engine()."""

    expected_output: int


# ---------------------------------------------------------------------------
# Network configuration
# ---------------------------------------------------------------------------


@dataclass
class NetworkConfig:
    """
    All chain-specific constants needed for a Uniswap V2 deployment.
    Pool addresses are discovered at runtime from the factory if not pre-seeded.
    """

    name: str
    chain_id: int
    router: str  # Uniswap V2 Router address
    factory: str  # Uniswap V2 Factory address (for getPair discovery)
    tokens: dict[str, DirectToken] = field(default_factory=dict)
    # Pre-seeded pool addresses: (base, quote) → pair address
    # Leave empty to let the pricer discover pools via factory.getPair()
    pools: dict[tuple[str, str], str] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Pre-built network configs
# ---------------------------------------------------------------------------

ETHEREUM = NetworkConfig(
    name="ethereum",
    chain_id=1,
    router="0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D",
    factory="0x5C69bEe701ef814a2B6a3EDD4B1652CB9cc5aA6f",
    tokens={
        "ETH": DirectToken("ETH", 18, "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"),  # WETH
        "USDT": DirectToken("USDT", 6, "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        "USDC": DirectToken("USDC", 6, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        "WBTC": DirectToken("WBTC", 8, "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
        "DAI": DirectToken("DAI", 18, "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
    },
    pools={
        # WETH(0xC02) / USDT(0xdAC) — WETH < USDT → WETH=token0
        ("ETH", "USDT"): "0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
        # USDC(0xA0b) / WETH(0xC02) — USDC < WETH → USDC=token0
        ("ETH", "USDC"): "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
    },
)

ARBITRUM = NetworkConfig(
    name="arbitrum",
    chain_id=42161,
    router="0x4752ba5dbc23f44d87826276bf6fd6b1c372ad24",
    factory="0xf1D7CC64Fb4452F05c498126312eBE29f30Fbcf9",
    tokens={
        "ETH": DirectToken("ETH", 18, "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),  # WETH
        "USDC": DirectToken("USDC", 6, "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),  # native USDC
        "USDT": DirectToken("USDT", 6, "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9"),
        "WBTC": DirectToken("WBTC", 8, "0x2f2a2543B76A4166549F7aaB2e75Bef0aefC5B0f"),
        "DAI": DirectToken("DAI", 18, "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1"),
        "ARB": DirectToken("ARB", 18, "0x912CE59144191C1204E64559FE8253a0e49E6548"),
    },
    # Pools are discovered via factory.getPair() at runtime — no hardcoding needed.
    pools={},
)

# SushiSwap on Arbitrum — same ABI as Uniswap V2, more liquidity for some pairs
ARBITRUM_SUSHI = NetworkConfig(
    name="arbitrum-sushi",
    chain_id=42161,
    router="0x1b02dA8Cb0d097eB8D57A175b88c7D8b47997506",
    factory="0xc35DADB65012eC5796536bD9864eD8773aBc74C4",
    tokens=ARBITRUM.tokens,
    pools={},
)


# ---------------------------------------------------------------------------
# Pricer
# ---------------------------------------------------------------------------


class UniswapDirectPricer:
    """
    Drop-in pricing_module for SignalGenerator backed by live Uniswap V2
    reserve queries.  Supports Ethereum mainnet and Arbitrum One out of the box.
    Pool addresses are discovered at runtime via factory.getPair() and cached.

    Usage:
        # Arbitrum (default for Week 5 — gas costs pennies)
        pricer = UniswapDirectPricer(rpc_url, network=ARBITRUM)

        # Ethereum mainnet (price data only — swaps cost $5-15)
        pricer = UniswapDirectPricer(rpc_url, network=ETHEREUM)
    """

    def __init__(
        self,
        rpc_url: str,
        network: NetworkConfig | None = None,
    ) -> None:
        self.rpc_url = rpc_url
        self.network = network or ARBITRUM
        # Runtime pool cache: populated lazily via factory.getPair()
        self._pool_cache: dict[tuple[str, str], str] = dict(self.network.pools)
        # token0 address cache per pool (to resolve reserve ordering)
        self._token0_cache: dict[str, str] = {}

    @property
    def router(self) -> str:
        return self.network.router

    # ------------------------------------------------------------------
    # SignalGenerator interface
    # ------------------------------------------------------------------

    def get_token(self, symbol: str) -> DirectToken:
        if symbol not in self.network.tokens:
            raise ValueError(
                f"UniswapDirectPricer ({self.network.name}): " f"unknown token '{symbol}'"
            )
        return self.network.tokens[symbol]

    def get_quote(
        self,
        token_in: DirectToken,
        token_out: DirectToken,
        amount_in: int,
        gas_price: int = 1,
    ) -> DirectQuote:
        """
        Return a DirectQuote for swapping amount_in of token_in to token_out.
        Uses the standard Uniswap V2 AMM formula with 0.3% fee (997/1000).
        Pool address is looked up via factory.getPair() if not cached.
        """
        pool_addr = self._resolve_pool(token_in.symbol, token_out.symbol)
        r0, r1 = self._get_reserves(pool_addr)
        reserve_in, reserve_out = self._order_reserves(pool_addr, token_in.address, r0, r1)
        amount_out = (reserve_out * amount_in * 997) // (reserve_in * 1000 + amount_in * 997)
        return DirectQuote(expected_output=amount_out)

    # ------------------------------------------------------------------
    # Extra helper used by the demo and executor
    # ------------------------------------------------------------------

    def get_prices_for_pair(self, pair: str, size: float) -> tuple[float, float]:
        """
        Return (dex_buy, dex_sell) in quote-currency-per-base-token units.

        dex_sell — effective price received when selling `size` base on Uniswap
        dex_buy  — effective cost when buying `size` base on Uniswap
        """
        base, quote = pair.split("/")
        token_base = self.get_token(base)
        token_quote = self.get_token(quote)

        pool_addr = self._resolve_pool(base, quote)
        r0, r1 = self._get_reserves(pool_addr)
        reserve_base, reserve_quote = self._order_reserves(pool_addr, token_base.address, r0, r1)

        size_raw = int(size * 10**token_base.decimals)

        # Sell base → receive quote
        out_raw = (reserve_quote * size_raw * 997) // (reserve_base * 1000 + size_raw * 997)
        dex_sell = out_raw / (10**token_quote.decimals * size)

        # Buy base ← pay quote  (AMM getAmountIn)
        num = reserve_quote * size_raw * 1000
        den = (reserve_base - size_raw) * 997
        if den <= 0:
            dex_buy = dex_sell * 1.003
        else:
            in_raw = num // den + 1
            dex_buy = in_raw / (10**token_quote.decimals * size)

        return dex_buy, dex_sell

    # ------------------------------------------------------------------
    # Pool discovery
    # ------------------------------------------------------------------

    def _resolve_pool(self, base: str, quote: str) -> str:
        """
        Return the pool address for (base, quote), discovering it via
        factory.getPair() if not already cached.
        """
        key = (base, quote)
        if key in self._pool_cache:
            return self._pool_cache[key]

        token_a = self.get_token(base)
        token_b = self.get_token(quote)
        addr = self._get_pair_from_factory(token_a.address, token_b.address)

        if addr == "0x" + "0" * 40:
            raise ValueError(
                f"UniswapDirectPricer ({self.network.name}): "
                f"no pool found for {base}/{quote} on factory {self.network.factory}"
            )

        self._pool_cache[key] = addr
        self._pool_cache[(quote, base)] = addr  # also cache reverse direction
        return addr

    def _get_pair_from_factory(self, token_a: str, token_b: str) -> str:
        """
        Call factory.getPair(tokenA, tokenB) → pair address.
        Selector: keccak256("getPair(address,address)")[:4] = 0xe6a43905
        """

        def _pad(addr: str) -> str:
            return addr.lower().replace("0x", "").zfill(64)

        data = "0xe6a43905" + _pad(token_a) + _pad(token_b)
        result = self._eth_call(self.network.factory, data)
        # Result is ABI-encoded address: 32 bytes, last 20 bytes are the address
        addr = "0x" + result[-40:]
        return addr

    def _order_reserves(
        self,
        pool_addr: str,
        token_in_address: str,
        r0: int,
        r1: int,
    ) -> tuple[int, int]:
        """
        Return (reserve_in, reserve_out) based on whether token_in is token0.
        Caches the token0 address per pool to avoid repeated RPC calls.
        """
        token0 = self._get_token0(pool_addr)
        if token_in_address.lower() == token0.lower():
            return r0, r1
        return r1, r0

    def _get_token0(self, pool_addr: str) -> str:
        """
        Call pair.token0() → address.
        Selector: keccak256("token0()")[:4] = 0x0dfe1681
        Cached per pool address.
        """
        if pool_addr in self._token0_cache:
            return self._token0_cache[pool_addr]
        result = self._eth_call(pool_addr, "0x0dfe1681")
        addr = "0x" + result[-40:]
        self._token0_cache[pool_addr] = addr
        return addr

    # ------------------------------------------------------------------
    # RPC helpers
    # ------------------------------------------------------------------

    def _get_reserves(self, pool_addr: str) -> tuple[int, int]:
        """Call getReserves() → (reserve0, reserve1)."""
        data = self._eth_call(pool_addr, "0x0902f1ac")
        return int(data[0:64], 16), int(data[64:128], 16)

    def _eth_call(self, to: str, data: str) -> str:
        """
        Make a raw eth_call and return the hex result (without leading 0x).
        Raises ValueError on RPC error or empty result.
        """
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": to, "data": data}, "latest"],
            "id": 1,
        }
        req = urllib.request.Request(
            self.rpc_url,
            data=json.dumps(payload).encode(),
            headers={
                "Content-Type": "application/json",
                "User-Agent": "PeanutTrade/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            result = json.loads(resp.read())
        if "error" in result or not result.get("result"):
            raise ValueError(f"eth_call to {to} failed: {result.get('error', 'empty result')}")
        return result["result"][2:]  # strip 0x
