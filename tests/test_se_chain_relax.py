"""``relax_chain`` and the handler-side ``chain_floppy`` re-emission —
:mod:`precis_se.chain.relax`.

The acceptance criteria this file IS: two helices joined by a 2-nt loop
settle to exit-to-exit ≤ 1.9 nm; settled poses come back
``origin='proposed'`` and a ``user`` pose needs ``move=``; ``relax_chain``
is handler-level and therefore a proposal in the web turn; a hairpin's
domain row has no ``meta.loop_curve`` before and a sampled curve after,
ending within one bond of the two :func:`precis_chain.fibre.backbone_exit`
points; and a design whose helix carries a ``material``
``persistence_length`` row gets exactly ONE ``chain_floppy`` per span,
measured against that row.

Every distance here is recomputed from the tree (the helix geometry's own
exits), never read off a stored blob.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.material import MaterialHandler
from precis.store import Store
from precis.utils.units import format_quantity
from precis_se import compose as se_compose
from precis_se import persist
from precis_se.atomic.apply import HANDLER_LEVEL_OPS, all_op_names
from precis_se.chain import nucleic
from precis_se.chain.layout import helix_geometry
from precis_se.chain.relax import op_relax_chain
from precis_se.handler import SeHandler
from precis_se.ops import OpError, SeTree, apply_ops
from precis_web.design_turn import is_auto_apply

#: The pair's axis separation — the criterion's "starting 6 nm apart".
PAIR_SPACING_M = 6.0e-9
#: The criterion's target: a 2-nt loop's own contour is (2+1)·0.63 nm =
#: 1.89 nm, so this is "the loop ends up taut, not stretched".
TARGET_GAP_M = 1.9e-9


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


@pytest.fixture
def material(store: Store) -> MaterialHandler:
    return MaterialHandler(hub=Hub(store=store))


def _straight_helix_ops(name: str, x_m: float, n_units: int) -> list[dict[str, Any]]:
    """A free-waypoint helix running along ``+z`` at ``x = x_m`` — no
    lattice, so the pair's spacing is exactly what the test states."""
    length = (n_units - 1) * nucleic.B_DNA_RISE_M
    return [
        {"op": "add_block", "name": name},
        {
            "op": "declare_helix",
            "block": name,
            "n_units": n_units,
            "path": {
                "waypoints": [
                    [f"{x_m} m", "0 m", "0 m"],
                    [f"{x_m} m", "0 m", f"{length} m"],
                ]
            },
        },
    ]


def _pair_tree(*, n_units: int = 10, loop_nt: int = 2) -> SeTree:
    """Two parallel helices :data:`PAIR_SPACING_M` apart, joined by one
    ``loop_nt``-nt loop, laid out."""
    ops: list[dict[str, Any]] = []
    ops += _straight_helix_ops("h0", 0.0, n_units)
    ops += _straight_helix_ops("h1", PAIR_SPACING_M, n_units)
    ops += [
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 5,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h1",
            "start": 0,
            "end": 5,
            "forward": False,
            "loop_before_nt": loop_nt,
        },
        {"op": "layout_chain"},
    ]
    tree = SeTree()
    apply_ops(tree, ops)
    return tree


def _hairpin_tree() -> SeTree:
    """One helix, one strand, two antiparallel domains on it and a 4-nt
    loop between them — the item's hairpin."""
    ops: list[dict[str, Any]] = _straight_helix_ops("h0", 0.0, 12)
    ops += [
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 4,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 4,
            "end": 8,
            "forward": False,
            "loop_before_nt": 4,
        },
        {"op": "layout_chain"},
    ]
    tree = SeTree()
    apply_ops(tree, ops)
    return tree


def _segment_of(tree: SeTree, helix: str, offset: int) -> Any:
    """The segment child covering ``offset`` of ``helix``."""
    for node in tree.blocks.values():
        record = node.chain or {}
        if record.get("role") != "segment" or record.get("helix") != helix:
            continue
        if int(record["start"]) <= offset <= int(record["end"]):
            return node
    raise AssertionError(f"no segment covers {helix}[{offset}]")


def _exit_world(tree: SeTree, helix: str, offset: int, forward: bool) -> np.ndarray:
    """A backbone exit as the segment's CURRENT placement puts it — the
    nominal exit carried through the segment child's own pose/rot, which is
    exactly what the settle moved."""
    from precis.cad.vec import as_vec3, pose
    from precis_chain.envelope import capsule_pose
    from precis_se.chain.layout import segment_capsule

    geom = helix_geometry(tree.blocks[helix])
    node = _segment_of(tree, helix, offset)
    record = node.chain or {}
    start, end = int(record["start"]), int(record["end"])
    _origin, euler, _length = capsule_pose(segment_capsule(geom, start, end))
    nominal = pose(
        as_vec3([float(v) for v in geom.origin(start)]), as_vec3(list(euler))
    )
    actual = pose(as_vec3(list(node.pose)), as_vec3(list(node.rot)))
    local = nominal.to_local_point(
        as_vec3([float(v) for v in geom.exit(offset, forward)])
    )
    return np.asarray(actual.to_world_point(local), dtype=float)


# ── the settle itself ───────────────────────────────────────────────────


def test_two_helices_joined_by_a_2nt_loop_settle_to_the_loop_contour() -> None:
    tree = _pair_tree()
    before = float(
        np.linalg.norm(
            helix_geometry(tree.blocks["h1"]).exit(4, False)
            - helix_geometry(tree.blocks["h0"]).exit(4, True)
        )
    )
    # The exits start further apart than a 2-nt loop can bridge …
    assert before > TARGET_GAP_M
    echo = op_relax_chain(None, tree, {"op": "relax_chain"})
    after = float(
        np.linalg.norm(
            _exit_world(tree, "h1", 4, False) - _exit_world(tree, "h0", 4, True)
        )
    )
    assert after <= TARGET_GAP_M, f"settled to {after:.3e} m from {before:.3e} m"
    # … and the settle is not merely reporting a number it never reached:
    # convergence is in the echo, and it is the real flag.
    assert "converged" in echo and "NOT converged" not in echo
    assert "2 segment bodies over 2 helices with 1 loop spring(s)" in echo
    assert "coded B-DNA default" in echo


def test_settled_poses_are_proposed_and_a_user_pose_needs_move() -> None:
    tree = _pair_tree()
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "h1.s0",
                "pose": [float(v) for v in tree.blocks["h1.s0"].local_pose],
                "origin": "user",
            }
        ],
    )
    pinned = list(tree.blocks["h1.s0"].pose)
    free = list(tree.blocks["h0.s0"].pose)
    op_relax_chain(None, tree, {"op": "relax_chain"})
    # The user's placement is contract: untouched, and still unstamped.
    assert tree.blocks["h1.s0"].pose == pinned
    assert "pose" not in tree.blocks["h1.s0"].origins
    # The proposed one moved, and says so.
    assert tree.blocks["h0.s0"].pose != free
    assert tree.blocks["h0.s0"].origins["pose"] == "proposed"

    # move= is the author's explicit authorisation, and it works by helix
    # name as well as by segment name.
    op_relax_chain(None, tree, {"op": "relax_chain", "move": ["h1"]})
    assert tree.blocks["h1.s0"].pose != pinned
    assert tree.blocks["h1.s0"].origins["pose"] == "proposed"


def test_a_second_settle_continues_from_the_first_rather_than_resetting() -> None:
    tree = _pair_tree()
    op_relax_chain(None, tree, {"op": "relax_chain"})
    once = np.asarray(tree.blocks["h0.s0"].pose, dtype=float)
    op_relax_chain(None, tree, {"op": "relax_chain"})
    twice = np.asarray(tree.blocks["h0.s0"].pose, dtype=float)
    # Already at equilibrium: the second call moves it by well under a bond.
    assert float(np.linalg.norm(twice - once)) < 0.1 * nucleic.SS_CONTOUR_PER_NT_M


def test_segment_length_survives_the_settle() -> None:
    """The rigid-body term is a stiff spring, not a constraint, so the
    write-back re-imposes the motif's own segment length — otherwise the
    stored ``cyl`` envelope and the pose would drift apart."""
    tree = _pair_tree(n_units=25)  # > one default segment → 2 bodies per helix
    geom = helix_geometry(tree.blocks["h0"])
    record = tree.blocks["h0.s0"].chain or {}
    # Origin to origin plus the half-rise cell at each end
    # (:func:`precis_se.chain.layout.segment_capsule`).
    nominal = (
        float(
            np.linalg.norm(
                geom.origin(int(record["end"])) - geom.origin(int(record["start"]))
            )
        )
        + geom.motif.rise
    )
    op_relax_chain(None, tree, {"op": "relax_chain"})
    a = np.asarray(tree.blocks["h0.s0"].pose, dtype=float)
    b = np.asarray(tree.blocks["h0.s1"].pose, dtype=float)
    # s1's origin is s0's far end (the weld), so |b - a| IS s0's length.
    assert float(np.linalg.norm(b - a)) == pytest.approx(nominal, rel=0.02)


# ── the meta.loop_curve seam ────────────────────────────────────────────


def test_hairpin_gets_its_loop_curve_only_from_relax_chain() -> None:
    tree = _hairpin_tree()
    loop_row = next(d for d in tree.domains if d.ord == 1)
    # Before: no curve at all — the distinction a realizer reads.
    assert loop_row.loop_curve is None
    assert "loop_curve" not in loop_row.meta()
    assert next(d for d in tree.domains if d.ord == 0).loop_curve is None

    op_relax_chain(None, tree, {"op": "relax_chain"})
    curve = loop_row.loop_curve
    assert curve is not None and len(curve) >= 8
    # Points are metres (a 12-bp helix is nanometres across, so every
    # coordinate is well under a micron) …
    assert all(abs(v) < 1e-6 for point in curve for v in point)
    # … and the curve's ends sit within one backbone bond of the two exits
    # the loop is pinned at.
    bond = nucleic.SS_CONTOUR_PER_NT_M
    p = _exit_world(tree, "h0", 3, True)
    q = _exit_world(tree, "h0", 7, False)
    assert float(np.linalg.norm(np.asarray(curve[0]) - p)) <= bond
    assert float(np.linalg.norm(np.asarray(curve[-1]) - q)) <= bond
    # The row with no preceding loop never gets one.
    assert next(d for d in tree.domains if d.ord == 0).loop_curve is None


def test_the_loop_curve_round_trips_through_the_store(
    handler: SeHandler, store: Store
) -> None:
    ops = [
        *_straight_helix_ops("h0", 0.0, 12),
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 4,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 4,
            "end": 8,
            "forward": False,
            "loop_before_nt": 4,
        },
        {"op": "layout_chain"},
        {"op": "relax_chain"},
    ]
    res = handler.put(id="hairpin", text=json.dumps({"ops": ops}))
    assert "relax_chain: settled" in res.body
    ref = store.get_ref(kind="se", id="hairpin")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    rows = {d.ord: d for d in tree.domains}
    assert rows[0].loop_curve is None
    curve = rows[1].loop_curve
    assert curve is not None and all(len(point) == 3 for point in curve)


# ── dispatch: handler-level, and a proposal in the web turn ─────────────


def test_relax_chain_is_handler_level_and_never_auto_applies() -> None:
    assert "relax_chain" in HANDLER_LEVEL_OPS
    assert "relax_chain" in all_op_names()
    ops = [{"op": "relax_chain"}]
    assert is_auto_apply(ops, kind="se") is False
    # And it does not auto-apply even mixed with ops that would.
    assert is_auto_apply([{"op": "add_block", "name": "b"}], kind="se") is True
    assert is_auto_apply([{"op": "add_block", "name": "b"}, *ops], kind="se") is False


# ── refusals: every one before any mutation ─────────────────────────────


def test_a_helix_with_no_layout_chain_children_is_refused_by_name() -> None:
    tree = SeTree()
    apply_ops(tree, _straight_helix_ops("h0", 0.0, 10))
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain"})
    assert "helix 'h0' has no layout_chain children" in str(exc.value)
    assert "run layout_chain first" in str(exc.value)


def test_a_design_with_no_helices_is_refused() -> None:
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", "name": "b"}])
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain"})
    assert "declares no helices" in str(exc.value)


def test_every_user_pose_and_no_move_is_refused_rather_than_settling_nothing() -> None:
    tree = _pair_tree()
    for name in ("h0.s0", "h1.s0"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_pose",
                    "block": name,
                    "pose": [float(v) for v in tree.blocks[name].local_pose],
                    "origin": "user",
                }
            ],
        )
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain"})
    assert "no movable segments" in str(exc.value)


def test_move_naming_nothing_is_refused_with_the_roster() -> None:
    tree = _pair_tree()
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain", "move": ["nope"]})
    assert "names no helix and no segment: 'nope'" in str(exc.value)
    assert "Helices: h0, h1" in str(exc.value)


def test_unknown_keys_and_a_bad_iters_are_refused() -> None:
    tree = _pair_tree()
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain", "steps": 10})
    assert "unknown key(s) steps" in str(exc.value)
    with pytest.raises(OpError) as exc:
        op_relax_chain(None, tree, {"op": "relax_chain", "iters": 0})
    assert "'iters' must be between 1 and" in str(exc.value)
    # Nothing was written by either refusal.
    assert all(
        node.origins.get("pose") == "proposed"
        for name, node in tree.blocks.items()
        if ".s" in name
    )


def test_a_raw_state_key_must_be_resolved_before_the_settle() -> None:
    """``state=`` reaches the op RESOLVED (:mod:`precis_se.state_arg`,
    from the handler-level dispatch); a bare ``state`` key with nothing
    resolved is refused rather than settled as if stateless."""
    tree = _pair_tree()
    with pytest.raises(OpError, match="must be resolved"):
        op_relax_chain(None, tree, {"op": "relax_chain", "state": {"w": "st0"}})


# ── the handler-side chain_floppy re-emission ───────────────────────────


def _floppy_design_ops(helix_units: int = 4) -> list[dict[str, Any]]:
    """One helix carrying a single ``helix_units``-nt single-stranded
    domain — one ``chain_floppy`` span, nothing else."""
    return [
        *_straight_helix_ops("h0", 0.0, helix_units),
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": helix_units,
            "forward": True,
        },
    ]


def _lp_material(
    material: MaterialHandler, store: Store, design: str, *, nm: float
) -> None:
    """A ``material`` persistence-length row the design is ``made-of``,
    scoped to helix ``h0`` — the star-schema path the settle and the
    finding both resolve through."""
    material.put(id="mat-lp", title="mat-lp")
    material.put(
        id="mat-lp",
        property=se_compose.LP_KEY,
        value=nm,
        unit="nm",
        conditions={"salt": "500 mM NaCl"},
    )
    design_ref = store.get_ref(kind="se", id=design)
    mat_ref = store.get_ref(kind="material", id="mat-lp")
    assert design_ref is not None and mat_ref is not None
    store.add_link(
        src_ref_id=design_ref.id,
        dst_ref_id=mat_ref.id,
        relation="made-of",
        meta={"block": "h0"},
    )


def test_without_a_material_row_the_pure_floppy_rows_survive_unchanged(
    handler: SeHandler,
) -> None:
    handler.put(id="floppy", text=json.dumps({"ops": _floppy_design_ops()}))
    body = handler.get(id="floppy", view="drc").body
    assert body.count("chain_floppy") == 1
    coded = format_quantity(nucleic.LP_SSDNA_M, "length")
    assert f"past ssDNA's persistence length {coded}" in body
    assert "coded default; a material Lp row with conditions overrides it" in body


def test_a_material_lp_row_replaces_the_coded_floppy_row_one_for_one(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    handler.put(id="floppy", text=json.dumps({"ops": _floppy_design_ops()}))
    # 1 nm: still under the 4-nt span's 2.52 nm of contour, so the SAME span
    # is reported — against the row's number instead of the coded one.
    _lp_material(material, store, "floppy", nm=1.0)
    body = handler.get(id="floppy", view="drc").body
    # ONE row for the one span — never the pure row and the re-emitted one.
    assert body.count("chain_floppy") == 1
    assert "past ssDNA's persistence length 1 nm" in body
    coded = format_quantity(nucleic.LP_SSDNA_M, "length")
    assert f"persistence length {coded}" not in body
    # The row's own conditions are named — a measured Lp without them is not
    # a measurement.
    assert "500 mM NaCl" in body


def test_the_material_row_also_reaches_the_settles_hinge_stiffness(
    handler: SeHandler, material: MaterialHandler, store: Store
) -> None:
    ops = [*_straight_helix_ops("h0", 0.0, 60), {"op": "layout_chain"}]
    handler.put(id="stiff", text=json.dumps({"ops": ops}))
    _lp_material(material, store, "stiff", nm=45.0)
    # Through the handler, because that is what wires ``tree.own_slug`` —
    # the design identity the star-schema resolution needs
    # (``persist.load_tree`` deliberately does not set it).
    echo = handler.edit(id="stiff", ops=[{"op": "relax_chain"}]).body
    # The summary names the row and its conditions, not the coded default —
    # a settle at 45 nm and one at 50 nm are different answers.
    assert "material row 45 nm" in echo
    assert "500 mM NaCl" in echo
    assert "coded B-DNA default" not in echo
    # 60 units of a lattice-free helix is 3 default segments, so there are
    # hinges for the Lp to have reached.
    assert "3 segment bodies" in echo


def test_the_helix_geometry_still_places_the_exits_where_the_azimuths_say() -> None:
    """Guard on the pass-A azimuth correction this settle rests on: the two
    strands' azimuths are groove-asymmetric, NOT antipodal, so a forward and
    a reverse exit at one offset are not π apart."""
    geom = helix_geometry(_pair_tree().blocks["h0"])
    forward = nucleic.strand_azimuth_rad(geom.base_motif, True)
    reverse = nucleic.strand_azimuth_rad(geom.base_motif, False)
    separation = abs((reverse - forward + math.pi) % (2.0 * math.pi) - math.pi)
    assert separation != pytest.approx(math.pi, abs=1e-6)
    assert 0.0 < separation < math.pi


# ── loop tangents at a helix end (gripe 457929) ─────────────────────────


def _end_hairpin_tree() -> SeTree:
    """The dogfood hairpin: a 4-unit helix whose one strand pairs with
    itself, the 4-nt loop leaving the helix's LAST unit on both exits."""
    ops: list[dict[str, Any]] = _straight_helix_ops("h0", 0.0, 4)
    ops += [
        {"op": "add_block", "name": "s"},
        {"op": "declare_strand", "block": "s"},
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 4,
            "forward": True,
        },
        {
            "op": "add_domain",
            "strand": "s",
            "helix": "h0",
            "start": 0,
            "end": 4,
            "forward": False,
            "loop_before_nt": 4,
        },
        {"op": "layout_chain"},
    ]
    tree = SeTree()
    apply_ops(tree, ops)
    return tree


def test_a_hairpin_at_the_helix_end_caps_it_rather_than_lying_flat() -> None:
    tree = _end_hairpin_tree()
    op_relax_chain(None, tree, {"op": "relax_chain"})
    curve = np.asarray(next(d for d in tree.domains if d.ord == 1).loop_curve)
    # Both exits sit in the last pair's plane (the helix runs along +z) …
    p = _exit_world(tree, "h0", 3, True)
    q = _exit_world(tree, "h0", 3, False)
    assert abs(float(p[2] - q[2])) < 1e-12
    # … and the loop leaves that plane along +z on its way round: every
    # interior sample is beyond the last pair, by a good fraction of a
    # backbone bond at the apex, instead of the flat in-plane bow the
    # radial-only tangents drew.
    reach = curve[1:-1, 2] - float(p[2])
    assert np.all(reach > 0), reach
    assert float(reach.max()) > 0.4 * nucleic.SS_CONTOUR_PER_NT_M, reach.max()


def test_only_a_terminal_unit_exit_gets_an_axial_tangent() -> None:
    from precis_se.chain.relax import _axial_sign

    geom = helix_geometry(_end_hairpin_tree().blocks["h0"])
    last = geom.n_units - 1
    # Leaving: a forward strand off the last unit runs +t, a reverse strand
    # off unit 0 runs -t.
    assert _axial_sign(geom, last, True, leaving=True) == 1.0
    assert _axial_sign(geom, 0, False, leaving=True) == -1.0
    # Entering: the outward direction is the strand's reversed.
    assert _axial_sign(geom, 0, True, leaving=False) == -1.0
    assert _axial_sign(geom, last, False, leaving=False) == 1.0
    # A mid-helix exit, or the wrong end for the strand's direction, keeps
    # the sideways-only tangent a crossover needs.
    assert _axial_sign(geom, 1, True, leaving=True) == 0.0
    assert _axial_sign(geom, 0, True, leaving=True) == 0.0
    assert _axial_sign(geom, last, True, leaving=False) == 0.0


def test_a_mid_helix_exit_tangent_stays_sideways_and_follows_the_settled_body() -> None:
    from precis_chain.relax import Attachment
    from precis_se.chain.relax import _Body, _exit_tangent

    # A body declared along +z, settled into a tilt: the tangent is the
    # radial offset carried into the SETTLED frame (so it stays
    # perpendicular to the body's new axis, where the old code handed
    # loop_curve the nominal-frame offset), and on a mid-helix exit it
    # has no axial component at all …
    body = _Body(
        name="h0.s0",
        helix="h0",
        start=0,
        end=9,
        a_nm=np.zeros(3),
        b_nm=np.array([0.0, 0.0, 3.0]),
        radius_nm=1.0,
        length_nm=3.0,
        carry=np.eye(3),
        movable=True,
    )
    settled = np.array([[[0.0, 0.0, 0.0], [0.0, 1.0, 3.0]]])
    axis = settled[0, 1] / np.linalg.norm(settled[0, 1])
    att = Attachment(body=0, along=0.5, offset=(0.0, 1.0, 0.0))
    side = _exit_tangent(body, settled, 0, att, 0.0)
    assert abs(float(np.dot(side, axis))) < 1e-12
    assert abs(float(np.linalg.norm(side)) - 1.0) < 1e-12
    # … while a terminal-unit exit adds exactly one unit of the axis.
    capped = _exit_tangent(body, settled, 0, att, 1.0)
    assert np.allclose(capped - side, axis)
