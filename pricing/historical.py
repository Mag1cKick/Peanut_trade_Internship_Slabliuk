"""
pricing/historical.py — Historical price impact analysis over multiple blocks.

Fetches pool reserves at historical block numbers using ``eth_call`` with the
``block_identifier`` override, then computes price-impact statistics across
different trade sizes and the reserve trend over the block range.

Usage::

    analyzer = HistoricalAnalyzer(chain_client)
    snapshots = analyzer.fetch_snapshots(
        pair=my_pair,
        blocks=[18_000_000, 18_000_100, 18_000_200],
        token_in=WETH,
        sample_sizes=[10**17, 10**18, 10**19],
    )
    report = analyzer.analyze_impact_trend(snapshots)
    print(report)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING

from web3 import Web3

from core.types import Address, Token
from pricing.amm import UniswapV2Pair

if TYPE_CHECKING:
    from chain.client import ChainClient

log = logging.getLogger(__name__)

# getReserves() selector (raw bytes, no 0x prefix)
_GET_RESERVES_SELECTOR = bytes.fromhex("0902f1ac")


# ── HistoricalSnapshot ────────────────────────────────────────────────────────


@dataclass
class HistoricalSnapshot:
    """
    On-chain state of a Uniswap V2 pair at a single historical block.

    Attributes:
        pair_address: Address of the pair contract.
        block_number: Ethereum block at which the snapshot was taken.
        reserve0:     token0 reserve (uint112).
        reserve1:     token1 reserve (uint112).
        token_in:     Token used as the input for impact calculations.
        impacts:      Mapping of sample_size → price_impact (fraction, e.g. 0.01 = 1 %).
    """

    pair_address: Address
    block_number: int
    reserve0: int
    reserve1: int
    token_in: Token
    impacts: dict[int, Decimal] = field(default_factory=dict)

    @property
    def spot_price(self) -> Decimal:
        """Spot price as token_out per token_in at this block."""
        if self.reserve0 <= 0 or self.reserve1 <= 0:
            return Decimal(0)
        # Determines direction from token_in identity (compared by object equality
        # as set up by fetch_snapshots — token0 / token1 share the same pair object).
        return Decimal(self.reserve1) / Decimal(self.reserve0)

    @property
    def liquidity_proxy(self) -> int:
        """Geometric-mean liquidity proxy: sqrt(reserve0 * reserve1)."""
        product = self.reserve0 * self.reserve1
        if product <= 0:
            return 0
        # Integer square root
        x = product
        y = (x + 1) // 2
        while y < x:
            x = y
            y = (x + product // x) // 2
        return x


# ── HistoricalAnalyzer ────────────────────────────────────────────────────────


class HistoricalAnalyzer:
    """
    Fetches historical pool snapshots and computes price-impact trend analytics.

    Args:
        client: Connected ChainClient (used for its web3 instance).
    """

    def __init__(self, client: ChainClient) -> None:
        self.client = client

    # ── public API ────────────────────────────────────────────────────────────

    def fetch_snapshots(
        self,
        pair: UniswapV2Pair,
        blocks: list[int],
        token_in: Token,
        sample_sizes: list[int],
    ) -> list[HistoricalSnapshot]:
        """
        Fetch reserves at each block and compute price impacts.

        For each block in *blocks*, this method calls ``getReserves()`` on
        the pair contract at that specific block height using
        ``eth_call(..., block_identifier=block_number)``.  It then simulates
        a swap of each size in *sample_sizes* and records the price impact.

        Args:
            pair:         The pool to query.
            blocks:       List of block numbers to sample (ascending order recommended).
            token_in:     The token being sold (determines swap direction).
            sample_sizes: List of raw input amounts to compute impact for.

        Returns:
            List of HistoricalSnapshot (one per block), preserving *blocks* order.
        """
        w3 = self.client._web3_instances[0]
        snapshots: list[HistoricalSnapshot] = []

        for block in blocks:
            try:
                r0, r1 = self._get_reserves_at(w3, pair.address, block)
            except Exception as exc:
                log.warning("Could not fetch reserves at block %d: %s", block, exc)
                continue

            if r0 <= 0 or r1 <= 0:
                log.debug("Skipping block %d — zero reserves.", block)
                continue

            # Reconstruct a transient pair with the historical reserves
            hist_pair = UniswapV2Pair(
                address=pair.address,
                token0=pair.token0,
                token1=pair.token1,
                reserve0=r0,
                reserve1=r1,
                fee_bps=pair.fee_bps,
            )

            impacts: dict[int, Decimal] = {}
            for size in sample_sizes:
                try:
                    impacts[size] = hist_pair.get_price_impact(size, token_in)
                except Exception as exc:
                    log.debug("Impact calc failed at block %d size %d: %s", block, size, exc)
                    impacts[size] = Decimal(0)

            snapshots.append(
                HistoricalSnapshot(
                    pair_address=pair.address,
                    block_number=block,
                    reserve0=r0,
                    reserve1=r1,
                    token_in=token_in,
                    impacts=impacts,
                )
            )

        return snapshots

    def analyze_impact_trend(
        self,
        snapshots: list[HistoricalSnapshot],
    ) -> dict:
        """
        Summarise price-impact statistics across the snapshot series.

        Returns a dict with the following keys:

        - ``block_range``: ``(first_block, last_block)`` tuple.
        - ``price_change_pct``: Percentage change in spot price from first to
          last snapshot.  Positive = price rose; negative = fell.
        - ``min_impact_1k``: Minimum price impact seen for the smallest
          sample size (or the first available sample size).
        - ``max_impact_1k``: Maximum price impact for that same size.
        - ``avg_impact_1k``: Average (arithmetic mean) impact for that size.
        - ``liquidity_trend``: ``"increasing"``, ``"decreasing"``, or
          ``"stable"`` based on the geometric-mean liquidity proxy.

        Args:
            snapshots: List from fetch_snapshots (must be non-empty).

        Returns:
            Dict with the statistics listed above.

        Raises:
            ValueError: If *snapshots* is empty.
        """
        if not snapshots:
            raise ValueError("snapshots list is empty.")

        first = snapshots[0]
        last = snapshots[-1]

        block_range = (first.block_number, last.block_number)

        # Price change
        p_first = first.spot_price
        p_last = last.spot_price
        if p_first > 0:
            price_change_pct = float((p_last - p_first) / p_first * 100)
        else:
            price_change_pct = 0.0

        # Pick the smallest sample size that all snapshots share
        all_sizes: set[int] = set()
        for snap in snapshots:
            all_sizes.update(snap.impacts.keys())

        common_sizes = {s for s in all_sizes if all(s in snap.impacts for snap in snapshots)}
        if common_sizes:
            ref_size = min(common_sizes)
        else:
            ref_size = None

        if ref_size is not None:
            impact_values = [float(snap.impacts[ref_size]) for snap in snapshots]
            min_impact = min(impact_values)
            max_impact = max(impact_values)
            avg_impact = sum(impact_values) / len(impact_values)
        else:
            min_impact = max_impact = avg_impact = 0.0

        # Liquidity trend (geometric mean proxy)
        liq_values = [snap.liquidity_proxy for snap in snapshots]
        if len(liq_values) >= 2 and liq_values[0] > 0:
            change = (liq_values[-1] - liq_values[0]) / liq_values[0]
            if change > 0.01:
                liquidity_trend = "increasing"
            elif change < -0.01:
                liquidity_trend = "decreasing"
            else:
                liquidity_trend = "stable"
        else:
            liquidity_trend = "stable"

        return {
            "block_range": block_range,
            "price_change_pct": price_change_pct,
            "min_impact_1k": min_impact,
            "max_impact_1k": max_impact,
            "avg_impact_1k": avg_impact,
            "liquidity_trend": liquidity_trend,
        }

    # ── internal helpers ──────────────────────────────────────────────────────

    def _get_reserves_at(
        self,
        w3: Web3,
        pair_address: Address,
        block_number: int,
    ) -> tuple[int, int]:
        """
        Call ``getReserves()`` on *pair_address* at *block_number* using
        ``eth_call`` with the block override, and decode the result.
        """
        checksum = Web3.to_checksum_address(pair_address.checksum)
        result: bytes = w3.eth.call(
            {"to": checksum, "data": "0x" + _GET_RESERVES_SELECTOR.hex()},
            block_number,
        )
        reserve0 = int.from_bytes(result[0:32], "big")
        reserve1 = int.from_bytes(result[32:64], "big")
        return reserve0, reserve1
