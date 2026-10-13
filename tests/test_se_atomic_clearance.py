"""``view='clearance'``'s **atomic** path — the valve doc's design tools
1–2 (``docs/backlog/rotary-ratchet-valve.md`` "Design tool set",
:mod:`precis_se.atomic.clearance`): the rotor-vs-shell van der Waals gap
as a function of rotor angle, and the pocket census whose count is the
metering number.

Two generated zigzag hexfold tubes — ``tube(12,0)`` inside ``tube(24,0)``
— posed coaxially are the fixture: a carbon rotor in a carbon shell whose
radial difference is set by the lattice constant alone, so the expected
vdW gap is exact arithmetic, not a fitted value.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.structure.scene import Scene as StructScene
from precis_se import persist
from precis_se.atomic import clearance as se_atomic_clearance
from precis_se.atomic.clearance import VDW_RADII_A, AtomCloud
from precis_se.handler import SeHandler, _render_clearance_digest
from precis_se.ops import SeTree, apply_ops

#: (n,0) zigzag tube radius is n·a/(2π) with a = √3·σ, σ = 1.42 Å —
#: R(12) ≈ 4.698, R(24) ≈ 9.395, so the coaxial centre-distance gap is
#: ~4.698 Å and the C–C van der Waals gap ~1.298 Å.
_TUBE12 = """\
hexfold 0.2
lattice: element=C sigma=1.42
origin t
t: tube(12,0,len=2)
"""

_TUBE24 = """\
hexfold 0.2
lattice: element=C sigma=1.42
origin s
s: tube(24,0,len=2)
"""

_VDW_C = 1.70


@pytest.fixture
def handler(hub: Hub) -> SeHandler:
    return SeHandler(hub=hub)


def _put_valve(handler: SeHandler, slug: str, *, offset_x_A: float = 0.0) -> None:
    """Shell (24,0) + rotor (12,0), coaxial at the origin — bare carbon
    nets (``terminate: 'none'``) so every atom shares the C radius and the
    expected gap is exactly radial-difference − 2·1.70."""
    ops: list[dict[str, Any]] = [
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE24, "terminate": "none"},
            "name": "shell",
        },
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE12, "terminate": "none"},
            "name": "rotor",
        },
    ]
    if offset_x_A:
        ops.append(
            {
                "op": "set_pose",
                "block": "rotor",
                "pose": f"[{offset_x_A * 1e-10:g} 0 0] m",
            }
        )
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def _scene(store: Store, slug: str) -> StructScene:
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None, slug
    scene, _handles = store.structure_load(ref.id)
    return scene


def _carts_A(scene: StructScene) -> np.ndarray:
    """Atom Cartesian positions (Å) in the scene's own cell frame."""
    return np.asarray(
        [scene.cell.frac_to_cart(atom.frac) for atom in scene.atoms.values()],
        dtype=float,
    )


def _median_radius_A(pts: np.ndarray) -> float:
    xy = pts[:, :2] - pts[:, :2].mean(axis=0)
    return float(np.median(np.hypot(xy[:, 0], xy[:, 1])))


def _min_gap_A(pa: np.ndarray, pb: np.ndarray, radii: float = 2 * _VDW_C) -> float:
    """Independent brute-force of the same physics the view reports:
    minimum cross pair centre distance minus the two radii."""
    d = np.linalg.norm(pa[:, None, :] - pb[None, :, :], axis=2)
    return float(d.min()) - radii


def _rotz(pts: np.ndarray, centre: np.ndarray, theta: float) -> np.ndarray:
    c, s = math.cos(theta), math.sin(theta)
    R = np.array([[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]])
    return centre + (pts - centre) @ R.T


def _toon_rows(body: str, header_key: str) -> list[dict[str, str]]:
    """Parse one TOON table out of a rendered view: the ``{...}`` header
    line containing ``header_key`` plus its following tab-separated rows."""
    lines = body.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("{") and header_key in line:
            cols = line.strip("{}").split("\t")
            rows = []
            for data in lines[i + 1 :]:
                if not data or data.startswith("{") or "\t" not in data:
                    break
                cells = data.split("\t")
                if len(cells) != len(cols):
                    break
                rows.append(dict(zip(cols, cells, strict=True)))
            return rows
    return []


def test_atomic_pair_gap_is_radial_difference_minus_two_vdw(
    handler: SeHandler, store: Store
) -> None:
    _put_valve(handler, "clear1")
    body = handler.get(
        id="clear1", view="clearance", args={"a": "shell", "b": "rotor"}
    ).body
    m = re.search(r"gap: (-?[\d.]+) Å", body)
    assert m, body
    measured = float(m.group(1))
    shell_r = _median_radius_A(_carts_A(_scene(store, "clear1-shell")))
    rotor_r = _median_radius_A(_carts_A(_scene(store, "clear1-rotor")))
    expected = (shell_r - rotor_r) - 2 * _VDW_C
    assert abs(measured - expected) < 0.05, (measured, expected, body)
    assert "free" in body and "worst pair" in body
    # same answer measured a third way: brute force over the loaded scenes
    # (rendered to 2 decimals — compare at print precision)
    assert (
        abs(
            _min_gap_A(
                _carts_A(_scene(store, "clear1-rotor")),
                _carts_A(_scene(store, "clear1-shell")),
            )
            - measured
        )
        < 0.005
    )


def test_coaxial_sweep_reproduces_min_gap_at_every_angle(
    handler: SeHandler,
) -> None:
    """A (12,0) zigzag tube is invariant under 30° rotation about its own
    axis, so a 90°-step coaxial sweep re-measures the identical pose."""
    _put_valve(handler, "clear2")
    body = handler.get(
        id="clear2",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "sweep": {"step_deg": 90, "range_deg": 360},
        },
    ).body
    rows = _toon_rows(body, "angle")
    assert len(rows) == 4, body
    gaps = [float(r["min gap (Å)"]) for r in rows]
    assert max(gaps) - min(gaps) < 0.05, (gaps, body)
    assert "verdict: free" in body


def test_offset_rotor_gap_varies_and_worst_angle_is_the_touching_side(
    handler: SeHandler, store: Store
) -> None:
    _put_valve(handler, "clear3", offset_x_A=2.0)
    step = 15
    body = handler.get(
        id="clear3",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "sweep": {"step_deg": step, "range_deg": 360},
        },
    ).body
    rows = _toon_rows(body, "angle")
    assert len(rows) == 24, body
    gaps = np.array([float(r["min gap (Å)"]) for r in rows])
    assert gaps.max() - gaps.min() > 0.05, (gaps, body)

    # expected worst angle, derived independently: rotate the loaded
    # rotor cloud about its own z axis through (2, 0, 0) Å and brute-force
    # the minimum pair gap at each sample.
    shell = _carts_A(_scene(store, "clear3-shell"))
    rotor = _carts_A(_scene(store, "clear3-rotor")) + np.array([2.0, 0.0, 0.0])
    centre = np.array([2.0, 0.0, 0.0])
    expected_gaps = [
        _min_gap_A(_rotz(rotor, centre, math.radians(k * step)), shell)
        for k in range(24)
    ]
    m = re.search(r"worst angle: ([\d.]+)°", body)
    assert m, body
    # (12,0) is 30°-periodic, so the same minimum can appear at several
    # sampled angles — the reported one must BE a minimum, not a
    # particular tied index.
    reported = float(m.group(1))
    k = round(reported / step) % 24
    assert math.isclose(reported / step, round(reported / step))
    assert expected_gaps[k] == pytest.approx(min(expected_gaps), abs=1e-6)


def test_interpenetrating_pose_reports_the_under_zero_count(
    handler: SeHandler,
) -> None:
    """3 Å of offset pushes the rotor's vdW spheres ~1.7 Å inside the
    shell's — ``interpenetrating`` with a nonzero under-0 census."""
    _put_valve(handler, "clear4", offset_x_A=3.0)
    body = handler.get(
        id="clear4", view="clearance", args={"a": "shell", "b": "rotor"}
    ).body
    assert "interpenetrating" in body
    m = re.search(r"under: <0 Å (\d+)", body)
    assert m and int(m.group(1)) > 0, body


def test_pocket_count_is_one_at_the_touching_side(
    handler: SeHandler,
) -> None:
    """Default threshold flags the whole interface (one pocket); a tight
    ``pocket_gap_A`` still gives one pocket, centred on the +x wedge the
    offset pushed into contact — the metering number at the loading
    angle."""
    _put_valve(handler, "clear5", offset_x_A=2.0)
    body = handler.get(
        id="clear5", view="clearance", args={"a": "shell", "b": "rotor"}
    ).body
    assert "pockets (nearest cross-block gap < 3.5 Å): 1" in body

    tight = handler.get(
        id="clear5",
        view="clearance",
        args={"a": "shell", "b": "rotor", "pocket_gap_A": 1.0},
    ).body
    assert "pockets (nearest cross-block gap < 1 Å): 1" in tight
    rows = _toon_rows(tight, "pocket")
    assert len(rows) == 1, tight
    theta = float(rows[0]["θ"].rstrip("°"))
    assert theta < 45.0 or theta > 315.0, rows


def test_mixed_cad_atomic_digest_still_renders(
    handler: SeHandler,
) -> None:
    """The all-pairs digest routes each CONNECTS pair by binding: the
    atomic pair through vdW, the envelope pair through the cad kernel —
    worst gap first, mixed on one table."""
    ops: list[dict[str, Any]] = [
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE24, "terminate": "none"},
            "name": "shell",
        },
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE12, "terminate": "none"},
            "name": "rotor",
        },
        {
            "op": "add_block",
            "name": "knob",
            "envelope": "sphere:r1e-09",
            "pose": "[0 0 5e-09] m",
        },
        {"op": "add_port", "block": "knob", "name": "p1"},
        {"op": "connect", "a": "shell.in", "b": "rotor.in"},
        {"op": "connect", "a": "knob.p1", "b": "shell.out"},
    ]
    handler.put(id="clear6", text=json.dumps({"ops": ops}))
    body = handler.get(id="clear6", view="clearance").body
    rows = _toon_rows(body, "gap")
    assert len(rows) == 2, body
    pairs = {frozenset((r["a"], r["b"])) for r in rows}
    assert {"knob", "shell"} in pairs and {"shell", "rotor"} in pairs


def test_sweep_on_a_non_atomic_pair_is_rejected(handler: SeHandler) -> None:
    ops = [
        {"op": "add_block", "name": "a", "envelope": "sphere:r1e-09"},
        {"op": "add_block", "name": "b", "envelope": "sphere:r1e-09"},
    ]
    handler.put(id="clear7", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="sweep.*atomic pair"):
        handler.get(
            id="clear7",
            view="clearance",
            args={"a": "a", "b": "b", "sweep": {"step_deg": 15}},
        )


def test_sweep_writes_no_pose(handler: SeHandler, store: Store) -> None:
    """The sweep is a read over a rotated cloud copy — the stored pose is
    byte-identical afterwards."""
    _put_valve(handler, "clear8", offset_x_A=2.0)
    ref = store.get_ref(kind="se", id="clear8")
    assert ref is not None
    before = persist.load_tree(store, ref.id).blocks["rotor"]
    handler.get(
        id="clear8",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "sweep": {"step_deg": 15, "range_deg": 360},
        },
    )
    after = persist.load_tree(store, ref.id).blocks["rotor"]
    assert after.pose == before.pose and after.rot == before.rot
    assert after.local_pose == before.local_pose


# ── gap-field / sweep-angle unit seams ─────────────────────────────────


def test_gap_nearest_partner_is_by_gap_not_centre() -> None:
    """Mixed radii flip the minimizer: the H is centre-nearest (3.0 Å)
    but the C half an Ångström farther has the tighter vdW gap
    (3.35−3.40 = −0.05 vs 3.0−2.90 = +0.10). A centre-nearest partner
    would report +0.10 and miss the interpenetration entirely."""
    cloud_a = AtomCloud(
        name="a",
        labels=["aC1"],
        elements=["C"],
        pts_A=np.array([[0.0, 0.0, 0.0]]),
        radii_A=np.array([VDW_RADII_A["C"]]),
    )
    cloud_b = AtomCloud(
        name="b",
        labels=["bH1", "bC1"],
        elements=["H", "C"],
        pts_A=np.array([[3.0, 0.0, 0.0], [3.35, 0.0, 0.0]]),
        radii_A=np.array([VDW_RADII_A["H"], VDW_RADII_A["C"]]),
    )
    field = se_atomic_clearance.gap_field(cloud_a, cloud_b)
    assert field.near_a[0] == 1  # the C, not the centre-nearest H
    assert field.gaps_a[0] == pytest.approx(-0.05)
    assert field.near_b[1] == 0
    assert field.gaps_b[1] == pytest.approx(-0.05)


def test_sweep_angles_samples_every_step_below_range() -> None:
    """floor, not round: a 150° step over 360° still lands 300°; a partial
    range keeps both the step grid and its endpoint; a full turn samples
    [0, 360) only."""
    assert se_atomic_clearance.sweep_angles(150, 360) == [0.0, 150.0, 300.0]
    assert se_atomic_clearance.sweep_angles(100, 140) == [0.0, 100.0, 140.0]
    assert se_atomic_clearance.sweep_angles(250, 360) == [0.0, 250.0]
    assert se_atomic_clearance.sweep_angles(15, 360) == [k * 15.0 for k in range(24)]
    assert se_atomic_clearance.sweep_angles(90, 360) == [0.0, 90.0, 180.0, 270.0]
    assert se_atomic_clearance.sweep_angles(15, 30) == [0.0, 15.0, 30.0]


def test_h_terminated_tube_min_gap_matches_brute_force(
    handler: SeHandler, store: Store
) -> None:
    """``terminate: 'H'`` mixes radii on the inner tube — with a >0 host
    radius spread the gap-minimal partner is no longer the centre-
    nearest atom, so the reported minimum must equal the true all-pairs
    vdW minimum, not the centre-nearest surrogate."""
    ops: list[dict[str, Any]] = [
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE24, "terminate": "none"},
            "name": "shell",
        },
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _TUBE12, "terminate": "H"},
            "name": "rotor",
        },
        {
            "op": "set_pose",
            "block": "rotor",
            "pose": "[2e-10 0 0] m",
        },
    ]
    handler.put(id="clear9", text=json.dumps({"ops": ops}))
    ref = store.get_ref(kind="se", id="clear9")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    cloud_shell = se_atomic_clearance.atom_cloud(
        tree.blocks["shell"], _scene(store, "clear9-shell")
    )
    cloud_rotor = se_atomic_clearance.atom_cloud(
        tree.blocks["rotor"], _scene(store, "clear9-rotor")
    )
    assert "H" in cloud_rotor.elements
    report = se_atomic_clearance.gap_report(
        se_atomic_clearance.gap_field(cloud_shell, cloud_rotor)
    )
    d = np.linalg.norm(
        cloud_shell.pts_A[:, None, :] - cloud_rotor.pts_A[None, :, :], axis=2
    )
    g = d - cloud_shell.radii_A[:, None] - cloud_rotor.radii_A[None, :]
    assert report.min_gap_A == pytest.approx(float(g.min()), abs=1e-6)
    assert g[report.worst_a, report.worst_b] == pytest.approx(
        report.min_gap_A, abs=1e-6
    )


def test_cad_only_digest_rows_say_envelope(handler: SeHandler) -> None:
    """Regression: a design with no structure-bound blocks renders the
    digest exactly on the envelope path — no scenes loaded, every row
    ``envelope``, no rungs line, the envelope verdict vocabulary."""
    ops = [
        {"op": "add_block", "name": "hub", "envelope": "cyl:r1e-09h1e-09"},
        {
            "op": "add_block",
            "name": "wheel",
            "envelope": "cyl:r3e-09h1e-09",
            "pose": "[0 0 2e-09] m",
        },
        {"op": "add_port", "block": "hub", "name": "p"},
        {"op": "add_port", "block": "wheel", "name": "p"},
        {"op": "connect", "a": "hub.p", "b": "wheel.p"},
    ]
    handler.put(id="clear10", text=json.dumps({"ops": ops}))
    body = handler.get(id="clear10", view="clearance").body
    rows = _toon_rows(body, "gap")
    assert len(rows) == 1 and rows[0]["via"] == "envelope"
    assert rows[0]["tag"] in ("clear", "≈ touching (within resolution)", "interference")
    assert "rungs:" not in body and "atoms" not in rows[0]["via"]


def test_dangling_binding_pair_is_skipped_with_a_note(store: Store) -> None:
    """A structure-bound pair whose bound design no longer resolves is a
    digest skip note, not an abort — the survey survives it."""
    tree = SeTree()
    apply_ops(
        tree,
        [
            {"op": "add_block", "name": "shell", "envelope": "cyl:r2e-09h1e-09"},
            {"op": "add_block", "name": "rotor", "envelope": "cyl:r1e-09h1e-09"},
            {"op": "add_port", "block": "shell", "name": "p"},
            {"op": "add_port", "block": "rotor", "name": "p"},
            {"op": "connect", "a": "shell.p", "b": "rotor.p"},
        ],
    )
    tree.blocks["shell"].bound_kind = "structure"
    tree.blocks["shell"].bound = "gone-shell"
    tree.blocks["rotor"].bound_kind = "structure"
    tree.blocks["rotor"].bound = "gone-rotor"
    body = _render_clearance_digest(store, tree)
    assert "skipped" in body
    assert "structure-bound" in body and "can't measure" in body
