"""Structured TOC builder (``build_toc_segments``) for the web nav.

Unit-level: drives the builder with a fake store returning Block-like
rows, so it needs no DB. The clustering itself is covered by the
existing toc_db markdown tests; here we assert the structured shape.
"""

from __future__ import annotations

from itertools import pairwise
from types import SimpleNamespace

from precis.utils.toc_db import _BUCKETING_THRESHOLD, build_toc_segments


class _Store:
    def __init__(self, blocks):
        self._blocks = blocks

    def list_blocks_for_ref(self, ref_id, *, pos_range=None):
        if pos_range is None:
            return list(self._blocks)
        lo, hi = pos_range
        return [b for b in self._blocks if lo <= b.pos <= hi]


def _blk(pos, keywords):
    return SimpleNamespace(pos=pos, keywords=keywords)


def test_empty_paper_yields_no_segments() -> None:
    assert build_toc_segments(store=_Store([]), ref_id=1, slug="x") == []


def test_short_range_is_one_segment_per_chunk() -> None:
    blocks = [_blk(i, [f"kw{i}"]) for i in range(5)]
    segs = build_toc_segments(store=_Store(blocks), ref_id=1, slug="smith24")
    assert len(segs) == 5
    assert segs[0] == {
        "handle": "smith24~0",
        "lo": 0,
        "hi": 0,
        "keywords": ["kw0"],
        "n": 1,
    }
    # Single-chunk segments carry the bare ``slug~pos`` handle (no range).
    assert all(s["lo"] == s["hi"] for s in segs)


def test_large_range_clusters_into_ranges() -> None:
    # Two coherent halves: keywords flip at the midpoint so the DP
    # segmentation has a real boundary to find.
    n = _BUCKETING_THRESHOLD + 20
    half = n // 2
    blocks = [
        _blk(i, ["alpha", "beta"] if i < half else ["gamma", "delta"]) for i in range(n)
    ]
    segs = build_toc_segments(store=_Store(blocks), ref_id=1, slug="p")
    # Clustered (fewer rows than chunks) and every segment spans a range.
    assert 1 < len(segs) < n
    assert segs[0]["lo"] == 0
    assert segs[-1]["hi"] == n - 1
    # Ranged handles use the lo..hi form.
    assert any(".." in s["handle"] for s in segs)
    # Contiguous, non-overlapping cover.
    for a, b in pairwise(segs):
        assert b["lo"] == a["hi"] + 1
