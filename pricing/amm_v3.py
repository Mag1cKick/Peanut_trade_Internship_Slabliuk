"""
pricing/amm_v3.py — Uniswap V3 concentrated liquidity pool math.

Uses Q64.96 fixed-point arithmetic (Q96 = 2**96) for sqrt prices.
Implements a single-tick approximation: the entire in-range liquidity
is treated as if the pool stays within the current tick for the trade.

V3 fees are in parts-per-million (100 = 0.01 %, 500 = 0.05 %,
3000 = 0.30 %, 10000 = 1.00 %).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from web3 import Web3

from core.types import Address, Token
from pricing.amm import _fetch_token

if TYPE_CHECKING:
    from chain.client import ChainClient

# Q64.96 denominator
Q96: int = 2**96

# V3 fee denominator (fees expressed in parts-per-million)
_FEE_DENOM: int = 1_000_000

# ── Uniswap V3 pool ABI fragments ────────────────────────────────────────────

_POOL_ABI = [
    {
        "name": "slot0",
        "type": "function",
        "inputs": [],
        "outputs": [
            {"name": "sqrtPriceX96", "type": "uint160"},
            {"name": "tick", "type": "int24"},
            {"name": "observationIndex", "type": "uint16"},
            {"name": "observationCardinality", "type": "uint16"},
            {"name": "observationCardinalityNext", "type": "uint16"},
            {"name": "feeProtocol", "type": "uint8"},
            {"name": "unlocked", "type": "bool"},
        ],
        "stateMutability": "view",
    },
    {
        "name": "liquidity",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint128"}],
        "stateMutability": "view",
    },
    {
        "name": "token0",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "token1",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "address"}],
        "stateMutability": "view",
    },
    {
        "name": "fee",
        "type": "function",
        "inputs": [],
        "outputs": [{"name": "", "type": "uint24"}],
        "stateMutability": "view",
    },
]


# ── UniswapV3Pool ─────────────────────────────────────────────────────────────


@dataclass
class UniswapV3Pool:
    """
    Uniswap V3 concentrated-liquidity pool (single-tick approximation).

    Attributes:
        address:        On-chain pool address.
        token0:         The lower-sorted token (canonical V3 ordering).
        token1:         The higher-sorted token.
        sqrt_price_x96: Current sqrt price in Q64.96 format.
        liquidity:      Active in-range liquidity (uint128).
        fee_ppm:        Fee in parts-per-million (100 / 500 / 3000 / 10000).
        tick:           Current tick index (informational, not used in math).
    """

    address: Address
    token0: Token
    token1: Token
    sqrt_price_x96: int
    liquidity: int
    fee_ppm: int = 3000
    tick: int = 0

    def __post_init__(self) -> None:
        if self.sqrt_price_x96 <= 0:
            raise ValueError(f"sqrt_price_x96 must be positive, got {self.sqrt_price_x96}.")
        if self.liquidity <= 0:
            raise ValueError(f"liquidity must be positive, got {self.liquidity}.")
        if self.fee_ppm not in (100, 500, 3000, 10000):
            raise ValueError(f"fee_ppm must be one of 100/500/3000/10000, got {self.fee_ppm}.")
        if self.token0 == self.token1:
            raise ValueError("token0 and token1 must be different tokens.")

    # ── swap math ─────────────────────────────────────────────────────────────

    def get_amount_out(self, amount_in: int, token_in: Token) -> int:
        """
        Compute the output amount for *amount_in* of *token_in*.

        Uses the exact Uniswap V3 single-tick concentrated-liquidity formula:

        **zeroForOne** (token0 → token1):
            net_in  = amount_in * (FEE_DENOM - fee) // FEE_DENOM
            new_sqrt = (L * sqrt) // (L + ceil(net_in * sqrt / Q96))
            out      = L * (sqrt - new_sqrt) // Q96

        **oneForZero** (token1 → token0):
            net_in  = amount_in * (FEE_DENOM - fee) // FEE_DENOM
            new_sqrt = sqrt + (net_in * Q96) // L
            out      = L * Q96^2 * (new_sqrt - sqrt) // (sqrt * new_sqrt)

        Args:
            amount_in: Raw integer amount of the input token.
            token_in:  The token being sold.

        Returns:
            Raw integer amount of the output token (floor).

        Raises:
            TypeError:  If amount_in is not an int.
            ValueError: If amount_in ≤ 0 or token_in is not in this pool.
        """
        if not isinstance(amount_in, int):
            raise TypeError(f"amount_in must be int, got {type(amount_in).__name__}.")
        if amount_in <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in}.")

        sqrt = self.sqrt_price_x96
        L = self.liquidity
        net_in = amount_in * (_FEE_DENOM - self.fee_ppm) // _FEE_DENOM

        if token_in == self.token0:
            # zeroForOne: price (sqrt) decreases
            # ceil(net_in * sqrt / Q96) to avoid underflow
            extra = (net_in * sqrt + Q96 - 1) // Q96
            new_sqrt = (L * sqrt) // (L + extra)
            return L * (sqrt - new_sqrt) // Q96

        if token_in == self.token1:
            # oneForZero: price (sqrt) increases
            new_sqrt = sqrt + (net_in * Q96) // L
            delta = new_sqrt - sqrt
            # amount_out_token0 = L * delta / (sqrt_P_old * sqrt_P_new)
            # In Q96 space: sqrt_P = sqrt_q96 / Q96, so:
            # = L * delta_q96 / Q96 / ((sqrt_q96 / Q96) * (new_sqrt_q96 / Q96))
            # = L * delta_q96 * Q96 / (sqrt_q96 * new_sqrt_q96)
            return L * delta * Q96 // (sqrt * new_sqrt)

        raise ValueError(
            f"Token {token_in} is not in pool " f"({self.token0.symbol}/{self.token1.symbol})."
        )

    # ── price helpers ─────────────────────────────────────────────────────────

    def get_spot_price(self, token_in: Token) -> Decimal:
        """
        Return the spot price as *token_out per token_in* at the current
        sqrt price.

        For token0 in: price = (sqrtPriceX96 / Q96) ** 2  (token1 per token0)
        For token1 in: price = (Q96 / sqrtPriceX96) ** 2  (token0 per token1)
        """
        if token_in not in (self.token0, self.token1):
            raise ValueError(
                f"Token {token_in} is not in pool " f"({self.token0.symbol}/{self.token1.symbol})."
            )
        sqrt_dec = Decimal(self.sqrt_price_x96) / Decimal(Q96)
        if token_in == self.token0:
            return sqrt_dec * sqrt_dec
        return Decimal(1) / (sqrt_dec * sqrt_dec)

    def get_price_impact(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Return price impact as a fraction (0.01 = 1 %).

        Defined as (spot_price - execution_price) / spot_price, where
        execution_price = amount_out / amount_in (raw integers, no decimal
        scaling).
        """
        spot = self.get_spot_price(token_in)
        if spot == 0:
            return Decimal(0)
        amount_out = self.get_amount_out(amount_in, token_in)
        if amount_out == 0:
            return Decimal(1)
        execution = Decimal(amount_out) / Decimal(amount_in)
        impact = (spot - execution) / spot
        # clamp to [0, 1] — negative can occur due to fee rounding artefacts
        return max(Decimal(0), impact)

    # ── chain loader ──────────────────────────────────────────────────────────

    @classmethod
    def from_chain(cls, address: Address, client: ChainClient) -> UniswapV3Pool:
        """
        Fetch current pool state (slot0, liquidity, tokens, fee) from chain.

        Args:
            address: Checksum address of the V3 pool contract.
            client:  Connected ChainClient.

        Returns:
            A fully populated UniswapV3Pool instance.
        """
        w3 = client._web3_instances[0]
        pool = w3.eth.contract(
            address=Web3.to_checksum_address(address.checksum),
            abi=_POOL_ABI,
        )
        slot0 = pool.functions.slot0().call()
        sqrt_price_x96: int = slot0[0]
        tick: int = slot0[1]
        liquidity: int = pool.functions.liquidity().call()
        token0_addr: str = pool.functions.token0().call()
        token1_addr: str = pool.functions.token1().call()
        fee_ppm: int = pool.functions.fee().call()

        token0 = _fetch_token(w3, token0_addr)
        token1 = _fetch_token(w3, token1_addr)

        return cls(
            address=address,
            token0=token0,
            token1=token1,
            sqrt_price_x96=sqrt_price_x96,
            liquidity=liquidity,
            fee_ppm=fee_ppm,
            tick=tick,
        )
