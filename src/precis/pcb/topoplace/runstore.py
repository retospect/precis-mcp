"""topo-place 1c run storage (Decisions 20, 22): a run is a directory of

``manifest.json`` — schema, wheel version, code version, seed, effort,
    objective shares, board content hash, the parts/footprints read (R-e),
    and the result row the drill-down diff reads;
``elites.json`` — the archive (the placer's canonical bytes);
``pairs.json`` — the pairwise part map;
``pins.json`` — the ``pins.lock.json`` payload (Decision 24);
``summary.txt`` — the Decision-19 one-screen summary;
``placement.svg`` — airwires, crossings marked, pair heatmap.

Two storage surfaces, one payload builder: the laptop run writes the
files under ``out/`` (:func:`write_run`); the ``pcb_place`` job stores
the same dict in job meta (``meta.topo_run.files``) for ``view='topo'``.
Determinism is literal — JSON keys sort, floats go through one format,
files write in name order — equal inputs give byte-identical runs.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import numpy as np

import precis_geom
from precis.pcb.topoplace.design import ObjectiveShares, TopoDesign
from precis.pcb.topoplace.pairmap import PairMap
from precis.pcb.topoplace.pins import PinAssignment
from precis.pcb.topoplace.placer import TopoPlaceResult
from precis.pcb.topoplace.summary import crossing_info
from precis.pcb.topoplace.writeback import lock_payload

#: ``manifest.json``'s schema tag — bump on any shape change.
RUN_SCHEMA = "precis.topo-run/1"
#: Kernel version stamped on every manifest: bump when the solver's
#: output changes for equal inputs (the route job's ``CODE_VERSION``
#: convention — a stored run must name the code that made it).
#: 2 — the shove LP runs before legalize (gr515107, phase 3a).
CODE_VERSION = 2

_JSON_FILES = ("manifest.json", "elites.json", "pairs.json", "pins.json")
_TEXT_FILES = ("summary.txt", "placement.svg")


def manifest_parts(d: TopoDesign, graph: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The manifest's ``parts`` provenance rows in ``d.parts`` order —
    Decision 25's categories: an LCSC-backed part records its library
    id, a script-defined one is named as such (JLC gates procurement,
    not this run). Shared by the laptop run and the ``pcb_place`` job."""
    inst = {str(i["refdes"]): i for i in graph.get("instances") or []}
    return [
        {
            "refdes": refdes,
            "lcsc": inst.get(refdes, {}).get("part_lcsc"),
            "source": (
                "lcsc" if inst.get(refdes, {}).get("part_lcsc") else "script-defined"
            ),
        }
        for refdes in d.parts
    ]


def footprint_shas(
    footprints: Mapping[str, Mapping[str, Any]],
) -> dict[str, str]:
    """``{refdes: sha16}`` — the 0c manifest's "footprints read" record."""
    return {
        refdes: hashlib.sha256(
            json.dumps(row, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        for refdes, row in sorted(footprints.items())
    }


def manifest(
    d: TopoDesign,
    result: TopoPlaceResult,
    *,
    objective: ObjectiveShares,
    effort: int,
    board_hash: str,
    parts: list[dict[str, Any]],
    footprints: dict[str, Any],
) -> dict[str, Any]:
    """The ``manifest.json`` payload. ``parts`` is the 0c provenance
    list (one ``{"refdes", "lcsc", "source"}`` row per design part, in
    ``d.parts`` order — the drill-down diff names poses through it);
    ``footprints`` the footprint rows read, ``{refdes: content sha}``."""
    return {
        "schema": RUN_SCHEMA,
        "code_version": CODE_VERSION,
        "wheel_version": precis_geom.__version__,
        "seed": int(result.seed),
        "effort": int(effort),
        "objective": {
            "crossings": float(objective.crossings),
            "ratsnest": float(objective.ratsnest),
            "overlap": float(objective.overlap),
            "spread": float(objective.spread),
            "vias": float(objective.vias),
            "backtrack": float(objective.backtrack),
            "direction": float(objective.direction),
        },
        "board_hash": str(board_hash),
        "parts": parts,
        "footprints": footprints,
        "result": {
            "crossings": int(result.crossings),
            "length_mm": float(result.length),
            "cost": float(result.best_cost),
            "evals": int(result.evals),
            "restarts": int(result.restarts),
            "last_improve_eval": int(result.last_improve_eval),
            # post-pass reports (additive — readers ignore absent keys)
            "legalize": (
                {
                    "resolved": bool(result.legalize.resolved),
                    "rounds": int(result.legalize.rounds),
                    "moved_mm": round(float(result.legalize.moved_mm), 4),
                    "remaining": len(result.legalize.remaining),
                }
                if result.legalize is not None
                else None
            ),
            "shove": (
                {
                    "resolved": bool(result.shove.resolved),
                    "rounds": int(result.shove.rounds),
                    "moved_mm": round(float(result.shove.moved_mm), 4),
                    "remaining": len(result.shove.remaining),
                    "reason": str(result.shove.reason),
                    "mesh_errors": int(result.shove.mesh_errors),
                    "mesh_error": str(result.shove.mesh_error),
                }
                if result.shove is not None
                else None
            ),
        },
    }


def pairs_json(d: TopoDesign, pm: PairMap) -> dict[str, Any]:
    """The ``pairs.json`` payload — every stored pair with refdes names
    plus the board-level stats the ``part='pairs'`` drill-down renders."""
    st = pm.stats()
    rows = [
        {
            "a": d.parts[s.a],
            "b": d.parts[s.b],
            "strength": s.strength,
            "best_rot": [int(s.best[0]), int(s.best[1])],
            "best_crossings": int(s.best_crossings),
        }
        for (a, b), s in sorted(pm.pairs.items())
    ]
    return {
        "stats": {
            "pairs": len(pm.pairs),
            "mean": st.mean,
            "p95": st.p95,
            "worst": [
                {"a": d.parts[s.a], "b": d.parts[s.b], "crossings": s.best_crossings}
                for s in st.worst
            ],
        },
        "pairs": rows,
    }


def _heat(strength: float, max_strength: float) -> str:
    """Pair-strength → wire colour: cool grey → hot red, fixed ramp."""
    t = min(max(strength / max(max_strength, 1e-9), 0.0), 1.0)
    r = round(0x6B + (0xE0 - 0x6B) * t)
    g = round(0x70 + (0x20 - 0x70) * t)
    b = round(0x76 + (0x20 - 0x76) * t)
    return f"#{r:02x}{g:02x}{b:02x}"


def _overlap_refdes(d: TopoDesign, pose: np.ndarray) -> set[str]:
    """Refdes of every part in a same-side courtyard collision — the
    figure's red-fill set. Computed pairwise (not read off the legalize
    report) so the mark reflects the pose actually drawn."""
    from precis.pcb.topoplace.proxy import (
        _EPS,
        _collides,
        _ring_geom,
        through_hole_parts,
    )

    n = len(d.parts)
    th = through_hole_parts(d)
    sides = np.asarray(pose)[:, 3] >= 0.5
    geoms = [_ring_geom(d, pose[i], i) for i in range(n)]
    out: set[str] = set()
    for i, gi in enumerate(geoms):
        if gi is None:
            continue
        x0, y0, x1, y1 = gi.bounds
        for j in range(i + 1, n):
            gj = geoms[j]
            if gj is None or not _collides(sides, th, i, j):
                continue
            bx0, by0, bx1, by1 = gj.bounds
            if x1 < bx0 or bx1 < x0 or y1 < by0 or by1 < y0:
                continue
            if float(gi.intersection(gj).area) > _EPS:
                out.update((str(d.parts[i]), str(d.parts[j])))
    return out


def placement_svg(d: TopoDesign, result: TopoPlaceResult, pm: PairMap) -> str:
    """``placement.svg`` — the best pose's MST airwires coloured by pair
    strength (the heatmap), each proper crossing marked, parts as their
    courtyard outlines through the shared figure
    (:func:`precis.pcb.topoplace.render.courtyard_figure`): bottom-side
    orange, same-side overlap filled red, the outline behind."""
    from precis.pcb.topoplace.proxy import local_to_world
    from precis.pcb.topoplace.render import courtyard_figure
    from precis.pcb.topoplace.summary import _wire_model

    wires, ends, _nets = _wire_model(d, result.best_pose)
    cross_pts, _counts = crossing_info(d, result.best_pose)
    max_str = max((s.strength for s in pm.pairs.values()), default=1.0)

    def underlay(fmt: Any) -> list[str]:
        out: list[str] = []
        for k, w in enumerate(wires):
            a, b = ends[k]
            key = (a, b) if a <= b else (b, a)
            strength = pm.pairs[key].strength if key in pm.pairs else 0.0
            out.append(
                f'<line x1="{fmt(w[0])}" y1="{fmt(w[1])}" x2="{fmt(w[2])}" '
                f'y2="{fmt(w[3])}" stroke="{_heat(strength, max_str)}" '
                'stroke-width="0.25" stroke-opacity="0.8"/>'
            )
        for x, y in cross_pts:
            out.append(
                f'<circle cx="{fmt(x)}" cy="{fmt(y)}" r="0.6" fill="none" '
                'stroke="#d00000" stroke-width="0.35"/>'
            )
        return out

    pose = np.asarray(result.best_pose, dtype=np.float64)
    rings = [
        (
            local_to_world(np.asarray(d.part_outline[i], dtype=np.float64), pose[i])
            if i < len(d.part_outline) and len(d.part_outline[i]) >= 4
            else None
        )
        for i in range(len(d.parts))
    ]
    return courtyard_figure(
        rings,
        refdes=[str(p) for p in d.parts],
        bottom=[bool(v) for v in pose[:, 3] >= 0.5],
        overlap=_overlap_refdes(d, pose),
        outline=d.outline if len(d.outline) >= 3 else None,
        centers=pose[:, :2],
        underlay=underlay,
    )


def run_files(
    d: TopoDesign,
    result: TopoPlaceResult,
    assignment: PinAssignment,
    pm: PairMap,
    *,
    summary: str,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    """The full run payload — ``{filename: parsed-json | text}``. The
    job meta stores it verbatim; :func:`write_run` serializes it."""
    return {
        "manifest.json": manifest_payload,
        "elites.json": json.loads(result.elites_json()),
        "pairs.json": pairs_json(d, pm),
        "pins.json": lock_payload(d, assignment),
        "summary.txt": summary,
        "placement.svg": placement_svg(d, result, pm),
    }


def write_run(files: dict[str, Any], out_dir: str | Path) -> list[str]:
    """Write a :func:`run_files` payload under ``out_dir`` — JSON files
    indent-1 sorted-keys with a trailing newline (``write_lock``'s
    convention), text files verbatim, in sorted name order. Returns the
    written filenames."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name in sorted(files):
        payload = files[name]
        if name in _JSON_FILES:
            text = json.dumps(payload, indent=1, sort_keys=True) + "\n"
        else:
            text = str(payload)
        (out / name).write_text(text, encoding="utf-8")
        written.append(name)
    return written


__all__ = [
    "CODE_VERSION",
    "RUN_SCHEMA",
    "footprint_shas",
    "manifest",
    "manifest_parts",
    "pairs_json",
    "placement_svg",
    "run_files",
    "write_run",
]
