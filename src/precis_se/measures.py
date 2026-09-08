"""se named measures + tolerance relations, and stack-up evaluation
(se-kind.md L2 "Tolerances as relations between named measures", grown by
slice 4's design-freedom vocabulary).

A measure is a named scalar on a block (``wheel.bore_d``), in one of the
**closed unit registry**'s units (:data:`UNITS` — ``m`` default, ``count``,
``ratio``, ``deg``; views render per-unit). A tolerance is a **relation
between two measures, never an absolute number on one block**: the
relation lives on its *target* measure —
``{"source": "hub.od_d", "scale": 1.0, "offset": 2e-4, "tol": 5e-5}``
reads "this = scale·hub.od_d + 0.2 mm ± 0.05 mm". ``scale`` (default 1,
dimensionless) makes 1:10 tooth relations and half-diameter relations
honest; **relations require unit agreement** between their endpoints —
a mismatch is a stack-up problem, not a crash. Strength is the
pcb_measures triad (hard gates realization, soft is an objective, gauge
just reports).

Slice 4's freedom fields: an optional ``min_value``/``max_value``
**interval** is the declarative alternative to a point ``value`` ("bore
≥ 4 mm" declares a set; a forced point is overspecification) — both may
coexist when a point has been chosen inside a declared band. ``origin``
(``user | proposed``) records who owns the number: a propose job revises
its own freely and treats the user's as contract.

**Stack-up** (:func:`stackup`) follows each measure's relation chain to
an anchor (a measure with a declared ``value`` — or, new, a declared
band — and no usable relation past it), accumulating a running
multiplier for ``scale``, summing offsets and accumulating tolerances
**worst-case linear** (Σ|mult·tol| — RSS is a later refinement, named so
it isn't re-derived). An interval anchor derives a *band*
(``derived_min``/``derived_max``); declared-vs-derived agreement is
checked point-vs-point, point-vs-band and band-vs-band, always with the
accumulated tolerance as slack. Chains that dangle or cycle are *DRC's
findings* (:mod:`precis_se.drc`), reported here as unresolved results —
write time deliberately tolerates a dangling source (forward references
within one ops batch are normal workflow).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

_RELATION_KEYS = frozenset({"source", "offset", "tol", "scale"})

#: The closed unit registry (se-kind.md slice 4): metres, dimensionless
#: counts (gear teeth), dimensionless ratios, degrees. Relations require
#: unit agreement; ``scale`` is dimensionless so it never converts.
UNITS = ("m", "count", "ratio", "deg")

#: Who owns a number (se-kind.md slice 4, the RFdiffusion
#: fixed-motif/free-scaffold split): ``user`` is contract, ``proposed``
#: is a propose job's own revisable choice.
ORIGINS = ("user", "proposed")


class MeasureError(ValueError):
    """A malformed measure payload (bad relation shape, bad numbers)."""


@dataclass
class MeasureSpec:
    """One named measure on a block. ``relation`` (validated through
    :func:`validate_relation`) ties it to a source measure; ``value`` is
    the independently declared number, ``min_value``/``max_value`` the
    declared acceptable band — any may be absent (suggestive by
    contract)."""

    block: str
    name: str
    value: float | None = None
    relation: dict[str, Any] | None = None
    strength: str = "gauge"
    reason: str | None = None
    min_value: float | None = None
    max_value: float | None = None
    origin: str = "user"
    unit: str = "m"


def validate_relation(raw: dict[str, Any]) -> dict[str, Any]:
    """Vet a relation dict's *shape* — source syntax, finite scale/offset,
    tol ≥ 0. Source **existence** (and unit agreement, which needs the
    source row) is deliberately not checked here (module docstring:
    dangling is legal at write, DRC's finding at read)."""
    unknown = set(raw) - _RELATION_KEYS
    if unknown:
        raise MeasureError(
            f"unknown relation key(s): {', '.join(sorted(unknown))} — a "
            "relation is {'source': 'block.measure', 'scale': <×, default "
            "1>, 'offset': <unit>, 'tol': <unit ≥ 0>}"
        )
    source_raw = raw.get("source")
    if not isinstance(source_raw, str):
        # str() would happily stringify a list/dict into a dot-containing
        # name that passes the shape check and lands as an unresolvable
        # relation (gameplay finding, 2026-09-04) — reject loudly instead.
        raise MeasureError(
            f"relation 'source' must be a single 'block.measure' string, "
            f"got {source_raw!r} — a relation has exactly one source; a "
            "sum of two measures (center distance = r1 + r2) is not yet "
            "expressible: give this measure a declared value and relate "
            "it to ONE source, folding the other term into 'offset'"
        )
    source = source_raw.strip()
    blk, sep, msr = source.rpartition(".")
    if not sep or not blk.strip() or not msr.strip():
        raise MeasureError(
            f"relation 'source' must be 'block.measure', got {raw.get('source')!r}"
        )
    try:
        scale = float(raw.get("scale", 1.0))
    except (TypeError, ValueError) as exc:
        raise MeasureError(
            f"relation 'scale' must be a number (dimensionless factor, "
            f"default 1), got {raw.get('scale')!r}"
        ) from exc
    if not math.isfinite(scale) or scale == 0.0:
        raise MeasureError(
            f"relation 'scale' must be finite and non-zero, got {scale!r} — "
            "a zero scale severs the relation (the source stops mattering); "
            "drop the relation and declare a value instead"
        )
    try:
        offset = float(raw.get("offset", 0.0))
    except (TypeError, ValueError) as exc:
        raise MeasureError(
            f"relation 'offset' must be a number (target measure's unit), "
            f"got {raw.get('offset')!r}"
        ) from exc
    try:
        tol = float(raw.get("tol", 0.0))
    except (TypeError, ValueError) as exc:
        raise MeasureError(
            f"relation 'tol' must be a number ≥ 0 (target measure's unit), "
            f"got {raw.get('tol')!r}"
        ) from exc
    if tol < 0.0:
        raise MeasureError(f"relation 'tol' must be ≥ 0, got {tol!r}")
    out = {"source": source, "offset": offset, "tol": tol}
    # keep stored rows minimal: the default scale is not persisted, so
    # pre-slice-4 rows and scale-less writes stay byte-identical.
    if scale != 1.0:
        out["scale"] = scale
    return out


@dataclass
class StackupResult:
    """One measure's stack-up evaluation. ``derived``/``tol_accum`` are set
    when the relation chain resolved to a point-valued anchor;
    ``derived_min``/``derived_max`` when it resolved to an interval
    anchor's band. ``problem`` names why nothing derived (dangling source,
    cycle, unit mismatch, malformed row) or a declared-vs-derived
    disagreement — DRC turns problems into findings."""

    measure: str  # 'block.name'
    declared: float | None = None
    derived: float | None = None
    derived_min: float | None = None
    derived_max: float | None = None
    tol_accum: float = 0.0
    unit: str = "m"
    chain: list[str] = field(default_factory=list)  # source-ward, this first
    problem: str | None = None
    #: 'dangling' | 'cycle' | 'mismatch' | 'malformed' | 'unit_mismatch' | None
    problem_kind: str | None = None


def _key(m: MeasureSpec) -> str:
    return f"{m.block}.{m.name}"


def declared_band(m: MeasureSpec) -> tuple[float, float] | None:
    """The measure's declared band when either bound exists — an open end
    is ±inf (a one-sided "bore ≥ 4 mm" is legal and useful)."""
    if m.min_value is None and m.max_value is None:
        return None
    lo = m.min_value if m.min_value is not None else -math.inf
    hi = m.max_value if m.max_value is not None else math.inf
    return (lo, hi)


def stackup(measures: list[MeasureSpec]) -> list[StackupResult]:
    """Evaluate every measure that carries a relation. A relation-less
    measure gets a row only when its declared ``value`` violates its own
    declared band — write time gates that contradiction for new ops, but
    a hand-edited stored row must still surface at read time (reviewer
    finding: a bare interval measure is slice 4's primary shape, and
    without this it read as clean in both ``view='measures'`` and DRC).
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
            # nothing to derive — but the declared point must still agree
            # with the measure's own declared band (docstring). Clean
            # anchor-only measures produce no row, as before.
            res = StackupResult(
                measure=_key(m), declared=m.value, unit=m.unit, chain=[_key(m)]
            )
            res.problem, res.problem_kind = _agreement_problem(m, res)
            if res.problem is not None:
                out.append(res)
            continue
        me = _key(m)
        if me in malformed:
            out.append(
                StackupResult(
                    measure=me,
                    declared=m.value,
                    unit=m.unit,
                    chain=[me],
                    problem=f"stored relation does not fit the schema: "
                    f"{malformed[me]} — set_measure to repair it",
                    problem_kind="malformed",
                )
            )
            continue
        res = StackupResult(measure=me, declared=m.value, unit=m.unit, chain=[me])
        seen = {me}
        mult = 1.0
        offset_sum = 0.0
        tol_sum = 0.0
        cur = m
        cur_key = me
        while True:
            rel = valid_rel[cur_key]
            assert rel is not None
            offset_sum += mult * float(rel["offset"])
            tol_sum += abs(mult) * abs(float(rel["tol"]))
            mult *= float(rel.get("scale", 1.0))
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
            if src.unit != cur.unit:
                # scale is dimensionless, so a relation never converts —
                # its endpoints must agree (module docstring).
                res.problem = (
                    f"unit mismatch: {cur_key} is in {cur.unit!r} but its "
                    f"relation source {src_key} is in {src.unit!r} — "
                    "relations require unit agreement (scale is a "
                    "dimensionless factor, not a converter)"
                )
                res.problem_kind = "unit_mismatch"
                break
            if src_key in seen:
                res.problem = f"relation cycle: {' → '.join(res.chain)}"
                res.problem_kind = "cycle"
                break
            seen.add(src_key)
            src_band = declared_band(src)
            if src.value is not None:
                # a valued measure anchors the chain wherever it sits —
                # nearest declared value wins (its own relation, if any,
                # is checked by its own StackupResult).
                res.derived = mult * src.value + offset_sum
                res.tol_accum = tol_sum
                break
            if valid_rel[src_key] is None:
                # chain's end without a point value: an interval anchor
                # derives a band; a bare unvalued anchor derives nothing
                # (fine mid-design — filled-fraction honesty covers it).
                if src_band is not None:
                    lo = mult * src_band[0] + offset_sum
                    hi = mult * src_band[1] + offset_sum
                    res.derived_min, res.derived_max = min(lo, hi), max(lo, hi)
                    res.tol_accum = tol_sum
                break
            cur = src
            cur_key = src_key
        if res.problem is None:
            res.problem, res.problem_kind = _agreement_problem(m, res)
        out.append(res)
    return out


def _agreement_problem(
    m: MeasureSpec, res: StackupResult
) -> tuple[str | None, str | None]:
    """Declared-vs-derived agreement, with ``tol_accum`` as slack: checks
    the declared point against the derived point or band, the declared
    band against the derived point or band, and the declared point
    against the measure's own band (a hand-edited row can violate what
    write time gates)."""
    tol = res.tol_accum
    band = declared_band(m)
    u = m.unit
    if band is not None and m.value is not None and not (band[0] <= m.value <= band[1]):
        return (
            f"declared {m.value:g} {u} lies outside this measure's own "
            f"declared band [{band[0]:g}, {band[1]:g}] {u}",
            "mismatch",
        )
    if res.derived is not None:
        if m.value is not None and abs(m.value - res.derived) > tol:
            return (
                f"declared {m.value:g} {u} disagrees with derived "
                f"{res.derived:g} {u} beyond the accumulated ±{tol:g} {u}",
                "mismatch",
            )
        if (
            band is not None
            and m.value is None
            and not (band[0] - tol <= res.derived <= band[1] + tol)
        ):
            return (
                f"derived {res.derived:g} {u} falls outside the declared "
                f"band [{band[0]:g}, {band[1]:g}] {u} beyond the "
                f"accumulated ±{tol:g} {u}",
                "mismatch",
            )
    if res.derived_min is not None and res.derived_max is not None:
        if m.value is not None and not (
            res.derived_min - tol <= m.value <= res.derived_max + tol
        ):
            return (
                f"declared {m.value:g} {u} falls outside the derived band "
                f"[{res.derived_min:g}, {res.derived_max:g}] {u} beyond "
                f"the accumulated ±{tol:g} {u}",
                "mismatch",
            )
        if (
            band is not None
            and m.value is None
            and (res.derived_max + tol < band[0] or res.derived_min - tol > band[1])
        ):
            return (
                f"derived band [{res.derived_min:g}, {res.derived_max:g}] "
                f"{u} is disjoint from the declared band "
                f"[{band[0]:g}, {band[1]:g}] {u} beyond the accumulated "
                f"±{tol:g} {u}",
                "mismatch",
            )
    return (None, None)
