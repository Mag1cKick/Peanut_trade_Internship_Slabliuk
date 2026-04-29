"""Safety module — risk management, kill switch, and absolute trading limits."""

from safety.constants import (
    ABSOLUTE_MAX_DAILY_LOSS,
    ABSOLUTE_MAX_TRADE_USD,
    ABSOLUTE_MAX_TRADES_PER_HOUR,
    ABSOLUTE_MIN_CAPITAL,
    KILL_SWITCH_FILE,
    is_kill_switch_active,
    safety_check,
    trigger_kill_switch,
)
from safety.risk import PreTradeValidator, RiskLimits, RiskManager

__all__ = [
    "RiskLimits",
    "RiskManager",
    "PreTradeValidator",
    "safety_check",
    "is_kill_switch_active",
    "KILL_SWITCH_FILE",
    "ABSOLUTE_MAX_TRADE_USD",
    "ABSOLUTE_MAX_DAILY_LOSS",
    "ABSOLUTE_MIN_CAPITAL",
    "ABSOLUTE_MAX_TRADES_PER_HOUR",
    "trigger_kill_switch",
]
