"""
tests/test_historical.py — Unit tests for pricing.historical.HistoricalAnalyzer

No live RPC node required — all eth_call responses are mocked.

Test groups:
  1. HistoricalSnapshot — properties (spot_price, liquidity_proxy)
  2. HistoricalAnalyzer.fetch_snapshots — happy path, ordering, error handling
  3. HistoricalAnalyzer.fetch_snapshots — zero reserves skipped, RPC errors skipped
  4. HistoricalAnalyzer.analyze_impact_trend — price change, impact stats, liquidity trend
  5. analyze_impact_trend — edge cases (single snapshot, empty raises, stable liquidity)
"""

from __future__ import annotations

from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from core.types import Address, Token
from pricing.amm import UniswapV2Pair
from pricing.historical import HistoricalAnalyzer, HistoricalSnapshot

# ── Shared fixtures ───────────────────────────────────────────────────────────

WETH = Token(
    address=Address("0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2"), symbol="WETH", decimals=18
)
DAI = Token(
    address=Address("0x6B175474E89094C44Da98b954EedeAC495271d0F"), symbol="DAI", decimals=18
)

PAIR_ADDR = Address("0x0000000000000000000000000000000000000001")

BASE_R0 = 10**18
BASE_R1 = 2000 * 10**18


def _pair(r0=BASE_R0, r1=BASE_R1) -> UniswapV2Pair:
    return UniswapV2Pair(
        address=PAIR_ADDR,
        token0=WETH,
        token1=DAI,
        reserve0=r0,
        reserve1=r1,
    )


def _encode_reserves(r0: int, r1: int) -> bytes:
    """ABI-encode two uint112 values as 32-byte words (getReserves return)."""
    return r0.to_bytes(32, "big") + r1.to_bytes(32, "big") + b"\x00" * 32


def _mock_client(reserve_sequence: list[tuple[int, int]]) -> MagicMock:
    """
    Build a mock ChainClient whose eth.call returns ABI-encoded reserves
    in the order given by *reserve_sequence*.
    """
    mock_w3 = MagicMock()
    mock_w3.eth.call.side_effect = [_encode_reserves(r0, r1) for r0, r1 in reserve_sequence]
    client = MagicMock()
    client._web3_instances = [mock_w3]
    return client


# ── 1. HistoricalSnapshot properties ─────────────────────────────────────────


class TestHistoricalSnapshot:
    def _make(self, r0=BASE_R0, r1=BASE_R1, impacts=None) -> HistoricalSnapshot:
        return HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=100,
            reserve0=r0,
            reserve1=r1,
            token_in=WETH,
            impacts=impacts or {},
        )

    def test_spot_price_is_r1_over_r0(self):
        snap = self._make(r0=10**18, r1=2000 * 10**18)
        assert snap.spot_price == Decimal(2000)

    def test_spot_price_zero_when_reserves_zero(self):
        snap = self._make(r0=0, r1=0)
        assert snap.spot_price == Decimal(0)

    def test_liquidity_proxy_positive(self):
        snap = self._make(r0=BASE_R0, r1=BASE_R1)
        proxy = snap.liquidity_proxy
        assert proxy > 0

    def test_liquidity_proxy_scales_with_reserves(self):
        small = self._make(r0=10**18, r1=10**18)
        large = self._make(r0=10**21, r1=10**21)
        assert large.liquidity_proxy > small.liquidity_proxy

    def test_liquidity_proxy_zero_for_zero_reserves(self):
        snap = self._make(r0=0, r1=0)
        assert snap.liquidity_proxy == 0

    def test_liquidity_proxy_is_integer(self):
        snap = self._make()
        assert isinstance(snap.liquidity_proxy, int)


# ── 2. fetch_snapshots — happy path ──────────────────────────────────────────


class TestFetchSnapshots:
    def test_returns_one_snapshot_per_block(self):
        client = _mock_client([(BASE_R0, BASE_R1), (BASE_R0 * 2, BASE_R1 * 2)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(),
            blocks=[100, 200],
            token_in=WETH,
            sample_sizes=[10**17],
        )
        assert len(snaps) == 2

    def test_snapshot_block_numbers_match(self):
        client = _mock_client([(BASE_R0, BASE_R1), (BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[100, 200], token_in=WETH, sample_sizes=[10**17]
        )
        assert snaps[0].block_number == 100
        assert snaps[1].block_number == 200

    def test_snapshot_reserves_correct(self):
        r0a, r1a = BASE_R0, BASE_R1
        r0b, r1b = BASE_R0 * 2, BASE_R1 * 2
        client = _mock_client([(r0a, r1a), (r0b, r1b)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1, 2], token_in=WETH, sample_sizes=[10**17]
        )
        assert snaps[0].reserve0 == r0a
        assert snaps[1].reserve0 == r0b

    def test_impacts_populated_for_all_sizes(self):
        client = _mock_client([(BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        sizes = [10**16, 10**17, 10**18]
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1], token_in=WETH, sample_sizes=sizes
        )
        for size in sizes:
            assert size in snaps[0].impacts

    def test_impacts_are_decimal(self):
        client = _mock_client([(BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1], token_in=WETH, sample_sizes=[10**17]
        )
        for v in snaps[0].impacts.values():
            assert isinstance(v, Decimal)

    def test_larger_size_has_greater_impact(self):
        client = _mock_client([(BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1], token_in=WETH, sample_sizes=[10**16, 10**18]
        )
        assert snaps[0].impacts[10**18] > snaps[0].impacts[10**16]

    def test_pair_address_stored(self):
        client = _mock_client([(BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1], token_in=WETH, sample_sizes=[10**17]
        )
        assert snaps[0].pair_address == PAIR_ADDR

    def test_empty_blocks_returns_empty(self):
        client = _mock_client([])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[], token_in=WETH, sample_sizes=[10**17]
        )
        assert snaps == []


# ── 3. fetch_snapshots — error / edge cases ───────────────────────────────────


class TestFetchSnapshotsEdgeCases:
    def test_zero_reserves_block_skipped(self):
        client = _mock_client([(0, 0), (BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1, 2], token_in=WETH, sample_sizes=[10**17]
        )
        assert len(snaps) == 1
        assert snaps[0].block_number == 2

    def test_rpc_error_block_skipped(self):
        mock_w3 = MagicMock()
        mock_w3.eth.call.side_effect = [
            RuntimeError("RPC error"),
            _encode_reserves(BASE_R0, BASE_R1),
        ]
        client = MagicMock()
        client._web3_instances = [mock_w3]

        analyzer = HistoricalAnalyzer(client)
        snaps = analyzer.fetch_snapshots(
            pair=_pair(), blocks=[1, 2], token_in=WETH, sample_sizes=[10**17]
        )
        assert len(snaps) == 1
        assert snaps[0].block_number == 2

    def test_eth_call_called_with_block_identifier(self):
        mock_w3 = MagicMock()
        mock_w3.eth.call.return_value = _encode_reserves(BASE_R0, BASE_R1)
        client = MagicMock()
        client._web3_instances = [mock_w3]

        with patch("web3.Web3.to_checksum_address", side_effect=lambda x: x):
            analyzer = HistoricalAnalyzer(client)
            analyzer.fetch_snapshots(
                pair=_pair(), blocks=[99999], token_in=WETH, sample_sizes=[10**17]
            )

        call_args = mock_w3.eth.call.call_args
        # Second positional argument is the block identifier
        assert call_args[0][1] == 99999


# ── 4. analyze_impact_trend — normal cases ────────────────────────────────────


class TestAnalyzeImpactTrend:
    def _snaps(
        self,
        reserve_pairs: list[tuple[int, int]],
        blocks: list[int] | None = None,
        size: int = 10**17,
    ) -> list[HistoricalSnapshot]:
        if blocks is None:
            blocks = list(range(1, len(reserve_pairs) + 1))
        client = _mock_client(reserve_pairs)
        analyzer = HistoricalAnalyzer(client)
        return analyzer.fetch_snapshots(
            pair=_pair(reserve_pairs[0][0], reserve_pairs[0][1]),
            blocks=blocks,
            token_in=WETH,
            sample_sizes=[size],
        )

    def test_block_range_correct(self):
        snaps = self._snaps([(BASE_R0, BASE_R1)] * 3, blocks=[10, 20, 30])
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend(snaps)
        assert report["block_range"] == (10, 30)

    def test_price_change_pct_positive_when_price_rises(self):
        """Reserve1 doubles → spot price doubles → +100 %."""
        snap1 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        snap2 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=2,
            reserve0=10**18,
            reserve1=4000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.002")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap1, snap2])
        assert report["price_change_pct"] == pytest.approx(100.0, rel=1e-6)

    def test_price_change_pct_negative_when_price_falls(self):
        snap1 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=4000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        snap2 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=2,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap1, snap2])
        assert report["price_change_pct"] == pytest.approx(-50.0, rel=1e-6)

    def test_min_max_avg_impact_computed(self):
        snaps = [
            HistoricalSnapshot(
                pair_address=PAIR_ADDR,
                block_number=i,
                reserve0=10**18,
                reserve1=2000 * 10**18,
                token_in=WETH,
                impacts={10**17: Decimal(str(v))},
            )
            for i, v in enumerate([0.001, 0.002, 0.003], start=1)
        ]
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend(snaps)
        assert report["min_impact_1k"] == pytest.approx(0.001, rel=1e-6)
        assert report["max_impact_1k"] == pytest.approx(0.003, rel=1e-6)
        assert report["avg_impact_1k"] == pytest.approx(0.002, rel=1e-6)

    def test_liquidity_trend_increasing(self):
        snaps = [
            HistoricalSnapshot(
                pair_address=PAIR_ADDR,
                block_number=i,
                reserve0=r,
                reserve1=r,  # isqrt(r*r) = r
                token_in=WETH,
                impacts={},
            )
            for i, r in [(1, 10**18), (2, 10**18 * 2)]
        ]
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend(snaps)
        assert report["liquidity_trend"] == "increasing"

    def test_liquidity_trend_decreasing(self):
        snaps = [
            HistoricalSnapshot(
                pair_address=PAIR_ADDR,
                block_number=i,
                reserve0=r,
                reserve1=r,
                token_in=WETH,
                impacts={},
            )
            for i, r in [(1, 10**18 * 2), (2, 10**18)]
        ]
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend(snaps)
        assert report["liquidity_trend"] == "decreasing"

    def test_liquidity_trend_stable(self):
        snaps = [
            HistoricalSnapshot(
                pair_address=PAIR_ADDR,
                block_number=i,
                reserve0=10**18,
                reserve1=10**18,
                token_in=WETH,
                impacts={},
            )
            for i in [1, 2]
        ]
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend(snaps)
        assert report["liquidity_trend"] == "stable"


# ── 5. analyze_impact_trend — edge cases ─────────────────────────────────────


class TestAnalyzeImpactTrendEdgeCases:
    def test_empty_snapshots_raises(self):
        with pytest.raises(ValueError, match="empty"):
            HistoricalAnalyzer(MagicMock()).analyze_impact_trend([])

    def test_single_snapshot_price_change_zero(self):
        snap = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap])
        assert report["price_change_pct"] == 0.0

    def test_single_snapshot_min_max_avg_equal(self):
        snap = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.005")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap])
        assert report["min_impact_1k"] == report["max_impact_1k"] == report["avg_impact_1k"]

    def test_returns_all_required_keys(self):
        snap = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap])
        required_keys = {
            "block_range",
            "price_change_pct",
            "min_impact_1k",
            "max_impact_1k",
            "avg_impact_1k",
            "liquidity_trend",
        }
        assert required_keys <= set(report.keys())


# ── 6. Additional edge cases ──────────────────────────────────────────────────


class TestAdditionalEdgeCases:
    def test_fetch_snapshots_impact_calc_failure_stored_as_zero(self):
        """
        When get_price_impact raises for a sample_size, the snapshot stores
        Decimal(0) for that size rather than crashing.
        """
        client = _mock_client([(BASE_R0, BASE_R1)])
        analyzer = HistoricalAnalyzer(client)

        # Use a negative size to force get_price_impact to raise ValueError
        snaps = analyzer.fetch_snapshots(
            pair=_pair(),
            blocks=[1],
            token_in=WETH,
            sample_sizes=[-1],  # negative → get_amount_out raises ValueError
        )
        assert len(snaps) == 1
        assert snaps[0].impacts[-1] == Decimal(0)

    def test_analyze_impact_trend_zero_first_spot_gives_zero_pct(self):
        """
        When the first snapshot has reserve0=0 (spot_price=0), price_change_pct
        should be 0.0 (the `else: price_change_pct = 0.0` branch).
        """
        snap1 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=1,
            reserve0=0,
            reserve1=0,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        snap2 = HistoricalSnapshot(
            pair_address=PAIR_ADDR,
            block_number=2,
            reserve0=10**18,
            reserve1=2000 * 10**18,
            token_in=WETH,
            impacts={10**17: Decimal("0.001")},
        )
        report = HistoricalAnalyzer(MagicMock()).analyze_impact_trend([snap1, snap2])
        assert report["price_change_pct"] == 0.0
