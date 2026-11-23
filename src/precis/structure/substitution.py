"""Per-axis substitution query — slice 1 of gr695283 (the materials-substitution db).

No query surface asked "near on one axis, far on another" (similar size,
different magnetism) over crystals. This module is the first testable slice
of the fix: the per-axis similarity query surface plus the
prototype/occupancy split, exactly the three-axis proposal Reto accepted
(td696679 comment 4): a learned structure embedding, volume/atom as a unit
scalar, and magnetic ordering + ordering temperature as
categorical + condition. Per that proposal there are **no new tables** until
gr695047 (structure-kind-demotion) lands, so the query runs over in-memory
:class:`Crystal` records; a store seam comes later, not a second import
path.

The design input (Reto 2026-11-16, td696679 comment 4) this implements:

- The space is a **set of named axes**; a query picks a subset and
  constrains each (near / far / range / equal / at condition X), runs kNN
  only over the embedded axes in the subset, applies the rest as filters,
  and reports a distance per axis.
- **Each axis declares a type and the type fixes its distance function**:
  categorical (a closed ``vocab`` plus a small distance table), ordinal
  (rank difference), scalar with unit and scale (scaled difference, log
  for temperatures and moduli), condition-dependent (evaluated at a
  stated condition — declared, not built; the magnetic axis's ``at`` op
  is this slice's one wired case), tensor (invariants first — declared,
  not built), and learned embedding (cosine or L2).
- **Yes/no phase properties store the ordered state plus the transition
  temperature**; "ordered at 300 K" is derived at query time as
  ``T_order > 300`` (:func:`ordering_at`), never stored per temperature.
- **Value origin is cross-cutting** (me693656): numeric values use the
  point/interval/err/origin/tier vocabulary of ``measures`` (:class:`Value`,
  me694355). Ordering labels and named vectors carry optional origins
  from the same vocabulary.

The 2026-11-19 ruling lands here as :func:`swap_pair_key`: a substitution
estimate is a *measure* on a swap-pair subject ref (origin
``ml_prediction``/``computation`` with uncertainty, append-only) — ranked
substitution lists are queries over this surface, not stored rows. The key
is canonical so a future measures writer mints one subject per undecorated
pair, whichever crystal the query started from.

Unit enclave (package docstring): distances are dimensionless per-axis
numbers in declared axis units (see each :class:`Axis`); native values keep
their unit on the :class:`Value`.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput, Unsupported

#: Value origins, mirroring the ``measures.origin`` CHECK — estimated rows
#: ride as ``ml_prediction``/``computation``, never a new origin.
ORIGINS = frozenset({"experiment", "computation", "ml_prediction", "literature"})
#: Value tiers, mirroring the ``measures.tier`` CHECK.
TIERS = frozenset({"measured", "computed", "derived", "asserted"})

#: Ground-state magnetic orderings (td696679 comment 4). A-type, C-type and
#: G-type AFM are separate values; ferro sits close to ferri.
ORDERINGS = (
    "dia",
    "para",
    "ferro",
    "ferri",
    "afm_a",
    "afm_c",
    "afm_g",
    "spin_glass",
    "helimagnet",
)

#: Small distance table for :func:`ordering_distance`; every unlisted
#: unequal pair costs 1.0, an equal pair 0.0. Ferro is close to ferri, the
#: AFM stack to each other and to helimagnetism, dia/para half-close as the
#: non-ordered pair, and any ordered state far from dia.
ORDERING_DISTANCE: Mapping[frozenset[str], float] = {
    frozenset(("dia", "para")): 0.5,
    frozenset(("ferro", "ferri")): 0.25,
    frozenset(("afm_a", "afm_c")): 0.25,
    frozenset(("afm_a", "afm_g")): 0.25,
    frozenset(("afm_c", "afm_g")): 0.25,
    frozenset(("afm_a", "helimagnet")): 0.5,
    frozenset(("afm_c", "helimagnet")): 0.5,
    frozenset(("afm_g", "helimagnet")): 0.5,
    frozenset(("ferro", "helimagnet")): 0.5,
    frozenset(("ferri", "helimagnet")): 0.5,
    frozenset(("spin_glass", "helimagnet")): 0.25,
    frozenset(("ferro", "spin_glass")): 0.5,
    frozenset(("ferri", "spin_glass")): 0.5,
}


def ordering_distance(a: str, b: str) -> float:
    """Categorical distance between two ground-state orderings.

    An ordering outside :data:`ORDERINGS` is a :class:`BadInput` — the
    vocabulary is closed so the table cannot silently drift.
    """
    for ordering in (a, b):
        if ordering not in ORDERINGS:
            raise BadInput(f"unknown magnetic ordering: {ordering!r}")
    if a == b:
        return 0.0
    return ORDERING_DISTANCE.get(frozenset((a, b)), 1.0)


def ordering_at(ordering: str, order_temperature: Value | None, t_k: float) -> str:
    """Ground-state ordering evaluated at temperature ``t_k`` (kelvin).

    Below the transition temperature the ordered state holds, at or above
    it the material is paramagnetic. Dia and para have no transition and
    hold at every temperature. An ordered state without a transition
    temperature is a :class:`BadInput` — the yes/no property must store the
    transition point, never a pre-computed "ordered at 300 K" flag
    (td696679 comment 4): "ordered at 300 K" is
    ``ordering_at(...) not in ("dia", "para")``, i.e. ``T_order > 300`` at
    query time. A non-finite temperature on either side of the comparison
    is :class:`BadInput` too — ``t_k < NaN`` is False, so an unchecked NaN
    transition would silently classify an ordered material as para
    (review td778113).
    """
    if ordering not in ORDERINGS:
        raise BadInput(f"unknown magnetic ordering: {ordering!r}")
    if not math.isfinite(t_k):
        raise BadInput(f"non-finite query temperature: {t_k!r}")
    if ordering in ("dia", "para"):
        return ordering
    if order_temperature is None or order_temperature.num is None:
        raise BadInput(f"ordering {ordering!r} carries no transition temperature")
    _check_unit("transition temperature", order_temperature, "K")
    if not math.isfinite(order_temperature.num):
        raise BadInput(f"ordering {ordering!r}: non-finite transition temperature")
    return ordering if t_k < order_temperature.num else "para"


def _check_unit(what: str, value: Any, unit: str) -> None:
    """A :class:`Value` in another unit is refused, never silently compared."""
    if isinstance(value, Value) and unit and value.unit != unit:
        raise Unsupported(f"{what}: unit {value.unit!r} where {unit!r} is declared")


@dataclass(frozen=True)
class Value:
    """One physical quantity in the ``measures`` value shape.

    Mirrors the ``measures`` row vocabulary (me693370 / me694355) so this
    slice mints no second value shape: a point or interval with
    uncertainty, unit, origin and tier. ``conditions`` holds the stated
    condition the value was taken at (e.g. ``{"T_K": 300}``); the magnetic
    transition temperature is the one condition this slice models
    explicitly (:func:`ordering_at`).
    """

    num: float | None = None
    low: float | None = None
    high: float | None = None
    err: float | None = None
    unit: str = ""
    origin: str = "literature"
    tier: str = "asserted"
    conditions: Mapping[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.origin not in ORIGINS:
            raise BadInput(f"unknown value origin: {self.origin!r}")
        if self.tier not in TIERS:
            raise BadInput(f"unknown value tier: {self.tier!r}")


@dataclass(frozen=True)
class Axis:
    """A named similarity axis; ``kind`` fixes the distance function.

    - ``categorical``: distance from ``table`` (0.0 equal, 1.0 unlisted
      unequal); a declared ``vocab`` is closed — an out-of-vocabulary
      value is :class:`BadInput`, in the data and in ``equal``/``at``
      operands alike.
    - ``ordinal``: rank difference over ``order``, normalised to [0, 1].
    - ``scalar``: ``|a − b| / span[1]`` (linear) or
      ``|ln a − ln b| / ln(span[1] / span[0])`` (log — temperatures and
      moduli). ``span`` is the declared typical range in native units.
    - ``condition``: declared by the design (evaluated at a stated
      condition, or the transition points compared); not built in this
      slice (:class:`Unsupported`). The ``at`` op on
      ``magnetic_ordering`` — :func:`ordering_at` over the stored
      transition temperature — is the one wired case.
    - ``tensor``: declared by the design, invariants first; not built in
      this slice (:class:`Unsupported`).
    - ``embedding``: ``1 − cos`` (cosine) or L2 over the named space's
      vector; these are the axes kNN runs over.
    - ``substitution``: the prototype/occupancy axis — the fraction of
      site roles with a different element between two decorations of one
      prototype (:func:`substitution_sites`).
    """

    name: str
    kind: str
    attr: str = ""
    unit: str = ""
    scale: str = "linear"
    span: tuple[float, float] = (0.0, 1.0)
    order: tuple[str, ...] = ()
    metric: str = "cosine"
    space: str = ""
    table: Mapping[frozenset[str], float] = field(default_factory=dict)
    vocab: tuple[str, ...] = ()


#: The slice-1 axis registry (td696679 comment 4): three axes exercising
#: three axis types — a learned structure embedding, a unit scalar, and the
#: magnetic axis as categorical + condition.
AXES: Mapping[str, Axis] = {
    "structure_embedding": Axis(
        name="structure_embedding",
        kind="embedding",
        attr="embedding",
        space="structure_graph",
        metric="cosine",
    ),
    "volume_per_atom": Axis(
        name="volume_per_atom",
        kind="scalar",
        attr="volume_per_atom",
        unit="angstrom3_per_atom",
        scale="linear",
        span=(0.0, 20.0),
    ),
    "magnetic_ordering": Axis(
        name="magnetic_ordering",
        kind="categorical",
        attr="ordering",
        table=ORDERING_DISTANCE,
        vocab=ORDERINGS,
    ),
    "ordering_temperature": Axis(
        name="ordering_temperature",
        kind="scalar",
        attr="order_temperature",
        unit="K",
        scale="log",
        span=(1.0, 2000.0),
    ),
    "decoration": Axis(
        name="decoration",
        kind="substitution",
        attr="decoration",
    ),
}


@dataclass(frozen=True)
class Crystal:
    """One decorated crystal: a prototype (topology) plus an occupancy.

    ``decoration`` is ``((role, element, count), ...)`` sorted by role —
    the site occupancy; stripping elements leaves the prototype
    (:func:`prototype_key`). ``embedding`` holds one vector per named
    similarity space (the design's "one embedding row per (entity, named
    similarity space)", in memory until gr695047 lands a store).
    """

    name: str
    family: str
    decoration: tuple[tuple[str, str, int], ...]
    prototype: str = ""
    volume_per_atom: Value | None = None
    ordering: str = "dia"
    order_temperature: Value | None = None
    embedding: Mapping[str, tuple[float, ...]] = field(default_factory=dict)
    ordering_origin: str | None = None
    embedding_origins: Mapping[str, str] = field(default_factory=dict)


def prototype_key(crystal: Crystal) -> tuple[str, tuple[tuple[str, int], ...]]:
    """Topology identity: label + recorded site-role multiplicities, elements off.

    An empty decoration is a :class:`BadInput` — a prototype with no site
    roles is malformed data, and accepting it would divide by zero in
    :func:`substitution_sites` (the scan in :func:`query` catches candidate
    :class:`BadInput` as "unusable value" and filters, so a malformed
    candidate never aborts the scan; a malformed *target* must be rejected
    up front via :func:`_target_term`).
    """
    if not crystal.decoration:
        raise BadInput(f"{crystal.name}: empty decoration (no site roles)")
    roles = Counter(role for role, _element, _count in crystal.decoration)
    if len(roles) != len(crystal.decoration):
        raise BadInput(f"{crystal.name}: duplicate site role in decoration")
    for role, _element, count in crystal.decoration:
        if count < 1:
            raise BadInput(f"{crystal.name}: site role {role!r} has count {count}")
    return (
        crystal.prototype or crystal.family,
        tuple(sorted((role, count) for role, _element, count in crystal.decoration)),
    )


def substitution_sites(a: Crystal, b: Crystal) -> float | None:
    """Per-site substitution distance: the fraction of site roles whose
    element differs, between two decorations of one prototype.

    ``None`` when the prototypes differ — a substitution across prototypes
    is not defined (the prototype/occupancy split, gr695283 body).
    :class:`BadInput` when either record's decoration is malformed
    (empty, duplicate role, non-positive count — :func:`prototype_key`).
    """
    if prototype_key(a) != prototype_key(b):
        return None
    elements_b = {role: element for role, element, _count in b.decoration}
    differing = sum(
        1 for role, element, _count in a.decoration if elements_b[role] != element
    )
    return differing / len(a.decoration)


def swap_pair_key(a: Crystal, b: Crystal) -> str:
    """Canonical subject key for a future substitution_estimate measures row.

    The 2026-11-19 ruling: a substitution estimate is a measure on a
    swap-pair subject ref. The pair is order-independent (sorted) and named
    by prototype plus both decorations, so whichever crystal a query
    started from, the pair mints one subject.
    """
    key = prototype_key(a)
    if prototype_key(b) != key:
        raise BadInput(f"different prototypes: {a.name}, {b.name}")
    label, counts = key
    shape = ",".join(f"{role}{count}" for role, count in counts)
    decorations = sorted(_decoration_label(c) for c in (a, b))
    return f"swap:{label}[{shape}]:{'->'.join(decorations)}"


def _decoration_label(crystal: Crystal) -> str:
    return ";".join(
        f"{role}={element}" for role, element, _count in sorted(crystal.decoration)
    )


def _closed_domain(axis: Axis) -> tuple[str, ...]:
    """The axis's declared finite value set, ``()`` when open."""
    if axis.kind == "categorical":
        return axis.vocab
    if axis.kind == "ordinal":
        return axis.order
    return ()


def axis_distance(axis: Axis, a: Any, b: Any) -> float:
    """The axis-typed distance between two native values."""
    if axis.kind == "categorical":
        for value in (a, b):
            if axis.vocab and value not in axis.vocab:
                raise BadInput(
                    f"value outside categorical axis {axis.name!r}: {value!r}"
                )
        if a == b:
            return 0.0
        return axis.table.get(frozenset((a, b)), 1.0)
    if axis.kind == "ordinal":
        if a not in axis.order or b not in axis.order:
            raise BadInput(f"value outside ordinal axis {axis.name!r}: {a!r}, {b!r}")
        return abs(axis.order.index(a) - axis.order.index(b)) / max(
            1, len(axis.order) - 1
        )
    if axis.kind == "scalar":
        return _scalar_distance(axis, _num(axis, a), _num(axis, b))
    if axis.kind == "embedding":
        return _embedding_distance(axis, a, b)
    raise Unsupported(f"axis kind {axis.kind!r} has no distance in this slice")


def _num(axis: Axis, value: Any) -> float:
    _check_unit(f"axis {axis.name!r}", value, axis.unit)
    num = value.num if isinstance(value, Value) else value
    if num is None:
        raise BadInput(f"axis {axis.name!r}: value carries no number")
    result = float(num)
    if not math.isfinite(result):
        raise BadInput(f"axis {axis.name!r}: non-finite value")
    return result


def _scalar_distance(axis: Axis, a: float, b: float) -> float:
    if axis.scale == "linear":
        width = axis.span[1]
        if width <= 0:
            raise BadInput(f"axis {axis.name!r}: non-positive linear span")
        return abs(a - b) / width
    if axis.scale == "log":
        lo, hi = axis.span
        if lo <= 0 or hi <= lo:
            raise BadInput(f"axis {axis.name!r}: bad log span")
        if a <= 0 or b <= 0:
            raise BadInput(f"axis {axis.name!r}: log scale over non-positive value")
        return abs(math.log(a) - math.log(b)) / math.log(hi / lo)
    raise BadInput(f"axis {axis.name!r}: unknown scale {axis.scale!r}")


def _embedding_distance(axis: Axis, a: Sequence[float], b: Sequence[float]) -> float:
    if len(a) != len(b):
        raise BadInput(f"axis {axis.name!r}: embedding width mismatch")
    if not all(map(math.isfinite, a)) or not all(map(math.isfinite, b)):
        # A NaN/inf component poisons the dot product into a NaN distance —
        # as a rank term it sorts arbitrarily and can hold a top-k slot
        # (review td778113).
        raise BadInput(f"axis {axis.name!r}: non-finite embedding component")
    if axis.metric == "cosine":
        dot = math.fsum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(math.fsum(x * x for x in a))
        norm_b = math.sqrt(math.fsum(y * y for y in b))
        if norm_a == 0.0 or norm_b == 0.0:
            raise BadInput(f"axis {axis.name!r}: zero embedding vector")
        return 1.0 - dot / (norm_a * norm_b)
    if axis.metric == "l2":
        return math.sqrt(math.fsum((x - y) ** 2 for x, y in zip(a, b)))
    raise BadInput(f"axis {axis.name!r}: unknown metric {axis.metric!r}")


@dataclass(frozen=True)
class Constraint:
    """One per-axis constraint. ``op`` is ``near`` (axis distance ≤
    ``max_d``), ``far`` (≥ ``min_d``), ``range`` (native value in
    ``[lo, hi]``), ``equal`` (native value == ``value``; on the
    substitution axis: same prototype, any decoration), or ``at``
    (condition evaluation at ``at_k`` kelvin, then compared to ``value``
    — wired for ``magnetic_ordering`` only in this slice). ``near`` on an
    embedded axis may omit ``max_d``: it then
    only ranks (pure kNN), filtering nothing. Candidates missing a
    constrained value — or carrying a malformed one, e.g. a non-finite
    component or temperature — are filtered out, not errors; the same
    malformation on the target is :class:`BadInput` before the scan."""

    axis: str
    op: str
    value: Any = None
    max_d: float | None = None
    min_d: float | None = None
    lo: float | None = None
    hi: float | None = None
    at_k: float | None = None


@dataclass(frozen=True)
class Match:
    """One query result: the candidate plus a distance per constrained
    axis (dimensionless axis units), and the kNN ``score`` — the mean
    distance over the embedded near-axes the query ranked on, ``None``
    when the query constrained no embedded near-axis."""

    crystal: Crystal
    axis_distances: Mapping[str, float]
    score: float | None


def query(
    crystals: Collection[Crystal],
    *,
    target: Crystal,
    constraints: Sequence[Constraint],
    k: int | None = None,
    origins: Collection[str] | None = None,
) -> list[Match]:
    """Per-axis substitution query: "near on one axis, far on another".

    Picks the constrained axis subset, filters every candidate (not the
    target) by the non-embedded constraints and by ``origins`` when given
    (the cross-cutting origin axis, me693656), ranks by kNN over the
    embedded near-axes only, and reports a distance per axis. ``k`` caps
    the ranked list and must be non-negative. Without embedded near-axes
    the input order is kept. With an origin filter, every constrained
    candidate property must have an accepted origin, including both
    inputs to magnetic phase evaluation when a transition is used.
    Decoration compares site identities and is exempt.
    """
    if k is not None and k < 0:
        raise BadInput("k must be non-negative")
    if origins is not None:
        unknown = set(origins) - ORIGINS
        if unknown:
            raise BadInput(f"unknown value origins: {sorted(unknown)}")
    if len({c.axis for c in constraints}) != len(constraints):
        raise BadInput("one constraint per axis; the query picks an axis subset")
    axes = {c.axis: _axis(c.axis) for c in constraints}
    for constraint in constraints:
        _validate_constraint(axes[constraint.axis], constraint)
    # Touch the target once so a misconfigured target raises before the
    # candidate loop, not halfway through it.
    for constraint in constraints:
        _target_term(axes[constraint.axis], constraint, target)

    ranked = any(
        axes[c.axis].kind == "embedding" and c.op == "near" for c in constraints
    )

    matches: list[tuple[Match, float]] = []
    for candidate in crystals:
        if candidate == target:
            continue
        distances: dict[str, float] = {}
        rank_terms: list[float] = []
        keep = True
        for constraint in constraints:
            axis = axes[constraint.axis]
            if origins is not None and not _origin_ok(
                axis, constraint, candidate, origins
            ):
                keep = False
                break
            try:
                distance = _constraint_distance(axis, constraint, target, candidate)
            except BadInput:
                keep = False  # candidate lacks a usable value for this axis
                break
            if distance is None:
                keep = False
                break
            distances[constraint.axis] = distance
            if axis.kind == "embedding" and constraint.op == "near":
                rank_terms.append(distance)
        if keep:
            score = math.fsum(rank_terms) / len(rank_terms) if rank_terms else None
            matches.append(
                (
                    Match(candidate, distances, score),
                    score if score is not None else 0.0,
                )
            )

    if ranked:
        matches.sort(key=lambda item: item[1])
    if k is not None:
        matches = matches[:k]
    return [match for match, _score in matches]


def _axis(name: str) -> Axis:
    try:
        return AXES[name]
    except KeyError:
        raise BadInput(f"unknown axis: {name!r}") from None


def _validate_constraint(axis: Axis, constraint: Constraint) -> None:
    scalar = axis.kind == "scalar"
    ops = {
        "near": constraint.max_d is not None or axis.kind == "embedding",
        "far": constraint.min_d is not None,
        "range": scalar and constraint.lo is not None and constraint.hi is not None,
        "equal": (constraint.value is not None and axis.kind != "embedding")
        or axis.kind == "substitution",
        "at": constraint.at_k is not None and constraint.value is not None,
    }
    if constraint.op not in ops:
        raise BadInput(f"constraint {axis.name}: unknown op {constraint.op!r}")
    if constraint.op == "range" and not scalar:
        raise BadInput(f"constraint {axis.name}/range: needs a scalar axis")
    if constraint.op == "equal" and axis.kind == "embedding":
        raise BadInput(f"constraint {axis.name}/equal: not defined on embeddings")
    if not ops[constraint.op]:
        raise BadInput(f"constraint {axis.name}/{constraint.op}: missing operand")
    if constraint.op == "at" and axis.name != "magnetic_ordering":
        raise Unsupported(f"condition evaluation for axis {axis.name!r}")
    for operand in (
        constraint.max_d,
        constraint.min_d,
        constraint.lo,
        constraint.hi,
        constraint.at_k,
    ):
        if operand is not None and not math.isfinite(operand):
            # A NaN bound compares False everywhere — it would silently
            # filter every candidate instead of failing the query.
            raise BadInput(
                f"constraint {axis.name}/{constraint.op}: non-finite operand"
            )
    domain = _closed_domain(axis)
    if constraint.op in ("equal", "at") and domain and constraint.value not in domain:
        raise BadInput(
            f"constraint {axis.name}/{constraint.op}: "
            f"{constraint.value!r} outside the axis's closed vocabulary"
        )
    if constraint.op == "equal" and axis.kind == "substitution":
        return  # "same prototype" carries no value operand


def _axis_value(axis: Axis, crystal: Crystal) -> Any:
    if axis.kind == "embedding":
        vector = crystal.embedding.get(axis.space)
        if vector is None:
            raise BadInput(f"{crystal.name}: no embedding for space {axis.space!r}")
        return vector
    if axis.kind == "substitution":
        return crystal.decoration
    value = getattr(crystal, axis.attr)
    if value is None:
        raise BadInput(f"{crystal.name}: no value for axis {axis.name!r}")
    return value


def _origin_ok(
    axis: Axis, constraint: Constraint, crystal: Crystal, origins: Collection[str]
) -> bool:
    if axis.kind == "substitution":
        return True
    if axis.kind == "embedding":
        return crystal.embedding_origins.get(axis.space) in origins
    if axis.name == "magnetic_ordering":
        if crystal.ordering_origin not in origins:
            return False
        if constraint.op != "at" or crystal.ordering in ("dia", "para"):
            return True
        value = crystal.order_temperature
    else:
        value = getattr(crystal, axis.attr, None)
    return isinstance(value, Value) and value.origin in origins


def _target_term(axis: Axis, constraint: Constraint, target: Crystal) -> Any:
    """Validate the target side of a constraint (raises on bad data)."""
    if axis.kind == "substitution":
        # Same-prototype matching runs substitution_sites per candidate;
        # validate the target's decoration here so malformed target data
        # raises before the scan, not as a candidate-loop filter.
        prototype_key(target)
        return None
    if constraint.op == "at":
        at_k = constraint.at_k
        assert at_k is not None  # validated in _validate_constraint
        ordering_at(target.ordering, target.order_temperature, at_k)
        return None
    value = _axis_value(axis, target)
    axis_distance(axis, value, value)  # self-distance runs unit/domain checks
    return value


def _constraint_distance(
    axis: Axis, constraint: Constraint, target: Crystal, candidate: Crystal
) -> float | None:
    """Distance (or ``None`` = filtered out) for one constraint."""
    if axis.kind in ("tensor", "condition"):
        raise Unsupported(f"{axis.kind} axis {axis.name!r} is not built in this slice")
    if axis.kind == "substitution":
        distance = substitution_sites(target, candidate)
        if distance is None:
            return None  # different prototypes are filtered, not distant
        if constraint.op == "near":
            max_d = constraint.max_d
            assert max_d is not None  # validated in _validate_constraint
            return distance if distance <= max_d else None
        if constraint.op == "far":
            min_d = constraint.min_d
            assert min_d is not None  # validated in _validate_constraint
            return distance if distance >= min_d else None
        if constraint.op == "equal":  # same prototype, any decoration
            return distance
        raise BadInput(f"op {constraint.op!r} unsupported on the substitution axis")

    if constraint.op == "at":
        at_k = constraint.at_k
        assert at_k is not None  # validated in _validate_constraint
        target_value = ordering_at(target.ordering, target.order_temperature, at_k)
        candidate_value = ordering_at(
            candidate.ordering, candidate.order_temperature, at_k
        )
        if candidate_value != constraint.value:
            return None
        return ordering_distance(target_value, candidate_value)

    target_value = _axis_value(axis, target)
    candidate_value = _axis_value(axis, candidate)
    distance = axis_distance(axis, target_value, candidate_value)

    if constraint.op == "near":
        return (
            distance
            if constraint.max_d is None or distance <= constraint.max_d
            else None
        )
    if constraint.op == "far":
        min_d = constraint.min_d
        assert min_d is not None  # validated in _validate_constraint
        return distance if distance >= min_d else None
    if constraint.op == "equal":
        return distance if _native(axis, candidate_value) == constraint.value else None
    if constraint.op == "range":
        lo = constraint.lo
        hi = constraint.hi
        assert lo is not None and hi is not None  # validated in _validate_constraint
        num = _native(axis, candidate_value)
        return distance if lo <= num <= hi else None
    raise BadInput(f"unhandled op {constraint.op!r} on axis {axis.name!r}")


def _native(axis: Axis, value: Any) -> Any:
    return value.num if axis.kind == "scalar" and isinstance(value, Value) else value
