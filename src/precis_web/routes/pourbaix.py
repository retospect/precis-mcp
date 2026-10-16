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
* Deep links: the panel state rides the query string — ``?surface=``,
  ``?view=compare``, ``?pathway=`` (which ready linked pathway feeds the
  staircase; default the best-ranked, :func:`precis.quest.staircase.
  ranked_pathways`), and the slider bank's ``?u=``/``?ph=``/``?reset=0``.
  The server renders a linked state on load; the page script keeps the
  bank params in the address bar with ``history.replaceState`` and carries
  them on every picker link. An unparseable value is named in a notice and
  the default kept.
"""

from __future__ import annotations

import logging
import math
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from precis.utils import handle_registry
from precis_web.deps import get_store, templates, timed_response
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
    masks: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One map for the template: the SVG plus its provenance footer and
    the region list the slider bank's resting-state label reads."""
    return {
        "svg": map_svg(m, u_marker=u_marker, vlines=vlines, reset=reset, masks=masks),
        "mask_rungs": [r["name"] for r in (masks or {}).get("rungs") or []],
        "mask_her": bool((masks or {}).get("her") is not None),
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


def _staircase_ctx(
    store: Store, ref: Any, *, pathway: str | None = None
) -> tuple[dict[str, Any] | None, Any, Any]:
    """The affine staircase of the candidate's best linked pathway — or of
    the ready linked pathway ``pathway`` names (handle, ref_id or slug; the
    ``?pathway=`` deep link, falling back to the best with a notice) —
    JSON-safe rungs for the client-side re-tilt (each carrying its
    slice-7 reset margin + estimator label), the engine's own per-route
    scalars, the HER competitor, footing and the honest ``missing``
    list. Returns ``(ctx, staircase_routes, pathway_ref)``; all ``None``
    when no ready pathway is linked."""
    from precis.quest import staircase as sq
    from precis_pathway import reset_contour, staircase_routes

    ranked = sq.ranked_pathways(store, int(ref.id))
    if not ranked:
        return None, None, None
    pw, notice = _pick_pathway(ranked, pathway)
    sr = staircase_routes.routes_for_pathway(store, pw)
    handle = _pathway_handle(pw)
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
            "pathway_notice": notice,
            "pathways": [
                {
                    "handle": _pathway_handle(p),
                    "title": (p.title or "").splitlines()[0] if p.title else "",
                    "selected": int(p.id) == int(pw.id),
                }
                for p in ranked
            ],
        },
        sr,
        pw,
    )


def _pathway_handle(pw: Any) -> str:
    return handle_registry.try_format("pathway", int(pw.id)) or f"pathway:{pw.id}"


def _pick_pathway(ranked: list[Any], raw: str | None) -> tuple[Any, str | None]:
    """``(pathway, notice)`` for the ``?pathway=`` pick among the ranked
    ready pathways — matched on handle, bare ref_id or slug — else the
    best-ranked. An unknown value falls back with a notice, never a 404."""
    if raw is None or not raw.strip():
        return ranked[0], None
    raw = raw.strip()
    for p in ranked:
        if raw in (_pathway_handle(p), str(p.id), getattr(p, "slug", None)):
            return p, None
    return ranked[0], (
        f"no ready pathway {raw!r} is linked to this structure — showing "
        f"{_pathway_handle(ranked[0])} (best-ranked)"
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


def _mask_layer(
    sr: Any, m: dict[str, Any], coads: dict[str, int]
) -> dict[str, Any] | None:
    """The slice-4 overlays for one map (gr489471): per PCET rung of
    route 1 (the scorecard's head — one route keeps the map legible), the
    U spans where it is viable; the HER mask's spans; and the operating
    window — the rung masks ∩ HER ∩ the map's region matching the surface
    the pathway was computed on. All pH-flat on RHE, so spans in U."""
    import numpy as np

    from precis.quest import staircase as sq
    from precis_pathway import staircase_masks as sm

    pcet = list(getattr(sr, "pcet_routes", None) or [])
    route = pcet[0] if pcet else None
    win = list(m.get("U_RHE_window") or [None, None])[:2]
    if route is None or not route.rungs or win[0] is None or win[1] is None:
        return None
    if not float(win[0]) < float(win[1]):
        return None
    u_axis = np.linspace(float(win[0]), float(win[1]), 401)
    full = sm.RestingMap.full(u_axis, np.array([0.0, 14.0]))
    # the off_map rule of point_states (staircase.mismatch): a U with no
    # region judges nothing, so it stays on-surface — one rule, both views
    on_surface = np.array(
        [
            sq.mismatch(sq.resting_region(m, float(u)), coads, u=float(u), label=None)
            is None
            for u in u_axis
        ]
    )
    resting = sm.RestingMap(
        u_axis, np.array([0.0, 14.0]), np.vstack([on_surface, on_surface])
    )
    rungs = [
        {
            "name": rg.name,
            "viable": _mask_intervals(sm.rung_mask(rg, full).viable[0], u_axis),
        }
        for rg in route.rungs
    ]
    her = None
    if sr.her is not None and sr.her.rungs:
        her = _mask_intervals(sm.her_mask(full, route, sr.her)[0], u_axis)
    window = sm.operating_window(resting, route, her=sr.her)
    return {
        "rungs": rungs,
        "her": her,
        "window": _mask_intervals(window.mask[0], u_axis),
    }


def _point(
    store: Store,
    ref: Any,
    pw: Any,
    sr: Any,
    maps: list[dict[str, Any]],
    *,
    u: float,
    ph: float,
    coads: dict[str, int] | None = None,
) -> dict[str, Any]:
    """:func:`precis_pathway.staircase_states.point_states` at ``(u, ph)``
    as JSON — the one call behind the first paint and ``/states``. The
    map is the structure's first (the one the overlays are drawn on); the
    surface the pathway was computed on follows ruling 3
    (:func:`~precis_pathway.staircase_states.pathway_coads`) unless the
    caller already resolved it."""
    from precis_pathway import staircase_states as ss

    if coads is None:
        coads = ss.pathway_coads(store, pw.meta or {}, ref.meta or {})
    return ss.point_states(
        sr, maps[0] if maps else None, u_rhe=u, ph=ph, coads=coads
    ).as_dict()


def _split_state(states: dict[str, Any], i: int, name: str) -> dict[str, Any] | None:
    """The PCET parent row a rendered (split) rung inherits, from the
    ``/states`` JSON — the dict twin of ``PointStates.state_of``."""
    routes = states.get("routes") or []
    if i >= len(routes):
        return None
    for rs in routes[i].get("rungs") or []:
        if name in (rs.get("split_children") or []):
            return rs
    return None


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
    ph: float = 0.0,
    pathway: str | None = None,
) -> dict[str, Any]:
    from precis.quest import staircase as sq
    from precis_pathway import pourbaix_map
    from precis_pathway import staircase_states as ss

    handle = handle_registry.try_format("structure", int(ref.id)) or f"st{ref.id}"
    staircase, sr, pw = _staircase_ctx(store, ref, pathway=pathway)
    raw_maps = pourbaix_map.maps_for_structure(store, ref)
    pw_coads = (
        ss.pathway_coads(store, pw.meta or {}, ref.meta or {})
        if pw is not None
        else sq.coads_of(ref.meta or {})
    )
    maps = [
        _map_ctx(
            m,
            u_marker=u_marker,
            vlines=_vlines(staircase),
            reset=_reset_layer(sr, m.get("U_RHE_window")),
            # overlays on the first map only: the rung states are judged
            # against it, so a second footing's map never contradicts them
            masks=_mask_layer(sr, m, pw_coads) if sr is not None and k == 0 else None,
        )
        for k, m in enumerate(raw_maps)
    ]
    coads = sq.coads_of(ref.meta or {})
    if staircase is not None and u_marker is not None:
        # the server-rendered first paint: the same call /states answers
        staircase["states"] = _point(
            store,
            ref,
            pw,
            sr,
            raw_maps,
            u=float(u_marker),
            ph=float(ph),
            coads=pw_coads,
        )
        staircase["states_url"] = (
            f"/pourbaix/{quote(handle, safe='')}/states?"
            + urlencode({"pathway": staircase["pathway_handle"]})
        )
        for i, r in enumerate(staircase["routes"]):
            for rg in r["rungs"]:
                rg["state"] = _split_state(staircase["states"], i, rg["name"])
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


#: Sanity clamp on a linked ``?u=`` (V vs RHE) — a hand-edited link may
#: widen the slider window, but not without bound.
_LINK_U_BOUND = 5.0


def _link_state(
    params: Any, u0: float, ph0: float
) -> tuple[float, float, bool, list[str]]:
    """The deep-link panel state (``?u=``, ``?ph=``, ``?reset=0``) over the
    server defaults: ``(u, pH, reset_layer_on, ignored)``. The client keeps
    these params in the address bar via ``history.replaceState`` as the
    sliders move; a value that doesn't parse is ignored (named in
    ``ignored`` for the notice) and the default kept — never an error."""
    ignored: list[str] = []
    u, ph = u0, ph0
    raw_u = params.get("u")
    if raw_u is not None:
        try:
            val = float(raw_u)
        except ValueError:
            val = math.nan
        if math.isfinite(val):
            u = max(-_LINK_U_BOUND, min(_LINK_U_BOUND, val))
        else:
            ignored.append("u")
    raw_ph = params.get("ph")
    if raw_ph is not None:
        try:
            val = float(raw_ph)
        except ValueError:
            val = math.nan
        if math.isfinite(val):
            ph = max(0.0, min(14.0, val))
        else:
            ignored.append("ph")
    raw_reset = params.get("reset")
    if raw_reset is not None and raw_reset not in ("0", "1"):
        ignored.append("reset")
    return u, ph, raw_reset != "0", ignored


@router.get("/pourbaix/{handle}/states")
async def pourbaix_states(request: Request, handle: str) -> JSONResponse:
    """The picked point's rung states as JSON (gr489471) —
    ``point_states`` for ``?U=&pH=&pathway=`` on one structure handle,
    the same call the page's first paint renders. ``U``/``pH`` default
    to the page's defaults; one that does not parse is a 400, not a
    silent default — the picker always sends both."""
    from precis_pathway import pourbaix_map

    store = get_store(request)
    resolved = _resolve(store, handle)
    if resolved is None or resolved[0] != "structure":
        return JSONResponse(
            {"error": f"no live structure with handle {handle!r}"}, status_code=404
        )
    ref = resolved[1]
    params = request.query_params
    point: dict[str, float] = {}
    for key, default in (
        ("U", _u_default(store, "structure", ref)),
        ("pH", _ph_default(ref)),
    ):
        raw = params.get(key)
        try:
            val = default if raw is None else float(raw)
        except ValueError:
            val = math.nan
        if not math.isfinite(val):
            return JSONResponse(
                {"error": f"{key}={raw!r} is not a finite number"}, status_code=400
            )
        point[key] = val
    staircase, sr, pw = _staircase_ctx(store, ref, pathway=params.get("pathway"))
    if staircase is None:
        return JSONResponse(
            {"error": "no ready pathway is linked to this structure"}, status_code=404
        )
    body = _point(
        store,
        ref,
        pw,
        sr,
        pourbaix_map.maps_for_structure(store, ref),
        u=max(-_LINK_U_BOUND, min(_LINK_U_BOUND, point["U"])),
        ph=max(0.0, min(14.0, point["pH"])),
    )
    body["pathway"] = staircase["pathway_handle"]
    body["pathway_notice"] = staircase["pathway_notice"]
    return JSONResponse(body)


@router.get("/pourbaix/{handle}", response_class=HTMLResponse)
async def pourbaix_detail(request: Request, handle: str) -> HTMLResponse:
    t0 = time.monotonic()
    store = get_store(request)
    from precis_pathway import pourbaix_map

    resolved = _resolve(store, handle)
    if resolved is None:
        return timed_response(
            request,
            templates.TemplateResponse(
                request,
                "error.html.j2",
                {
                    "title": "Pourbaix map not found",
                    "detail": f"no live quest or structure with handle {handle!r}",
                    "status": 404,
                },
                status_code=404,
            ),
            t0,
        )
    kind, ref = resolved
    # operating point known before entries: the marker/markers ride it;
    # a deep link's ?u=/?ph=/?reset= override the defaults
    u_def, ph_def = _u_default(store, kind, ref), _ph_default(ref)
    u0, ph0, reset_on, link_ignored = _link_state(request.query_params, u_def, ph_def)
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
    # ?pathway= picks the staircase's pathway on the one drawn surface; the
    # comparison view keeps every surface on its own best pathway
    pathway = None if compare else request.query_params.get("pathway")
    entries = [
        _candidate_ctx(store, s["ref"], u_marker=u0, ph=ph0, pathway=pathway)
        for s in drawn
    ]
    u_lo, u_hi = _u_window(entries, u0)
    return timed_response(
        request,
        templates.TemplateResponse(
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
                "ph0": ph0,
                # the server defaults: the client leaves a param out of the
                # address bar while its slider sits on the default
                "u_def": u_def,
                "ph_def": ph_def,
                "reset_on": reset_on,
                "link_ignored": link_ignored,
            },
        ),
        t0,
    )


__all__ = ["router"]
