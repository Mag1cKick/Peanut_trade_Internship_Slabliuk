"""
core/serializer.py — Deterministic canonical JSON serialization.
"""

from __future__ import annotations

import json
import logging
import warnings
from typing import Any

from eth_hash.auto import keccak

log = logging.getLogger(__name__)

_JS_MAX_SAFE_INT = 2**53 - 1


class CanonicalSerializer:
    """
    Produces deterministic JSON bytes suitable for cryptographic signing.
    """

    @staticmethod
    def serialize(obj: Any) -> bytes:
        """
        Return canonical UTF-8 JSON bytes.
        """
        normalised = CanonicalSerializer._normalise(obj)
        return json.dumps(
            normalised,
            sort_keys=True,
            separators=(",", ":"),  # no whitespace
            ensure_ascii=False,  # preserve unicode as-is
        ).encode("utf-8")

    @staticmethod
    def hash(obj: Any) -> bytes:
        """
        Return keccak256 of the canonical serialization.
        """
        return keccak(CanonicalSerializer.serialize(obj))

    @staticmethod
    def verify_determinism(obj: Any, iterations: int = 100) -> bool:
        """
        Verify that serialization produces identical output across N calls.
        """
        if iterations < 2:
            raise ValueError("iterations must be >= 2 to verify determinism")

        first = CanonicalSerializer.serialize(obj)
        for _ in range(iterations - 1):
            if CanonicalSerializer.serialize(obj) != first:
                return False
        return True

    @staticmethod
    def _normalise(obj: Any) -> Any:
        """
        Recursively walk obj and apply canonical rules before JSON encoding.
        """
        if isinstance(obj, bool):
            return obj

        if isinstance(obj, float):
            raise FloatRejectedError(
                f"Float value {obj!r} is not allowed in canonical serialization. "
                "Use string amounts for trading data (e.g. '1.5' instead of 1.5) "
                "to avoid precision loss and non-determinism."
            )

        if isinstance(obj, int):
            if obj > _JS_MAX_SAFE_INT or obj < -_JS_MAX_SAFE_INT:
                warnings.warn(
                    f"Integer {obj} exceeds JavaScript's Number.MAX_SAFE_INTEGER "
                    f"(2^53-1). Serializing as decimal string to preserve precision.",
                    LargeIntegerWarning,
                    stacklevel=4,
                )
                return str(obj)
            return obj

        if isinstance(obj, dict):
            return {
                str(k): CanonicalSerializer._normalise(v)
                for k, v in sorted(obj.items(), key=lambda item: str(item[0]))
            }

        if isinstance(obj, list | tuple):
            return [CanonicalSerializer._normalise(item) for item in obj]
        return obj


class FloatRejectedError(TypeError):
    """
    Raised when a float is passed to CanonicalSerializer.
    """


class LargeIntegerWarning(UserWarning):
    """
    Emitted when an integer exceeds 2^53 - 1 (JavaScript's MAX_SAFE_INTEGER).
    """
