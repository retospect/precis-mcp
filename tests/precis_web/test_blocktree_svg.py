"""Pure-function coverage for :mod:`precis_web.blocktree_svg` — the
projection/traversal/colour core behind the se/nm web reader (gr335242
items 1-3), independent of any store/handler."""

from __future__ import annotations

import pytest

from precis.blocktree.types import BlockNode, Connect, Tree
from precis_web.blocktree_svg import (
    BlockDraw,
    MemberLine,
    build_block_draws,
    collapse_depth,
    convex_hull,
    envelope_polygon,
    fill_fraction_line,
    force_colour,
    group_of,
    part_colours,
    plan_visibility,
    render_svg,
    validator_summary,
)


def _tree(**blocks: BlockNode) -> Tree[BlockNode, Connect]:
    t: Tree[BlockNode, Connect] = Tree()
    t.blocks = dict(blocks)
    return t


def _effective_envelope(_tree: object, node: BlockNode) -> str | None:
    return node.envelope


# ── level ladder ─────────────────────────────────────────────────────────


def test_collapse_depth_ladder() -> None:
    assert collapse_depth("envelope") == 0
    assert collapse_depth("interfaces") == 1
    assert collapse_depth("refined") is None
    assert collapse_depth("realized") is None


def test_collapse_depth_rejects_unknown_level() -> None:
    with pytest.raises(ValueError, match="unknown abstraction level"):
        collapse_depth("depth-3")


# ── hull / bbox ───────────────────────────────────────────────────────────


def test_convex_hull_drops_interior_points() -> None:
    pts = [(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0), (1.0, 1.0)]
    hull = convex_hull(pts)
    assert (1.0, 1.0) not in hull
    assert set(hull) == {(0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0)}


def test_envelope_polygon_box_projects_to_its_footprint() -> None:
    # box:w1d1h1 is centred in x/y, base at z=0 (cad.tessellate's own
    # convention) -> a top-view (axis='z') footprint spanning [-0.5, 0.5].
    poly = envelope_polygon(
        "box:w1d1h1", pose=[0.0, 0.0, 0.0], rot=[0.0, 0.0, 0.0], axis="z"
    )
    assert poly is not None
    us = [p[0] for p in poly]
    vs = [p[1] for p in poly]
    assert min(us) == pytest.approx(-0.5) and max(us) == pytest.approx(0.5)
    assert min(vs) == pytest.approx(-0.5) and max(vs) == pytest.approx(0.5)


def test_envelope_polygon_honest_absence_on_bad_config() -> None:
    assert (
        envelope_polygon("not-a-shape", pose=[0, 0, 0], rot=[0, 0, 0], axis="z") is None
    )
    # chamfer is an unbounded half-space — no finite mesh to project.
    assert (
        envelope_polygon("chamfer:1x45", pose=[0, 0, 0], rot=[0, 0, 0], axis="z")
        is None
    )


# ── traversal / plan_visibility ──────────────────────────────────────────


def _fork_tree() -> Tree[BlockNode, Connect]:
    return _tree(
        hub=BlockNode(name="hub", pose=[0, 0, 0], envelope="cyl:r0.02h0.05"),
        fork=BlockNode(name="fork", pose=[0, 0, -0.1], envelope="box:w0.04d0.02h0.08"),
        fork_arm=BlockNode(
            name="fork_arm",
            parent="fork",
            pose=[0, 0, -0.15],
            envelope="box:w0.01d0.01h0.05",
        ),
        fork_tip=BlockNode(
            name="fork_tip",
            parent="fork_arm",
            pose=[0, 0, -0.18],
            envelope="sphere:r0.005",
        ),
    )


def test_plan_visibility_envelope_level_collapses_fork_to_a_box() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(tree, children_map(tree), level="envelope", isolate=None)
    assert plan.shown["hub"] == "shape"  # a genuine leaf, never collapsed
    assert plan.shown["fork"] == "box"
    assert "fork_arm" not in plan.shown
    assert "fork_tip" not in plan.shown


def test_plan_visibility_interfaces_level_reveals_one_more_layer() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(tree, children_map(tree), level="interfaces", isolate=None)
    assert plan.shown["fork"] == "shape"
    assert plan.shown["fork_arm"] == "box"
    assert "fork_tip" not in plan.shown


def test_plan_visibility_refined_level_shows_every_leaf() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(tree, children_map(tree), level="refined", isolate=None)
    assert plan.shown["fork"] == "shape"
    assert plan.shown["fork_arm"] == "shape"
    assert plan.shown["fork_tip"] == "shape"


def test_plan_visibility_isolate_narrows_to_one_subtree() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(tree, children_map(tree), level="refined", isolate="fork")
    assert "hub" not in plan.shown
    assert plan.shown["fork"] == "shape"
    assert plan.shown["fork_tip"] == "shape"


def test_plan_visibility_unknown_isolate_raises_key_error() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    with pytest.raises(KeyError):
        plan_visibility(tree, children_map(tree), level="refined", isolate="nope")


def test_plan_visibility_stops_on_a_stored_parent_cycle() -> None:
    # the render path runs BEFORE adapter.validate (which would normally
    # catch a stored parent-cycle) -- a two-block mutual-parent cycle must
    # terminate, not hang, once reached via isolate=.
    tree = _tree(
        a=BlockNode(name="a", parent="b", envelope="cyl:r1h1"),
        b=BlockNode(name="b", parent="a", envelope="cyl:r1h1"),
    )
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(tree, children_map(tree), level="refined", isolate="a")
    assert plan.shown == {"a": "shape", "b": "shape"}


# ── per-subtree level override (round 2a) ────────────────────────────────


def test_plan_visibility_override_reveals_one_subtree_at_a_different_level() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    # ambient level collapses everything at the root -- but 'fork' itself
    # is overridden to 'refined', so ITS subtree still shows every leaf.
    plan = plan_visibility(
        tree,
        children_map(tree),
        level="envelope",
        isolate=None,
        level_overrides={"fork": "refined"},
    )
    assert plan.shown["hub"] == "shape"  # unaffected, still a genuine leaf
    assert plan.shown["fork"] == "shape"
    assert plan.shown["fork_arm"] == "shape"
    assert plan.shown["fork_tip"] == "shape"


def test_plan_visibility_override_can_collapse_deeper_than_ambient() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    plan = plan_visibility(
        tree,
        children_map(tree),
        level="refined",
        isolate=None,
        level_overrides={"fork": "envelope"},
    )
    assert plan.shown["hub"] == "shape"
    assert plan.shown["fork"] == "box"
    assert "fork_arm" not in plan.shown
    assert "fork_tip" not in plan.shown


def test_plan_visibility_override_force_opens_ancestors_to_reach_a_nested_target() -> (
    None
):
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    # ambient level 'envelope' would normally collapse 'fork' immediately
    # (depth 0) and never even reach 'fork_arm' -- overriding fork_arm's
    # OWN level must force 'fork' open as a pass-through so the override
    # target is actually reachable.
    plan = plan_visibility(
        tree,
        children_map(tree),
        level="envelope",
        isolate=None,
        level_overrides={"fork_arm": "refined"},
    )
    assert plan.shown["fork"] == "shape"  # forced open, not collapsed
    assert plan.shown["fork_arm"] == "shape"
    assert plan.shown["fork_tip"] == "shape"


def test_plan_visibility_override_of_unreachable_name_is_silently_dropped() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    # isolate narrows to 'fork' -- an override naming 'hub' (outside that
    # subtree) must not raise or otherwise disturb the render.
    plan = plan_visibility(
        tree,
        children_map(tree),
        level="envelope",
        isolate="fork",
        level_overrides={"hub": "refined"},
    )
    assert "hub" not in plan.shown
    assert plan.shown["fork"] == "box"


def test_plan_visibility_override_rejects_unknown_level_name() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    with pytest.raises(ValueError, match="unknown abstraction level"):
        plan_visibility(
            tree,
            children_map(tree),
            level="refined",
            isolate=None,
            level_overrides={"fork": "nope"},
        )


def test_descendants_stops_on_a_stored_cycle() -> None:
    from precis_web.blocktree_svg import _descendants

    kids = {"a": ["b"], "b": ["a"]}
    assert set(_descendants("a", kids)) == {"b"}


def test_build_block_draws_box_covers_hidden_descendant_extent() -> None:
    tree = _fork_tree()
    from precis_web.blocktree_svg import children_map

    kids = children_map(tree)
    plan = plan_visibility(tree, kids, level="envelope", isolate=None)
    draws = build_block_draws(tree, _effective_envelope, kids, plan, "z")
    fork_draw = next(d for d in draws if d.name == "fork")
    assert fork_draw.kind == "box"
    # the box must at least cover fork's own footprint (w0.04) — a
    # collapsed subtree can never claim LESS extent than what it hides.
    us = [p[0] for p in fork_draw.polygon]
    assert max(us) - min(us) >= 0.04 - 1e-9


# ── grouping / colour ─────────────────────────────────────────────────────


def test_group_of_walks_up_to_the_render_root() -> None:
    tree = _fork_tree()
    boundary = {"hub", "fork"}
    assert group_of(tree, "fork_tip", boundary) == "fork"
    assert group_of(tree, "hub", boundary) == "hub"


def test_part_colours_are_deterministic_and_distinct() -> None:
    m1 = part_colours(["a", "b", "c"])
    m2 = part_colours(["c", "b", "a"])
    assert m1 == m2
    assert len({m1["a"], m1["b"], m1["c"]}) == 3


def test_force_colour_tie_strut_rod_undeclared() -> None:
    assert force_colour("tie", None) == "#16a34a"
    assert force_colour("strut", None) == "#dc2626"
    assert force_colour("rod", 5.0) == "#16a34a"
    assert force_colour("rod", -5.0) == "#dc2626"
    assert force_colour("rod", None) == "#94a3b8"
    assert force_colour("undeclared", None) == "#94a3b8"


# ── honesty header ────────────────────────────────────────────────────────


def test_fill_fraction_line_empty_tree() -> None:
    assert "0/0" in fill_fraction_line(_tree(), _effective_envelope)


def test_fill_fraction_line_counts_ordinary_blocks_with_envelopes() -> None:
    tree = _tree(
        a=BlockNode(name="a", envelope="cyl:r1h1"),
        b=BlockNode(name="b", envelope=None),
    )
    line = fill_fraction_line(tree, _effective_envelope)
    assert line.startswith("1/2")


class _Finding:
    def __init__(self, severity: str) -> None:
        self.severity = severity


def test_validator_summary_tiers() -> None:
    assert validator_summary([]) == ("no validator findings", "ok")
    line, tier = validator_summary([_Finding("warn")])
    assert tier == "warn" and "1 warning" in line
    line, tier = validator_summary([_Finding("error"), _Finding("warn")])
    assert tier == "error"


# ── SVG emission smoke test ────────────────────────────────────────────────


def test_render_svg_contains_header_and_polygons_and_member_lines() -> None:
    draws = [
        BlockDraw(
            name="hub",
            polygon=[(0, 0), (1, 0), (1, 1), (0, 1)],
            kind="shape",
            group="hub",
        ),
    ]
    members = [
        MemberLine(a=(0, 0), b=(1, 1), colour="#16a34a", subject="hub.pin—rim.pin")
    ]
    svg = render_svg(
        draws,
        members,
        channel="part",
        header_lines=["stability: rigid", "1/1 block(s) have envelopes (L1 filled)"],
        tier="ok",
    )
    assert svg.startswith("<svg")
    assert "stability: rigid" in svg
    assert "<title>hub</title>" in svg
    assert "hub.pin—rim.pin" in svg
    assert "#16a34a" in svg
