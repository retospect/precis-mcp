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
    assert _cited_chunk(s, 10, "3..5")["ord"] == 3  # range uses the 'from' ord
