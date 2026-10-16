"""Public multi-rail seam tube entry, through ``hexfold_scene`` only.

The reactive-tunnel cross-section (:mod:`hexfold.railtube`): a closed tube
whose wall is N arcs meeting at N equal-120 sp2 k3 seams along the axis,
every seam's third sheet an inward strip (a rib) and authored vacancy-ring
pores in the arcs. One tethered stick pass: every seam atom and its three
neighbours pinned at the authored 120-degree registration (as the Y and
the seam tube), each strip tethered to its own half-plane, the arcs free
-- so the cross section they settle into is the relaxer's answer to the
seams, not a tether's. No surface-of-revolution receipt exists for it, so
``surface_deviation`` stays unavailable. The site list (every open-valence
rib carbon plus every seam atom, with two-point pendant pairs) is measured
on the stored coordinates and kept in ``topology["sites"]``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.railtube import rail_tube
from hexfold.report import Finding, Report, Severity
from hexfold.stick import angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import (
    _block_from_net,
    surviving_ordinals,
    terminate_mode,
)

FEATURE_TYPE = "rail-k3-120-z"
DEFAULT_ROWS = 4
#: same-strip site pairs inside this band can anchor one two-point pendant
_PAIR_BAND_A = (2.5, 3.0)


def _normalize(
    params: dict[str, Any],
) -> tuple[int, int, int, int, tuple[tuple[int, int, int], ...], str, float]:
    if set(params) - {"tube", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "rail-tube scene accepts only tube, features, k_tether, terminate; "
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
            "rail-tube tube must be [rows, periods]: even row pairs per wall "
            "arc in [4,40] and integer seam periods in [2,60]"
        )
    if tube[0] % 2:
        raise GeneratorError(
            "rail-tube tube rows must be even so each arc's top zigzag rim "
            "lands in phase with the seam lattice"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"rail-tube scene requires exactly one {FEATURE_TYPE} feature; "
            "no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "rails")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"rail-tube feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - {*required, "rows", "pores", "side"}))
    if extra:
        raise GeneratorError(
            "rail-tube feature takes only name, type, rails, rows, pores; "
            f"unexpected: {', '.join(extra)}"
        )
    if feature["type"] != FEATURE_TYPE:
        raise GeneratorError(f"rail-tube feature type must be {FEATURE_TYPE!r}")
    side = feature.get("side", "in")
    if side != "in":
        raise GeneratorError(
            "rail-tube strips always point inward; for an outward strip use "
            "fin-k3-120-z"
        )
    rails = feature["rails"]
    if isinstance(rails, bool) or type(rails) is not int or not 2 <= rails <= 6:
        raise GeneratorError("rail-tube rails must be an integer in [2,6]")
    rows = feature.get("rows", DEFAULT_ROWS)
    if isinstance(rows, bool) or type(rows) is not int or not 2 <= rows <= 12:
        raise GeneratorError("rail-tube strip rows must be an integer in [2,12]")
    raw_pores = feature.get("pores", [])
    if not isinstance(raw_pores, list):
        raise GeneratorError("rail-tube pores must be a list of {arc, at:[u,v]}")
    pores: list[tuple[int, int, int]] = []
    for p in raw_pores:
        at = p.get("at") if isinstance(p, dict) else None
        if (
            not isinstance(p, dict)
            or set(p) - {"arc", "at"}
            or not isinstance(p.get("arc"), int)
            or isinstance(p.get("arc"), bool)
            or not isinstance(at, list | tuple)
            or len(at) != 2
            or any(isinstance(v, bool) or type(v) is not int for v in at)
        ):
            raise GeneratorError(
                "rail-tube pores must be {arc: int, at: [u, v]} with integer "
                "cell coordinates"
            )
        pores.append((p["arc"], at[0], at[1]))
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("rail-tube feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "rail-tube k_tether must be finite and positive (tethered preview only)"
        )
    return tube[0], tube[1], rows, rails, tuple(pores), name, float(k)


def _site_ordinals(
    degrees: np.ndarray,
    strip_atoms: tuple[tuple[int, ...], ...],
    seam_atoms: tuple[tuple[int, ...], ...],
) -> list[tuple[int, str, int]]:
    """(ordinal, region, seam index) for every strip carbon carrying an
    open valence (H-capped) and every seam atom."""
    out: list[tuple[int, str, int]] = []
    for k, atoms in enumerate(strip_atoms):
        out.extend((o, f"strip{k}", k) for o in atoms if degrees[o] < 3)
    for k, atoms in enumerate(seam_atoms):
        out.extend((o, f"seam{k}", k) for o in atoms)
    return out


def _site_list(
    coords: np.ndarray,
    sites: list[tuple[int, str, int]],
) -> dict[str, Any]:
    """Cylindrical site record per site ordinal, measured on the given
    (stored) coordinates: the lumen attachment-site list the nanoreactor
    doc asks for, plus same-strip pairs inside the two-point pendant
    band."""
    entries: list[dict[str, Any]] = []
    for o, region, seam in sites:
        x, y, z = (float(v) for v in coords[o])
        r = math.hypot(x, y)
        entries.append(
            {
                "ordinal": int(o),
                "region": region,
                "seam": seam,
                "z_A": z,
                "theta_deg": math.degrees(math.atan2(y, x)),
                "r_A": r,
                "normal": [-x / r, -y / r, 0.0],
            }
        )
    lo, hi = _PAIR_BAND_A
    pairs: list[list[int]] = []
    by_strip: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if e["region"].startswith("strip"):
            by_strip.setdefault(e["region"], []).append(e)
    for members in by_strip.values():
        for i in range(len(members)):
            for j in range(i + 1, len(members)):
                d = float(
                    np.linalg.norm(
                        coords[members[i]["ordinal"]] - coords[members[j]["ordinal"]]
                    )
                )
                if lo <= d <= hi:
                    pairs.append([members[i]["ordinal"], members[j]["ordinal"]])
    return {"sites": entries, "pairs": pairs}


def build_rail120(params: dict[str, Any]) -> GeneratedBlock:
    wall_rows, periods, strip_rows, rails, pores, name, k = _normalize(params)
    termination = terminate_mode(params)
    try:
        net, tube = rail_tube(periods, wall_rows, strip_rows, rails, pores=pores)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    seed = tube.coords
    phi = [2.0 * math.pi * i / rails for i in range(rails)]
    tangents = [np.array([-math.sin(p), math.cos(p), 0.0]) for p in phi]

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # each strip is tethered to its own half-plane (the plane through
        # the axis at its rail angle); arcs and seam atoms get a
        # zero-residual foot, so the arcs are free to settle
        foot = pos.copy()
        normal = np.zeros_like(pos)
        for kk, idx in enumerate(tube.strip_atoms):
            t = tangents[kk]
            atoms = np.asarray(idx, dtype=np.int64)
            foot[atoms] = pos[atoms] - (pos[atoms] @ t)[:, None] * t
            normal[atoms] = t
        return foot, normal

    movable = np.ones(len(net.atoms), dtype=float)
    movable[list(tube.pinned)] = 0
    raw, force = stick_relax_pinned(
        seed,
        np.asarray([(i, j) for i, j, _ in net.bonds], dtype=np.int64),
        np.full(len(net.bonds), net.lattice.sigma_A),
        np.asarray(angle_springs(net), dtype=float),
        net.lattice.sigma_A,
        movable=movable,
        tether=tether,
        k_tether=k,
    )
    pos = np.asarray(raw, dtype=float)
    if not np.isfinite(pos).all() or not math.isfinite(force):
        raise GeneratorError(
            "rail-tube tethered relaxation returned nonfinite coordinates/force"
        )
    adjacency: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _ in net.bonds:
        adjacency[i].append(j)
        adjacency[j].append(i)
    angle_dev = 0.0
    triple = 0.0
    for seam in tube.seam_atoms:
        for atom in seam:
            neighbors = adjacency[atom]
            if len(neighbors) != 3:
                raise GeneratorError("rail-tube seam atom is not degree-three")
            vectors = pos[neighbors] - pos[atom]
            lengths = np.linalg.norm(vectors, axis=1)
            if np.any(lengths == 0):
                raise GeneratorError(
                    "rail-tube relaxed seam contains a zero-length bond"
                )
            vectors /= lengths[:, None]
            triple = max(triple, abs(float(np.linalg.det(vectors))))
            angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
            angle_dev = max(
                angle_dev,
                float(np.max(np.abs(angles[np.triu_indices(3, 1)] - 120))),
            )
    # the cross section the arcs settled into, about the tube's own axis
    arc_idx = np.asarray([o for arc in tube.arc_atoms for o in arc])
    centre = pos[arc_idx, :2].mean(axis=0)
    per_arc: list[dict[str, Any]] = []
    for kk, arc in enumerate(tube.arc_atoms):
        rho = np.linalg.norm(pos[list(arc), :2] - centre, axis=1)
        seam_rho = float(
            np.linalg.norm(
                pos[
                    list(tube.seam_atoms[kk]) + list(tube.seam_atoms[(kk + 1) % rails]),
                    :2,
                ]
                - centre,
                axis=1,
            ).mean()
        )
        per_arc.append(
            {
                "arc": kk,
                "rho_mean_A": float(rho.mean()),
                "rho_min_A": float(rho.min()),
                "rho_max_A": float(rho.max()),
                "seam_rho_A": seam_rho,
            }
        )
    eight = [list(r) for r in net.rings if len(r) == 8]
    bond_lengths = np.asarray(
        [np.linalg.norm(pos[i] - pos[j]) for i, j, _ in net.bonds]
    )
    degrees = np.bincount(
        [v for i, j, _ in net.bonds for v in (i, j)],
        minlength=len(net.atoms),
    )
    sites = _site_ordinals(degrees, tube.strip_atoms, tube.seam_atoms)
    site_data = _site_list(pos, sites)
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{len(eight)} eight-cycle faces along {rails} straight seams "
                f"of {periods} periods each",
                where=name,
                data=(
                    ("rings", eight),
                    ("periods", periods),
                    ("seam_atoms", [list(s) for s in tube.seam_atoms]),
                ),
            ),
            Finding(
                "seam.geometry",
                Severity.INFO,
                f"Measured tethered degree-three sp2 seams ({rails} rails); "
                "angle and coplanarity residuals are preview evidence",
                where=name,
                data=(
                    ("degree", 3),
                    ("hybridisation", "sp2"),
                    ("rails", rails),
                    ("angle_deviation_max_deg", angle_dev),
                    ("normalized_triple_max", triple),
                    ("bond_min_A", float(bond_lengths.min())),
                    ("bond_max_A", float(bond_lengths.max())),
                    (
                        "joint_constraint",
                        "authored seams and immediate neighbours pinned",
                    ),
                ),
            ),
            Finding(
                "tube.cross_section",
                Severity.INFO,
                f"{rails} free arcs settled "
                f"{min(a['rho_min_A'] for a in per_arc):.2f}-"
                f"{max(a['rho_max_A'] for a in per_arc):.2f} A from the axis "
                f"(seed circle {tube.radius_A:.2f} A, seam lines "
                f"{tube.seam_radius_A:.2f} A)",
                where=name,
                data=(
                    ("seed_radius_A", tube.radius_A),
                    ("seed_seam_radius_A", tube.seam_radius_A),
                    ("arcs", per_arc),
                    ("arc_tether", "none"),
                ),
            ),
            Finding(
                "pore.rings",
                Severity.INFO,
                f"{len(tube.pore_rims)} authored pore(s), vacancy rings open "
                "to H2, rims "
                + ("H-terminated" if termination != "none" else "uncapped"),
                where=name,
                data=(
                    ("count", len(tube.pore_rims)),
                    ("rim_sizes", [len(r) for r in tube.pore_rims]),
                    ("rim_atoms", [list(r) for r in tube.pore_rims]),
                ),
            ),
            Finding(
                "sites",
                Severity.INFO,
                f"{len(sites)} lumen attachment sites "
                f"({len(site_data['pairs'])} same-strip pairs in the "
                f"{_PAIR_BAND_A[0]:.1f}-{_PAIR_BAND_A[1]:.1f} A two-point "
                "pendant band)",
                where=name,
                data=(
                    ("count", len(sites)),
                    ("pairs", site_data["pairs"]),
                ),
            ),
            Finding(
                "surface.target.unavailable",
                Severity.INFO,
                "A multi-seam rail tube is unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    strip_idx = np.asarray([o for s in tube.strip_atoms for o in s])
    normalized_feature: dict[str, Any] = {
        "name": name,
        "type": FEATURE_TYPE,
        "rails": rails,
        "rows": strip_rows,
    }
    if pores:
        normalized_feature["pores"] = [
            {"arc": a_, "at": [u_, v_]} for a_, u_, v_ in pores
        ]
    normalized = {
        "tube": [wall_rows, periods],
        "features": [normalized_feature],
        "k_tether": k,
    }
    if "terminate" in params:
        normalized["terminate"] = termination
    block = _block_from_net(
        net,
        pos,
        spec="",
        terminate=termination,
        pinned=frozenset(tube.pinned),
        report=Report(tuple(findings)).sorted(),
        fidelity="stick",
        extra_topology={
            "scene": normalized,
            "plan": {
                "construction": (
                    f"{rails} equal-120 k3 seams along the axis: {strip_rows}-row-pair "
                    f"inward strips and a wall of {rails} arcs of {wall_rows} row "
                    f"pairs; {periods} seam periods per seam; "
                    f"{len(tube.pore_rims)} authored pore(s)"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(tube.pinned),
                "joint_constraint": "seams and immediate neighbours fixed at authored analytic registration; arcs free",
                "seed_radius_A": tube.radius_A,
                "seed_seam_radius_A": tube.seam_radius_A,
                "strip_height_A": tube.strip_height_A,
                "plane_distance_mean_A": float(residuals[strip_idx].mean()),
                "plane_distance_max_A": float(residuals[strip_idx].max()),
                "surface_deviation": "unavailable: a multi-seam rail tube is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" {rails} equal-120 k3 seams along a tube axis, inward strips, "
            "one tethered stick pass with seams and neighbours pinned and "
            "the arcs free; replay generated.scene params; no stability claim"
        ),
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    # sites are measured on the relaxed build frame (authored tube axis at
    # x=y=0, theta counted from strip 0's half-plane): the canonical frame
    # is a PCA fit whose in-plane axis and origin are not the tube's, so
    # they are not a stable reference once a pore breaks the N-fold
    # symmetry. ``frame.to_stored`` is the affine map into the stored
    # coordinates for round-tripping against stored atoms.
    # a seam-end stub the terminator trimmed (gr486406) is gone from the
    # block: fit the frame on the survivors and remap the site records
    remap = surviving_ordinals(net, block)
    kept = sorted(remap, key=remap.__getitem__)
    design = np.hstack([pos[kept], np.ones((len(kept), 1))])
    xf, *_ = np.linalg.lstsq(design, block.coords[: len(kept)], rcond=None)
    site_data["sites"] = [
        {**e, "ordinal": remap[e["ordinal"]]}
        for e in site_data["sites"]
        if e["ordinal"] in remap
    ]
    site_data["pairs"] = [
        [remap[a], remap[b]] for a, b in site_data["pairs"] if a in remap and b in remap
    ]
    site_data["frame"] = {
        "axis": "authored tube axis, +z through x=y=0",
        "origin_A": [0.0, 0.0, 0.0],
        "theta_zero": "strip0's half-plane (seam0 at theta_deg 0)",
        "measured_on": "relaxed coordinates before canonical framing",
        "to_stored": {
            "rot": xf[:3].tolist(),
            "shift_A": xf[3].tolist(),
        },
    }
    block.topology["sites"] = site_data
    return block
