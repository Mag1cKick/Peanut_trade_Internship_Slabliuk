"""
pricing/protocols.py — Structural interfaces (Protocols) for the pricing module.

Defining these protocols satisfies the Interface Segregation Principle (ISP)
and the Liskov Substitution Principle (LSP): any class that provides the right
methods can be used wherever an AMMPool is expected — no inheritance required.

This also supports the Open/Closed Principle (OCP): Route, RouteFinder, and
ForkSimulator are open for extension (new pool types) without modification.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol, runtime_checkable

from core.types import Address, Token


@runtime_checkable
class AMMPool(Protocol):
    """
    Minimal interface every AMM pool implementation must satisfy.

    Both UniswapV2Pair and UniswapV3Pool conform structurally — they do not
    need to inherit from this class.  isinstance(pool, AMMPool) works because
    of @runtime_checkable.

    Responsibilities (ISP — keep it narrow):
      - Price calculation (get_amount_out, get_spot_price)
      - Impact estimation (get_price_impact)
      - Identity (address, token0, token1)

    Not included here: chain loading, simulation, routing — those are separate
    concerns with their own collaborators.
    """

    address: Address
    token0: Token
    token1: Token

    def get_amount_out(self, amount_in: int, token_in: Token) -> int:
        """Return the raw output amount for amount_in of token_in."""
        ...

    def get_spot_price(self, token_in: Token) -> Decimal:
        """Return the marginal price as token_out units per token_in unit."""
        ...

    def get_price_impact(self, amount_in: int, token_in: Token) -> Decimal:
        """Return price impact as a fraction in [0, 1] (0.01 = 1 %)."""
        ...
