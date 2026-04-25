"""
pricing/uniswap_direct.py — Lightweight Uniswap V2 pricer via raw JSON-RPC.

Queries pool reserves directly with a single eth_call (getReserves selector
0x0902f1ac) and applies the standard AMM formula.  No fork simulator, no
load_pools() call, no WebSocket subscription required — just an RPC URL.

Implements get_token() and get_quote() so it is a drop-in replacement for
PricingEngine in SignalGenerator when the full pricing stack is unavailable.

Supported mainnet pools:
  ETH/USDT — Uniswap V2 WETH/USDT  0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852
  ETH/USDC — Uniswap V2 USDC/WETH  0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc
"""

from __future__ import annotations

import json
import urllib.request
from dataclasses import dataclass


@dataclass
class DirectToken:
    """Minimal token descriptor compatible with SignalGenerator._get_token()."""

    symbol: str
    decimals: int
    address: str = ""  # checksummed mainnet ERC-20 address; empty = native ETH


@dataclass
class DirectQuote:
    """Minimal quote descriptor compatible with SignalGenerator._dex_prices_from_engine()."""

    expected_output: int


class UniswapDirectPricer:
    """
    Drop-in pricing_module for SignalGenerator backed by live Uniswap V2
    reserve queries.  No local fork or pool pre-loading needed.

    Usage:
        pricer = UniswapDirectPricer(rpc_url="https://eth.llamarpc.com")
        generator = SignalGenerator(..., pricing_module=pricer, ...)
    """

    # WETH address is used for ETH legs — Uniswap V2 requires WETH for routing.
    WETH_ADDRESS = "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"

    TOKENS: dict[str, DirectToken] = {
        "ETH": DirectToken("ETH", 18, WETH_ADDRESS),
        "USDT": DirectToken("USDT", 6, "0xdAC17F958D2ee523a2206206994597C13D831ec7"),
        "USDC": DirectToken("USDC", 6, "0xA0b86991c6218b36c1d19D4a2e9Eb0cE3606eB48"),
        "WBTC": DirectToken("WBTC", 8, "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599"),
        "BNB": DirectToken("BNB", 18, "0xB8c77482e45F1F44dE1745F52C74426C631bDD52"),
        "DAI": DirectToken("DAI", 18, "0x6B175474E89094C44Da98b954EedeAC495271d0F"),
    }

    # (base, quote) → Uniswap V2 pair address.
    # Token ordering follows Uniswap V2 convention: lower address = token0.
    POOLS: dict[tuple[str, str], str] = {
        # WETH(0xC02) / USDT(0xdAC) — WETH < USDT → WETH=token0(r0), USDT=token1(r1)
        ("ETH", "USDT"): "0x0d4a11d5EEaaC28EC3F61d100daF4d40471f1852",
        # USDC(0xA0b) / WETH(0xC02) — USDC < WETH → USDC=token0(r0), WETH=token1(r1)
        ("ETH", "USDC"): "0xB4e16d0168e52d35CaCD2c6185b44281Ec28C9Dc",
    }

    # Which reserve index is the base (ETH) for each pool
    _BASE_IS_R1: set[tuple[str, str]] = {("ETH", "USDC")}

    def __init__(self, rpc_url: str) -> None:
        self.rpc_url = rpc_url

    # ------------------------------------------------------------------
    # SignalGenerator interface
    # ------------------------------------------------------------------

    def get_token(self, symbol: str) -> DirectToken:
        if symbol not in self.TOKENS:
            raise ValueError(f"UniswapDirectPricer: unknown token '{symbol}'")
        return self.TOKENS[symbol]

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
        """
        base = "ETH" if token_in.symbol == "ETH" or token_out.symbol == "ETH" else None
        if base is None:
            raise ValueError(
                f"UniswapDirectPricer: no direct pool for " f"{token_in.symbol}/{token_out.symbol}"
            )
        quote_sym = token_out.symbol if token_in.symbol == "ETH" else token_in.symbol
        pool_addr = self.POOLS.get(("ETH", quote_sym))
        if pool_addr is None:
            raise ValueError(f"UniswapDirectPricer: no pool for ETH/{quote_sym}")

        r0, r1 = self._get_reserves(pool_addr)
        base_is_r1 = ("ETH", quote_sym) in self._BASE_IS_R1

        if token_in.symbol == "ETH":
            reserve_in = r1 if base_is_r1 else r0
            reserve_out = r0 if base_is_r1 else r1
        else:
            reserve_in = r0 if base_is_r1 else r1
            reserve_out = r1 if base_is_r1 else r0

        amount_out = (reserve_out * amount_in * 997) // (reserve_in * 1000 + amount_in * 997)
        return DirectQuote(expected_output=amount_out)

    # ------------------------------------------------------------------
    # Extra helper used by the demo
    # ------------------------------------------------------------------

    def get_prices_for_pair(self, pair: str, size: float) -> tuple[float, float]:
        """
        Return (dex_buy, dex_sell) in quote-currency-per-base-token units.

        dex_sell — effective price received when selling `size` base on Uniswap
        dex_buy  — effective cost when buying `size` base on Uniswap
        """
        base, quote = pair.split("/")
        pool_addr = self.POOLS.get((base, quote))
        if pool_addr is None:
            raise ValueError(f"UniswapDirectPricer: no pool for {pair}")

        r0, r1 = self._get_reserves(pool_addr)
        base_dec = self.TOKENS[base].decimals
        quote_dec = self.TOKENS[quote].decimals
        base_is_r1 = (base, quote) in self._BASE_IS_R1

        reserve_base = r1 if base_is_r1 else r0
        reserve_quote = r0 if base_is_r1 else r1

        size_raw = int(size * 10**base_dec)

        # Sell base → receive quote
        out_raw = (reserve_quote * size_raw * 997) // (reserve_base * 1000 + size_raw * 997)
        dex_sell = out_raw / (10**quote_dec * size)

        # Buy base ← pay quote  (AMM getAmountIn)
        num = reserve_quote * size_raw * 1000
        den = (reserve_base - size_raw) * 997
        if den <= 0:
            dex_buy = dex_sell * 1.003
        else:
            in_raw = num // den + 1
            dex_buy = in_raw / (10**quote_dec * size)

        return dex_buy, dex_sell

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _get_reserves(self, pool_addr: str) -> tuple[int, int]:
        """Call getReserves() on a Uniswap V2 pair contract via eth_call."""
        payload = {
            "jsonrpc": "2.0",
            "method": "eth_call",
            "params": [{"to": pool_addr, "data": "0x0902f1ac"}, "latest"],
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
            raise ValueError(f"RPC getReserves failed: {result.get('error')}")
        data = result["result"][2:]
        return int(data[0:64], 16), int(data[64:128], 16)
