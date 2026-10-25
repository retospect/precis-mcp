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
CODE_VERSION = 1

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


def placement_svg(d: TopoDesign, result: TopoPlaceResult, pm: PairMap) -> str:
    """``placement.svg`` — the best pose's MST airwires coloured by pair
    strength (the heatmap), each proper crossing marked, parts as
    labelled dots, the outline behind. Hand-rendered (not
    ``precis.pcb.svg``) so the file carries exactly the three layers the
    spec names and stays byte-deterministic."""
    from precis.pcb.topoplace.summary import _wire_model

    wires, ends, _nets = _wire_model(d, result.best_pose)
    cross_pts, _counts = crossing_info(d, result.best_pose)

    pts = [result.best_pose[:, :2]]
    if len(d.outline) >= 3:
        pts.append(d.outline)
    if len(cross_pts):
        pts.append(cross_pts)
    all_pts = np.concatenate([p for p in pts if len(p)])
    finite = np.isfinite(all_pts).all(axis=1)
    lo = all_pts[finite].min(axis=0) if finite.any() else np.zeros(2)
    hi = all_pts[finite].max(axis=0) if finite.any() else np.ones(2) * 10.0
    pad = max(float((hi - lo).max()) * 0.06, 2.0)
    lo, hi = lo - pad, hi + pad
    w_mm, h_mm = float(hi[0] - lo[0]), float(hi[1] - lo[1])

    def fmt(v: float) -> str:
        return f"{v:.3f}".rstrip("0").rstrip(".")

    def X(v: float) -> str:
        return fmt(v - lo[0])

    def Y(v: float) -> str:
        return fmt(v - lo[1])

    max_str = max((s.strength for s in pm.pairs.values()), default=1.0)

    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 '
        f'{fmt(w_mm)} {fmt(h_mm)}" width="960">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
    ]
    if len(d.outline) >= 3:
        poly = " ".join(f"{X(x)},{Y(y)}" for x, y in d.outline)
        out.append(
            f'<polygon points="{poly}" fill="none" stroke="#222222" '
            'stroke-width="0.4"/>'
        )
    for k, w in enumerate(wires):
        a, b = ends[k]
        key = (a, b) if a <= b else (b, a)
        strength = pm.pairs[key].strength if key in pm.pairs else 0.0
        out.append(
            f'<line x1="{X(w[0])}" y1="{Y(w[1])}" x2="{X(w[2])}" '
            f'y2="{Y(w[3])}" stroke="{_heat(strength, max_str)}" '
            'stroke-width="0.25" stroke-opacity="0.8"/>'
        )
    for x, y in cross_pts:
        out.append(
            f'<circle cx="{X(x)}" cy="{Y(y)}" r="0.6" fill="none" '
            'stroke="#d00000" stroke-width="0.35"/>'
        )
    for part, row in enumerate(result.best_pose):
        x, y = float(row[0]), float(row[1])
        if not (np.isfinite(x) and np.isfinite(y)):
            continue
        mark = "#d00000" if bool(d.fixed[part]) else "#222222"
        out.append(
            f'<circle cx="{X(x)}" cy="{Y(y)}" r="0.7" fill="{mark}"/>'
            f'<text x="{X(x)}" y="{Y(y)}" font-size="1.6" fill="#333333" '
            f'text-anchor="middle" dy="-1">{d.parts[part]}</text>'
        )
    out.append("</svg>")
    return "\n".join(out) + "\n"


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
