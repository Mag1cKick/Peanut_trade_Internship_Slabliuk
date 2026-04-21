"""
strategy/scorer.py — Signal scoring for arbitrage opportunity ranking.

SignalScorer converts a raw Signal into a 0-100 quality score using
four weighted components: spread attractiveness, liquidity proxy,
inventory alignment, and historical win rate.
"""

from __future__ import annotations

from dataclasses import dataclass

from strategy.signal import Signal


@dataclass
class ScorerConfig:
    spread_weight: float = 0.4
    liquidity_weight: float = 0.2
    inventory_weight: float = 0.2
    history_weight: float = 0.2

    excellent_spread_bps: float = 100.0
    min_spread_bps: float = 30.0

    def __post_init__(self) -> None:
        weights = [
            self.spread_weight,
            self.liquidity_weight,
            self.inventory_weight,
            self.history_weight,
        ]
        if any(w < 0 for w in weights):
            raise ValueError("All weights must be >= 0")
        total = sum(weights)
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Weights must sum to 1.0, got {total}")
        if self.excellent_spread_bps <= self.min_spread_bps:
            raise ValueError(
                f"excellent_spread_bps ({self.excellent_spread_bps}) must be "
                f"> min_spread_bps ({self.min_spread_bps})"
            )


class SignalScorer:
    """
    Score a Signal on a 0–100 scale using spread, liquidity, inventory,
    and historical win-rate components.
    """

    def __init__(self, config: ScorerConfig | None = None) -> None:
        self.config = config or ScorerConfig()
        self.recent_results: list[tuple[str, bool]] = []

    def score(self, signal: Signal, inventory_state: list[dict]) -> float:
        """
        Compute a composite 0–100 score and store it on the signal.

        inventory_state is a list of dicts with keys 'asset' and 'needs_rebalance'.
        """
        scores = {
            "spread": self._score_spread(signal.spread_bps),
            "liquidity": 80.0,
            "inventory": self._score_inventory(signal, inventory_state),
            "history": self._score_history(signal.pair),
        }
        weighted = sum(scores[k] * getattr(self.config, f"{k}_weight") for k in scores)
        result = round(max(0.0, min(100.0, weighted)), 1)
        signal.score = result
        return result

    def _score_spread(self, spread_bps: float) -> float:
        """Linear score: 0 at min_spread_bps, 100 at excellent_spread_bps."""
        if spread_bps <= self.config.min_spread_bps:
            return 0.0
        if spread_bps >= self.config.excellent_spread_bps:
            return 100.0
        span = self.config.excellent_spread_bps - self.config.min_spread_bps
        return (spread_bps - self.config.min_spread_bps) / span * 100.0

    def _score_inventory(self, signal: Signal, skews: list[dict]) -> float:
        """Return 20 if any relevant asset needs rebalancing, else 60."""
        base = signal.pair.split("/")[0]
        relevant = [s for s in skews if s["asset"] == base]
        if any(s["needs_rebalance"] for s in relevant):
            return 20.0
        return 60.0

    def _score_history(self, pair: str) -> float:
        """Win-rate over the last 20 results for this pair; 50 if < 3 results."""
        results = [r for p, r in self.recent_results[-20:] if p == pair]
        if len(results) < 3:
            return 50.0
        return sum(results) / len(results) * 100.0

    def record_result(self, pair: str, success: bool) -> None:
        """Record a trade outcome; keeps only the last 100 results."""
        self.recent_results.append((pair, success))
        self.recent_results = self.recent_results[-100:]

    def apply_decay(self, signal: Signal) -> float:
        """
        Return a time-decayed version of signal.score.

        Score degrades linearly to 50% of its value by the time the signal expires.
        Does not mutate signal.score.
        """
        age = signal.age_seconds()
        ttl = signal.expiry - signal.timestamp
        if ttl <= 0:
            return 0.0
        decay_factor = max(0.0, 1.0 - (age / ttl) * 0.5)
        return signal.score * decay_factor
