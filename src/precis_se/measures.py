"""se named measures + tolerance relations, and stack-up evaluation
(se-kind.md L2 "Tolerances as relations between named measures").

A measure is a named scalar on a block (``wheel.bore_d``), metres. A
tolerance is a **relation between two measures, never an absolute number
on one block**: the relation lives on its *target* measure —
``{"source": "hub.od_d", "offset": 2e-4, "tol": 5e-5}`` reads
"this = hub.od_d + 0.2 mm ± 0.05 mm". Strength is the pcb_measures triad
(hard gates realization, soft is an objective, gauge just reports) —
strength *consumers* arrive with realization; today it is carried and
rendered.

**Stack-up** (:func:`stackup`) is the pure L2→L4 evaluation the spec calls
genuinely new: follow each measure's relation chain to an anchor (a
measure with a declared ``value`` and no relation, or the chain's end),
summing offsets and accumulating tolerances **worst-case linear** (Σ|tol|
— the standard first method; RSS is a later refinement, named so it isn't
re-derived). A measure that declares its own ``value`` *and* derives one
through a relation is checked for agreement — disagreement beyond the
accumulated tolerance is a finding for DRC. Chains that dangle (source
measure doesn't exist) or cycle are *DRC's findings*
(:mod:`precis_se.drc`), reported here as unresolved results — write time
deliberately tolerates a dangling source (forward references within one
ops batch are normal workflow; the spec lists "unresolvable relations" as
a graph-DRC finding, i.e. a read-time report).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

_RELATION_KEYS = frozenset({"source", "offset", "tol"})


class MeasureError(ValueError):
    """A malformed measure payload (bad relation shape, bad numbers)."""


@dataclass
class MeasureSpec:
    """One named measure on a block. ``relation`` (validated through
    :func:`validate_relation`) ties it to a source measure; ``value`` is
    the independently declared number (metres), either may be absent
    (suggestive by contract)."""

    block: str
    name: str
    value: float | None = None
    relation: dict[str, Any] | None = None
    strength: str = "gauge"
    reason: str | None = None


def validate_relation(raw: dict[str, Any]) -> dict[str, Any]:
    """Vet a relation dict's *shape* — source syntax, finite offset,
    tol ≥ 0. Source **existence** is deliberately not checked here (module
    docstring: dangling is legal at write, DRC's finding at read)."""
    unknown = set(raw) - _RELATION_KEYS
    if unknown:
        raise MeasureError(
            f"unknown relation key(s): {', '.join(sorted(unknown))} — a "
            "relation is {'source': 'block.measure', 'offset': <m>, "
            "'tol': <m ≥ 0>}"
        )
    source = str(raw.get("source") or "").strip()
    blk, sep, msr = source.rpartition(".")
    if not sep or not blk.strip() or not msr.strip():
        raise MeasureError(
            f"relation 'source' must be 'block.measure', got {raw.get('source')!r}"
        )
    try:
        offset = float(raw.get("offset", 0.0))
    except (TypeError, ValueError) as exc:
        raise MeasureError(
            f"relation 'offset' must be a number (m), got {raw.get('offset')!r}"
        ) from exc
    try:
        tol = float(raw.get("tol", 0.0))
    except (TypeError, ValueError) as exc:
        raise MeasureError(
            f"relation 'tol' must be a number ≥ 0 (m), got {raw.get('tol')!r}"
        ) from exc
    if tol < 0.0:
        raise MeasureError(f"relation 'tol' must be ≥ 0 m, got {tol!r}")
    return {"source": source, "offset": offset, "tol": tol}


@dataclass
class StackupResult:
    """One measure's stack-up evaluation. ``derived``/``tol_accum`` are set
    when the relation chain resolved to an anchored value; ``problem``
    names why it didn't (dangling source, cycle, unanchored chain) or a
    declared-vs-derived disagreement — DRC turns problems into findings."""

    measure: str  # 'block.name'
    declared: float | None = None
    derived: float | None = None
    tol_accum: float = 0.0
    chain: list[str] = field(default_factory=list)  # source-ward, this first
    problem: str | None = None
    #: 'dangling' | 'cycle' | 'mismatch' | 'malformed' | None
    problem_kind: str | None = None


def _key(m: MeasureSpec) -> str:
    return f"{m.block}.{m.name}"


def stackup(measures: list[MeasureSpec]) -> list[StackupResult]:
    """Evaluate every measure that carries a relation (plus none of the
    anchor-only ones — a bare declared value has nothing to evaluate).
    Pure; no store access.

    Stored relations are **re-validated here** (write time gates new ones,
    but a hand-corrected jsonb row must surface as a ``malformed`` problem,
    never a crash — the malformed_joint posture). A measure whose own
    relation is malformed gets its own problem row; when it sits mid-chain
    for someone else it acts as an unvalued/valued *anchor* (the chain
    stops there — its brokenness is its own finding, not its dependents')."""
    by_key = {_key(m): m for m in measures}
    valid_rel: dict[str, dict[str, Any] | None] = {}
    malformed: dict[str, str] = {}
    for m in measures:
        k = _key(m)
        if m.relation is None:
            valid_rel[k] = None
            continue
        try:
            valid_rel[k] = validate_relation(m.relation)
        except MeasureError as exc:
            valid_rel[k] = None
            malformed[k] = str(exc)
    out: list[StackupResult] = []
    for m in measures:
        if m.relation is None:
            continue
        me = _key(m)
        if me in malformed:
            out.append(
                StackupResult(
                    measure=me,
                    declared=m.value,
                    chain=[me],
                    problem=f"stored relation does not fit the schema: "
                    f"{malformed[me]} — set_measure to repair it",
                    problem_kind="malformed",
                )
            )
            continue
        res = StackupResult(measure=me, declared=m.value, chain=[me])
        seen = {me}
        offset_sum = 0.0
        tol_sum = 0.0
        cur_key = me
        while True:
            rel = valid_rel[cur_key]
            assert rel is not None
            offset_sum += float(rel["offset"])
            tol_sum += abs(float(rel["tol"]))
            src_key = str(rel["source"])
            res.chain.append(src_key)
            src = by_key.get(src_key)
            if src is None:
                res.problem = (
                    f"relation source {src_key!r} does not exist — "
                    "unresolvable relation"
                )
                res.problem_kind = "dangling"
                break
            if src_key in seen:
                res.problem = f"relation cycle: {' → '.join(res.chain)}"
                res.problem_kind = "cycle"
                break
            seen.add(src_key)
            if valid_rel[src_key] is None:
                # anchor: no (usable) relation past here — chain ends,
                # valued or not (a malformed source's brokenness is its
                # own problem row, see docstring).
                if src.value is not None:
                    res.derived = src.value + offset_sum
                    res.tol_accum = tol_sum
                # an unvalued anchor is fine mid-design — nothing derived,
                # nothing to disagree with; filled-fraction honesty covers
                # the absence.
                break
            if src.value is not None:
                # a valued mid-chain measure anchors the shorter chain —
                # nearest declared value wins (its own relation is checked
                # by its own StackupResult).
                res.derived = src.value + offset_sum
                res.tol_accum = tol_sum
                break
            cur_key = src_key
        if (
            res.problem is None
            and res.derived is not None
            and res.declared is not None
            and abs(res.declared - res.derived) > res.tol_accum
        ):
            res.problem = (
                f"declared {res.declared:g} m disagrees with derived "
                f"{res.derived:g} m beyond the accumulated ±{res.tol_accum:g} m"
            )
            res.problem_kind = "mismatch"
        out.append(res)
    return out
