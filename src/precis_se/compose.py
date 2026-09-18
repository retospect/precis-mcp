"""Composition proposer — ``search(kind='se', compose={...})``. See
docs/backlog/port-pose-and-composition-search.md "New item — composition
proposer" and blocktree-library-build-plan.md §Slice 4.

A deterministic small-integer enumeration over slice 4's library rows
(:mod:`precis_se.library`): a *requirement box* — a port-to-port stroke
``delta`` and a long-state ``span``, each an interval — is met by ``n``
switches in series plus ``m`` spacers. Every composition is scored
exactly like a slice 4 row (its synthesized Δ/span value rows run
through the same :func:`~precis_se.library._match_value_row`; ``wants=``
keys evaluate on the switch block and pass through unchanged) and ranked
with the same :func:`~precis_se.library.order_rows`. Never a strict
filter, never empty while the library holds one switch: with no feasible
composition the nearest misses show with their distances.

NOT the LLM proposers: ``se_propose_atomic`` (:mod:`precis_se.atomic.
propose`) fills one block's fragment; ``se_propose`` is reserved for the
whole-design LLM proposer. This is arithmetic over sourced facts.

Per-unit facts come through the star schema, never off the block, under
five ordinary ``material``/``component`` property keys (an unknown one
mints ``proposed``-tier on first write — no migration):
``delta_length`` (Å, long→short Δ end-to-end; a block with a row is a
*switch*), ``unit_length`` (nm, long-state port-to-port; with no
``delta_length`` the block is a *spacer*), ``pss_short_fraction`` (0–1,
photostationary-state conversion — the row's conditions carry the
wavelength), ``thermal_half_life`` (s, the T-type reverse), and
``persistence_length`` (nm, stiffness). Blocks with neither length row
are skipped and counted in the header.

What every row must surface (the backlog's "never hide" list): the PSS-
scaled stroke beside the ideal one (or a ``PSS unknown`` mark), the
T-type verdict with τ½, ``floppy`` whenever the span exceeds the
stiffness-bearing unit's persistence length (``stiffness unknown`` when
it has no row), and the switch↔spacer port complementarity (slice 3's
halves). The Next line is the ``instance_block`` × n + spacer ops script.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis_se import library
from precis_se.atomic.vocab import JOINING_HALVES, role_halves
from precis_se.library import (
    AttrResult,
    WantSpec,
    _Candidate,
    _ReadCache,
)

DELTA_KEY = "delta_length"
LENGTH_KEY = "unit_length"
PSS_KEY = "pss_short_fraction"
HALF_LIFE_KEY = "thermal_half_life"
LP_KEY = "persistence_length"

#: Display units when the registry holds no canonical unit for a key (a
#: freshly minted proposed-tier property declared without ``unit=``).
_DEFAULT_UNITS = {DELTA_KEY: "Å", LENGTH_KEY: "nm", LP_KEY: "nm"}

_DEFAULT_N_MAX = 6
_DEFAULT_M_MAX = 4
#: Sanity ceiling on a caller's ``n_max``/``m_max`` — past this the
#: enumeration is a search, not a proposal.
_HARD_MAX = 50
#: Named cap on compositions one call scores (the backlog's 2 000).
_MAX_COMPOSITIONS = 2_000
_ALLOWED_KEYS = frozenset({"delta", "span", "n_max", "m_max"})


# ── compose= parsing ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComposeBox:
    """The parsed requirement box — Decision 3's "box with interval
    constraints" as a kwarg until transitions carry ranges."""

    delta: WantSpec | None
    span: WantSpec | None
    n_max: int = _DEFAULT_N_MAX
    m_max: int = _DEFAULT_M_MAX

    @property
    def specs(self) -> dict[str, WantSpec]:
        out: dict[str, WantSpec] = {}
        if self.delta is not None:
            out["delta"] = self.delta
        if self.span is not None:
            out["span"] = self.span
        return out


def parse_compose(compose: Any) -> ComposeBox:
    """Vet + canonicalise ``compose=``. :class:`BadInput` for a non-dict,
    an unknown key, a box with neither ``delta`` nor ``span``, a
    non-numeric range, or an out-of-range count — never for what the
    library holds (that is scored and reported)."""
    example = "compose={'delta': [10, 12], 'span': [40, 50]}"
    if isinstance(compose, str):
        raise BadInput(
            f"compose={compose!r}: reading the box off a block's declared "
            "transition ranges is not shipped yet (port-pose-and-composition-"
            "search.md Decision 3) — pass the box as a dict",
            next=example,
        )
    if not isinstance(compose, dict):
        raise BadInput(
            f"search(kind='se', compose=...) must be a JSON object, got {compose!r}",
            next=example,
        )
    unknown = sorted(set(compose) - _ALLOWED_KEYS)
    if unknown:
        raise BadInput(
            f"compose has unknown key(s) {unknown} — allowed: {sorted(_ALLOWED_KEYS)}",
            next=example,
        )
    delta = _range(compose, "delta", "Å")
    span = _range(compose, "span", "nm")
    if delta is None and span is None:
        raise BadInput(
            "compose= needs at least one of 'delta' (Å, port-to-port stroke) "
            "or 'span' (nm, long-state length)",
            next=example,
        )
    return ComposeBox(
        delta=delta,
        span=span,
        n_max=_count(compose, "n_max", _DEFAULT_N_MAX, lo=1),
        m_max=_count(compose, "m_max", _DEFAULT_M_MAX, lo=0),
    )


def _range(compose: dict[str, Any], key: str, unit: str) -> WantSpec | None:
    if key not in compose:
        return None
    spec = library._parse_one_want(key, compose[key])
    if not spec.is_numeric:
        raise BadInput(
            f"compose[{key!r}] must be a number ({unit}) or a [lo, hi] "
            f"range, got {compose[key]!r}"
        )
    return spec


def _count(compose: dict[str, Any], key: str, default: int, *, lo: int) -> int:
    raw = compose.get(key, default)
    if isinstance(raw, bool) or not isinstance(raw, int) or not lo <= raw <= _HARD_MAX:
        raise BadInput(
            f"compose[{key!r}] must be an integer in {lo}..{_HARD_MAX}, got {raw!r}"
        )
    return raw


# ── per-unit facts ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Unit:
    """One library block with its length facts resolved. ``delta`` set →
    a switch; ``length`` set with no ``delta`` → a spacer."""

    cand: _Candidate
    delta: float | None
    length: float | None
    pss: float | None
    pss_conditions: str
    half_life: float | None
    lp: float | None

    @property
    def handle(self) -> str:
        return f"{self.cand.design_slug}#{self.cand.block_name}"

    @property
    def is_switch(self) -> bool:
        return self.delta is not None

    @property
    def is_spacer(self) -> bool:
        return self.delta is None and self.length is not None

    @property
    def roles(self) -> set[str]:
        roles: set[str] = set()
        for port in self.cand.node.ports.values():
            roles.update(port.roles)
        return roles


def _number(row: dict[str, Any]) -> float | None:
    num = row.get("value_num")
    if num is not None:
        return float(num)
    low, high = row.get("value_low"), row.get("value_high")
    if low is not None and high is not None:
        return (float(low) + float(high)) / 2.0
    return None


def _fact(
    cand: _Candidate, key: str, cache: _ReadCache
) -> tuple[float, dict[str, Any]] | None:
    hit = library._resolve_star_value(cand, key, cache)
    if hit is None:
        return None
    row, _unit, _prov = hit
    value = _number(row)
    return None if value is None else (value, row)


def _conditions(row: dict[str, Any] | None) -> str:
    conditions = (row or {}).get("conditions") or {}
    return ", ".join(f"{k}={v}" for k, v in conditions.items())


def resolve_unit(cand: _Candidate, cache: _ReadCache) -> Unit:
    delta = _fact(cand, DELTA_KEY, cache)
    length = _fact(cand, LENGTH_KEY, cache)
    pss = _fact(cand, PSS_KEY, cache)
    half_life = _fact(cand, HALF_LIFE_KEY, cache)
    lp = _fact(cand, LP_KEY, cache)
    return Unit(
        cand=cand,
        delta=None if delta is None else delta[0],
        length=None if length is None else length[0],
        pss=None if pss is None else pss[0],
        pss_conditions=_conditions(None if pss is None else pss[1]),
        half_life=None if half_life is None else half_life[0],
        lp=None if lp is None else lp[0],
    )


def unit_label(cache: _ReadCache, key: str) -> str:
    """The registry's canonical unit for a fact key (material property,
    else component spec), falling back to the documented default."""
    for row in (cache.prop_row(key), cache.spec_row(key)):
        if row is not None and row["canonical_unit"]:
            return str(row["canonical_unit"])
    return _DEFAULT_UNITS.get(key, "")


# ── enumeration ────────────────────────────────────────────────────────────


@dataclass
class Composition:
    switch: Unit
    n: int
    spacer: Unit | None
    m: int
    delta_ideal: float
    delta_eff: float
    span: float | None
    attrs: dict[str, AttrResult]
    score: float
    max_score: float
    notes: list[str] = field(default_factory=list)
    sum_distance: float = 0.0
    on_frontier: bool = False

    @property
    def handle(self) -> str:
        label = f"{self.n} × {self.switch.handle}"
        if self.spacer is not None and self.m:
            label += f" + {self.m} × {self.spacer.handle}"
        return label


def _fmt_duration(seconds: float) -> str:
    if seconds < 60.0:
        return f"{seconds:.3g} s"
    if seconds < 3600.0:
        return f"{seconds / 60.0:.3g} min"
    if seconds < 86400.0:
        return f"{seconds / 3600.0:.3g} h"
    return f"{seconds / 86400.0:.3g} d"


def _delta_attr(comp: Composition, spec: WantSpec, unit: str) -> AttrResult:
    matched, _display, distance = library._match_value_row(
        {"value_num": comp.delta_eff}, spec
    )
    actual = f"{comp.delta_ideal:g} {unit}"
    switch = comp.switch
    if switch.pss is not None:
        cond = f"; {switch.pss_conditions}" if switch.pss_conditions else ""
        actual += (
            f" ({comp.delta_eff:g} {unit} at PSS {switch.pss * 100:.0f} % short{cond})"
        )
    else:
        actual += " (PSS unknown)"
    return AttrResult(
        matched=matched, actual=actual, weight=spec.weight, distance=distance
    )


def _span_attr(comp: Composition, spec: WantSpec, unit: str) -> AttrResult:
    if comp.span is None:
        missing = comp.switch if comp.switch.length is None else comp.spacer
        who = missing.handle if missing is not None else comp.switch.handle
        return AttrResult(
            matched=False,
            actual=f"unknown (no {LENGTH_KEY} row on {who})",
            weight=spec.weight,
            distance=float("inf"),
        )
    matched, _display, distance = library._match_value_row(
        {"value_num": comp.span}, spec
    )
    return AttrResult(
        matched=matched,
        actual=f"{comp.span:g} {unit}",
        weight=spec.weight,
        distance=distance,
    )


def _tau_suffix(switch: Unit) -> str:
    if switch.half_life is None:
        return ""
    return f", τ½ {_fmt_duration(switch.half_life)}"


def _bistable_note(comp: Composition, cache: _ReadCache) -> str:
    verdict = library._eval_bistable(comp.switch.cand, WantSpec(target=True), cache)
    mark = "✓" if verdict.matched else "✗"
    return f"bistable {mark} ({verdict.actual}{_tau_suffix(comp.switch)})"


def _stiffness_note(comp: Composition, unit: str) -> str | None:
    if comp.span is None:
        return None
    bearer = comp.spacer if comp.spacer is not None else comp.switch
    if comp.spacer is None and comp.n == 1:
        return None
    if bearer.lp is None:
        return f"stiffness unknown ({bearer.handle}: no {LP_KEY} row)"
    if comp.span > bearer.lp:
        return (
            f"floppy: span {comp.span:g} {unit} > Lp {bearer.lp:g} {unit} "
            f"({bearer.handle})"
        )
    return f"stiff: Lp {bearer.lp:g} {unit} ≥ span ({bearer.handle})"


def _joining_name(a: str, b: str) -> str:
    for name, halves in JOINING_HALVES.items():
        if {a, b} == set(halves):
            return f" ({name})"
    return ""


def _complementary_pair(a_roles: set[str], b_roles: set[str]) -> tuple[str, str] | None:
    """A ``(role on A, role on B)`` pair slice 3 lets bond — complementary
    halves first, then a symmetric role both afford."""
    for role in sorted(a_roles):
        halves = role_halves(role)
        if halves is None:
            continue
        other = halves[1] if halves[0] == role else halves[0]
        if other in b_roles:
            return role, other
    for role in sorted(a_roles & b_roles):
        if role_halves(role) is None:
            return role, role
    return None


def _joining_note(comp: Composition) -> str | None:
    if comp.spacer is not None:
        pair = _complementary_pair(comp.switch.roles, comp.spacer.roles)
        who = "switch↔spacer"
        other_roles = comp.spacer.roles
    elif comp.n > 1:
        pair = _complementary_pair(comp.switch.roles, comp.switch.roles)
        who = "switch↔switch"
        other_roles = comp.switch.roles
    else:
        return None
    if pair is not None:
        return f"joining {who}: {pair[0]}↔{pair[1]}{_joining_name(*pair)}"
    a = ", ".join(sorted(comp.switch.roles)) or "no ports"
    b = ", ".join(sorted(other_roles)) or "no ports"
    return f"joining {who}: no complementary ports ({a} vs {b})"


def enumerate_compositions(
    switches: list[Unit],
    spacers: list[Unit],
    box: ComposeBox,
    *,
    cap: int = _MAX_COMPOSITIONS,
) -> tuple[list[Composition], bool]:
    """Every (switch, n, spacer, m) up to the box's counts — ``(rows,
    capped)``. Attrs/score are filled by :func:`score_compositions`."""
    out: list[Composition] = []
    for switch in switches:
        delta_s = switch.delta
        if delta_s is None:  # not a switch — caller's classification slipped
            continue
        for n in range(1, box.n_max + 1):
            delta_ideal = n * delta_s
            delta_eff = (
                delta_ideal * switch.pss if switch.pss is not None else delta_ideal
            )
            for spacer in [None, *spacers]:
                m_values = range(1, box.m_max + 1) if spacer is not None else range(1)
                for m in m_values:
                    span: float | None = None
                    if switch.length is not None and (
                        spacer is None or spacer.length is not None
                    ):
                        span = n * switch.length
                        if spacer is not None and spacer.length is not None:
                            span += m * spacer.length
                    out.append(
                        Composition(
                            switch=switch,
                            n=n,
                            spacer=spacer,
                            m=m,
                            delta_ideal=delta_ideal,
                            delta_eff=delta_eff,
                            span=span,
                            attrs={},
                            score=0.0,
                            max_score=0.0,
                        )
                    )
                    if len(out) >= cap:
                        return out, True
    return out, False


def score_compositions(
    store: Any,
    comps: list[Composition],
    box: ComposeBox,
    want_specs: dict[str, WantSpec],
    cache: _ReadCache,
    *,
    units: dict[str, str],
) -> list[Composition]:
    """Score like slice 4 (box keys on synthesized value rows, ``wants``
    keys on the switch block), stamp the must-surface notes, and rank
    with :func:`~precis_se.library.order_rows`."""
    box_specs = box.specs
    numeric_keys = list(box_specs) + [
        k
        for k, s in want_specs.items()
        if k not in library._BUILTIN_KEYS and s.is_numeric
    ]
    max_score = sum(s.weight for s in box_specs.values()) + sum(
        s.weight for s in want_specs.values()
    )
    switch_attrs: dict[str, dict[str, AttrResult]] = {}
    for comp in comps:
        attrs: dict[str, AttrResult] = {}
        if box.delta is not None:
            attrs["delta"] = _delta_attr(comp, box.delta, units[DELTA_KEY])
        if box.span is not None:
            attrs["span"] = _span_attr(comp, box.span, units[LENGTH_KEY])
        if want_specs:
            key = comp.switch.handle
            if key not in switch_attrs:
                switch_attrs[key] = library.resolve_block_attrs(
                    store, comp.switch.cand, want_specs, cache
                )
            attrs.update(switch_attrs[key])
        if "bistable" in attrs:
            verdict = attrs["bistable"]
            attrs["bistable"] = AttrResult(
                matched=verdict.matched,
                actual=verdict.actual + _tau_suffix(comp.switch),
                weight=verdict.weight,
                distance=verdict.distance,
            )
        else:
            comp.notes.append(_bistable_note(comp, cache))
        for note in (_stiffness_note(comp, units[LENGTH_KEY]), _joining_note(comp)):
            if note:
                comp.notes.append(note)
        comp.attrs = attrs
        comp.score = sum(a.weight for a in attrs.values() if a.matched)
        comp.max_score = max_score
        comp.sum_distance = library.sum_distances(attrs, numeric_keys)
    return library.order_rows(comps, numeric_keys)


# ── rendering ──────────────────────────────────────────────────────────────


def _chain(comp: Composition) -> list[tuple[str, Unit]]:
    """Instance names in chain order: switches and spacers alternate while
    both remain, leftovers trail."""
    chain: list[tuple[str, Unit]] = []
    for i in range(comp.n):
        chain.append((f"s{i + 1}", comp.switch))
        if comp.spacer is not None and i < comp.m:
            chain.append((f"p{i + 1}", comp.spacer))
    if comp.spacer is not None:
        for j in range(comp.n, comp.m):
            chain.append((f"p{j + 1}", comp.spacer))
    return chain


def _port_pair(a: Unit, b: Unit) -> tuple[str, str]:
    pair = _complementary_pair(a.roles, b.roles)
    if pair is None:
        return "<port>", "<port>"

    def port_named(unit: Unit, role: str) -> str:
        for name, port in unit.cand.node.ports.items():
            if role in port.roles:
                return name
        return "<port>"

    return port_named(a, pair[0]), port_named(b, pair[1])


def ops_script(comp: Composition) -> list[dict[str, Any]]:
    """The ``instance_block`` × n + spacer ops that realise one row,
    joining consecutive units through complementary ports."""
    chain = _chain(comp)
    ops: list[dict[str, Any]] = [
        {"op": "instance_block", "name": name, "template": unit.handle}
        for name, unit in chain
    ]
    for (a_name, a_unit), (b_name, b_unit) in itertools.pairwise(chain):
        a_port, b_port = _port_pair(a_unit, b_unit)
        ops.append(
            {"op": "connect", "a": f"{a_name}.{a_port}", "b": f"{b_name}.{b_port}"}
        )
    return ops


def render_compositions(
    rows: list[Composition],
    box: ComposeBox,
    want_specs: dict[str, WantSpec],
    *,
    compose_repr: Any,
    wants_repr: Any,
    counts: dict[str, int],
    unknown: list[str],
    capped: bool,
    narrow_note: str = "",
    page_size: int = 20,
) -> str:
    header = f"# {len(rows)} composition(s) ranked for compose={compose_repr!r}"
    if want_specs:
        header += f" wants={wants_repr!r}"
    if narrow_note:
        header += f"  {narrow_note}"
    lines = [header]
    facts = (
        f"{counts['switches']} switch(es) × {counts['spacers']} spacer(s) from "
        f"{counts['blocks']} library block(s)"
    )
    if counts["skipped"]:
        facts += (
            f"; {counts['skipped']} block(s) carry no length facts — "
            f"put(kind='material', id=<mat>, property='{LENGTH_KEY}', value=…, "
            f"unit='nm') and link the design made-of it (property='{DELTA_KEY}' "
            "for a switch)"
        )
    if capped:
        facts += f"; enumeration capped at {_MAX_COMPOSITIONS} — lower n_max/m_max"
    lines.append(facts)
    if unknown:
        lines.append(
            f"⚠ not a registered property/spec: {', '.join(unknown)} "
            "— scored as a miss, never a refusal"
        )
    keys = [*box.specs, *want_specs]
    shown = rows[: max(page_size, 0)]
    for i, row in enumerate(shown, start=1):
        cells = "  ".join(library._fmt_attr(k, row.attrs[k]) for k in keys)
        line = f"{i}. {row.handle}  {row.score:g}/{row.max_score:g}  {cells}"
        if row.notes:
            line += "  · " + " · ".join(row.notes)
        lines.append(line)
    if len(rows) > len(shown):
        lines.append(f"... {len(rows) - len(shown)} more (page_size={page_size})")
    if rows:
        lines.append("")
        lines.append(
            f"Next: edit(kind='se', id='<your-design>', ops={ops_script(rows[0])!r})"
        )
    return "\n".join(lines)


def render_compose(
    store: Any,
    *,
    compose: Any,
    wants: Any = None,
    q: str | None = None,
    narrowed_slugs: set[str] | None = None,
    narrow_note: str = "",
    page_size: int = 20,
) -> str:
    """The whole ``search(kind='se', compose=...)`` read — the entry
    :func:`precis_se.library.render_search` dispatches to."""
    box = parse_compose(compose)
    want_specs = library.parse_wants(wants) if wants is not None else {}
    clash = sorted(set(want_specs) & set(box.specs))
    if clash:
        raise BadInput(
            f"wants{clash} collides with compose{clash} — the box owns that key",
            next="move the range into compose= and drop it from wants=",
        )
    cache = _ReadCache(store)
    candidates = library.iter_candidates(store)
    if not candidates:
        return library._EMPTY_LIBRARY_BODY
    candidates, narrow_note = library.narrow_candidates(
        candidates, narrowed_slugs, q, narrow_note
    )
    switches: list[Unit] = []
    spacers: list[Unit] = []
    skipped = 0
    for cand in candidates:
        unit = resolve_unit(cand, cache)
        if unit.is_switch:
            switches.append(unit)
        elif unit.is_spacer:
            spacers.append(unit)
        else:
            skipped += 1
    counts = {
        "blocks": len(candidates),
        "switches": len(switches),
        "spacers": len(spacers),
        "skipped": skipped,
    }
    if not switches:
        return (
            f"no library block carries a {DELTA_KEY} row — nothing to compose "
            f"({counts['blocks']} block(s) inspected, {counts['spacers']} spacer(s))"
            f"{'  ' + narrow_note if narrow_note else ''}\n\n"
            f"Next: put(kind='material', id='<mat>', property='{DELTA_KEY}', "
            "value=<Å>, unit='Å') and link the switch's design or component "
            "made-of it"
        )
    units = {key: unit_label(cache, key) for key in (DELTA_KEY, LENGTH_KEY)}
    comps, capped = enumerate_compositions(switches, spacers, box)
    rows = score_compositions(store, comps, box, want_specs, cache, units=units)
    return render_compositions(
        rows,
        box,
        want_specs,
        compose_repr=compose,
        wants_repr=wants,
        counts=counts,
        unknown=library.unknown_keys(store, want_specs, cache) if want_specs else [],
        capped=capped,
        narrow_note=narrow_note,
        page_size=page_size,
    )


__all__ = [
    "ComposeBox",
    "Composition",
    "Unit",
    "enumerate_compositions",
    "ops_script",
    "parse_compose",
    "render_compose",
    "render_compositions",
    "resolve_unit",
    "score_compositions",
]
