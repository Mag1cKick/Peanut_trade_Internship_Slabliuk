"""
Unit tests for src.utils

Covers:
  - deterministic / invariant behaviour (same input → same output)
  - negative / edge-case inputs (bad data, boundary violations)
"""

import pytest

from src.utils import calculate_position_size, clamp


class TestClamp:
    def test_value_below_low_returns_low(self):
        assert clamp(-10, 0, 100) == 0

    def test_value_above_high_returns_high(self):
        assert clamp(200, 0, 100) == 100

    def test_value_within_range_unchanged(self):
        assert clamp(50, 0, 100) == 50

    def test_boundary_low_inclusive(self):
        assert clamp(0, 0, 100) == 0

    def test_boundary_high_inclusive(self):
        assert clamp(100, 0, 100) == 100

    def test_deterministic_same_input_same_output(self):
        """Calling clamp twice with identical args must return identical results."""
        result_a = clamp(42, 0, 100)
        result_b = clamp(42, 0, 100)
        assert result_a == result_b

    def test_invalid_range_raises(self):
        """low > high is a contract violation."""
        with pytest.raises(ValueError, match="low"):
            clamp(5, 100, 0)

    def test_float_precision_stable(self):
        result = clamp(0.1 + 0.2, 0.0, 1.0)
        assert 0.0 <= result <= 1.0


class TestCalculatePositionSize:
    def test_basic_calculation(self):
        assert calculate_position_size(10_000, 0.01, 50) == pytest.approx(2.0)

    def test_result_scales_with_equity(self):
        small = calculate_position_size(1_000, 0.01, 10)
        large = calculate_position_size(10_000, 0.01, 10)
        assert large == pytest.approx(small * 10)

    def test_deterministic(self):
        a = calculate_position_size(5_000, 0.02, 25)
        b = calculate_position_size(5_000, 0.02, 25)
        assert a == b

    def test_zero_equity_raises(self):
        with pytest.raises(ValueError, match="equity"):
            calculate_position_size(0, 0.01, 10)

    def test_negative_equity_raises(self):
        with pytest.raises(ValueError, match="equity"):
            calculate_position_size(-500, 0.01, 10)

    def test_zero_risk_pct_raises(self):
        with pytest.raises(ValueError, match="risk_pct"):
            calculate_position_size(10_000, 0, 10)

    def test_risk_pct_above_one_raises(self):
        with pytest.raises(ValueError, match="risk_pct"):
            calculate_position_size(10_000, 1.5, 10)

    def test_zero_stop_distance_raises(self):
        with pytest.raises(ValueError, match="stop_distance"):
            calculate_position_size(10_000, 0.01, 0)

    def test_negative_stop_distance_raises(self):
        with pytest.raises(ValueError, match="stop_distance"):
            calculate_position_size(10_000, 0.01, -5)
