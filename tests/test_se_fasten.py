"""Rung 3: what a `screw` joint does to the parts it joins
(``se-off-the-shelf-fabrication.md`` engine 2).

Pure arithmetic and geometry over a hand-built :class:`SeTree` — no store,
no DB. That is not a shortcut: :mod:`precis_se.fasten` reads the tree and
its catalog-derived facets and nothing else, so a test that spun up
Postgres would be testing rung 2b's wiring a second time
(``test_se_catalog_binding.py`` owns that seam).

The fixtures pose a real stack in metres: an M6×30 socket cap at the
origin driving ``+z``, its head face at ``z=0``, and plates stacked above
it. Every expected number below is checkable by hand against those poses,
which is the point — a stack-up test whose expected values came out of the
implementation would agree with any bug it has.
"""

from __future__ import annotations

import math
from typing import Any

import pytest

from precis import fit_classes as core_fit
from precis_se import catalog, fasten
from precis_se.handler import _render_fasten
from precis_se.ops import ConnectSpec, SeBlock, SeTree

#: M6×30 ISO 4762, in **metres** — the shape `persist._derive_one` hands
#: the catalog after converting the component store's millimetres.
M6X30 = {
    "thread_size": "M6",
    "thread_pitch": 0.001,
    "outer_diameter": 0.006,
    "head_diameter": 0.010,
    "head_height": 0.006,
    "length": 0.030,
    "drive_size": 0.005,
}
#: ISO 4032 M6 hex nut, metres.
M6_NUT = {
    "thread_size": "M6",
    "thread_pitch": 0.001,
    "inner_diameter": 0.006,
    "across_flats": 0.010,
    "height": 0.0052,
}

PLATE_6MM = "box:w0.05d0.05h0.006"


def _bought(name: str, pose: list[float], specs: dict, *, slug: str) -> SeBlock:
    node = SeBlock(name=name, pose=list(pose))
    node.bound_kind = "component"
    node.bound = slug
    node.derived = catalog.derive("fastener", specs)
    return node


def _designed(name: str, pose: list[float], envelope: str = PLATE_6MM) -> SeBlock:
    return SeBlock(name=name, pose=list(pose), envelope=envelope)


def _stack(*, nut: bool, joint: dict | None = None, length: float = 0.030) -> SeTree:
    """Head face at z=0; 6 mm plate 0.006→0.012, 6 mm plate 0.012→0.018,
    optional nut 0.018→0.0232."""
    specs = dict(M6X30, length=length)
    tree = SeTree()
    tree.blocks["bolt"] = _bought("bolt", [0, 0, 0], specs, slug="iso-4762-m6x30")
    tree.blocks["plate_a"] = _designed("plate_a", [0, 0, 0.006])
    tree.blocks["plate_b"] = _designed("plate_b", [0, 0, 0.012])
    if nut:
        tree.blocks["nut"] = _bought("nut", [0, 0, 0.018], M6_NUT, slug="iso-4032-m6")
    tree.connects.append(
        ConnectSpec(
            a_block="bolt",
            a_port="thread",
            b_block="plate_a",
            b_port="hole",
            joint=joint
            if joint is not None
            else {"class": "rigid", "mechanism": "screw"},
        )
    )
    return tree


def _only(tree: SeTree) -> fasten.FastenResult:
    results = fasten.fasten(tree)
    assert len(results) == 1
    return results[0]


def _rules(res: fasten.FastenResult) -> set[str]:
    return {f.rule for f in res.findings}


class TestFitClasses:
    def test_the_house_rule_is_the_default_and_is_d_plus_02(self) -> None:
        fit = core_fit.clearance_hole("M6")
        assert fit is not None
        assert fit.fit_class == core_fit.default_class() == "house"
        assert fit.hole_mm == pytest.approx(6.2)
        assert fit.hole_m == pytest.approx(0.0062)

    @pytest.mark.parametrize(
        ("fit_class", "expected"),
        [("fine", 6.4), ("medium", 6.6), ("coarse", 7.0)],
    )
    def test_the_iso_273_columns(self, fit_class: str, expected: float) -> None:
        fit = core_fit.clearance_hole("M6", fit_class)
        assert fit is not None and fit.hole_mm == pytest.approx(expected)

    def test_the_house_rule_is_tighter_than_iso_fine(self) -> None:
        """The fact the pattern finding exists for: 0.1 mm of radial slack
        against ISO fine's 0.2 mm."""
        house = core_fit.clearance_hole("M6", "house")
        fine = core_fit.clearance_hole("M6", "fine")
        assert house is not None and fine is not None
        assert house.hole_mm < fine.hole_mm
        assert house.radial_slack_mm == pytest.approx(0.1)
        assert fine.radial_slack_mm == pytest.approx(0.2)

    def test_size_is_matched_case_insensitively(self) -> None:
        assert core_fit.clearance_hole("m6") == core_fit.clearance_hole(" M6 ")

    def test_an_unknown_size_or_class_is_none_not_a_guess(self) -> None:
        # M14 is a real thread the ISO 273 table here does not list; a
        # rule class cannot rescue it either, because the nominal
        # diameter it would apply to lives in the same size table.
        assert core_fit.clearance_hole("M14") is None
        assert core_fit.clearance_hole("M14", "house") is None
        assert core_fit.clearance_hole("M6", "snug") is None

    def test_every_size_resolves_in_every_class(self) -> None:
        for size in core_fit.sizes():
            for cls in core_fit.classes():
                fit = core_fit.clearance_hole(size, cls)
                assert fit is not None, f"{size}/{cls}"
                assert fit.hole_mm > fit.nominal_mm, f"{size}/{cls} does not clear"

    def test_the_fine_column_agrees_with_the_iso_7089_washer_bore(self) -> None:
        """The consistency check `fit_classes.json`'s note claims, run as
        a test: two independently transcribed standards tables overlap at
        every shared size, so a typo in either shows up here."""
        from precis import component_series

        washer = component_series.find_series("iso-7089")
        assert washer is not None
        checked = 0
        for size in washer.sizes:
            fit = core_fit.clearance_hole(str(size.specs["thread_size"]), "fine")
            if fit is None:
                continue
            assert fit.hole_mm == pytest.approx(float(size.specs["inner_diameter"])), (
                f"ISO 273 fine and the ISO 7089 bore disagree at {size.key}"
            )
            checked += 1
        assert checked >= 8


class TestAxisWalk:
    def test_the_stack_is_measured_from_the_poses(self) -> None:
        res = _only(_stack(nut=True))
        assert res.why_not is None
        assert [m.block for m in res.members] == ["plate_a", "plate_b", "nut"]
        assert res.members[0].t_in == pytest.approx(0.006)  # under the head
        assert res.stack_m == pytest.approx(0.006 + 0.006 + 0.0052)
        assert res.grip_m == pytest.approx(0.012)  # clamped, nut excluded
        assert res.termination == "nut"

    def test_material_behind_the_head_is_not_a_member(self) -> None:
        """A screw clamps what is in front of it. A plate sitting behind
        the head is in the design, on the same line, and irrelevant."""
        tree = _stack(nut=True)
        tree.blocks["behind"] = _designed("behind", [0, 0, -0.006])
        res = _only(tree)
        assert "behind" not in [m.block for m in res.members]

    def test_a_rotated_screw_walks_the_other_way(self) -> None:
        """The axis is read off the fastener's own pose, so flipping the
        block flips the stack — nothing is declared."""
        tree = _stack(nut=False)
        tree.blocks["bolt"].rot = [180.0, 0.0, 0.0]
        tree.blocks["plate_a"].pose = [0, 0, -0.012]
        tree.blocks["plate_b"].pose = [0, 0, -0.018]
        res = _only(tree)
        assert [m.block for m in res.members] == ["plate_a", "plate_b"]
        assert res.stack_m == pytest.approx(0.012)

    def test_a_screw_pointing_at_nothing_says_so(self) -> None:
        tree = _stack(nut=False)
        del tree.blocks["plate_a"]
        del tree.blocks["plate_b"]
        res = _only(tree)
        assert res.members == []
        assert "passes through nothing" in (res.why_not or "")

    def test_a_block_off_the_axis_is_not_a_member(self) -> None:
        tree = _stack(nut=False)
        tree.blocks["plate_b"].pose = [0.4, 0, 0.012]
        res = _only(tree)
        assert [m.block for m in res.members] == ["plate_a"]


class TestLengthAndEngagement:
    def test_a_long_enough_screw_through_a_nut_has_no_finding(self) -> None:
        res = _only(_stack(nut=True))
        assert res.required_length_m == pytest.approx(0.0172 + 0.002)
        assert res.length_m == pytest.approx(0.030)
        assert "screw_too_short" not in _rules(res)

    def test_a_short_screw_is_a_finding_that_names_the_shortfall(self) -> None:
        res = _only(_stack(nut=True, length=0.016))
        assert "screw_too_short" in _rules(res)
        detail = next(f.detail for f in res.findings if f.rule == "screw_too_short")
        assert "3.2 mm short" in detail  # 19.2 needed − 16.0 available

    def test_a_nutless_stack_taps_the_last_member(self) -> None:
        res = _only(_stack(nut=False))
        assert res.termination == "tapped"
        # grip is everything above the tapped member; engagement is 1×D
        assert res.grip_m == pytest.approx(0.006)
        assert res.required_length_m == pytest.approx(0.006 + 0.006)

    def test_a_tapped_member_thinner_than_one_diameter_is_a_finding(self) -> None:
        tree = _stack(nut=False)
        tree.blocks["plate_b"].envelope = "box:w0.05d0.05h0.003"
        res = _only(tree)
        assert "thread_engagement" in _rules(res)
        detail = next(f.detail for f in res.findings if f.rule == "thread_engagement")
        assert "3.0 mm thick" in detail and "6.0 mm" in detail


class TestThread:
    def test_the_thread_is_a_lead_with_limits(self) -> None:
        """Reto's framing: a thread is distance per turn, bounded. An M6
        coarse advances 1 mm per revolution and has 5.2 mm of nut to
        traverse, so it seats in 5.2 turns."""
        res = _only(_stack(nut=True))
        assert res.thread is not None
        assert res.thread.lead_m == pytest.approx(0.001)
        assert res.thread.starts == 1
        assert res.thread.engagement_m == pytest.approx(0.0052)
        assert res.thread.turns == pytest.approx(5.2)

    def test_engagement_in_a_tapped_hole_is_bounded_by_the_screw(self) -> None:
        """A 16 mm screw through 6 mm of plate has 10 mm of thread left,
        but the tapped plate is only 6 mm deep — the limit is the shorter
        of the two, and it is the plate here."""
        res = _only(_stack(nut=False, length=0.016))
        assert res.thread is not None
        assert res.thread.engagement_m == pytest.approx(0.006)

    def test_engagement_is_bounded_by_the_thread_when_the_screw_is_short(
        self,
    ) -> None:
        res = _only(_stack(nut=False, length=0.009))
        assert res.thread is not None
        assert res.thread.engagement_m == pytest.approx(0.003)

    def test_a_declared_lead_that_disagrees_with_the_pitch_is_a_finding(self) -> None:
        res = _only(
            _stack(
                nut=True,
                joint={
                    "class": "screw",
                    "axis": [0, 0, 1],
                    "mechanism": "screw",
                    "params": {"lead": 0.002},
                },
            )
        )
        assert "lead_disagreement" in _rules(res)
        assert res.declared_lead_m == pytest.approx(0.002)

    def test_a_declared_lead_that_matches_is_silent(self) -> None:
        res = _only(
            _stack(
                nut=True,
                joint={
                    "class": "screw",
                    "axis": [0, 0, 1],
                    "params": {"lead": 0.001},
                },
            )
        )
        assert "lead_disagreement" not in _rules(res)

    def test_a_screw_class_joint_is_in_scope_without_the_mechanism(self) -> None:
        """The two registries meet here: `screw` the kinematic class (a
        helical pair) and `screw` the mechanism (threaded fastening).
        Declaring either brings a joint into this pass."""
        res = _only(_stack(nut=True, joint={"class": "screw", "axis": [0, 0, 1]}))
        assert res.klass == "screw" and res.mechanism is None
        assert res.thread is not None


class TestStampedHoles:
    def test_designed_members_get_clearance_holes_at_the_house_fit(self) -> None:
        res = _only(_stack(nut=True))
        assert [h.block for h in res.holes] == ["plate_a", "plate_b"]
        assert all(h.kind == "clearance" for h in res.holes)
        assert res.holes[0].diameter_m == pytest.approx(0.0062)
        assert res.holes[0].depth_m == pytest.approx(0.006)

    def test_a_bought_member_is_never_drilled(self) -> None:
        """You do not drill a nut. It arrives threaded."""
        res = _only(_stack(nut=True))
        assert "nut" not in [h.block for h in res.holes]

    def test_a_hole_is_named_after_the_connect_that_made_it(self) -> None:
        res = _only(_stack(nut=True))
        assert res.holes[0].name.startswith(res.subject)
        assert res.holes[0].name.endswith("plate_a.clearance")

    def test_a_hole_sits_at_the_member_it_enters(self) -> None:
        res = _only(_stack(nut=True))
        assert res.holes[1].origin == pytest.approx([0.0, 0.0, 0.012])
        assert res.holes[1].axis == pytest.approx([0.0, 0.0, 1.0])

    def test_the_terminal_member_of_a_nutless_stack_is_tapped(self) -> None:
        res = _only(_stack(nut=False))
        kinds = {h.block: h.kind for h in res.holes}
        assert kinds == {"plate_a": "clearance", "plate_b": "tapped"}
        tapped = next(h for h in res.holes if h.kind == "tapped")
        assert tapped.diameter_m == pytest.approx(0.005)  # d − P, M6 coarse

    def test_a_declared_fit_class_overrides_the_house_default(self) -> None:
        res = _only(
            _stack(
                nut=True,
                joint={
                    "class": "rigid",
                    "mechanism": "screw",
                    "params": {"fit_class": "coarse"},
                },
            )
        )
        assert res.fit is not None and res.fit.fit_class == "coarse"
        assert res.holes[0].diameter_m == pytest.approx(0.007)

    def test_an_unresolvable_fit_stamps_nothing_and_says_why(self) -> None:
        tree = _stack(nut=True)
        specs = dict(M6X30)
        del specs["thread_size"]
        tree.blocks["bolt"] = _bought("bolt", [0, 0, 0], specs, slug="mystery")
        res = _only(tree)
        assert res.holes == []
        assert "fit_unresolved" in _rules(res)
        assert "no thread_size" in next(
            f.detail for f in res.findings if f.rule == "fit_unresolved"
        )

    def test_an_unlisted_thread_size_stamps_nothing(self) -> None:
        tree = _stack(nut=True)
        tree.blocks["bolt"] = _bought(
            "bolt", [0, 0, 0], dict(M6X30, thread_size="M14"), slug="m14"
        )
        res = _only(tree)
        assert res.holes == []
        assert "fit_unresolved" in _rules(res)


class TestDeclaredAxis:
    def test_a_declared_axis_that_disagrees_with_the_screw_is_a_finding(self) -> None:
        res = _only(
            _stack(
                nut=True,
                joint={"class": "screw", "axis": [1, 0, 0], "mechanism": "screw"},
            )
        )
        assert "fastener_axis" in _rules(res)

    def test_the_opposite_sign_is_the_same_line_not_a_disagreement(self) -> None:
        """A joint axis is a line. ``-z`` and ``+z`` name the same one, and
        flagging that would train designers to ignore the finding."""
        res = _only(
            _stack(
                nut=True,
                joint={"class": "screw", "axis": [0, 0, -1], "mechanism": "screw"},
            )
        )
        assert "fastener_axis" not in _rules(res)

    def test_a_matching_axis_is_silent(self) -> None:
        res = _only(
            _stack(
                nut=True,
                joint={"class": "screw", "axis": [0, 0, 1], "mechanism": "screw"},
            )
        )
        assert "fastener_axis" not in _rules(res)


class TestPatternTolerance:
    def _two_bolt_tree(self, fit_class: str | None = None) -> SeTree:
        tree = _stack(nut=False)
        tree.blocks["bolt2"] = _bought(
            "bolt2", [0.02, 0, 0], M6X30, slug="iso-4762-m6x30"
        )
        joint: dict[str, Any] = {"class": "rigid", "mechanism": "screw"}
        if fit_class:
            joint["params"] = {"fit_class": fit_class}
            tree.connects[0].joint = dict(joint)
        tree.connects.append(
            ConnectSpec(
                a_block="bolt2",
                a_port="thread",
                b_block="plate_a",
                b_port="hole2",
                joint=dict(joint),
            )
        )
        return tree

    def test_two_holes_in_one_member_warn_about_position_error(self) -> None:
        results = fasten.fasten(self._two_bolt_tree())
        rules = {f.rule for r in results for f in r.findings}
        assert "hole_pattern_tolerance" in rules
        detail = next(
            f.detail
            for r in results
            for f in r.findings
            if f.rule == "hole_pattern_tolerance"
        )
        assert "0.10 mm of radial slack" in detail

    def test_the_warning_is_said_once_per_member_not_once_per_hole(self) -> None:
        results = fasten.fasten(self._two_bolt_tree())
        hits = [
            f for r in results for f in r.findings if f.rule == "hole_pattern_tolerance"
        ]
        # Two bolts stamp four holes, but only one finding: plate_a takes
        # both clearance holes, and plate_b's two are TAPPED — a tapped
        # hole has no slack to spend, which is exactly why the whole
        # position-error budget sits in plate_a and why saying it once
        # there is the useful message.
        assert len(hits) == 1
        assert hits[0].subject == "plate_a"

    def test_a_single_screw_never_warns_about_a_pattern(self) -> None:
        res = _only(_stack(nut=False))
        assert "hole_pattern_tolerance" not in _rules(res)

    def test_a_looser_fit_still_warns_but_with_its_own_slack(self) -> None:
        results = fasten.fasten(self._two_bolt_tree("coarse"))
        detail = next(
            f.detail
            for r in results
            for f in r.findings
            if f.rule == "hole_pattern_tolerance"
        )
        assert "0.50 mm of radial slack" in detail


class TestTotality:
    def test_a_connect_with_no_fastener_block_reports_the_gap(self) -> None:
        tree = _stack(nut=True)
        tree.connects[0] = ConnectSpec(
            a_block="plate_a",
            a_port="face",
            b_block="plate_b",
            b_port="face",
            joint={"class": "rigid", "mechanism": "screw"},
        )
        res = _only(tree)
        assert res.fastener is None
        assert "bound to a screw-form" in (res.why_not or "")

    def test_a_malformed_stored_joint_is_skipped_not_raised(self) -> None:
        """It is already ``drc``'s ``malformed_joint`` finding; reporting
        it twice, from a module that cannot say anything useful about it,
        is noise."""
        tree = _stack(nut=True)
        tree.connects[0].joint = {"class": "nonsense", "mechanism": "screw"}
        assert fasten.fasten(tree) == []

    def test_joints_that_are_not_screws_are_ignored(self) -> None:
        tree = _stack(nut=True)
        tree.connects[0].joint = {"class": "revolute", "axis": [0, 0, 1]}
        assert fasten.fasten(tree) == []

    def test_a_block_with_no_envelope_does_not_break_the_walk(self) -> None:
        tree = _stack(nut=True)
        tree.blocks["sketch"] = SeBlock(name="sketch")
        res = _only(tree)
        assert [m.block for m in res.members] == ["plate_a", "plate_b", "nut"]

    def test_findings_flattens_every_result(self) -> None:
        tree = _stack(nut=True, length=0.010)
        assert {f.rule for f in fasten.findings(fasten.fasten(tree))} == {
            "screw_too_short"
        }

    def test_nothing_is_written_back_onto_the_tree(self) -> None:
        """Stamped features are derived: the pass must leave the blocks it
        read exactly as it found them."""
        tree = _stack(nut=True)
        before = {n: (b.envelope, dict(b.ports)) for n, b in tree.blocks.items()}
        fasten.fasten(tree)
        after = {n: (b.envelope, dict(b.ports)) for n, b in tree.blocks.items()}
        assert before == after


class TestRender:
    def test_an_empty_design_says_what_the_view_covers(self) -> None:
        body = _render_fasten(SeTree())
        assert "no screw joints" in body
        assert "mechanism" in body and "kinematic class" in body

    def test_the_report_carries_the_numbers_a_designer_acts_on(self) -> None:
        body = _render_fasten(_stack(nut=True))
        assert "grip 12.00 mm" in body
        assert "1.00 mm of travel per turn" in body
        assert "5.2 turns" in body
        assert "6.20" in body  # the M6 house clearance hole
        assert "plate_a" in body and "nut" in body

    def test_a_gap_is_rendered_not_hidden(self) -> None:
        tree = _stack(nut=False)
        del tree.blocks["plate_a"]
        del tree.blocks["plate_b"]
        body = _render_fasten(tree)
        assert "passes through nothing" in body

    def test_findings_reach_the_view(self) -> None:
        body = _render_fasten(_stack(nut=True, length=0.010))
        assert "screw_too_short" in body


class TestDrcIntegration:
    def test_a_short_screw_surfaces_in_the_drc_report(self) -> None:
        from precis_se import drc as se_drc

        report = se_drc.drc(_stack(nut=True, length=0.010))
        assert "screw_too_short" in {f.rule for f in report.findings}


class TestUnitsAreNotFudged:
    def test_every_length_the_pass_reports_is_metres(self) -> None:
        """se is float64 metres everywhere; the millimetre boundary is the
        `component` store on the way in and the view on the way out. A
        stack of two 6 mm plates measuring 12 (not 0.012) is the bug this
        guards."""
        res = _only(_stack(nut=True))
        assert res.stack_m is not None and res.stack_m < 0.1
        assert res.length_m is not None and res.length_m < 0.1
        assert all(h.diameter_m < 0.05 for h in res.holes)
        assert res.thread is not None and math.isclose(res.thread.lead_m, 0.001)
