"""
Pure utility functions — no side effects, fully unit-testable.
"""


def clamp(value: float, low: float, high: float) -> float:
    """Return value clamped to [low, high]."""
    if low > high:
        raise ValueError(f"low ({low}) must be <= high ({high})")
    return max(low, min(value, high))


def calculate_position_size(
    equity: float,
    risk_pct: float,
    stop_distance: float,
) -> float:
    """
    Return position size in base units.

    Args:
        equity:        total account equity (> 0)
        risk_pct:      fraction of equity to risk, e.g. 0.01 for 1 %
        stop_distance: price distance to stop-loss (> 0)

    Raises:
        ValueError: on invalid inputs
    """
    if equity <= 0:
        raise ValueError("equity must be positive")
    if not (0 < risk_pct <= 1):
        raise ValueError("risk_pct must be in (0, 1]")
    if stop_distance <= 0:
        raise ValueError("stop_distance must be positive")

    risk_amount = equity * risk_pct
    return risk_amount / stop_distance
