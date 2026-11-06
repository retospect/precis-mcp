"""Read the two separable operating optima from stored pathway evidence.

CHE only re-tilts thermodynamics at the stored temperature; the kinetics
sweep has no potential axis. Keeping those domains and their source records
separate prevents an apparent joint optimum. Coverage masks are rebuilt
from stored scan regions on the pathway's own slab, never from a sibling
slab or a candidate's possibly newer aggregate window stamps. No engine,
job mint or metadata write belongs on this read path. Window measures need
a recorded scan temperature matching the pathway; CHE cannot extrapolate
the resting-state map to another temperature.
"""

from __future__ import annotations

import json
import math
from typing import Any

import numpy as np

from precis.quest import pourbaix_map, staircase, window_objectives

from . import staircase_masks
from .staircase_routes import StaircaseRoutes, routes_for_pathway
from .staircase_states import pathway_coads


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) else None


def _show(value: Any) -> str:
    number = _number(value)
    return "unrecorded" if number is None else f"{number:.6g}"


def _run_u(meta: dict[str, Any], handle: str) -> str:
    results = _record(meta.get("results"))
    # A recompute may override the profile; only result evidence describes
    # the stored run. Prefer the sweep's own evidence over the solve's.
    for key in ("kinetics_sweep", "kinetics_v2"):
        environment = _record(_record(results.get(key)).get("environment"))
        raw = _record(environment.get("electrochemical")).get("U_vs_RHE")
        if _number(raw) is not None:
            return (
                f"{_show(raw)} V vs RHE (source: {handle} "
                f"results.{key}.environment.electrochemical.U_vs_RHE)"
            )
    return "unknown — run U is not recorded in the stored sweep or solve"


def _coverage_next(candidate: Any, handle: str) -> str:
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return (
            "Next: put(kind='job', job_type='surface_coverage_scan', "
            f"parent_id={candidate}, params={{'structure_ref': {candidate}, "
            "'model_index': 0, 'config': <reaction config>}) — use the stored "
            f"reaction config from get(kind='pathway', id='{handle}', view='config')."
        )
    return (
        "Next: get(kind='skill', id='precis-pathway-help') — "
        "Scan a candidate's coverage anchors; identify this pathway's slab first."
    )


def _feasible_line(resting: staircase_masks.RestingMap, sr: StaircaseRoutes) -> str:
    """Report sampled intervals for the same best-window route as the measures."""
    windows = [
        (route.name, staircase_masks.operating_window(resting, route, her=sr.her))
        for route in sr.pcet_routes
        if route.rungs
    ]
    if not windows:
        return "Feasible U_RHE intervals (sampled): unavailable — no usable route."
    name, window = max(windows, key=lambda item: (item[1].fraction, item[1].area))
    live = np.flatnonzero(window.mask.any(axis=0))
    # Do not span forbidden gaps between separate resting/viability regions.
    runs = np.split(live, np.flatnonzero(np.diff(live) > 1) + 1) if live.size else []
    intervals = ", ".join(
        f"[{_show(float(resting.u_rhe[run[0]]))}, "
        f"{_show(float(resting.u_rhe[run[-1]]))}]"
        for run in runs
    )
    return (
        f"Feasible U_RHE intervals (sampled): {intervals or 'none'} V vs RHE "
        f"(route: {name}; resting ∩ PCET rungs ∩ HER where recorded)."
    )


def _coverage_lines(
    store: Any, ref: Any, sr: StaircaseRoutes, handle: str
) -> list[str]:
    meta = _record(ref.meta)
    candidate = meta.get("candidate_ref")
    structures = staircase.structures_for_pathway(store, int(ref.id))
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        # candidate_ref is authoritative even when another linked slab has scans.
        structure = store.get_ref(kind="structure", id=candidate)
        structures = [structure] if structure is not None else []
    elif len(structures) != 1:
        structures = []
    lines: list[str] = []
    usable_map_found = False
    for structure in structures:
        coads = pathway_coads(store, meta, _record(structure.meta))
        for entry in pourbaix_map.maps_for_structure(store, structure):
            bounds = entry.get("U_RHE_window") or []
            ph_bounds = entry.get("pH_window") or []
            if (
                len(bounds) != 2
                or len(ph_bounds) != 2
                or any(_number(v) is None for v in [*bounds, *ph_bounds])
                or not entry.get("regions")
            ):
                continue
            jobs = ", ".join(f"jo{j}" for j in entry.get("scan_jobs") or [])
            source = (
                f"{handle} results.che_affine + results.her; st{structure.id} "
                f"coverage scans {jobs or 'unrecorded'}; "
                f"sweep jo{entry.get('sweep_job')}"
            )
            lines.append(f"Window/Shapley source: {source}")
            scan_t = _number(_record(entry.get("conditions")).get("temperature"))
            # Allow serialization noise, not extrapolation of the resting state.
            if (
                scan_t is None
                or sr.T is None
                or not math.isclose(scan_t, sr.T, rel_tol=0.0, abs_tol=1e-6)
            ):
                scan_at = (
                    "an unrecorded temperature"
                    if scan_t is None
                    else f"{_show(scan_t)} K"
                )
                lines.append(
                    f"Resting state is from a scan at {scan_at}; "
                    f"validity at the pathway's T = {_show(sr.T)} K is unavailable. "
                    "Window area and Shapley unavailable."
                )
                continue
            u_axis = np.linspace(
                float(bounds[0]),
                float(bounds[1]),
                max(int(entry.get("grid") or 151), 2),
            )
            ph_axis = np.linspace(float(ph_bounds[0]), float(ph_bounds[1]), 29)
            regions = [staircase.resting_region(entry, float(u)) for u in u_axis]
            cols = np.array(
                [r is not None and staircase.region_coads(r) == coads for r in regions]
            )
            resting = staircase_masks.RestingMap(
                u_axis,
                ph_axis,
                np.broadcast_to(cols, (len(ph_axis), len(u_axis))),
            )
            measures = window_objectives.window_measures(
                resting, sr.routes, sr.her, pcet_routes=sr.pcet_routes
            )
            usable_map_found = True
            lines.extend(
                [
                    f"U_RHE scan domain = {bounds} V; pH display window = {ph_bounds}; "
                    "pH is flat on RHE (no unique optimal pH).",
                    f"Resting termination required: {staircase.assumed_label(coads)}.",
                    _feasible_line(resting, sr),
                ]
            )
            for key in (
                "window_area_VpH",
                "window_fraction",
                "shapley_sumsq_raw",
                "shapley_ipr",
                "ridge_churn",
                "her_margin_at_Uopt",
            ):
                lines.append(f"{key} = {_show(measures.get(key))}")
            lines.append(
                f"bottleneck_rung = {measures.get('bottleneck_rung') or 'none'}"
            )
            lines.extend(str(n) for n in measures.get("window_notes") or [])
            for key in ("incomplete", "dropped", "notes"):
                lines.extend(str(n) for n in entry.get(key) or [])
    if not usable_map_found:
        lines.extend(
            [
                "No stored coverage scan with a usable resting-state map: resting state "
                "unknown; pathway energetics only. Window area and Shapley unavailable.",
                _coverage_next(candidate, handle),
            ]
        )
    return lines


def _thermodynamic_lines(store: Any, ref: Any, handle: str) -> list[str]:
    sr = routes_for_pathway(store, ref)
    lines = [
        "Thermodynamic: thermodynamic window at the stored T.",
        f"T = {_show(sr.T)} K (source: {handle} results.che_affine.T).",
    ]
    if not any(route.rungs for route in sr.routes):
        lines.extend(
            [
                "No stored thermodynamic data: no usable CHE staircase.",
                f"Next: get(kind='pathway', id='{handle}', view='config'), then "
                "put(kind='pathway', text=<stored config YAML>) to run the pathway "
                "with CHE enabled on the current engine.",
            ]
        )
        return lines
    per_route = (
        _record(_record(_record(ref.meta).get("results")).get("che_affine")).get(
            "per_route"
        )
        or []
    )
    for index, route in enumerate(sr.routes):
        stats = sr.route_stats.get(route.name, {})
        stored_stats = _record(per_route[index]) if index < len(per_route) else {}
        pcet = sr.pcet_routes[index]
        blocker = max(
            (r for r in pcet.rungs if r.n_e == 0 and r.delta_g0 > r.eps),
            key=lambda r: r.delta_g0 - r.eps,
            default=None,
        )
        limiting = (
            f"n/a — blocked by {blocker.name} "
            f"(delta_g0 = {_show(blocker.delta_g0)} eV, U-flat)"
            if blocker is not None
            else f"{_show(stats.get('U_L'))} V vs RHE (derived in the PCET convention)"
        )
        engine_label = (
            "engine evaluation potential" if blocker is not None else "engine U_L"
        )
        lines.append(
            f"Route {route.name}: U_L = {limiting}; "
            f"U_opt = {_show(stats.get('U_opt'))} V vs RHE; "
            f"span_at_UL = {_show(stats.get('span_at_UL'))} eV "
            f"(at {engine_label} = {_show(stats.get('U_L_engine'))} V vs RHE); "
            f"span_at_Uopt = {_show(stored_stats.get('span_at_Uopt'))} eV "
            f"(source: {handle} results.che_affine.per_route[{index}])."
        )
    lines.extend(_coverage_lines(store, ref, sr, handle))
    lines.extend(sr.missing)
    return lines


def _kinetic_lines(meta: dict[str, Any], handle: str) -> list[str]:
    results = _record(meta.get("results"))
    sweep = _record(results.get("kinetics_sweep"))
    optimum = _record(sweep.get("optimum"))
    source = f"{handle} results.kinetics_sweep"
    lines = [
        "Kinetic: kinetic optimum at the run U.",
        f"Run U: {_run_u(meta, handle)}.",
        "The stored sweep varies T, p_total and composition; it has no U axis.",
    ]
    if _number(optimum.get("value")) is None:
        lines.extend(
            [
                f"No stored kinetic optimum with a finite objective (source: {source}).",
                f"Next: put(kind='pathway', id='{handle}', mode='recompute_kinetics', "
                "args={'sweep': {'axes': {'T': [350, 900], 'p_total': [0.1, 100]}, "
                "'n': 64, 'composition': 'fixed'}}) "
                "after computing pathway energies; supply environment and objective "
                "if absent, including the desired subject_to rate floor.",
            ]
        )
    else:
        lines.extend(
            [
                f"Source: {source}.optimum (stored selection; no new optimization).",
                f"T = {_show(optimum.get('T'))} K; "
                f"p_total = {_show(optimum.get('p_total'))} bar; "
                f"composition = {json.dumps(optimum.get('composition'), sort_keys=True)}.",
                f"objective = {optimum.get('objective', 'unrecorded')}; "
                f"value = {_show(optimum.get('value'))}; "
                f"target = {sweep.get('target', 'unrecorded')} (source: {source}.target).",
            ]
        )
    floor = sweep.get("subject_to", optimum.get("subject_to"))
    if floor is not None:
        location = "subject_to" if "subject_to" in sweep else "optimum.subject_to"
        lines.append(
            f"subject_to = {json.dumps(floor, sort_keys=True)} (source: {source}.{location})."
        )
    else:
        lines.append(
            "subject_to rate floor unrecorded in the sweep; cannot verify the applied floor."
        )
        declared = _record(_record(meta.get("config")).get("objective")).get(
            "subject_to"
        )
        if declared is not None:
            lines.append(
                f"Declared subject_to = {json.dumps(declared, sort_keys=True)} "
                f"(source: {handle} config.objective.subject_to; "
                "not proof of the sweep's effective constraint)."
            )
    lines.extend(str(n) for n in sweep.get("warnings") or [])
    if results.get("kinetics_sweep_error"):
        lines.append(
            f"{handle} results.kinetics_sweep_error: {results['kinetics_sweep_error']}"
        )
    return lines


def optimum_text(store: Any, ref: Any) -> str:
    """Render both halves independently, including absent-data next calls."""
    handle = f"pw{ref.id}"
    return "\n".join(
        [
            "Separable operating optima — stored data only.",
            *_thermodynamic_lines(store, ref, handle),
            "",
            *_kinetic_lines(_record(ref.meta), handle),
            "",
            "No joint optimum: CHE is thermodynamic post-processing. A joint "
            "objective awaits a U axis on the kinetics sweep (not yet specified; "
            "see the slab-environment-optimizer and kinetics-engine-v2 specs).",
        ]
    )
