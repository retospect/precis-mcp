"""Structures tab — a browser view over the ``structure`` kind.

The structure kind is otherwise a text/MCP surface: the LLM authors atoms +
bonds as typed ops and reads them as an ASCII graph, never pixels (that is the
whole point of the IR). This route is the *human* affordance on top of the same
data — for the person who wants to actually **see** the cell rotate and read the
compute history.

* ``GET /structure`` — the design list (atoms / runs / latest energy).
* ``GET /structure/{slug}`` — one design: an interactive 3D cell viewer
  (initial vs DFT-relaxed geometry) beside the **run-cube** panel — every
  fidelity-ladder pass with its energy, forces, and the content-addressed
  ``cache_key`` that makes an identical relax a zero-compute hit (§23.16).
* ``GET /structure/{slug}/run/{run_id}`` — one run of the cube, explained:
  the rung and what that rung actually computes, the numbers with their
  caveats, the convergence curve, and where the numbers came from (computed
  here / on the compute node / reused from cache / imported). The hashes live
  here, folded away — they are machine identity, not something a reader needs.
* ``POST /structure/{slug}/relax`` — the run-cube "Relax" button: run a
  chosen rung (clean/ml/dft) with default params. An energy rung with no
  local backend dispatches a ``struct_relax`` job to the GPU node, parented
  on the structure — no todo required.

The 3D view is interactive: atoms are coloured by element and clickable (label /
element / position / coordination / constraint), and the **authoritative** bond
graph — declared bonds, or the inferred covalent bonds for a raw cell — is drawn
as clickable cylinders carrying order / kind / provenance / length, not left to
3Dmol's distance heuristic. Geometry is pushed to the vendored-by-CDN 3Dmol.js;
the unit cell is the dashed wireframe from the lattice.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
from typing import TYPE_CHECKING, Any

import numpy as np
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.structure import paper_provenance_rows
from precis.structure import evaluate_measure
from precis.structure.cache import apply_geometry
from precis.structure.elements import covalent_radius
from precis.structure.probe import coordination, detect_bonds
from precis.structure.scene import FIX_X, FIX_Y, FIX_Z
from precis_web.deps import await_dispatch, get_runtime, get_store, templates
from precis_web.timefmt import ago as _ago

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["structure"])

log = logging.getLogger(__name__)

#: Cap the design list — this is a browse surface, not an export.
_LIST_LIMIT = 100

#: CPK / Jmol-ish element colours (hex) so the 3D view + legend read like a
#: chemist expects. Covers the structure atomistic IR palette + common neighbours; unknown
#: elements fall back to a loud pink so a typo is obvious, not silently grey.
_CPK: dict[str, str] = {
    "H": "#e6e6e6", "He": "#d9ffff", "B": "#ffb5b5", "C": "#404040",
    "N": "#3050f8", "O": "#ff0d0d", "F": "#90e050", "Si": "#f0c8a0",
    "P": "#ff8000", "S": "#dcdc28", "Cl": "#1ff01f", "Ni": "#50d050",
    "Cu": "#c88033", "Pd": "#006985", "Pt": "#d0d0e0", "Au": "#ffd123",
}  # fmt: skip
_CPK_DEFAULT = "#ff2fa0"


def _element_color(element: str) -> str:
    return _CPK.get(element, _CPK_DEFAULT)


def _fixed_str(fixed: int) -> str:
    """Human-readable constraint flags — ``free`` or e.g. ``fixed xz``."""
    if not fixed:
        return "free"
    axes = "".join(
        ax for bit, ax in ((FIX_X, "x"), (FIX_Y, "y"), (FIX_Z, "z")) if fixed & bit
    )
    return f"fixed {axes}"


def _list_rows(store: Store) -> list[dict[str, Any]]:
    """Live structure designs, newest first, with atom / run counts and the
    most-recent successful energy (a one-glance ladder summary)."""
    sql = """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               COALESCE((r.meta->>'version')::int, 0)          AS version,
               (SELECT count(*) FROM struct_atoms a
                 WHERE a.ref_id = r.ref_id
                   AND a.retired_version IS NULL)              AS n_atoms,
               (SELECT count(*) FROM struct_runs sr
                 WHERE sr.ref_id = r.ref_id)                   AS n_runs,
               (SELECT sr.energy FROM struct_runs sr
                 WHERE sr.ref_id = r.ref_id
                   AND sr.status = 'succeeded'
                   AND sr.energy IS NOT NULL
                 ORDER BY sr.id DESC LIMIT 1)                  AS last_energy,
               (SELECT sr.fidelity FROM struct_runs sr
                 WHERE sr.ref_id = r.ref_id
                   AND sr.status = 'succeeded'
                   AND sr.energy IS NOT NULL
                 ORDER BY sr.id DESC LIMIT 1)                  AS last_fidelity,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'structure'
           AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (_LIST_LIMIT,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "ref_id": int(r[0]),
                "slug": r[1],
                "title": r[2] or r[1],
                "version": int(r[3]),
                "n_atoms": int(r[4]),
                "n_runs": int(r[5]),
                "last_energy": float(r[6]) if r[6] is not None else None,
                "last_fidelity": r[7],
                "updated": _ago(r[8]),
            }
        )
    return out


#: Plain-English gloss per fidelity rung — the mouseover behind every rung
#: chip, the Relax picker's options, and the run page's "what ran" line. Keyed
#: by the recorded ``fidelity`` string; :func:`rung_info` widens a ``dft-fast``
#: / ``dft-tight`` into the ``dft`` family rather than rendering it bare.
RUNG_INFO: dict[str, dict[str, str]] = {
    "clean": {
        "label": "geometry repair",
        "where": "runs here, seconds",
        "blurb": (
            "Geometry repair, no physics: atoms are nudged apart until nothing "
            "overlaps and no bond is shorter than the two atoms allow. It has "
            "no energy at all — that is why Energy reads an em dash, not 0."
        ),
    },
    "emt": {
        "label": "cheap approximate physics",
        "where": "runs here, seconds",
        "blurb": (
            "Effective-medium theory (ASE EMT) — a real but rough energy and "
            "forces, for the closed set Al/Ni/Cu/Pd/Ag/Pt/Au plus H/C/N/O. "
            "Good enough to shake out a bad geometry before paying for GPU."
        ),
    },
    "ml": {
        "label": "machine-learned potential",
        "where": "runs on the GPU node",
        "blurb": (
            "A machine-learned interatomic potential (MACE-MP-0 / CHGNet) "
            "predicts the forces a DFT calculation would give, thousands of "
            "times faster. The standard pre-relax before any real DFT."
        ),
    },
    "ff": {
        "label": "classical force field",
        "where": "rented compute",
        "blurb": (
            "A classical force field — fixed bonded/non-bonded terms, no "
            "electrons. Fast and only as good as its parameters for these atoms."
        ),
    },
    "xtb": {
        "label": "semi-empirical quantum",
        "where": "rented compute",
        "blurb": (
            "Semi-empirical tight binding (xTB) — approximate electronic "
            "structure: between a force field and DFT in both cost and trust."
        ),
    },
    "dft": {
        "label": "density-functional theory",
        "where": "runs on the GPU node",
        "blurb": (
            "Density-functional theory — actually solves for the electrons to "
            "get the energy and forces. The slow, trustworthy rung: minutes to "
            "hours, and the number other people's DFT can be compared against."
        ),
    },
}

#: Fallback gloss for a rung we have no entry for (a newly added backend) —
#: better an honest "unknown rung" than a chip with no explanation.
_RUNG_UNKNOWN: dict[str, str] = {
    "label": "unrecognised rung",
    "where": "",
    "blurb": "No description on file for this fidelity rung.",
}


def rung_info(fidelity: str | None) -> dict[str, str]:
    """Gloss for a recorded ``fidelity``, widening ``dft-fast`` → ``dft``."""
    key = (fidelity or "").strip().lower()
    if key in RUNG_INFO:
        return RUNG_INFO[key]
    family = key.split("-", 1)[0]
    return RUNG_INFO.get(family, _RUNG_UNKNOWN)


#: Mouseover text for the numbers a run reports. The run-cube is the one place
#: on the site where a reader meets eV and eV/Å, so every column says what it
#: means and — the part that actually bites — what it may *not* be compared to.
METRIC_HELP: dict[str, str] = {
    "energy": (
        "Total energy of the whole cell, in eV. Only meaningful against another "
        "run of the same rung on the same atoms — lower is more stable. An ml "
        "energy and a DFT energy are different scales; never subtract them."
    ),
    "max_force": (
        "The largest force still pulling on any single atom, in eV/Å — the "
        "'is it settled?' number. A relax stops once it drops under its "
        "threshold (~0.05 eV/Å); a big value means the geometry is still moving."
    ),
    "steps": "How many optimiser steps the relax took before it stopped.",
    "converged": (
        "The relax reached its force threshold instead of running out of steps. "
        "A non-converged geometry is still informative, but its energy is not a "
        "minimum — treat it as a snapshot, not a result."
    ),
    "cached": (
        "This exact relax — same geometry, same rung, same settings — had "
        "already been run, here or on another design, so the stored result was "
        "reused. No compute was spent."
    ),
    "external": (
        "Imported numbers: the energy came from an outside dataset (e.g. "
        "Materials Project / OC20), not from a relax we ran."
    ),
    "model": "The specific potential or code that produced these numbers.",
    "on_version": (
        "The design version the run started from. Editing the design bumps the "
        "version, so an older run describes older atoms."
    ),
    "status": (
        "succeeded = the backend finished and returned numbers · running = in "
        "flight · failed = it stopped without a usable result."
    ),
    "cache_key": (
        "Content-addressed over (structure_sha, fidelity, model, params, "
        "code version). An identical relax on any design hits this key and "
        "costs nothing."
    ),
    "structure_sha": (
        "Fingerprint of the exact geometry that went in — the atoms, positions "
        "and cell this run actually saw."
    ),
}

#: What a run-cube row *is*, said once. Reaction barriers are a different
#: animal that lives on the quest's pathway runs, and conflating the two is the
#: easiest mistake to make on this page.
RUN_KIND_BLURB = (
    "Every row here is one relax: the atoms were moved downhill until the "
    "forces on them were small, and the settled geometry + energy recorded. A "
    "relax is not a reaction — it computes no barrier and no rate. Those come "
    "from pathway runs, which hang off the quest, not off this design."
)

#: The ``?`` popover: what each region of the structure page is for.
PAGE_HELP: tuple[tuple[str, str], ...] = (
    (
        "Cell",
        "The design's atoms and bonds in 3D. Click an atom or bond to inspect "
        "it; once a relax has landed you can flip between the input geometry, "
        "the relaxed one, and an overlay of the two.",
    ),
    (
        "Compute runs",
        "The design's compute history, newest first — one row per relax, with "
        "the rung it ran at and what it found. Open a row to see what that run "
        "actually did. Relax ▸ starts a new one.",
    ),
    (
        "Further instructions",
        "Describe a change in plain English; an LLM proposes the concrete edit "
        "ops. Nothing is applied until you review and accept, and accepting "
        "makes a new design rather than overwriting this one.",
    ),
    (
        "Eyes & measures",
        "Named distances, angles and marked sites saved on the design, "
        "re-evaluated live against the current geometry. Hover a row to glow "
        "the atoms it covers.",
    ),
    (
        "Lineage & provenance",
        "Where this design came from: the designs it was derived from or "
        "spawned, the quest it is a candidate for, and the papers that "
        "motivated it.",
    ),
)


def _pending_jobs(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """In-flight ``struct_relax`` jobs for this design — the compute-lane jobs
     parented on the structure whose ``STATUS`` is not yet
    ``succeeded`` (a succeeded relax has sunk a ``struct_runs`` row, so it shows
    in :func:`_run_rows` instead). This is what makes a just-dispatched relax
    *visible* before the GPU node finishes — the run-cube has no row yet."""
    sql = """
        SELECT r.ref_id, r.meta, t.value, r.created_at
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
         WHERE r.parent_id = %s
           AND r.kind = 'job'
           AND r.retired_at IS NULL
           AND r.meta->>'job_type' = 'struct_relax'
           AND t.value IN ('queued', 'running', 'failed')
         ORDER BY r.ref_id DESC
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ref_id,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        params = (r[1] or {}).get("params", {})
        fidelity = params.get("fidelity", "?")
        out.append(
            {
                "job_id": int(r[0]),
                "fidelity": fidelity,
                "rung_help": rung_info(fidelity)["blurb"],
                "status": r[2],
                "created": _ago(r[3]),
            }
        )
    return out


def _run_count(store: Store, ref_id: int) -> int:
    """Total recorded runs for this design — the poll's reload trigger (a
    dispatched relax has *landed* once this grows)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM struct_runs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return int(row[0]) if row else 0


def _run_rows(store: Store, ref_id: int) -> list[dict[str, Any]]:
    """The design's compute history with the §23.16 cache columns the MCP
    ``view='runs'`` table omits (``cache_key`` / ``structure_sha``)."""
    sql = """
        SELECT id, fidelity, status, model, on_version, converged,
               n_steps, energy, max_force, max_disp, cache_key,
               structure_sha, final_geometry, created_at,
               provenance, params
          FROM struct_runs
         WHERE ref_id = %s
         ORDER BY id DESC
         LIMIT 50
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (ref_id,)).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": int(r[0]),
                "fidelity": r[1],
                "status": r[2],
                "model": r[3],
                "on_version": int(r[4]),
                "converged": bool(r[5]),
                "n_steps": int(r[6]),
                "energy": float(r[7]) if r[7] is not None else None,
                "max_force": float(r[8]) if r[8] is not None else None,
                "max_disp": float(r[9]) if r[9] is not None else None,
                "cache_key": r[10],
                "structure_sha": r[11],
                "final_geometry": r[12],
                "created": _ago(r[13]),
                "provenance": r[14] or "computed",
                # A cache *hit* still records a per-design row (the cube is
                # append-only) — ``params.cached`` is what marks it as free.
                "cached": bool((r[15] or {}).get("cached")),
                "rung_help": rung_info(r[1])["blurb"],
                "rung_label": rung_info(r[1])["label"],
            }
        )
    return out


def _run_detail(store: Store, ref_id: int, run_id: int) -> dict[str, Any] | None:
    """One run, everything it recorded — the per-run page's payload.

    ``ref_id`` is part of the lookup, not a post-hoc check: a run id belonging
    to a *different* design must 404 here rather than render under this
    design's slug. Returns ``None`` when there is no such run on this design.
    """
    sql = """
        SELECT id, fidelity, status, model, on_version, converged,
               n_steps, energy, max_force, max_disp, cache_key,
               structure_sha, created_at, provenance, params, method,
               (final_geometry IS NOT NULL) AS has_geometry,
               (forces IS NOT NULL)         AS has_forces
          FROM struct_runs
         WHERE id = %s AND ref_id = %s
    """
    with store.pool.connection() as conn:
        row = conn.execute(sql, (run_id, ref_id)).fetchone()
        if row is None:
            return None
        # The convergence curve: max_force per optimiser step (§6.9 stores the
        # curve + the final state, never a geometry per frame).
        curve = [
            float(c[0])
            for c in conn.execute(
                "SELECT max_force FROM struct_frames "
                "WHERE run_id = %s AND max_force IS NOT NULL ORDER BY step",
                (run_id,),
            ).fetchall()
        ]
        # The dispatched-to-the-GPU-node case: the worker stamps the run it
        # sank onto the job's meta, and the job parents on this structure — so
        # the reverse link is an indexed parent_id lookup, not a meta scan.
        job = conn.execute(
            "SELECT r.ref_id, "
            "       (SELECT t.value FROM ref_tags rt JOIN tags t "
            "          ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS' "
            "         WHERE rt.ref_id = r.ref_id LIMIT 1) "
            "  FROM refs r "
            " WHERE r.parent_id = %s AND r.kind = 'job' "
            "   AND r.retired_at IS NULL "
            "   AND r.meta->>'struct_run_id' = %s "
            " ORDER BY r.ref_id DESC LIMIT 1",
            (ref_id, str(run_id)),
        ).fetchone()

    info = rung_info(row[1])
    params = dict(row[14] or {})
    provenance = row[13] or "computed"
    cached = bool(params.get("cached"))
    return {
        "id": int(row[0]),
        "fidelity": row[1],
        "status": row[2],
        "model": row[3],
        "on_version": int(row[4]),
        "converged": bool(row[5]),
        "n_steps": int(row[6]),
        "energy": float(row[7]) if row[7] is not None else None,
        "max_force": float(row[8]) if row[8] is not None else None,
        "max_disp": float(row[9]) if row[9] is not None else None,
        "cache_key": row[10],
        "structure_sha": row[11],
        "created": _ago(row[12]),
        "provenance": provenance,
        "params": params,
        "method": dict(row[15] or {}),
        "has_geometry": bool(row[16]),
        "has_forces": bool(row[17]),
        "cached": cached,
        "rung_label": info["label"],
        "rung_help": info["blurb"],
        "rung_where": info["where"],
        "origin": _run_origin(provenance, cached, bool(job)),
        "job_id": int(job[0]) if job else None,
        "job_status": (job[1] if job else None),
        "curve": curve,
        "curve_svg": _curve_svg(curve),
    }


def _run_origin(provenance: str, cached: bool, has_job: bool) -> str:
    """One sentence on where these numbers came from — the question a reader
    asks before trusting them."""
    if provenance == "external":
        return METRIC_HELP["external"]
    if cached:
        return METRIC_HELP["cached"]
    if has_job:
        return "Computed for this design as a job on the compute node."
    return "Computed for this design, locally, when the relax op ran."


#: Sparkline geometry — a glance at the convergence curve, not a plot.
_CURVE_W, _CURVE_H = 240.0, 40.0


def _curve_svg(curve: list[float]) -> str:
    """The convergence curve as SVG polyline points (max force per step,
    scaled into :data:`_CURVE_W` x :data:`_CURVE_H`). Empty for a curve too
    short to say anything — one point is a dot, not a trend."""
    if len(curve) < 2:
        return ""
    hi = max(curve)
    lo = min(curve)
    span = (hi - lo) or 1.0
    step = _CURVE_W / (len(curve) - 1)
    pts = [
        f"{i * step:.1f},{_CURVE_H - (v - lo) / span * _CURVE_H:.1f}"
        for i, v in enumerate(curve)
    ]
    return " ".join(pts)


#: Pauling bond-order decay constant (Å) — ``s = exp((R0-d)/0.37)`` reads
#: s≈1 at the sum-of-covalent-radii ideal single-bond distance, decaying
#: smoothly as the pair stretches past it.
_BOND_ORDER_DECAY = 0.37

#: Drop a neighbour pair once its bond order decays below this — anything
#: weaker isn't a meaningful interaction to list.
_NEIGHBOR_MIN_S = 0.10

#: Cap the ranked neighbour list per atom — a click panel, not a full
#: distance matrix.
_NEIGHBOR_CAP = 8

#: Skip the O(n²) neighbour pass entirely above this atom count — a pathway
#: page runs it per state (up to ~16 scenes/render), so an outsized supercell
#: must degrade to empty lists, not stall the server.
_NEIGHBOR_MAX_ATOMS = 500


def _atom_neighbors(scene: Any, atoms: list[dict[str, Any]]) -> None:
    """Populate each atom dict's ``neighbors``: every OTHER atom in the scene,
    MIC distance + Pauling bond-order strength ``s = exp((R0-d)/0.37)`` (R0 =
    sum of covalent radii; s≈1 = ideal single bond), kept at ``s >= 0.10``,
    sorted strongest-first, capped at 8. Plain O(n²) pairwise loop, guarded by
    ``_NEIGHBOR_MAX_ATOMS``. Each unordered pair is computed once and appended
    to both sides."""
    by_label = {a["label"]: a for a in atoms}
    labels = list(by_label)
    for a in atoms:
        a["neighbors"] = []
    if len(labels) > _NEIGHBOR_MAX_ATOMS:
        return
    for ai in range(len(labels)):
        li = labels[ai]
        ei = by_label[li]["element"]
        for bj in range(ai + 1, len(labels)):
            lj = labels[bj]
            ej = by_label[lj]["element"]
            d, _img = scene.cell.mic(scene.atoms[li].frac, scene.atoms[lj].frac)
            r0 = covalent_radius(ei) + covalent_radius(ej)
            s = math.exp((r0 - d) / _BOND_ORDER_DECAY)
            if s < _NEIGHBOR_MIN_S:
                continue
            by_label[li]["neighbors"].append(
                {
                    "label": lj,
                    "element": ej,
                    "d": round(float(d), 3),
                    "s": round(float(s), 2),
                }
            )
            by_label[lj]["neighbors"].append(
                {
                    "label": li,
                    "element": ei,
                    "d": round(float(d), 3),
                    "s": round(float(s), 2),
                }
            )
    for a in atoms:
        a["neighbors"].sort(key=lambda nb: nb["s"], reverse=True)
        del a["neighbors"][_NEIGHBOR_CAP:]


def _geom_payload(scene: Any, comment: str) -> dict[str, Any]:
    """One geometry → everything the client needs to draw + interrogate it:

    * ``xyz`` — plain Cartesian XYZ for the 3Dmol model (atom order == the
      ``atoms`` list order, so a clicked atom's model index maps straight back).
    * ``atoms`` — per-atom detail (label / element / frac + cart / constraint /
      magmom / oxidation / hybridization / coordination / colour / covalent
      radius (``r_cov``, so the client can recompute a bond's length +
      Pauling strength against any other atom, even one not in this
      geometry's own bond graph) / ranked ``neighbors`` — see
      :func:`_atom_neighbors`).
    * ``bonds`` — the **authoritative** graph (declared bonds if any, else the
      inferred covalent bonds), each with its two endpoints in Cartesian Å (in
      the bond's periodic image) so we draw the real edge, not a distance guess.
    * ``lattice`` — the 3×3 cell, for the wireframe box.
    """
    lattice = [[float(v) for v in row] for row in np.asarray(scene.cell.lattice)]
    atoms: list[dict[str, Any]] = []
    for idx, a in enumerate(scene.atoms.values()):
        cart = scene.cell.frac_to_cart(a.frac)
        atoms.append(
            {
                "index": idx,
                "label": a.label,
                "element": a.element,
                "frac": [round(float(x), 4) for x in a.frac],
                "cart": [float(x) for x in cart],
                "fixed": _fixed_str(a.fixed),
                "magmom": a.magmom,
                "oxidation": a.oxidation,
                "hybridization": a.hybridization,
                "coordination": coordination(scene, a.label),
                "color": _element_color(a.element),
                "r_cov": round(float(covalent_radius(a.element)), 3),
            }
        )
    _atom_neighbors(scene, atoms)

    # Authoritative graph: prefer declared bonds; fall back to auto-detected so
    # a raw cell still shows (and can be clicked) — marked by ``provenance``.
    bonds_src = scene.bonds if scene.bonds else detect_bonds(scene)
    bonds: list[dict[str, Any]] = []
    for b in bonds_src:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        pi = scene.cell.frac_to_cart(scene.atoms[b.i].frac)
        pj = scene.cell.frac_to_cart(scene.atoms[b.j].frac + np.array(b.image))
        bonds.append(
            {
                "i": b.i,
                "j": b.j,
                "order": float(b.order),
                "kind": b.kind,
                "provenance": b.provenance,
                "image": [int(x) for x in b.image],
                "length": round(float(np.linalg.norm(pj - pi)), 3),
                "start": [float(x) for x in pi],
                "end": [float(x) for x in pj],
            }
        )

    lines = [str(len(atoms)), comment]
    for a_dict in atoms:
        x, y, z = a_dict["cart"]
        lines.append(f"{a_dict['element']} {x:.6f} {y:.6f} {z:.6f}")
    return {
        "xyz": "\n".join(lines) + "\n",
        "atoms": atoms,
        "bonds": bonds,
        "lattice": lattice,
    }


def _viewer(
    store: Store,
    ref: Any,
    runs: list[dict[str, Any]],
    *,
    pin_run_id: int | None = None,
) -> dict[str, Any]:
    """Build the 3D viewer payload: the input geometry, the optional relaxed
    geometry (newest succeeded run carrying a ``final_geometry``), and a colour
    legend. Each geometry carries its own atoms/bonds/lattice.

    ``pin_run_id`` shows *that* run's geometry instead of the newest one — the
    per-run page's "show this geometry in the viewer" link. A pin naming a run
    with no stored geometry simply leaves the relaxed side empty (the page then
    reads as input-only), rather than silently showing a different run's atoms.
    """
    scene, _handles = store.structure_load(ref.id)
    initial = _geom_payload(scene, f"{ref.slug} (input)")

    relaxed: dict[str, Any] | None = None
    relaxed_run_id: int | None = None
    for run in runs:  # newest-first
        if pin_run_id is not None and int(run["id"]) != pin_run_id:
            continue
        geom = run.get("final_geometry")
        if run["status"] == "succeeded" and geom:
            apply_geometry(scene, geom)  # mutate to the relaxed positions
            relaxed = _geom_payload(scene, f"{ref.slug} (relaxed r{run['id']})")
            relaxed_run_id = int(run["id"])
            break

    legend: dict[str, dict[str, Any]] = {}
    for a in initial["atoms"]:
        slot = legend.setdefault(
            a["element"],
            {"element": a["element"], "color": a["color"], "count": 0, "labels": []},
        )
        slot["count"] += 1
        slot["labels"].append(a["label"])

    # "What moved" — per-atom Cartesian displacement input→relaxed, so a change
    # list can hover-highlight the atoms that actually shifted (the same
    # text→viewer highlight the proposed-ops list will reuse).
    moved: list[dict[str, Any]] = []
    if relaxed is not None:
        init_cart = {a["label"]: a["cart"] for a in initial["atoms"]}
        for a in relaxed["atoms"]:
            ic = init_cart.get(a["label"])
            if ic is None:
                continue
            delta = math.dist(ic, a["cart"])
            if delta > 1e-6:
                moved.append(
                    {
                        "label": a["label"],
                        "element": a["element"],
                        "delta": round(delta, 3),
                    }
                )
        moved.sort(key=lambda m: m["delta"], reverse=True)

    return {
        "initial": initial,
        "relaxed": relaxed,
        "relaxed_run_id": relaxed_run_id,
        "legend": sorted(legend.values(), key=lambda d: d["element"]),
        "moved": moved,
        "n_atoms": len(initial["atoms"]),
    }


def _markers(scene: Any) -> list[dict[str, Any]]:
    """The design's eyes + measures, each live-evaluated, shaped for the panel
    + the viewer overlay (``operands`` become the ``data-atoms`` hover targets)."""
    out: list[dict[str, Any]] = []
    for m in scene.measures:
        value, verdict = evaluate_measure(scene, m)
        if m.kind == "eye":
            shown = value.get("error") or f"touches {len(value.get('touch', []))}"
        elif "error" in value:
            shown = value["error"]
        else:
            unit = value.get("unit") or ""
            shown = f"{value.get('value')}{(' ' + unit) if unit else ''}"
        out.append(
            {
                "kind": m.kind,
                "is_eye": m.kind == "eye",
                "label": m.name or m.kind,
                "operands": m.operands,
                "for": m.for_,
                "value": str(shown),
                "verdict": verdict,
            }
        )
    return out


def _slug_of(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'cite_key' ORDER BY created_at DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _lineage(store: Store, ref_id: int) -> dict[str, list[dict[str, str]]]:
    """Parents (this design is ``derived-from`` them) + children (derived from
    this one), for the lineage section — the same shape as _followup_discussions."""
    parents: list[dict[str, str]] = []
    for lnk in store.links_for(ref_id, direction="out", relation="derived-from"):
        s = _slug_of(store, lnk.dst_ref_id)
        if s:
            parents.append({"slug": s})
    children: list[dict[str, str]] = []
    for lnk in store.links_for(ref_id, direction="in", relation="derived-from"):
        s = _slug_of(store, lnk.src_ref_id)
        if s:
            children.append({"slug": s})
    return {"parents": parents, "children": children}


def _quest_context(store: Store, ref_id: int) -> dict[str, Any] | None:
    """Quest-candidate context, or ``None`` for a design outside any quest.

    ``quests`` — the quest(s) this design ``serves`` (the candidate→quest
    link :func:`precis.quest.compute` mints), each a backlink to the quest
    page. ``pathways`` — the design's autocatpath pathway runs (the barrier
    compute pages), keyed on the pathway's own ``meta.candidate_ref`` stamp,
    newest first, with tier + rate-limiting barrier when present.
    """
    quests: list[dict[str, Any]] = []
    for lnk in store.links_for(ref_id, direction="out", relation="serves"):
        other = store.fetch_refs_by_ids([lnk.dst_ref_id]).get(lnk.dst_ref_id)
        if other is not None and other.kind == "quest":
            quests.append(
                {"ref_id": other.id, "title": other.title or f"quest {other.id}"}
            )
    pathways: list[dict[str, Any]] = []
    try:
        with store.pool.connection() as conn:
            # barrier fallback chain mirrors refs._pathway_barrier_figure
            # (top-level rate_Ea first, then the meta.results spellings)
            rows = conn.execute(
                "SELECT ref_id, meta->>'tier', "
                "COALESCE(meta->>'rate_Ea', meta->'results'->>'barrier', "
                "meta->'results'->>'rate_Ea', meta->'results'->>'rate_ea', "
                "meta->'results'->>'ea') FROM refs "
                "WHERE kind = 'pathway' AND retired_at IS NULL "
                "AND meta->>'candidate_ref' = %s ORDER BY ref_id DESC",
                (str(ref_id),),
            ).fetchall()
    except Exception:
        log.warning("structure %s: pathway-run query failed", ref_id, exc_info=True)
        rows = []
    for pid, tier, ea in rows:
        try:
            barrier = float(ea) if ea is not None else None
        except (TypeError, ValueError):
            barrier = None
        pathways.append({"ref_id": int(pid), "tier": tier or "neb", "barrier": barrier})
    if not quests and not pathways:
        return None
    return {"quests": quests, "pathways": pathways}


def _latest_proposal(store: Store, ref_id: int) -> dict[str, Any] | None:
    """The newest ``structure_propose`` job for this design — its STATUS + the
    ``job_result`` proposal chunk. Keyed on ``params.structure_ref_id`` so the
    route never has to capture the job id at mint time."""
    sql = """
        SELECT r.ref_id,
               (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                 WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1) AS status,
               (SELECT c.text FROM chunks c
                 WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'job_result'
                 ORDER BY c.ord DESC LIMIT 1)                                  AS result,
               r.created_at
          FROM refs r
         WHERE r.kind = 'job'
           AND r.meta->>'job_type' = 'structure_propose'
           AND (r.meta->'params'->>'structure_ref_id')::int = %s
           AND r.retired_at IS NULL
         ORDER BY r.ref_id DESC LIMIT 1
    """
    with store.pool.connection() as conn:
        row = conn.execute(sql, (ref_id,)).fetchone()
    if row is None:
        return None
    job_id, status, result_text, created = row
    proposal: dict[str, Any] | None = None
    if result_text:
        try:
            proposal = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            proposal = None
    return {
        "job_id": int(job_id),
        "status": status or "queued",
        "proposal": proposal,
        "created": _ago(created),
    }


@router.get("/structure", response_class=HTMLResponse)
async def structure_list(request: Request) -> HTMLResponse:
    """The design list."""
    store = get_store(request)
    rows = _list_rows(store)
    return templates.TemplateResponse(
        request,
        "structure/list.html.j2",
        {"active_tab": "structure", "designs": rows, "total": len(rows)},
    )


@router.get("/structure/{slug}", response_class=HTMLResponse)
async def structure_detail(request: Request, slug: str) -> HTMLResponse:
    """One design: 3D cell viewer + run-cube panel."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    except NotFound:
        return _not_found(request, f"no live structure design with slug {slug!r}")
    runs = _run_rows(store, ref.id)
    pin = _int_or_none(request.query_params.get("run"))
    viewer = _viewer(store, ref, runs, pin_run_id=pin)
    scene, _handles = store.structure_load(ref.id)
    meta = dict(ref.meta or {})
    return templates.TemplateResponse(
        request,
        "structure/detail.html.j2",
        {
            "active_tab": "structure",
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "version": int(meta.get("version", 0)),
            "pbc": list(meta.get("pbc", (True, True, True))),
            "runs": runs,
            "pending": _pending_jobs(store, ref.id),
            "run_count": _run_count(store, ref.id),
            "viewer": viewer,
            "markers": _markers(scene),
            "lineage": _lineage(store, ref.id),
            "provenance": paper_provenance_rows(store, ref.id),
            "proposal": _latest_proposal(store, ref.id),
            "quest_context": _quest_context(store, ref.id),
            "pinned_run": pin,
            "page_help": PAGE_HELP,
            "metric_help": METRIC_HELP,
            "rung_info": RUNG_INFO,
            "run_kind_blurb": RUN_KIND_BLURB,
        },
    )


def _int_or_none(raw: str | None) -> int | None:
    """A query param that must be an int or absent — a junk value is ignored
    (a stale link should degrade to the default view, never 500)."""
    try:
        return int(raw) if raw else None
    except ValueError:
        return None


@router.get("/structure/{slug}/run/{run_id}", response_class=HTMLResponse)
async def structure_run(request: Request, slug: str, run_id: int) -> HTMLResponse:
    """One compute run, in words: which rung ran and what that rung *is*, what
    the numbers mean, whether it settled, and where the numbers came from."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    except NotFound:
        return _not_found(request, f"no live structure design with slug {slug!r}")
    run = _run_detail(store, ref.id, run_id)
    if run is None:
        return _not_found(request, f"design {slug!r} has no compute run r{run_id}")
    return templates.TemplateResponse(
        request,
        "structure/run.html.j2",
        {
            "active_tab": "structure",
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "run": run,
            "metric_help": METRIC_HELP,
            "run_kind_blurb": RUN_KIND_BLURB,
            "curve_w": _CURVE_W,
            "curve_h": _CURVE_H,
            "quest_context": _quest_context(store, ref.id),
        },
    )


def _not_found(request: Request, detail: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": "Not found", "detail": detail, "status": 404},
        status_code=404,
    )


@router.post("/structure/{slug}/instruct")
async def structure_instruct(
    request: Request, slug: str, instruction: str = Form(...)
) -> RedirectResponse:
    """The "Further instructions" box: mint a todo + a ``structure_propose`` job
    so the agent worker proposes ops for this design (propose-only; the human
    applies them separately). Redirects back to the design."""
    store = get_store(request)
    instruction = instruction.strip()
    try:
        ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    except NotFound:
        return RedirectResponse(url="/structure", status_code=303)
    if not instruction:
        return RedirectResponse(url=f"/structure/{slug}", status_code=303)

    # A todo to parent the job (JobHandler.put requires a live todo parent).
    todo_body, err = await await_dispatch(
        request,
        "put",
        {"kind": "todo", "text": f"structure {slug}: {instruction[:200]}"},
    )
    if err:
        return RedirectResponse(url=f"/structure/{slug}", status_code=303)
    m = re.search(r"\btd(\d+)\b", todo_body)
    if m is None:
        return RedirectResponse(url=f"/structure/{slug}", status_code=303)
    todo_id = int(m.group(1))

    await await_dispatch(
        request,
        "put",
        {
            "kind": "job",
            "parent_id": todo_id,
            "job_type": "structure_propose",
            "executor": "claude_inproc",
            "params": {
                "structure_ref_id": ref.id,
                "slug": slug,
                "instruction": instruction,
            },
        },
    )
    return RedirectResponse(url=f"/structure/{slug}#instruct", status_code=303)


@router.get("/structure/{slug}/runs_status")
async def structure_runs_status(request: Request, slug: str) -> JSONResponse:
    """Live run-cube state (the panel polls this): the in-flight ``struct_relax``
    jobs + the total recorded-run count. The client keeps polling while a job is
    ``queued``/``running`` and reloads once ``run_count`` grows (a relax landed)."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "pending": _pending_jobs(store, ref.id),
            "run_count": _run_count(store, ref.id),
        }
    )


@router.get("/structure/{slug}/proposal")
async def structure_proposal(request: Request, slug: str) -> JSONResponse:
    """Poll the latest proposal job for this design (the box polls this)."""
    store = get_store(request)
    try:
        ref = resolve_live_slug_ref(store, kind="structure", id=slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_latest_proposal(store, ref.id) or {"status": None})


@router.post("/structure/{slug}/apply")
async def structure_apply(
    request: Request, slug: str, to: str = Form(...), job_id: int = Form(...)
) -> Any:
    """Apply a proposal: read its ops from the job's ``job_result`` chunk and
    ``derive`` a new design ``to`` from this one (linked derived-from)."""
    store = get_store(request)
    to_slug = to.strip()
    try:
        ref_id = _require_ref(store, slug)
    except NotFound:
        return RedirectResponse(url="/structure", status_code=303)
    proposal = _latest_proposal(store, ref_id)
    ops: list[dict[str, Any]] = []
    if proposal and proposal.get("proposal"):
        ops = list(proposal["proposal"].get("ops") or [])
    if not ops:
        return _apply_error(request, slug, "that proposal has no ops to apply")

    handler = get_runtime(request).hub.handler_for("structure")

    def _do() -> tuple[bool, str]:
        try:
            handler.derive(id=slug, to=to_slug, ops=ops)
            return True, to_slug
        except (BadInput, NotFound) as exc:
            return False, str(exc)

    ok, msg = await asyncio.to_thread(_do)
    if not ok:
        return _apply_error(request, slug, msg)
    return RedirectResponse(url=f"/structure/{to_slug}", status_code=303)


#: Fidelity rungs offered by the run-cube "Relax" button, cheap → correct.
#: ``clean`` repairs geometry locally (instant); ``ml``/``dft`` with no local
#: backend dispatch a ``struct_relax`` job to the GPU node (the intent-vs-compute job lanes — no todo
#: needed). Default params otherwise (steps/model at the op defaults).
_RELAX_RUNGS: tuple[str, ...] = ("clean", "ml", "dft")


@router.post("/structure/{slug}/relax")
async def structure_relax(
    request: Request, slug: str, fidelity: str = Form("dft")
) -> Any:
    """Run a relax at the chosen rung with default params. A local rung runs
    inline; an energy rung with no local backend dispatches a ``struct_relax``
    job to the GPU node (parented on the structure — no todo) and
    returns immediately. The run-cube panel polls the result."""
    fidelity = fidelity.strip() or "dft"
    if fidelity not in _RELAX_RUNGS:
        return _apply_error(
            request, slug, f"unknown fidelity {fidelity!r}; pick one of {_RELAX_RUNGS}"
        )
    try:
        _require_ref(get_store(request), slug)
    except NotFound:
        return RedirectResponse(url="/structure", status_code=303)

    handler = get_runtime(request).hub.handler_for("structure")

    def _do() -> tuple[bool, str]:
        try:
            handler.edit(id=slug, ops=[{"op": "relax", "fidelity": fidelity}])
            return True, ""
        except (BadInput, NotFound, Unsupported) as exc:
            return False, str(exc)

    ok, msg = await asyncio.to_thread(_do)
    if not ok:
        return _apply_error(request, slug, msg)
    return RedirectResponse(url=f"/structure/{slug}#runs", status_code=303)


def _require_ref(store: Store, slug: str) -> int:
    return resolve_live_slug_ref(store, kind="structure", id=slug).id


def _apply_error(request: Request, slug: str, detail: str) -> Any:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": "Apply failed", "detail": detail, "status": 400},
        status_code=400,
    )
