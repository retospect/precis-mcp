"""TOC round-trip: every segment ``build_toc_segments`` emits must resolve
via the chunk selector (``_cited_chunk`` — the shared resolver behind the
web ``/chunk/{sel}`` route, ``?chunk=``, and the Jump box).

Regression for the paper-viewer-nav slice 1 gap: the TOC shows a segment's
``lo`` (and the compound ``pa<id>~lo..hi`` handle built from it), but
nothing in the UI actually accepted those forms before this slice — a row
click, or a pasted handle, was a dead end. This drives the real
``build_toc_segments`` clustering against a fake store shared by both
halves (``list_blocks_for_ref`` for the TOC, ``pool.connection`` for the
chunk lookup), so a segment's own ``lo`` is fed straight back into the
resolver, same as a TOC click does.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from precis.utils.toc_db import _BUCKETING_THRESHOLD, build_toc_segments
from precis_web.routes.papers import _cited_chunk


class _Store:
    """Backs both ``list_blocks_for_ref`` (TOC clustering) and
    ``pool.connection`` (``_cited_chunk``'s raw chunk lookup) off the same
    seeded ``{pos: (text, page)}`` rows."""

    def __init__(self, rows: dict[int, tuple[str, int]]) -> None:
        self._rows = rows

        @contextmanager
        def _conn():
            class _C:
                def execute(_self, sql, params=None):
                    _ref_id, ord_ = params

                    class _Cur:
                        def fetchone(__self):
                            return rows.get(ord_)

                    return _Cur()

            yield _C()

        self.pool = type("P", (), {"connection": staticmethod(_conn)})()

    def list_blocks_for_ref(self, ref_id: int, *, pos_range=None):
        items = sorted(self._rows.items())
        if pos_range is not None:
            lo, hi = pos_range
            items = [(p, r) for p, r in items if lo <= p <= hi]
        # Two coherent halves (keywords flip at the midpoint) so the DP
        # segmentation has a real boundary and emits multi-chunk ranges,
        # not just a per-chunk fallback.
        half = len(self._rows) // 2
        return [
            SimpleNamespace(
                pos=p, keywords=["alpha", "beta"] if p < half else ["gamma", "delta"]
            )
            for p, _r in items
        ]


def test_every_toc_segment_lo_resolves_to_a_chunk() -> None:
    n = _BUCKETING_THRESHOLD + 30
    rows = {i: (f"chunk {i} body text", (i // 10) + 1) for i in range(n)}
    store = _Store(rows)
    segs = build_toc_segments(store=store, ref_id=10, handle="pa10")
    assert 1 < len(segs) < n  # clustering actually produced ranged rows

    for seg in segs:
        cited = _cited_chunk(store, 10, str(seg["lo"]))
        assert cited is not None, f"segment lo={seg['lo']} did not resolve"
        assert cited["ord"] == seg["lo"]
        # The segment's own compound handle (what the TOC row displays,
        # e.g. "pa10~13..15") resolves identically.
        assert _cited_chunk(store, 10, seg["handle"]) == cited
