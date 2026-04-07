"""
pricing/fork_simulator.py — Simulate swaps against a local Anvil/Hardhat fork.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from eth_abi import decode as abi_decode
from eth_abi import encode as abi_encode
from web3 import Web3

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.router import Route

# ── Constants ──────────────────────────────────────────────────────────────────

# Canonical Uniswap V2 Router 02 on mainnet (used as default in compare helper)
_UNISWAP_V2_ROUTER = Address("0x7a250d5630B4cF539739dF2C5dAcb4c659F2488D")

# Neutral sender for read-only eth_call calls that don't need a balance
_ZERO_SENDER = Address("0x0000000000000000000000000000000000000001")

# ABI selectors
_SEL_GET_AMOUNTS_OUT = bytes.fromhex("d06ca61f")  # getAmountsOut(uint256,address[])
_SEL_GET_RESERVES = bytes.fromhex("0902f1ac")  # getReserves()


# ── SimulationResult ───────────────────────────────────────────────────────────


@dataclass
class SimulationResult:
    """Result from a fork simulation."""

    success: bool
    amount_out: int
    gas_used: int
    error: str | None
    logs: list = field(default_factory=list)


# ── ForkSimulator ──────────────────────────────────────────────────────────────


class ForkSimulator:
    """
    Simulates transactions against a local fork (Anvil / Hardhat).

    Args:
        fork_url: HTTP RPC endpoint of the local fork, e.g. http://127.0.0.1:8545
    """

    def __init__(self, fork_url: str) -> None:
        self.w3 = Web3(Web3.HTTPProvider(fork_url))

    # ── Public API ─────────────────────────────────────────────────────────────

    def simulate_swap(
        self,
        router: Address,
        swap_params: dict,
        sender: Address,
    ) -> SimulationResult:
        """
        Simulate a swap by calling the router's getAmountsOut view function.

        This is gas-free and requires no token approvals.  It reflects the
        pool state at the fork's current block.

        Args:
            router:      Uniswap V2-compatible router address.
            swap_params: Must contain:
                           'amount_in' (int)  – raw input amount
                           'path'      (list) – ordered list of token address strings
            sender:      tx.from for the eth_call (rarely matters for view calls).

        Returns:
            SimulationResult — success=False with error message on any failure.
        """
        amount_in: int = swap_params["amount_in"]
        path: list[str] = swap_params["path"]

        calldata = _SEL_GET_AMOUNTS_OUT + abi_encode(["uint256", "address[]"], [amount_in, path])
        tx = {
            "to": router.checksum,
            "from": sender.checksum,
            "data": "0x" + calldata.hex(),
        }
        try:
            raw = self.w3.eth.call(tx)
            (amounts,) = abi_decode(["uint256[]"], raw)
            amount_out = amounts[-1]
            gas_used = 150_000 + 100_000 * (len(path) - 1)
            return SimulationResult(
                success=True,
                amount_out=amount_out,
                gas_used=gas_used,
                error=None,
            )
        except Exception as exc:
            return SimulationResult(
                success=False,
                amount_out=0,
                gas_used=0,
                error=str(exc),
            )

    def simulate_route(
        self,
        route: Route,
        amount_in: int,
        sender: Address,
    ) -> SimulationResult:
        """
        Simulate a multi-hop Route by fetching live reserves for each pair
        from the fork and running the AMM math hop-by-hop.

        Args:
            route:     Route object whose pools define the hops.
            amount_in: Raw input amount for route.path[0].
            sender:    Not used for computation, carried into the result for
                       traceability.

        Returns:
            SimulationResult — success=False with error message on any failure.
        """
        try:
            current = amount_in
            for pair, token_in in zip(route.pools, route.path):
                live_r0, live_r1 = self._get_reserves(pair.address)
                live_pair = UniswapV2Pair(
                    address=pair.address,
                    token0=pair.token0,
                    token1=pair.token1,
                    reserve0=live_r0,
                    reserve1=live_r1,
                    fee_bps=pair.fee_bps,
                )
                current = live_pair.get_amount_out(current, token_in)
            return SimulationResult(
                success=True,
                amount_out=current,
                gas_used=route.estimate_gas(),
                error=None,
            )
        except Exception as exc:
            return SimulationResult(
                success=False,
                amount_out=0,
                gas_used=0,
                error=str(exc),
            )

    def compare_simulation_vs_calculation(
        self,
        pair: UniswapV2Pair,
        amount_in: int,
        token_in: Token,
    ) -> dict:
        """
        Compare our offline AMM math against the live fork state.

        Fetches current on-chain reserves, re-runs our formula, and reports
        whether the two values agree.  A mismatch indicates either stale
        reserves in the pair object or a discrepancy in our formula.

        Args:
            pair:      UniswapV2Pair whose stored reserves represent
                       the "calculated" baseline.
            amount_in: Raw input amount to test.
            token_in:  Input token (must be pair.token0 or pair.token1).

        Returns:
            dict with keys: calculated, simulated, difference, match.
        """
        calculated = pair.get_amount_out(amount_in, token_in)

        token_out = pair.token1 if token_in == pair.token0 else pair.token0
        result = self.simulate_swap(
            router=_UNISWAP_V2_ROUTER,
            swap_params={
                "amount_in": amount_in,
                "path": [token_in.address.checksum, token_out.address.checksum],
            },
            sender=_ZERO_SENDER,
        )

        simulated = result.amount_out
        diff = abs(calculated - simulated)
        return {
            "calculated": calculated,
            "simulated": simulated,
            "difference": diff,
            "match": calculated == simulated,
        }

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _get_reserves(self, pair_address: Address) -> tuple[int, int]:
        """
        Fetch (reserve0, reserve1) from a Uniswap V2 pair contract.

        Calls getReserves() → (uint112 reserve0, uint112 reserve1, uint32 ts).
        """
        raw = self.w3.eth.call(
            {"to": pair_address.checksum, "data": "0x" + _SEL_GET_RESERVES.hex()}
        )
        reserve0, reserve1, _ts = abi_decode(["uint112", "uint112", "uint32"], raw)
        return reserve0, reserve1
