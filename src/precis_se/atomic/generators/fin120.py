"""Public 120-degree seam tube entry, through ``hexfold_scene`` only.

The Y's equal-120 sp2 k3 seam run along a tube axis (:mod:`hexfold.fin120`):
the strip is one half-sheet, the two tube-wall halves are the other two,
closed on the far side by a zigzag fuse. One tethered stick pass: the seam
atoms and their three neighbours pinned at the authored 120-degree
registration (as the Y), the strip tethered to its half-plane, the wall
free -- so the cross section the wall settles into (a teardrop around an
outward strip, a heart-shaped notch around an inward one) is the
relaxer's answer to the seam, not a tether's. No surface-of-revolution
receipt exists for it, so ``surface_deviation`` stays unavailable.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.fin import SIDES
from hexfold.fin120 import seam_tube
from hexfold.report import Finding, Report, Severity
from hexfold.stick import _angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import _block_from_net, terminate_mode

FEATURE_TYPE = "fin-k3-120-z"
DEFAULT_ROWS = 4


def _normalize(params: dict[str, Any]) -> tuple[int, int, int, str, str, float]:
    if set(params) - {"tube", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "seam-tube scene accepts only tube, features, k_tether, terminate; "
            "no sheet, extra or mixed feet"
        )
    tube = params.get("tube")
    if (
        not isinstance(tube, list | tuple)
        or len(tube) != 2
        or any(isinstance(v, bool) or type(v) is not int for v in tube)
        or not 4 <= tube[0] <= 40
        or not 2 <= tube[1] <= 60
    ):
        raise GeneratorError(
            "seam-tube tube must be [rows, periods]: row pairs per wall half "
            "(the closed wall is about the (rows,rows) armchair family) in "
            "[4,40] and integer seam periods in [2,60]"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"seam-tube scene requires exactly one {FEATURE_TYPE} feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "side")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"seam-tube feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - {*required, "rows"}))
    if extra:
        raise GeneratorError(
            "seam-tube feature takes only name, type, side, rows; "
            f"unexpected: {', '.join(extra)}"
        )
    if feature["type"] != FEATURE_TYPE:
        raise GeneratorError(f"seam-tube feature type must be {FEATURE_TYPE!r}")
    side = feature["side"]
    if side not in SIDES:
        raise GeneratorError(f"seam-tube side must be one of {SIDES}; got {side!r}")
    rows = feature.get("rows", DEFAULT_ROWS)
    if isinstance(rows, bool) or type(rows) is not int or not 2 <= rows <= 12:
        raise GeneratorError("seam-tube strip rows must be an integer in [2,12]")
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("seam-tube feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "seam-tube k_tether must be finite and positive (tethered preview only)"
        )
    return tube[0], tube[1], rows, side, name, float(k)


def build_fin120(params: dict[str, Any]) -> GeneratedBlock:
    wall_rows, periods, strip_rows, side, name, k = _normalize(params)
    try:
        net, tube = seam_tube(periods, wall_rows, strip_rows, side)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    seed = tube.coords
    strip_idx = np.asarray(tube.strip_atoms, dtype=np.int64)

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # only the strip is tethered (to its half-plane); wall and seam
        # atoms get a zero-residual foot, so the wall is free to close
        foot = pos.copy()
        normal = np.zeros_like(pos)
        foot[strip_idx, 1] = 0.0
        normal[strip_idx, 1] = 1.0
        return foot, normal

    movable = np.ones(len(net.atoms), dtype=float)
    movable[list(tube.pinned)] = 0
    raw, force = stick_relax_pinned(
        seed,
        np.asarray([(i, j) for i, j, _ in net.bonds], dtype=np.int64),
        np.full(len(net.bonds), net.lattice.sigma_A),
        np.asarray(_angle_springs(net), dtype=float),
        net.lattice.sigma_A,
        movable=movable,
        tether=tether,
        k_tether=k,
    )
    pos = np.asarray(raw, dtype=float)
    if not np.isfinite(pos).all() or not math.isfinite(force):
        raise GeneratorError(
            "seam-tube tethered relaxation returned nonfinite coordinates/force"
        )
    adjacency: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _ in net.bonds:
        adjacency[i].append(j)
        adjacency[j].append(i)
    angle_dev = 0.0
    triple = 0.0
    for atom in tube.seam_atoms:
        neighbors = adjacency[atom]
        if len(neighbors) != 3:
            raise GeneratorError("seam-tube seam atom is not degree-three")
        vectors = pos[neighbors] - pos[atom]
        lengths = np.linalg.norm(vectors, axis=1)
        if np.any(lengths == 0):
            raise GeneratorError("seam-tube relaxed seam contains a zero-length bond")
        vectors /= lengths[:, None]
        triple = max(triple, abs(float(np.linalg.det(vectors))))
        angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
        angle_dev = max(
            angle_dev, float(np.max(np.abs(angles[np.triu_indices(3, 1)] - 120)))
        )
    # the cross section the wall settled into, about the wall's own axis
    wall_idx = np.asarray(tube.wall_a_atoms + tube.wall_b_atoms, dtype=np.int64)
    centre = pos[wall_idx, :2].mean(axis=0)
    rho = np.linalg.norm(pos[wall_idx, :2] - centre, axis=1)
    seam_rho = float(
        np.linalg.norm(pos[list(tube.seam_atoms), :2] - centre, axis=1).mean()
    )
    far = [p for p, _q in tube.fuse_pairs]
    far_rho = float(np.linalg.norm(pos[far, :2] - centre, axis=1).mean())
    eight = [list(r) for r in net.rings if len(r) == 8]
    bond_lengths = np.asarray(
        [np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds]
    )
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{len(eight)} eight-cycle faces along {periods} straight seam "
                f"periods; {len(tube.fuse_rings)} six-cycles close the far side",
                where=name,
                data=(
                    ("rings", eight),
                    ("periods", periods),
                    ("seam_atoms", list(tube.seam_atoms)),
                    ("fuse_pairs", [list(p) for p in tube.fuse_pairs]),
                ),
            ),
            Finding(
                "seam.geometry",
                Severity.INFO,
                "Measured tethered degree-three sp2 seam; angle and coplanarity "
                "residuals are preview evidence",
                where=name,
                data=(
                    ("degree", 3),
                    ("hybridisation", "sp2"),
                    ("angle_deviation_max_deg", angle_dev),
                    ("normalized_triple_max", triple),
                    ("bond_min_A", float(bond_lengths.min())),
                    ("bond_max_A", float(bond_lengths.max())),
                    (
                        "joint_constraint",
                        "authored seam and immediate neighbours pinned",
                    ),
                ),
            ),
            Finding(
                "tube.cross_section",
                Severity.INFO,
                f"wall settled {rho.min():.2f}-{rho.max():.2f} A from its axis "
                f"(seed circle {tube.radius_A:.2f} A); seam line at "
                f"{seam_rho:.2f} A, far side at {far_rho:.2f} A: the "
                f"{'teardrop' if side == 'out' else 'heart-shaped notch'} the "
                "120-degree seam makes of a free wall",
                where=name,
                data=(
                    ("side", side),
                    ("seed_radius_A", tube.radius_A),
                    ("seed_seam_radius_A", tube.seam_radius_A),
                    ("wall_rho_min_A", float(rho.min())),
                    ("wall_rho_max_A", float(rho.max())),
                    ("wall_rho_mean_A", float(rho.mean())),
                    ("seam_rho_A", seam_rho),
                    ("far_rho_A", far_rho),
                    ("wall_tether", "none"),
                ),
            ),
            Finding(
                "surface.target.unavailable",
                Severity.INFO,
                "A seam-cusped tube with a half-plane is unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    normalized = {
        "tube": [wall_rows, periods],
        "features": [
            {"name": name, "type": FEATURE_TYPE, "side": side, "rows": strip_rows}
        ],
        "k_tether": k,
    }
    block = _block_from_net(
        net,
        pos,
        spec="",
        terminate=terminate_mode(params),
        report=Report(tuple(findings)).sorted(),
        fidelity="stick",
        extra_topology={
            "scene": normalized,
            "plan": {
                "construction": (
                    f"equal-120 k3 seam along the axis: {strip_rows}-row-pair strip "
                    f"{side}ward and two wall halves of {wall_rows} row pairs, closed "
                    f"on the far side by a zigzag fuse; {periods} seam periods"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(tube.pinned),
                "joint_constraint": "seam and immediate neighbours fixed at authored analytic registration; wall free",
                "seed_radius_A": tube.radius_A,
                "seed_seam_radius_A": tube.seam_radius_A,
                "strip_height_A": tube.strip_height_A,
                "plane_distance_mean_A": float(residuals[strip_idx].mean()),
                "plane_distance_max_A": float(residuals[strip_idx].max()),
                "surface_deviation": "unavailable: a seam-cusped tube is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" equal-120 k3 seam along a tube axis, {side}ward strip, open end rims, "
            "one tethered stick pass with seam and neighbours pinned and the wall "
            "free; replay generated.scene params; no stability claim"
        ),
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    return block
