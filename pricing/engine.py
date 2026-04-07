"""
pricing/engine.py — Unified pricing interface integrating AMM math, routing,
                    fork simulation, and mempool monitoring.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.fork_simulator import ForkSimulator
from pricing.mempool import MempoolMonitor, ParsedSwap
from pricing.router import Route, RouteFinder

log = logging.getLogger(__name__)

# Neutral sender address for simulation calls (no balance required for eth_call)
_SIM_SENDER = Address("0x0000000000000000000000000000000000000001")


# ── Exceptions ─────────────────────────────────────────────────────────────────


class QuoteError(Exception):
    """Raised when a price quote cannot be produced."""


# ── Quote ──────────────────────────────────────────────────────────────────────


@dataclass
class Quote:
    """Result of a best-route price quote."""

    route: Route
    amount_in: int
    expected_output: int  # net output from AMM math (after gas cost deducted)
    simulated_output: int  # gross output from fork simulation (before gas deduction)
    gas_estimate: int
    timestamp: float

    @property
    def is_valid(self) -> bool:
        """
        True if the fork-simulated output is within 0.1% of the expected output.

        A larger divergence signals that the stored pool reserves are stale and
        the quote should be refreshed before use.
        """
        if self.expected_output == 0:
            return self.simulated_output == 0
        diff = abs(self.expected_output - self.simulated_output)
        return Decimal(diff) / Decimal(self.expected_output) < Decimal("0.001")


# ── PricingEngine ──────────────────────────────────────────────────────────────


class PricingEngine:
    """
    Main interface for the pricing module.

    Integrates AMM math (UniswapV2Pair), routing (RouteFinder), fork simulation
    (ForkSimulator), and mempool monitoring (MempoolMonitor).

    Args:
        chain_client: ChainClient from the core chain module.
        fork_url:     HTTP endpoint of a local Anvil/Hardhat fork.
        ws_url:       WebSocket endpoint for mempool subscriptions.
    """

    def __init__(
        self,
        chain_client,
        fork_simulator: ForkSimulator,
        ws_url: str,
    ) -> None:
        self.client = chain_client
        self.simulator = fork_simulator
        self.monitor = MempoolMonitor(ws_url, self._on_mempool_swap)
        self.pools: dict[Address, UniswapV2Pair] = {}
        self.router: RouteFinder | None = None
        # Mempool swaps that affect our pools, stored for observability
        self.pending_swaps: list[ParsedSwap] = []

    def load_pools(self, pool_addresses: list[Address]) -> None:
        """
        Fetch pool state from chain for each address and build the route graph.

        Args:
            pool_addresses: On-chain Uniswap V2 pair addresses to track.
        """
        for addr in pool_addresses:
            self.pools[addr] = UniswapV2Pair.from_chain(addr, self.client)
            log.debug(
                "Loaded pool %s (%s/%s)",
                addr,
                self.pools[addr].token0.symbol,
                self.pools[addr].token1.symbol,
            )
        self.router = RouteFinder(list(self.pools.values()))
        log.info("Loaded %d pool(s), route graph ready.", len(self.pools))

    def refresh_pool(self, address: Address) -> None:
        """
        Refresh a single pool's reserves from chain without rebuilding the graph.

        Updates the existing pool object in place so all Route objects that
        already reference it automatically pick up the new reserves.

        Args:
            address: Address of the pool to refresh.

        Raises:
            KeyError: If the pool was not loaded via load_pools().
        """
        if address not in self.pools:
            raise KeyError(f"Pool {address} not loaded. Call load_pools() first.")
        fresh = UniswapV2Pair.from_chain(address, self.client)
        existing = self.pools[address]
        existing.reserve0 = fresh.reserve0
        existing.reserve1 = fresh.reserve1
        existing.fee_bps = fresh.fee_bps
        log.debug(
            "Refreshed pool %s: r0=%d r1=%d",
            address,
            existing.reserve0,
            existing.reserve1,
        )

    def get_quote(
        self,
        token_in: Token,
        token_out: Token,
        amount_in: int,
        gas_price_gwei: int,
    ) -> Quote:
        """
        Find the best route and return a validated quote.

        Uses AMM math to pick the net-optimal route, then cross-checks the
        result with a fork simulation.  Raises QuoteError if no route exists
        or if the simulation reports failure.

        Args:
            token_in:       Input token.
            token_out:      Output token.
            amount_in:      Raw input amount.
            gas_price_gwei: Gas price for net-output calculation.

        Returns:
            Quote with routing info, expected/simulated output, and validity flag.

        Raises:
            QuoteError: If no pools are loaded, no route is found, or simulation fails.
        """
        if self.router is None:
            raise QuoteError("No pools loaded. Call load_pools() first.")

        try:
            route, net_output = self.router.find_best_route(
                token_in, token_out, amount_in, gas_price_gwei
            )
        except ValueError as exc:
            raise QuoteError(str(exc)) from exc

        sim_result = self.simulator.simulate_route(route, amount_in, _SIM_SENDER)

        if not sim_result.success:
            raise QuoteError(f"Simulation failed: {sim_result.error}")

        return Quote(
            route=route,
            amount_in=amount_in,
            expected_output=net_output,
            simulated_output=sim_result.amount_out,
            gas_estimate=sim_result.gas_used,
            timestamp=time.time(),
        )

    def _on_mempool_swap(self, swap: ParsedSwap) -> None:
        """
        Callback invoked by MempoolMonitor when a pending swap is detected.

        Logs swaps that affect loaded pools and queues them in pending_swaps
        for downstream inspection (e.g. to trigger a re-quote).
        """
        affected = self._pools_affected_by(swap)
        if not affected:
            return
        log.info(
            "Mempool %s.%s detected — affects %d pool(s) (token_in=%s token_out=%s)",
            swap.dex,
            swap.method,
            len(affected),
            swap.token_in,
            swap.token_out,
        )
        self.pending_swaps.append(swap)

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _pools_affected_by(self, swap: ParsedSwap) -> list[UniswapV2Pair]:
        """Return loaded pools whose token set overlaps the swap's token path."""
        swap_addrs: set[Address] = set()
        if swap.token_in:
            swap_addrs.add(swap.token_in)
        if swap.token_out:
            swap_addrs.add(swap.token_out)
        if not swap_addrs:
            return []
        return [
            pair
            for pair in self.pools.values()
            if {pair.token0.address, pair.token1.address} & swap_addrs
        ]
