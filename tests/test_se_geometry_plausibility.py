"""Connect geometric plausibility (gr337040 + gr338426).

Pure geometry over a hand-built :class:`SeTree` — same posture as
``test_se_fasten.py``: :mod:`precis_se.geometry_plausibility` reads only
the tree and the cad kernel's own :func:`~precis.cad.relate.clearance`
primitive, no store, no DB, no ports/op-time vetting needed (a directly
constructed :class:`ConnectSpec` skips that layer entirely, same as the
fasten tests do).

Envelope primitives are base-at-pose: a ``cyl:rXhY`` spans local
``z=0..h`` from its own ``pose`` (never centred) — every fixture below
poses blocks with that convention in mind, and
``test_bearing_housed_sign_and_magnitude`` asserts the resulting
clearance sign explicitly (gr334763's clearance sign-flip class of bug).
"""

from __future__ import annotations

import pytest

from precis_se import geometry_plausibility as se_geom
from precis_se.ops import ConnectSpec, SeBlock, SeTree


def _block(
    name: str,
    pose: list[float],
    envelope: str | None,
    *,
    rot: list[float] | None = None,
) -> SeBlock:
    return SeBlock(
        name=name,
        pose=list(pose),
        envelope=envelope,
        rot=list(rot) if rot is not None else [0.0, 0.0, 0.0],
    )


def _tree(blocks: list[SeBlock], connects: list[ConnectSpec]) -> SeTree:
    tree = SeTree()
    for b in blocks:
        tree.blocks[b.name] = b
    tree.connects = connects
    return tree


def _connect(a: str, b: str, joint: dict | None) -> ConnectSpec:
    return ConnectSpec(a_block=a, a_port="p", b_block=b, b_port="p", joint=joint)


def _rule(findings: list, rule: str) -> list:
    return [f for f in findings if f.rule == rule]


# ── gr337040: connect_envelope_disjoint ─────────────────────────────────


def test_disjoint_declared_connect_is_flagged() -> None:
    # unicycle-printed-v1's own shape: a "seatpost" cylinder 0.15 m above
    # its "clamp" — floating, but declared connected.
    tree = _tree(
        [
            _block("clamp", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("seatpost", [0, 0, 0.49], "cyl:r0.008h0.15"),
        ],
        [_connect("clamp", "seatpost", {"class": "rigid"})],
    )
    findings = se_geom.findings(tree)
    hits = _rule(findings, "connect_envelope_disjoint")
    assert len(hits) == 1
    assert hits[0].severity == "warn"
    assert "clamp" in hits[0].subject and "seatpost" in hits[0].subject
    assert "0.47" in hits[0].detail or "apart" in hits[0].detail


def test_touching_declared_connect_is_not_flagged() -> None:
    tree = _tree(
        [
            _block("clamp", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("seatpost", [0, 0, 0.02], "cyl:r0.008h0.15"),
        ],
        [_connect("clamp", "seatpost", {"class": "rigid"})],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "connect_envelope_disjoint")


def test_axial_class_is_exempt_from_the_disjoint_check() -> None:
    # a pin-ended two-force member (a spoke) legitimately spans open space
    # between its endpoints — KINEMATIC_CLASSES's own docstring.
    tree = _tree(
        [
            _block("hub", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("rim", [0.3, 0, 0], "cyl:r0.01h0.02"),
        ],
        [
            _connect(
                "hub",
                "rim",
                {
                    "class": "axial",
                    "params": {
                        "tension_capacity": 100.0,
                        "compression_capacity": 0.0,
                    },
                },
            )
        ],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "connect_envelope_disjoint")


def test_undeclared_joint_still_gets_the_disjoint_check() -> None:
    # gr337040: "a declared connect" — no joint at all is still a claim
    # of physical contact.
    tree = _tree(
        [
            _block("a", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("b", [1.0, 0, 0], "cyl:r0.01h0.02"),
        ],
        [_connect("a", "b", None)],
    )
    findings = se_geom.findings(tree)
    assert _rule(findings, "connect_envelope_disjoint")


# ── gr338426: press/snap ⇒ interference ──────────────────────────────────


def test_press_mechanism_with_zero_overlap_is_flagged() -> None:
    tree = _tree(
        [
            _block("shaft", [0, 0, 0], "cyl:r0.005h0.02"),
            _block("boss", [0.02, 0, 0], "cyl:r0.005h0.02"),
        ],
        [_connect("shaft", "boss", {"class": "rigid", "mechanism": "press"})],
    )
    findings = se_geom.findings(tree)
    hits = _rule(findings, "mechanism_no_interference")
    assert len(hits) == 1
    assert hits[0].severity == "warn"
    assert "unbuildable" in hits[0].detail


def test_press_mechanism_with_real_interference_is_clean() -> None:
    tree = _tree(
        [
            _block("shaft", [0, 0, 0], "cyl:r0.005h0.02"),
            _block("boss", [0.001, 0, 0], "cyl:r0.005h0.02"),
        ],
        [_connect("shaft", "boss", {"class": "rigid", "mechanism": "press"})],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "mechanism_no_interference")


def test_snap_mechanism_is_covered_the_same_way() -> None:
    tree = _tree(
        [
            _block("a", [0, 0, 0], "cyl:r0.005h0.02"),
            _block("b", [0.02, 0, 0], "cyl:r0.005h0.02"),
        ],
        [_connect("a", "b", {"class": "rigid", "mechanism": "snap"})],
    )
    findings = se_geom.findings(tree)
    assert _rule(findings, "mechanism_no_interference")


# ── gr338426: captive ⇒ containment ──────────────────────────────────────


def test_captive_without_overlap_is_flagged() -> None:
    tree = _tree(
        [
            _block("ring", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("pin", [0.05, 0, 0], "cyl:r0.002h0.03"),
        ],
        [_connect("ring", "pin", {"class": "captive"})],
    )
    findings = se_geom.findings(tree)
    hits = _rule(findings, "captive_not_contained")
    assert len(hits) == 1
    assert hits[0].severity == "warn"


def test_captive_with_overlap_is_clean() -> None:
    tree = _tree(
        [
            _block("ring", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("pin", [0.001, 0, 0], "cyl:r0.002h0.03"),
        ],
        [_connect("ring", "pin", {"class": "captive"})],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "captive_not_contained")


# ── gr338426: screw class ⇒ overlap along its axis ───────────────────────


def test_screw_class_without_overlap_is_flagged() -> None:
    tree = _tree(
        [
            _block("nut", [0, 0, 0], "cyl:r0.005h0.02"),
            _block("lead", [0.05, 0, 0], "cyl:r0.003h0.1"),
        ],
        [_connect("nut", "lead", {"class": "screw", "axis": [0, 0, 1]})],
    )
    findings = se_geom.findings(tree)
    assert _rule(findings, "screw_axis_no_overlap")


def test_screw_class_with_overlap_is_clean() -> None:
    tree = _tree(
        [
            _block("nut", [0, 0, 0], "cyl:r0.005h0.02"),
            _block("lead", [0.001, 0, 0], "cyl:r0.003h0.1"),
        ],
        [_connect("nut", "lead", {"class": "screw", "axis": [0, 0, 1]})],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "screw_axis_no_overlap")


def test_screw_mechanism_alone_is_out_of_scope() -> None:
    # module docstring's scope decision: mechanism='screw' (threaded
    # fastening) is precis_se.fasten's territory — an ordinary bolted
    # flange (two face-to-face plates, no envelope-level overlap at all)
    # must NOT misfire here.
    tree = _tree(
        [
            _block("plate_a", [0, 0, 0], "box:w0.05d0.05h0.006"),
            _block("plate_b", [0, 0, 0.006], "box:w0.05d0.05h0.006"),
        ],
        [_connect("plate_a", "plate_b", {"class": "rigid", "mechanism": "screw"})],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "screw_axis_no_overlap")
    assert not _rule(findings, "mechanism_no_interference")


# ── clearance sign convention (base-at-pose, gr334763's history) ────────


def test_press_fit_clearance_sign_and_magnitude() -> None:
    # The base-at-pose clearance sign convention, asserted explicitly per
    # the task's own instruction (gr334763's history: a shallow
    # interpenetration silently flipped sign). Two cylinders offset
    # sideways by ``offset`` overlap by exactly ``offset - r_a - r_b``
    # metres, negative = interference — the same formula this repo's own
    # clearance tests already rely on (test_se_plugin.py's ``gap ~
    # 0.2 - 0.008 - 0.04``) — but ONLY while neither circle fully contains
    # the other (``|r_a - r_b| <= offset < r_a + r_b``, here
    # ``0.012 <= 0.02 < 0.028``); below that band the smaller body is
    # nested and the gap is the constant ``-2 * r_b`` instead, independent
    # of offset (the deepest point of the smaller body governs — see
    # ``test_bearing_housed_coaxial_and_nested_is_clean`` for that
    # regime).
    tree = _tree(
        [
            _block("a", [0, 0, 0], "cyl:r0.02h0.02"),
            _block("b", [0.02, 0, 0], "cyl:r0.008h0.02"),
        ],
        [_connect("a", "b", {"class": "rigid", "mechanism": "press"})],
    )
    pair = se_geom._pair_clearance(tree, "a", "b")
    assert pair is not None
    result, scale = pair
    gap_m = result.gap / scale
    assert gap_m == pytest.approx(0.02 - 0.02 - 0.008, abs=1e-4)
    assert gap_m < 0.0  # interference, never clearance — the sign this test asserts


# ── gr338426: bearing/revolute/cylindrical ⇒ coaxial + radially nested ──


def test_bearing_housed_coaxial_and_nested_is_clean() -> None:
    # unicycle-mk2's own real shape: a bearing coaxially nested inside a
    # leg pocket (gr338426 reports it reads as -12 mm interference on the
    # real design). The axis/radius rule doesn't depend on clearance()'s
    # exact number — it reads the envelopes' own parsed radii and
    # pose/rot directly — so this only asserts the coaxial + nested
    # outcome, not a hand-derived clearance figure.
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "cyl:r0.02h0.05"),
            _block("bearing", [0, 0, 0.01], "cyl:r0.008h0.01"),
        ],
        [
            _connect(
                "fork_leg",
                "bearing",
                {"class": "revolute", "axis": [0, 0, 1], "mechanism": "bearing"},
            )
        ],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "axis_not_coaxial")
    assert not _rule(findings, "axis_not_radially_contained")


def test_tangent_bearing_is_flagged_not_coaxial() -> None:
    # gr338426's own motivating bug: bearings TANGENT below the fork legs
    # — point contact, axes parallel but offset by r_a + r_b, no radial
    # nesting at all. gap ~ 0 (externally tangent circles), so
    # connect_envelope_disjoint must NOT fire either (gr338426: "tangent
    # is not disjoint") — only the coaxial check catches this.
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "cyl:r0.02h0.05"),
            _block("bearing", [0.028, 0, 0.01], "cyl:r0.008h0.01"),
        ],
        [
            _connect(
                "fork_leg",
                "bearing",
                {"class": "revolute", "axis": [0, 0, 1], "mechanism": "bearing"},
            )
        ],
    )
    findings = se_geom.findings(tree)
    coax = _rule(findings, "axis_not_coaxial")
    assert len(coax) == 1
    assert coax[0].severity == "warn"
    assert not _rule(findings, "connect_envelope_disjoint")


def test_crossed_axes_are_flagged_not_coaxial() -> None:
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "cyl:r0.02h0.05"),
            _block(
                "bearing",
                [0, 0, 0.01],
                "cyl:r0.008h0.01",
                rot=[1.5707963267948966, 0, 0],  # +90° about x — axis now ±y
            ),
        ],
        [
            _connect(
                "fork_leg", "bearing", {"class": "revolute", "mechanism": "bearing"}
            )
        ],
    )
    findings = se_geom.findings(tree)
    assert _rule(findings, "axis_not_coaxial")


def test_coaxial_but_not_radially_nested_is_flagged() -> None:
    # same axis line, but the pocket TAPERS narrower (a tcone: 0.02 at its
    # base down to 0.006 at its far end) than the bearing's 0.008 radius —
    # two plain cylinders can never fail this check (whichever has the
    # smaller radius trivially nests in the other by construction), so
    # only a tapered envelope gives the containment half of the rule real
    # bite; see _axis_findings' own docstring.
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "tcone:rb0.02rt0.006h0.05"),
            _block("bearing", [0, 0, 0.01], "cyl:r0.008h0.01"),
        ],
        [
            _connect(
                "fork_leg", "bearing", {"class": "revolute", "mechanism": "bearing"}
            )
        ],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "axis_not_coaxial")
    hits = _rule(findings, "axis_not_radially_contained")
    assert len(hits) == 1
    assert hits[0].severity == "warn"


def test_non_circular_envelope_is_an_honest_skip() -> None:
    # a box has no inferable axis — this pass must not fabricate one.
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "box:w0.04d0.04h0.05"),
            _block("bearing", [0, 0, 0.01], "cyl:r0.008h0.01"),
        ],
        [
            _connect(
                "fork_leg", "bearing", {"class": "revolute", "mechanism": "bearing"}
            )
        ],
    )
    findings = se_geom.findings(tree)
    assert not _rule(findings, "axis_not_coaxial")
    assert not _rule(findings, "axis_not_radially_contained")


# ── malformed / cross-scale / budget honesty ─────────────────────────────


def test_malformed_stored_joint_does_not_crash_or_double_report() -> None:
    tree = _tree(
        [
            _block("a", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("b", [1.0, 0, 0], "cyl:r0.01h0.02"),
        ],
        [_connect("a", "b", {"kind": "revolute"})],  # pre-slice-3 free-form shape
    )
    findings = se_geom.findings(tree)  # must not raise
    # still gets the disjoint check (a declared connect regardless of a
    # malformed joint payload) but none of the mechanism/class rules,
    # which all require a validated joint.
    assert _rule(findings, "connect_envelope_disjoint")
    assert not _rule(findings, "mechanism_no_interference")


def test_envelope_less_block_is_an_honest_skip() -> None:
    tree = _tree(
        [
            _block("a", [0, 0, 0], None),
            _block("b", [1.0, 0, 0], "cyl:r0.01h0.02"),
        ],
        [_connect("a", "b", {"class": "rigid", "mechanism": "press"})],
    )
    findings = se_geom.findings(tree)
    assert findings == []


def test_budget_exceeded_reports_unchecked_not_clean() -> None:
    tree = _tree(
        [
            _block("a", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("b", [1.0, 0, 0], "cyl:r0.01h0.02"),
        ],
        [_connect("a", "b", {"class": "rigid"})],
    )
    findings = se_geom.findings(tree, budget_s=-1.0)
    hits = _rule(findings, "geometry_plausibility_budget_exceeded")
    assert len(hits) == 1
    assert hits[0].severity == "warn"
    assert "a.p—b.p" in hits[0].detail
    # the budget-blown connect is UNCHECKED, not silently reported clean.
    assert not _rule(findings, "connect_envelope_disjoint")


# ── unicycle-mk2 / unicycle-printed-v1 fixture pair (task spec) ──────────


def _unicycle_mk2_like(*, housed: bool) -> SeTree:
    """A minimal mirror of the two real unicycle designs named in both
    gripes — just the parts each incident actually turned on: a fork leg
    housing a bearing (revolute/bearing mechanism) and a seatpost seated
    in its clamp (a plain rigid connect). ``housed=True`` mirrors
    unicycle-mk2 (the positive control, correctly housed/seated);
    ``housed=False`` mirrors unicycle-printed-v1 (the negative — floating
    seatpost, tangent bearing)."""
    if housed:
        bearing_pose = [0, 0, 0.01]
        seatpost_pose = [0, 0, 0.02]
    else:
        bearing_pose = [0.028, 0, 0.01]  # tangent, not disjoint (gr338426)
        seatpost_pose = [0, 0, 0.49]  # 144 mm-style floating gap (gr337040)
    tree = _tree(
        [
            _block("fork_leg", [0, 0, 0], "cyl:r0.02h0.05"),
            _block("bearing", bearing_pose, "cyl:r0.008h0.01"),
            _block("clamp", [0, 0, 0], "cyl:r0.01h0.02"),
            _block("seatpost", seatpost_pose, "cyl:r0.008h0.15"),
        ],
        [
            _connect(
                "fork_leg",
                "bearing",
                {"class": "revolute", "axis": [0, 0, 1], "mechanism": "bearing"},
            ),
            _connect("clamp", "seatpost", {"class": "rigid"}),
        ],
    )
    return tree


def test_unicycle_mk2_positive_control_is_clean() -> None:
    findings = se_geom.findings(_unicycle_mk2_like(housed=True))
    assert findings == []


def test_unicycle_printed_v1_negative_control_is_flagged() -> None:
    findings = se_geom.findings(_unicycle_mk2_like(housed=False))
    rules = {f.rule for f in findings}
    assert "axis_not_coaxial" in rules  # the tangent bearing
    assert "connect_envelope_disjoint" in rules  # the floating seatpost
    assert all(f.severity == "warn" for f in findings)
