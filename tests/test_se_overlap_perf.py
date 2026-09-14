"""``envelope_overlaps``'s broad-phase AABB prefilter + explicit budget
(gr337045).

Root cause: the undeclared-interpenetration check
(:func:`precis_se.validate.envelope_overlaps`) ran a full numeric SDF
clearance minimisation (:func:`precis.cad.relate.clearance`) over *every*
unordered block pair, unbounded — a flat ~2.3s/pair regardless of overlap,
so a 29-block design (~400 pairs) projected to ~15 minutes inside one
synchronous ``view='validate'`` MCP call, and hung the server
uncancellably. The fix adds a cheap AABB broad phase (skips pairs whose
posed envelopes cannot possibly overlap without ever running the SDF
descent) and a wall-clock budget on whatever narrow-phase work remains,
with an honest ``overlap_budget_exceeded`` finding — never a silent
truncation — for whatever the budget didn't reach.

These tests build a synthetic :class:`~precis_se.ops.SeTree` directly (no
store/DB/MCP), the same shape the gr337045 investigation used to measure
the ~2.3s/pair baseline.
"""

from __future__ import annotations

import time

from precis_se.ops import SeTree, apply_ops
from precis_se.validate import envelope_overlaps, validate


def _spread_ops(n: int, *, pitch: float = 10.0) -> list[dict]:
    """``n`` unit boxes strung out along x, ``pitch`` apart — every pair's
    AABBs are separated by ``pitch - 1`` (unit box half-width 0.5 each
    side), far past any contact tolerance, so the broad phase should clear
    all of them without ever touching the SDF kernel."""
    return [
        {
            "op": "add_block",
            "name": f"b{i}",
            "envelope": "box:w1d1h1",
            "pose": [i * pitch, 0, 0],
        }
        for i in range(n)
    ]


def test_spread_29_block_design_validates_near_instantly() -> None:
    """The gr337045 repro shape: 29 blocks, no ancestor relations — 406
    unordered pairs. Uncapped, the dossier measured this class of design
    at a flat ~2.3s/pair (projected ~15 minutes here); the AABB broad
    phase should clear every pair for the cost of a couple of bounding-box
    lookups each, so the whole check completes in a small fraction of a
    second. The bound below (5s) is generous relative to that — nowhere
    near the ~120s the MCP harness saw hang, let alone the ~15 minute
    projection — while still catching a regression back to the O(n²)×SDF
    shape."""
    tree = SeTree()
    apply_ops(tree, _spread_ops(29))

    t0 = time.monotonic()
    overlaps, cross_scale, unchecked = envelope_overlaps(tree)
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"took {elapsed:.2f}s — broad phase not filtering"
    assert overlaps == []
    assert cross_scale == []
    assert unchecked == []


def test_spread_design_validate_reports_clean_and_fast() -> None:
    """Same shape through the actual ``validate()`` entry point the
    handler's ``view='validate'`` dispatches to — no findings (nothing
    overlaps, nothing connected, no ports), and fast."""
    tree = SeTree()
    apply_ops(tree, _spread_ops(29))

    t0 = time.monotonic()
    findings = validate(tree)
    elapsed = time.monotonic() - t0

    assert elapsed < 5.0, f"took {elapsed:.2f}s"
    assert findings == []


def test_close_overlapping_pair_still_gets_a_real_clearance_gap() -> None:
    """The broad phase must never weaken the reported depth for a pair
    that genuinely interferes — only skip work whose outcome (no overlap)
    the AABB test already proved. Two identical unit cubes offset by 0.5
    along x interpenetrate by exactly half their width; this pins the
    narrow-phase result (unchanged by the prefilter) rather than merely
    asserting a nonzero sign."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": "box:w1d1h1"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "box:w1d1h1",
                "pose": [0.5, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree)

    assert cross_scale == []
    assert unchecked == []
    pairs = {frozenset((x, y)): gap for x, y, gap in overlaps}
    gap = pairs.get(frozenset(("a", "b")))
    assert gap is not None, overlaps
    assert gap == -0.5


def test_overlap_budget_exceeded_marks_the_result_partial_not_silent() -> None:
    """A tiny ``budget_s`` must not silently drop the pair(s) it didn't
    reach — they come back in ``unchecked_budget``, and ``validate()``
    raises an explicit ``overlap_budget_exceeded`` finding naming them,
    mirroring the existing ``cross_scale_unverifiable`` honesty shape.
    Five far-apart blocks (cleared by the broad phase regardless of
    budget) plus one genuinely overlapping pair, well clear of the spread
    blocks, isolates the budget path to exactly that pair."""
    tree = SeTree()
    apply_ops(tree, _spread_ops(5))
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "c1",
                "envelope": "box:w1d1h1",
                "pose": [100, 0, 0],
            },
            {
                "op": "add_block",
                "name": "c2",
                "envelope": "box:w1d1h1",
                "pose": [100.5, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)
    assert cross_scale == []
    assert unchecked == [("c1", "c2")]
    assert overlaps == []  # never reached the real SDF check — not "clear"

    findings = validate(tree, budget_s=0.0)
    rules = {f.rule for f in findings}
    assert "overlap_budget_exceeded" in rules
    assert "undeclared_interpenetration" not in rules  # not silently "clean" either
    budget_finding = next(f for f in findings if f.rule == "overlap_budget_exceeded")
    assert "c1" in budget_finding.detail and "c2" in budget_finding.detail
