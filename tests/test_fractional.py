"""Fractional indexing + handle minting (ADR 0033 §§1–2)."""

from __future__ import annotations

import random

import pytest

from precis.utils.fractional import (
    even_keys,
    key_between,
    n_keys_between,
)
from precis.utils.handles import ALPHABET, HANDLE_LEN, is_handle, new_handle


def test_key_between_open_ends():
    assert key_between(None, None) > ""
    assert key_between(None, "V") < "V"
    assert key_between("V", None) > "V"


def test_key_between_strictly_between():
    a, b = "V", "W"
    c = key_between(a, b)
    assert a < c < b


def test_key_between_rejects_out_of_order():
    with pytest.raises(ValueError):
        key_between("W", "V")
    with pytest.raises(ValueError):
        key_between("V", "V")


def test_repeated_insert_into_same_gap_stays_ordered():
    # Insert 200x between the same two neighbours; order must hold each time.
    lo, hi = "V", "W"
    left = lo
    for _ in range(200):
        c = key_between(left, hi)
        assert left < c < hi
        left = c


def test_n_keys_between_ascending_and_strict():
    keys = n_keys_between("A", "z", 50)
    assert keys == sorted(keys)
    assert len(set(keys)) == 50
    assert all("A" < k < "z" for k in keys)


def test_even_keys_ascending_unique():
    for n in (1, 5, 30, 61, 200):
        ks = even_keys(n)
        assert len(ks) == n
        assert ks == sorted(ks)
        assert len(set(ks)) == n


def test_property_random_inserts_keep_global_order():
    """The real proof: thousands of random insertions into random
    positions must keep the whole sequence strictly ordered and unique.
    """
    rng = random.Random(0xC0FFEE)
    # start with one key
    seq = [key_between(None, None)]
    for _ in range(4000):
        i = rng.randint(0, len(seq))  # gap index: before/between/after
        a = seq[i - 1] if i > 0 else None
        b = seq[i] if i < len(seq) else None
        c = key_between(a, b)
        seq.insert(i, c)
        # invariant after every insert
        if a is not None:
            assert a < c
        if b is not None:
            assert c < b
    assert seq == sorted(seq), "global order broke"
    assert len(set(seq)) == len(seq), "duplicate key minted"


def test_handle_shape():
    h = new_handle()
    assert is_handle(h)
    assert len(h) == HANDLE_LEN
    assert all(c in ALPHABET for c in h)
    for bad in ("0OlI", "short", "toolong7", "!!!!!!"):
        assert not is_handle(bad)


def test_handle_no_ambiguous_chars():
    # 5k handles, never an ambiguous char
    for _ in range(5000):
        assert not (set(new_handle()) & set("0OlI"))


def test_handle_randomness():
    # not all identical (sanity)
    assert len({new_handle() for _ in range(1000)}) > 900
