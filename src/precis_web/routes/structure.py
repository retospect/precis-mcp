"""Structures tab — a browser view over the ``structure`` kind (ADR 0043).

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
from typing import Any

import numpy as np
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.structure import evaluate_measure
from precis.structure.cache import apply_geometry
from precis.structure.probe import coordination, detect_bonds
from precis.structure.scene import FIX_X, FIX_Y, FIX_Z
from precis_web.deps import await_dispatch, get_runtime, get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(tags=["structure"])

log = logging.getLogger(__name__)

#: Cap the design list — this is a browse surface, not an export.
_LIST_LIMIT = 100

#: CPK / Jmol-ish element colours (hex) so the 3D view + legend read like a
#: chemist expects. Covers the ADR 0043 §3 palette + common neighbours; unknown
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


def _list_rows(store: Any) -> list[dict[str, Any]]:
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
           AND r.deleted_at IS NULL
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


def _run_rows(store: Any, ref_id: int) -> list[dict[str, Any]]:
    """The design's compute history with the §23.16 cache columns the MCP
    ``view='runs'`` table omits (``cache_key`` / ``structure_sha``)."""
    sql = """
        SELECT id, fidelity, status, model, on_version, converged,
               n_steps, energy, max_force, max_disp, cache_key,
               structure_sha, final_geometry, created_at
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
            }
        )
    return out


def _geom_payload(scene: Any, comment: str) -> dict[str, Any]:
    """One geometry → everything the client needs to draw + interrogate it:

    * ``xyz`` — plain Cartesian XYZ for the 3Dmol model (atom order == the
      ``atoms`` list order, so a clicked atom's model index maps straight back).
    * ``atoms`` — per-atom detail (label / element / frac + cart / constraint /
      magmom / oxidation / hybridization / coordination / colour).
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
            }
        )

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


def _viewer(store: Any, ref: Any, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the 3D viewer payload: the input geometry, the optional relaxed
    geometry (newest succeeded run carrying a ``final_geometry``), and a colour
    legend. Each geometry carries its own atoms/bonds/lattice."""
    scene, _handles = store.structure_load(ref.id)
    initial = _geom_payload(scene, f"{ref.slug} (input)")

    relaxed: dict[str, Any] | None = None
    relaxed_run_id: int | None = None
    for run in runs:  # newest-first
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
    """The design's cursors + measures, each live-evaluated, shaped for the panel
    + the viewer overlay (``operands`` become the ``data-atoms`` hover targets)."""
    out: list[dict[str, Any]] = []
    for m in scene.measures:
        value, verdict = evaluate_measure(scene, m)
        if m.kind == "cursor":
            shown = value.get("error") or f"touches {len(value.get('touch', []))}"
        elif "error" in value:
            shown = value["error"]
        else:
            unit = value.get("unit") or ""
            shown = f"{value.get('value')}{(' ' + unit) if unit else ''}"
        out.append(
            {
                "kind": m.kind,
                "is_cursor": m.kind == "cursor",
                "label": m.name or m.kind,
                "operands": m.operands,
                "for": m.for_,
                "value": str(shown),
                "verdict": verdict,
            }
        )
    return out


def _slug_of(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'cite_key' ORDER BY created_at DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _lineage(store: Any, ref_id: int) -> dict[str, list[dict[str, str]]]:
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


def _latest_proposal(store: Any, ref_id: int) -> dict[str, Any] | None:
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
           AND r.deleted_at IS NULL
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
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Structure not found",
                "detail": f"no live structure design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    runs = _run_rows(store, ref.id)
    viewer = _viewer(store, ref, runs)
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
            "viewer": viewer,
            "markers": _markers(scene),
            "lineage": _lineage(store, ref.id),
            "proposal": _latest_proposal(store, ref.id),
        },
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


def _require_ref(store: Any, slug: str) -> int:
    return resolve_live_slug_ref(store, kind="structure", id=slug).id


def _apply_error(request: Request, slug: str, detail: str) -> Any:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": "Apply failed", "detail": detail, "status": 400},
        status_code=400,
    )
