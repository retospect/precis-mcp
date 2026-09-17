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

import numpy as np

from precis_se.ops import SeTree, apply_ops
from precis_se.validate import _aabb_clear, envelope_overlaps, validate


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


# ---------------------------------------------------------------------------
# _aabb_clear direct unit tests — pin the subtraction direction
# (lo_a - hi_b / lo_b - hi_a), not just its sign.
# ---------------------------------------------------------------------------


def test_aabb_clear_true_for_a_genuinely_separated_pair() -> None:
    """Two boxes far apart in the negative-coordinate region: box_a's low
    corner is well past box_b's high corner along every axis, so the real
    separation (``lo_a - hi_b``) is a large positive number — clearly
    separated. Flipping the subtraction to addition (``lo_a + hi_b``) sums
    two negative-ish coordinates into a value that no longer tracks the
    real gap, and (as designed below) evaluates <= margin on both terms,
    flipping the verdict to "not clear"."""
    box_a = (np.array([-2.0, -2.0, -2.0]), np.array([-1.0, -1.0, -1.0]))
    box_b = (np.array([-10.0, -10.0, -10.0]), np.array([-9.0, -9.0, -9.0]))

    assert _aabb_clear(box_a, box_b, margin=0.1) is True


def test_aabb_clear_false_for_an_overlapping_pair_at_large_coordinates() -> None:
    """Two overlapping boxes positioned far from the origin (large,
    same-signed coordinates) so that ``lo_a - hi_b`` correctly stays small
    (they overlap) while the mutated ``lo_a + hi_b`` would sum to a large
    positive number and wrongly clear the pair."""
    box_a = (np.array([100.0, 100.0, 100.0]), np.array([101.0, 101.0, 101.0]))
    box_b = (np.array([99.6, 99.6, 99.6]), np.array([100.6, 100.6, 100.6]))

    assert _aabb_clear(box_a, box_b, margin=0.05) is False


def test_aabb_clear_false_for_a_touching_pair_per_axis() -> None:
    """Boxes that exactly touch (zero gap) along the probed axis, with the
    other axes offset into negative coordinates — pins per-axis behaviour
    of both ``lo_a - hi_b`` and ``lo_b - hi_a`` at once."""
    box_a = (np.array([-5.0, 0.0, -5.0]), np.array([-4.0, 1.0, -4.0]))
    box_b = (np.array([-4.0, -1.0, -6.0]), np.array([-3.0, 0.0, -5.0]))

    assert _aabb_clear(box_a, box_b, margin=0.0) is False


# ---------------------------------------------------------------------------
# diag > 0.0 filter — a real (nonzero) margin must gate a sub-margin gap
# into the narrow phase, not let the broad phase clear it outright.
# ---------------------------------------------------------------------------


def test_subcontact_gap_is_not_aabb_cleared_reaches_narrow_phase() -> None:
    """Two unit cubes (diag = sqrt(3), CONTACT_TOL_REL=1e-3 → margin ≈
    1.732e-3 m) separated by a 1 mm gap — bigger than zero, but well
    inside the pair's own margin. The broad phase must NOT clear this
    pair (a positive margin computed off the diag keeps it in the narrow
    phase); with ``budget_s=0.0`` that means it lands in
    ``unchecked_budget`` rather than vanishing. If the diag>0.0 filter
    were flipped to <=0.0, ``diags`` would always be empty for real
    (positive-diagonal) boxes, margin would collapse to 0.0, and this
    genuinely-positive 1 mm gap would satisfy ``lo_b - hi_a > 0`` and be
    wrongly cleared — the pair would disappear from every bucket."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "p", "envelope": "box:w1d1h1"},
            {
                "op": "add_block",
                "name": "q",
                "envelope": "box:w1d1h1",
                "pose": [1.001, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)

    assert cross_scale == []
    assert overlaps == []
    assert unchecked == [("p", "q")]


# ---------------------------------------------------------------------------
# continue vs. break — losing the rest of an a's inner loop must not
# silently drop a later, genuinely-reportable pair.
# ---------------------------------------------------------------------------


def test_aabb_cleared_pair_does_not_stop_the_inner_loop() -> None:
    """``a`` pairs with two later blocks in sorted-name order: ``b`` (far
    away — AABB-cleared, the ``continue`` arm) then ``c`` (overlapping —
    must still be reported). If the AABB-clear ``continue`` were a
    ``break``, the inner loop over ``a``'s remaining partners would stop
    at ``b`` and never even reach ``c``, losing a real overlap."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": "box:w1d1h1"},
            {
                "op": "add_block",
                "name": "b",
                "envelope": "box:w1d1h1",
                "pose": [1000, 0, 0],
            },
            {
                "op": "add_block",
                "name": "c",
                "envelope": "box:w1d1h1",
                "pose": [0.5, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree)

    assert cross_scale == []
    assert unchecked == []
    pairs = {frozenset((x, y)): gap for x, y, gap in overlaps}
    assert pairs.get(frozenset(("a", "c"))) == -0.5
    assert frozenset(("a", "b")) not in pairs


def test_budget_exceeded_reports_every_non_cleared_pair_not_just_the_first() -> None:
    """Three mutually overlapping unit cubes (all overlapping, none
    AABB-clear) with ``budget_s=0.0`` — every one of the three pairs must
    land in ``unchecked_budget``. If the post-budget-check ``continue``
    were a ``break``, the inner loop over the first block's partners would
    stop after recording the first pair, silently losing the second."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": "box:w1d1h1"},
            {
                "op": "add_block",
                "name": "m",
                "envelope": "box:w1d1h1",
                "pose": [0.5, 0, 0],
            },
            {
                "op": "add_block",
                "name": "z",
                "envelope": "box:w1d1h1",
                "pose": [-0.5, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)

    assert cross_scale == []
    assert overlaps == []
    assert set(unchecked) == {("a", "m"), ("a", "z"), ("m", "z")}


# ---------------------------------------------------------------------------
# deadline `is not None` guards — the unbounded (None) path must never be
# budget-checked, and a bounded 0.0 budget must still be honestly partial.
# ---------------------------------------------------------------------------


def test_unbounded_budget_never_marks_a_close_pair_unchecked() -> None:
    """``budget_s=None`` is the "unbounded — tests only" escape hatch
    (module docstring): the deadline must stay ``None`` and the
    ``deadline is not None`` guard must stay closed for every pair, so a
    genuinely overlapping pair is always resolved by the real narrow-phase
    check, never shunted into ``unchecked_budget``."""
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

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=None)

    assert cross_scale == []
    assert unchecked == []
    pairs = {frozenset((x, y)): gap for x, y, gap in overlaps}
    assert pairs.get(frozenset(("a", "b"))) == -0.5


def test_zero_budget_single_close_pair_is_unchecked_not_computed() -> None:
    """Minimal echo of the budget-exceeded shape with exactly one pair (no
    spread blocks) — pins that a bounded ``budget_s=0.0`` still routes a
    genuinely-overlapping pair to ``unchecked_budget`` rather than letting
    it fall through to a real (and wrong, because unbudgeted) narrow-phase
    result."""
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

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)

    assert cross_scale == []
    assert overlaps == []
    assert unchecked == [("a", "b")]


# ---------------------------------------------------------------------------
# overlap_budget_exceeded finding text — exact shown-pairs slice, the
# "(+N more)" arithmetic, and the budget_desc branch.
# ---------------------------------------------------------------------------


def test_overlap_budget_exceeded_detail_shows_five_and_counts_the_rest() -> None:
    """Four fully-coincident unit cubes give exactly 6 pairwise
    combinations (C(4,2)), all mutually overlapping (none AABB-clear); with
    ``budget_s=0.0`` all 6 land in ``unchecked_budget``. This pins the
    finding text's exact shape: only the first 5 (in the same
    ``sorted(tree.blocks)``-then-combinations order ``envelope_overlaps``
    produces) are named, "+1 more" accounts for the 6th precisely (not
    off-by-one either direction), and the budget is rendered as "0s" (not
    "0.0s" or "unbounded" — the ``budget_s is not None`` branch)."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": f"n{i}", "envelope": "box:w1d1h1"}
            for i in range(4)
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)
    assert overlaps == []
    assert cross_scale == []
    assert len(unchecked) == 6

    findings = validate(tree, budget_s=0.0)
    rules = {f.rule for f in findings}
    assert "overlap_budget_exceeded" in rules
    assert "undeclared_interpenetration" not in rules
    finding = next(f for f in findings if f.rule == "overlap_budget_exceeded")
    assert finding.subject == "6 pair(s)"
    shown_pairs = [f"{a}—{b}" for a, b in unchecked[:5]]
    shown_text = ", ".join(shown_pairs)
    assert shown_text in finding.detail
    sixth_a, sixth_b = unchecked[5]
    assert f"{sixth_a}—{sixth_b}" not in shown_text
    assert " (+1 more)" in finding.detail
    assert "0s time budget" in finding.detail
    assert "0.0s" not in finding.detail
    assert "unbounded" not in finding.detail


# ---------------------------------------------------------------------------
# gr338945 — 3 mutation survivors after the round above: ``_aabb_diag``'s
# subtraction direction was only pinned at the origin (where translating
# the diagonal off-centre still leaves ``hi - lo`` unchanged but flips what
# ``hi + lo`` would compute), the budget deadline's sign was only exercised
# at ``budget_s`` values (``None``/``0.0``) blind to its direction, and the
# ancestor/descendant skip's ``continue`` had no case where losing the rest
# of the inner loop (a ``break``) would drop a real, later pair.
# ---------------------------------------------------------------------------


def test_aabb_diag_margin_is_translation_invariant_off_origin() -> None:
    """``_aabb_diag`` must compute ``hi - lo`` (a size, invariant under
    translating the pair), not ``hi + lo`` (which tracks absolute position).
    Every existing pair in this module straddles the origin symmetrically,
    where a pose chosen to make ``hi + lo`` collapse toward zero for one box
    still leaves the *other* box's mutant diagonal large enough to produce a
    margin that (coincidentally) still gates the same sub/super-margin
    outcome as the real ``hi - lo`` value — the flip is invisible there.
    Off-centre poses break that coincidence: two unit cubes 1.0005 m apart
    (a 0.5 mm gap — inside the real ``margin`` ≈ 1.732 mm for a unit cube,
    so the broad phase must NOT clear this pair) placed so ``b`` sits just
    past the origin (pose 0.001) and ``a`` trails behind it (pose -0.9995).
    ``b``'s mutant diagonal (``hi + lo`` ≈ 2×0.001) collapses to near zero,
    dragging the mutant margin down to ~2e-6 m — far below the real 0.5 mm
    gap — so the mutant wrongly clears the pair outright (it never reaches
    the budget check and disappears from every bucket), while the real
    ``hi - lo`` margin correctly keeps it un-cleared, landing in
    ``unchecked_budget`` under a zero budget."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {
                "op": "add_block",
                "name": "a",
                "envelope": "box:w1d1h1",
                "pose": [-0.9995, 0, 0],
            },
            {
                "op": "add_block",
                "name": "b",
                "envelope": "box:w1d1h1",
                "pose": [0.001, 0, 0],
            },
        ],
    )

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=0.0)

    assert cross_scale == []
    assert overlaps == []
    assert unchecked == [("a", "b")], (
        "sub-margin pair vanished from every bucket — the broad phase "
        "wrongly cleared it"
    )


def test_generous_budget_still_computes_a_real_overlap_not_unchecked() -> None:
    """``deadline = time.monotonic() + budget_s`` must push the deadline
    into the *future* — flipping the sign puts it in the past, so the very
    first non-AABB-cleared pair immediately reads as past-deadline and
    lands in ``unchecked_budget`` no matter how generous ``budget_s`` is.
    The existing budget tests only use ``budget_s=None`` (deadline stays
    ``None``, blind to sign) or ``budget_s=0.0`` (``now + 0`` and
    ``now - 0`` are the same instant, also blind to sign); a large finite
    budget on a genuinely close pair is the case that actually depends on
    the deadline landing in the future."""
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

    overlaps, cross_scale, unchecked = envelope_overlaps(tree, budget_s=60.0)

    assert cross_scale == []
    assert unchecked == [], (
        "a 60s budget should never be exhausted by one close pair — a "
        "negative deadline would exhaust it instantly"
    )
    pairs = {frozenset((x, y)): gap for x, y, gap in overlaps}
    assert pairs.get(frozenset(("a", "b"))) == -0.5


def test_ancestor_skip_continue_does_not_stop_the_inner_loop() -> None:
    """The ancestor/descendant skip (``if _is_ancestor(...) or
    _is_ancestor(...): continue``) must only skip that one pair, not abort
    the rest of ``a``'s inner loop. ``a_child``'s parent is ``a`` and sorts
    immediately after it, so it is the *first* partner ``a`` considers;
    ``b`` (a genuine overlap with ``a``) sorts after ``a_child`` and is
    only reached because the skip is a ``continue``. If it were a
    ``break``, the inner loop would stop at the ancestor pair and never
    even evaluate ``a``/``b``, silently losing a real overlap — distinct
    from ``test_aabb_cleared_pair_does_not_stop_the_inner_loop`` above,
    which pins the same shape for the AABB-clear ``continue`` a few lines
    later, not this ancestor-skip one."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "a", "envelope": "box:w1d1h1"},
            {
                "op": "add_block",
                "name": "a_child",
                "envelope": "box:w1d1h1",
                "parent": "a",
                "pose": [1000, 0, 0],
            },
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
    assert pairs.get(frozenset(("a", "b"))) == -0.5
    assert frozenset(("a", "a_child")) not in pairs
