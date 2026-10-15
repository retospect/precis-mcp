"""SPSO slice 6 — the operating-window robustness score and the Shapley
vector as quest Pareto objectives, plus active anchor placement.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(§8.2 two levers, §8.4 reductions, Rulings 2026-10-13: HER is a mask rung
AND its own Pareto objective; rung masks carry a tolerance ε on ΔG and a
TOF floor stays a separate objective). The physics lives in
``precis_pathway.staircase_masks`` (slices 4+5); this module only *reads*
it — nothing here recomputes a mask.

**The plugin-import boundary** (``tests/test_plugin_import_boundary.py``)
forbids ``src/precis`` importing ``precis_pathway``, so the plugin is
reached with :func:`importlib.import_module` inside the call, and a
missing module — plugin not installed, or the sibling slice's
``staircase_routes`` adapter not yet landed — degrades to "no measures",
never a crash and never a fabricated value (the same rule
``pourbaix_map`` applies to invented sigmas).

**Computed whenever the anchors exist.** A coverage scan is the paid
measurement: the window/Shapley fields stamp on every candidate that
carries one, whether or not the quest's ``meta.rubric_objectives``
declares a :data:`WINDOW_MEASURE_KEYS` key (gr489165 — an undeclared
objective left the fields unreadable on every scanned candidate). The
declaration still decides what the *frontier* ranks on and whether the
logbook announces the stamps, so an unflagged quest's ranking and logbook
stay as before. Optional knobs ride
``meta.window_objectives = {"eps_eV": …, "her_margin_eV": …,
"reset_eta_eV": …, "reset_ea_floor_eV": …}`` (ε defaults to each rung's
own ``eps_eV`` then 0; the HER margin to 0; η to the contour module's
default; the barrier floor to 0).

**Stamps** (:func:`stamp_window_measures`, called once per candidate from
``compute.harvest_measures``): the numeric measures below (the frontier
reads them like any other ``structure.meta`` scalar), plus the
non-numeric diagnostics ``bottleneck_rung``, ``window_notes`` and
``validity_region_at_point`` — the ruling-2026-10-07 "which slab model to
trust" answer at the operating point, shown on the leaderboard.

The resting map's ``wanted`` mask marks cells whose resting termination
equals the termination resting at the quest's operating point
(``meta.operating_conditions.U_RHE``, else B1's
``resting_termination_at_point`` stamp): the window is where the slab
model stays in the state it was assumed to be in AND every staircase
constraint holds. With no operating point at all the mask is all-wanted
(window = rungs ∩ HER) and ``window_notes`` says so.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import logging
from dataclasses import replace as _dc_replace
from types import ModuleType
from typing import Any

log = logging.getLogger(__name__)

#: Numeric measures this module can stamp — the Pareto-objective keys a
#: quest may declare in ``meta.rubric_objectives``. ``bottleneck_rung``
#: (a name) and ``window_notes``/``validity_region_at_point`` (diagnostics)
#: are stamped alongside but are never measures.
WINDOW_MEASURE_KEYS: tuple[str, ...] = (
    "window_area_VpH",
    "window_fraction",
    "shapley_sumsq_raw",
    "shapley_ipr",
    "ridge_churn",
    "her_margin_at_Uopt",
    "reset_window_area",
    "reset_window_fraction",
)

#: pH grid the resting map is rebuilt on. The sweep and every rung mask
#: are pH-flat on the RHE scale, so resolution is a display choice; the
#: span itself (``pH_window`` on the map) sets the V·pH area scale.
_PH_GRID_POINTS = 29

#: Candidate-meta key holding the input hash the last stamp ran against —
#: when it matches, the map/routes/Shapley recompute is skipped entirely.
WINDOW_INPUTS_KEY = "window_measures_input"

_NO_KEY = object()  # sentinel: no wanted-termination criterion could be derived


def _centroid_u(
    resting_map: Any, weights: Any, total: float, notes: list[str]
) -> float:
    """The window's area-weighted mean U — snapped to the nearest U
    column that carries window weight when the mean falls in a forbidden
    gap between disjoint strips (an empty column can never be an
    operating point). ``weights`` is the settled mask product (window,
    or the baseline/resting fallback)."""
    u_best = float((resting_map.u_grid() * weights).sum() / total)
    u_ax = [float(u) for u in resting_map.u_rhe]
    col_w = weights.sum(axis=0)  # over the pH axis
    live = [j for j in range(len(u_ax)) if float(col_w[j]) > 0.0]
    if not live:
        return u_best
    nearest = min(range(len(u_ax)), key=lambda j: abs(u_ax[j] - u_best))
    if float(col_w[nearest]) > 0.0:
        return u_best
    snapped = u_ax[min(live, key=lambda j: abs(u_ax[j] - u_best))]
    notes.append(
        f"U centroid {u_best:+.3f} V sits in a forbidden gap — read at the "
        f"nearest in-window column {snapped:+.3f} V"
    )
    return snapped


def _staircase_masks() -> ModuleType | None:
    """The plugin's masks module, or ``None`` when the plugin is absent —
    the plugin-boundary-safe lazy reach (module docstring)."""
    try:
        return importlib.import_module("precis_pathway.staircase_masks")
    except ImportError:
        return None


def _staircase_routes() -> ModuleType | None:
    """The sibling slice's store→Route adapter (B2), or ``None`` while it
    has not landed / the plugin is absent — treated as "routes missing"."""
    try:
        return importlib.import_module("precis_pathway.staircase_routes")
    except ImportError:
        return None


def _reset_contour() -> ModuleType | None:
    """The slice-7 guaranteed-reset contour (the estimator ladder's
    hysteresis mask), or ``None`` while it has not landed."""
    try:
        return importlib.import_module("precis_pathway.reset_contour")
    except ImportError:
        return None


def _declared_objective_keys(quest_meta: dict[str, Any] | None) -> set[str]:
    """Measure keys the quest's ``meta.rubric_objectives`` declares —
    mirrors ``frontier._objectives_for``'s defensive parse."""
    raw = (quest_meta or {}).get("rubric_objectives")
    keys: set[str] = set()
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict) and str(item.get("key") or "").strip():
                keys.add(str(item["key"]).strip())
    return keys


def window_objectives_enabled(quest_meta: dict[str, Any] | None) -> bool:
    """The feature flag: the quest declares at least one window measure."""
    return bool(_declared_objective_keys(quest_meta) & set(WINDOW_MEASURE_KEYS))


def _window_config(quest_meta: dict[str, Any] | None) -> dict[str, Any]:
    raw = (quest_meta or {}).get("window_objectives")
    return dict(raw) if isinstance(raw, dict) else {}


def _num(v: Any) -> float | None:
    if isinstance(v, bool):
        return None
    return float(v) if isinstance(v, (int, float)) else None


# ── window_measures (pure) ────────────────────────────────────────────────


def window_measures(
    resting_map: Any,
    routes: Any,
    her: Any,
    *,
    eps_eV: float | None = None,
    her_margin_eV: float = 0.0,
    reset_eta_eV: float | None = None,
    reset_ea_floor_eV: float = 0.0,
    pcet_routes: Any = None,
) -> dict[str, Any]:
    """The window robustness + Shapley scalars for one candidate.

    The window, Shapley, HER margin and bottleneck read each route in the
    one-rung CHE convention (``pcet_routes``, matched to ``routes`` by
    name; ``staircase_masks.pcet_route`` when not given, gr489164); the
    reset contour keeps the engine's split rungs, where the thermal
    barriers live.

    ``resting_map`` is a ``staircase_masks.RestingMap`` (or a duck-typed
    equivalent — this function never imports the plugin for types, only
    for the compute verbs); ``routes`` the candidate's main
    ``Route``s and ``her`` the HER competitor ``Route`` (or ``None``).
    ``eps_eV`` fills in the viability tolerance on rungs that carry none
    (a rung's own ``eps_eV`` always wins); ``her_margin_eV`` is the
    selectivity margin of ``staircase_masks.her_mask``. ``reset_eta_eV``
    is the slice-7 overdrive margin η (``None`` = the contour module's
    default); ``reset_ea_floor_eV`` the barrier floor the BEP/NEB rungs
    price their margin against.

    With several routes the best-windowed one wins — the route a smart
    operator would run — and ``window_notes`` names it. Every measure is
    ``None`` (and ``window_notes`` says why) when inputs are missing;
    nothing is ever fabricated.

    * ``her_margin_at_Uopt`` = ``limiting_her − limiting_main`` at the
      window's U centroid (the "best U" — a simple area-weighted mean, not
      an optimisation): the same sign convention as
      ``staircase_masks.her_mask``'s margin, so positive = the main route
      outruns HER (NORR wins).
    * ``bottleneck_rung`` = the limiting rung (max ΔG) at that point —
      the constraint closest to binding there, ``None`` on an empty
      window.
    * ``reset_window_area`` / ``reset_window_fraction`` = the
      *guaranteed* window ``window ∩ reset_ok`` (slice 7: every edge
      downhill by its priced margin, launched past the propagated σ) —
      stamped beside the thermodynamic window, never blended into it.
      ``reset_limited_by`` names the edge binding the guarantee at the
      same U centroid; ``reset_estimators`` records each edge's ladder
      rung (``overdrive``/``bep``/``neb``).
    """
    notes: list[str] = []
    out: dict[str, Any] = dict.fromkeys(WINDOW_MEASURE_KEYS)
    out["bottleneck_rung"] = None
    out["reset_limited_by"] = None
    out["reset_estimators"] = None
    out["window_notes"] = notes

    sm = _staircase_masks()
    if sm is None:
        notes.append("precis_pathway.staircase_masks is not importable")
        return out
    if resting_map is None:
        notes.append("no resting map")
    pcet_by_name = {
        getattr(p, "name", None): p for p in (pcet_routes or []) if p is not None
    }
    live = [r for r in (routes or []) if getattr(r, "rungs", None)]
    if not live:
        notes.append("no main routes")
    if resting_map is None or not live:
        return out
    if her is None or not getattr(her, "rungs", None):
        notes.append("no HER competitor — her_margin_at_Uopt unset")
        her = None

    def _with_eps(route: Any) -> Any:
        if eps_eV is None:
            return route
        try:
            return _dc_replace(
                route,
                rungs=tuple(
                    r
                    if getattr(r, "eps_eV", None) is not None
                    else _dc_replace(r, eps_eV=eps_eV)
                    for r in route.rungs
                ),
            )
        except (TypeError, AttributeError):
            return route

    best: tuple[Any, Any, Any, Any, Any] | None = None
    for route in live:
        pcet = _with_eps(pcet_by_name.get(route.name) or sm.pcet_route(route))
        route = _with_eps(route)
        win = sm.operating_window(
            resting_map, pcet, None, her=her, her_margin=her_margin_eV
        )
        red = sm.reduce_field(
            sm.shapley_field(resting_map, pcet, None, her=her, her_margin=her_margin_eV)
        )
        score = (win.fraction, win.area)
        if best is None or score > best[0]:
            best = (score, route, pcet, win, red)
    assert best is not None  # live is non-empty (checked above)
    _, route, pcet, win, red = best
    if len(live) > 1:
        notes.append(f"{len(live)} routes — window of {route.name!r} is largest")

    out["window_area_VpH"] = float(win.area)
    out["window_fraction"] = float(win.fraction)
    out["shapley_sumsq_raw"] = float((red.sumsq * resting_map.cell_areas()).sum())
    out["shapley_ipr"] = float(red.ipr_mean)
    out["ridge_churn"] = float(red.churn)

    rc = _reset_contour()
    contour = None
    if rc is None:
        notes.append(
            "precis_pathway.reset_contour is not importable — reset measures unset"
        )
    else:
        contour = rc.reset_contour(
            resting_map,
            route,
            eta_eV=rc.DEFAULT_ETA_EV if reset_eta_eV is None else reset_eta_eV,
            ea_floor_eV=reset_ea_floor_eV,
        )
        # window := window ∩ reset_ok — the guaranteed layer stamped
        # beside the thermodynamic one (series composition, §6.3).
        guaranteed = win.mask & contour.reset_ok
        out["reset_window_area"] = float(resting_map.area(guaranteed))
        out["reset_window_fraction"] = float(resting_map.fraction(guaranteed))
        out["reset_estimators"] = {e.rung.name: e.estimator for e in contour.edges}
        notes.extend(contour.notes)

    weights = resting_map.cell_areas() * win.mask
    total = float(weights.sum())
    if total <= 0.0:
        # Empty window: the "best U" falls back to the baseline centroid
        # (resting ∩ HER), then to the resting mask's — a dead candidate
        # still gets a HER margin and a bottleneck diagnosis where the
        # window would have been.
        weights = resting_map.cell_areas() * win.baseline
        total = float(weights.sum())
        where = "baseline"
        if total <= 0.0:
            weights = resting_map.cell_areas() * resting_map.wanted
            total = float(weights.sum())
            where = "resting"
        notes.append(
            f"operating window is empty — margin and bottleneck read at "
            f"the {where} centroid"
        )
        if total <= 0.0:
            return out
    u_best = _centroid_u(resting_map, weights, total, notes)
    if her is not None:
        out["her_margin_at_Uopt"] = float(
            her.limiting_g(u_best) - pcet.limiting_g(u_best)
        )
    rung = max(pcet.rungs, key=lambda r: float(r.delta_g(u_best)))
    out["bottleneck_rung"] = rung.name
    if contour is not None:
        verdict = contour.at(u_best, float(resting_map.ph[0]))
        out["reset_limited_by"] = verdict["limiting"] or None
    return out


# ── map dict → RestingMap ────────────────────────────────────────────────


def _region_at(m: dict[str, Any], u: float) -> dict[str, Any] | None:
    """The validity region whose ``U_RHE_range`` contains ``u``; at a
    boundary the lower region wins (regions are ascending, contiguous)."""
    regions = m.get("regions") or []
    for r in regions:
        lo, hi = r.get("U_RHE_range") or [None, None]
        if lo is not None and hi is not None and float(lo) <= u <= float(hi):
            return r
    if not regions:
        return None
    first_lo = (regions[0].get("U_RHE_range") or [None])[0]
    if first_lo is not None and u < float(first_lo):
        return regions[0]
    return regions[-1]


def _operating_point_u(quest_meta: dict[str, Any] | None) -> float | None:
    cond = (quest_meta or {}).get("operating_conditions")
    if not isinstance(cond, dict):
        return None
    return _num(cond.get("U_RHE"))


def _wanted_key(
    m: dict[str, Any],
    point_u: float | None,
    structure_meta: dict[str, Any] | None,
) -> Any:
    """The resting-termination key the window is wanted on.

    ``[adsorbate, n]`` (or ``None`` = clean) of the region at the
    operating point — the slab model's assumed surface state. Falls back
    to B1's ``resting_termination_at_point`` stamp (``{"adsorbate":…,
    "n":…}`` or a region-label string); ``_NO_KEY`` when nothing is
    derivable."""
    if point_u is not None:
        r = _region_at(m, point_u)
        if r is not None:
            return r.get("key")
    at = (structure_meta or {}).get("resting_termination_at_point")
    if isinstance(at, dict):
        term = at.get("termination")
        term = term if isinstance(term, dict) else at
        if "adsorbate" in term or "n" in term:
            return [term.get("adsorbate"), term.get("n")]
        if at.get("label"):
            at = at["label"]
    if isinstance(at, str):
        for r in m.get("regions") or []:
            if r.get("label") == at:
                return r.get("key")
    return _NO_KEY


def _resting_map_for(sm: ModuleType, m: dict[str, Any], wanted_key: Any) -> Any | None:
    """Rebuild a ``staircase_masks.RestingMap`` from a ``build_map`` dict:
    the U axis from ``U_RHE_window``×``grid``, a display pH axis over
    ``pH_window``, and ``wanted`` = "the operating point's termination
    still rests here" per U column (pH-flat on RHE)."""
    import numpy as np

    win = m.get("U_RHE_window") or [None, None]
    if win[0] is None or win[1] is None:
        return None
    n_u = int(m.get("grid") or 151)
    u_axis = np.linspace(float(win[0]), float(win[1]), max(n_u, 2))
    ph_win = m.get("pH_window") or [0.0, 14.0]
    ph_axis = np.linspace(float(ph_win[0]), float(ph_win[1]), _PH_GRID_POINTS)
    if wanted_key is _NO_KEY:
        wanted = np.ones((ph_axis.size, u_axis.size), dtype=bool)
    else:
        cols = np.array(
            [(_region_at(m, float(u)) or {}).get("key") == wanted_key for u in u_axis]
        )
        wanted = np.broadcast_to(cols[None, :], (ph_axis.size, u_axis.size))
    return sm.RestingMap(u_axis, ph_axis, wanted)


def _validity_region_at_point(
    m: dict[str, Any], point_u: float | None
) -> dict[str, Any] | None:
    """ "Which slab model to trust" at the operating point (ruling
    2026-10-07): the containing region's termination + trust label + the
    map's footing (anchor_key / engine / correction set)."""
    if point_u is None:
        return None
    r = _region_at(m, point_u)
    if r is None:
        return None
    return {
        "termination": r.get("label") or "clean",
        "trust": r.get("trust"),
        "footing": {
            "anchor_key": m.get("anchor_key"),
            "engine_version": m.get("engine_version"),
            "corrections": m.get("corrections"),
            "uncorrected": m.get("uncorrected"),
        },
        "U_RHE": float(point_u),
    }


def _candidate_pathway_ref_id(store: Any, structure_ref_id: int) -> int | None:
    """The newest `pathway` ref minted for this candidate (pathways carry
    ``meta.candidate_ref``) — what B2's ``routes_for_pathway`` reads."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'pathway' AND retired_at IS NULL "
            "AND meta->>'candidate_ref' = %s ORDER BY ref_id DESC LIMIT 1",
            (str(structure_ref_id),),
        ).fetchone()
    return int(row[0]) if row else None


# ── harvest hook ─────────────────────────────────────────────────────────


def _input_hash(
    quest_meta: dict[str, Any],
    structure_meta: dict[str, Any],
    jobs: list[dict[str, Any]],
    pathway_id: int | None,
    pathway_results: Any,
) -> str:
    """Fingerprint everything the window/Shapley compute reads — the scan
    anchors (per succeeded scan's id + footing), the pathway's stored
    results, the quest's objective/condition inputs and the candidate's
    resting-termination stamp — plus the precis version and the plugin
    modules' presence so a deploy or a landed adapter re-computes once.
    A succeeded job's ``che_sweep`` and a pathway's ``results`` are written
    once and never mutated, so ids + digests identify them exactly."""
    import precis

    basis = {
        "precis": precis.__version__,
        "staircase_masks": _staircase_masks() is not None,
        "staircase_routes": _staircase_routes() is not None,
        "scans": sorted(
            [
                int(j["ref_id"]),
                j.get("anchor_key"),
                j.get("engine_version"),
                json.dumps(j.get("corrections"), sort_keys=True, default=str),
            ]
            for j in jobs
        ),
        "pathway": [pathway_id, pathway_results],
        "quest": {
            k: quest_meta.get(k)
            for k in (
                "rubric_objectives",
                "window_objectives",
                "operating_conditions",
            )
        },
        "resting": structure_meta.get("resting_termination_at_point"),
    }
    blob = json.dumps(basis, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def stamp_window_measures(
    store: Any,
    quest_id: int,
    structure: Any,
    *,
    quest_meta: dict[str, Any] | None = None,
) -> str | None:
    """Stamp the window/Shapley measures + validity region on one
    candidate. The single call ``compute.harvest_measures`` makes, placed
    after the bulk-Pourbaix harvest.

    No-ops (returns ``None``, stamps nothing) when the candidate carries
    no coverage scans — the scans are what is being measured. The rubric
    declaration only decides whether the frontier ranks on the measures
    and the logbook announces the stamps (gr489165). ``quest_meta`` lets
    the tick pass the
    quest meta it already fetched. When the input hash
    (``meta.window_measures_input``) is unchanged the map/routes/Shapley
    recompute is skipped — the stamps it would write are identical by
    construction. Missing inputs degrade to notes, not crashes: no scan
    map → nothing to say; no pathway/routes adapter → only the validity
    stamp lands. Returns a one-line note on change.
    """
    from precis.quest import pourbaix_map
    from precis.quest.logbook import MEASURED_BY, append_entry
    from precis.utils import handle_registry

    if quest_meta is None:
        quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
        quest_meta = dict(quest.meta or {}) if quest is not None else {}

    # Fresh meta: the anchors harvest may have stamped
    # ``resting_termination_at_point`` earlier in this same tick.
    fresh = store.fetch_refs_by_ids({int(structure.id)}).get(int(structure.id))
    existing = dict(
        (fresh.meta if fresh is not None else getattr(structure, "meta", None)) or {}
    )
    jobs = pourbaix_map.scan_jobs(store, int(structure.id))
    if not jobs:
        return None
    pathway_id = _candidate_pathway_ref_id(store, int(structure.id))
    pw_results = None
    if pathway_id is not None:
        pw = store.fetch_refs_by_ids({pathway_id}).get(pathway_id)
        if pw is not None:
            pw_results = (pw.meta or {}).get("results")
    input_hash = _input_hash(quest_meta, existing, jobs, pathway_id, pw_results)
    if existing.get(WINDOW_INPUTS_KEY) == input_hash:
        return None

    maps = pourbaix_map.maps_for_structure(store, structure, jobs=jobs)
    if not maps:
        return None
    m = maps[0]
    sm = _staircase_masks()
    if sm is None:
        return None

    notes: list[str] = []
    if len(maps) > 1:
        notes.append(
            f"{len(maps)} footings — newest footing's map used "
            f"(anchor_key {str(m.get('anchor_key'))[:12]}…)"
        )
    point_u = _operating_point_u(quest_meta)
    wanted_key = _wanted_key(m, point_u, existing)
    if wanted_key is _NO_KEY:
        notes.append(
            "no operating point (meta.operating_conditions.U_RHE or "
            "resting_termination_at_point) — resting mask not applied"
        )
    resting = _resting_map_for(sm, m, wanted_key)
    if resting is None:
        notes.append("map carries no U_RHE_window/grid — no resting map")

    routes = her = pcet_routes = None
    if pathway_id is None:
        notes.append("no pathway ref for this candidate — no staircase routes")
    else:
        routes_mod = _staircase_routes()
        if routes_mod is None:
            notes.append("precis_pathway.staircase_routes absent — no routes")
        else:
            try:
                sr = routes_mod.routes_for_pathway(store, pathway_id)
                routes = list(getattr(sr, "routes", None) or [])
                pcet_routes = list(getattr(sr, "pcet_routes", None) or [])
                her = getattr(sr, "her", None)
                missing = getattr(sr, "missing", None) or []
                if missing:
                    notes.append(
                        f"routes missing rungs: {', '.join(map(str, missing))}"
                    )
                # gr489168: a quest that declares the HER margin against a
                # pathway run without ``network.her`` gets the name of the
                # missing input, not a silent ``None``.
                if her is None and "her_margin_at_Uopt" in _declared_objective_keys(
                    quest_meta
                ):
                    pw_meta = (pw.meta or {}) if pw is not None else {}
                    pw_config = pw_meta.get("config")
                    net = (
                        pw_config.get("network")
                        if isinstance(pw_config, dict)
                        else None
                    )
                    if not (isinstance(net, dict) and net.get("her")):
                        notes.append(
                            "quest declares her_margin_at_Uopt but the pathway "
                            "ran without network.her — set network.her in the "
                            "pathway config and re-run; the margin stays unset"
                        )
            except Exception:
                log.exception(
                    "stamp_window_measures: routes_for_pathway failed for %s",
                    structure.id,
                )
                notes.append("routes_for_pathway failed — no routes")

    cfg = _window_config(quest_meta)
    out = window_measures(
        resting,
        routes,
        her,
        eps_eV=_num(cfg.get("eps_eV")),
        her_margin_eV=_num(cfg.get("her_margin_eV")) or 0.0,
        reset_eta_eV=_num(cfg.get("reset_eta_eV")),
        reset_ea_floor_eV=_num(cfg.get("reset_ea_floor_eV")) or 0.0,
        pcet_routes=pcet_routes,
    )
    all_notes = notes + list(out.get("window_notes") or [])

    desired: dict[str, Any] = {}
    for k in WINDOW_MEASURE_KEYS:
        v = out.get(k)
        desired[k] = (
            float(v)
            if isinstance(v, (int, float)) and not isinstance(v, bool)
            else None
        )
    desired["bottleneck_rung"] = out.get("bottleneck_rung") or None
    desired["reset_limited_by"] = out.get("reset_limited_by") or None
    desired["reset_estimators"] = out.get("reset_estimators") or None
    desired["window_notes"] = all_notes or None
    validity = _validity_region_at_point(m, point_u)
    if validity is not None:
        desired["validity_region_at_point"] = validity
    desired[WINDOW_INPUTS_KEY] = input_hash

    changed = {
        k: v
        for k, v in desired.items()
        if existing.get(k) != v and (v is not None or existing.get(k) is not None)
    }
    if not changed:
        return None
    store.stamp_ref_meta(structure.id, changed)
    if set(changed) == {WINDOW_INPUTS_KEY}:
        # Inputs changed but every measure came out identical — restamp the
        # hash (so the recompute isn't paid again) but report nothing.
        return None

    handle = (
        handle_registry.try_format("structure", structure.id)
        or f"structure:{structure.id}"
    )
    measured = sorted(
        k for k, v in changed.items() if k in WINDOW_MEASURE_KEYS and v is not None
    )
    text = (
        f"window measures for [{handle}]: "
        + (", ".join(measured) if measured else "none yet")
        + (f" — {'; '.join(all_notes)}" if all_notes else "")
    )
    if measured and window_objectives_enabled(quest_meta):
        append_entry(store, quest_id, text=text, entry_type="result", by=MEASURED_BY)
    return text


# ── active anchor placement (propose only) ────────────────────────────────


def propose_anchor_scans(
    store: Any, quest_id: int, *, top_n: int = 3
) -> list[dict[str, Any]]:
    """Rank the frontier members' widest propagated-boundary strips and
    return proposed ``surface_coverage_scan`` params — **propose only**:
    the list is returned and summarised on the quest logbook as a note;
    minting the jobs is the dispatcher's call (B1), not this module's.

    Each proposal targets one candidate's top ``needs_anchor`` strip (the
    boundary whose propagated band is widest — uncertainty source 2, whose
    remedy is "add an anchor"): the two terminations it separates and a
    coverage-grid extension bracketing them, so the next scan resolves the
    boundary the Shapley field can't currently place.
    """
    from precis.quest import pourbaix_map
    from precis.quest.frontier import quest_frontier
    from precis.quest.logbook import MEASURED_BY, append_entry

    fr = quest_frontier(store, quest_id)
    proposals: list[dict[str, Any]] = []
    for c in fr.frontier:
        sref = store.fetch_refs_by_ids({c.ref_id}).get(c.ref_id)
        if sref is None:
            continue
        for m in pourbaix_map.maps_for_structure(store, sref):
            # Widest propagated band, chosen here rather than trusting the
            # map's list order.
            ranked = [
                (b, st)
                for st in (m.get("needs_anchor") or [])
                if isinstance(st, dict)
                and (b := _num(st.get("band_propagated"))) is not None
            ]
            if not ranked:
                continue
            band, strip = max(ranked, key=lambda bs: bs[0])
            below, above = strip.get("below"), strip.get("above")
            adsorbates = sorted(
                {
                    str(t["adsorbate"])
                    for t in (below, above)
                    if isinstance(t, dict) and t.get("adsorbate")
                }
            )
            coverages = sorted(
                {
                    int(t["n"])
                    for t in (below, above)
                    if isinstance(t, dict) and isinstance(t.get("n"), int)
                }
            )
            proposals.append(
                {
                    "structure_ref": c.ref_id,
                    "handle": c.handle,
                    "anchor_key": m.get("anchor_key"),
                    "job_type": "surface_coverage_scan",
                    "params": {
                        "around_U_RHE": _num(strip.get("U_RHE")),
                        "adsorbates": adsorbates,
                        "coverage_grid_extension": {
                            "below": below,
                            "above": above,
                            "n": coverages,
                        },
                    },
                    "band_propagated_V": band,
                    "reason": (
                        f"widest propagated band on {c.handle}: ±{band:.3f} V at "
                        f"U_RHE {_num(strip.get('U_RHE')) or 0.0:+.3f} V"
                    ),
                }
            )
    proposals.sort(key=lambda p: -p["band_propagated_V"])
    proposals = proposals[: max(int(top_n), 0)]

    signature = [
        [p["structure_ref"], round(p["params"]["around_U_RHE"] or 0.0, 4)]
        for p in proposals
    ]
    quest = store.fetch_refs_by_ids({quest_id}).get(quest_id)
    # Logbook/meta writes only on a quest that opted into window objectives;
    # an unflagged quest gets the proposals back and nothing written.
    if (
        proposals
        and quest is not None
        and window_objectives_enabled(dict(quest.meta or {}))
        and (quest.meta or {}).get("window_anchor_proposals") != signature
    ):
        lines = "; ".join(p["reason"] for p in proposals)
        append_entry(
            store,
            quest_id,
            text=f"anchor-scan proposal(s), propose-only: {lines}",
            entry_type="note",
            by=MEASURED_BY,
        )
        store.stamp_ref_meta(quest_id, {"window_anchor_proposals": signature})
    return proposals


__all__ = [
    "WINDOW_INPUTS_KEY",
    "WINDOW_MEASURE_KEYS",
    "propose_anchor_scans",
    "stamp_window_measures",
    "window_measures",
    "window_objectives_enabled",
]
