"""gr524390: the fasten stack is measured on each member's **realized
solid** — the bound cad design's folded SDF (the same resolution the cad
inspect probes use, :func:`precis_se.rays_macro.block_geoms`), else its
envelope — never on a bounding-envelope guess.

The fixture is se-box2 in miniature, the shape the gripe numbers came
from: a cad-bound box whose solid is a floor plate with an Ø8 mm boss
rising to z=60 mm, a 4 mm envelope-only lid at z=60→64, and an M4×12
socket cap driving ``−z`` from the lid's top face. On envelopes the
walk reported "member box 0 m → 137 mm" for the 60 mm part, the lid
dropped out of the stack entirely, and the core-hole origin floated 4 mm
above the boss top. Every expected number below is checkable by hand
against those poses, which is the point.

Same posture as `test_se_fasten.py` — a hand-built :class:`SeTree`, real
poses in metres, no store. The cad binding is wired through the tree's
own :attr:`SeTree.cad_designs` resolver slot (what
:func:`precis_se.persist.load_tree` fills from the store), holding a
design built straight from scene source.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis.cad.scene import build_design, parse_source
from precis_se import catalog, fasten
from precis_se.ops import ConnectSpec, SeBlock, SeTree

#: ISO 4762 M4×12 socket cap, metres — the same spec shape the other
#: fasten tests use.
M4_CAP = {
    "thread_size": "M4",
    "thread_pitch": 0.0007,
    "outer_diameter": 0.004,
    "head_diameter": 0.007,
    "head_height": 0.004,
    "length": 0.012,
    "drive_size": 0.003,
    "drive_type": "socket",
    "head_form": "cap",
}

#: The box's bound cad design: a 4 mm floor plate plus the Ø8 mm boss
#: standing on it, boss top at local z=60 mm — placed at the world
#: origin, so the boss top lands at world z=0.060 where the lid sits.
BOX_CAD = (
    "component p\nplate add box:w60mmd60mmh4mm\nboss  add cyl:r4mmh56mm @0mm,0mm,4mm\n"
)

LID = "box:w0.06d0.06h0.004"


def _bought(name: str, pose: list[float], rot: list[float], specs: dict) -> SeBlock:
    node = SeBlock(name=name, pose=list(pose), rot=list(rot))
    node.bound_kind = "component"
    node.bound = "the-screw"
    node.derived = catalog.derive("fastener", specs)
    return node


def _box2(*, params: dict | None = None) -> SeTree:
    """M4 cap head at z=0.064 pointing down: lid 60→64 mm, then the
    box's boss from 60 mm down — the stack the envelope walk mangled."""
    tree = SeTree()
    # Head bearing face at the lid's top surface, thread going −z.
    tree.blocks["bolt"] = _bought("bolt", [0, 0, 0.064], [math.pi, 0, 0], M4_CAP)
    lid = SeBlock(name="lid", pose=[0, 0, 0.060], envelope=LID)
    lid.mode = "fdm/asa"
    tree.blocks["lid"] = lid
    box = SeBlock(name="box", pose=[0, 0, 0], envelope="box:w0.06d0.06h0.06")
    box.mode = "fdm/asa"
    box.bound_kind = "cad"
    box.bound = "box-cad"
    tree.blocks["box"] = box
    box_design = build_design(parse_source(BOX_CAD))
    designs: dict[str, Any] = {"box-cad": (box_design, {})}
    tree.cad_designs = lambda slug: designs.get(slug)
    tree.connects.append(
        ConnectSpec(
            a_block="bolt",
            a_port="thread",
            b_block="box",
            b_port="boss",
            joint={
                "class": "rigid",
                "mechanism": "screw",
                "params": params or {"thread_strategy": "thread-forming"},
            },
        )
    )
    return tree


def _only(tree: SeTree) -> fasten.FastenResult:
    results = fasten.fasten(tree)
    assert len(results) == 1
    return results[0]


def _rules(res: fasten.FastenResult) -> set[str]:
    return {f.rule for f in res.findings}


class TestRealizedStack:
    def test_the_stack_is_lid_then_the_boss_not_the_box_envelope(self) -> None:
        """The gripe's measurement: lid 60→64 mm, then the box's real
        material on the axis — the boss — not a 137 mm envelope."""
        res = _only(_box2())
        assert [m.block for m in res.members] == ["lid", "box"]
        lid, box = res.members
        assert (lid.t_in, lid.t_out) == (pytest.approx(0.0), pytest.approx(0.004))
        assert (box.t_in, box.t_out) == (
            pytest.approx(0.004),
            pytest.approx(0.064),
        )
        assert lid.thickness_m == pytest.approx(0.004)
        assert box.thickness_m == pytest.approx(0.060)
        assert res.termination == "tapped"

    def test_a_member_the_axis_only_pierces_is_still_in_the_stack(self) -> None:
        """The connect names only the far end (``box.boss``); the lid the
        axis pierces on the way is a member anyway — the lid that dropped
        out of the envelope-measured stack."""
        res = _only(_box2())
        assert res.members[0].block == "lid"
        assert "fastener_target_missed" not in _rules(res)

    def test_the_far_end_is_the_deepest_run_of_the_target_block(self) -> None:
        """A block whose solid gives the axis two runs — a top wall, air,
        then the boss — contributes two members, and the thread seats in
        the deeper one, which is what the connect's far end means."""
        tree = _box2()
        # Give the box a closed top: a wall the screw clears, then air,
        # then the boss it threads into — one block, two runs.
        walled = build_design(
            parse_source(
                "component p\n"
                "plate add box:w60mmd60mmh4mm\n"
                "boss  add cyl:r4mmh52mm @0mm,0mm,4mm\n"
                "top   add box:w60mmd60mmh3mm @0mm,0mm,57mm\n"
            )
        )
        tree.cad_designs = lambda slug: {"box-cad": (walled, {})}.get(slug)
        res = _only(tree)
        assert [m.block for m in res.members] == ["lid", "box", "box"]
        # Wall at 57–60 mm world → t 0.004–0.007; boss top at 56 mm → t
        # 0.008 down to the floor.
        wall, boss = res.members[1], res.members[2]
        assert (wall.t_in, wall.t_out) == (
            pytest.approx(0.004),
            pytest.approx(0.007),
        )
        assert boss.t_in == pytest.approx(0.008)
        core = next(h for h in res.holes if h.kind == "core")
        assert core.block == "box"
        # The core seats in the boss — its surface — not the wall's.
        assert core.origin[2] == pytest.approx(0.056, abs=1e-6)

    def test_a_pre_cut_hole_keeps_the_member_in_the_stack(self) -> None:
        """A member whose axis interval is all carved void — the lid's
        clearance hole already cut in the bound cad — is pierced but
        present: it is clamped, it stays in the stack, and the stamp it
        needs is still named (whether the cut exists is printsolid's
        question)."""
        tree = _box2()
        lid_design = build_design(
            parse_source(
                "component p\n"
                "plate add box:w60mmd60mmh4mm\n"
                "hole  cut cyl:r2.5mmh6mm @0mm,0mm,-1mm\n"
            )
        )
        assert tree.cad_designs is not None
        designs: dict[str, Any] = {"box-cad": tree.cad_designs("box-cad")}
        designs["lid-cad"] = (lid_design, {})
        tree.cad_designs = lambda slug: designs.get(slug)
        tree.blocks["lid"].bound_kind = "cad"
        tree.blocks["lid"].bound = "lid-cad"
        res = _only(tree)
        assert [m.block for m in res.members] == ["lid", "box"]
        assert res.members[0].thickness_m == pytest.approx(0.004)


class TestSurfaceAnchoredStamps:
    def test_the_core_hole_starts_at_the_boss_top(self) -> None:
        """The feature that floated 4 mm in air: origin is the pierced
        member's real surface on the axis — the boss top at z=0.060 —
        with depth = engagement + the 2-pitch tip allowance."""
        res = _only(_box2())
        core = next(h for h in res.holes if h.kind == "core")
        # boss top at world z=0.060, axis −z.
        assert core.origin == pytest.approx([0.0, 0.0, 0.060], abs=1e-9)
        assert core.axis == pytest.approx([0.0, 0.0, -1.0], abs=1e-9)
        # 12 mm screw − 4 mm lid = 8 mm engaged; +2×0.7 mm pitch.
        assert core.depth_m == pytest.approx(0.008 + 2 * 0.0007)
        assert not core.through


class TestPiercedMemberClearance:
    def test_the_pierced_lid_gets_a_shank_clearance(self) -> None:
        """Every member between head and far end is stamped — the lid
        that used to get nothing at all (gr525812 item 4)."""
        res = _only(
            _box2(
                params={
                    "thread_strategy": "thread-forming",
                    "fit_class": "medium",
                }
            )
        )
        clearance = next(
            h for h in res.holes if h.kind == "clearance" and h.block == "lid"
        )
        # ISO 273 medium for M4 is 4.5 mm, plus the printed-hole
        # compensation the member's fdm/asa mode asks for.
        assert clearance.diameter_m == pytest.approx(0.0045 + 0.00025, abs=1e-4)
        assert clearance.depth_m == pytest.approx(0.004)
        assert clearance.through
        assert clearance.origin == pytest.approx([0.0, 0.0, 0.064], abs=1e-9)

    def test_the_near_member_gets_the_counterbore_not_the_seat(self) -> None:
        """gr525812's destructive stamp: with the lid in the measured
        stack the counterbore lands on it, and nothing is withheld."""
        res = _only(
            _box2(params={"thread_strategy": "thread-forming", "counterbore": True})
        )
        bore = next(h for h in res.holes if h.kind == "counterbore")
        assert bore.block == "lid"
        assert bore.origin == pytest.approx([0.0, 0.0, 0.064], abs=1e-9)
        assert bore.depth_m == pytest.approx(0.004)  # the M4 head height
        assert "head_in_thread_seat" not in _rules(res)
        # …but a 4 mm bore in a 4 mm lid leaves the head no floor — said,
        # not hidden (the thin-plate counterbore rule).
        assert "head_feature_no_floor" in _rules(res)

    def test_a_single_member_stack_still_withholds_the_bore(self) -> None:
        """The guard keeps its real case: remove the lid and the box is
        the only member — bore and thread seat in one."""
        tree = _box2(params={"thread_strategy": "thread-forming", "counterbore": True})
        del tree.blocks["lid"]
        res = _only(tree)
        assert [m.block for m in res.members] == ["box"]
        assert not [h for h in res.holes if h.kind == "counterbore"]
        assert "head_in_thread_seat" in _rules(res)

    def test_a_countersunk_single_member_still_gets_its_cone(self) -> None:
        """The seat-bound cone is the ordinary countersunk-tapped hole —
        required, so it stamps where the optional bore would not."""
        sunk = dict(M4_CAP, head_form="countersunk", head_diameter=0.00896)
        sunk.pop("head_height")
        tree = _box2(params={"thread_strategy": "thread-forming"})
        del tree.blocks["lid"]
        tree.blocks["bolt"] = _bought("bolt", [0, 0, 0.060], [math.pi, 0, 0], sunk)
        res = _only(tree)
        sink = next(h for h in res.holes if h.kind == "countersink")
        assert sink.block == "box"
        assert "head_in_thread_seat" not in _rules(res)
