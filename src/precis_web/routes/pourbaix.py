"""Pourbaix tab — the resting-state surface-Pourbaix validity map and the
linked reaction-staircase panel (slices 2+3).

* ``GET /pourbaix/{handle}`` — the map for one structure handle (``st<id>``
  or slug). For a quest handle (``qu<id>``/id) the page draws ONE candidate
  surface at a time — a Pourbaix map is a per-surface object (gr486821):
  ``?surface=<handle|slug|id>`` picks it, defaulting to the quest's
  best-ranked candidate (leaderboard order); the other surfaces are listed
  as links to their own ``/pourbaix/<handle>`` pages, and ``?view=compare``
  is the explicit secondary view that draws them all, labelled a
  comparison. The map object is the one ``view='pourbaix'`` renders as
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
from urllib.parse import quote

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
    reset: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One map for the template: the SVG plus its provenance footer and
    the region list the slider bank's resting-state label reads."""
    return {
        "svg": map_svg(m, u_marker=u_marker, vlines=vlines, reset=reset),
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


def _staircase_ctx(store: Store, ref: Any) -> tuple[dict[str, Any] | None, Any]:
    """The affine staircase of the candidate's best linked pathway —
    JSON-safe rungs for the client-side re-tilt (each carrying its
    slice-7 reset margin + estimator label), the engine's own per-route
    scalars, the HER competitor, footing and the honest ``missing``
    list. Returns ``(ctx, staircase_routes)``; ``(None, None)`` when no
    ready pathway is linked."""
    from precis.quest import staircase as sq
    from precis_pathway import reset_contour, staircase_routes

    pw = sq.best_pathway(store, int(ref.id))
    if pw is None:
        return None, None
    sr = staircase_routes.routes_for_pathway(store, pw)
    handle = handle_registry.try_format("pathway", int(pw.id)) or f"pathway:{pw.id}"
    routes = []
    for r in sr.routes:
        stats = sr.route_stats.get(r.name) or {}
        edges = {e.rung.name: e for e in reset_contour.reset_edges(r)}
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
                        # m_e + sigma — the required overdrive the client
                        # re-evaluates live as the U slider moves
                        "margin": (
                            edges[rg.name].required_eV if rg.name in edges else None
                        ),
                        "estimator": (
                            edges[rg.name].estimator if rg.name in edges else None
                        ),
                        "eps": rg.eps,
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
    return (
        {
            "pathway_id": int(pw.id),
            "pathway_handle": handle,
            "pathway_url": ref_url("pathway", pw.id, getattr(pw, "slug", None)),
            "title": (pw.title or "").splitlines()[0] if pw.title else "",
            "routes": routes,
            "her": her,
            "footing": sr.footing,
            "missing": list(sr.missing),
        },
        sr,
    )


def _u_edges(axis: Any) -> Any:
    """Cell edges of a 1-D axis (midpoints, half a step beyond the ends)
    — mirrors ``staircase_masks``'s cell measure on the display grid."""
    import numpy as np

    axis = np.asarray(axis, dtype=float)
    n = axis.size
    if n == 1:
        return np.array([axis[0] - 0.5, axis[0] + 0.5])
    edges = np.empty(n + 1)
    edges[1:n] = 0.5 * (axis[:-1] + axis[1:])
    edges[0] = axis[0] - 0.5 * (axis[1] - axis[0])
    edges[n] = axis[-1] + 0.5 * (axis[-1] - axis[-2])
    return edges


def _mask_intervals(row: Any, axis: Any) -> list[list[float]]:
    """Contiguous True runs of a pH-flat mask row as ``[u_lo, u_hi]``
    spans (cell edges — the layer covers what the mask owns)."""
    import numpy as np

    edges = _u_edges(axis)
    idx = np.flatnonzero(np.asarray(row, dtype=bool))
    out: list[list[float]] = []
    for grp in np.split(idx, np.flatnonzero(np.diff(idx) > 1) + 1):
        if grp.size:
            out.append([float(edges[grp[0]]), float(edges[grp[-1] + 1])])
    return out


def _reset_layer(sr: Any, u_window: Any) -> list[dict[str, Any]]:
    """Per-route guaranteed/hysteretic spans over the map's U window —
    the slice-7 layer (``reset_contour`` on a display grid; every mask is
    pH-flat on the RHE scale, so row 0 is the whole picture)."""
    import numpy as np

    from precis_pathway import reset_contour
    from precis_pathway import staircase_masks as sm

    routes = list(getattr(sr, "routes", None) or [])[:_MAX_LINED_ROUTES]
    if not routes:
        return []
    win = list(u_window or [None, None])[:2]
    if win[0] is None or win[1] is None or not float(win[0]) < float(win[1]):
        return []
    u_axis = np.linspace(float(win[0]), float(win[1]), 201)
    resting = sm.RestingMap.full(u_axis, np.array([0.0, 14.0]))
    out: list[dict[str, Any]] = []
    for i, route in enumerate(routes):
        c = reset_contour.reset_contour(resting, route)
        ok = _mask_intervals(c.reset_ok[0], u_axis)
        band = _mask_intervals(c.hysteretic[0], u_axis)
        if ok or band:
            out.append(
                {
                    "name": f"r{i + 1}",
                    "color": _ROUTE_COLORS[i % len(_ROUTE_COLORS)],
                    "ok": ok,
                    "band": band,
                }
            )
    return out


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
    staircase, sr = _staircase_ctx(store, ref)
    maps = [
        _map_ctx(
            m,
            u_marker=u_marker,
            vlines=_vlines(staircase),
            reset=_reset_layer(sr, m.get("U_RHE_window")),
        )
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


def _quest_surfaces(store: Store, quest_ref: Any) -> list[dict[str, Any]]:
    """The quest's candidate surfaces, best-ranked first.

    :func:`precis.quest.pourbaix_map.quest_maps` supplies the live
    ``serves`` structures plus their maps; the leaderboard order
    (frontier → dominated → provisional → awaiting, the primary objective
    inside a band — the same order ``view='leaderboard'`` prints)
    supplies the ranking. A failed frontier read degrades to link order:
    ranking must not 500 the page."""
    from precis.quest import frontier as frontier_mod
    from precis_pathway import pourbaix_map

    entries = pourbaix_map.quest_maps(store, quest_ref)
    order: list[str] = []
    band: dict[str, str] = {}
    try:
        rows, _schema = frontier_mod.leaderboard(
            frontier_mod.quest_frontier(store, int(quest_ref.id))
        )
        order = [str(r.get("design") or "") for r in rows]
        band = {str(r.get("design") or ""): str(r.get("band") or "") for r in rows}
    except Exception:
        log.warning(
            "pourbaix: frontier ranking failed for quest %s",
            quest_ref.id,
            exc_info=True,
        )
    by_handle = {e["handle"]: e for e in entries}
    ranked = [by_handle.pop(h) for h in order if h in by_handle]
    ranked.extend(e for e in entries if e["handle"] in by_handle)
    out: list[dict[str, Any]] = []
    for e in ranked:
        ref = e["ref"]
        handle = str(e["handle"])
        quoted = quote(handle, safe="")
        out.append(
            {
                "ref": ref,
                "handle": handle,
                "title": (ref.title or "").splitlines()[0]
                if getattr(ref, "title", None)
                else "",
                "band": band.get(handle, ""),
                "has_maps": bool(e["maps"]),
                "page_url": f"/pourbaix/{quoted}",
                "select_url": f"?surface={quoted}",
            }
        )
    return out


def _select_surface(
    surfaces: list[dict[str, Any]], raw: str | None
) -> tuple[dict[str, Any] | None, str | None]:
    """``(entry, notice)`` for the ``?surface=`` pick — matched on the
    formatted handle, the bare ref_id or the slug — else the best-ranked
    surface. An unknown value falls back to best-ranked with a notice: a
    picker param never 404s."""
    if not surfaces:
        return None, None
    if raw is None or not raw.strip():
        return surfaces[0], None
    raw = raw.strip()
    for s in surfaces:
        ref = s["ref"]
        if raw in (s["handle"], str(ref.id), getattr(ref, "slug", None)):
            return s, None
    return surfaces[0], (
        f"no surface {raw!r} serves this quest — showing "
        f"{surfaces[0]['handle']} (best-ranked)"
    )


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
    # operating point known before entries: the marker/markers ride it
    u0 = _u_default(store, kind, ref)
    surfaces: list[dict[str, Any]] = []
    compare = False
    selected: dict[str, Any] | None = None
    surface_notice: str | None = None
    if kind == "quest":
        # one surface at a time (gr486821): the ranked picker lists them
        # all; ?surface= picks the drawn one, ?view=compare is the
        # labelled cross-surface comparison.
        surfaces = _quest_surfaces(store, ref)
        compare = request.query_params.get("view") == "compare"
        selected, surface_notice = _select_surface(
            surfaces, request.query_params.get("surface")
        )
        drawn = surfaces if compare else ([selected] if selected else [])
        subject = {
            "kind": "quest",
            "handle": handle_registry.try_format("quest", ref.id) or f"qu{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    else:
        drawn = [{"ref": ref}]
        subject = {
            "kind": "structure",
            "handle": handle_registry.try_format("structure", ref.id) or f"st{ref.id}",
            "title": (ref.title or "").splitlines()[0] if ref.title else "",
        }
    entries = [_candidate_ctx(store, s["ref"], u_marker=u0) for s in drawn]
    u_lo, u_hi = _u_window(entries, u0)
    return templates.TemplateResponse(
        request,
        "pourbaix/detail.html.j2",
        {
            "active_tab": "pourbaix",
            "subject": subject,
            "title": f"surface-Pourbaix — {subject['handle']}",
            "entries": entries,
            "surfaces": surfaces,
            "selected_handle": selected["handle"] if selected else None,
            "compare": compare,
            "surface_notice": surface_notice,
            "bulk_note": pourbaix_map.BULK_LAYER_NOTE,
            "u0": u0,
            "u_lo": u_lo,
            "u_hi": u_hi,
            "ph0": _ph_default(ref),
        },
    )


__all__ = ["router"]
