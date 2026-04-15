"""
inventory/tracker.py — Position tracking: cost-basis P&L and multi-venue balances.

Two independent trackers serve different purposes:

CostBasisTracker  — fills-based P&L accounting (weighted-average cost basis,
                    realized PnL, fees).  Used by PnLEngine.

InventoryTracker  — real-time multi-venue balance sheet (CEX + on-chain wallet).
                    Tracks where funds actually sit, supports pre-flight checks
                    and arbitrage execution decisions.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

# ── Shared types ───────────────────────────────────────────────────────────────

TradeType = Literal["buy", "sell"]


class Venue(str, Enum):
    BINANCE = "binance"
    WALLET = "wallet"  # On-chain / DEX venue


# ══════════════════════════════════════════════════════════════════════════════
# CostBasisTracker — fills-based P&L accounting
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class Trade:
    """One completed fill recorded by CostBasisTracker."""

    asset: str
    side: TradeType
    qty: Decimal
    price: Decimal
    fee: Decimal
    timestamp: int  # Unix ms


@dataclass
class Position:
    """Current state of a single-asset position (cost-basis view)."""

    asset: str
    qty: Decimal = field(default_factory=lambda: Decimal("0"))
    avg_cost: Decimal = field(default_factory=lambda: Decimal("0"))
    realized_pnl: Decimal = field(default_factory=lambda: Decimal("0"))
    total_fees: Decimal = field(default_factory=lambda: Decimal("0"))


class CostBasisTracker:
    """
    Tracks open positions and realized P&L using weighted-average cost basis.

    Usage::

        tracker = CostBasisTracker()
        tracker.record_fill("ETH", "buy",  qty=Decimal("1"), price=Decimal("2000"))
        tracker.record_fill("ETH", "sell", qty=Decimal("0.5"), price=Decimal("2200"))

        pos = tracker.get_position("ETH")
        print(pos.realized_pnl)  # Decimal("100")
    """

    def __init__(self) -> None:
        self._positions: dict[str, Position] = {}
        self._trades: list[Trade] = []

    def record_fill(
        self,
        asset: str,
        side: TradeType,
        qty: Decimal,
        price: Decimal,
        fee: Decimal = Decimal("0"),
        timestamp: int | None = None,
    ) -> None:
        if qty <= 0:
            raise ValueError(f"qty must be positive, got {qty}")
        if price <= 0:
            raise ValueError(f"price must be positive, got {price}")
        if fee < 0:
            raise ValueError(f"fee must be non-negative, got {fee}")
        if side not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

        ts = timestamp if timestamp is not None else int(time.time() * 1000)
        self._trades.append(
            Trade(asset=asset, side=side, qty=qty, price=price, fee=fee, timestamp=ts)
        )

        pos = self._positions.setdefault(asset, Position(asset=asset))
        pos.total_fees += fee

        if side == "buy":
            total_value = pos.avg_cost * pos.qty + price * qty
            pos.qty += qty
            pos.avg_cost = total_value / pos.qty
        else:
            sell_qty = min(qty, pos.qty)
            if sell_qty > 0:
                realized = (price - pos.avg_cost) * sell_qty - fee
                pos.realized_pnl += realized
                pos.qty -= sell_qty
                if pos.qty == 0:
                    pos.avg_cost = Decimal("0")

    def get_position(self, asset: str) -> Position:
        return self._positions.get(asset, Position(asset=asset))

    def all_positions(self) -> dict[str, Position]:
        return {a: p for a, p in self._positions.items() if p.qty > 0}

    def all_positions_including_closed(self) -> dict[str, Position]:
        return dict(self._positions)

    def unrealized_pnl(self, asset: str, mark_price: Decimal) -> Decimal:
        pos = self.get_position(asset)
        if pos.qty == 0:
            return Decimal("0")
        return (mark_price - pos.avg_cost) * pos.qty

    def total_exposure(self, prices: dict[str, Decimal]) -> Decimal:
        total = Decimal("0")
        for asset, pos in self.all_positions().items():
            if asset in prices:
                total += pos.qty * prices[asset]
        return total

    def trade_history(self, asset: str | None = None) -> list[Trade]:
        if asset is None:
            return list(self._trades)
        return [t for t in self._trades if t.asset == asset]


# ── Backwards-compat alias used by PnLEngine ──────────────────────────────────
# Kept so existing imports of InventoryTracker still work during migration.
# Will be removed once all callers are updated.


# ══════════════════════════════════════════════════════════════════════════════
# InventoryTracker — multi-venue real-time balance sheet
# ══════════════════════════════════════════════════════════════════════════════


@dataclass
class Balance:
    """Real-time balance of one asset at one venue."""

    venue: Venue
    asset: str
    free: Decimal
    locked: Decimal = field(default_factory=lambda: Decimal("0"))

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


class InventoryTracker:
    """
    Tracks positions across CEX and DEX venues.
    Single source of truth for where funds currently sit.

    Usage::

        tracker = InventoryTracker([Venue.BINANCE, Venue.WALLET])

        # After fetching from ExchangeClient:
        tracker.update_from_cex(Venue.BINANCE, client.fetch_balance())

        # After reading on-chain wallet:
        tracker.update_from_wallet(Venue.WALLET, {"ETH": Decimal("5.0")})

        # Pre-flight arb check:
        ok = tracker.can_execute(
            buy_venue=Venue.BINANCE,  buy_asset="USDT",  buy_amount=Decimal("4000"),
            sell_venue=Venue.WALLET,  sell_asset="ETH",  sell_amount=Decimal("2"),
        )
    """

    def __init__(self, venues: list[Venue]) -> None:
        self._venues: list[Venue] = list(venues)
        # {venue: {asset: Balance}}
        self._balances: dict[Venue, dict[str, Balance]] = {v: {} for v in venues}

    # ── Balance ingestion ──────────────────────────────────────────────────────

    def update_from_cex(self, venue: Venue, balances: dict) -> None:
        """
        Replace the stored snapshot for ``venue`` with fresh CEX data.

        ``balances`` is the dict returned by ``ExchangeClient.fetch_balance()``:
        ``{asset: {'free': Decimal, 'locked': Decimal, 'total': Decimal}}``.
        """
        snapshot: dict[str, Balance] = {}
        for asset, info in balances.items():
            if not isinstance(info, dict):
                continue
            snapshot[asset] = Balance(
                venue=venue,
                asset=asset,
                free=Decimal(str(info.get("free", 0))),
                locked=Decimal(str(info.get("locked", 0))),
            )
        self._balances[venue] = snapshot

    def update_from_wallet(self, venue: Venue, balances: dict) -> None:
        """
        Replace the stored snapshot for ``venue`` with on-chain wallet data.

        ``balances`` is ``{asset: amount}`` (all funds are free on-chain).
        """
        snapshot: dict[str, Balance] = {}
        for asset, amount in balances.items():
            snapshot[asset] = Balance(
                venue=venue,
                asset=asset,
                free=Decimal(str(amount)),
            )
        self._balances[venue] = snapshot

    # ── Queries ────────────────────────────────────────────────────────────────

    def snapshot(self) -> dict:
        """
        Full portfolio snapshot at current time.

        Returns::

            {
                'timestamp': datetime,
                'venues': {
                    'binance': {'ETH': {'free': ..., 'locked': ..., 'total': ...}},
                    'wallet':  {'ETH': {'free': ..., 'locked': ..., 'total': ...}},
                },
                'totals': {'ETH': Decimal('20.0'), 'USDT': Decimal('40000.0')},
            }
        """
        venues_data: dict[str, dict] = {}
        totals: dict[str, Decimal] = {}

        for venue, assets in self._balances.items():
            venue_key = venue.value
            venues_data[venue_key] = {}
            for asset, bal in assets.items():
                venues_data[venue_key][asset] = {
                    "free": bal.free,
                    "locked": bal.locked,
                    "total": bal.total,
                }
                totals[asset] = totals.get(asset, Decimal("0")) + bal.total

        return {
            "timestamp": datetime.now(tz=UTC),
            "venues": venues_data,
            "totals": totals,
        }

    def get_available(self, venue: Venue, asset: str) -> Decimal:
        """Free (non-locked) balance of ``asset`` at ``venue``."""
        bal = self._balances.get(venue, {}).get(asset)
        return bal.free if bal is not None else Decimal("0")

    def can_execute(
        self,
        buy_venue: Venue,
        buy_asset: str,
        buy_amount: Decimal,
        sell_venue: Venue,
        sell_asset: str,
        sell_amount: Decimal,
    ) -> dict:
        """
        Pre-flight check: can both legs of an arbitrage be executed?

        Returns::

            {
                'can_execute': bool,
                'buy_venue_available': Decimal,
                'buy_venue_needed': Decimal,
                'sell_venue_available': Decimal,
                'sell_venue_needed': Decimal,
                'reason': str | None,
            }
        """
        buy_available = self.get_available(buy_venue, buy_asset)
        sell_available = self.get_available(sell_venue, sell_asset)

        reasons: list[str] = []
        if buy_available < buy_amount:
            reasons.append(
                f"Insufficient {buy_asset} at {buy_venue.value}: "
                f"need {buy_amount}, have {buy_available}"
            )
        if sell_available < sell_amount:
            reasons.append(
                f"Insufficient {sell_asset} at {sell_venue.value}: "
                f"need {sell_amount}, have {sell_available}"
            )

        return {
            "can_execute": len(reasons) == 0,
            "buy_venue_available": buy_available,
            "buy_venue_needed": buy_amount,
            "sell_venue_available": sell_available,
            "sell_venue_needed": sell_amount,
            "reason": "; ".join(reasons) if reasons else None,
        }

    def record_trade(
        self,
        venue: Venue,
        side: str,
        base_asset: str,
        quote_asset: str,
        base_amount: Decimal,
        quote_amount: Decimal,
        fee: Decimal,
        fee_asset: str,
    ) -> None:
        """
        Update internal balances after a trade executes.

        Buy:  base increases, quote decreases.
        Sell: base decreases, quote increases.
        Fee is always deducted from ``fee_asset``.
        """
        if venue not in self._balances:
            self._balances[venue] = {}

        def _ensure(asset: str) -> Balance:
            if asset not in self._balances[venue]:
                self._balances[venue][asset] = Balance(venue=venue, asset=asset, free=Decimal("0"))
            return self._balances[venue][asset]

        base_bal = _ensure(base_asset)
        quote_bal = _ensure(quote_asset)

        if side == "buy":
            base_bal.free += base_amount
            quote_bal.free -= quote_amount
        else:  # sell
            base_bal.free -= base_amount
            quote_bal.free += quote_amount

        fee_bal = _ensure(fee_asset)
        fee_bal.free -= fee

    # ── Skew analysis ──────────────────────────────────────────────────────────

    def skew(self, asset: str) -> dict:
        """
        Distribution of ``asset`` across all venues.

        Compares each venue's share against the equal-weight benchmark
        (100% / number_of_venues).  A deviation > 30 % flags rebalance need.

        Returns::

            {
                'asset': str,
                'total': Decimal,
                'venues': {
                    'binance': {'amount': Decimal, 'pct': float, 'deviation_pct': float},
                    'wallet':  {'amount': Decimal, 'pct': float, 'deviation_pct': float},
                },
                'max_deviation_pct': float,
                'needs_rebalance': bool,
            }
        """
        total = Decimal("0")
        venue_amounts: dict[str, Decimal] = {}

        for venue in self._venues:
            bal = self._balances.get(venue, {}).get(asset)
            amount = bal.total if bal is not None else Decimal("0")
            venue_amounts[venue.value] = amount
            total += amount

        n = len(self._venues)
        equal_pct = 100.0 / n if n > 0 else 0.0
        max_deviation = 0.0
        venues_data: dict[str, dict] = {}

        for venue_name, amount in venue_amounts.items():
            if total > 0:
                pct = float(amount / total * 100)
                deviation = abs(pct - equal_pct)
            else:
                pct = 0.0
                deviation = 0.0
            max_deviation = max(max_deviation, deviation)
            venues_data[venue_name] = {
                "amount": amount,
                "pct": pct,
                "deviation_pct": deviation,
            }

        return {
            "asset": asset,
            "total": total,
            "venues": venues_data,
            "max_deviation_pct": max_deviation,
            "needs_rebalance": max_deviation > 30.0,
        }

    def get_skews(self) -> list[dict]:
        """
        Skew analysis for every asset that appears across any venue.

        Returns one dict per asset (same schema as :meth:`skew`),
        sorted alphabetically.  Used by Week 4's SignalScorer.
        """
        all_assets: set[str] = set()
        for assets in self._balances.values():
            all_assets.update(assets.keys())
        return [self.skew(asset) for asset in sorted(all_assets)]
