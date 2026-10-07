"""Bulk Pourbaix verdict for a host phase over a U/pH window.

The engine half of the bulk Pourbaix gate; the job that feeds it is
``precis.workers.job_types.pourbaix_bulk``, and the quest gate that will
read the verdict (dispatch, stamp, rule-out) is
``docs/backlog/pourbaix-quest-gate.md``.

**Why.** Catalyst quests rank on reaction energetics. A candidate whose bulk
dissolves or turns into another phase at the electrolysis conditions cannot
be the catalyst that was modelled. A bulk verdict is a necessary condition,
not a sufficient one: a bulk-stable phase can still restructure at the
surface under bias (surface Pourbaix is not modelled here, nor are dopant-in-
alloy stability, kinetics, or MLIP/DFT formation energies).

**Inputs.** Pourbaix entries for a chemical system (Materials Project,
ion-corrected per Persson et al. 2012, or hand-built for tests), a
host-phase composition and its dopants, an operating point (U_RHE, pH), an
optional window and a grid size (default 5 × 5). V_SHE = U_RHE − PREFAC·pH
with pymatgen's own ``PREFAC`` (0.0591), so the conversion and the diagram
agree. MP entries hold solids and aqueous ions, not gases: a decomposition
that would evolve PH3, H2 or NH3 is not represented, and every verdict text
says so.

**Matching.** :func:`verdict` matches the host phase to the nearest MP solid
inside its chemsys: L1 distance over non-O/H element fractions (slabs are
rarely stoichiometric — a Cu3P(001) slab at 26:10 is 0.056 from Cu3P), ties
broken by the all-element distance (so a Cu host matches Cu, not Cu2O),
then by the lowest ``energy_per_atom``. Past :data:`MATCH_DISTANCE_MAX`
(0.15) the verdict is ``unmatched`` — a flag, never a rule-out. The diagram's
``comp_dict`` is the matched entry's composition, so
``get_decomposition_energy`` cannot raise on a mismatch. A chemsys over
:data:`MAX_CHEMSYS_ELEMENTS` (3) non-O/H elements is refused: the
multi-element build is combinatorial. ``nproc=None`` is pymatgen's serial
path; any integer, even 1, starts a multiprocessing Pool in the worker.

**Classification.** At each (U, pH) the stable domain is classified over its
whole ``entry_list``, per host element:

* every host element in an aqueous ion → ``dissolved``;
* some host elements in ions, some in solids → ``leached`` (the surviving
  solids and the leached ions are named; Cu3P → Cu(s) + H2PO4⁻ at a
  cathodic point);
* every host element in solids → ``oxidised`` (the domain's solids carry
  more O per host atom than the matched phase) or ``transformed`` (fewer O,
  or the same O in a different solid: a hydride, a reduced oxide, another
  stoichiometry — named).

Those four all require ΔG_pbx (``get_decomposition_energy``, eV/atom) >
``stability_tol`` (default 0.1, of the order of the GGA error, so it is a
recomputable parameter rather than a final threshold); at or under it the
verdict is ``stable`` whatever domain is lower, and the point's ``note``
names that domain and the margin. ``worst_in_window`` ranks by
:data:`VERDICT_ORDER` (dissolved > leached > transformed > oxidised >
unmatched > stable); ``dissolved_everywhere`` is true only when the point
and every grid cell are dissolved.

**pymatgen workaround.** A multi-element ``PourbaixDiagram`` never keeps the
lone entry whose composition equals ``comp_dict``: ``process_multientry``
reads the product coefficient through ``Reaction.get_coeff``, which finds
the identical reactant first and returns −1, so the positive-coefficient
check drops it (reproduced on pymatgen 2026.5.4 and 2026.9.24; filed
upstream as materialsproject/pymatgen#4709 — delete the restore once it is
fixed there). A truly
stable Cu3P would then read as a phantom ``leached``. When ΔG_pbx <
−:data:`RESTORE_EPS` (1e-6 eV/atom; smaller negatives are numerical noise),
the matched phase is restored as the domain with ΔG_pbx 0, and the cell
carries ``domain_restored: true`` so the result says the engine overrode
the diagram.

**Dopants** are not assessed in the alloy (MP holds no dilute-site alloy
energies). Each gets a separate elemental verdict under
:data:`DOPANT_FLAG` with ``verdict_source: "elemental"``; it never drives a
rule-out. The host verdict carries ``verdict_source: "host_phase"``.

**Recomputable.** The result carries its inputs (ΔG_pbx, domain names,
U_RHE, pH, ion_conc_M, window, MP version, matched phase and distance,
composition path, ``stability_tol``) plus the source line "Materials
Project <version>, CC-BY 4.0", so a caller can tell when a stored verdict is
stale and recompute it.

Subtree rule: this module imports pymatgen and mp_api only, nothing from
``precis`` (``precis_dft`` package docstring). Both are the ``[pourbaix]``
extra; they are imported lazily so importing this module never needs them.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, cast

#: Verdict values, worst first — the order ``worst_in_window`` ranks by.
VERDICT_ORDER: tuple[str, ...] = (
    "dissolved",
    "leached",
    "transformed",
    "oxidised",
    "unmatched",
    "stable",
)

#: Default tolerance (eV/atom) for calling the matched phase ``stable``: the
#: GGA formation-energy error is of the same order, so the number is a
#: recomputable job parameter rather than a final threshold.
DEFAULT_STABILITY_TOL = 0.1

#: Default dissolved-ion activity (M) — the Pourbaix-atlas corrosion
#: convention MP's own diagrams use.
DEFAULT_ION_CONC_M = 1e-6

#: Default grid size per window axis (5 × 5 points).
DEFAULT_GRID = 5

#: Largest L1 distance (over non-O/H element fractions) between the host
#: phase and the nearest MP solid that still counts as a match.
MATCH_DISTANCE_MAX = 0.15

#: A chemical system with more non-O/H elements than this is refused: the
#: multi-element ``PourbaixDiagram`` build is combinatorial.
MAX_CHEMSYS_ELEMENTS = 3

#: Flag recorded for each dopant: it is not assessed in the alloy.
DOPANT_FLAG = "pourbaix:dopant-unassessed"

#: The necessary-not-sufficient caveat every verdict text carries.
NECESSARY_NOT_SUFFICIENT = (
    "A bulk verdict is a necessary condition, not a sufficient one: a "
    "bulk-stable phase can still restructure at the surface under bias."
)

#: What the MP Pourbaix entries cannot represent.
GAS_LIMIT = (
    "MP Pourbaix entries hold solids and aqueous ions, not gases; a "
    "decomposition that would evolve PH3, H2 or NH3 is not represented."
)

_OH = frozenset({"O", "H"})


ION_REFERENCE_URL = "https://contribs-api.materialsproject.org/contributions/"
ION_REFERENCE_FIELDS = "identifier,formula,data"


def fetch_ion_reference_data(api_key: str) -> list[dict[str, Any]]:
    """Fetch MP's ion_ref_data project over its supported Contribs REST API.

    This is the exact endpoint and field selection used by mp-api's
    ``MPRester.get_ion_reference_data``; using httpx avoids importing its
    optional MPContribs client (whose transitive Pint bounds conflict with
    the ``[estimate]`` extra). Results are paginated and schema-checked.
    """
    import httpx

    records: list[dict[str, Any]] = []
    total_pages: int | None = None
    page = 1
    while total_pages is None or page <= total_pages:
        response = httpx.get(
            ION_REFERENCE_URL,
            params={
                "project": "ion_ref_data",
                "_fields": ION_REFERENCE_FIELDS,
                "page": page,
            },
            headers={"x-api-key": api_key},
            timeout=30.0,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            raise ValueError("MP ion-reference response has an invalid data envelope")
        if not isinstance(payload.get("total_pages"), int):
            raise ValueError("MP ion-reference response is missing integer total_pages")
        if total_pages is None:
            total_pages = payload["total_pages"]
            if total_pages < 1:
                raise ValueError("MP ion-reference response has no pages")
        elif payload["total_pages"] != total_pages:
            raise ValueError("MP ion-reference response changed total_pages mid-fetch")
        for row in payload["data"]:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("identifier"), str)
                or not isinstance(row.get("formula"), str)
                or not isinstance(row.get("data"), dict)
            ):
                raise ValueError("MP ion-reference response contains an invalid record")
            records.append(row)
        page += 1
    return records


def prefac() -> float:
    """pymatgen's Nernst prefactor (``PREFAC``, 0.0591 V/pH), imported so the
    RHE→SHE conversion and the diagram use the same number."""
    from pymatgen.analysis.pourbaix_diagram import PREFAC

    return float(PREFAC)


def v_she(u_rhe: float, ph: float) -> float:
    """V vs SHE for a potential on the RHE scale: ``U_RHE − PREFAC·pH``."""
    return float(u_rhe) - prefac() * float(ph)


def source_line(mp_version: str | None) -> str:
    """The data source and licence line a verdict carries."""
    return f"Materials Project {mp_version or 'unknown version'}, CC-BY 4.0"


def non_oh_elements(composition: Any) -> frozenset[str]:
    """Element symbols of a pymatgen composition, minus O and H."""
    return frozenset(str(el.symbol) for el in composition.elements) - _OH


def chemsys(elements: Iterable[str]) -> str:
    """MP chemsys string (``"Cu-P"``) for a set of elements, O and H dropped
    (MP adds them to every Pourbaix fetch)."""
    return "-".join(sorted(set(elements) - _OH))


def worst(verdicts: Iterable[str]) -> str | None:
    """The worst verdict by :data:`VERDICT_ORDER`, or ``None`` when empty."""
    rank = {v: i for i, v in enumerate(VERDICT_ORDER)}
    seen = [v for v in verdicts if v in rank]
    return min(seen, key=lambda v: rank[v]) if seen else None


# ── host phase → MP solid ─────────────────────────────────────────────


def _fractions(amounts: Mapping[str, float], *, drop_oh: bool) -> dict[str, float]:
    kept = {
        str(el): float(n)
        for el, n in amounts.items()
        if float(n) > 0 and not (drop_oh and str(el) in _OH)
    }
    total = sum(kept.values())
    return {el: n / total for el, n in kept.items()} if total > 0 else {}


def _l1(a: Mapping[str, float], b: Mapping[str, float]) -> float:
    return sum(abs(a.get(el, 0.0) - b.get(el, 0.0)) for el in set(a) | set(b))


def _comp_amounts(composition: Any) -> dict[str, float]:
    return {str(el.symbol): float(n) for el, n in composition.items()}


def match_host_phase(
    entries: Sequence[Any], host: Mapping[str, float]
) -> dict[str, Any] | None:
    """Nearest MP solid to the host phase.

    Distance is the L1 distance over non-O/H element fractions. Ties (a pure
    Cu host is distance 0 from Cu, Cu2O and CuO alike) break on the L1
    distance over all element fractions including O/H, then on the lowest
    formation ``energy_per_atom`` (so the ground-state polymorph wins among
    same-formula solids). Only solids whose non-O/H elements lie inside the
    host's are candidates. Returns ``None`` when there is no candidate
    solid at all, else ``{"entry", "formula", "entry_id", "distance"}``.
    """
    host_elts = frozenset(_fractions(host, drop_oh=True))
    host_frac = _fractions(host, drop_oh=True)
    host_full = _fractions(host, drop_oh=False)
    best: tuple[float, float, float] | None = None
    best_entry: Any = None
    for pe in entries:
        if pe.phase_type != "Solid":
            continue
        elts = non_oh_elements(pe.composition)
        if not elts or not elts <= host_elts:
            continue
        amounts = _comp_amounts(pe.composition)
        key = (
            round(_l1(host_frac, _fractions(amounts, drop_oh=True)), 9),
            round(_l1(host_full, _fractions(amounts, drop_oh=False)), 9),
            float(pe.entry.energy_per_atom),
        )
        if best is None or key < best:
            best, best_entry = key, pe
    if best is None:
        return None
    return {
        "entry": best_entry,
        "formula": str(best_entry.composition.reduced_formula),
        "entry_id": best_entry.entry_id,
        "distance": float(best[0]),
    }


# ── classification ────────────────────────────────────────────────────


def _components(domain: Any) -> list[tuple[Any, float]]:
    """A stable domain's ``(entry, weight)`` components: a ``MultiEntry``'s
    ``entry_list`` with its weights, or the single ``PourbaixEntry`` at 1."""
    from pymatgen.analysis.pourbaix_diagram import MultiEntry

    if isinstance(domain, MultiEntry):
        return [
            (e, float(w))
            for e, w in zip(domain.entry_list, domain.weights, strict=True)
        ]
    return [(domain, 1.0)]


def _o_per_host_atom(parts: Iterable[tuple[Any, float]], host: frozenset[str]) -> float:
    """Oxygen atoms per host (non-O/H) atom over weighted compositions."""
    o = n = 0.0
    for comp, weight in parts:
        amounts = _comp_amounts(comp)
        o += weight * amounts.get("O", 0.0)
        n += weight * sum(v for el, v in amounts.items() if el in host)
    return o / n if n else 0.0


#: Below this (eV/atom) a negative ΔG_pbx is numerical noise, not the
#: matched phase lying under the diagram's hull.
RESTORE_EPS = 1e-6


def classify(
    domain: Any,
    host_elements: Iterable[str],
    *,
    matched: Any,
    dg_pbx: float,
    tol: float,
) -> dict[str, Any]:
    """Classify one stable domain for the host phase.

    ``dg_pbx <= tol`` → ``stable`` whatever kind of domain lies lower (ion
    or solid); when the domain is not the matched phase itself, ``note``
    names it and the margin. Past the tolerance, per host element: *solid*
    when any solid component of the domain holds it, else *ion*. All ion →
    ``dissolved``; mixed → ``leached``; all solid → ``oxidised`` when the
    domain's solids carry more O per host atom than the matched phase, else
    ``transformed`` (fewer O, or the same O in a different solid). Returns
    ``{"verdict", "domain", "solids", "ions"}`` (names of the host-bearing
    solid and ion components) plus ``note`` when set.
    """
    comps = _components(domain)
    host = frozenset(host_elements)
    solids = [
        (c, w)
        for c, w in comps
        if c.phase_type == "Solid" and non_oh_elements(c.composition) & host
    ]
    ions = [
        (c, w)
        for c, w in comps
        if c.phase_type == "Ion" and non_oh_elements(c.composition) & host
    ]
    in_solid = set().union(*(non_oh_elements(c.composition) for c, _w in solids)) & host
    # Sorted: a MultiEntry's entry_list order follows pymatgen's set
    # iteration (hash order), so unsorted names differ run to run.
    out: dict[str, Any] = {
        "domain": sorted(str(c.name) for c, _w in comps),
        "solids": sorted(str(c.name) for c, _w in solids),
        "ions": sorted(str(c.name) for c, _w in ions),
    }
    matched_formula = str(matched.composition.reduced_formula)
    is_matched = not ions and all(
        str(c.composition.reduced_formula) == matched_formula for c, _w in solids
    )
    if dg_pbx <= tol:
        out["verdict"] = "stable"
        if not is_matched:
            # Within tolerance of the hull, though another domain is lower:
            # say which and by how much, so a near-boundary "stable" shows.
            # Clamped: a ΔG_pbx in (−RESTORE_EPS, 0) is noise, not "below".
            out["note"] = (
                f"{matched_formula} is {max(dg_pbx, 0.0):.3f} eV/atom above "
                f"{' + '.join(out['domain'])} (within stability_tol {tol})"
            )
    elif not in_solid:
        out["verdict"] = "dissolved"
    elif in_solid != host:
        out["verdict"] = "leached"
    else:
        domain_o = _o_per_host_atom(((c.composition, w) for c, w in solids), host)
        matched_o = _o_per_host_atom([(matched.composition, 1.0)], host)
        out["verdict"] = "oxidised" if domain_o > matched_o + 1e-9 else "transformed"
    return out


def _axis(span: Sequence[float] | None, centre: float, n: int) -> list[float]:
    if not span:
        return [float(centre)]
    lo, hi = float(span[0]), float(span[1])
    if n <= 1 or math.isclose(lo, hi):
        return [lo]
    return [lo + (hi - lo) * i / (n - 1) for i in range(n)]


def _point_eval(
    diagram: Any,
    matched: Any,
    host_elements: frozenset[str],
    u_rhe: float,
    ph: float,
    tol: float,
) -> dict[str, Any]:
    v = v_she(u_rhe, ph)
    dg = float(diagram.get_decomposition_energy(matched, ph, v))
    domain = diagram.get_stable_entry(ph, v)
    restored = dg < -RESTORE_EPS
    if restored:
        # The matched phase lies below every combination the diagram kept,
        # so it is itself the stable domain. A multi-element PourbaixDiagram
        # never keeps the lone entry whose composition equals ``comp_dict``:
        # ``process_multientry`` reads the product coefficient with
        # ``Reaction.get_coeff``, which finds the identical reactant first
        # and returns −1, failing the positive-coefficient check
        # (materialsproject/pymatgen#4709). Restore it
        # here; its ΔG_pbx is 0 by definition, and ``domain_restored`` says
        # the engine overrode the diagram.
        domain, dg = matched, 0.0
    cls = classify(domain, host_elements, matched=matched, dg_pbx=dg, tol=tol)
    return {
        "U_RHE": round(float(u_rhe), 6),
        "pH": round(float(ph), 6),
        "V_SHE": round(v, 6),
        "dG_pbx_eV_atom": round(dg, 6),
        "domain_restored": restored,
        **cls,
    }


class _SinglePhase:
    """Stand-in diagram for a pool holding only the matched solid."""

    def __init__(self, entry: Any) -> None:
        self._entry = entry

    def get_decomposition_energy(self, entry: Any, pH: float, V: float) -> float:
        return 0.0

    def get_stable_entry(self, pH: float, V: float) -> Any:
        return self._entry


def _unmatched_eval(u_rhe: float, ph: float) -> dict[str, Any]:
    return {
        "U_RHE": round(float(u_rhe), 6),
        "pH": round(float(ph), 6),
        "V_SHE": round(v_she(u_rhe, ph), 6),
        "dG_pbx_eV_atom": None,
        "domain_restored": False,
        "verdict": "unmatched",
        "domain": [],
        "solids": [],
        "ions": [],
    }


def _assess(
    entries: Sequence[Any],
    host: Mapping[str, float],
    point: Mapping[str, float],
    window: Mapping[str, Any] | None,
    tol: float,
    ion_conc_m: float,
    grid: int,
) -> dict[str, Any]:
    """Point + window verdict for one host composition over the entries
    whose non-O/H elements lie inside it."""
    host_elts = frozenset(_fractions(host, drop_oh=True))
    if len(host_elts) > MAX_CHEMSYS_ELEMENTS:
        raise ValueError(
            f"chemsys {chemsys(host_elts)} has {len(host_elts)} non-O/H elements; "
            f"at most {MAX_CHEMSYS_ELEMENTS} are supported"
        )
    pool = [
        pe
        for pe in entries
        if non_oh_elements(pe.composition)
        and non_oh_elements(pe.composition) <= host_elts
    ]
    u0, ph0 = float(point["U_RHE"]), float(point["pH"])
    us = _axis((window or {}).get("U_RHE"), u0, grid) if window else []
    phs = _axis((window or {}).get("pH"), ph0, grid) if window else []
    grid_pts = [(u, p) for u in us for p in phs]

    match = match_host_phase(pool, host)
    matched_info: dict[str, Any] = {
        "formula": match["formula"] if match else None,
        "entry_id": match["entry_id"] if match else None,
        "distance": round(match["distance"], 6) if match else None,
    }
    if match is None or match["distance"] > MATCH_DISTANCE_MAX:
        pt = _unmatched_eval(u0, ph0)
        cells = [_unmatched_eval(u, p) for u, p in grid_pts]
    else:
        from pymatgen.analysis.pourbaix_diagram import PourbaixDiagram

        matched = match["entry"]
        comp_elts = non_oh_elements(matched.composition)
        comp_dict = {
            el: amt
            for el, amt in _comp_amounts(matched.composition).items()
            if el in comp_elts
        }
        sub = [pe for pe in pool if non_oh_elements(pe.composition) <= comp_elts]
        diagram: Any
        if len(sub) == 1:
            # The matched solid is the only species: qhull cannot build a
            # one-plane diagram, and the answer is trivially that solid.
            diagram = _SinglePhase(matched)
        else:
            diagram = PourbaixDiagram(
                sub,
                comp_dict=comp_dict,
                conc_dict={el: float(ion_conc_m) for el in comp_elts},
                filter_solids=True,
                # None is pymatgen's serial path; any int (even 1) spins up a
                # multiprocessing Pool inside the worker daemon.
                nproc=None,
            )
        pt = _point_eval(diagram, matched, comp_elts, u0, ph0, tol)
        cells = [
            _point_eval(diagram, matched, comp_elts, u, p, tol) for u, p in grid_pts
        ]
    evaluated = [pt, *cells]
    return {
        "verdict": pt["verdict"],
        "point": pt,
        "grid": cells,
        "worst_in_window": worst(c["verdict"] for c in evaluated),
        "dissolved_everywhere": all(c["verdict"] == "dissolved" for c in evaluated),
        "dG_pbx_eV_atom": pt["dG_pbx_eV_atom"],
        "domain": pt["domain"],
        "matched_phase": matched_info,
    }


def verdict(
    entries: Sequence[Any],
    host: Mapping[str, float],
    dopants: Mapping[str, float] | Sequence[str],
    point: Mapping[str, float],
    window: Mapping[str, Any] | None = None,
    tol: float = DEFAULT_STABILITY_TOL,
    *,
    ion_conc_M: float = DEFAULT_ION_CONC_M,
    grid: int = DEFAULT_GRID,
    mp_version: str | None = None,
    composition_path: str | None = None,
) -> dict[str, Any]:
    """Bulk Pourbaix verdict for ``host`` at ``point`` and over ``window``.

    ``entries`` — ``PourbaixEntry`` objects covering the host chemsys and,
    for each dopant, its elemental chemsys (one list; each diagram takes the
    entries whose non-O/H elements lie inside its own system). ``host`` —
    the host-phase composition, dopants already removed (element → amount;
    counts or fractions). ``dopants`` — dopant elements, or element → host
    atom fraction. ``point`` — ``{"U_RHE": V, "pH": x}``. ``window`` —
    optional ``{"U_RHE": [lo, hi], "pH": [lo, hi]}``; an absent axis stays
    at the point's value. ``grid`` points per window axis.

    Raises ``ValueError`` for a host chemsys over
    :data:`MAX_CHEMSYS_ELEMENTS` non-O/H elements or an empty host.
    """
    if not _fractions(host, drop_oh=True):
        raise ValueError("host phase has no non-O/H element")
    tol = float(tol)
    grid = int(grid)
    win = dict(window) if window else None
    host_res = _assess(entries, host, point, win, tol, ion_conc_M, grid)

    dopant_fracs: dict[str, float | None] = (
        {str(k): float(v) for k, v in dopants.items()}
        if isinstance(dopants, Mapping)
        else {str(el): None for el in dopants}
    )
    dopant_rows: list[dict[str, Any]] = []
    for el, frac in sorted(dopant_fracs.items()):
        elemental = _assess(entries, {el: 1.0}, point, win, tol, ion_conc_M, grid)
        dopant_rows.append(
            {
                "element": el,
                "host_atom_fraction": frac,
                "flag": DOPANT_FLAG,
                "verdict_source": "elemental",
                "verdict": elemental["verdict"],
                "worst_in_window": elemental["worst_in_window"],
                "dissolved_everywhere": elemental["dissolved_everywhere"],
                "dG_pbx_eV_atom": elemental["dG_pbx_eV_atom"],
                "domain": elemental["domain"],
                "matched_phase": elemental["matched_phase"],
            }
        )

    flags = [DOPANT_FLAG] if dopant_rows else []

    matched = host_res["matched_phase"]
    basis = {
        "U_RHE": float(point["U_RHE"]),
        "pH": float(point["pH"]),
        "ion_conc_M": float(ion_conc_M),
        "window": win,
        "grid": grid,
        "stability_tol": tol,
        "mp_version": mp_version,
        "matched_phase": matched["formula"],
        "match_distance": matched["distance"],
        "composition_path": composition_path,
        "host_phase": _fractions(host, drop_oh=False),
        "dopants": sorted(dopant_fracs),
        "prefac": prefac(),
    }
    result: dict[str, Any] = {
        **host_res,
        "verdict_source": "host_phase",
        "host_phase": basis["host_phase"],
        "dopants": dopant_rows,
        "flags": flags,
        "basis": basis,
        "source": source_line(mp_version),
        "limits": [NECESSARY_NOT_SUFFICIENT, GAS_LIMIT],
    }
    result["text"] = render_text(result)
    return result


def _fmt_dg(dg: float | None) -> str:
    return "n/a" if dg is None else f"{dg:.3f} eV/atom"


def render_text(result: Mapping[str, Any]) -> str:
    """The agent-readable account of a :func:`verdict` result."""
    b = result["basis"]
    m = result["matched_phase"]
    pt = result["point"]
    if m["formula"] is None:
        phase = "no MP solid in the host chemsys"
    else:
        phase = f"{m['formula']} (L1 distance {m['distance']:.3f})"
    lines = [
        f"Bulk Pourbaix verdict at U_RHE {b['U_RHE']:+.3f} V, pH {b['pH']:g} "
        f"(V_SHE {pt['V_SHE']:+.3f}, ions {b['ion_conc_M']:g} M): "
        f"{result['verdict']} — host phase matched to {phase}; "
        f"ΔG_pbx {_fmt_dg(result['dG_pbx_eV_atom'])}; "
        f"domain {' + '.join(result['domain']) or 'n/a'}.",
    ]
    if pt["verdict"] == "leached":
        lines.append(
            f"Surviving solid(s): {', '.join(pt['solids'])}; "
            f"leached ion(s): {', '.join(pt['ions'])}."
        )
    elif pt["verdict"] in ("oxidised", "transformed"):
        lines.append(f"Solid(s) at the point: {', '.join(pt['solids'])}.")
    if b["window"]:
        lines.append(
            f"Window {b['window']} ({len(result['grid'])} grid points): worst "
            f"{result['worst_in_window']}; dissolved everywhere: "
            f"{'yes' if result['dissolved_everywhere'] else 'no'}."
        )
    else:
        lines.append("Point only (no window given).")
    for d in result["dopants"]:
        lines.append(
            f"Dopant {d['element']} not assessed in the alloy ({d['flag']}); "
            f"elemental verdict {d['verdict']} (worst in window "
            f"{d['worst_in_window']})."
        )
    lines.append(
        f"stability_tol {b['stability_tol']} eV/atom; composition path "
        f"{b['composition_path'] or 'n/a'}."
    )
    lines.extend(result["limits"])
    lines.append(f"Data: {result['source']}.")
    return "\n".join(lines)


# ── MP fetch + compact serialisation ──────────────────────────────────


def fetch_entries(api_key: str, chemsys_str: str) -> list[Any]:
    """Pourbaix entries for one chemsys from the Materials Project — one
    ``get_pourbaix_entries`` call (MP adds O and H itself)."""
    from mp_api.client import MPRester

    ion_reference_data = fetch_ion_reference_data(api_key)

    class _MPResterWithIonReferenceData(MPRester):
        """Use the supported REST-fetched references in the mp-api workflow."""

    def _ion_reference_data(_self: Any) -> list[dict[Any, Any]]:
        return ion_reference_data

    # mp-api decorates the base method with functools.lru_cache; a typed
    # override of that wrapper is an incompatible-override error for mypy
    # (and an unused ignore where the [estimate] extra is absent and
    # MPRester is Any), so the method is attached through an Any view.
    cast(
        Any, _MPResterWithIonReferenceData
    ).get_ion_reference_data = _ion_reference_data

    with _MPResterWithIonReferenceData(api_key) as mpr:
        return list(mpr.get_pourbaix_entries(chemsys_str))


def database_version(api_key: str) -> str:
    """The current MP database version (cheap; no entry fetch) — the cache
    key and the verdict's provenance."""
    from mp_api.client import MPRester

    with MPRester(api_key) as mpr:
        return str(mpr.get_database_version())


def entries_to_json(entries: Iterable[Any]) -> list[dict[str, Any]]:
    """Compact JSON form of Pourbaix entries — only what a ``PourbaixDiagram``
    reads (composition or ion, total energy, id, concentration). A
    ``ComputedEntry``'s ``energy`` already includes its corrections, so the
    rebuilt ``PDEntry`` reproduces the same diagram without the structure
    and parameter payload."""
    out: list[dict[str, Any]] = []
    for pe in entries:
        row: dict[str, Any] = {
            "energy": float(pe.entry.energy),
            "entry_id": pe.entry_id,
            "concentration": float(pe.concentration),
        }
        if pe.phase_type == "Ion":
            row["ion"] = pe.entry.ion.as_dict()
            row["name"] = str(pe.entry.name)
        else:
            row["composition"] = _comp_amounts(pe.entry.composition)
        out.append(row)
    return out


def entries_from_json(rows: Iterable[Mapping[str, Any]]) -> list[Any]:
    """Inverse of :func:`entries_to_json`."""
    from pymatgen.analysis.phase_diagram import PDEntry
    from pymatgen.analysis.pourbaix_diagram import IonEntry, PourbaixEntry
    from pymatgen.core import Composition
    from pymatgen.core.ion import Ion

    out: list[Any] = []
    for row in rows:
        entry: Any
        if "ion" in row:
            entry = IonEntry(
                Ion.from_dict(row["ion"]), float(row["energy"]), row.get("name")
            )
        else:
            entry = PDEntry(Composition(dict(row["composition"])), float(row["energy"]))
        out.append(
            PourbaixEntry(
                entry,
                entry_id=row.get("entry_id"),
                concentration=float(row.get("concentration", DEFAULT_ION_CONC_M)),
            )
        )
    return out


__all__ = [
    "DEFAULT_GRID",
    "DEFAULT_ION_CONC_M",
    "DEFAULT_STABILITY_TOL",
    "DOPANT_FLAG",
    "GAS_LIMIT",
    "MATCH_DISTANCE_MAX",
    "MAX_CHEMSYS_ELEMENTS",
    "NECESSARY_NOT_SUFFICIENT",
    "RESTORE_EPS",
    "VERDICT_ORDER",
    "chemsys",
    "classify",
    "database_version",
    "entries_from_json",
    "entries_to_json",
    "fetch_entries",
    "match_host_phase",
    "prefac",
    "render_text",
    "source_line",
    "v_she",
    "verdict",
    "worst",
]
