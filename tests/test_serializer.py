"""
tests/test_serializer.py — Unit tests for core.serializer.CanonicalSerializer

Test groups:
  1. Basic serialization (primitives, structure)
  2. Key ordering (nested dicts, mixed key types)
  3. Unicode handling (emoji, non-ASCII, RTL text)
  4. Large integers (> 2^53)
  5. None / null values
  6. Empty objects and arrays
  7. Floats — must be rejected
  8. hash() — keccak256 correctness
  9. verify_determinism()
  10. Edge cases (booleans, nested lists, mixed nesting)
"""

import warnings

import pytest

from core.serializer import (
    CanonicalSerializer,
    FloatRejectedError,
    LargeIntegerWarning,
)

S = CanonicalSerializer  # shorthand


# ── 1. Basic serialization ────────────────────────────────────────────────────


class TestBasicSerialization:
    def test_returns_bytes(self):
        assert isinstance(S.serialize({"a": 1}), bytes)

    def test_simple_dict(self):
        assert S.serialize({"a": 1}) == b'{"a":1}'

    def test_no_whitespace_in_output(self):
        result = S.serialize({"key": "value", "num": 42})
        assert b" " not in result
        assert b"\n" not in result
        assert b"\t" not in result

    def test_string_value(self):
        assert S.serialize({"msg": "hello"}) == b'{"msg":"hello"}'

    def test_integer_value(self):
        assert S.serialize({"n": 42}) == b'{"n":42}'

    def test_boolean_true(self):
        assert S.serialize({"flag": True}) == b'{"flag":true}'

    def test_boolean_false(self):
        assert S.serialize({"flag": False}) == b'{"flag":false}'

    def test_list_value(self):
        assert S.serialize({"items": [1, 2, 3]}) == b'{"items":[1,2,3]}'

    def test_output_is_valid_utf8(self):
        result = S.serialize({"a": 1})
        result.decode("utf-8")  # should not raise


# ── 2. Key ordering ───────────────────────────────────────────────────────────


class TestKeyOrdering:
    def test_keys_sorted_alphabetically(self):
        result = S.serialize({"z": 1, "a": 2, "m": 3})
        assert result == b'{"a":2,"m":3,"z":1}'

    def test_reverse_order_input_same_output(self):
        forward = S.serialize({"a": 1, "b": 2, "c": 3})
        backward = S.serialize({"c": 3, "b": 2, "a": 1})
        assert forward == backward

    def test_nested_dict_keys_sorted(self):
        obj = {"outer": {"z": 1, "a": 2}}
        assert S.serialize(obj) == b'{"outer":{"a":2,"z":1}}'

    def test_deeply_nested_keys_sorted(self):
        obj = {"c": {"z": {"y": 1, "x": 2}, "a": 0}, "a": 1}
        result = S.serialize(obj)
        # Outer: a before c; inner c: a before z; innermost z: x before y
        assert result == b'{"a":1,"c":{"a":0,"z":{"x":2,"y":1}}}'

    def test_mixed_insertion_order_is_irrelevant(self):
        """Python dicts preserve insertion order — sorting must override this."""
        obj1 = {"b": 2, "a": 1}
        obj2 = {"a": 1, "b": 2}
        assert S.serialize(obj1) == S.serialize(obj2)

    def test_list_order_is_preserved(self):
        """Lists are ordered — do NOT sort list elements."""
        result = S.serialize({"items": [3, 1, 2]})
        assert result == b'{"items":[3,1,2]}'

    def test_list_of_dicts_inner_keys_sorted(self):
        obj = {"rows": [{"b": 2, "a": 1}, {"d": 4, "c": 3}]}
        assert S.serialize(obj) == b'{"rows":[{"a":1,"b":2},{"c":3,"d":4}]}'


# ── 3. Unicode handling ───────────────────────────────────────────────────────


class TestUnicodeHandling:
    def test_ascii_string_unchanged(self):
        assert S.serialize({"k": "hello"}) == b'{"k":"hello"}'

    def test_unicode_preserved_not_escaped(self):
        """ensure_ascii=False — characters must appear as-is, not \\uXXXX."""
        result = S.serialize({"name": "Привіт"})
        assert "Привіт".encode() in result

    def test_emoji_preserved(self):
        result = S.serialize({"icon": "🚀"})
        assert "🚀".encode() in result

    def test_emoji_in_key(self):
        result = S.serialize({"🔑": "value"})
        assert "🔑".encode() in result

    def test_mixed_ascii_and_unicode(self):
        obj = {"city": "Київ", "code": "UA"}
        result = S.serialize(obj)
        assert "Київ".encode() in result
        assert b"UA" in result

    def test_null_byte_in_string(self):
        """Null bytes are valid JSON string content."""
        result = S.serialize({"k": "a\x00b"})
        assert isinstance(result, bytes)

    def test_rtl_text(self):
        result = S.serialize({"text": "مرحبا"})
        assert "مرحبا".encode() in result

    def test_deterministic_across_unicode_inputs(self):
        obj = {"emoji": "🎯", "text": "Привіт", "ascii": "hello"}
        assert S.verify_determinism(obj, iterations=50)


# ── 4. Large integers ─────────────────────────────────────────────────────────


class TestLargeIntegers:
    JS_MAX = 2**53 - 1  # 9007199254740991

    def test_small_int_serialized_as_number(self):
        result = S.serialize({"n": 42})
        assert result == b'{"n":42}'

    def test_js_max_safe_int_serialized_as_number(self):
        result = S.serialize({"n": self.JS_MAX})
        assert result == b'{"n":9007199254740991}'

    def test_js_max_safe_int_plus_one_warns(self):
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            S.serialize({"n": self.JS_MAX + 1})
            assert any(issubclass(w.category, LargeIntegerWarning) for w in caught)

    def test_large_int_serialized_as_string(self):
        large = 2**256  # typical Ethereum uint256
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = S.serialize({"val": large})
        # Must be a quoted string in the JSON output
        assert f'"{large}"'.encode() in result

    def test_large_negative_int_warns_and_is_string(self):
        large_neg = -(2**53)
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = S.serialize({"n": large_neg})
            assert any(issubclass(w.category, LargeIntegerWarning) for w in caught)
        assert f'"{large_neg}"'.encode() in result

    def test_wei_amount_preserved_exactly(self):
        """1 ETH in wei = 10^18, well above JS safe integer."""
        one_eth_wei = 10**18
        with warnings.catch_warnings(record=True):
            warnings.simplefilter("always")
            result = S.serialize({"amount_wei": one_eth_wei})
        assert b'"1000000000000000000"' in result


# ── 5. None / null ────────────────────────────────────────────────────────────


class TestNullValues:
    def test_none_serialized_as_null(self):
        assert S.serialize({"k": None}) == b'{"k":null}'

    def test_top_level_none(self):
        assert S.serialize(None) == b"null"

    def test_none_in_list(self):
        assert S.serialize({"items": [1, None, 3]}) == b'{"items":[1,null,3]}'

    def test_none_value_in_nested_dict(self):
        obj = {"outer": {"inner": None}}
        assert S.serialize(obj) == b'{"outer":{"inner":null}}'


# ── 6. Empty objects and arrays ───────────────────────────────────────────────


class TestEmptyCollections:
    def test_empty_dict(self):
        assert S.serialize({}) == b"{}"

    def test_empty_list(self):
        assert S.serialize([]) == b"[]"

    def test_empty_string_value(self):
        assert S.serialize({"k": ""}) == b'{"k":""}'

    def test_dict_with_empty_list_value(self):
        assert S.serialize({"items": []}) == b'{"items":[]}'

    def test_dict_with_empty_dict_value(self):
        assert S.serialize({"meta": {}}) == b'{"meta":{}}'

    def test_nested_empty_collections(self):
        obj = {"a": {}, "b": [], "c": ""}
        assert S.serialize(obj) == b'{"a":{},"b":[],"c":""}'


# ── 7. Floats — must be rejected ──────────────────────────────────────────────


class TestFloatRejection:
    def test_float_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"price": 1.5})

    def test_float_zero_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"v": 0.0})

    def test_float_in_list_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"prices": [1.0, 2.0]})

    def test_float_in_nested_dict_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"outer": {"inner": 3.14}})

    def test_float_error_message_is_helpful(self):
        with pytest.raises(FloatRejectedError, match="string amounts"):
            S.serialize({"amount": 1.5})

    def test_string_amount_accepted(self):
        """The correct alternative to floats."""
        result = S.serialize({"amount": "1.5"})
        assert result == b'{"amount":"1.5"}'

    def test_nan_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"v": float("nan")})

    def test_inf_raises(self):
        with pytest.raises(FloatRejectedError):
            S.serialize({"v": float("inf")})


# ── 8. hash() ─────────────────────────────────────────────────────────────────


class TestHash:
    def test_returns_32_bytes(self):
        result = S.hash({"a": 1})
        assert isinstance(result, bytes)
        assert len(result) == 32

    def test_same_input_same_hash(self):
        assert S.hash({"a": 1}) == S.hash({"a": 1})

    def test_different_input_different_hash(self):
        assert S.hash({"a": 1}) != S.hash({"a": 2})

    def test_key_order_does_not_affect_hash(self):
        assert S.hash({"a": 1, "b": 2}) == S.hash({"b": 2, "a": 1})

    def test_hash_is_keccak256(self):
        """Verify against a known keccak256 value."""
        from eth_hash.auto import keccak

        data = S.serialize({"test": "value"})
        expected = keccak(data)
        assert S.hash({"test": "value"}) == expected

    def test_whitespace_difference_changes_hash(self):
        """Canonical bytes must never include extra whitespace."""
        canonical = S.hash({"a": 1})
        # Manually compute hash of spaced JSON — must differ
        from eth_hash.auto import keccak

        spaced = keccak(b'{"a": 1}')
        assert canonical != spaced


# ── 9. verify_determinism() ───────────────────────────────────────────────────


class TestVerifyDeterminism:
    def test_simple_object_is_deterministic(self):
        assert S.verify_determinism({"a": 1, "b": 2}) is True

    def test_nested_object_is_deterministic(self):
        obj = {"z": {"y": [1, 2, 3], "x": "hello"}, "a": None}
        assert S.verify_determinism(obj) is True

    def test_unicode_is_deterministic(self):
        assert S.verify_determinism({"emoji": "🚀", "text": "Привіт"}) is True

    def test_default_iterations_is_100(self):
        """verify_determinism with no iterations arg should run 100 times."""
        import unittest.mock as mock

        original = S.serialize
        call_count = []

        def counting_serialize(obj):
            call_count.append(1)
            return original(obj)

        with mock.patch.object(S, "serialize", staticmethod(counting_serialize)):
            S.verify_determinism({"a": 1})

        assert sum(call_count) == 100

    def test_too_few_iterations_raises(self):
        with pytest.raises(ValueError, match="iterations"):
            S.verify_determinism({"a": 1}, iterations=1)


# ── 10. Edge cases ────────────────────────────────────────────────────────────


class TestEdgeCases:
    def test_boolean_not_confused_with_int(self):
        """True == 1 in Python — must stay as JSON boolean, not integer."""
        result = S.serialize({"flag": True})
        assert result == b'{"flag":true}'
        assert result != b'{"flag":1}'

    def test_nested_list_of_lists(self):
        obj = {"matrix": [[1, 2], [3, 4]]}
        assert S.serialize(obj) == b'{"matrix":[[1,2],[3,4]]}'

    def test_integer_zero(self):
        assert S.serialize({"n": 0}) == b'{"n":0}'

    def test_negative_integer(self):
        assert S.serialize({"n": -42}) == b'{"n":-42}'

    def test_top_level_list(self):
        assert S.serialize([3, 1, 2]) == b"[3,1,2]"

    def test_top_level_string(self):
        assert S.serialize("hello") == b'"hello"'

    def test_top_level_integer(self):
        assert S.serialize(42) == b"42"

    def test_tuple_treated_as_list(self):
        result = S.serialize({"t": (1, 2, 3)})
        assert result == b'{"t":[1,2,3]}'

    def test_deeply_nested_structure_deterministic(self):
        obj = {"level1": {"level2": {"level3": {"z": [None, True, "🔥"], "a": 0}}}}
        assert S.verify_determinism(obj, iterations=50)
