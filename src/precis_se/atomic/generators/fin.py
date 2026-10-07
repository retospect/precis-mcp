"""Public fin-on-tube entry, through ``hexfold_scene`` only.

An armchair tube with a graphene strip grafted along one axial zigzag chain
by one radial sp3 C-C bond per lattice period (:mod:`hexfold.fin`), relaxed
once under the existing tethered stick pass: wall atoms toward the tube's
cylinder, strip atoms toward the strip's half-plane, the grafted pairs
pinned at their authored registration. A cylinder with a half-plane is not
a surface of revolution, so no surface-target receipt is stored and
``surface_deviation`` stays unavailable rather than zero.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.fin import SIDES, fin_tube
from hexfold.lattice import SP3_IDEAL_DEG
from hexfold.report import Finding, Report, Severity
from hexfold.stick import _angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import _block_from_net

FEATURE_TYPE = "fin-sp3-z"
DEFAULT_ROWS = 4


def _normalize(params: dict[str, Any]) -> tuple[int, int, int, str, str, float]:
    if set(params) - {"tube", "features", "k_tether"}:
        raise GeneratorError(
            "fin scene accepts only tube, features, k_tether; no sheet, extra or mixed feet"
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
            "fin tube must be [n, periods]: an armchair (n,n) tube with n in "
            "[4,40] and integer axial periods in [2,60]"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"fin scene requires exactly one {FEATURE_TYPE} feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "side")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"fin feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - {*required, "rows"}))
    if extra:
        raise GeneratorError(
            "fin feature takes only name, type, side, rows; "
            f"unexpected: {', '.join(extra)}"
        )
    if feature["type"] != FEATURE_TYPE:
        raise GeneratorError(f"fin feature type must be {FEATURE_TYPE!r}")
    side = feature["side"]
    if side not in SIDES:
        raise GeneratorError(f"fin side must be one of {SIDES}; got {side!r}")
    rows = feature.get("rows", DEFAULT_ROWS)
    if isinstance(rows, bool) or type(rows) is not int or not 2 <= rows <= 12:
        raise GeneratorError("fin rows must be an integer in [2,12] (row pairs)")
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("fin feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "fin k_tether must be finite and positive (tethered preview only)"
        )
    return tube[0], tube[1], rows, side, name, float(k)


def build_fin(params: dict[str, Any]) -> GeneratedBlock:
    n, periods, rows, side, name, k = _normalize(params)
    try:
        net, graft = fin_tube(n, periods, rows, side)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    seed = graft.coords
    radius = graft.radius_A
    tube_idx = np.asarray(graft.tube_atoms, dtype=np.int64)
    fin_idx = np.asarray(graft.fin_atoms, dtype=np.int64)

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot = pos.copy()
        normal = np.zeros_like(pos)
        xy = pos[tube_idx, :2]
        rho = np.linalg.norm(xy, axis=1)
        direction = np.divide(
            xy, rho[:, None], out=np.zeros_like(xy), where=rho[:, None] > 0
        )
        foot[tube_idx, :2] = direction * radius
        normal[tube_idx, :2] = direction
        foot[fin_idx, 1] = 0.0
        normal[fin_idx, 1] = 1.0
        return foot, normal

    pinned = set(graft.wall_atoms) | set(graft.rim_atoms)
    movable = np.ones(len(net.atoms), dtype=float)
    movable[list(pinned)] = 0
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
            "fin tethered relaxation returned nonfinite coordinates/force"
        )
    adjacency: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _ in net.bonds:
        adjacency[i].append(j)
        adjacency[j].append(i)
    angle_min, angle_max = 360.0, 0.0
    for atom in graft.wall_atoms:
        neighbors = adjacency[atom]
        if len(neighbors) != 4:
            raise GeneratorError("fin graft host is not degree four")
        vectors = pos[neighbors] - pos[atom]
        lengths = np.linalg.norm(vectors, axis=1)
        if np.any(lengths == 0):
            raise GeneratorError("fin relaxed graft contains a zero-length bond")
        vectors /= lengths[:, None]
        angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
        pairs = angles[np.triu_indices(4, 1)]
        angle_min = min(angle_min, float(pairs.min()))
        angle_max = max(angle_max, float(pairs.max()))
    radial = np.asarray(
        [
            np.linalg.norm(pos[w] - pos[r])
            for w, r in zip(graft.wall_atoms, graft.rim_atoms)
        ]
    )
    bond_lengths = np.asarray(
        [np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds]
    )
    grafts = len(graft.wall_atoms)
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "graft.rings",
                Severity.INFO,
                f"{len(graft.graft_rings)} six-cycles closed by {grafts} consecutive "
                "sp3 grafts along one axial zigzag chain",
                where=name,
                data=(
                    ("rings", [list(r) for r in graft.graft_rings]),
                    ("grafts", grafts),
                    ("wall_atoms", list(graft.wall_atoms)),
                    ("rim_atoms", list(graft.rim_atoms)),
                ),
            ),
            Finding(
                "graft.geometry",
                Severity.INFO,
                f"Measured tethered sp3 graft hosts: bond angles {angle_min:.1f}-"
                f"{angle_max:.1f} deg against {SP3_IDEAL_DEG:.2f} tetrahedral; "
                "residuals are preview evidence at a pinned registration",
                where=name,
                data=(
                    ("degree", 4),
                    ("hybridisation", "sp3"),
                    ("ideal_angle_deg", SP3_IDEAL_DEG),
                    ("angle_min_deg", angle_min),
                    ("angle_max_deg", angle_max),
                    ("radial_bond_min_A", float(radial.min())),
                    ("radial_bond_max_A", float(radial.max())),
                    ("bond_min_A", float(bond_lengths.min())),
                    ("bond_max_A", float(bond_lengths.max())),
                    ("side", side),
                    ("joint_constraint", "grafted wall and rim atoms pinned"),
                ),
            ),
            Finding(
                "surface.target.unavailable",
                Severity.INFO,
                "A cylinder with a grafted half-plane is unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    normalized = {
        "tube": [n, periods],
        "features": [{"name": name, "type": FEATURE_TYPE, "side": side, "rows": rows}],
        "k_tether": k,
    }
    block = _block_from_net(
        net,
        pos,
        spec="",
        report=Report(tuple(findings)).sorted(),
        fidelity="stick",
        extra_topology={
            "scene": normalized,
            "plan": {
                "construction": (
                    f"({n},{n}) armchair tube, {periods} periods; {rows}-row-pair "
                    f"strip grafted {side}ward by {grafts} radial sp3 bonds, one per "
                    "period, along one axial zigzag chain; open end rims ungrafted"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(pinned),
                "joint_constraint": "grafted wall and rim atoms fixed at authored analytic registration",
                "grafts": grafts,
                "radius_A": radius,
                "fin_height_A": graft.fin_height_A,
                "cylinder_distance_mean_A": float(residuals[tube_idx].mean()),
                "cylinder_distance_max_A": float(residuals[tube_idx].max()),
                "plane_distance_mean_A": float(residuals[fin_idx].mean()),
                "plane_distance_max_A": float(residuals[fin_idx].max()),
                "surface_deviation": "unavailable: a cylinder with a half-plane is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" sp3-grafted {side}ward fin on an armchair tube, open rims, one tethered "
            "stick pass with the grafted pairs pinned; replay generated.scene params; "
            "no stability claim"
        ),
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    return block
