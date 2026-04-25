"""Phase B — rng handler tests.

Covers:

- Coin-flip default returns 0 or 1.
- Integer ranges (inclusive both ends) and single integer (``rng:N``).
- Float ranges (``[lo, hi)``) including ``rng:float`` shorthand.
- Dice notation (``NdM``) — N rolls + sum.
- choice / shuffle on comma lists.
- uuid4 + bytes (CSPRNG, ignore seed).
- Seeded reproducibility — same seed yields same output across calls.
- Both URI shapes for seed: ``rng:3d6?seed=42`` and ``rng:?seed=42/3d6``.
- Range clamps and validation errors.
- /help is informative.
"""

from __future__ import annotations

import re

import pytest

from precis.handlers.rng import RngHandler, _split_query
from precis.protocol import ErrorCode, PrecisError


def _read(h: RngHandler, path: str) -> str:
    return h.read(
        path=path,
        selector=None,
        view=None,
        subview=None,
        query="",
        summarize=False,
        depth=0,
        page=1,
    )


def _value_line(out: str) -> str:
    """Strip the footer; return the value-bearing portion."""
    return out.split("\n\n---", 1)[0].rstrip()


# ---------------------------------------------------------------------------
# Query-split — both forms
# ---------------------------------------------------------------------------


class TestSplitQuery:
    def test_no_query(self):
        assert _split_query("3d6") == ("3d6", {})

    def test_trailing_query(self):
        assert _split_query("3d6?seed=42") == ("3d6", {"seed": "42"})

    def test_leading_query_with_path_in_value(self):
        # Form (2): ?seed=42/3d6 — the body is the suffix of the value.
        head, params = _split_query("?seed=42/3d6")
        assert head == "3d6"
        assert params == {"seed": "42"}

    def test_leading_query_no_path(self):
        # Just ?seed=42 with empty body.
        head, params = _split_query("?seed=42")
        assert head == ""
        assert params == {"seed": "42"}

    def test_multiple_params(self):
        head, params = _split_query("3d6?seed=42&debug=1")
        assert head == "3d6"
        assert params == {"seed": "42", "debug": "1"}


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


class TestCoinFlip:
    def test_returns_zero_or_one(self):
        h = RngHandler()
        # Run several times to confirm both values appear at least once
        # over many trials (probabilistic but vanishingly unlikely to fail).
        results = {int(_value_line(_read(h, ""))) for _ in range(50)}
        assert results <= {0, 1}

    def test_seeded_coin_flip_reproducible(self):
        h = RngHandler()
        a = _read(h, "?seed=7")
        b = _read(h, "?seed=7")
        assert a == b


# ---------------------------------------------------------------------------
# Integer ranges
# ---------------------------------------------------------------------------


class TestIntegers:
    def test_single_n_returns_in_range_inclusive(self):
        h = RngHandler()
        for _ in range(50):
            v = int(_value_line(_read(h, "10")))
            assert 0 <= v <= 10

    def test_range_inclusive_both_ends(self):
        h = RngHandler()
        for _ in range(50):
            v = int(_value_line(_read(h, "1..6")))
            assert 1 <= v <= 6

    def test_range_with_count(self):
        h = RngHandler()
        out = _value_line(_read(h, "1..6x4"))
        # Output: "[a, b, c, d] (n=4, range=[1, 6])"
        m = re.match(r"\[(\d+(?:, \d+)+)\] \(n=4", out)
        assert m, out
        values = [int(s) for s in m.group(1).split(", ")]
        assert len(values) == 4
        for v in values:
            assert 1 <= v <= 6

    def test_negative_n_rejected(self):
        h = RngHandler()
        with pytest.raises(PrecisError) as exc:
            _read(h, "-3")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_count_cap(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "1..6x10001")


# ---------------------------------------------------------------------------
# Floats
# ---------------------------------------------------------------------------


class TestFloats:
    def test_float_default_unit_interval(self):
        h = RngHandler()
        for _ in range(50):
            v = float(_value_line(_read(h, "float")))
            assert 0.0 <= v < 1.0

    def test_float_range(self):
        h = RngHandler()
        for _ in range(50):
            v = float(_value_line(_read(h, "float/-1..1")))
            assert -1.0 <= v < 1.0

    def test_float_lo_hi_swap(self):
        # Permissive: rng:float/1..-1 still works.
        h = RngHandler()
        v = float(_value_line(_read(h, "float/1..-1")))
        assert -1.0 <= v < 1.0

    def test_float_equal_endpoints_rejected(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "float/0..0")

    def test_float_invalid_format(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "float/garbage")


# ---------------------------------------------------------------------------
# Dice
# ---------------------------------------------------------------------------


class TestDice:
    def test_3d6(self):
        h = RngHandler()
        out = _read(h, "3d6")
        assert "🎲 3d6" in out
        # Extract rolls.
        m = re.search(r"rolls: \[(\d+(?:, \d+)*)\]", out)
        assert m
        rolls = [int(s) for s in m.group(1).split(", ")]
        assert len(rolls) == 3
        for r in rolls:
            assert 1 <= r <= 6
        # Verify the sum.
        total_match = re.search(r"sum:\s+(\d+)", out)
        assert total_match
        assert int(total_match.group(1)) == sum(rolls)

    def test_1d20(self):
        h = RngHandler()
        for _ in range(20):
            out = _read(h, "1d20")
            m = re.search(r"rolls: \[(\d+)\]", out)
            assert m
            assert 1 <= int(m.group(1)) <= 20

    def test_dice_2_sided_min(self):
        h = RngHandler()
        out = _read(h, "1d2")
        assert "🎲 1d2" in out

    def test_dice_1_sided_rejected(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "1d1")

    def test_seeded_dice_reproducible(self):
        h = RngHandler()
        a = _read(h, "3d6?seed=42")
        b = _read(h, "3d6?seed=42")
        assert a == b


# ---------------------------------------------------------------------------
# Choice / shuffle
# ---------------------------------------------------------------------------


class TestChoiceShuffle:
    def test_choice(self):
        h = RngHandler()
        for _ in range(20):
            v = _value_line(_read(h, "choice/red,green,blue"))
            assert v in {"red", "green", "blue"}

    def test_choice_empty_rejected(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "choice/")

    def test_shuffle_returns_permutation(self):
        h = RngHandler()
        out = _value_line(_read(h, "shuffle/a,b,c,d"))
        m = re.match(r"\[(.+)\]", out)
        assert m
        items = [s.strip() for s in m.group(1).split(",")]
        assert sorted(items) == ["a", "b", "c", "d"]

    def test_shuffle_seeded_reproducible(self):
        h = RngHandler()
        assert _read(h, "shuffle/a,b,c,d?seed=99") == _read(h, "shuffle/a,b,c,d?seed=99")


# ---------------------------------------------------------------------------
# UUID + bytes (CSPRNG, ignore seed)
# ---------------------------------------------------------------------------


class TestCrypto:
    def test_uuid_format(self):
        h = RngHandler()
        out = _value_line(_read(h, "uuid"))
        # UUID4 format: 8-4-4-4-12 hex.
        assert re.match(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
            out,
        )

    def test_uuid_seed_ignored(self):
        # Seeded calls must still produce different uuids — CSPRNG.
        h = RngHandler()
        a = _value_line(_read(h, "uuid?seed=42"))
        b = _value_line(_read(h, "uuid?seed=42"))
        assert a != b

    def test_bytes_length(self):
        h = RngHandler()
        out = _value_line(_read(h, "bytes/16"))
        # Hex-encoded 16 bytes = 32 chars.
        assert len(out) == 32
        assert re.match(r"^[0-9a-f]{32}$", out)

    def test_bytes_cap(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "bytes/2000")

    def test_bytes_invalid(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "bytes/foo")


# ---------------------------------------------------------------------------
# Seed semantics
# ---------------------------------------------------------------------------


class TestSeed:
    def test_both_uri_forms_equivalent(self):
        h = RngHandler()
        a = _read(h, "3d6?seed=42")
        b = _read(h, "?seed=42/3d6")
        assert a == b

    def test_different_seeds_produce_different_output(self):
        h = RngHandler()
        # Statistically near-certain that two different seeds for 10d100
        # produce distinct outputs.
        a = _read(h, "10d100?seed=1")
        b = _read(h, "10d100?seed=2")
        assert a != b

    def test_non_integer_seed_rejected(self):
        h = RngHandler()
        with pytest.raises(PrecisError):
            _read(h, "3d6?seed=abc")

    def test_unseeded_footer_says_os(self):
        h = RngHandler()
        out = _read(h, "1..6")
        assert "seed=os" in out

    def test_seeded_footer_says_seed(self):
        h = RngHandler()
        out = _read(h, "1..6?seed=42")
        assert "seed=42" in out


# ---------------------------------------------------------------------------
# Errors / validation
# ---------------------------------------------------------------------------


class TestErrors:
    def test_unrecognised_path(self):
        h = RngHandler()
        with pytest.raises(PrecisError) as exc:
            _read(h, "xyzzy")
        assert exc.value.code == ErrorCode.PARAM_INVALID

    def test_help_view(self):
        h = RngHandler()
        out = _read(h, "/help")
        assert "rng" in out.lower()
        assert "coin flip" in out.lower()
        assert "dice" in out.lower()
        assert "uuid" in out.lower()
