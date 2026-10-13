"""Pourbaix tab — the resting-state surface-Pourbaix validity map and the
linked reaction-staircase panel (slices 2+3).

* ``GET /pourbaix/{handle}`` — the map for one structure handle (``st<id>``
  or slug) or, for a quest handle (``qu<id>``/id), every candidate structure
  that serves it. The map object is the one ``view='pourbaix'`` renders as
  text (:mod:`precis.quest.pourbaix_map`, reached here through the
  pathway-side name :mod:`precis_pathway.pourbaix_map` — ``precis_web`` sits
  outside the core→plugin import boundary): validity regions per U interval,
  each boundary with its propagated and model-form bands drawn apart, and
  the bulk verdict as a dashed, labelled uncertain layer.
* Slice 3 rides the same page: the candidate's best linked pathway
  (:func:`precis.quest.staircase.best_pathway`) feeds the affine-CHE
  staircase (:mod:`precis_pathway.staircase_routes`) beside the map, on one
  shared slider bank — the U slider re-tilts every rung
  (``ΔG = ΔG0 + n_e·U``) and moves the marker on every map client-side;
  the pH slider only relabels SHE (the sweep is pH-flat on RHE). The page
  degrades honestly: no pathway → map only; no anchors → staircase only.
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from precis.utils import handle_registry
from precis_web.deps import get_store, templates
from precis_web.pourbaix_svg import map_svg
from precis_web.ref_urls import ref_url

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(tags=["pourbaix"])

log = logging.getLogger(__name__)

#: U_RHE slider span when no map supplies a swept window.
_DEFAULT_U_WINDOW = (-1.0, 0.5)

#: Per-route line colours for the U_L / U_opt verticals (cycle-safe for
#: the handful of routes an engine scorecard emits).
_ROUTE_COLORS = ("#7c3aed", "#0d9488", "#b45309", "#be185d", "#0369a1", "#4d7c0f")

#: Cap on routes whose U_L/U_opt get map lines (legibility).
_MAX_LINED_ROUTES = 6


def _resolve(store: Store, handle: str) -> tuple[str, Any] | None:
    """``(kind, ref)`` for a quest or structure handle, else ``None``.

    ``qu<N>``/``st<N>`` parse through the handle registry; anything else is
    tried as a structure slug, then a bare ref_id on both kinds."""
    parsed = handle_registry.parse(handle)
    if parsed is not None and not parsed[1] and parsed[0] in ("quest", "structure"):
        ref = store.get_ref(kind=parsed[0], id=parsed[2])
        return (parsed[0], ref) if ref is not None else None
    ref = store.get_ref(kind="structure", id=handle)
    if ref is not None:
        return "structure", ref
    if handle.strip().isdigit():
        ref = store.get_ref(kind="quest", id=int(handle))
        if ref is not None:
            return "quest", ref
        ref = store.get_ref(kind="structure", id=int(handle))
        if ref is not None:
            return "structure", ref
    return None


def _map_ctx(
    m: dict[str, Any],
    *,
    u_marker: float | None = None,
    vlines: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One map for the template: the SVG plus its provenance footer and
    the region list the slider bank's resting-state label reads."""
    return {
        "svg": map_svg(m, u_marker=u_marker, vlines=vlines),
        "anchor_key": str(m.get("anchor_key") or "?"),
        "models": m.get("models") or [],
        "engine_version": m.get("engine_version"),
        "corrections": m.get("corrections"),
        "uncorrected": bool(m.get("uncorrected")),
        "scan_jobs": m.get("scan_jobs") or [],
        "sigma_e_eV": m.get("sigma_e_eV"),
        "incomplete": m.get("incomplete") or [],
        "u_window": m.get("U_RHE_window"),
        "bulk": m.get("bulk"),
        "regions": [
            {
                "label": r.get("label"),
                "range": r.get("U_RHE_range"),
                "termination": r.get("termination"),
            }
            for r in m.get("regions") or []
        ],
    }


def _staircase_ctx(store: Store, ref: Any) -> dict[str, Any] | None:
    """The affine staircase of the candidate's best linked pathway —
    JSON-safe rungs for the client-side re-tilt, the engine's own
    per-route scalars, the HER competitor, footing and the honest
    ``missing`` list. ``None`` when no ready pathway is linked."""
    from precis.quest import staircase as sq
    from precis_pathway import staircase_routes

    pw = sq.best_pathway(store, int(ref.id))
    if pw is None:
        return None
    sr = staircase_routes.routes_for_pathway(store, pw)
    handle = handle_registry.try_format("pathway", int(pw.id)) or f"pathway:{pw.id}"
    routes = []
    for r in sr.routes:
        stats = sr.route_stats.get(r.name) or {}
        routes.append(
            {
                "name": r.name,
                "U_L": stats.get("U_L"),
                "U_opt": stats.get("U_opt"),
                "span_at_UL": stats.get("span_at_UL"),
                "rungs": [
                    {
                        "name": rg.name,
                        "delta_g0": rg.delta_g0,
                        "n_e": rg.n_e,
                        "kind": rg.kind,
                        "sigma": rg.sigma,
                    }
                    for rg in r.rungs
                ],
            }
        )
    her = None
    if sr.her is not None:
        her = {
            "rungs": [
                {
                    "name": rg.name,
                    "delta_g0": rg.delta_g0,
                    "n_e": rg.n_e,
                    "kind": rg.kind,
                }
                for rg in sr.her.rungs
            ],
            "U_L_her": (sr.her_stats or {}).get("U_L_her"),
            "dG_H_star": (sr.her_stats or {}).get("dG_H_star"),
            "product": (sr.her_stats or {}).get("product"),
        }
    return {
        "pathway_id": int(pw.id),
        "pathway_handle": handle,
        "pathway_url": ref_url("pathway", pw.id, getattr(pw, "slug", None)),
        "title": (pw.title or "").splitlines()[0] if pw.title else "",
        "routes": routes,
        "her": her,
        "footing": sr.footing,
        "missing": list(sr.missing),
    }


def _vlines(staircase: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The campaign carry-over: each route's ``U_L`` (solid) and ``U_opt``
    (dashed) drawn as verticals on the map, plus ``U_L_her`` — clipped to
    the swept window by the SVG, never extrapolated."""
    out: list[dict[str, Any]] = []
    routes = ((staircase or {}).get("routes") or [])[:_MAX_LINED_ROUTES]
    for i, r in enumerate(routes):
        color = _ROUTE_COLORS[i % len(_ROUTE_COLORS)]
        for key, label, dash in (
            ("U_L", f"U_L{i + 1}", None),
            ("U_opt", f"U_opt{i + 1}", "4 3"),
        ):
            u = r.get(key)
            if isinstance(u, int | float) and not isinstance(u, bool):
                out.append(
                    {"u": float(u), "label": label, "color": color, "dash": dash}
                )
    her = (staircase or {}).get("her") or {}
    u_her = her.get("U_L_her")
    if isinstance(u_her, int | float) and not isinstance(u_her, bool):
        out.append(
            {"u": float(u_her), "label": "U_L_her", "color": "#dc2626", "dash": "2 2"}
        )
    return out


def _candidate_ctx(
    store: Store,
    ref: Any,
    *,
    u_marker: float | None = None,
) -> dict[str, Any]:
    from precis.quest import staircase as sq
    from precis_pathway import pourbaix_map

    handle = handle_registry.try_format("structure", int(ref.id)) or f"st{ref.id}"
    staircase = _staircase_ctx(store, ref)
    maps = [
        _map_ctx(m, u_marker=u_marker, vlines=_vlines(staircase))
        for m in pourbaix_map.maps_for_structure(store, ref)
    ]
    coads = sq.coads_of(ref.meta or {})
    return {
        "ref_id": int(ref.id),
        "handle": handle,
        "title": (ref.title or "").splitlines()[0] if ref.title else "",
        "url": ref_url("structure", ref.id, getattr(ref, "slug", None)),
        "maps": maps,
        "staircase": staircase,
        # the slider bank's data island: resting regions per map + the
        # candidate's assumed coads (for the live mismatch verdict)
        "pbx": {
            "maps": [{"regions": m["regions"]} for m in maps],
            "coads": coads,
        },
    }


def _u_default(store: Store, kind: str, ref: Any) -> float:
    """The slider bank's initial U: the quest's human-set operating point
    when one is recorded (``meta.operating_conditions.U_RHE``), then the
    first candidate's pathway operating point, then 0 V."""
    cond = (ref.meta or {}).get("operating_conditions")
    if isinstance(cond, dict):
        u = cond.get("U_RHE")
        if isinstance(u, int | float) and not isinstance(u, bool) and math.isfinite(u):
            return float(u)
    if kind == "structure":
        from precis.quest import staircase as sq

        u = sq.quest_operating_u(store, int(ref.id))
        if u is not None:
            return u
        pw = sq.best_pathway(store, int(ref.id))
        if pw is not None:
            return sq.pathway_operating_u(
                (pw.meta or {}).get("config"), (pw.meta or {}).get("results")
            )
    return 0.0


def _ph_default(ref: Any) -> float:
    cond = (ref.meta or {}).get("operating_conditions")
    if isinstance(cond, dict):
        ph = cond.get("pH")
        if isinstance(ph, int | float) and not isinstance(ph, bool):
            return float(ph)
    return 0.0


def _u_window(entries: list[dict[str, Any]], u0: float) -> tuple[float, float]:
    """The shared U slider's span: the union of the drawn maps' swept
    windows (widened to cover ``u0``), the default when no map exists."""
    lo, hi = _DEFAULT_U_WINDOW
    for e in entries:
        for m in e["maps"]:
            # window rides the svg data attrs; regions/footers need none
            w = m.get("u_window") or [None, None]
            if w[0] is not None and w[1] is not None:
                lo, hi = min(lo, float(w[0])), max(hi, float(w[1]))
    return min(lo, u0), max(hi, u0)


@router.get("/pourbaix/{handle}", response_class=HTMLResponse)
async def pourbaix_detail(request: Request, handle: str) -> HTMLResponse:
    store = get_store(request)
    from precis_pathway import pourbaix_map

    resolved = _resolve(store, handle)
    if resolved is None:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {
                "title": "Pourbaix map not found",
                "detail": f"no live quest or structure with handle {handle!r}",
                "status": 404,
            },
            status_code=404,
        )
    kind, ref = resolved
    if kind == "quest":
        # operating point known before entries: the marker/markers ride it
        u0 = _u_default(store, kind, ref)
        entries = []
        for e in pourbaix_map.quest_maps(store, ref):
            entries.append(_candidate_ctx(store, e["ref"], u_marker=u0))
        subject = {
            "kind": "quest",
            "handle": handle_registry.try_format("quest", ref.id) or f"qu{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    else:
        u0 = _u_default(store, kind, ref)
        entries = [_candidate_ctx(store, ref, u_marker=u0)]
        subject = {
            "kind": "structure",
            "handle": handle_registry.try_format("structure", ref.id) or f"st{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    u_lo, u_hi = _u_window(entries, u0)
    return templates.TemplateResponse(
        request,
        "pourbaix/detail.html.j2",
        {
            "active_tab": "pourbaix",
            "subject": subject,
            "title": f"surface-Pourbaix — {subject['handle']}",
            "entries": entries,
            "bulk_note": pourbaix_map.BULK_LAYER_NOTE,
            "u0": u0,
            "u_lo": u_lo,
            "u_hi": u_hi,
            "ph0": _ph_default(ref),
        },
    )


__all__ = ["router"]
