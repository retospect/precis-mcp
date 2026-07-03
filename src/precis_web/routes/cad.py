"""CAD tab — a browser 3D viewer + edit-by-prompt over the ``cad`` kind (ADR 0041).

The cad kind is otherwise a text/MCP surface: the LLM authors a parametric solid
as a small design language and *probes* it analytically, never pixels. This route
is the *human* affordance on the same data — see the solid rotate, click a
feature, and edit it by natural-language instruction.

* ``GET  /cad`` — the design list.
* ``GET  /cad/{slug}`` — one design: an interactive 3D viewer (parts coloured,
  cuts translucent) beside an analysis/export panel + the edit-by-prompt box.
* ``GET  /cad/{slug}/model.gltf`` — the viewer/download glTF (features | solid).
* ``GET  /cad/{slug}/export.{fmt}`` — stream a download (scad / stl / 3mf / step).
* ``POST /cad/{slug}/instruct`` — mint a ``cad_propose`` job (the "Propose" box).
* ``GET  /cad/{slug}/proposal`` — poll the latest proposal.
* ``POST /cad/{slug}/apply`` — derive a new design from a proposal (optionally
  soft-deleting the parent).

The mesh the browser sees comes from the *same* IR→tessellate pipeline that feeds
the STL/3MF exporter (:mod:`precis.cad.gltf`), so the view can never drift from
the geometry the probes reason about or the exporter emits.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from precis.cad.bulk import _expr_aabb
from precis.cad.bulk import volume as cad_volume
from precis.cad.dsl import DslError
from precis.cad.dsl import parse as parse_shape
from precis.cad.export import (
    ExportError,
    export_mesh,
    export_step,
    manifold_available,
    step_available,
    to_openscad,
)
from precis.cad.gltf import component_colors, solid_available, to_glb
from precis.cad.relate import clearance as cad_clearance
from precis.cad.scene import build_design
from precis.errors import BadInput, NotFound
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis_web.deps import await_dispatch, get_runtime, get_store, templates
from precis_web.timefmt import ago as _ago

router = APIRouter(tags=["cad"])

log = logging.getLogger(__name__)

#: Cap the design list — this is a browse surface, not an export.
_LIST_LIMIT = 100

#: Streamed-export content types.
_EXPORT_MEDIA = {
    "scad": "text/plain; charset=utf-8",
    "stl": "model/stl",
    "3mf": "model/3mf",
    "step": "application/step",
}


# ── list ─────────────────────────────────────────────────────────────────
def _list_rows(store: Any) -> list[dict[str, Any]]:
    """Live cad designs, newest first, with node + part counts."""
    sql = """
        SELECT r.ref_id,
               (SELECT id_value FROM ref_identifiers
                 WHERE ref_id = r.ref_id AND id_kind = 'cite_key'
                 ORDER BY created_at DESC LIMIT 1)             AS slug,
               r.title,
               (SELECT count(*) FROM cad_nodes n
                 WHERE n.ref_id = r.ref_id
                   AND n.retired_at IS NULL)                   AS n_nodes,
               (SELECT count(DISTINCT n.component) FROM cad_nodes n
                 WHERE n.ref_id = r.ref_id
                   AND n.retired_at IS NULL)                   AS n_parts,
               r.updated_at
          FROM refs r
         WHERE r.kind = 'cad'
           AND r.deleted_at IS NULL
         ORDER BY r.ref_id DESC
         LIMIT %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (_LIST_LIMIT,)).fetchall()
    return [
        {
            "ref_id": int(r[0]),
            "slug": r[1],
            "title": r[2] or r[1],
            "n_nodes": int(r[3]),
            "n_parts": int(r[4]),
            "updated": _ago(r[5]),
        }
        for r in rows
    ]


# ── detail context (node list + analysis) ────────────────────────────────
def _pose_str(node: Any) -> str:
    if node.pattern is not None:
        p = node.pattern
        if p["kind"] == "polar":
            return f"polar n{int(p['n'])} r{p['r']:g}"
        return f"linear n{int(p['n'])} d({p['dx']:g},{p['dy']:g},{p['dz']:g})"
    x, y, z = node.loc
    pose = f"@{x:g},{y:g},{z:g}" if node.loc != (0.0, 0.0, 0.0) else "—"
    if node.rot != (0.0, 0.0, 0.0):
        pose += f" rot({node.rot[0]:g},{node.rot[1]:g},{node.rot[2]:g})"
    return pose


def _detail_ctx(store: Any, ref: Any) -> dict[str, Any]:
    """The cheap-to-render context: node list (coloured per part), a per-part
    legend, and counts. The heavy analysis (volume + interference) is computed
    lazily by :func:`cad_analysis` and fetched by the page after first paint."""
    spec, _handles = store.cad_load(ref.id)
    colors = component_colors(spec.components)
    nodes = [
        {
            "name": n.name,
            "component": n.component,
            "op": n.op,
            "config": n.config,
            "pose": _pose_str(n),
            "color": colors.get(n.component, "#8a9bb0"),
        }
        for n in spec.nodes
    ]
    legend = [
        {
            "component": c,
            "color": colors[c],
            "count": sum(1 for n in spec.nodes if n.component == c),
        }
        for c in spec.components
    ]

    return {
        "nodes": nodes,
        "legend": legend,
        "n_nodes": len(nodes),
        "n_parts": len(spec.components),
    }


#: Memoised analysis, keyed on ``(ref_id, version)``. A cad design's geometry is
#: immutable for a given ref version (edits derive a *new* slug), so the volume /
#: interference quadrature never needs recomputing for the same version.
_ANALYSIS_CACHE: dict[tuple[int, str], dict[str, Any]] = {}
_ANALYSIS_CACHE_MAX = 256


def _ref_version(store: Any, ref_id: int) -> str:
    """A cache-busting token for a ref's geometry (its ``updated_at``)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT updated_at FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row and row[0] is not None else "0"


def _analysis(store: Any, ref_id: int, version: str) -> dict[str, Any]:
    """Bounding box + total volume + inter-part interference, off the analytic
    IR. Memoised per ``(ref_id, version)``."""
    key = (ref_id, version)
    cached = _ANALYSIS_CACHE.get(key)
    if cached is not None:
        return cached

    spec, _handles = store.cad_load(ref_id)
    analysis: dict[str, Any] = {"parts": spec.components, "warnings": []}
    try:
        design = build_design(spec)
        lo, hi = _expr_aabb(design, design.whole())
        analysis["bbox"] = [round(float(hi[i] - lo[i]), 3) for i in range(3)]
        try:
            vol = cad_volume(design)
            analysis["volume"] = round(float(vol.volume), 3)
            analysis["volume_err"] = round(float(vol.rel_err) * 100, 1)
        except Exception:  # pragma: no cover - volume is best-effort
            log.debug("cad analysis: volume failed", exc_info=True)
        comps = list(dict.fromkeys(spec.components))
        for i in range(len(comps)):
            for j in range(i + 1, len(comps)):
                try:
                    res = cad_clearance(design, comps[i], comps[j])
                except Exception:  # pragma: no cover - defensive
                    continue
                if res.interfering:
                    analysis["warnings"].append(
                        f"{comps[i]} ↔ {comps[j]} interfere ({res.gap:g} mm)"
                    )
    except Exception:  # pragma: no cover - a bad build shouldn't blank the panel
        log.debug("cad analysis: build failed", exc_info=True)

    if len(_ANALYSIS_CACHE) >= _ANALYSIS_CACHE_MAX:
        _ANALYSIS_CACHE.clear()  # simple bound — the set is tiny and cheap to refill
    _ANALYSIS_CACHE[key] = analysis
    return analysis


def _slug_of(store: Any, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT id_value FROM ref_identifiers WHERE ref_id = %s "
            "AND id_kind = 'cite_key' ORDER BY created_at DESC LIMIT 1",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _lineage(store: Any, ref_id: int) -> dict[str, list[dict[str, str]]]:
    """Parents (this design is ``derived-from`` them) + children."""
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
    """The newest ``cad_propose`` job for this design — STATUS + its proposal
    chunk. Keyed on ``params.cad_ref_id`` so the route never captures a job id."""
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
           AND r.meta->>'job_type' = 'cad_propose'
           AND (r.meta->'params'->>'cad_ref_id')::int = %s
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


def _require_ref(store: Any, slug: str) -> Any:
    return resolve_live_slug_ref(store, kind="cad", id=slug)


# ── routes ───────────────────────────────────────────────────────────────
@router.get("/cad", response_class=HTMLResponse)
async def cad_list(request: Request) -> HTMLResponse:
    store = get_store(request)
    rows = _list_rows(store)
    return templates.TemplateResponse(
        request,
        "cad/list.html.j2",
        {"active_tab": "cad", "designs": rows, "total": len(rows)},
    )


@router.get("/cad/{slug}", response_class=HTMLResponse)
async def cad_detail(request: Request, slug: str) -> HTMLResponse:
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "CAD design not found",
                "detail": f"no live cad design with slug {slug!r}",
                "status": 404,
            },
            status_code=404,
        )
    ctx = _detail_ctx(store, ref)
    ctx.update(
        {
            "active_tab": "cad",
            "slug": ref.slug,
            "title": ref.title or ref.slug,
            "lineage": _lineage(store, ref.id),
            "proposal": _latest_proposal(store, ref.id),
            "solid_available": solid_available(),
            "manifold_available": manifold_available(),
            "step_available": step_available(),
        }
    )
    return templates.TemplateResponse(request, "cad/detail.html.j2", ctx)


@router.get("/cad/{slug}/model.gltf")
async def cad_model(request: Request, slug: str, mode: str = "features") -> Response:
    """The viewer/download glTF. ``mode=features`` (default, per-feature, editable)
    or ``mode=solid`` (folded true solid, when ``[cad-export]`` is present)."""
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    m = "solid" if mode == "solid" and solid_available() else "features"

    def _build() -> bytes:
        spec, _handles = store.cad_load(ref.id)
        return to_glb(spec, mode=m)

    try:
        glb = await asyncio.to_thread(_build)
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("cad model.gltf build failed for %s: %s", slug, exc)
        return JSONResponse({"error": "render failed"}, status_code=500)
    return Response(
        content=glb,
        media_type="model/gltf-binary",
        headers={"Cache-Control": "no-store"},
    )


@router.get("/cad/{slug}/scene.json")
async def cad_scene(request: Request, slug: str) -> JSONResponse:
    """The design's *recipe* — parsed primitives + poses + colours — so the
    browser tessellates client-side (the ~1 KB "3D-SVG" payload) instead of
    downloading a baked mesh. Each node carries its parsed ``shape`` (alias +
    numeric params); ``None`` for the unbounded chamfer half-space (no mesh)."""
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    spec, _handles = store.cad_load(ref.id)
    colors = component_colors(spec.components)
    nodes: list[dict[str, Any]] = []
    for n in spec.nodes:
        try:
            sh = parse_shape(n.config)
            shape: dict[str, Any] | None = {"alias": sh.alias, "params": sh.params}
        except DslError:
            shape = None  # e.g. chamfer — unbounded, drawn as a resolved cut only
        nodes.append(
            {
                "name": n.name,
                "component": n.component,
                "op": n.op,
                "loc": list(n.loc),
                "rot": list(n.rot),
                "pattern": n.pattern,
                "shape": shape,
                "color": colors.get(n.component, "#8a9bb0"),
            }
        )
    return JSONResponse(
        {"nodes": nodes, "components": spec.components, "meta": spec.meta}
    )


@router.get("/cad/{slug}/analysis")
async def cad_analysis(request: Request, slug: str) -> JSONResponse:
    """Bounding box + volume + interference for the design (the panel fetches
    this after first paint; memoised per ref version)."""
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    version = _ref_version(store, ref.id)
    result = await asyncio.to_thread(_analysis, store, ref.id, version)
    return JSONResponse(result)


@router.get("/cad/{slug}/export.{fmt}")
async def cad_export(request: Request, slug: str, fmt: str) -> Response:
    """Stream a download: ``scad`` (text) / ``stl`` / ``3mf`` (mesh) / ``step``
    (exact B-rep). Missing backend extra → a friendly 400, never a crash."""
    fmt = fmt.lower()
    if fmt not in _EXPORT_MEDIA:
        return _err(request, f"unknown export format {fmt!r}")
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return RedirectResponse(url="/cad", status_code=303)
    if fmt in ("stl", "3mf") and not manifold_available():
        return _err(request, f"{fmt.upper()} export needs the [cad-export] extra")
    if fmt == "step" and not step_available():
        return _err(request, "STEP export needs the [cad-step] extra")

    def _build() -> bytes:
        spec, _handles = store.cad_load(ref.id)
        if fmt == "scad":
            return to_openscad(spec, name=str(ref.slug or slug)).encode("utf-8")
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / f"{ref.slug or slug}.{fmt}"
            if fmt == "step":
                export_step(spec, out)
            else:
                export_mesh(spec, out, fmt=fmt)
            return out.read_bytes()

    try:
        data = await asyncio.to_thread(_build)
    except ExportError as exc:
        return _err(request, str(exc))
    filename = f"{ref.slug or slug}.{fmt}"
    return Response(
        content=data,
        media_type=_EXPORT_MEDIA[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cad/{slug}/instruct")
async def cad_instruct(
    request: Request, slug: str, instruction: str = Form(...)
) -> RedirectResponse:
    """The "Further instructions" box: mint a todo + a ``cad_propose`` job so the
    agent worker proposes a rewrite (propose-only; the human applies separately)."""
    store = get_store(request)
    instruction = instruction.strip()
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return RedirectResponse(url="/cad", status_code=303)
    if not instruction:
        return RedirectResponse(url=f"/cad/{slug}", status_code=303)

    # A todo to parent the job (JobHandler.put requires a live todo parent).
    todo_body, err = await await_dispatch(
        request, "put", {"kind": "todo", "text": f"cad {slug}: {instruction[:200]}"}
    )
    if err:
        return RedirectResponse(url=f"/cad/{slug}", status_code=303)
    m = re.search(r"\btd(\d+)\b", todo_body)
    if m is None:
        return RedirectResponse(url=f"/cad/{slug}", status_code=303)
    todo_id = int(m.group(1))

    await await_dispatch(
        request,
        "put",
        {
            "kind": "job",
            "parent_id": todo_id,
            "job_type": "cad_propose",
            "executor": "claude_inproc",
            "params": {
                "cad_ref_id": ref.id,
                "slug": slug,
                "instruction": instruction,
            },
        },
    )
    return RedirectResponse(url=f"/cad/{slug}#instruct", status_code=303)


@router.get("/cad/{slug}/proposal")
async def cad_proposal(request: Request, slug: str) -> JSONResponse:
    """Poll the latest proposal job for this design (the box polls this)."""
    store = get_store(request)
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(_latest_proposal(store, ref.id) or {"status": None})


@router.post("/cad/{slug}/apply")
async def cad_apply(
    request: Request,
    slug: str,
    to: str = Form(...),
    job_id: int = Form(...),
    delete_original: str = Form(""),
) -> Any:
    """Apply a proposal: read its source from the job's ``job_result`` chunk and
    ``derive`` a new design ``to`` from this one (linked derived-from). Optionally
    soft-delete the parent afterwards ("we can soft-delete the parents after")."""
    store = get_store(request)
    to_slug = to.strip()
    try:
        ref = _require_ref(store, slug)
    except NotFound:
        return RedirectResponse(url="/cad", status_code=303)
    proposal = _latest_proposal(store, ref.id)
    source = ""
    if proposal and proposal.get("proposal"):
        source = str(proposal["proposal"].get("source") or "")
    if not source.strip():
        return _err(request, "that proposal has no source to apply")

    handler = get_runtime(request).hub.handler_for("cad")
    drop_parent = delete_original.strip().lower() in ("1", "true", "on", "yes")

    def _do() -> tuple[bool, str]:
        try:
            handler.derive(id=slug, to=to_slug, text=source)
            if drop_parent:
                handler.delete(id=slug)
            return True, to_slug
        except (BadInput, NotFound) as exc:
            return False, str(exc)

    ok, msg = await asyncio.to_thread(_do)
    if not ok:
        return _err(request, msg)
    return RedirectResponse(url=f"/cad/{to_slug}", status_code=303)


def _err(request: Request, detail: str) -> Any:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {"title": "CAD action failed", "detail": detail, "status": 400},
        status_code=400,
    )
