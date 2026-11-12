"""Word handles: reversible number <-> words map and the ``handle`` kind (gr648264)."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handle_words import _ADJECTIVES, _COLORS, _NOUNS, to_number, to_words
from precis.handlers.handle import HandleHandler


def test_lists_distinct() -> None:
    allw = _COLORS + _ADJECTIVES + _NOUNS
    assert len(allw) == len(set(allw)) == 384


@pytest.mark.parametrize(
    "n", [0, 1, 127, 128, 16383, 16384, 648264, 2097151, 2097152, 10**9]
)
def test_roundtrip(n: int) -> None:
    assert to_number(to_words(n)) == n


def test_known_and_distinct() -> None:
    assert to_words(0).split() == [_COLORS[0], _ADJECTIVES[0], _NOUNS[0]]
    assert len(to_words(648264).split()) == 3
    assert len({to_words(i) for i in range(5000)}) == 5000
    assert len(to_words(2097152).split()) == 4


def test_parse_tolerant_and_strict() -> None:
    w = to_words(648264)
    assert to_number(w.upper().replace(" ", "-")) == 648264
    with pytest.raises(ValueError):
        to_number("amber brave")
    with pytest.raises(ValueError):
        to_number("zzz brave dog")
    with pytest.raises(ValueError):
        to_number(f"{_COLORS[0]} {to_words(5)}")


def test_handler_both_directions() -> None:
    h = HandleHandler(hub=Hub())
    words = to_words(648264)
    assert words in h.get(id=648264).body
    assert "648264" in h.get(q=words).body
    assert "648264" in h.get(id=words).body
    with pytest.raises(BadInput):
        h.get()
    with pytest.raises(BadInput):
        h.get(q="not a handle")
