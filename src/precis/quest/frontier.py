"""Quest Pareto frontier — the non-dominated candidate materials.

Slice 4b of the quest layer (docs/proposals/quest-layer.md §Materials are
`structure` servers). Every candidate a quest tries is a `structure` that
``serves`` it, carrying its relax **measures** (energy, max force, …). "Do
better" = push the **Pareto frontier** of those measures against the quest's
objective vector (its rubric). This module is the read-time computation of that
frontier: the non-dominated set is *the current best*, the dominated set is
*explored-and-beaten*, and the un-evaluated set is *awaiting a sim*.

The objective vector (which measures, minimise or maximise) is the machine
reading of the quest's prose rubric — an open question (docs, slice-4 Q3). For
now it defaults to **minimise energy** and can be overridden per quest via
``meta.rubric_objectives = [{"key": "energy", "sense": "min"}, …]``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: Default objective when a quest declares no rubric: the lowest-energy
#: (most stable) converged candidate wins.
DEFAULT_OBJECTIVES: tuple[tuple[str, str], ...] = (("energy", "min"),)

_VALID_SENSES = frozenset({"min", "max"})


@dataclass(frozen=True)
class Candidate:
    """A candidate material + the measures of its best converged relax."""

    ref_id: int
    handle: str
    name: str
    measures: dict[str, float]
    converged: bool
    #: The candidate's point in the quest's named param space (``meta.params``,
    #: §7.8). Rides along for a later optimizer advisor; never a ranking measure.
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontierResult:
    objectives: list[tuple[str, str]]
    frontier: list[Candidate] = field(default_factory=list)  # non-dominated
    dominated: list[Candidate] = field(default_factory=list)  # explored + beaten
    unevaluated: list[Candidate] = field(default_factory=list)  # no measures yet


def _dominates(a: Candidate, b: Candidate, objectives: list[tuple[str, str]]) -> bool:
    """True when ``a`` Pareto-dominates ``b`` over ``objectives``.

    ``a`` dominates ``b`` iff it is no worse on every objective and strictly
    better on at least one. Missing a measure on either side → not comparable
    (returns False), so a partially-measured candidate never dominates.
    """
    strictly_better = False
    for key, sense in objectives:
        av = a.measures.get(key)
        bv = b.measures.get(key)
        if av is None or bv is None:
            return False
        if sense == "min":
            if av > bv:
                return False
            if av < bv:
                strictly_better = True
        else:  # max
            if av < bv:
                return False
            if av > bv:
                strictly_better = True
    return strictly_better


def pareto_split(
    candidates: list[Candidate], objectives: list[tuple[str, str]]
) -> FrontierResult:
    """Partition ``candidates`` into frontier / dominated / unevaluated."""
    keys = [k for k, _ in objectives]
    evaluated = [
        c
        for c in candidates
        if c.converged and all(c.measures.get(k) is not None for k in keys)
    ]
    unevaluated = [c for c in candidates if c not in evaluated]

    frontier: list[Candidate] = []
    dominated: list[Candidate] = []
    for c in evaluated:
        if any(_dominates(o, c, objectives) for o in evaluated if o.ref_id != c.ref_id):
            dominated.append(c)
        else:
            frontier.append(c)
    return FrontierResult(
        objectives=objectives,
        frontier=frontier,
        dominated=dominated,
        unevaluated=unevaluated,
    )


def _objectives_for(store: Store, quest_id: int) -> list[tuple[str, str]]:
    ref = store.get_ref(kind="quest", id=quest_id)
    raw = (ref.meta or {}).get("rubric_objectives") if ref else None
    out: list[tuple[str, str]] = []
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "").strip()
            sense = str(item.get("sense") or "min").strip().lower()
            if key and sense in _VALID_SENSES:
                out.append((key, sense))
    return out or list(DEFAULT_OBJECTIVES)


#: struct_runs columns that are bookkeeping, not measures — never rank on these
#: (``converged`` is a bool and ``status``/``fidelity``/``model``/``created_at``
#: are non-numeric, so ``_numeric`` already filters them; these are the numeric
#: ones we must exclude by name).
_RUN_NON_MEASURE: frozenset[str] = frozenset({"id", "ref_id", "on_version"})


def _numeric(v: Any) -> float | None:
    """A measure value, or None. ``bool`` is an ``int`` subclass but never a measure."""
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _candidate_from_structure(store: Store, s: Any) -> Candidate:
    """Build a :class:`Candidate` from a structure ref + its measures.

    Measures are gathered **generically** (not a fixed column list) so the
    frontier can rank on any named objective a quest declares (barrier,
    formation-energy, selectivity, …). Two sources:

    1. every numeric field of the best converged ``struct_runs`` row — today
       ``energy`` / ``max_force`` / ``max_disp`` / ``n_steps``, plus any future
       run scalar, with no code change here;
    2. every numeric top-level key of ``structure.meta`` — the escape hatch a
       synthesis/harvest pass stamps computed measures onto. This is how the
       reaction **barrier** reaches the frontier: a catpath run over the
       candidate is harvested onto the candidate's own ``meta`` (Slice 3), so
       the frontier reads a plain scalar — no catpath import, no graph
       recompute. Fill-only: a stamped measure never clobbers a real relax
       measure of the same name.

    ``meta.params`` (the candidate's point in the quest param space, §7.8) rides
    along for a later optimizer advisor; it is not a measure.
    """
    from precis.utils import handle_registry

    handle = handle_registry.try_format("structure", s.id) or f"structure:{s.id}"
    name = (s.title or "").splitlines()[0] if s.title else handle
    runs = store.structure_runs(s.id)
    # Best = the most recent converged run (structure_runs is newest-first).
    best = next((r for r in runs if r.get("converged")), None)
    converged = best is not None

    measures: dict[str, float] = {}
    if best is not None:
        for k, v in best.items():
            if k in _RUN_NON_MEASURE:
                continue
            fv = _numeric(v)
            if fv is not None:
                measures[k] = fv

    meta = getattr(s, "meta", None) or {}
    for k, v in meta.items():
        if k == "params":
            continue
        fv = _numeric(v)
        if fv is not None:
            measures.setdefault(k, fv)  # runs win on collision

    raw_params = meta.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}

    return Candidate(
        ref_id=s.id,
        handle=handle,
        name=name[:70],
        measures=measures,
        converged=converged,
        params=params,
    )


def leaderboard(
    fr: FrontierResult, *, graduated: frozenset[int] | set[int] = frozenset()
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows + TOON schema for the **by-total** design leaderboard (§7.3).

    One row per candidate design: identity, the objective vector, its Pareto
    ``band`` (``frontier`` / ``dominated`` / ``awaiting``), and a graduation
    flag. Ordered frontier → dominated → awaiting, and within each band sorted
    by the primary objective (best first). Pure over a :class:`FrontierResult`
    so it is trivially testable; the handler renders it via ``toon.dump``. This
    is the striving's authoritative leaderboard — catpath's own ``compare`` view
    is a compute-side diagnostic over sibling pathways, not this.
    """
    obj_keys = [k for k, _ in fr.objectives]
    primary = fr.objectives[0] if fr.objectives else None

    def _sort_key(c: Candidate) -> float:
        if primary is None:
            return 0.0
        key, sense = primary
        v = c.measures.get(key)
        if v is None:
            return float("inf")  # unmeasured sinks to the bottom of its band
        return v if sense == "min" else -v

    def _rows(cands: list[Candidate], band: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for c in sorted(cands, key=_sort_key):
            row: dict[str, Any] = {"design": c.handle, "name": c.name, "band": band}
            for key in obj_keys:
                v = c.measures.get(key)
                row[key] = f"{v:g}" if isinstance(v, (int, float)) else "—"
            row["graduated"] = "★" if c.ref_id in graduated else ""
            out.append(row)
        return out

    rows = (
        _rows(fr.frontier, "frontier")
        + _rows(fr.dominated, "dominated")
        + _rows(fr.unevaluated, "awaiting")
    )
    schema = ["design", "name", *obj_keys, "band", "graduated"]
    return rows, schema


def quest_frontier(
    store: Store,
    quest_id: int,
    *,
    objectives: list[tuple[str, str]] | None = None,
) -> FrontierResult:
    """The Pareto frontier over the quest's candidate `structure` servers."""
    from precis.quest.gaps import _live_servers

    objs = objectives or _objectives_for(store, quest_id)
    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    candidates = [_candidate_from_structure(store, s) for s in structures]
    return pareto_split(candidates, objs)


__all__ = [
    "DEFAULT_OBJECTIVES",
    "Candidate",
    "FrontierResult",
    "leaderboard",
    "pareto_split",
    "quest_frontier",
]
