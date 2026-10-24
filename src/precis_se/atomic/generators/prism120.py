"""The ``prism-k3-120-z`` scene feature: a closed tube of flat zigzag wall
strips joined at signed equal-120 seams, through ``hexfold_scene`` only.

Sibling of the rail tube (:mod:`.rail120`), same k3 equal-120 seams and
the same tethered stick pass -- every seam atom and its three neighbours
pinned at the authored 120-degree registration, each fin/rib tethered to
its own half-plane, the flat walls free -- but the wall strips carry no
curvature: the cross-section is a polygon, and each seam's third sheet is
an outward fin (``+``) or an inward rib (``-``).  The feature carries the
whole geometry (``rows``, ``periods``, ``strip_rows``, ``seams``,
``pores``); there is no ``tube`` key.

```json
{"op":"generate","generator":"hexfold_scene","name":"p",
 "params":{"k_tether":1.0,
 "features":[{"name":"p","type":"prism-k3-120-z",
 "rows":6,"periods":8,"strip_rows":4,"seams":"++++++"}]}}
```

``topology.junctions``/``seam_atoms``/``sigma_A`` persist at mint -- one
junction record per seam, tagged ``seam:<i>:<sign>`` -- so
``relax_block``/``tunnel_strain`` re-score ``junction.strain`` per seam
without the stick seed (the tunnel's H3 bookkeeping contract).  The site
list (every open-valence fin/rib carbon plus every seam atom, with
two-point pendant pairs) is measured on the stored coordinates and kept
in ``topology["sites"]``; seam entries carry their ``sign``.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.check import Relaxed, geometry_findings
from hexfold.prism import prism_tube
from hexfold.report import Finding, Report, Severity
from hexfold.stick import angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import (
    _block_from_net,
    surviving_ordinals,
    terminate_mode,
)

FEATURE_TYPE = "prism-k3-120-z"
#: same-strip site pairs inside this band can anchor one two-point pendant
_PAIR_BAND_A = (2.5, 3.0)


def _is_int(v: object) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _normalize(
    params: dict[str, Any],
) -> tuple[int, int, int, str, tuple[tuple[int, int, int], ...], str, float]:
    if set(params) - {"features", "k_tether", "terminate"}:
        raise GeneratorError(
            "prism scene accepts only features, k_tether, terminate; "
            "no tube, sheet, extra or mixed feet"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"prism scene requires exactly one {FEATURE_TYPE} feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type", "rows", "periods", "strip_rows", "seams")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"prism feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - {*required, "pores"}))
    if extra:
        raise GeneratorError(
            "prism feature takes only name, type, rows, periods, "
            f"strip_rows, seams, pores; unexpected: {', '.join(extra)}"
        )
    if feature["type"] != FEATURE_TYPE:
        raise GeneratorError(f"prism feature type must be {FEATURE_TYPE!r}")
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("prism feature name must be nonempty")
    rows = feature["rows"]
    odd_rows = _is_int(rows) and 4 <= rows <= 40 and rows % 2 == 1
    if not _is_int(rows) or not 4 <= rows <= 40 or rows % 2:
        raise GeneratorError(
            "prism rows must be an even integer in [4,40] so each wall's "
            "top zigzag rim lands in phase with the seam lattice",
            code="prism.rows.odd" if odd_rows else "prism.rows",
            fix=(
                f"rows is odd — nearest legal: rows={rows - 1} or rows={rows + 1}"
                if odd_rows
                else "rows: an even integer in [4,40]"
            ),
        )
    periods = feature["periods"]
    if not _is_int(periods) or not 2 <= periods <= 60:
        raise GeneratorError("prism periods must be an integer in [2,60]")
    strip_rows = feature["strip_rows"]
    if not _is_int(strip_rows) or not 2 <= strip_rows <= 12:
        raise GeneratorError("prism strip_rows must be an integer in [2,12]")
    seams = feature["seams"]
    if not isinstance(seams, str):
        raise GeneratorError(
            f"prism seams must be one string of '+'/'-'/'−' signs; got {seams!r}"
        )
    text = seams.replace("−", "-")
    if any(c not in "+-" for c in text) or not 3 <= len(text) <= 24:
        raise GeneratorError(
            f"prism seams must be 3..24 '+'/'-'/'−' signs; got {seams!r}"
        )
    raw_pores = feature.get("pores", [])
    if not isinstance(raw_pores, list):
        raise GeneratorError("prism pores must be a list of [wall, u, v]")
    pores: list[tuple[int, int, int]] = []
    for p in raw_pores:
        if (
            not isinstance(p, list | tuple)
            or len(p) != 3
            or any(not _is_int(v) for v in p)
        ):
            raise GeneratorError(
                "prism pores must be [wall, u, v] integer cell coordinates"
            )
        pores.append((int(p[0]), int(p[1]), int(p[2])))
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "prism k_tether must be finite and positive (tethered preview only)"
        )
    return rows, periods, strip_rows, seams, tuple(pores), name, float(k)


def _site_ordinals(
    degrees: np.ndarray,
    strip_atoms: tuple[tuple[int, ...], ...],
    seam_atoms: tuple[tuple[int, ...], ...],
    signs: tuple[str, ...],
) -> list[tuple[int, str, int, str | None]]:
    """(ordinal, region, seam index, sign) for every strip carbon carrying
    an open valence (H-capped) and every seam atom."""
    out: list[tuple[int, str, int, str | None]] = []
    for k, atoms in enumerate(strip_atoms):
        out.extend((o, f"strip{k}", k, None) for o in atoms if degrees[o] < 3)
    for k, atoms in enumerate(seam_atoms):
        out.extend((o, f"seam{k}", k, signs[k]) for o in atoms)
    return out


def _site_list(
    coords: np.ndarray,
    sites: list[tuple[int, str, int, str | None]],
) -> dict[str, Any]:
    """Cylindrical site record per site ordinal, measured on the given
    (stored) coordinates -- the rail tube's lumen list verbatim plus a
    ``sign`` on seam entries."""
    entries: list[dict[str, Any]] = []
    for o, region, seam, sign in sites:
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
            | ({"sign": sign} if sign is not None else {})
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


def build_prism120(params: dict[str, Any]) -> GeneratedBlock:
    rows, periods, strip_rows, seams, pores, name, k = _normalize(params)
    termination = terminate_mode(params)
    try:
        net, tube = prism_tube(periods, rows, strip_rows, seams, pores=pores)
    except ValueError as exc:
        raise GeneratorError.lift(exc) from exc
    n = len(tube.signs)
    seed = tube.coords
    verts = np.asarray(tube.vertices)
    # each fin/rib is tethered to its own half-plane (the plane through its
    # seam vertex along the third-sheet direction — not through the axis:
    # a fin's plane is offset from the centroid by V·t); walls and seam
    # atoms get a zero-residual foot, so the walls are free to settle
    tangents = [np.array([-d[1], d[0], 0.0]) for d in np.asarray(tube.strip_dirs)]

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        foot = pos.copy()
        normal = np.zeros_like(pos)
        for kk, idx in enumerate(tube.strip_atoms):
            t = tangents[kk]
            atoms = np.asarray(idx, dtype=np.int64)
            rel = pos[atoms] - np.array([verts[kk, 0], verts[kk, 1], 0.0])
            foot[atoms] = pos[atoms] - (rel @ t)[:, None] * t
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
            "prism tethered relaxation returned nonfinite coordinates/force"
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
                raise GeneratorError("prism seam atom is not degree-three")
            vectors = pos[neighbors] - pos[atom]
            lengths = np.linalg.norm(vectors, axis=1)
            if np.any(lengths == 0):
                raise GeneratorError("prism relaxed seam contains a zero-length bond")
            vectors /= lengths[:, None]
            triple = max(triple, abs(float(np.linalg.det(vectors))))
            angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
            angle_dev = max(
                angle_dev,
                float(np.max(np.abs(angles[np.triu_indices(3, 1)] - 120))),
            )
    # wall flatness after the free relax: out-of-plane residual off each
    # wall's authored plane (the tether=none measurement)
    edge_dirs = np.asarray(
        [
            np.asarray(tube.vertices)[(kk + 1) % n] - np.asarray(tube.vertices)[kk]
            for kk in range(n)
        ]
    )
    edge_dirs /= np.linalg.norm(edge_dirs, axis=1)[:, None]
    wall_normals = np.stack([-edge_dirs[:, 1], edge_dirs[:, 0], np.zeros(n)], axis=1)
    per_wall: list[dict[str, Any]] = []
    for kk, wall in enumerate(tube.wall_atoms):
        rel = pos[list(wall)] - np.array(
            [tube.vertices[kk][0], tube.vertices[kk][1], 0.0]
        )
        off = rel @ wall_normals[kk]
        per_wall.append(
            {
                "wall": kk,
                "off_plane_mean_A": float(off.mean()),
                "off_plane_max_A": float(np.abs(off).max()),
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
    sites = _site_ordinals(degrees, tube.strip_atoms, tube.seam_atoms, tube.signs)
    site_data = _site_list(pos, sites)
    n_fins = tube.signs.count("+")
    n_ribs = n - n_fins
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{len(eight)} eight-cycle faces along {n} straight seams "
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
                f"Measured tethered degree-three sp2 seams ({n} seams, "
                f"{n_fins} fins, {n_ribs} ribs); angle and coplanarity "
                "residuals are preview evidence",
                where=name,
                data=(
                    ("degree", 3),
                    ("hybridisation", "sp2"),
                    ("seams", n),
                    ("signs", list(tube.signs)),
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
                "prism.cross_section",
                Severity.INFO,
                f"{n} free flat walls settled at most "
                f"{max(w['off_plane_max_A'] for w in per_wall):.2f} A off "
                f"their authored planes (side {tube.side_A:.2f} A, mean "
                f"vertex radius {tube.radius_A:.2f} A)",
                where=name,
                data=(
                    ("side_A", tube.side_A),
                    ("seed_vertex_radius_A", tube.radius_A),
                    ("walls", per_wall),
                    ("wall_tether", "none"),
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
                f"{len(sites)} attachment sites "
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
                "A polygonal prism is unsupported by the stored revolution-target format; surface_deviation remains unknown",
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
        "rows": rows,
        "periods": periods,
        "strip_rows": strip_rows,
        "seams": "".join(tube.signs),
    }
    if pores:
        normalized_feature["pores"] = [list(p) for p in pores]
    normalized = {
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
                    f"{n} signed equal-120 k3 seams along the axis "
                    f"({''.join(tube.signs)}): {n_fins} outward fins and "
                    f"{n_ribs} inward ribs of {strip_rows} row pairs, a "
                    f"polygonal wall of {n} flat strips of {rows} row "
                    f"pairs on {tube.side_A:.2f} A sides; {periods} seam "
                    f"periods per seam; {len(tube.pore_rims)} authored pore(s)"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(tube.pinned),
                "joint_constraint": "seams and immediate neighbours fixed at authored analytic registration; walls free",
                "side_A": tube.side_A,
                "seed_vertex_radius_A": tube.radius_A,
                "strip_height_A": tube.strip_height_A,
                "plane_distance_mean_A": float(residuals[strip_idx].mean()),
                "plane_distance_max_A": float(residuals[strip_idx].max()),
                "surface_deviation": "unavailable: a polygonal prism is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" {n} signed equal-120 k3 seams along a tube axis "
            f"({''.join(tube.signs)}), flat wall strips, one tethered stick "
            "pass with seams and neighbours pinned and the walls free; "
            "replay generated.scene params; no stability claim"
        ),
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    # sites are measured on the relaxed build frame (authored tube axis at
    # x=y=0, theta counted from seam 0 on +x): the canonical frame is a
    # PCA fit whose in-plane axis and origin are not the tube's, so they
    # are not a stable reference once a pore breaks the symmetry.
    # ``frame.to_stored`` is the affine map into the stored coordinates
    # for round-tripping against stored atoms.
    # a seam-end stub the terminator trimmed (gr486406) is gone from the
    # block: fit the frame on the survivors and remap the site records
    remap = surviving_ordinals(net, block)
    kept = sorted(remap, key=remap.__getitem__)
    design = np.hstack([pos[kept], np.ones((len(kept), 1))])
    xf, *_ = np.linalg.lstsq(design, block.coords[: len(kept)], rcond=None)
    # the terminator's polish can move a surviving atom after the site list
    # was measured: pull the stored coordinates back through the frame and
    # re-measure, so the list round-trips to the atoms actually stored
    rot, shift = xf[:3], xf[3]
    polished = pos.copy()
    polished[kept] = (block.coords[: len(kept)] - shift) @ np.linalg.pinv(rot)
    site_data = _site_list(polished, sites)
    site_data["sites"] = [
        {**e, "ordinal": remap[e["ordinal"]]}
        for e in site_data["sites"]
        if e["ordinal"] in remap
    ]
    site_data["pairs"] = [
        [remap[a], remap[b]] for a, b in site_data["pairs"] if a in remap and b in remap
    ]
    site_data["frame"] = {
        "axis": "authored tube axis, +z through the cross-section centroid",
        "origin_A": [0.0, 0.0, 0.0],
        "theta_zero": "seam 0 on +x",
        "measured_on": "relaxed coordinates before canonical framing",
        "to_stored": {
            "rot": xf[:3].tolist(),
            "shift_A": xf[3].tolist(),
        },
    }
    block.topology["sites"] = site_data
    # Strain bookkeeping on stored ordinals (H3): one junction record per
    # seam, tagged seam:<i>:<sign>, so relax_block/tunnel_strain re-score
    # junction.strain per seam without re-running the generator.
    junctions: list[dict[str, Any]] = []
    for kk, rec in enumerate(net.junctions):
        atoms = [int(remap[o]) for o in rec.atoms if o in remap]
        fused = [
            [int(remap[a]), int(remap[b])]
            for a, b in rec.fused
            if a in remap and b in remap
        ]
        faces = [
            [int(remap[x]) for x in f] for f in rec.faces if all(x in remap for x in f)
        ]
        junctions.append(
            {"name": rec.name, "atoms": atoms, "fused": fused, "faces": faces}
        )
    block.topology["junctions"] = junctions
    block.topology["seam_atoms"] = [
        [int(remap[o]) for o in s if o in remap] for s in tube.seam_atoms
    ]
    block.topology["sigma_A"] = float(net.lattice.sigma_A)
    return block
