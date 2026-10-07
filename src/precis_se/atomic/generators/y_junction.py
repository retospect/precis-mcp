"""Public equal-120 straight Y entry, through hexfold_scene only.

Reuses the private joiner and the existing tethered stick pass. Authored
planes belong to individual sheets, avoiding nearest-plane reassignment
at their common line. Three planes cannot be encoded as a stored surface
of revolution: no synthetic surface_target receipt or zero deviation.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.report import Finding, Report, Severity
from hexfold.stick import _angle_springs, stick_relax_pinned
from hexfold.y_junction import straight_y
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import _block_from_net, terminate_mode


def _normalize(params: dict[str, Any]) -> tuple[int, int, str, float]:
    if set(params) - {"sheet", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "straight Y accepts only sheet, features, k_tether; no extra or mixed feet"
        )
    sheet = params.get("sheet")
    if (
        not isinstance(sheet, list | tuple)
        or len(sheet) != 2
        or any(type(n) is not int or not 2 <= n <= 30 for n in sheet)
    ):
        raise GeneratorError(
            "straight Y sheet must be [periods,row_pairs], integers in [2,30]"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            "straight Y requires exactly one k3-sp2-120-z feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "dihedrals_deg")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"straight Y feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - set(required)))
    if extra:
        raise GeneratorError(
            "straight Y feature requires only name, type, dihedrals_deg; "
            f"unexpected: {', '.join(extra)}"
        )
    angles = feature["dihedrals_deg"]
    if (
        feature["type"] != "k3-sp2-120-z"
        or not isinstance(angles, list | tuple)
        or len(angles) != 3
        or any(
            isinstance(a, bool) or not isinstance(a, int | float) or a != 120
            for a in angles
        )
    ):
        raise GeneratorError(
            "fit.unsolvable: only type=k3-sp2-120-z with explicit [120,120,120] is supported; unequal k3/k>=5 unavailable"
        )
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("straight Y feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "straight Y k_tether must be finite and positive (tethered preview only)"
        )
    return sheet[0], sheet[1], name, float(k)


def build_y_junction(params: dict[str, Any]) -> GeneratedBlock:
    periods, row_pairs, name, k = _normalize(params)
    net, result = straight_y(periods, row_pairs)
    normals = np.zeros_like(result.coords)
    for j, (_region, ords) in enumerate(net.regions):
        # row-vector placement uses rotation.T; input sheet normal is z.
        normals[list(ords)] = result.transforms[j][0][:, 2]
    seed = result.coords

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        distance = np.sum((pos - seed) * normals, axis=1)
        foot = pos - distance[:, None] * normals
        # Fixed analytic seam line, rather than selecting one sheet normal.
        seam = list(result.seam_atoms)
        radial = pos[seam].copy()
        radial[:, 0] = 0
        length = np.linalg.norm(radial, axis=1)
        direction = np.divide(
            radial,
            length[:, None],
            out=np.zeros_like(radial),
            where=length[:, None] > 0,
        )
        foot[seam] = pos[seam] - radial
        normal = normals.copy()
        normal[seam] = direction
        return foot, normal

    # Fixed authored registration: plane tethers alone permit axial
    # sliding and pyramidalisation of the seam's three bonds. Pin only
    # the seam and its immediate neighbours, leaving sheets movable.
    pinned = set(result.seam_atoms)
    seam_set = set(result.seam_atoms)
    for i, j, _ in net.bonds:
        if i in seam_set:
            pinned.add(j)
        if j in seam_set:
            pinned.add(i)
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
            "straight Y tethered relaxation returned nonfinite coordinates/force"
        )
    adjacency: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _ in net.bonds:
        adjacency[i].append(j)
        adjacency[j].append(i)
    angle_dev = 0.0
    triple = 0.0
    for atom in result.seam_atoms:
        neighbors = adjacency[atom]
        if len(neighbors) != 3:
            raise GeneratorError("straight Y seam atom is not degree-three")
        vectors = pos[neighbors] - pos[atom]
        lengths = np.linalg.norm(vectors, axis=1)
        if np.any(lengths == 0):
            raise GeneratorError("straight Y relaxed seam contains a zero-length bond")
        vectors /= lengths[:, None]
        triple = max(triple, abs(float(np.linalg.det(vectors))))
        angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
        angle_dev = max(
            angle_dev, float(np.max(np.abs(angles[np.triu_indices(3, 1)] - 120)))
        )
    faces = [list(ring) for ring in net.rings if len(ring) == 8]
    bond_lengths = np.asarray(
        [np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds]
    )
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{len(faces)} eight-cycle faces along {periods} straight seam periods",
                where=name,
                data=(
                    ("rings", faces),
                    ("periods", periods),
                    ("seam_atoms", list(result.seam_atoms)),
                ),
            ),
            Finding(
                "seam.geometry",
                Severity.INFO,
                "Measured tethered degree-three sp2 seam; angle and coplanarity residuals are preview evidence",
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
                "surface.target.unavailable",
                Severity.INFO,
                "Three authored sheet planes are unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    normalized = {
        "sheet": [periods, row_pairs],
        "features": [
            {"name": name, "type": "k3-sp2-120-z", "dihedrals_deg": [120, 120, 120]}
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
                "construction": "straight equal-120 Y; private open zigzag segments; two guard columns",
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(pinned),
                "joint_constraint": "seam and immediate neighbours fixed at authored analytic registration",
                "plane_distance_mean_A": float(residuals.mean()),
                "plane_distance_max_A": float(residuals.max()),
                "surface_deviation": "unavailable: three-plane Y is not a surface of revolution",
            },
        },
        provenance_tail=" equal-120 k3 straight Y, open rims, one tethered stick pass with authored seam and immediate neighbours pinned; replay generated.scene params; no stability claim",
    )
    # This scene has no authored hx input: don't label an empty Spec as one.
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    return block
