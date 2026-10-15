"""Public channel-junction entry, through ``hexfold_scene`` only.

The channel junction (:mod:`hexfold.channel`): N wall arcs meet at N k3
seams along the axis, every seam's third sheet a strip pointing outward
(default) or inward per ``sides``.  The corners carry the ``k3-sp3-z``
dimer chain — the motif free GFN2-xTB relaxes every equal-120 sp2 seam
into, verified on the channel geometry itself (2026-10-14: the sp2-seamed
channel converges to the pre-paired structure within 0.1 eV on 354
carbons).  One tethered stick pass: seam atoms and their neighbours
pinned at the authored registration, each strip tethered to its own
half-plane, the arcs free.  No surface-of-revolution receipt exists, so
``surface_deviation`` stays unavailable.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np

from hexfold.channel import SIDES, channel_junction
from hexfold.check import Relaxed, geometry_findings
from hexfold.lattice import SP3_IDEAL_DEG
from hexfold.report import Finding, Report, Severity
from hexfold.stick import angle_springs, stick_relax_pinned
from precis_se.atomic.generators._types import GeneratedBlock, GeneratorError
from precis_se.atomic.generators.hexfold_spec import _block_from_net, terminate_mode

FEATURE_TYPE = "chan-k3-sp3-z"
DEFAULT_ROWS = 4


def _normalize(
    params: dict[str, Any],
) -> tuple[int, int, int, tuple[str, ...], str, float]:
    if set(params) - {"tube", "features", "k_tether", "terminate"}:
        raise GeneratorError(
            "channel scene accepts only tube, features, k_tether, terminate; "
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
            "channel tube must be [rows, periods]: even row pairs per wall "
            "arc in [4,40] and integer seam periods in [2,60]"
        )
    if tube[0] % 2:
        raise GeneratorError(
            "channel wall rows must be even so each arc's top zigzag rim "
            "lands in phase with the seam lattice"
        )
    if tube[1] % 2:
        raise GeneratorError(
            "fit.unsolvable: channel seams carry the k3-sp3-z dimer chain; "
            "needs an even period count"
        )
    features = params.get("features")
    if (
        not isinstance(features, list)
        or len(features) != 1
        or not isinstance(features[0], dict)
    ):
        raise GeneratorError(
            f"channel scene requires exactly one {FEATURE_TYPE} feature; no mixed feet"
        )
    feature = features[0]
    required = ("name", "type")
    missing = [key for key in required if key not in feature]
    if missing:
        raise GeneratorError(
            f"channel feature is missing required key(s): {', '.join(missing)}"
        )
    extra = sorted(map(str, set(feature) - {*required, "rows", "sides"}))
    if extra:
        raise GeneratorError(
            "channel feature takes only name, type, sides, rows; "
            f"unexpected: {', '.join(extra)}"
        )
    if feature["type"] != FEATURE_TYPE:
        raise GeneratorError(f"channel feature type must be {FEATURE_TYPE!r}")
    sides = feature.get("sides")
    if sides is None:
        sides = ("out", "out", "out")
    if (
        not isinstance(sides, list | tuple)
        or not 2 <= len(sides) <= 6
        or any(not isinstance(s, str) or s not in SIDES for s in sides)
    ):
        raise GeneratorError(
            "channel sides must be a list of 2-6 entries, each 'in' or 'out'"
        )
    rows = feature.get("rows", DEFAULT_ROWS)
    if isinstance(rows, bool) or type(rows) is not int or not 2 <= rows <= 12:
        raise GeneratorError("channel strip rows must be an integer in [2,12]")
    name = feature["name"]
    if not isinstance(name, str) or not name.strip():
        raise GeneratorError("channel feature name must be nonempty")
    k = params.get("k_tether", 1.0)
    if (
        isinstance(k, bool)
        or not isinstance(k, int | float)
        or not math.isfinite(k)
        or k <= 0
    ):
        raise GeneratorError(
            "channel k_tether must be finite and positive (tethered preview only)"
        )
    return tube[0], tube[1], rows, tuple(sides), name, float(k)


def build_channel(params: dict[str, Any]) -> GeneratedBlock:
    wall_rows, periods, strip_rows, sides, name, k = _normalize(params)
    termination = terminate_mode(params)
    try:
        net, channel = channel_junction(periods, wall_rows, strip_rows, sides)
    except ValueError as exc:
        raise GeneratorError(str(exc)) from exc
    seed = channel.coords
    phi = [2.0 * math.pi * i / channel.rails for i in range(channel.rails)]
    tangents = [np.array([-math.sin(p), math.cos(p), 0.0]) for p in phi]

    def tether(pos: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        # each strip is tethered to its own half-plane (the plane through
        # the axis at its rail angle); arcs and seam atoms get a
        # zero-residual foot, so the arcs are free to settle
        foot = pos.copy()
        normal = np.zeros_like(pos)
        for kk, idx in enumerate(channel.strip_atoms):
            t = tangents[kk]
            atoms = np.asarray(idx, dtype=np.int64)
            foot[atoms] = pos[atoms] - (pos[atoms] @ t)[:, None] * t
            normal[atoms] = t
        return foot, normal

    movable = np.ones(len(net.atoms), dtype=float)
    movable[list(channel.pinned)] = 0
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
            "channel tethered relaxation returned nonfinite coordinates/force"
        )
    adjacency: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _ in net.bonds:
        adjacency[i].append(j)
        adjacency[j].append(i)
    angle_dev = 0.0
    for seam in channel.seam_atoms:
        for atom in seam:
            neighbors = adjacency[atom]
            if len(neighbors) != 4:
                raise GeneratorError(
                    "channel seam atom is not degree-four; the dimer chain "
                    "needs an even period count"
                )
            vectors = pos[neighbors] - pos[atom]
            lengths = np.linalg.norm(vectors, axis=1)
            if np.any(lengths == 0):
                raise GeneratorError("channel relaxed seam contains a zero-length bond")
            vectors /= lengths[:, None]
            angles = np.degrees(np.arccos(np.clip(vectors @ vectors.T, -1, 1)))
            angles = np.triu(angles, k=1)
            angle_dev = max(
                angle_dev,
                float(np.max(np.abs(angles[angles > 0] - SP3_IDEAL_DEG))),
            )
    arc_idx = np.asarray([o for arc in channel.arc_atoms for o in arc])
    centre = pos[arc_idx, :2].mean(axis=0)
    per_arc: list[dict[str, Any]] = []
    for kk, arc in enumerate(channel.arc_atoms):
        rho = np.linalg.norm(pos[list(arc), :2] - centre, axis=1)
        seam_rho = float(
            np.linalg.norm(
                pos[
                    list(channel.seam_atoms[kk])
                    + list(channel.seam_atoms[(kk + 1) % channel.rails]),
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
    findings = geometry_findings(net, relaxed=Relaxed(pos, force, "tethered"))
    findings.extend(
        [
            Finding(
                "seam.rings",
                Severity.INFO,
                f"{len(eight)} eight-cycle faces along {channel.rails} "
                f"straight seams of {periods} periods each",
                where=name,
                data=(
                    ("rings", eight),
                    ("periods", periods),
                    ("seam_atoms", [list(s) for s in channel.seam_atoms]),
                ),
            ),
            Finding(
                "seam.geometry",
                Severity.INFO,
                f"Measured tethered degree-four sp3 seams ({channel.rails} "
                "corners of the k3-sp3-z dimer chain); angle residuals are "
                "preview evidence",
                where=name,
                data=(
                    ("degree", 4),
                    ("hybridisation", "sp3"),
                    ("rails", channel.rails),
                    ("angle_deviation_max_deg", angle_dev),
                    ("bond_min_A", float(bond_lengths.min())),
                    ("bond_max_A", float(bond_lengths.max())),
                    (
                        "joint_constraint",
                        "authored seams and immediate neighbours pinned",
                    ),
                ),
            ),
            Finding(
                "channel.cross_section",
                Severity.INFO,
                f"{channel.rails} free arcs settled "
                f"{min(a['rho_min_A'] for a in per_arc):.2f}-"
                f"{max(a['rho_max_A'] for a in per_arc):.2f} A from the axis "
                f"(seed circle {channel.radius_A:.2f} A, seam lines at "
                f"{min(channel.seam_radius_A):.2f}-{max(channel.seam_radius_A):.2f} A)",
                where=name,
                data=(
                    ("seed_radius_A", channel.radius_A),
                    ("seed_seam_radius_A", list(channel.seam_radius_A)),
                    ("sides", list(sides)),
                    ("arcs", per_arc),
                    ("arc_tether", "none"),
                ),
            ),
            Finding(
                "surface.target.unavailable",
                Severity.INFO,
                "A multi-seam channel is unsupported by the stored revolution-target format; surface_deviation remains unknown",
                where=name,
            ),
        ]
    )
    foot, _normal = tether(pos)
    residuals = np.linalg.norm(pos - foot, axis=1)
    strip_idx = np.asarray([o for s in channel.strip_atoms for o in s])
    normalized = {
        "tube": [wall_rows, periods],
        "features": [
            {
                "name": name,
                "type": FEATURE_TYPE,
                "sides": list(sides),
                "rows": strip_rows,
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
                    f"{channel.rails} k3-sp3-z seam corners along a channel: "
                    f"{strip_rows}-row-pair strips ({' '.join(sides)}) and a "
                    f"wall of {channel.rails} arcs of {wall_rows} row pairs; "
                    f"{periods} seam periods per seam"
                ),
                "units": "Angstrom",
                "passes": 1,
                "relax": "tethered",
                "max_force": force,
                "pinned_joint_atoms": len(channel.pinned),
                "joint_constraint": "seams and immediate neighbours fixed at authored analytic registration; arcs free",
                "seed_radius_A": channel.radius_A,
                "seed_seam_radius_A": list(channel.seam_radius_A),
                "strip_height_A": channel.strip_height_A,
                "plane_distance_mean_A": float(residuals[strip_idx].mean()),
                "plane_distance_max_A": float(residuals[strip_idx].max()),
                "surface_deviation": "unavailable: a multi-seam channel is not a surface of revolution",
            },
        },
        provenance_tail=(
            f" {channel.rails} k3-sp3-z seam corners along a channel axis, "
            f"strips {' '.join(sides)}, "
            "one tethered stick pass with seams and neighbours pinned and "
            "the arcs free; replay generated.scene params; no stability claim"
        ),
        drop_h_under_A=1.0,
    )
    block.topology.pop("spec", None)
    block.topology.pop("canonical_json", None)
    return block
