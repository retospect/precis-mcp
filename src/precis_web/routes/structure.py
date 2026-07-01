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

The 3D view is best-effort chrome: geometry is pushed to the vendored-by-CDN
3Dmol.js as plain XYZ (auto-bonded by distance) plus the unit-cell edges drawn
from the lattice. The authoritative bond graph still lives in the MCP probes.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.errors import NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.structure.cache import apply_geometry
from precis_web.deps import get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(tags=["structure"])

log = logging.getLogger(__name__)

#: Cap the design list — this is a browse surface, not an export.
_LIST_LIMIT = 100


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


def _xyz(scene: Any, comment: str) -> str:
    """Scene → plain XYZ (Cartesian, Å). 3Dmol auto-bonds by distance."""
    lines = [str(len(scene.atoms)), comment]
    for a in scene.atoms.values():
        x, y, z = scene.cell.frac_to_cart(a.frac)
        lines.append(f"{a.element} {x:.6f} {y:.6f} {z:.6f}")
    return "\n".join(lines) + "\n"


def _viewer(store: Any, ref: Any, runs: list[dict[str, Any]]) -> dict[str, Any]:
    """Build the 3D viewer payload: initial XYZ, optional relaxed XYZ (from the
    newest succeeded run carrying a ``final_geometry``), and the lattice edges."""
    scene, _handles = store.structure_load(ref.id)
    initial_xyz = _xyz(scene, f"{ref.slug} (input)")
    lattice = [[float(v) for v in row] for row in np.asarray(scene.cell.lattice)]

    relaxed_xyz: str | None = None
    relaxed_run: dict[str, Any] | None = None
    for run in runs:  # newest-first
        geom = run.get("final_geometry")
        if run["status"] == "succeeded" and geom:
            apply_geometry(scene, geom)  # mutate to the relaxed positions
            relaxed_xyz = _xyz(scene, f"{ref.slug} (relaxed r{run['id']})")
            relaxed_run = run
            break
    return {
        "initial_xyz": initial_xyz,
        "relaxed_xyz": relaxed_xyz,
        "relaxed_run_id": relaxed_run["id"] if relaxed_run else None,
        "lattice": lattice,
        "n_atoms": len(scene.atoms),
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
        },
    )
