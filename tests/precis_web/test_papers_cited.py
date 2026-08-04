"""Cited-passage resolution for the paper detail page (?chunk=N)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

pytest.importorskip("fastapi")

from precis_web.routes.papers import _cited_chunk


class _Store:
    def __init__(self, row):
        self._row = row

        @contextmanager
        def _conn():
            class _C:
                def execute(_self, sql, params=None):
                    class _Cur:
                        def fetchone(__self):
                            return row

                    return _Cur()

            yield _C()

        self.pool = type("P", (), {"connection": staticmethod(_conn)})()


def test_cited_chunk_none_paths() -> None:
    s = _Store(None)
    assert _cited_chunk(s, 10, None) is None
    assert _cited_chunk(s, 10, "p23") is None  # page jump, not a chunk ord
    assert _cited_chunk(s, 10, "junk") is None
    assert _cited_chunk(s, 10, "3") is None  # query returns no row


def test_cited_chunk_returns_text_and_page() -> None:
    s = _Store(("the cited passage", 7))
    assert _cited_chunk(s, 10, "3") == {
        "ord": 3,
        "text": "the cited passage",
        "page": 7,
    }
    range_result = _cited_chunk(s, 10, "3..5")
    assert range_result is not None
    assert range_result["ord"] == 3  # range uses the 'from' ord


# ── ADR-0032 compound handle (``pa<ref_id>~lo..hi``, slice 1) ────────


def test_cited_chunk_accepts_compound_handle_for_same_paper() -> None:
    """The TOC displays ``pa<ref_id>~lo..hi`` handles — the Jump box and
    ``?chunk=`` must accept the same form the UI shows."""
    s = _Store(("the cited passage", 7))
    assert _cited_chunk(s, 10, "pa10~3") == {
        "ord": 3,
        "text": "the cited passage",
        "page": 7,
    }
    ranged = _cited_chunk(s, 10, "pa10~3..5")
    assert ranged is not None
    assert ranged["ord"] == 3  # range uses the low end, same as the bare form


def test_cited_chunk_rejects_compound_handle_for_a_different_paper() -> None:
    """A compound handle naming another ref's id must never resolve into
    *this* ref's chunk table — the guard, not just a wrong-answer risk."""
    s = _Store(("the cited passage", 7))
    assert _cited_chunk(s, 10, "pa99~3") is None
    assert _cited_chunk(s, 10, "pa99~3..5") is None


def test_cited_chunk_garbage_selector_returns_none_not_raise() -> None:
    s = _Store(("the cited passage", 7))
    for garbage in ("pa~3", "pa10~", "abc~3", "pa10~abc", "pa10-3", ""):
        assert _cited_chunk(s, 10, garbage) is None


def test_cited_chunk_compound_handle_equivalent_to_bare_ord() -> None:
    """``?chunk=pa10~3..5`` must resolve to the same cited chunk as the
    bare ``?chunk=3`` form — one resolver, one answer either way."""
    s = _Store(("the cited passage", 7))
    assert _cited_chunk(s, 10, "3") == _cited_chunk(s, 10, "pa10~3..5")
