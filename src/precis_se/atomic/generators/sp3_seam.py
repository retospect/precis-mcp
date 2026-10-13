"""Public sp3 seam lines, through hexfold_scene only.

Reuses the private analytic construction and the Y's tethered stick pass.
Authored planes belong to individual sheets, avoiding nearest-plane
reassignment at their common line. Three or four planes cannot be encoded
as a stored surface of revolution: no synthetic surface_target receipt or
zero deviation.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, _findings_from_net, geometry_findings
from hexfold.lattice import SP3_IDEAL_DEG
from hexfold.report import Finding, Report, Severity
from hexfold.sp3_seam import _rim_angles, _seam_angles, straight_k3z, straight_k4
from hexfold.stick import _angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import _block_from_net, terminate_mode

_FEATURE_TYPES = ("k4-sp3-90-a", "k3-sp3-z")


def _normalize(params: dict[str, Any]) -> tuple[str, int, int, str, float]:
    if set(params) - {"sheet", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "sp3 seam rows accept only sheet, features, k_tether; no extra or mixed feet"
        )
    sheet = params.get("sheet")
    if (
        not isinstance(sheet, list | tuple)
        or len(sheet) != 2
        or any(type(n) is not int or not 2 <= n <= 30 for n in sheet)
    ):
        raise GeneratorError(
            "sp3 seam sheet must be [periods,rows], integers in [2,30]"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            "sp3 seam rows require exactly one typed feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "dihedrals_deg")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"sp3 seam feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - set(required)))
    if extra:
        raise GeneratorError(
            "sp3 seam feature requires only name, type, dihedrals_deg; "
            f"unexpected: {', '.join(extra)}"
        )
    kind = feature["type"]
    expected = {
        "k4-sp3-90-a": [90.0] * 4,
        "k3-sp3-z": [120.0] * 3,
    }
    angles = feature["dihedrals_deg"]
    if kind not in expected or not (
        isinstance(angles, list | tuple)
        and len(angles) == len(expected.get(kind, ()))
        and all(
            not isinstance(a, bool) and isinstance(a, int | float) and a == d
            for a, d in zip(angles, expected.get(kind, ()))
        )
    ):
        raise GeneratorError(
            "fit.unsolvable: only k4-sp3-90-a with [90,90,90,90] and "
            "k3-sp3-z with [120,120,120] are supported; unequal dihedrals/"
            "k>=5 unavailable"
        )
    if kind == "k3-sp3-z" and sheet[0] % 2 != 0:
        raise GeneratorError(
            "fit.unsolvable: k3-sp3-z pairs seam atoms along the chain; "
            "needs an even period count"
        )
    if kind == "k4-sp3-90-a" and sheet[1] < 3:
        raise GeneratorError("k4-sp3-90-a rows (armchair atom levels) must be >= 3")
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("sp3 seam feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "sp3 seam k_tether must be finite and positive (tethered preview only)"
        )
    return str(kind), sheet[0], sheet[1], name, float(k)


def build_sp3_seam(params: dict[str, Any]) -> GeneratedBlock:
    kind, periods, rows, name, k = _normalize(params)
    termination = terminate_mode(params)
    net, result = (
        straight_k4(periods, rows)
        if kind == "k4-sp3-90-a"
        else straight_k3z(periods, rows)
    )
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
    # sliding and pyramidalisation of the seam's bonds. Pin only the seam
    # atoms and their immediate neighbours, leaving sheets movable.
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
        np.asarray(_angle_springs(net), dtype=float).reshape(-1, 3),
        net.lattice.sigma_A,
        movable=movable,
        tether=tether,
        k_tether=k,
    )
    pos = np.asarray(raw, dtype=float)
    if not np.isfinite(pos).all() or not math.isfinite(force):
        raise GeneratorError(
            "sp3 seam tethered relaxation returned nonfinite coordinates/force"
        )
    tables = _seam_angles(pos, net.bonds, result.seam_atoms)
    if any(len(t) != 6 for t in tables):
        raise GeneratorError("sp3 seam atom is not degree-four")
    rim_tables = _rim_angles(pos, net.bonds, result.seam_atoms)
    angle_dev = float(np.max(np.abs(np.asarray(tables) - SP3_IDEAL_DEG)))
    faces = [list(ring) for ring in net.rings if any(o in seam_set for o in ring)]
    sizes = sorted({len(ring) for ring in faces})
    census = {n: sum(1 for ring in faces if len(ring) == n) for n in sizes}
    bond_lengths = np.asarray(
        [np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds]
    )
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(_findings_from_net(net))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{dict(sorted(census.items()))} seam faces along {periods} straight seam periods",
                where=name,
                data=(
                    ("rings", faces),
                    ("census", dict(sorted(census.items()))),
                    ("periods", periods),
                    ("seam_atoms", list(result.seam_atoms)),
                ),
            ),
            Finding(
                "seam.geometry",
                Severity.INFO,
                "Degree-four sp3 seam at its pinned analytic seed registration; "
                "the seam and rim tables are authored geometry held through the "
                "tethered pass, not measured relaxation evidence",
                where=name,
                data=(
                    ("degree", 4),
                    ("hybridisation", "sp3"),
                    ("angle_deviation_max_deg", angle_dev),
                    (
                        "angles_deg",
                        {str(a): t for a, t in zip(result.seam_atoms, tables)},
                    ),
                    (
                        "rim_angles_deg",
                        {str(a): t for a, t in rim_tables},
                    ),
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
                "Multiple authored sheet planes are unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    normalized = {
        "sheet": [periods, rows],
        "features": [
            {
                "name": name,
                "type": kind,
                "dihedrals_deg": (
                    [90, 90, 90, 90] if kind == "k4-sp3-90-a" else [120, 120, 120]
                ),
            }
        ],
        "k_tether": k,
    }
    if "terminate" in params:
        normalized["terminate"] = termination
    block = _block_from_net(
        net,
        pos,
        spec="",
        terminate=termination,
        report=Report(tuple(findings)).sorted(),
        fidelity="stick",
        extra_topology={
            "scene": normalized,
            "plan": {
                "construction": (
                    f"{kind} straight sp3 seam; private open segments; "
                    "two guard columns"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(pinned),
                "joint_constraint": "seam and immediate neighbours fixed at authored analytic registration",
                "plane_distance_mean_A": float(residuals.mean()),
                "plane_distance_max_A": float(residuals.max()),
                "surface_deviation": "unavailable: multi-plane seam is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" {kind} straight sp3 seam, open rims, one tethered stick pass "
            "with authored seam and immediate neighbours pinned; replay "
            "generated.scene params; no stability claim"
        ),
        drop_h_under_A=1.0,
    )
    # This scene has no authored hx input: don't label an empty Spec as one.
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    return block
