"""
safety/constants.py — Absolute hard limits for the arbitrage bot.

These values are NON-NEGOTIABLE and must NOT be changed at runtime.
They form the last line of defense against runaway trading losses.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Absolute safety constants — DO NOT MODIFY THESE VALUES
# ---------------------------------------------------------------------------

ABSOLUTE_MAX_TRADE_USD = 25.0  # Hard ceiling on any single trade
ABSOLUTE_MAX_DAILY_LOSS = 20.0  # Hard ceiling on daily loss
ABSOLUTE_MIN_CAPITAL = 50.0  # Auto-stop if total capital < $50
ABSOLUTE_MAX_TRADES_PER_HOUR = 30  # Prevent runaway loops

# ---------------------------------------------------------------------------
# Kill switch
# ---------------------------------------------------------------------------

KILL_SWITCH_FILE = "/tmp/arb_bot_kill"


def is_kill_switch_active() -> bool:
    """Return True if the kill switch file exists — bot must halt immediately."""
    return os.path.exists(KILL_SWITCH_FILE)


def trigger_kill_switch(reason: str = "") -> None:
    """
    Arm the kill switch by creating the sentinel file.

    Called automatically when capital drops below ABSOLUTE_MIN_CAPITAL.
    The next _tick() will detect the file and call self.stop().
    """
    import logging

    try:
        with open(KILL_SWITCH_FILE, "w") as fh:
            fh.write(reason or "auto-triggered")
        logging.getLogger(__name__).critical(
            "Kill switch ARMED: %s → %s", reason or "auto-triggered", KILL_SWITCH_FILE
        )
    except OSError as exc:
        logging.getLogger(__name__).error("Failed to arm kill switch: %s", exc)


# ---------------------------------------------------------------------------
# Final safety gate
# ---------------------------------------------------------------------------


def safety_check(
    trade_usd: float,
    daily_loss: float,
    total_capital: float,
    trades_this_hour: int,
) -> tuple[bool, str]:
    """
    Final safety gate — runs AFTER all other checks.

    All four checks are hard limits derived from the module-level constants.
    Returns (True, "OK") only when every check passes.
    """
    if trade_usd > ABSOLUTE_MAX_TRADE_USD:
        return False, f"Trade ${trade_usd:.0f} exceeds absolute max ${ABSOLUTE_MAX_TRADE_USD:.0f}"
    if daily_loss <= -ABSOLUTE_MAX_DAILY_LOSS:
        return False, f"Absolute daily loss limit ${ABSOLUTE_MAX_DAILY_LOSS:.0f} reached"
    if total_capital < ABSOLUTE_MIN_CAPITAL:
        return False, f"Capital ${total_capital:.0f} below minimum ${ABSOLUTE_MIN_CAPITAL:.0f}"
    if trades_this_hour >= ABSOLUTE_MAX_TRADES_PER_HOUR:
        return False, f"Absolute hourly trade limit {ABSOLUTE_MAX_TRADES_PER_HOUR} reached"
    return True, "OK"
