"""
safety/risk.py — Runtime risk management for the arbitrage bot.

Three layers:
  1. PreTradeValidator  — signal sanity (prices positive, TTL, within_limits)
  2. RiskManager        — configurable per-trade, per-day, drawdown limits
  3. safety_check()     — absolute hard limits (imported from constants)

All checks return (bool, reason_str) so the caller can log the reason.
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass


@dataclass
class RiskLimits:
    """Configurable risk thresholds — all can be tightened but not raised
    above the absolute limits defined in safety.constants."""

    max_trade_usd: float = 5.0  # single trade value cap
    max_trade_pct: float = 0.20  # max fraction of capital per trade
    max_position_per_token: float = 30.0  # max ETH/token in a single position
    max_open_positions: int = 1  # bot executes sequentially; kept for future
    max_loss_per_trade: float = 5.0  # stop a trade if expected loss > this
    max_daily_loss: float = 10.0  # halt for the day beyond this
    max_drawdown_pct: float = 0.20  # halt if peak-to-trough > 20 %
    max_trades_per_hour: int = 20  # rolling 60-min window
    consecutive_loss_limit: int = 3  # pause after N losses in a row


class RiskManager:
    """
    Tracks runtime risk state and gates pre-trade approval.

    Call check_pre_trade() before every execution.
    Call record_trade() after every completed execution.
    """

    def __init__(self, limits: RiskLimits, initial_capital: float = 100.0) -> None:
        self.limits = limits
        self._capital = initial_capital
        self._peak_capital = initial_capital

        self._daily_loss: float = 0.0
        self._daily_reset: float = self._next_midnight()

        self._consecutive_losses: int = 0
        # Rolling deque of trade timestamps for trades-per-hour accounting
        self._trade_times: deque[float] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def check_pre_trade(self, signal) -> tuple[bool, str]:
        """
        Gate the signal against all configured risk limits.

        Checks (in order): daily loss, drawdown, consecutive losses,
        hourly rate, trade size, position size.
        """
        self._maybe_reset_daily()
        self._prune_trade_times()

        if self._daily_loss <= -self.limits.max_daily_loss:
            return False, (
                f"daily_loss ${self._daily_loss:.2f} <= "
                f"-max_daily_loss ${self.limits.max_daily_loss:.2f}"
            )

        drawdown = (self._peak_capital - self._capital) / self._peak_capital
        if drawdown >= self.limits.max_drawdown_pct:
            return False, (f"drawdown {drawdown:.1%} >= limit {self.limits.max_drawdown_pct:.1%}")

        if self._consecutive_losses >= self.limits.consecutive_loss_limit:
            return False, (
                f"consecutive_losses {self._consecutive_losses} "
                f">= limit {self.limits.consecutive_loss_limit}"
            )

        if len(self._trade_times) >= self.limits.max_trades_per_hour:
            return False, (
                f"trades_this_hour {len(self._trade_times)} "
                f">= limit {self.limits.max_trades_per_hour}"
            )

        # Trade value: use size × CEX price as the reference notional
        trade_usd = float(signal.size) * float(signal.cex_price)
        if trade_usd > self.limits.max_trade_usd:
            return False, (
                f"trade_usd ${trade_usd:.2f} > max_trade_usd ${self.limits.max_trade_usd:.2f}"
            )

        capital_pct = trade_usd / self._capital if self._capital > 0 else 1.0
        if capital_pct > self.limits.max_trade_pct:
            return False, (
                f"trade_pct {capital_pct:.1%} > max_trade_pct {self.limits.max_trade_pct:.1%}"
            )

        if float(signal.size) > self.limits.max_position_per_token:
            return False, (
                f"size {signal.size:.4f} > max_position_per_token "
                f"{self.limits.max_position_per_token}"
            )

        return True, ""

    def record_trade(self, pnl: float) -> None:
        """Update all risk state after a completed trade."""
        self._maybe_reset_daily()

        self._capital += pnl
        self._peak_capital = max(self._peak_capital, self._capital)

        if pnl < 0:
            self._daily_loss += pnl
            self._consecutive_losses += 1
        else:
            self._consecutive_losses = 0

        self._trade_times.append(time.monotonic())

    @property
    def daily_loss(self) -> float:
        self._maybe_reset_daily()
        return self._daily_loss

    @property
    def current_capital(self) -> float:
        return self._capital

    @property
    def trades_this_hour(self) -> int:
        self._prune_trade_times()
        return len(self._trade_times)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_reset_daily(self) -> None:
        now = time.time()
        if now >= self._daily_reset:
            self._daily_loss = 0.0
            self._consecutive_losses = 0
            self._daily_reset = self._next_midnight()

    def _prune_trade_times(self) -> None:
        cutoff = time.monotonic() - 3600.0
        while self._trade_times and self._trade_times[0] < cutoff:
            self._trade_times.popleft()

    @staticmethod
    def _next_midnight() -> float:
        import datetime

        tomorrow = datetime.date.today() + datetime.timedelta(days=1)
        return time.mktime(datetime.datetime.combine(tomorrow, datetime.time.min).timetuple())


class PreTradeValidator:
    """
    Signal sanity checks — runs before RiskManager.

    Catches obviously bad signals: zero/negative prices, expired TTL,
    inventory check already failed, or no spread to trade on.
    """

    # Spread above this is almost certainly bad price data, not real opportunity
    MAX_SANE_SPREAD_BPS: float = 500.0

    def validate_signal(self, signal) -> tuple[bool, str]:
        if float(signal.cex_price) <= 0:
            return False, "cex_price <= 0"
        if float(signal.dex_price) <= 0:
            return False, "dex_price <= 0"
        if float(signal.size) <= 0:
            return False, "size <= 0"
        spread = float(signal.spread_bps)
        if spread <= 0:
            return False, "spread_bps <= 0 (no arbitrage opportunity)"
        if spread > self.MAX_SANE_SPREAD_BPS:
            return False, (
                f"spread_bps {spread:.1f} > {self.MAX_SANE_SPREAD_BPS:.0f} — likely bad price data"
            )
        if not signal.within_limits:
            return False, "signal.within_limits=False (inventory check failed)"

        age = time.time() - signal.timestamp
        ttl = getattr(signal, "signal_ttl_seconds", 5.0)
        if age > ttl:
            return False, f"signal expired: age {age:.1f}s > ttl {ttl:.1f}s"

        return True, ""
