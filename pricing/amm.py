"""
pricing/amm.py — Uniswap V2 AMM math and pair representation.

"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING

from web3 import Web3

from core.types import Address, Token

if TYPE_CHECKING:
    from chain.client import ChainClient

PAIR_ABI = [
    {
        "name": "getReserves",
        "type": "function",
        "inputs": [],
        "outputs": [
            {"name": "reserve0", "type": "uint112"},
            {"name": "reserve1", "type": "uint112"},
            {"name": "blockTimestampLast", "type": "uint32"},
        ],
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
]

ERC20_META_ABI = [
    {
        "name": "symbol",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "string"}],
        "stateMutability": "view",
    },
    {
        "name": "decimals",
        "type": "function",
        "inputs": [],
        "outputs": [{"type": "uint8"}],
        "stateMutability": "view",
    },
]


def _fetch_token(w3: Web3, address: str) -> Token:
    """Fetch token symbol and decimals from chain, with a fallback."""
    checksum = Web3.to_checksum_address(address)
    contract = w3.eth.contract(address=checksum, abi=ERC20_META_ABI)
    try:
        symbol = contract.functions.symbol().call()
        decimals = contract.functions.decimals().call()
    except Exception:
        symbol = checksum[:8] + "…"
        decimals = 18
    return Token(address=Address(checksum), symbol=symbol, decimals=decimals)


@dataclass
class UniswapV2Pair:
    """
    Represents a Uniswap V2 liquidity pair.
    """

    address: Address
    token0: Token
    token1: Token
    reserve0: int
    reserve1: int
    fee_bps: int = 30

    def __post_init__(self) -> None:
        if self.reserve0 <= 0 or self.reserve1 <= 0:
            raise ValueError(
                f"Reserves must be positive, got "
                f"reserve0={self.reserve0}, reserve1={self.reserve1}."
            )
        if not (0 <= self.fee_bps < 10000):
            raise ValueError(f"fee_bps must be in [0, 9999], got {self.fee_bps}.")
        if self.token0 == self.token1:
            raise ValueError("token0 and token1 must be different tokens.")

    def _reserves_for_token_in(self, token_in: Token) -> tuple[int, int]:
        """
        Return (reserve_in, reserve_out) for the given input token.
        """
        if token_in == self.token0:
            return self.reserve0, self.reserve1
        if token_in == self.token1:
            return self.reserve1, self.reserve0
        raise ValueError(
            f"Token {token_in} is not in pair " f"({self.token0.symbol}/{self.token1.symbol})."
        )

    def get_amount_out(self, amount_in: int, token_in: Token) -> int:
        """
        Calculate output amount for a given input using exact Uniswap V2 formula.
        """
        if not isinstance(amount_in, int):
            raise TypeError(f"amount_in must be int, got {type(amount_in).__name__}.")
        if amount_in <= 0:
            raise ValueError(f"amount_in must be positive, got {amount_in}.")
        reserve_in, reserve_out = self._reserves_for_token_in(token_in)
        amount_in_with_fee = amount_in * (10000 - self.fee_bps)
        numerator = amount_in_with_fee * reserve_out
        denominator = reserve_in * 10000 + amount_in_with_fee
        return numerator // denominator

    def get_amount_in(self, amount_out: int, token_out: Token) -> int:
        """
        Calculate required input for a desired output (inverse of get_amount_out).
        """
        if not isinstance(amount_out, int):
            raise TypeError(f"amount_out must be int, got {type(amount_out).__name__}.")
        if amount_out <= 0:
            raise ValueError(f"amount_out must be positive, got {amount_out}.")
        if token_out == self.token0:
            reserve_in, reserve_out = self.reserve1, self.reserve0
        elif token_out == self.token1:
            reserve_in, reserve_out = self.reserve0, self.reserve1
        else:
            raise ValueError(
                f"Token {token_out} is not in pair " f"({self.token0.symbol}/{self.token1.symbol})."
            )
        if amount_out >= reserve_out:
            raise ValueError(
                f"Insufficient liquidity: amount_out ({amount_out}) >= "
                f"reserve_out ({reserve_out})."
            )
        numerator = reserve_in * amount_out * 10000
        denominator = (reserve_out - amount_out) * (10000 - self.fee_bps)
        return numerator // denominator + 1

    def get_spot_price(self, token_in: Token) -> Decimal:
        """
        Return spot price as token_out per token_in at current reserves.
        """
        reserve_in, reserve_out = self._reserves_for_token_in(token_in)
        return Decimal(reserve_out) / Decimal(reserve_in)

    def get_execution_price(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Return actual execution price (token_out per token_in) for a trade.
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        if amount_out == 0:
            return Decimal(0)
        return Decimal(amount_out) / Decimal(amount_in)

    def get_price_impact(self, amount_in: int, token_in: Token) -> Decimal:
        """
        Return price impact as a fraction (0.01 = 1%).
        """
        spot = self.get_spot_price(token_in)
        if spot == 0:
            return Decimal(0)
        execution = self.get_execution_price(amount_in, token_in)
        return (spot - execution) / spot

    def simulate_swap(self, amount_in: int, token_in: Token) -> UniswapV2Pair:
        """
        Return a NEW pair with reserves updated as if the swap occurred.
        """
        amount_out = self.get_amount_out(amount_in, token_in)
        if token_in == self.token0:
            new_r0 = self.reserve0 + amount_in
            new_r1 = self.reserve1 - amount_out
        else:
            new_r0 = self.reserve0 - amount_out
            new_r1 = self.reserve1 + amount_in
        return UniswapV2Pair(
            address=self.address,
            token0=self.token0,
            token1=self.token1,
            reserve0=new_r0,
            reserve1=new_r1,
            fee_bps=self.fee_bps,
        )

    @classmethod
    def from_chain(cls, address: Address, client: ChainClient) -> UniswapV2Pair:
        """
        Fetch current pair state (reserves + token metadata) from on-chain.
        """
        w3 = client._web3_instances[0]
        pair_contract = w3.eth.contract(
            address=Web3.to_checksum_address(address.checksum),
            abi=PAIR_ABI,
        )
        reserve0, reserve1, _ = pair_contract.functions.getReserves().call()
        token0_addr = pair_contract.functions.token0().call()
        token1_addr = pair_contract.functions.token1().call()
        token0 = _fetch_token(w3, token0_addr)
        token1 = _fetch_token(w3, token1_addr)
        return cls(
            address=address,
            token0=token0,
            token1=token1,
            reserve0=reserve0,
            reserve1=reserve1,
        )
