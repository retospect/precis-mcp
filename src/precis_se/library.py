"""Blocktree slice 4 — ranked library search (``search(kind='se',
wants=...)``). See docs/backlog/blocktree-library-build-plan.md §Slice 4
and docs/backlog/port-pose-and-composition-search.md Decision 2.

The rule that governs everything here: **every attribute is optional,
never a strict filter**; results are ranked, and every row shows its
per-attribute match/miss WITH the actual value — an empty result is
impossible unless the library itself is empty.

A candidate row is one block from one live ``se`` design (excluding
instances — a block with ``template`` set inherits its facts). Three
attributes are structural (:data:`_BUILTIN_KEYS`, read straight off the
block/tree); every other key is a **star-schema lookup** — a
``component`` spec value, or a ``material`` property value reached
through ``made-of`` — never a fact denormalised onto the block. Ranking
reuses :mod:`precis.quest.frontier` (Pareto tie-break over the numeric
keys) rather than inventing a second dominance rule.

Small pure(ish) functions so the handler (:mod:`precis_se.handler`) and
tests can each call the piece they need: :func:`parse_wants` (vet the
``wants=`` payload), :func:`resolve_block_attrs` (one block's per-key
match/miss), :func:`rank_rows` (score + Pareto tie-break), and
:func:`render_rows`/:func:`render_search` (the text body). Only
:func:`iter_candidates`/:func:`resolve_block_attrs`/:func:`render_search`
touch the store — the rest is plain data in, plain data out.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol

from precis.design import states as design_states
from precis.errors import BadInput
from precis.quest import frontier
from precis.utils import handle_registry
from precis_se import persist
from precis_se.atomic.vocab import role_halves
from precis_se.ops import SeBlock, SeTree

log = logging.getLogger(__name__)

#: The three structural keys resolved from the block/tree itself, never the
#: star schema — see the module docstring's "Three built-in structural
#: keys" and blocktree-library-build-plan.md §Slice 4.
_BUILTIN_KEYS = frozenset({"stimulus", "bistable", "joining"})

#: The only keys an explicit ``{...}`` want form may carry.
_ALLOWED_WANT_DICT_KEYS = frozenset(
    {"target", "min", "max", "tol", "weight", "conditions"}
)

_DEFAULT_TOL_REL = 0.1
_DEFAULT_WEIGHT = 1.0

#: ``store.list_refs`` page size while walking the whole se library — a
#: cheap round trip, and small enough that a realistic library finishes in
#: a handful of pages (never the silent-cap-at-50 the spec calls out).
_LIST_PAGE = 200

#: A named ceiling on how many blocks one search() call inspects, so a
#: pathological corpus can't make one call unbounded — the library is
#: store-provided, not user input, so this is a safety valve, not a filter.
_MAX_ROWS = 20_000


# ── wants= parsing ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class WantSpec:
    """One parsed ``wants[key]`` entry — the canonical union of the three
    accepted shapes (scalar target / ``[lo, hi]`` interval / explicit
    dict). ``tol``/``weight`` always carry a value (the documented
    defaults) even when the caller gave a bare scalar or interval."""

    target: Any = None
    min: float | None = None
    max: float | None = None
    tol: float = _DEFAULT_TOL_REL
    weight: float = _DEFAULT_WEIGHT
    #: Optional material-value-row filter (gr346735) — every given
    #: key/value pair must appear on a row's own ``conditions`` (compared
    #: as ``str(v).strip().lower()`` on both sides) for that row to
    #: survive the filter before :func:`pick_material_row` picks one. Only
    #: consulted for star-schema (material) keys — see
    #: :func:`_resolve_star_value`.
    conditions: dict[str, Any] | None = None

    @property
    def is_interval(self) -> bool:
        return self.min is not None or self.max is not None

    @property
    def is_numeric(self) -> bool:
        """True for a key whose match is a NUMBER comparison (interval, or
        a numeric target) — the axis that enters :mod:`precis.quest.
        frontier`'s tie-break. A built-in key is never numeric regardless
        of the shape it was given (:data:`_BUILTIN_KEYS` are categorical by
        construction — see :func:`resolve_block_attrs`)."""
        if self.is_interval:
            return True
        return isinstance(self.target, (int, float)) and not isinstance(
            self.target, bool
        )


def parse_wants(wants: Any) -> dict[str, WantSpec]:
    """Vet + canonicalise a ``wants=`` payload. Raises :class:`BadInput`
    naming the offending key for any non-dict/empty ``wants``, an unknown
    key in an explicit want dict, a malformed 2-list, or ``min > max`` —
    never for an unrecognised *attribute* name (that's a miss, scored and
    reported, see :func:`resolve_block_attrs`)."""
    if not isinstance(wants, dict):
        raise BadInput(
            f"search(kind='se', wants=...) must be a JSON object, got {wants!r}",
            next="wants={'stimulus': 'light', 'bistable': True}",
        )
    if not wants:
        raise BadInput(
            "search(kind='se', wants={}) — an empty wants= has nothing to rank on",
            next="name at least one attribute, e.g. wants={'stimulus': 'light'}",
        )
    return {key: _parse_one_want(key, raw) for key, raw in wants.items()}


def parse_conditions(context: str, raw: Any) -> dict[str, Any]:
    """Vet a ``conditions=`` filter — a non-empty JSON object of scalars,
    :class:`BadInput` otherwise. Shared by ``wants[key]['conditions']``
    (:func:`_parse_one_want`) and the ``compose=``/``requires=`` box's
    ``conditions`` key (:mod:`precis_se.compose`) so both fail the same
    way. ``context`` names the offending path, e.g.
    ``"wants['persistence_length']['conditions']"``."""
    if not isinstance(raw, dict) or not raw:
        raise BadInput(
            f"{context} must be a non-empty JSON object of scalars, got {raw!r}"
        )
    bad = {k: v for k, v in raw.items() if v is None or isinstance(v, (dict, list))}
    if bad:
        raise BadInput(f"{context} values must be str/number/bool, got {bad!r}")
    return raw


def _parse_one_want(key: str, raw: Any) -> WantSpec:
    if isinstance(raw, dict):
        unknown = sorted(set(raw) - _ALLOWED_WANT_DICT_KEYS)
        if unknown:
            raise BadInput(
                f"wants[{key!r}] has unknown key(s) {unknown} — allowed: "
                f"{sorted(_ALLOWED_WANT_DICT_KEYS)}"
            )
        lo, hi = raw.get("min"), raw.get("max")
        if lo is not None and hi is not None and lo > hi:
            raise BadInput(f"wants[{key!r}]: min={lo!r} > max={hi!r}")
        conditions = None
        if "conditions" in raw:
            conditions = parse_conditions(
                f"wants[{key!r}]['conditions']", raw["conditions"]
            )
        return WantSpec(
            target=raw.get("target"),
            min=None if lo is None else float(lo),
            max=None if hi is None else float(hi),
            tol=float(raw.get("tol", _DEFAULT_TOL_REL)),
            weight=float(raw.get("weight", _DEFAULT_WEIGHT)),
            conditions=conditions,
        )
    if isinstance(raw, list):
        if len(raw) != 2 or any(
            isinstance(v, bool) or not isinstance(v, (int, float)) for v in raw
        ):
            raise BadInput(
                f"wants[{key!r}] as a list must be [lo, hi] (two numbers), got {raw!r}"
            )
        lo, hi = float(raw[0]), float(raw[1])
        if lo > hi:
            raise BadInput(f"wants[{key!r}]: [{raw[0]}, {raw[1]}] has lo > hi")
        return WantSpec(min=lo, max=hi)
    if isinstance(raw, (int, float, str, bool)):
        return WantSpec(target=raw)
    raise BadInput(
        f"wants[{key!r}] must be a scalar, a [lo, hi] list, or a dict — got {raw!r}"
    )


# ── candidate walk ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Candidate:
    """One row's identity — a block from one live design, plus the loaded
    tree it lives in and the design ref's id (needed for the shared
    design-states tables and the design-level ``made-of`` lookup)."""

    design_slug: str
    block_name: str
    node: SeBlock
    tree: SeTree
    ref_id: int


def iter_candidates(store: Any) -> list[_Candidate]:
    """Every non-instance block in every live ``se`` design — paged
    (:data:`_LIST_PAGE`, never the silent 50-cap other list views use). A
    design that fails to load is logged and skipped, never raised (one bad
    design must not sink the whole library read)."""
    out: list[_Candidate] = []
    offset = 0
    while True:
        refs = store.list_refs(
            kind="se", order_by="id_desc", limit=_LIST_PAGE, offset=offset
        )
        if not refs:
            break
        for ref in refs:
            try:
                tree = persist.load_tree(store, ref.id)
            except Exception:
                log.warning(
                    "se library search: failed to load design %r",
                    ref.slug,
                    exc_info=True,
                )
                continue
            for name, node in tree.blocks.items():
                if node.template is not None:  # an instance inherits its facts
                    continue
                out.append(_Candidate(str(ref.slug), name, node, tree, ref.id))
                if len(out) >= _MAX_ROWS:
                    return out
        if len(refs) < _LIST_PAGE:
            break
        offset += _LIST_PAGE
    return out


# ── per-search read cache ───────────────────────────────────────────────────


class _ReadCache:
    """Memo of every store read the library walk repeats — one instance
    per :func:`render_search` call, so it can never serve a stale row
    across calls. Without it a four-key query over a few hundred blocks
    re-reads a block's transitions once per key and a bound component's
    spec values + ``made-of`` links once per block per key: thousands of
    round trips on the shared DB for one search. Every public function
    below takes ``cache=None`` and makes a fresh one, so tests and the
    handler may call any piece alone."""

    def __init__(self, store: Any) -> None:
        self.store = store
        self._transitions: dict[tuple[int, int], list[Any]] = {}
        self._states: dict[int, dict[int, list[Any]]] = {}
        self._components: dict[str, tuple[Any, dict[str, Any], list[Any]] | None] = {}
        self._materials: dict[int, tuple[Any, list[dict[str, Any]]] | None] = {}
        self._design_links: dict[int, list[Any]] = {}
        self._spec_rows: dict[str, Any] = {}
        self._prop_rows: dict[str, Any] = {}

    def transitions(self, ref_id: int, uid: int) -> list[Any]:
        key = (ref_id, uid)
        if key not in self._transitions:
            self._transitions[key] = design_states.transitions_for(
                self.store, ref_id, uid
            )
        return self._transitions[key]

    def states(self, ref_id: int, uid: int) -> list[Any]:
        # One query per DESIGN (design_states), not per block.
        if ref_id not in self._states:
            self._states[ref_id] = design_states.design_states(self.store, ref_id)
        return self._states[ref_id].get(uid, [])

    def component(self, slug: str) -> tuple[Any, dict[str, Any], list[Any]] | None:
        """``(ref, current spec values, made-of links)`` or ``None``."""
        if slug not in self._components:
            ref = self.store.get_ref(kind="component", id=slug)
            self._components[slug] = (
                None
                if ref is None
                else (
                    ref,
                    self.store.component_current_spec_values(ref.id),
                    self.store.links_for(ref.id, direction="out", relation="made-of"),
                )
            )
        return self._components[slug]

    def material(self, mat_ref_id: int) -> tuple[Any, list[dict[str, Any]]] | None:
        """``(ref, value rows)`` or ``None`` for a missing material."""
        if mat_ref_id not in self._materials:
            ref = self.store.get_ref(kind="material", id=mat_ref_id)
            self._materials[mat_ref_id] = (
                None
                if ref is None
                else (ref, self.store.material_values_for_ref(ref.id))
            )
        return self._materials[mat_ref_id]

    def design_links(self, ref_id: int) -> list[Any]:
        if ref_id not in self._design_links:
            self._design_links[ref_id] = self.store.links_for(
                ref_id, direction="out", relation="made-of"
            )
        return self._design_links[ref_id]

    def spec_row(self, key: str) -> Any:
        if key not in self._spec_rows:
            self._spec_rows[key] = self.store.component_spec_get(key)
        return self._spec_rows[key]

    def prop_row(self, key: str) -> Any:
        if key not in self._prop_rows:
            self._prop_rows[key] = self.store.material_property_get(key)
        return self._prop_rows[key]


# ── per-block attribute resolution ─────────────────────────────────────────


@dataclass
class AttrResult:
    matched: bool
    actual: str
    weight: float
    #: Numeric miss-distance for the frontier tie-break — ``None`` for a
    #: categorical (built-in) key, a finite value or ``float('inf')`` for a
    #: numeric star-schema key (spec: "missing value → inf").
    distance: float | None = None


def resolve_block_attrs(
    store: Any,
    cand: _Candidate,
    want_specs: dict[str, WantSpec],
    cache: _ReadCache | None = None,
) -> dict[str, AttrResult]:
    """One block's match/miss verdict for every wanted key."""
    cache = cache or _ReadCache(store)
    out: dict[str, AttrResult] = {}
    for key, spec in want_specs.items():
        if key == "stimulus":
            out[key] = _eval_stimulus(cand, spec, cache)
        elif key == "bistable":
            out[key] = _eval_bistable(cand, spec, cache)
        elif key == "joining":
            out[key] = _eval_joining(cand, spec)
        else:
            out[key] = _eval_star_key(cand, key, spec, cache)
    return out


def _eval_stimulus(cand: _Candidate, spec: WantSpec, cache: _ReadCache) -> AttrResult:
    kinds: set[str] = set()
    if cand.node.uid is not None:
        transitions = cache.transitions(cand.ref_id, cand.node.uid)
        kinds = {t.driver_kind for t in transitions}
    actual = ", ".join(sorted(kinds)) if kinds else "no transitions"
    matched = spec.target is not None and str(spec.target) in kinds
    return AttrResult(matched=matched, actual=actual, weight=spec.weight)


def _eval_bistable(cand: _Candidate, spec: WantSpec, cache: _ReadCache) -> AttrResult:
    n_states = 0
    has_thermal = False
    if cand.node.uid is not None:
        n_states = len(cache.states(cand.ref_id, cand.node.uid))
        has_thermal = any(
            t.driver_kind == "thermal"
            for t in cache.transitions(cand.ref_id, cand.node.uid)
        )
    is_bistable = n_states >= 2 and not has_thermal
    if n_states < 2:
        actual = f"{n_states} state" + ("" if n_states == 1 else "s")
    elif has_thermal:
        actual = "T-type (thermal reverse)"
    else:
        actual = f"{n_states} states, no thermal path"
    matched = isinstance(spec.target, bool) and spec.target == is_bistable
    return AttrResult(matched=matched, actual=actual, weight=spec.weight)


def _eval_joining(cand: _Candidate, spec: WantSpec) -> AttrResult:
    roles: set[str] = set()
    for port in cand.node.ports.values():
        roles.update(port.roles)
    actual = ", ".join(sorted(roles)) if roles else "no ports"
    matched = False
    if spec.target is not None:
        want = str(spec.target)
        if want in roles:
            matched = True
        else:
            halves = role_halves(want)
            hit = next((h for h in (halves or ()) if h in roles), None)
            if hit is not None:
                matched = True
                actual = f"{hit} ({want})"
    return AttrResult(matched=matched, actual=actual, weight=spec.weight)


def _miss_reason(cand: _Candidate) -> str:
    node = cand.node
    if node.bound_kind == "structure" and node.bound:
        return f"bound to structure {node.bound}: no value rows"
    return "no value row"


def _eval_star_key(
    cand: _Candidate, key: str, spec: WantSpec, cache: _ReadCache
) -> AttrResult:
    hit = _resolve_star_value(cand, key, cache, conditions=spec.conditions)
    if hit is None:
        return AttrResult(
            matched=False,
            actual=_miss_reason(cand),
            weight=spec.weight,
            distance=float("inf") if spec.is_numeric else None,
        )
    row, unit, provenance = hit
    matched, value_repr, distance = _match_value_row(row, spec)
    unit_suffix = f" {unit}" if unit else ""
    actual = f"{value_repr}{unit_suffix} {provenance}".strip()
    return AttrResult(
        matched=matched, actual=actual, weight=spec.weight, distance=distance
    )


def unknown_keys(
    store: Any, want_specs: dict[str, WantSpec], cache: _ReadCache | None = None
) -> list[str]:
    """Wanted keys that are neither a built-in nor a registered
    ``component`` spec / ``material`` property — the header's honesty note
    (never a refusal, see :func:`_eval_star_key`'s always-scored miss)."""
    cache = cache or _ReadCache(store)
    out = []
    for key in want_specs:
        if key in _BUILTIN_KEYS:
            continue
        if cache.spec_row(key) is not None:
            continue
        if cache.prop_row(key) is not None:
            continue
        out.append(key)
    return sorted(out)


# ── the star-schema join ────────────────────────────────────────────────


def _kind_for_ref_id(store: Any, ref_id: int | None) -> str | None:
    if ref_id is None:
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT kind FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return str(row[0]) if row is not None else None


def _display_source(store: Any, row: dict[str, Any]) -> str | None:
    ref_id = row.get("source_ref_id")
    if ref_id is not None:
        kind = row.get("source_kind") or _kind_for_ref_id(store, ref_id)
        if kind:
            return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"
    if row.get("source_url"):
        return str(row["source_url"])
    return None


def _row_value_repr(row: dict[str, Any]) -> str:
    """A bare display string for one value row — no unit, no spec-relative
    match info (that's :func:`_match_value_row`'s job); used only to list
    the samples a pick passed over in :func:`_provenance`."""
    if row.get("value_bool") is not None:
        return str(bool(row["value_bool"]))
    if row.get("value_text") is not None:
        return str(row["value_text"])
    num, low, high = row.get("value_num"), row.get("value_low"), row.get("value_high")
    if low is not None and high is not None:
        return f"{low}–{high}"
    if num is not None:
        return f"{num:g}"
    return "—"


def format_conditions(conditions: dict[str, Any] | None) -> str:
    """``"k1=v1, k2=v2"`` for a value row's ``conditions`` dict — the one
    shared formatter for :func:`_provenance`, :func:`_others_str`, and
    :func:`precis_se.compose._conditions` (gr346735 review — three
    copies of the same join collapsed into this). ``""`` for
    ``None``/empty."""
    if not conditions:
        return ""
    return ", ".join(f"{k}={v}" for k, v in conditions.items())


def _others_str(rows: list[dict[str, Any]], unit: str | None) -> str:
    """``others: <value>[<unit>] @ <conditions or —>``, capped at 3 with
    a ``+M more`` tail — the samples :func:`pick_material_row` passed
    over, for :func:`_provenance`."""
    cap = 3
    unit_suffix = f" {unit}" if unit else ""
    parts = []
    for row in rows[:cap]:
        cond_str = format_conditions(row.get("conditions")) or "—"
        parts.append(f"{_row_value_repr(row)}{unit_suffix} @ {cond_str}")
    out = ", ".join(parts)
    if len(rows) > cap:
        out += f", +{len(rows) - cap} more"
    return out


def _conditions_match(row: dict[str, Any], conditions: dict[str, Any]) -> bool:
    row_conditions = row.get("conditions") or {}
    for key, want in conditions.items():
        have = row_conditions.get(key)
        if have is None:
            return False
        if str(have).strip().lower() != str(want).strip().lower():
            return False
    return True


def pick_material_row(
    rows: list[dict[str, Any]], conditions: dict[str, Any] | None
) -> tuple[dict[str, Any], str]:
    """Pick ONE row from a property's value rows and say why (gr346735 —
    the star-schema read used to silently take ``rows[0]``, the newest,
    even when several sourced samples disagreed).

    ``rows`` arrives in :meth:`~precis.store.Store.material_values_for_ref`
    order: newest first within the property. Rule: if ``conditions`` is
    given, keep only rows whose own ``conditions`` contain every given
    key/value pair (``str(v).strip().lower()`` compared both sides); an
    empty match set falls back to the *unfiltered* rows instead of
    erroring (a documented near-miss). Among the surviving rows — filtered
    or not — a single row carrying a band (``value_low`` AND
    ``value_high`` both set — the spread summary) wins; otherwise the
    newest survivor wins. A ``conditions`` filter that actually narrowed
    the set doesn't override that tie-break, it only says so in the
    ``why`` label (``"conditions match, band"`` / ``"conditions match,
    newest"``).

    Returns ``(row, note)`` — ``note`` is ``""`` for a single-row property
    (the pre-existing, unchanged case), else ``"[no sample matches
    conditions {...}; took ]sample k of N (<why>)"`` for
    :func:`_provenance` to surface."""
    if len(rows) == 1:
        return rows[0], ""
    indexed = list(enumerate(rows, start=1))
    survivors = indexed
    prefix = ""
    why_prefix = ""
    if conditions:
        matched = [pair for pair in indexed if _conditions_match(pair[1], conditions)]
        if matched:
            survivors = matched
            if len(matched) < len(indexed):
                why_prefix = "conditions match, "
        else:
            prefix = f"no sample matches conditions {conditions!r}; took "
    bands = [
        pair
        for pair in survivors
        if pair[1].get("value_low") is not None
        and pair[1].get("value_high") is not None
    ]
    if len(bands) == 1:
        idx, picked = bands[0]
        why = f"{why_prefix}band"
    else:
        idx, picked = survivors[0]
        why = f"{why_prefix}newest"
    return picked, f"{prefix}sample {idx} of {len(rows)} ({why})"


def _provenance(
    store: Any,
    entity_kind: str,
    entity_ref: Any,
    row: dict[str, Any],
    *,
    unit: str | None = None,
    pick_note: str = "",
    other_rows: list[dict[str, Any]] | None = None,
) -> str:
    bits = [f"{entity_kind}:{entity_ref.slug}"]
    source = _display_source(store, row)
    if source:
        bits.append(f"← {source}")
    label = " ".join(bits)
    conditions = row.get("conditions") or {}
    if conditions:
        label += f"; conditions: {format_conditions(conditions)}"
    if pick_note:
        label += f"; {pick_note}"
        if other_rows:
            label += f"; others: {_others_str(other_rows, unit)}"
    return f"({label})"


def _material_hit(
    mat_ref_id: int,
    key: str,
    cache: _ReadCache,
    *,
    conditions: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, str] | None:
    hit = cache.material(mat_ref_id)
    if hit is None:
        return None
    mat_ref, rows = hit
    matching = [row for row in rows if row["property_id"] == key]
    if not matching:
        return None
    row, note = pick_material_row(matching, conditions)
    prop = cache.prop_row(key)
    unit = prop["canonical_unit"] if prop else None
    others = [r for r in matching if r is not row]
    return (
        row,
        unit,
        _provenance(
            cache.store,
            "material",
            mat_ref,
            row,
            unit=unit,
            pick_note=note,
            other_rows=others,
        ),
    )


def _resolve_star_value(
    cand: _Candidate,
    key: str,
    cache: _ReadCache,
    *,
    conditions: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str | None, str] | None:
    """Order: bound component's own spec values, then that component's
    ``made-of`` materials, then the design-level ``made-of`` materials
    (scoped to this block when the link's ``meta.block`` names one) —
    first hit wins. See the module docstring / blocktree-library-build-
    plan.md §Slice 4 "Attribute resolution per block". ``conditions``
    (gr346735) filters a *material* property's value rows before
    :func:`pick_material_row` picks one — it has no effect on a bound
    component's own spec value, which carries no ``conditions``."""
    node = cand.node
    if node.bound_kind == "component" and node.bound:
        comp = cache.component(node.bound)
        if comp is not None:
            comp_ref, specs, made_of = comp
            if key in specs:
                spec_row = cache.spec_row(key)
                unit = spec_row["canonical_unit"] if spec_row else None
                return (
                    dict(specs[key]),
                    unit,
                    _provenance(cache.store, "component", comp_ref, dict(specs[key])),
                )
            for link in made_of:
                hit = _material_hit(link.dst_ref_id, key, cache, conditions=conditions)
                if hit is not None:
                    return hit
    for link in cache.design_links(cand.ref_id):
        block_scope = (link.meta or {}).get("block")
        if block_scope is not None and block_scope != cand.block_name:
            continue
        hit = _material_hit(link.dst_ref_id, key, cache, conditions=conditions)
        if hit is not None:
            return hit
    return None


def value_row_number(row: dict[str, Any]) -> float | None:
    """A resolved star-schema value row's own numeric point value —
    ``value_num`` if the row carries one, else the midpoint of
    ``value_low``/``value_high`` (a declared band with no single point),
    else ``None`` (a bool/text row, or an empty one). Shared by every
    caller of :func:`_resolve_star_value` that wants a plain number out
    of the ``(row, unit, provenance)`` hit rather than
    :func:`_match_value_row`'s spec-scored comparison — factored out here
    (not duplicated per caller) after :mod:`precis_se.compose` and
    :mod:`precis_se.kinematics` both independently grew the identical
    three lines."""
    num = row.get("value_num")
    if num is not None:
        return float(num)
    low, high = row.get("value_low"), row.get("value_high")
    if low is not None and high is not None:
        return (float(low) + float(high)) / 2.0
    return None


def _match_value_row(
    row: dict[str, Any], spec: WantSpec
) -> tuple[bool, str, float | None]:
    """``(matched, display value, miss-distance)`` for one resolved value
    row against one :class:`WantSpec` — the three value shapes
    (bool/text/numeric) each compared their own way; only the numeric
    branch ever returns a non-``None`` distance."""
    if row.get("value_bool") is not None:
        value = bool(row["value_bool"])
        matched = isinstance(spec.target, bool) and spec.target == value
        return matched, str(value), None
    if row.get("value_text") is not None:
        text = str(row["value_text"])
        matched = isinstance(spec.target, str) and spec.target == text
        return matched, text, None

    num = row.get("value_num")
    low, high = row.get("value_low"), row.get("value_high")
    band_lo = low if low is not None else num
    band_hi = high if high is not None else num
    if band_lo is None or band_hi is None:
        return False, "—", float("inf") if spec.is_numeric else None
    display = f"{low}–{high}" if low is not None and high is not None else f"{num:g}"

    if spec.is_interval:
        lo = spec.min if spec.min is not None else float("-inf")
        hi = spec.max if spec.max is not None else float("inf")
        matched = not (band_hi < lo or band_lo > hi)
        if matched:
            return True, display, 0.0
        width = (
            (spec.max - spec.min)
            if (spec.min is not None and spec.max is not None)
            else max(abs(lo if lo != float("-inf") else hi), 1.0)
        )
        width = width or 1.0
        raw = (lo - band_hi) if band_hi < lo else (band_lo - hi)
        return False, display, raw / width

    if isinstance(spec.target, (int, float)) and not isinstance(spec.target, bool):
        point = num if num is not None else (band_lo + band_hi) / 2.0
        target = float(spec.target)
        diff = abs(point - target)
        matched = diff <= spec.tol * abs(target)
        if matched:
            return True, display, 0.0
        if target != 0:
            return False, display, diff / abs(target)
        return False, display, 0.0 if point == 0 else float("inf")

    return False, display, float("inf") if spec.is_numeric else None


# ── ranking ─────────────────────────────────────────────────────────────


@dataclass
class LibraryRow:
    design_slug: str
    block_name: str
    attrs: dict[str, AttrResult]
    score: float
    max_score: float
    sum_distance: float = 0.0
    on_frontier: bool = False

    @property
    def handle(self) -> str:
        return f"{self.design_slug}#{self.block_name}"


class Rankable(Protocol):
    """What :func:`order_rows` needs of a row — a slice 4 block row or a
    :mod:`precis_se.compose` composition row, ranked by the one order."""

    attrs: dict[str, AttrResult]
    score: float
    sum_distance: float
    on_frontier: bool

    @property
    def handle(self) -> str: ...


def order_rows[R: Rankable](rows: list[R], numeric_keys: list[str]) -> list[R]:
    """The one ranking order, in place: score desc, then :mod:`precis.
    quest.frontier`'s Pareto split over the numeric keys' miss distances
    (frontier first), then Σ distance, then handle — never a second
    dominance rule (docs/backlog/blocktree-library-build-plan.md §Slice 4
    "Reuse quest's selection machinery")."""
    fcands = [
        frontier.Candidate(
            ref_id=idx,
            handle=row.handle,
            name=row.handle,
            # Every numeric key always carries a finite-or-inf distance
            # (never None) — see AttrResult.distance's contract — but a
            # defensive filter keeps the type honest for Candidate.measures.
            measures={
                k: d for k in numeric_keys if (d := row.attrs[k].distance) is not None
            },
            converged=True,
        )
        for idx, row in enumerate(rows)
    ]
    result = frontier.pareto_split(fcands, [(k, "min") for k in numeric_keys])
    frontier_idx = {c.ref_id for c in result.frontier}
    for idx, row in enumerate(rows):
        row.on_frontier = idx in frontier_idx
    rows.sort(
        key=lambda r: (-r.score, 0 if r.on_frontier else 1, r.sum_distance, r.handle)
    )
    return rows


def rank_rows(
    store: Any,
    candidates: list[_Candidate],
    want_specs: dict[str, WantSpec],
    cache: _ReadCache | None = None,
) -> list[LibraryRow]:
    """Score every candidate, then break ties with :mod:`precis.quest.
    frontier`'s Pareto split over the numeric keys — never a second
    dominance rule (docs/backlog/blocktree-library-build-plan.md §Slice 4
    "Reuse quest's selection machinery")."""
    numeric_keys = [
        k for k, s in want_specs.items() if k not in _BUILTIN_KEYS and s.is_numeric
    ]
    max_score = sum(s.weight for s in want_specs.values())
    cache = cache or _ReadCache(store)

    rows: list[LibraryRow] = []
    for cand in candidates:
        attrs = resolve_block_attrs(store, cand, want_specs, cache)
        rows.append(
            LibraryRow(
                design_slug=cand.design_slug,
                block_name=cand.block_name,
                attrs=attrs,
                score=sum(a.weight for a in attrs.values() if a.matched),
                max_score=max_score,
                sum_distance=sum_distances(attrs, numeric_keys),
            )
        )
    return order_rows(rows, numeric_keys)


def sum_distances(attrs: dict[str, AttrResult], numeric_keys: list[str]) -> float:
    return sum(d for k in numeric_keys if (d := attrs[k].distance) is not None)


# ── rendering ────────────────────────────────────────────────────────────


def _fmt_attr(key: str, attr: AttrResult) -> str:
    mark = "✓" if attr.matched else "✗"
    return f"{mark}{key}: {attr.actual}"


def render_rows(
    rows: list[LibraryRow],
    want_specs: dict[str, WantSpec],
    *,
    wants_repr: Any,
    unknown: list[str],
    narrow_note: str = "",
    page_size: int = 20,
) -> str:
    header = f"# {len(rows)} library block(s) ranked for wants={wants_repr!r}"
    if narrow_note:
        header += f"  {narrow_note}"
    lines = [header]
    if unknown:
        lines.append(
            f"⚠ not a registered property/spec: {', '.join(unknown)} "
            "— scored as a miss, never a refusal"
        )
    shown = rows[: max(page_size, 0)]
    for i, row in enumerate(shown, start=1):
        cells = "  ".join(_fmt_attr(k, row.attrs[k]) for k in want_specs)
        lines.append(
            f"{i}. {row.design_slug}#{row.block_name}  "
            f"{row.score:g}/{row.max_score:g}  {cells}"
        )
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more (page_size={page_size})")
    if rows:
        top = rows[0]
        lines.append("")
        lines.append(
            "Next: edit(kind='se', id='<your-design>', "
            "ops=[{'op':'instance_block','name':'<new-name>',"
            f"'template':'{top.design_slug}#{top.block_name}'}}])"
        )
    return "\n".join(lines)


_EMPTY_LIBRARY_BODY = (
    "the se library is empty — no designs carry blocks yet\n\n"
    "Next: put(kind='se', id='<slug>', "
    'text=\'{"ops":[{"op":"add_block","name":"<block>"}]}\')'
)


def narrow_candidates(
    candidates: list[_Candidate],
    narrowed_slugs: set[str] | None,
    q: str | None,
    narrow_note: str,
) -> tuple[list[_Candidate], str]:
    """Apply the handler's ``q=`` pre-pass: an empty/``None`` set means
    "the whole library"; a narrow whose designs hold no library block of
    their own (instances only) also falls back, and the note says that
    instead of the handler's "narrowed to N"."""
    if not narrowed_slugs:
        return candidates, narrow_note
    scoped = [c for c in candidates if c.design_slug in narrowed_slugs]
    if scoped:
        return scoped, narrow_note
    return candidates, (
        f"(q={q!r} matched {len(narrowed_slugs)} design(s) with no "
        "library blocks of their own — showing the whole library)"
    )


def render_search(
    store: Any,
    *,
    wants: Any,
    compose: Any = None,
    q: str | None = None,
    narrowed_slugs: set[str] | None = None,
    narrow_note: str = "",
    page_size: int = 20,
) -> str:
    """The whole ``search(kind='se', wants=...)`` read: parse, walk the
    library, score, rank, render. ``narrowed_slugs``/``narrow_note`` are
    the handler's ``q=`` pre-pass (:meth:`precis_se.handler.SeHandler.
    search`) — see :func:`narrow_candidates`. With ``compose=`` the same
    walk feeds the composition proposer (:mod:`precis_se.compose`) and
    ``wants=`` becomes optional."""
    if compose is not None:
        from precis_se import compose as compose_mod

        return compose_mod.render_compose(
            store,
            compose=compose,
            wants=wants,
            q=q,
            narrowed_slugs=narrowed_slugs,
            narrow_note=narrow_note,
            page_size=page_size,
        )
    want_specs = parse_wants(wants)
    cache = _ReadCache(store)
    candidates = iter_candidates(store)
    if not candidates:
        return _EMPTY_LIBRARY_BODY
    candidates, narrow_note = narrow_candidates(
        candidates, narrowed_slugs, q, narrow_note
    )
    unknown = unknown_keys(store, want_specs, cache)
    rows = rank_rows(store, candidates, want_specs, cache)
    return render_rows(
        rows,
        want_specs,
        wants_repr=wants,
        unknown=unknown,
        narrow_note=narrow_note,
        page_size=page_size,
    )


__all__ = [
    "AttrResult",
    "LibraryRow",
    "Rankable",
    "WantSpec",
    "format_conditions",
    "iter_candidates",
    "narrow_candidates",
    "order_rows",
    "parse_conditions",
    "parse_wants",
    "pick_material_row",
    "rank_rows",
    "render_rows",
    "render_search",
    "resolve_block_attrs",
    "sum_distances",
    "unknown_keys",
]
