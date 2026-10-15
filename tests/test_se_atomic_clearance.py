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
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis.structure.scene import Scene as StructScene
from precis_se import persist
from precis_se.atomic import clearance as se_atomic_clearance
from precis_se.atomic import sites as se_atomic_sites
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


def test_bonded_pair_is_tagged_bonded_not_interpenetrating(
    handler: SeHandler,
) -> None:
    """gr488903: a ``kind='bond'`` connect (what ``join`` writes for a
    fused seam) puts atoms at bond length, so a negative vdW gap across
    it is the bond, not a clash. The digest tags the row ``bonded`` and
    ranks it after the real interferences; an ``interaction`` connect
    over the same geometry still reads ``interpenetrating``."""
    _put_valve(handler, "clear11", offset_x_A=3.0)
    handler.edit(
        id="clear11",
        ops=[{"op": "connect", "a": "shell.in", "b": "rotor.in", "kind": "bond"}],
    )
    body = handler.get(id="clear11", view="clearance").body
    (row,) = _toon_rows(body, "gap")
    assert row["tag"] == "bonded", body
    assert "bond length" in body  # the legend says what the tag means
    pair = handler.get(
        id="clear11", view="clearance", args={"a": "shell", "b": "rotor"}
    ).body
    assert "kind='bond' connect" in pair, pair

    _put_valve(handler, "clear12", offset_x_A=3.0)
    handler.edit(
        id="clear12",
        ops=[
            {
                "op": "connect",
                "a": "shell.in",
                "b": "rotor.in",
                "kind": "interaction",
            }
        ],
    )
    (row,) = _toon_rows(handler.get(id="clear12", view="clearance").body, "gap")
    assert row["tag"] == "interpenetrating"


def test_nested_pair_is_tagged_nested(store: Store, handler: SeHandler) -> None:
    """gr488903: a join composite holds its parts' atoms, so composite vs
    its own descendant overlaps by construction — ``nested``, never a
    clash, and sorted after the measured rows."""
    _put_valve(handler, "clear13", offset_x_A=3.0)
    ref = store.get_ref(kind="se", id="clear13")
    assert ref is not None
    tree = persist.load_tree(store, int(ref.id))
    tree.blocks["rotor"].parent = "shell"
    apply_ops(
        tree,
        [
            {
                "op": "connect",
                "a": "shell.in",
                "b": "rotor.in",
                "kind": "interaction",
            }
        ],
    )
    (row,) = _toon_rows(_render_clearance_digest(store, tree), "gap")
    assert row["tag"] == "nested"


# ── view='sites' — the attachment-site enumerator ──────────────────────


def _site_rows(body: str) -> list[dict[str, str]]:
    return _toon_rows(body, "ordinal")


def _counts(body: str) -> dict[tuple[str, str], int]:
    """The summary table — the block whose header carries 'count' (the
    site table itself has a 'ring' column but no 'count')."""
    for table in body.split("\n\n"):
        lines = [ln for ln in table.splitlines() if ln.strip()]
        if not lines:
            continue
        header = lines[0].lstrip("{").rstrip("}").split("\t")
        if "count" not in header or "ring" not in header:
            continue
        out: dict[tuple[str, str], int] = {}
        for ln in lines[1:]:
            cols = ln.split("\t")
            row = dict(zip(header, cols))
            out[(row["ring"], row["face"])] = int(row["count"])
        return out
    return {}


def test_sites_enumerates_rim_and_ring_carbons(
    handler: SeHandler,
) -> None:
    """The (12,0) zigzag tube: every rim carbon at the open ends is
    open-edge (degree-2 dangling bond, axial free direction); every
    interior carbon is 6-ring with both wall faces."""
    _put_valve(handler, "sites1")
    body = handler.get(
        id="sites1", view="sites", args={"block": "rotor", "limit": 0}
    ).body
    counts = _counts(body)
    assert counts.get(("open-edge", "axial"), 0) > 0
    assert ("open-edge", "6-ring") not in counts
    # interior atoms show both wall faces
    assert counts.get(("6-ring", "out"), 0) == counts.get(("6-ring", "in"), 0)
    assert counts.get(("6-ring", "out"), 0) > 0
    # the two ends each carry the (12,0) rim — 24 open-edge carbons
    rim = [r for r in _site_rows(body) if r["ring"] == "open-edge"]
    assert len(rim) == 24


def test_sites_face_and_exposure_match_the_geometry(
    handler: SeHandler,
) -> None:
    """The offset rotor's outward-facing wall sites on the +x side are
    less exposed than its axial rim sites — the probe sphere meets the
    shell's atoms — and wall sites come in 'in'/'out' pairs per carbon."""
    _put_valve(handler, "sites2", offset_x_A=2.0)
    body = handler.get(
        id="sites2", view="sites", args={"a": "shell", "b": "rotor", "limit": 0}
    ).body
    rows = _site_rows(body)
    assert rows, body
    # pair view: only sites that face the partner — rotor 'out' and
    # shell 'in' rows, no axial or far-side rows.
    faces = {(r["block"], r["face"]) for r in rows}
    assert faces <= {("rotor", "out"), ("shell", "in")}
    facing_shell = min(float(r["exposure"]) for r in rows if r["block"] == "rotor")
    # rim sites face the open ends, not the partner — read them off the
    # single-block view; an axial dangling bond is wider open than a wall
    # site pressed against the shell.
    single = handler.get(
        id="sites2", view="sites", args={"block": "rotor", "limit": 0}
    ).body
    open_end = max(
        float(r["exposure"]) for r in _site_rows(single) if r["face"] == "axial"
    )
    assert facing_shell < open_end


def test_sites_pair_view_is_facing_only(handler: SeHandler) -> None:
    """Coaxial: every rotor 'out' site and shell 'in' site faces the
    partner within the default pocket gap; nothing axial shows."""
    _put_valve(handler, "sites3")
    body = handler.get(
        id="sites3", view="sites", args={"a": "shell", "b": "rotor", "limit": 0}
    ).body
    rows = _site_rows(body)
    assert rows
    assert all(r["face"] in ("out", "in") for r in rows)
    assert {r["block"] for r in rows} == {"shell", "rotor"}


def test_complementarity_verdict_names_two_arcs(
    handler: SeHandler, store: Store
) -> None:
    """CH3-decorated rotor orbiting inside a shell that is OH on one
    θ-half and CH3 on the other: the contacted arc alternates through
    the sweep, so the polarity term flips and the verdict names both
    arcs. STUB — a polarity flip, not an energetic one."""
    _put_valve(handler, "sites4", offset_x_A=2.0)
    ref = store.get_ref(kind="se", id="sites4")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    scene = _scene(store, "sites4-shell")
    cloud = se_atomic_clearance.atom_cloud(tree.blocks["shell"], scene)
    # shell carbons split by azimuth into two decorated halves
    oh_sites = [
        i
        for i, p in enumerate(cloud.pts_A)
        if cloud.elements[i] == "C"
        and math.degrees(math.atan2(p[1], p[0])) % 360 >= 180
    ]
    ch3_sites = [
        i
        for i, p in enumerate(cloud.pts_A)
        if cloud.elements[i] == "C" and math.degrees(math.atan2(p[1], p[0])) % 360 < 180
    ]
    assert oh_sites and ch3_sites
    lining = {
        "shell": (
            [{"site": i, "group": "OH"} for i in oh_sites]
            + [{"site": i, "group": "CH3"} for i in ch3_sites]
        ),
        "rotor": [
            {"site": "ring:6-ring", "group": "CH3"},
            {"site": "ring:open-edge", "group": "CH3"},
        ],
    }
    body = handler.get(
        id="sites4",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "sweep": {"axis": "a", "step_deg": 60},
            # 1.5 Å keeps the pocket at the touching arc — at the default
            # 3.5 the far side flags too and the pocket spans every arc.
            "pocket_gap_A": 1.5,
            "cargo_A3": 50,
            "lining": lining,
        },
    ).body
    assert "STUB" in body
    # arcs are SWEEP-ANGLE ranges [t1°–t2°], not pocket azimuths
    arcs = re.search(
        r"admits on arcs? \[([\d.]+)°–([\d.]+)°\].*rejects on arcs? \[([\d.]+)°–([\d.]+)°\]",
        body,
    )
    assert arcs, body
    admit_lo, admit_hi, reject_lo, reject_hi = (float(v) for v in arcs.groups())
    # the apolar CH3 half admits, the polar OH half rejects — each arc a
    # contiguous run of sweep angles (a wrap crossing 360° reads >360).
    assert admit_lo < admit_hi
    assert reject_lo <= 180.0 and reject_hi >= 240.0
    # still read-only
    after = persist.load_tree(store, ref.id).blocks["rotor"]
    assert after.pose == tree.blocks["rotor"].pose


def test_lining_without_sweep_still_prints_verdict_and_no_pose_header(
    handler: SeHandler,
) -> None:
    """`lining` alone implies the default sweep — the clearance verdict
    and the read-only header print even without args.sweep."""
    _put_valve(handler, "sites5")
    body = handler.get(
        id="sites5",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "lining": {"rotor": [{"site": 0, "group": "OH"}]},
        },
    ).body
    assert "read-only, no pose written" in body
    assert re.search(r"verdict: (free|touching|interpenetrating)", body)
    assert "lining (STUB" in body


def test_complementarity_no_rectification_branch(handler: SeHandler) -> None:
    """A uniform OH lining on both blocks: every decorated contact is
    compatible, the polarity term never changes sign → the no-
    rectification verdict."""
    _put_valve(handler, "sites6")
    body = handler.get(
        id="sites6",
        view="clearance",
        args={
            "a": "shell",
            "b": "rotor",
            "sweep": {"axis": "b", "step_deg": 60},
            "pocket_gap_A": 1.5,
            "lining": {
                "shell": [{"site": "ring:6-ring", "group": "OH"}],
                "rotor": [{"site": "ring:6-ring", "group": "OH"}],
            },
        },
    ).body
    assert "no rectification" in body


def test_sites_face_filter(handler: SeHandler) -> None:
    """face='out' lists only outward sites, face='in' only inward —
    each a subset of the unfiltered table."""
    _put_valve(handler, "sites7")
    for want in ("out", "in"):
        body = handler.get(
            id="sites7",
            view="sites",
            args={"block": "rotor", "face": want},
        ).body
        rows = _site_rows(body)
        assert rows and all(r["face"] == want for r in rows)
        assert all(r["ring"] != "open-edge" for r in rows)  # rim sites are axial


def test_ring_defects_classify() -> None:
    """Hand-built C–C graph: a degree-3 carbon on a pure 5-ring reads
    '5-ring', one on a 7-ring '7-ring'; its pendant leaves are
    open-edge and an O-substituted carbon is 'substituted'."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom, Bond

    for ring_size, want in ((5, "5-ring"), (7, "7-ring")):
        n = ring_size
        labels = [f"aC{i + 1}" for i in range(2 * n)]
        # ring carbons on a circle, one pendant C leaf each → every ring
        # atom has exactly three C neighbours and closes only the n-ring.
        pts = np.array(
            [
                *[
                    [
                        3.0 * math.cos(2 * math.pi * i / n),
                        3.0 * math.sin(2 * math.pi * i / n),
                        0.0,
                    ]
                    for i in range(n)
                ],
                *[
                    [
                        4.6 * math.cos(2 * math.pi * i / n),
                        4.6 * math.sin(2 * math.pi * i / n),
                        0.0,
                    ]
                    for i in range(n)
                ],
            ]
        )
        atoms = {
            l: Atom(label=l, element="C", frac=pts[i] / 50.0)
            for i, l in enumerate(labels)
        }
        bonds = [
            *[Bond(i=labels[i], j=labels[(i + 1) % n], order=1.33) for i in range(n)],
            *[Bond(i=labels[i], j=labels[n + i], order=1.33) for i in range(n)],
        ]
        scene = StructScene(
            cell=Cell(lattice=np.eye(3) * 50.0, pbc=(False, False, False)),
            atoms=atoms,
            bonds=bonds,
        )
        cloud = AtomCloud(
            name="toy",
            labels=labels,
            elements=["C"] * (2 * n),
            pts_A=pts,
            radii_A=np.full(2 * n, VDW_RADII_A["C"]),
        )
        adj, unsat = se_atomic_sites._bond_graph(scene, labels, pts)
        classes = se_atomic_sites.ring_classes(cloud, adj, unsat)
        assert classes[:n] == [want] * n
        assert classes[n:] == ["open-edge"] * n


def test_sites_is_read_only(handler: SeHandler, store: Store) -> None:
    """Single and pair `view='sites'` calls leave the design's pose, rot
    and the bound structure's atoms untouched."""
    _put_valve(handler, "sites8", offset_x_A=2.0)
    ref = store.get_ref(kind="se", id="sites8")
    assert ref is not None
    before_tree = persist.load_tree(store, ref.id)
    before_scene = _carts_A(_scene(store, "sites8-rotor"))
    handler.get(id="sites8", view="sites", args={"block": "rotor"})
    handler.get(id="sites8", view="sites", args={"a": "shell", "b": "rotor"})
    after_tree = persist.load_tree(store, ref.id)
    assert after_tree.blocks["rotor"].pose == before_tree.blocks["rotor"].pose
    assert after_tree.blocks["rotor"].rot == before_tree.blocks["rotor"].rot
    assert np.allclose(_carts_A(_scene(store, "sites8-rotor")), before_scene)


def test_fin_graft_atoms_classify_sp3_not_sites(handler: SeHandler) -> None:
    """A fin-sp3-z scene's graft junctions count under 'sp3' in the
    summary and never appear as site rows — no free valence."""
    ops = [
        {
            "op": "generate",
            "generator": "hexfold_scene",
            "params": {
                "tube": [6, 4],
                "features": [
                    {"name": "fin", "type": "fin-sp3-z", "side": "out", "rows": 3}
                ],
            },
            "name": "finwall",
        }
    ]
    handler.put(id="sites9", text=json.dumps({"ops": ops}))
    body = handler.get(id="sites9", view="sites", args={"block": "finwall"}).body
    counts = _counts(body)
    assert counts.get(("sp3", "—"), 0) > 0, body
    assert all(r["ring"] != "sp3" for r in _site_rows(body))


def test_bad_lining_fails_before_the_sweep(
    handler: SeHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad lining (unknown group) raises BadInput BEFORE any sweep
    geometry runs — the sweep must never fire on a rejected lining."""
    _put_valve(handler, "sites10")

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise AssertionError(
            "run_sweep ran — the lining should have been rejected first"
        )

    monkeypatch.setattr(se_atomic_clearance, "run_sweep", _boom)
    with pytest.raises(BadInput, match="group"):
        handler.get(
            id="sites10",
            view="clearance",
            args={
                "a": "shell",
                "b": "rotor",
                "lining": {"rotor": [{"site": 0, "group": "bogus"}]},
            },
        )


def test_sites_rejects_a_non_atomic_block(handler: SeHandler) -> None:
    ops = [{"op": "add_block", "name": "box", "envelope": "sphere:r1e-09"}]
    handler.put(id="sites11", text=json.dumps({"ops": ops}))
    with pytest.raises(BadInput, match="structure-bound"):
        handler.get(id="sites11", view="sites", args={"block": "box"})


# ── gr488904 — sites correctness and size ──────────────────────────────

_PILLBOX = """\
hexfold 0.2
origin a
a: tube(12, 0, len=4)
b: cap(12, 0)
c: cap(12, 0)
a.out --fuse k=0--> b.in
a.in --fuse k=0--> c.in
"""


def _put_pillbox(handler: SeHandler, slug: str) -> None:
    ops = [
        {
            "op": "generate",
            "generator": "hexfold",
            "params": {"spec": _PILLBOX, "terminate": "none"},
            "name": "pill",
        }
    ]
    handler.put(id=slug, text=json.dumps({"ops": ops}))


def test_lining_ring_selector_matching_nothing_is_refused(
    handler: SeHandler,
) -> None:
    """(a) a ring-class selector that matches no carbon was a silent
    no-op; it now names the classes the block does have and that the
    class is the SMALLEST ring a carbon closes."""
    _put_valve(handler, "sites12")
    with pytest.raises(BadInput, match="(?i)smallest ring") as exc:
        handler.get(
            id="sites12",
            view="clearance",
            args={
                "a": "shell",
                "b": "rotor",
                "lining": {"rotor": [{"site": "ring:5-ring", "group": "OH"}]},
            },
        )
    assert "6-ring" in str(exc.value)


def test_closed_cage_lists_every_carbon_and_seals_its_interior(
    handler: SeHandler,
) -> None:
    """(b) every carbon of a capped tube is a site — the cap carbons whose
    wall normal runs along the axis used to be dropped; (c) a closed cage's
    'in' face is sealed: exposure 0 and a header note."""
    _put_pillbox(handler, "sites13")
    body = handler.get(
        id="sites13", view="sites", args={"block": "pill", "limit": 0}
    ).body
    rows = _site_rows(body)
    m = re.search(r"(\d+) carbon", body)
    assert m, body[:400]
    n_carbon = int(m.group(1))
    assert {int(r["ordinal"]) for r in rows} == set(range(n_carbon)), body[:400]
    assert sum(_counts(body).values()) == 2 * n_carbon
    inside = [r for r in rows if r["face"] == "in"]
    assert inside and all(float(r["exposure"]) == 0.0 for r in inside)
    assert "closed cage" in body


def test_sites_table_is_capped_by_limit(handler: SeHandler) -> None:
    """(d) the table shows the top ``limit`` rows by exposure (default
    50) and says how many it held back; ``limit=0`` lists all; the
    summary always counts every row."""
    _put_valve(handler, "sites14")
    full = handler.get(
        id="sites14", view="sites", args={"block": "shell", "limit": 0}
    ).body
    total = len(_site_rows(full))
    assert total > 50
    capped = handler.get(id="sites14", view="sites", args={"block": "shell"}).body
    assert len(_site_rows(capped)) == 50
    assert f"showing 50 of {total}" in capped
    assert _counts(capped) == _counts(full)
    with pytest.raises(BadInput, match="limit"):
        handler.get(id="sites14", view="sites", args={"block": "shell", "limit": -1})


def test_sweep_takes_united_angles(handler: SeHandler) -> None:
    """gr488905: se's units-carry rule — ``step``/``range`` take united
    angles ('30 deg', 'rad'); the bare-degree ``step_deg``/``range_deg``
    stay for existing callers. A bare ``step`` and naming both spellings
    of one quantity are refused."""
    _put_valve(handler, "sweep1")
    pair = {"a": "shell", "b": "rotor"}
    legacy = handler.get(
        id="sweep1",
        view="clearance",
        args={**pair, "sweep": {"step_deg": 90, "range_deg": 180}},
    ).body
    united = handler.get(
        id="sweep1",
        view="clearance",
        args={**pair, "sweep": {"step": "90 deg", "range": "180 deg"}},
    ).body
    assert _toon_rows(united, "angle") == _toon_rows(legacy, "angle")
    assert len(_toon_rows(united, "angle")) == 3  # 0, 90, 180: a part-turn
    radians = handler.get(
        id="sweep1",
        view="clearance",
        args={**pair, "sweep": {"step": f"{math.pi!r} rad"}},
    ).body
    assert [r["angle"] for r in _toon_rows(radians, "angle")] == ["0°", "180°"]
    with pytest.raises(BadInput, match="step"):
        handler.get(id="sweep1", view="clearance", args={**pair, "sweep": {"step": 30}})
    with pytest.raises(BadInput, match="both"):
        handler.get(
            id="sweep1",
            view="clearance",
            args={**pair, "sweep": {"step": "30 deg", "step_deg": 30}},
        )


def test_atomic_view_errors_point_at_the_atomic_skill(handler: SeHandler) -> None:
    """gr488906: sites/lining/sweep args are documented in
    precis-se-atomic-help, so their BadInputs say so first — the runtime's
    generic precis-se-help hint only follows."""
    _put_valve(handler, "hint1")
    hint = "get(kind='skill', id='precis-se-atomic-help')"
    for args, view in (
        ({"block": "rotor", "face": "sideways"}, "sites"),
        ({"a": "shell", "b": "rotor", "sweep": {"bogus": 1}}, "clearance"),
        (
            {"a": "shell", "b": "rotor", "lining": {"rotor": [{"site": -1}]}},
            "clearance",
        ),
    ):
        with pytest.raises(BadInput) as exc:
            handler.get(id="hint1", view=view, args=args)
        nxt = exc.value.next
        first = nxt if isinstance(nxt, str) else (nxt[0] if nxt else None)
        assert first == hint, (view, args, nxt)


def test_se_help_views_list_names_the_atomic_views() -> None:
    text = (
        Path(__file__).resolve().parents[1] / "src/precis/data/skills/precis-se-help.md"
    ).read_text(encoding="utf-8")
    views = next(ln for ln in text.splitlines() if ln.startswith("`tree · block"))
    block = text[text.index(views) :].split("\n\n")[0]
    assert "sites" in block and "precis-se-atomic-help" in block


def test_sites_face_filter_accepts_axial(handler: SeHandler) -> None:
    """(e) the table prints ``axial`` faces, so the filter takes it."""
    _put_valve(handler, "sites15")
    body = handler.get(
        id="sites15", view="sites", args={"block": "rotor", "face": "axial"}
    ).body
    rows = _site_rows(body)
    assert rows and {r["face"] for r in rows} == {"axial"}
