"""Rung 3c: fastening a **printed** part (``se-off-the-shelf-fabrication``
engine 2b) — thread strategies, head-form features, the drive policy and
the printed-hole compensation.

Same posture as `test_se_fasten.py`, whose fixtures this borrows: a hand-
built tree, real poses in metres, no store. The stack is one M4 screw
driving ``+z`` through a 6 mm plate into a 10 mm block, so every expected
number below is checkable by hand.

What is deliberately NOT asserted: that 0.8 × D is the right core-hole
factor, or that +0.2 mm is the right printed-hole compensation. Those are
house judgements living in data; pinning them here would turn a number
someone should be free to calibrate into a test someone has to fight.
"""

from __future__ import annotations

import pytest

from precis_se import catalog, fasten
from precis_se.ops import ConnectSpec, SeBlock, SeTree

#: ISO 4762 M4×20 socket cap, metres — the shape `persist._derive_one`
#: hands the catalog after converting the component store's millimetres.
M4_CAP = {
    "thread_size": "M4",
    "thread_pitch": 0.0007,
    "outer_diameter": 0.004,
    "head_diameter": 0.007,
    "head_height": 0.004,
    "length": 0.020,
    "drive_size": 0.003,
    "drive_type": "socket",
    "head_form": "cap",
    "point_type": "machine",
}
#: ISO 10642 M4×20 countersunk: no head_height (the cone is derived), and
#: the length covers the head.
M4_CSK = {
    **M4_CAP,
    "head_diameter": 0.00896,
    "head_form": "countersunk",
    "head_angle": 90.0,
}
M4_CSK.pop("head_height")

#: ISO 14585 ST4.2×16 hexalobular pan tapping screw — the pointy one.
ST42_TAP = {
    "thread_size": "ST4.2",
    "thread_pitch": 0.0014,
    "outer_diameter": 0.00422,
    "head_diameter": 0.008,
    "head_height": 0.0031,
    "length": 0.016,
    "drive_size": 0.00395,
    "drive_code": "T20",
    "drive_type": "torx",
    "head_form": "pan",
    "point_type": "tapping-c",
}

PLATE_6MM = "box:w0.05d0.05h0.006"
BLOCK_10MM = "box:w0.05d0.05h0.010"


def _bought(name: str, pose: list[float], specs: dict) -> SeBlock:
    node = SeBlock(name=name, pose=list(pose))
    node.bound_kind = "component"
    node.bound = "the-screw"
    node.derived = catalog.derive("fastener", specs)
    return node


def _stack(
    *,
    screw: dict,
    mode: str | None,
    params: dict | None = None,
) -> SeTree:
    """Head face at z=0; a 6 mm plate 0.006→0.012 and a 10 mm block
    0.012→0.022 that the thread lands in."""
    tree = SeTree()
    tree.blocks["screw"] = _bought("screw", [0, 0, 0], screw)
    tree.blocks["plate"] = SeBlock(name="plate", pose=[0, 0, 0.006], envelope=PLATE_6MM)
    block = SeBlock(name="block", pose=[0, 0, 0.012], envelope=BLOCK_10MM)
    block.mode = mode
    tree.blocks["block"] = block
    tree.connects.append(
        ConnectSpec(
            a_block="screw",
            a_port="thread",
            b_block="block",
            b_port="hole",
            joint={
                "class": "rigid",
                "mechanism": "screw",
                "params": params or {},
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


def _holes(res: fasten.FastenResult, kind: str) -> list[fasten.Hole]:
    return [h for h in res.holes if h.kind == kind]


class TestStrategyResolution:
    def test_a_printed_member_with_no_strategy_gets_no_hole(self) -> None:
        """The defect rung 3c exists to fix: a cut thread stamped into a
        printed boss prints, holds for two assemblies, then strips."""
        res = _only(_stack(screw=M4_CAP, mode="fdm/pla"))
        assert "thread_strategy_undeclared" in _rules(res)
        assert res.thread_strategy is None
        assert not _holes(res, "tapped")
        assert not _holes(res, "core")
        # …and the clearance hole through the plate is still stamped: the
        # refusal is about the far end, not about the whole joint.
        assert [h.block for h in _holes(res, "clearance")] == ["plate"]

    def test_the_finding_names_the_four_alternatives(self) -> None:
        res = _only(_stack(screw=M4_CAP, mode="fdm/pla"))
        detail = next(
            f.detail for f in res.findings if f.rule == "thread_strategy_undeclared"
        )
        for option in ("nut", "nut-trap", "insert", "thread-forming"):
            assert option in detail

    def test_metal_still_taps_without_being_asked(self) -> None:
        res = _only(_stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium"))
        assert res.thread_strategy == "tapped"
        assert res.material_class == "metal"
        tapped = _holes(res, "tapped")
        assert len(tapped) == 1
        assert tapped[0].diameter_m == pytest.approx(0.0033, abs=1e-5)  # d − P

    def test_a_member_with_no_mode_keeps_the_old_behaviour_and_says_so(self) -> None:
        """Refusing here would break every design that predates modes, so
        the cut thread stands — visibly as an assumption."""
        res = _only(_stack(screw=M4_CAP, mode=None))
        assert res.thread_strategy == "tapped"
        assert "member_material_undeclared" in _rules(res)
        assert len(_holes(res, "tapped")) == 1

    def test_declaring_tapped_on_plastic_is_allowed_but_noted(self) -> None:
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/pla", params={"thread_strategy": "tapped"})
        )
        assert "tapped_plastic" in _rules(res)
        assert len(_holes(res, "tapped")) == 1  # declared, so it is stamped

    def test_a_nut_in_the_stack_settles_it_without_a_param(self) -> None:
        tree = _stack(screw=M4_CAP, mode="fdm/pla")
        nut = _bought(
            "nut",
            [0, 0, 0.022],
            {
                "thread_size": "M4",
                "thread_pitch": 0.0007,
                "inner_diameter": 0.004,
                "across_flats": 0.007,
                "height": 0.0032,
            },
        )
        tree.blocks["nut"] = nut
        res = _only(tree)
        assert res.thread_strategy == "nut"
        assert "thread_strategy_undeclared" not in _rules(res)


class TestPrintedStrategies:
    def test_thread_forming_stamps_a_core_hole_between_minor_and_major(self) -> None:
        res = _only(
            _stack(
                screw=ST42_TAP,
                mode="fdm/petg",
                params={"thread_strategy": "thread-forming"},
            )
        )
        core = _holes(res, "core")
        assert len(core) == 1
        # Between the thread's minor (2.84 mm) and its major (4.22 mm),
        # plus whatever the printer compensation adds — the useful
        # invariant, not the exact figure.
        assert 0.00284 < core[0].diameter_m < 0.00422
        assert core[0].source and "minor" in core[0].source

    def test_a_formed_thread_asks_for_a_boss_in_prose(self) -> None:
        res = _only(
            _stack(
                screw=ST42_TAP,
                mode="fdm/petg",
                params={"thread_strategy": "thread-forming"},
            )
        )
        assert "boss_required" in _rules(res)

    def test_an_insert_stamps_a_pocket_and_demands_the_insert(self) -> None:
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/asa", params={"thread_strategy": "insert"})
        )
        pockets = _holes(res, "insert-pocket")
        assert len(pockets) == 1
        assert pockets[0].chamfer_m and pockets[0].chamfer_m > 0
        assert "insert_bom" in _rules(res)

    def test_a_nut_trap_stamps_a_hex_pocket_with_its_across_flats(self) -> None:
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/asa", params={"thread_strategy": "nut-trap"})
        )
        pockets = _holes(res, "nut-pocket")
        assert len(pockets) == 1
        assert pockets[0].across_flats_m is not None
        assert pockets[0].across_flats_m > 0.007  # the M4 nut plus its fit
        assert "nut_trap_bom" in _rules(res)

    def test_the_screw_is_drilled_through_to_reach_the_captive_nut(self) -> None:
        """A pocket with no hole to it is a nut the screw never meets."""
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/asa", params={"thread_strategy": "nut-trap"})
        )
        through = [h for h in _holes(res, "clearance") if h.block == "block"]
        assert len(through) == 1
        assert through[0].depth_m == pytest.approx(0.010)  # the whole member

    def test_the_pocket_sits_at_the_far_face_where_the_nut_loads(self) -> None:
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/asa", params={"thread_strategy": "nut-trap"})
        )
        pocket = _holes(res, "nut-pocket")[0]
        # The block spans z 0.012→0.022; the pocket starts one pocket-depth
        # up from the far face, not at the face the screw enters.
        assert pocket.origin[2] == pytest.approx(0.022 - pocket.depth_m, abs=1e-4)

    def test_a_pocket_deeper_than_its_member_is_a_finding(self) -> None:
        tree = _stack(
            screw=M4_CAP, mode="fdm/asa", params={"thread_strategy": "insert"}
        )
        tree.blocks["block"].envelope = "box:w0.05d0.05h0.004"  # 4 mm, too thin
        res = _only(tree)
        assert "pocket_too_deep" in _rules(res)

    def test_plastic_demands_more_engagement_than_steel(self) -> None:
        """The same 5 mm member is fine for a cut thread in aluminium and
        thin for one in plastic — the material rule, not the geometry."""
        thin = "box:w0.05d0.05h0.005"
        metal = _stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium")
        metal.blocks["block"].envelope = thin
        assert "thread_engagement" not in _rules(_only(metal))
        plastic = _stack(
            screw=M4_CAP, mode="fdm/pla", params={"thread_strategy": "tapped"}
        )
        plastic.blocks["block"].envelope = thin
        assert "thread_engagement" in _rules(_only(plastic))


class TestHeadFeatures:
    def test_a_countersunk_head_cuts_a_cone_in_the_near_member(self) -> None:
        res = _only(_stack(screw=M4_CSK, mode="cnc-2.5ax/aluminium"))
        sinks = _holes(res, "countersink")
        assert len(sinks) == 1
        assert sinks[0].block == "plate"  # the first designed member
        assert sinks[0].diameter_m == pytest.approx(0.00896, abs=1e-4)
        # 90° cone: the depth follows from the diameters.
        assert sinks[0].depth_m == pytest.approx((0.00896 - 0.004) / 2, abs=1e-4)

    def test_a_cap_head_is_left_proud_and_the_choice_is_reported(self) -> None:
        """Burying a cap head is a decision, not a requirement — and this
        pass does not make decisions. A 4 mm bore in a 6 mm plate is a
        big thing to do to someone's part unasked."""
        res = _only(_stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium"))
        assert not _holes(res, "counterbore")
        assert "head_stands_proud" in _rules(res)

    def test_asking_for_a_counterbore_stamps_one(self) -> None:
        res = _only(
            _stack(
                screw=M4_CAP,
                mode="cnc-2.5ax/aluminium",
                params={"counterbore": True},
            )
        )
        bores = _holes(res, "counterbore")
        assert len(bores) == 1
        assert bores[0].diameter_m > 0.007  # head Ø plus the fit's slack
        assert bores[0].depth_m == pytest.approx(0.004)
        assert "head_stands_proud" not in _rules(res)

    def test_a_countersunk_head_needs_no_asking(self) -> None:
        """The cone is physics: the screw does not seat without it."""
        res = _only(_stack(screw=M4_CSK, mode="cnc-2.5ax/aluminium"))
        assert len(_holes(res, "countersink")) == 1
        assert "head_stands_proud" not in _rules(res)

    def test_a_head_form_nobody_declared_stamps_nothing(self) -> None:
        specs = dict(M4_CAP)
        specs.pop("head_form")
        res = _only(_stack(screw=specs, mode="cnc-2.5ax/aluminium"))
        assert not _holes(res, "counterbore")
        assert not _holes(res, "countersink")

    def test_the_countersunk_length_includes_its_head(self) -> None:
        """A countersunk screw is measured over the whole screw. Adding
        the head would lengthen the part by its own head, and the grip
        check would believe it."""
        sunk = _only(_stack(screw=M4_CSK, mode="cnc-2.5ax/aluminium"))
        proud = _only(_stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium"))
        assert sunk.members[-1].t_out <= proud.members[-1].t_out + 1e-9


class TestDrivePolicy:
    def test_a_cross_recess_screw_is_a_finding(self) -> None:
        specs = dict(M4_CAP, drive_type="phillips")
        res = _only(_stack(screw=specs, mode="cnc-2.5ax/aluminium"))
        assert "drive_not_preferred" in _rules(res)

    def test_hex_socket_and_torx_are_silent(self) -> None:
        for drive in ("socket", "torx"):
            res = _only(
                _stack(screw=dict(M4_CAP, drive_type=drive), mode="cnc-2.5ax/aluminium")
            )
            assert "drive_not_preferred" not in _rules(res)

    def test_a_row_with_no_drive_is_not_accused_of_having_the_wrong_one(self) -> None:
        specs = dict(M4_CAP)
        specs.pop("drive_type")
        res = _only(_stack(screw=specs, mode="cnc-2.5ax/aluminium"))
        assert "drive_not_preferred" not in _rules(res)


class TestPrintedHoleCompensation:
    def test_a_hole_in_a_printed_member_is_modelled_oversize(self) -> None:
        printed = _only(
            _stack(screw=M4_CAP, mode="fdm/pla", params={"thread_strategy": "tapped"})
        )
        milled = _only(_stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium"))
        printed_hole = _holes(printed, "tapped")[0]
        milled_hole = _holes(milled, "tapped")[0]
        assert printed_hole.diameter_m > milled_hole.diameter_m
        assert "compensation" in (printed_hole.source or "")

    def test_the_compensation_says_how_confident_it_is(self) -> None:
        res = _only(
            _stack(screw=M4_CAP, mode="fdm/pla", params={"thread_strategy": "tapped"})
        )
        assert "confidence" in (_holes(res, "tapped")[0].source or "")

    def test_an_uncharacterized_process_adds_nothing(self) -> None:
        res = _only(_stack(screw=M4_CAP, mode="cnc-2.5ax/aluminium"))
        assert _holes(res, "tapped")[0].diameter_m == pytest.approx(0.0033, abs=1e-5)


class TestCatalogFormDispatch:
    """A row that *declares* its form beats a guess from its spec shape —
    which is what lets a countersunk screw (no head_height: the cone depth
    is derived) and an insert (no head at all) resolve correctly."""

    def test_a_countersunk_row_is_a_screw_despite_having_no_head_height(
        self,
    ) -> None:
        assert "head_height" not in M4_CSK
        assert catalog.fastener_form(M4_CSK) == "screw"
        derived = catalog.derive("fastener", M4_CSK)
        assert derived.envelope is not None
        # Length covers the head on a countersunk screw, so the envelope
        # is the length itself; the same screw with a proud head is that
        # much longer overall.
        assert "h0.02" in derived.envelope
        proud = catalog.derive("fastener", M4_CAP)
        assert proud.envelope is not None
        assert "h0.024" in proud.envelope  # 20 mm under a 4 mm head

    def test_the_sink_depth_is_derived_from_the_two_diameters(self) -> None:
        depth = catalog.head_height(M4_CSK)
        assert depth == pytest.approx((0.00896 - 0.004) / 2)

    def test_an_insert_is_its_own_form_with_a_thread_port(self) -> None:
        insert = {
            "thread_size": "M3",
            "outer_diameter": 0.00455,
            "inner_diameter": 0.003,
            "height": 0.00574,
            "point_type": "insert",
            "head_form": "none",
        }
        assert catalog.fastener_form(insert) == "insert"
        derived = catalog.derive("fastener", insert)
        assert derived.envelope == "cyl:r0.002275h0.00574"
        assert derived.ports is not None and "thread" in derived.ports

    def test_a_headless_row_with_flats_is_a_nut_not_a_set_screw(self) -> None:
        nut = {
            "thread_size": "M4",
            "inner_diameter": 0.004,
            "across_flats": 0.007,
            "height": 0.0032,
            "head_form": "none",
        }
        assert catalog.fastener_form(nut) == "nut"

    def test_a_hand_entered_row_still_falls_back_to_its_spec_shape(self) -> None:
        """Rows that predate migration 0163 declare no form at all, and
        the old shape rules must still answer for them."""
        bare = {
            "thread_size": "M4",
            "outer_diameter": 0.004,
            "head_diameter": 0.007,
            "head_height": 0.004,
            "length": 0.012,
        }
        assert catalog.fastener_form(bare) == "screw"

    def test_a_row_that_says_nothing_at_all_is_none(self) -> None:
        assert catalog.fastener_form({"thread_size": "M4"}) is None


class TestDrivePort:
    def test_an_internal_drive_gets_a_port_to_sweep_a_driver_from(self) -> None:
        ports = catalog.derive("fastener", M4_CAP).ports
        assert ports is not None
        assert ports["drive"].annotations["drive_type"] == "socket"

    def test_an_external_hex_has_no_drive_port(self) -> None:
        """A hex-head bolt is driven on its flats, which is a different
        geometry — claiming a socket it does not have would put a bit
        driver into a finding about a spanner."""
        specs = dict(M4_CAP, drive_type="hex")
        ports = catalog.derive("fastener", specs).ports
        assert ports is not None and "drive" not in ports
