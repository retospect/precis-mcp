"""Read-only adapter for the stored environment objective points."""

from __future__ import annotations

from typing import Any

from precis.quest import pourbaix_map
from precis.quest.environment_optimizer import EnvironmentPoint, evaluate_point, number
from precis.utils.pathway_conditions import kinetics_environment

from .staircase_routes import routes_for_pathway
from .staircase_states import pathway_coads


def _floor(config: dict[str, Any]) -> float | None:
    constraints = (config.get("objective") or {}).get("subject_to") or {}
    # D7 subject_to uses [lower, upper] boxes on the named scalar.
    for key in ("tof", "tof_mf"):
        bounds = constraints.get(key)
        if isinstance(bounds, list | tuple) and len(bounds) == 2:
            return number(bounds[0])
    return None


def stored_points(
    meta: dict[str, Any], *, ion_conc_M: float | None
) -> list[EnvironmentPoint]:
    """Run point plus actual sweep rows, never the optimum's summary value."""
    results = meta.get("results") or {}
    config = meta.get("config") or {}
    conditions = config.get("conditions") or {}
    electrochem = config.get("electrochemistry") or {}
    che = results.get("che_affine") or {}
    u = None
    for raw in (conditions.get("potential_U"), electrochem.get("U_vs_RHE")):
        u = number(results.get("U_opt") if raw == "optimal" else raw)
        if u is not None:
            break
    ph = next(
        (
            x
            for raw in (conditions.get("pH"), electrochem.get("pH"), che.get("pH"))
            if (x := number(raw)) is not None
        ),
        None,
    )
    env = kinetics_environment({"kinetics_v2": results.get("kinetics_v2")})
    points = []
    if env is not None:
        points.append(
            EnvironmentPoint(
                u, ph, number(env.get("T")), env.get("pressures"), ion_conc_M
            )
        )
    for row in (results.get("kinetics_sweep") or {}).get("points") or []:
        if isinstance(row, dict):
            point = EnvironmentPoint(
                u, ph, number(row.get("T")), row.get("pressures"), ion_conc_M
            )
            if point not in points:
                points.append(point)
    return points or [EnvironmentPoint(u, ph, number(che.get("T")), None, ion_conc_M)]


def objective_text(store: Any, ref: Any) -> str:
    """Load the actual candidate's map and bulk evidence, then evaluate purely."""
    meta = ref.meta or {}
    results = meta.get("results") or {}
    config = meta.get("config") or {}
    staircase = routes_for_pathway(store, ref)
    candidate = meta.get("candidate_ref")
    structure = (
        store.get_ref(kind="structure", id=candidate)
        if isinstance(candidate, int) and not isinstance(candidate, bool)
        else None
    )
    candidate_meta = structure.meta or {} if structure is not None else {}
    maps = (
        pourbaix_map.maps_for_structure(store, structure)
        if structure is not None
        else []
    )
    # Distinct footings cannot be silently blended or selected by list order.
    map_entry = maps[0] if len(maps) == 1 else None
    bulk = (
        pourbaix_map.bulk_layer(
            candidate_meta, pourbaix_map.bulk_verdict(store, candidate_meta)
        )
        if candidate_meta
        else None
    )
    slab = config.get("slab") or {}
    known_surface = isinstance(slab.get("coads"), dict) or structure is not None
    coads = pathway_coads(store, meta, candidate_meta) if known_surface else None
    target = (
        results.get("target")
        or (results.get("kinetics_sweep") or {}).get("target")
        or (config.get("network") or {}).get("target")
    )
    floor = _floor(config)
    points = stored_points(meta, ion_conc_M=number((bulk or {}).get("ion_conc_M")))
    lines = [
        "Stored environment objectives (eV); J=min(m_th,m_kin,m_HER,s,r) under M.",
        "CHE varies U only at the stored thermodynamic footing; kinetics at the run U.",
        "No environment search or optimum is claimed by this read.",
    ]
    if len(maps) > 1:
        lines.append(
            "Multiple surface-map footings: termination unavailable until a footing is selected."
        )
    for index, point in enumerate(points, 1):
        lines.append(
            f"Point {index}: U_RHE={point.U}, pH={point.pH}, T={point.T} K, pressures={point.pressures} bar, ion_conc_M={point.ion_conc_M}"
        )
        evaluation = evaluate_point(
            results,
            staircase,
            point,
            target=target,
            tof_floor=floor,
            map_entry=map_entry,
            coads=coads,
            bulk=bulk,
        )
        for name, term in {
            **evaluation.terms,
            "M": evaluation.mask,
            "J": evaluation.objective,
        }.items():
            value = "None" if term.value is None else f"{term.value:+.6g}"
            unit = "" if name == "M" else " eV"
            flags = f" [{'; '.join(term.flags)}]" if term.flags else ""
            lines.append(f"  {name}={value}{unit}{flags}: {term.reason}")
        lines.append(f"  unmasked limiting term: {evaluation.limiting or 'unknown'}")
    return "\n".join(lines)
