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

``n_max``/``m_max`` bound the linear chain's enumeration (:func:`enumerate_
compositions`) — an explicit ``compose['n_max'/'m_max']`` is respected as
given; left out, each switch derives its OWN ``n_max`` from ``ceil(delta_hi
/ switch.delta_length)`` (only when ``delta`` is boxed) and each spacer its
OWN ``m_max`` from ``ceil(span_hi / spacer.unit_length)`` (only when
``span`` is boxed), so a 20–30 nm span box is reachable through a 0.34
nm/bp spacer (m ≈ 59–88) without a fixed small default capping it below
4 nm (gr356739) — every derived bound is clamped to :data:`_HARD_MAX`.
Past :data:`_MAX_COMPOSITIONS` candidates for one (switch, spacer) pair,
the ``m`` sweep stops walking every value from 1 and narrows to the band
around the box's own span edges instead (:func:`_band_m_values`) — the
derivation exists so the feasible band is reached at all, not so every
unrelated small ``m`` is scored on the way there. When the best reachable
span (or delta) under the effective bounds still misses the box's lower
edge, the header says so by name — the bound, the best value actually
reached, and (unless the bound is already at the hard ceiling) the value
that would reach it (:func:`_span_reachability_note`/:func:`_delta_
reachability_note`).

R3 (docs/backlog/port-rotation-and-lever-composition.md "Slice R3") adds
a second family beside the linear chain: a **rotary unit** — a block
whose R2-derived transition swing (:mod:`precis_se.kinematics`) or
sourced ``step_angle`` is non-zero (:func:`resolve_rotary`) — paired with
``k`` **arm** units (ordinary spacers) at its rotating port. A third box
key, ``swing`` (°, total rotary angle), joins ``delta``/``span``
(:class:`ComposeBox`); ``delta`` scores a lever row's tip stroke
(``2·(arm₀ + k·unit_length)·sin(angle/2)``) through the SAME value-row
match as a switch chain's, ranked in ONE merged list (``family`` on each
row: ``'lever'``/``'chain'``); ``swing`` instead scores ``n`` rotary
units chained (angles summed, ``family='series'``), alone (a switch
chain has no angle). ``delta`` and ``swing`` together is refused — one
stroke measure, never both. A lever row's own ``span`` is its arm reach
(``arm₀ + k·unit_length``); a rotary with no usable envelope (no arm₀)
is left out of the lever family and counted in the header, mirroring
``counts['skipped']``. The rotating-port↔arm complementarity gets the
same ``joining`` note a switch↔spacer chain does — never a fabricated
``'<port>'`` connect when there is no complementary role.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.design import states as design_states
from precis.errors import BadInput, NotFound
from precis_se import kinematics as se_kinematics
from precis_se import library, persist
from precis_se.atomic.validate import _M_TO_A
from precis_se.atomic.vocab import _complementary_pair, _joining_name
from precis_se.library import (
    AttrResult,
    WantSpec,
    _Candidate,
    _ReadCache,
)
from precis_se.ops import SeBlock, SeTree, effective_envelope

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
#: Sanity ceiling on ANY ``n_max``/``m_max`` — an explicit one (checked in
#: :func:`_count`) or a DERIVED one (gr356739, :func:`_derive_bound`:
#: ``ceil(delta_hi / switch.delta_length)`` / ``ceil(span_hi /
#: spacer.unit_length)``, so a caller's box is reachable through a
#: short-unit spacer like a 0.34 nm/bp dsDNA one without the header's own
#: "pass n_max=X/m_max=X or larger" suggestion naming a value the caller
#: could never actually pass) — past this the enumeration is a search,
#: not a proposal.
_HARD_MAX = 200
#: Named cap on compositions one call scores (the backlog's 2 000). Also
#: the per-(switch, spacer) pair budget past which :func:`_band_m_values`
#: stops walking every ``m`` from 1 and narrows to the band around the
#: box's own span edges instead — otherwise a derived ``m_max`` in the
#: hundreds, crossed with several switches, would either blow the cap
#: enumerating small-``m`` rows nowhere near the box (never reaching the
#: actually feasible ones) or just explode.
_MAX_COMPOSITIONS = 2_000
#: Extra ``m`` values kept on each side of the span-feasible window when
#: :func:`_band_m_values` narrows — enough to still show a legible
#: nearest miss just outside the box's own edges.
_BAND_PAD = 2
_ALLOWED_KEYS = frozenset({"delta", "span", "swing", "n_max", "m_max", "conditions"})


# ── compose= parsing ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class ComposeBox:
    """The parsed requirement box — Decision 3's "box with interval
    constraints", whether it arrived as a literal ``compose={...}`` dict
    (:func:`parse_compose`) or read back off a block's declared
    transition ``requires`` (:func:`resolve_compose`)."""

    delta: WantSpec | None
    span: WantSpec | None
    swing: WantSpec | None = None
    n_max: int = _DEFAULT_N_MAX
    m_max: int = _DEFAULT_M_MAX
    #: Whether ``n_max``/``m_max`` were the caller's own explicit count
    #: (respected as given everywhere) rather than this dataclass's ready-
    #: to-use default — the linear chain family (:func:`enumerate_
    #: compositions`) derives its OWN, per-switch/per-spacer bound instead
    #: of the default when the flag is ``False`` (gr356739,
    #: :func:`_derive_bound`); the lever/series family (:func:`enumerate_
    #: levers`) is untouched by gr356739 and always reads ``n_max``/
    #: ``m_max`` directly, explicit or not.
    n_max_explicit: bool = True
    m_max_explicit: bool = True
    #: Filter (gr346735) applied to every per-unit fact read
    #: (:func:`resolve_unit`/:func:`_fact`) — same rule as
    #: ``wants[key]['conditions']`` (:func:`~precis_se.library.
    #: pick_material_row`), vetted by :func:`~precis_se.library.
    #: parse_conditions`.
    conditions: dict[str, Any] | None = None

    @property
    def specs(self) -> dict[str, WantSpec]:
        out: dict[str, WantSpec] = {}
        if self.delta is not None:
            out["delta"] = self.delta
        if self.span is not None:
            out["span"] = self.span
        if self.swing is not None:
            out["swing"] = self.swing
        return out


def parse_compose(compose: dict[str, Any]) -> ComposeBox:
    """Vet + canonicalise a dict ``compose=``. :class:`BadInput` for a
    non-dict, an unknown key, a box with none of ``delta``/``span``/
    ``swing``, ``delta`` and ``swing`` together (one stroke measure —
    R3, docs/backlog/port-rotation-and-lever-composition.md "Slice R3"),
    a non-numeric range, or an out-of-range count — never for what the
    library holds (that is scored and reported). The string form
    (``'<design>#<block>'``) is :func:`resolve_compose`, not this — it
    needs ``store`` to read a block's declared transition."""
    example = "compose={'delta': [10, 12], 'span': [40, 50]}"
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
    swing = _range(compose, "swing", "°")
    if delta is None and span is None and swing is None:
        raise BadInput(
            "compose= needs at least one of 'delta' (Å, port-to-port stroke), "
            "'span' (nm, long-state length), or 'swing' (°, total rotary angle)",
            next=example,
        )
    if delta is not None and swing is not None:
        raise BadInput(
            "compose= carries both 'delta' and 'swing' — one stroke measure: "
            "delta (tip) or swing (angle)",
            next="drop one of compose['delta']/compose['swing']",
        )
    conditions = None
    if "conditions" in compose:
        conditions = library.parse_conditions(
            "compose['conditions']", compose["conditions"]
        )
    return ComposeBox(
        delta=delta,
        span=span,
        swing=swing,
        n_max=_count(compose, "n_max", _DEFAULT_N_MAX, lo=1),
        m_max=_count(compose, "m_max", _DEFAULT_M_MAX, lo=0),
        n_max_explicit="n_max" in compose,
        m_max_explicit="m_max" in compose,
        conditions=conditions,
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


def parse_requires(requires: Any, *, opname: str) -> dict[str, Any]:
    """Vet + canonicalise a declared transition's ``requires=`` object
    (``declare_transitions``, Decision 3, port-pose-and-composition-
    search.md) — the one shared vetter so a box that fails here fails the
    same way ``compose=`` would: ``delta``/``span``/``n_max``/``m_max``/
    ``conditions`` run through the same :func:`_range`/:func:`_count`/
    :func:`~precis_se.library.parse_conditions` rules as ``compose=`` and
    land canonicalised (``[lo, hi]`` floats for ``delta``/``span``, a
    plain int for the counts, an unchanged dict for ``conditions``); any
    other key is a ``wants=`` entry, vetted (not reshaped) through
    :func:`~precis_se.library._parse_one_want`'s value shapes — a scalar,
    a ``[lo, hi]`` list, or ``{target, min, max, tol, weight}``.
    ``stimulus`` is refused: it is derived from the transition's own
    ``driver_kind`` at read time, never a declared target. An empty
    object and an absent key both mean "no requirement" — pass ``{}`` for
    either; a NON-empty one must name ``delta``, ``span`` or ``swing``
    (the same invariant :func:`parse_compose` enforces at read time,
    R3's ``swing`` added alongside — docs/backlog/port-rotation-and-
    lever-composition.md "Slice R3") — a ``wants=``-only box would
    otherwise fail later, at ``compose=``, with no block or transition
    named; ``delta`` and ``swing`` together fails here too, the same
    "one stroke measure" refusal. :class:`BadInput` always names
    ``opname`` (the caller's op, e.g. ``'declare_transitions'``), so a
    malformed box fails at write time the same way it would fail at
    ``compose=`` read time."""
    if not isinstance(requires, dict):
        raise BadInput(f"{opname}: requires must be a JSON object, got {requires!r}")
    if not requires:
        return {}
    if "stimulus" in requires:
        raise BadInput(
            f"{opname}: requires may not declare 'stimulus' — the stimulus "
            "IS the transition's driver_kind, read back automatically by "
            "compose='<slug>#<block>'"
        )
    if "delta" not in requires and "span" not in requires and "swing" not in requires:
        raise BadInput(
            f"{opname}: requires= needs at least one of 'delta' (Å), "
            "'span' (nm) or 'swing' (°); wants-only keys belong in search(wants=)"
        )
    out: dict[str, Any] = {}
    for key, unit in (("delta", "Å"), ("span", "nm"), ("swing", "°")):
        if key not in requires:
            continue
        try:
            spec = _range(requires, key, unit)
        except BadInput as exc:
            raise BadInput(f"{opname}: {exc.cause}", next=exc.next) from exc
        assert spec is not None
        lo = spec.min if spec.min is not None else spec.target
        hi = spec.max if spec.max is not None else spec.target
        out[key] = [float(lo), float(hi)]
    if "delta" in out and "swing" in out:
        raise BadInput(
            f"{opname}: requires carries both 'delta' and 'swing' — one "
            "stroke measure: delta (tip) or swing (angle)"
        )
    for key in ("n_max", "m_max"):
        if key not in requires:
            continue
        try:
            out[key] = _count(requires, key, 0, lo=1 if key == "n_max" else 0)
        except BadInput as exc:
            raise BadInput(f"{opname}: {exc.cause}", next=exc.next) from exc
    if "conditions" in requires:
        try:
            out["conditions"] = library.parse_conditions(
                "requires['conditions']", requires["conditions"]
            )
        except BadInput as exc:
            raise BadInput(f"{opname}: {exc.cause}", next=exc.next) from exc
    for key, value in requires.items():
        if key in _ALLOWED_KEYS:
            continue
        try:
            library._parse_one_want(key, value)
        except BadInput as exc:
            raise BadInput(f"{opname}: {exc.cause}", next=exc.next) from exc
        out[key] = value
    return out


def resolve_compose(store: Any, compose: str) -> tuple[ComposeBox, dict[str, Any], str]:
    """Read the requirement box off a block's declared transition —
    Decision 3's ``compose='<design>#<block>'`` /
    ``'<design>#<block>/<from>-><to>'`` string form.

    ``<design>`` is the design's slug (``se`` is slug-only, resolved
    exactly as ``get(kind='se', id=)`` does); the block resolves through
    :meth:`~precis_se.ops.SeTree.resolve_key` (name or ``#<uid>``). Among
    that block's transitions, the ones carrying a non-empty ``requires``
    are the addressable edges: exactly one → that box; none → a pointer
    at ``declare_transitions … requires=``; several without the
    ``/<from>-><to>`` segment → a list of the addressable edges; with the
    segment → that edge (:class:`BadInput` if it has no ``requires``).

    Returns ``(box, wants_from_requires, source_note)`` — the parsed
    :class:`ComposeBox`, a ``wants=`` overlay built from the requires'
    non-box keys plus a derived ``stimulus`` (the edge's ``driver_kind``),
    and a header line naming the source."""
    design_part, sep, rest = compose.partition("#")
    example = "compose='<design-slug>#<block>' or '<design-slug>#<block>/<from>-><to>'"
    if not sep or not design_part.strip() or not rest.strip():
        raise BadInput(f"compose={compose!r}: string form is {example}", next=example)
    slug = design_part.strip()
    block_token, _sep2, selector = rest.partition("/")
    block_token = block_token.strip()
    from_state: str | None = None
    to_state: str | None = None
    if selector:
        from_part, arrow, to_part = selector.partition("->")
        if not arrow or not from_part.strip() or not to_part.strip():
            raise BadInput(
                f"compose={compose!r}: the selector must be '<from>-><to>'",
                next=example,
            )
        from_state, to_state = from_part.strip(), to_part.strip()
    ref = store.get_ref(kind="se", id=slug)
    if ref is None:
        raise NotFound(f"se design {slug!r} not found")
    tree = persist.load_tree(store, ref.id)
    key = tree.resolve_key(block_token)
    if key is None:
        raise NotFound(f"se design {slug!r} has no block {block_token!r}")
    node = tree.blocks[key]
    assert node.uid is not None, "a saved design's blocks carry a uid"
    edges = [
        t for t in design_states.transitions_for(store, ref.id, node.uid) if t.requires
    ]
    if from_state is not None:
        edges = [
            t for t in edges if t.from_state == from_state and t.to_state == to_state
        ]
        if not edges:
            raise BadInput(
                f"compose={compose!r}: no transition {from_state!r}->{to_state!r} "
                f"on se:{slug}#{key} carries a requires= box",
                next=f"declare_transitions … requires={{'delta': [lo, hi]}} "
                f"on se:{slug}#{key}",
            )
    elif not edges:
        raise BadInput(
            f"compose={compose!r}: se:{slug}#{key} has no transition with a "
            "requires= box",
            next=f"declare_transitions … requires={{'delta': [lo, hi]}} "
            f"on se:{slug}#{key}",
        )
    elif len(edges) > 1:
        addressable = ", ".join(
            f"'{slug}#{key}/{t.from_state}->{t.to_state}'" for t in edges
        )
        raise BadInput(
            f"compose={compose!r}: se:{slug}#{key} has {len(edges)} "
            f"transitions with a requires= box — address one: {addressable}",
            next=f"compose='{slug}#{key}/{edges[0].from_state}->{edges[0].to_state}'",
        )
    transition = edges[0]
    box_payload = {k: v for k, v in transition.requires.items() if k in _ALLOWED_KEYS}
    box = parse_compose(box_payload)
    wants_from_requires = {
        k: v for k, v in transition.requires.items() if k not in _ALLOWED_KEYS
    }
    wants_from_requires["stimulus"] = transition.driver_kind
    source_note = (
        f"box from se:{slug}#{key} {transition.from_state}->{transition.to_state} "
        f"({transition.driver_kind})"
    )
    return box, wants_from_requires, source_note


def _merge_requires_wants(
    wants: Any, wants_from_requires: dict[str, Any]
) -> dict[str, Any]:
    """Layer a caller's explicit ``wants=`` over a resolved block's own
    ``requires`` (:func:`resolve_compose`). ``stimulus`` is derived from
    the edge's ``driver_kind`` — an explicit caller value silently wins;
    any other overlap is the block's declared requirement owning that
    key, same as a caller trying to override a ``compose=`` box key."""
    if wants is not None and not isinstance(wants, dict):
        raise BadInput(
            f"search(kind='se', wants=...) must be a JSON object, got {wants!r}",
            next="wants={'stimulus': 'light', 'bistable': True}",
        )
    caller = wants or {}
    clash = sorted(k for k in caller if k in wants_from_requires and k != "stimulus")
    if clash:
        raise BadInput(
            f"wants{clash} collides with requires{clash} — the block's "
            "requirement owns that key",
            next="drop it from wants= — the block's requires= already sets it",
        )
    merged = dict(wants_from_requires)
    merged.update(caller)
    return merged


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


def _fact(
    cand: _Candidate,
    key: str,
    cache: _ReadCache,
    *,
    conditions: dict[str, Any] | None = None,
) -> tuple[float, dict[str, Any]] | None:
    hit = library._resolve_star_value(cand, key, cache, conditions=conditions)
    if hit is None:
        return None
    row, _unit, _prov = hit
    value = library.value_row_number(row)
    return None if value is None else (value, row)


def _conditions(row: dict[str, Any] | None) -> str:
    return library.format_conditions((row or {}).get("conditions"))


def resolve_unit(
    cand: _Candidate, cache: _ReadCache, *, conditions: dict[str, Any] | None = None
) -> Unit:
    delta = _fact(cand, DELTA_KEY, cache, conditions=conditions)
    length = _fact(cand, LENGTH_KEY, cache, conditions=conditions)
    pss = _fact(cand, PSS_KEY, cache, conditions=conditions)
    half_life = _fact(cand, HALF_LIFE_KEY, cache, conditions=conditions)
    lp = _fact(cand, LP_KEY, cache, conditions=conditions)
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


# ── rotary facts (R3, docs/backlog/port-rotation-and-lever-composition.md
# "Slice R3") ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class RotaryUnit:
    """One library block resolved as a **rotary** — the lever family's
    analog of :class:`Unit`, and a SEPARATE role from
    :func:`resolve_unit`'s switch/spacer classification (a block can be
    both — the module's "keeps both roles" rule). ``angle_rad`` is
    whichever swing this row uses: R2's derived transition swing on
    ``port`` (:mod:`precis_se.kinematics`, the largest angle when several
    ports swing) when one exists, else a sourced ``step_angle``
    (:data:`precis_se.kinematics.STEP_ANGLE_KEY`) — :attr:`angle_source`
    names which. ``arm0_m``/``arm0_label`` are the corresponding lever
    arm: R2's own per-port envelope arm when derived, else half the
    block's envelope diagonal (no axis to project along), labelled
    accordingly. ``step_angle_rad``/``step_angle_source``/``disagree``
    carry the sourced fact independently of :attr:`angle_source` — the
    whole point when both exist and disagree (R3 acceptance criteria).
    ``length``/``half_life`` resolve through the star schema exactly like
    :class:`Unit`'s, for a swing series' summed span and the same
    bistability/τ½ verdict a switch's row shows."""

    cand: _Candidate
    port: str
    transition: str | None
    angle_rad: float
    angle_source: str  # 'derived' | 'sourced'
    axis: tuple[float, float, float] | None
    arm0_m: float | None
    arm0_label: str  # 'envelope' | 'envelope, no axis'
    step_angle_rad: float | None
    step_angle_source: str | None
    disagree: bool
    length: float | None
    half_life: float | None

    @property
    def handle(self) -> str:
        return f"{self.cand.design_slug}#{self.cand.block_name}"

    @property
    def roles(self) -> set[str]:
        roles: set[str] = set()
        for port in self.cand.node.ports.values():
            roles.update(port.roles)
        return roles


def _envelope_diag_half_m(tree: SeTree, node: SeBlock) -> float | None:
    """Half the block envelope's AABB diagonal — the fallback lever arm
    when a sourced ``step_angle`` has no derived axis to project along
    (R3: "arm₀ = half the envelope diagonal"). ``None`` for no/unparsable
    envelope, same as :func:`precis_se.kinematics._port_arm_m`'s own
    fallback — the caller shows ``—``/"unknown", never a fabricated
    number."""
    env = effective_envelope(tree, node)
    if not env:
        return None
    try:
        prim = cad_dsl.build_config(env)
    except cad_dsl.DslError:
        return None
    lo, hi = prim.aabb_local()
    if not (np.all(np.isfinite(lo)) and np.all(np.isfinite(hi))):
        return None
    diag = np.asarray(hi, dtype=np.float64) - np.asarray(lo, dtype=np.float64)
    return float(np.linalg.norm(diag)) / 2.0


def resolve_rotary(
    cand: _Candidate,
    cache: _ReadCache,
    derive_cache: dict[int, se_kinematics.DeriveResult],
) -> RotaryUnit | None:
    """``None`` when neither a derived swing nor a sourced ``step_angle``
    exists on ``cand``'s block — the caller (:func:`render_compose`)
    simply leaves it out of the rotary family, the same "skipped" shape
    :func:`resolve_unit`'s switch/spacer classification already has.

    ``derive_cache`` memoizes :func:`~precis_se.kinematics.derive` per
    TREE (keyed by ``id(cand.tree)`` — candidates from the same live
    design share one already-loaded tree object, :mod:`precis_se.
    library`'s ``_Candidate``), so a design with several blocks derives
    its kinematics once, not once per candidate row — the R3 spec's
    "reuse the candidate's already-loaded tree … do NOT reload"."""
    tree_key = id(cand.tree)
    result = derive_cache.get(tree_key)
    if result is None:
        result = se_kinematics.derive(cache.store, cand.tree, cand.ref_id)
        derive_cache[tree_key] = result
    best: se_kinematics.KinematicsRow | None = None
    for row in result.rows:
        if row.block != cand.block_name or row.axis is None:
            continue
        if best is None or abs(row.angle_rad) > abs(best.angle_rad):
            best = row
    length_hit = _fact(cand, LENGTH_KEY, cache)
    half_life_hit = _fact(cand, HALF_LIFE_KEY, cache)
    length = None if length_hit is None else length_hit[0]
    half_life = None if half_life_hit is None else half_life_hit[0]
    if best is not None:
        return RotaryUnit(
            cand=cand,
            port=best.port,
            transition=best.transition,
            angle_rad=best.angle_rad,
            angle_source="derived",
            axis=best.axis,
            arm0_m=best.arm_m,
            arm0_label="envelope",
            step_angle_rad=best.step_angle_rad,
            step_angle_source=best.step_angle_source,
            disagree=best.disagree,
            length=length,
            half_life=half_life,
        )
    step_hit = library._resolve_star_value(cand, se_kinematics.STEP_ANGLE_KEY, cache)
    if step_hit is None:
        return None
    value_row, _unit, provenance = step_hit
    step_angle_rad = library.value_row_number(value_row)
    if step_angle_rad is None or abs(step_angle_rad) < 1e-12:
        return None
    return RotaryUnit(
        cand=cand,
        port="",
        transition=None,
        angle_rad=abs(step_angle_rad),
        angle_source="sourced",
        axis=None,
        arm0_m=_envelope_diag_half_m(cand.tree, cand.node),
        arm0_label="envelope, no axis",
        step_angle_rad=step_angle_rad,
        step_angle_source=provenance,
        disagree=False,
        length=length,
        half_life=half_life,
    )


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
    #: The linear family's own name on the merged ranked list R3 grows
    #: (:class:`LeverComposition`'s ``'lever'``/``'series'`` sit beside
    #: this one) — a plain default so every existing chain row picks it
    #: up for free.
    family: str = "chain"

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


def _tau_suffix(unit: Unit | RotaryUnit) -> str:
    if unit.half_life is None:
        return ""
    return f", τ½ {_fmt_duration(unit.half_life)}"


def _bistable_note(unit: Unit | RotaryUnit, cache: _ReadCache) -> str:
    """The bistability + τ½ verdict — shared by a linear chain's switch
    (:func:`score_compositions`) and a lever/series row's rotary unit
    (:func:`score_levers`, R3's "PSS/bistability/τ½ verdicts exactly like
    linear rows" requirement): the same read (:func:`~precis_se.library.
    _eval_bistable`) off whichever block owns the row's own states, so the
    two families can never drift apart."""
    verdict = library._eval_bistable(unit.cand, WantSpec(target=True), cache)
    mark = "✓" if verdict.matched else "✗"
    return f"bistable {mark} ({verdict.actual}{_tau_suffix(unit)})"


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


def _derive_bound(
    spec: WantSpec | None,
    default: int,
    explicit: bool,
    unit_value: float | None,
) -> tuple[int, bool]:
    """One switch's/spacer's own effective ``n_max``/``m_max`` — ``(bound,
    at_ceiling)`` (gr356739). The caller's explicit count (``explicit``)
    or the box's ready-to-use default (``default`` — used verbatim when
    there is nothing to derive from: no box key, or this unit carries no
    matching length fact) pass straight through with ``at_ceiling=False``;
    otherwise ``ceil(spec's hi / unit_value)`` clamped to :data:`_HARD_MAX`
    — the same ceiling :func:`_count` enforces on an explicit value, so a
    header's "pass n_max=X" suggestion (:func:`_span_reachability_note`/
    :func:`_delta_reachability_note`) always names something the caller
    could actually pass."""
    if explicit or spec is None or unit_value is None or unit_value <= 0:
        return default, False
    hi = spec.max if spec.max is not None else spec.target
    if hi is None or hi <= 0:
        return default, False
    derived = math.ceil(hi / unit_value)
    if derived >= _HARD_MAX:
        return _HARD_MAX, True
    return max(1, derived), False


def _switch_n_bound(switch: Unit, box: ComposeBox) -> tuple[int, bool]:
    """``switch``'s own effective ``n_max`` — derived from ``box.delta``
    (gr356739) unless the caller gave an explicit ``n_max``. Derives off
    ``delta_eff`` — ``switch.delta`` scaled by ``switch.pss`` when known,
    the SAME quantity :func:`_delta_attr`/:func:`_delta_reachability_note`
    score/suggest against — never the unscaled ideal, which would
    undercount ``n_max`` for any switch with ``pss < 1`` (a low-PSS
    switch needs MORE n to clear the box, not fewer)."""
    pss = switch.pss if switch.pss is not None else 1.0
    per_n = None if switch.delta is None else switch.delta * pss
    return _derive_bound(box.delta, box.n_max, box.n_max_explicit, per_n)


def _spacer_m_bound(spacer: Unit, box: ComposeBox) -> tuple[int, bool]:
    """``spacer``'s own effective ``m_max`` — derived from ``box.span``
    (gr356739) unless the caller gave an explicit ``m_max``."""
    return _derive_bound(box.span, box.m_max, box.m_max_explicit, spacer.length)


def _band_m_values(
    n: int, switch: Unit, spacer: Unit, box: ComposeBox, m_max: int
) -> list[int]:
    """The ``m`` values to try for one ``(switch, n, spacer)`` triple when
    the pair's full ``n_max × m_max`` grid blew past :data:`_MAX_
    COMPOSITIONS` (gr356739) — the band around the box's own span edges
    (``[span_lo, span_hi] / spacer.unit_length``, padded by :data:`_BAND_
    PAD` for a legible nearest miss just outside them) rather than every
    ``m`` from 1. Always includes ``m_max`` itself, so the true maximum
    reachable span for this pair stays in the enumerated set even when
    narrowed — :func:`_span_reachability_note`'s "max X nm" figure reads
    it straight off the enumerated rows, never a value recomputed (and
    possibly drifted) separately."""
    assert (
        box.span is not None and switch.length is not None and spacer.length is not None
    )
    lo_edge = box.span.min if box.span.min is not None else box.span.target
    hi_edge = box.span.max if box.span.max is not None else box.span.target
    residual_lo = None if lo_edge is None else lo_edge - n * switch.length
    residual_hi = None if hi_edge is None else hi_edge - n * switch.length
    lo_m = (
        1
        if residual_lo is None
        else math.floor(residual_lo / spacer.length) - _BAND_PAD
    )
    hi_m = (
        m_max
        if residual_hi is None
        else math.ceil(residual_hi / spacer.length) + _BAND_PAD
    )
    lo_m = max(1, lo_m)
    hi_m = min(m_max, max(lo_m, hi_m))
    values = list(range(lo_m, hi_m + 1))
    if m_max not in values:
        values.append(m_max)
    return values


def enumerate_compositions(
    switches: list[Unit],
    spacers: list[Unit],
    box: ComposeBox,
    *,
    cap: int = _MAX_COMPOSITIONS,
) -> tuple[list[Composition], bool]:
    """Every (switch, n, spacer, m) up to the box's counts — ``(rows,
    capped)``. ``n_max``/``m_max`` are each switch's/spacer's OWN
    effective bound (:func:`_switch_n_bound`/:func:`_spacer_m_bound`,
    gr356739) — the caller's explicit count, or one derived from the box.
    Attrs/score are filled by :func:`score_compositions`."""
    out: list[Composition] = []
    for switch in switches:
        delta_s = switch.delta
        if delta_s is None:  # not a switch — caller's classification slipped
            continue
        n_max, _n_ceiling = _switch_n_bound(switch, box)
        for spacer in [None, *spacers]:
            if spacer is None:
                m_max = 0
                narrow = False
            else:
                m_max, _m_ceiling = _spacer_m_bound(spacer, box)
                narrow = (
                    n_max * m_max > _MAX_COMPOSITIONS
                    and box.span is not None
                    and switch.length is not None
                    and spacer.length is not None
                )
            for n in range(1, n_max + 1):
                delta_ideal = n * delta_s
                delta_eff = (
                    delta_ideal * switch.pss if switch.pss is not None else delta_ideal
                )
                if spacer is None:
                    m_values: list[int] | range = range(1)
                elif narrow:
                    m_values = _band_m_values(n, switch, spacer, box, m_max)
                else:
                    m_values = range(1, m_max + 1)
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


@dataclass
class LeverComposition:
    """One R3 lever-family row: a single rotary unit alone or paired with
    ``k`` arm units at its rotating port (``family='lever'``, ``n`` fixed
    at 1, scored on tip stroke — :attr:`tip_m`), or ``n`` copies of the
    SAME rotary unit chained with no arms (``family='series'``, ``k``
    fixed at 0, scored on total swing — :attr:`angle_total_rad`) — never
    both shapes on the same row, mirroring :class:`Composition`'s
    switch/spacer shape one level up (docs/backlog/port-rotation-and-
    lever-composition.md "Slice R3")."""

    rotary: RotaryUnit
    n: int
    arm: Unit | None
    k: int
    family: str
    angle_total_rad: float
    tip_m: float | None
    delta_ideal: float | None
    delta_eff: float | None
    span: float | None
    attrs: dict[str, AttrResult]
    score: float
    max_score: float
    notes: list[str] = field(default_factory=list)
    sum_distance: float = 0.0
    on_frontier: bool = False

    @property
    def handle(self) -> str:
        if self.family == "series":
            return f"{self.n} × {self.rotary.handle}"
        label = self.rotary.handle
        if self.arm is not None and self.k:
            label += f" + {self.k} × {self.arm.handle}"
        return label


def enumerate_levers(
    rotaries: list[RotaryUnit],
    spacers: list[Unit],
    box: ComposeBox,
    *,
    cap: int = _MAX_COMPOSITIONS,
) -> tuple[list[LeverComposition], bool, int]:
    """Every lever (``box.swing`` absent, one rotary + ``k`` arm units,
    ``k`` from 0 through ``box.m_max``) or swing series (``box.swing``
    present, ``n`` copies of one rotary from 1 through ``box.n_max``) up
    to the box's counts — ``(rows, capped, skipped)``, :func:`enumerate_
    compositions`'s shape (``skipped`` mirrors its ``counts['skipped']``:
    a rotary with no usable envelope offers no lever arm and is left out
    of the lever family, counted rather than silently dropped — the
    swing family needs no envelope, so it never skips here). Empty when
    the box carries neither ``delta`` nor ``swing`` (a span-only box
    scores the linear family alone — R3 only grows a lever/series row
    off one of its own two trigger keys). Attrs/score are filled by
    :func:`score_levers`."""
    out: list[LeverComposition] = []
    if box.swing is not None:
        for rotary in rotaries:
            for n in range(1, box.n_max + 1):
                angle_total = n * rotary.angle_rad
                span = None if rotary.length is None else n * rotary.length
                out.append(
                    LeverComposition(
                        rotary=rotary,
                        n=n,
                        arm=None,
                        k=0,
                        family="series",
                        angle_total_rad=angle_total,
                        tip_m=None,
                        delta_ideal=None,
                        delta_eff=None,
                        span=span,
                        attrs={},
                        score=0.0,
                        max_score=0.0,
                    )
                )
                if len(out) >= cap:
                    return out, True, 0
    elif box.delta is not None:
        skipped = 0
        for rotary in rotaries:
            if rotary.arm0_m is None:
                skipped += 1
                continue  # no envelope — nothing to lever, never a guess
            for arm in [None, *spacers]:
                k_values = range(1, box.m_max + 1) if arm is not None else range(1)
                for k in k_values:
                    k_eff = 0 if arm is None else k
                    arm_len_m = (
                        0.0 if arm is None or arm.length is None else arm.length * 1e-9
                    )
                    arm_total_m = rotary.arm0_m + k_eff * arm_len_m
                    tip_m = 2.0 * arm_total_m * math.sin(rotary.angle_rad / 2.0)
                    tip_a = tip_m * _M_TO_A  # the one sanctioned Å crossing
                    out.append(
                        LeverComposition(
                            rotary=rotary,
                            n=1,
                            arm=arm,
                            k=k_eff,
                            family="lever",
                            angle_total_rad=rotary.angle_rad,
                            tip_m=tip_m,
                            delta_ideal=tip_a,
                            delta_eff=tip_a,
                            # R3 review decision: a lever's own span is its
                            # arm reach (nm) — :func:`_lever_span_attr`.
                            span=arm_total_m * 1e9,
                            attrs={},
                            score=0.0,
                            max_score=0.0,
                        )
                    )
                    if len(out) >= cap:
                        return out, True, skipped
        return out, False, skipped
    return out, False, 0


def _numeric_keys(box: ComposeBox, want_specs: dict[str, WantSpec]) -> list[str]:
    """The one list of keys :func:`~precis_se.library.order_rows`'s
    Pareto tie-break runs over — box keys (``delta``/``span``/R3's
    ``swing``) plus any numeric ``wants=`` key — shared by
    :func:`score_compositions` and :func:`score_levers` so a merged
    chain+lever ranked list (:func:`render_compose`) sorts on the SAME
    axes regardless of which family a row came from."""
    return list(box.specs) + [
        k
        for k, s in want_specs.items()
        if k not in library._BUILTIN_KEYS and s.is_numeric
    ]


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
    numeric_keys = _numeric_keys(box, want_specs)
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
            comp.notes.append(_bistable_note(comp.switch, cache))
        for note in (_stiffness_note(comp, units[LENGTH_KEY]), _joining_note(comp)):
            if note:
                comp.notes.append(note)
        comp.attrs = attrs
        comp.score = sum(a.weight for a in attrs.values() if a.matched)
        comp.max_score = max_score
        comp.sum_distance = library.sum_distances(attrs, numeric_keys)
    return library.order_rows(comps, numeric_keys)


def _lever_delta_attr(comp: LeverComposition, spec: WantSpec, unit: str) -> AttrResult:
    assert comp.delta_eff is not None and comp.delta_ideal is not None
    matched, _display, distance = library._match_value_row(
        {"value_num": comp.delta_eff}, spec
    )
    return AttrResult(
        matched=matched,
        actual=f"{comp.delta_ideal:g} {unit}",
        weight=spec.weight,
        distance=distance,
    )


def _lever_span_attr(comp: LeverComposition, spec: WantSpec, unit: str) -> AttrResult:
    """``box.span`` on a lever row is its own reach — ``arm0 +
    k·unit_length`` (:func:`enumerate_levers` computes it in nm already,
    R3 review decision: a lever's ``span`` IS its arm reach, scored
    through the SAME :func:`~precis_se.library._match_value_row` band/
    tolerance rules a chain row's ``span`` uses, labelled "(arm reach)"
    so it is never mistaken for a switch chain's long-state length). A
    series row's ``span`` is its rotary units' own summed
    ``unit_length`` instead (R3: "span … scores the series' summed
    unit_length as before") — both shapes land in :attr:`LeverComposition
    .span` already, so this is one function; ``None`` (no length fact on
    the block) is an honest miss, never a guess."""
    if comp.span is None:
        return AttrResult(
            matched=False,
            actual=f"unknown (no {LENGTH_KEY} row on {comp.rotary.handle})",
            weight=spec.weight,
            distance=float("inf"),
        )
    matched, _display, distance = library._match_value_row(
        {"value_num": comp.span}, spec
    )
    suffix = " (arm reach)" if comp.family == "lever" else ""
    return AttrResult(
        matched=matched,
        actual=f"{comp.span:g} {unit}{suffix}",
        weight=spec.weight,
        distance=distance,
    )


def _lever_arm_note(comp: LeverComposition) -> str:
    rotary = comp.rotary
    if rotary.arm0_m is None:
        arm_str = f"arm unknown ({rotary.handle}: no envelope)"
    else:
        arm_str = f"arm {rotary.arm0_m * 1e9:g} nm ({rotary.arm0_label})"
    if comp.arm is not None and comp.k:
        length = "unknown" if comp.arm.length is None else f"{comp.arm.length:g} nm"
        arm_str += f" + {comp.k} × spacer {length} ({comp.arm.handle})"
    return arm_str


def _angle_origin_note(rotary: RotaryUnit) -> str:
    """The angle a lever/series row used, and where it came from — R3's
    "the angle used and its origin (derived vs sourced)"; when a sourced
    ``step_angle`` ALSO exists it always shows too, agreeing or
    disagreeing (R3 acceptance: "a rotary unit with a sourced step_angle
    disagreeing with its derived angle shows both")."""
    angle_deg = math.degrees(rotary.angle_rad)
    if rotary.angle_source == "derived":
        origin = f"derived from {rotary.transition} on port {rotary.port}"
    else:
        prov = f" {rotary.step_angle_source}" if rotary.step_angle_source else ""
        origin = f"sourced step_angle{prov}"
    note = f"angle {angle_deg:g}° ({origin})"
    if rotary.angle_source == "derived" and rotary.step_angle_rad is not None:
        step_deg = math.degrees(rotary.step_angle_rad)
        verdict = "disagrees" if rotary.disagree else "agrees"
        note += f"; sourced step_angle {step_deg:g}° {verdict}"
    return note


def score_levers(
    store: Any,
    comps: list[LeverComposition],
    box: ComposeBox,
    want_specs: dict[str, WantSpec],
    cache: _ReadCache,
    *,
    delta_unit: str,
    span_unit: str,
) -> list[LeverComposition]:
    """Score every lever/series row exactly as :func:`score_compositions`
    scores the linear family (``wants=`` keys on the rotary block,
    mirroring "wants= keys score on the switch block"), stamp the must-
    surface notes (arm breakdown, angle + origin, bistable/τ½ — R3's "PSS
    /bistability/τ½ verdicts exactly like linear rows, same helpers" —
    PSS has no lever analog: the tip-stroke formula carries no PSS term,
    so only bistability/τ½ apply here). NOT ordered here — :func:`render_
    compose` merges chain and lever rows before the one :func:`~precis_se
    .library.order_rows` call, so the two families are ranked on the
    SAME pass, never sorted twice."""
    numeric_keys = _numeric_keys(box, want_specs)
    rotary_attrs: dict[str, dict[str, AttrResult]] = {}
    for comp in comps:
        attrs: dict[str, AttrResult] = {}
        if comp.family == "lever" and box.delta is not None:
            attrs["delta"] = _lever_delta_attr(comp, box.delta, delta_unit)
        if comp.family == "series" and box.swing is not None:
            total_deg = math.degrees(comp.angle_total_rad)
            matched, _display, distance = library._match_value_row(
                {"value_num": total_deg}, box.swing
            )
            attrs["swing"] = AttrResult(
                matched=matched,
                actual=f"{total_deg:g}°",
                weight=box.swing.weight,
                distance=distance,
            )
        if box.span is not None:
            attrs["span"] = _lever_span_attr(comp, box.span, span_unit)
        if want_specs:
            key = comp.rotary.handle
            if key not in rotary_attrs:
                rotary_attrs[key] = library.resolve_block_attrs(
                    store, comp.rotary.cand, want_specs, cache
                )
            attrs.update(rotary_attrs[key])
        if "bistable" in attrs:
            verdict = attrs["bistable"]
            attrs["bistable"] = AttrResult(
                matched=verdict.matched,
                actual=verdict.actual + _tau_suffix(comp.rotary),
                weight=verdict.weight,
                distance=verdict.distance,
            )
            bistable_note = None
        else:
            bistable_note = _bistable_note(comp.rotary, cache)
        comp.attrs = attrs
        comp.score = sum(a.weight for a in attrs.values() if a.matched)
        comp.max_score = sum(s.weight for s in box.specs.values()) + sum(
            s.weight for s in want_specs.values()
        )
        comp.sum_distance = library.sum_distances(attrs, numeric_keys)
        notes = [_lever_arm_note(comp), _angle_origin_note(comp.rotary)]
        if bistable_note:
            notes.append(bistable_note)
        join_note = _lever_joining_note(comp)
        if join_note:
            notes.append(join_note)
        notes.append(f"family: {comp.family}")
        comp.notes = notes
    return comps


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


def _port_named(cand: _Candidate, role: str) -> str:
    for name, port in cand.node.ports.items():
        if role in port.roles:
            return name
    return "<port>"


def _port_pair(a: Unit | RotaryUnit, b: Unit | RotaryUnit) -> tuple[str, str]:
    pair = _complementary_pair(a.roles, b.roles)
    if pair is None:
        return "<port>", "<port>"
    return _port_named(a.cand, pair[0]), _port_named(b.cand, pair[1])


def ops_script(comp: Composition | LeverComposition) -> list[dict[str, Any]]:
    """The ``instance_block`` × n + spacer/arm ops that realise one row,
    joining consecutive units through complementary ports — dispatches on
    the row's family (R3 adds :class:`LeverComposition` beside the
    original :class:`Composition`; :func:`_lever_ops_script` is its own
    shape, "connect from the ROTATING port")."""
    if isinstance(comp, LeverComposition):
        return _lever_ops_script(comp)
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


def _lever_chain(comp: LeverComposition) -> list[tuple[str, RotaryUnit | Unit]]:
    """Instance names in chain order: the rotary unit (``n`` copies for a
    ``'series'`` row, one for a ``'lever'`` row) then the ``k`` arm units,
    if any — mirrors :func:`_chain`'s shape one level up."""
    chain: list[tuple[str, RotaryUnit | Unit]] = [
        (f"r{i + 1}", comp.rotary) for i in range(comp.n)
    ]
    if comp.arm is not None:
        chain += [(f"a{i + 1}", comp.arm) for i in range(comp.k)]
    return chain


def _rotating_port(rotary: RotaryUnit) -> tuple[str, set[str]]:
    """``(port name, its roles)`` for the rotary unit's own named ROTATING
    port (R2's derived-swing port, when known); falls back to the unit's
    whole role set (like :func:`_port_pair`) when the row's angle came
    off a sourced ``step_angle`` with no derived port to name."""
    if rotary.port and rotary.port in rotary.cand.node.ports:
        return rotary.port, set(rotary.cand.node.ports[rotary.port].roles)
    return rotary.port or "(unnamed)", rotary.roles


def _rotating_port_pair(rotary: RotaryUnit, other: Unit) -> tuple[str, str] | None:
    """The rotary unit's own named ROTATING port paired with ``other``'s
    complementary port — R3's "connect from the ROTATING port to the
    first arm's complementary port", never just any port the rotary unit
    happens to carry a matching role on. ``None`` — never a ``'<port>'``
    placeholder — when the rotating port has no role complementary to
    ``other``'s: :func:`_lever_ops_script` skips the connect and
    :func:`_lever_joining_note` says so on the row, the same "no
    complementary ports" honesty :func:`_joining_note` already gives the
    linear family."""
    port_name, roles = _rotating_port(rotary)
    pair = _complementary_pair(roles, other.roles)
    if pair is None:
        return None
    rotary_port = (
        port_name
        if port_name in rotary.cand.node.ports
        else _port_named(rotary.cand, pair[0])
    )
    return rotary_port, _port_named(other.cand, pair[1])


def _lever_joining_note(comp: LeverComposition) -> str | None:
    """The rotating-port↔arm complementarity — :func:`_joining_note`'s
    switch↔spacer check, one level up for the lever family. ``None`` for
    a bare rotary (no arm, nothing to join) or a swing series (chained
    through the generic :func:`_port_pair`, same as a switch↔switch
    chain — no ROTATING-port concept there)."""
    if comp.family != "lever" or comp.arm is None:
        return None
    port_name, roles = _rotating_port(comp.rotary)
    pair = _complementary_pair(roles, comp.arm.roles)
    if pair is not None:
        return f"joining rotating port↔arm: {pair[0]}↔{pair[1]}{_joining_name(*pair)}"
    return (
        f"joining: none (rotating port {port_name} has no role "
        f"complementary to {comp.arm.handle}'s ports)"
    )


def _lever_ops_script(comp: LeverComposition) -> list[dict[str, Any]]:
    """The ``instance_block`` × (1 or n) rotary + × k arm ops, connecting
    the rotary's ROTATING port to the first arm's complementary port and
    every following pair through :func:`_port_pair` (arm↔arm, or
    rotary↔rotary for a ``'series'`` row — the same generic role match
    the linear family's switch↔switch chaining already uses). The
    ROTATING-port connect is skipped — never a ``'<port>'`` placeholder —
    when there is no complementary role, replaced by an op-free comment
    entry (:func:`_lever_joining_note` says the same thing on the row)."""
    chain = _lever_chain(comp)
    ops: list[dict[str, Any]] = [
        {"op": "instance_block", "name": name, "template": unit.handle}
        for name, unit in chain
    ]
    for idx, ((a_name, a_unit), (b_name, b_unit)) in enumerate(
        itertools.pairwise(chain)
    ):
        if idx == 0 and comp.family == "lever" and isinstance(a_unit, RotaryUnit):
            assert isinstance(b_unit, Unit)
            pair = _rotating_port_pair(a_unit, b_unit)
            if pair is None:
                ops.append(
                    {
                        "note": (
                            f"no connect: rotating port {a_unit.port or '(unnamed)'} "
                            f"on {a_unit.handle} has no role complementary to "
                            f"{b_unit.handle}'s ports"
                        )
                    }
                )
                continue
            a_port, b_port = pair
        else:
            a_port, b_port = _port_pair(a_unit, b_unit)
        ops.append(
            {"op": "connect", "a": f"{a_name}.{a_port}", "b": f"{b_name}.{b_port}"}
        )
    return ops


def _reachability_note(
    key: str,
    spec: WantSpec,
    unit: str,
    best_value: float | None,
    handle: str | None,
    bound_name: str,
    bound_value: int,
    at_ceiling: bool,
    needed: int | None,
) -> str | None:
    """One header line — ``None`` when ``best_value`` already reaches the
    box's own lower edge. Otherwise names the bound (``bound_name=
    bound_value``), the best ``key`` actually reached and by whom, and
    either the value that would reach the box (``needed``) or, when the
    bound derived (:func:`_derive_bound`) is already :data:`_HARD_MAX`,
    says so instead — a "pass n_max=X" naming a value past the ceiling
    would be a suggestion the caller could never carry out (gr356739)."""
    lo = spec.min if spec.min is not None else spec.target
    if lo is None or best_value is None or best_value >= lo:
        return None
    best_str = f"max {best_value:g} {unit}"
    if handle is not None:
        best_str += f" with {handle}"
    if at_ceiling:
        return (
            f"{key} unreachable at {bound_name}={bound_value} (hard ceiling; "
            f"{best_str}) — no {bound_name} within the {_HARD_MAX} hard "
            f"ceiling reaches the box's {key}"
        )
    if needed is None:
        return f"{key} unreachable at {bound_name}={bound_value} ({best_str})"
    return (
        f"{key} unreachable at {bound_name}={bound_value} ({best_str}) — "
        f"pass {bound_name}={needed} or larger"
    )


def _span_reachability_note(
    comps: list[Composition], box: ComposeBox, unit: str
) -> str | None:
    """Header honesty for ``span`` (gr356739): the best ``span`` any
    enumerated chain row actually reaches (:func:`enumerate_compositions`
    always keeps each pair's own maximum in the set — see :func:`_band_m_
    values`) versus the box's own lower edge. Names ``m_max`` + the spacer
    when the winning chain uses one (the derived-bound key, gr356739);
    falls back to ``n_max`` + the switch for a spacer-free chain, where
    only the switch count grows the span."""
    if box.span is None:
        return None
    reachable = [c for c in comps if c.span is not None]
    if not reachable:
        return None
    best = max(reachable, key=lambda c: c.span if c.span is not None else float("-inf"))
    assert best.span is not None
    lo = box.span.min if box.span.min is not None else box.span.target
    if lo is None or best.span >= lo:
        return None
    if best.spacer is not None and best.spacer.length is not None:
        bound_value, at_ceiling = _spacer_m_bound(best.spacer, box)
        bound_name, handle = "m_max", best.spacer.handle
        needed = None
        if not at_ceiling and best.switch.length is not None:
            residual = lo - best.n * best.switch.length
            needed_m = max(1, math.ceil(residual / best.spacer.length))
            needed = needed_m if needed_m <= _HARD_MAX else None
    elif best.switch.length is not None:
        bound_value, at_ceiling = _switch_n_bound(best.switch, box)
        bound_name, handle = "n_max", best.switch.handle
        needed_n = max(1, math.ceil(lo / best.switch.length))
        needed = needed_n if (not at_ceiling and needed_n <= _HARD_MAX) else None
    else:
        return None
    return _reachability_note(
        "span",
        box.span,
        unit,
        best.span,
        handle,
        bound_name,
        bound_value,
        at_ceiling,
        needed,
    )


def _delta_reachability_note(
    comps: list[Composition], box: ComposeBox, unit: str
) -> str | None:
    """Header honesty for ``delta`` (gr356739's "same for n_max vs delta
    when a switch's delta_length is small") — mirrors :func:`_span_
    reachability_note` one key over: ``n_max`` is always the bound here
    (only a switch's own count grows ``delta``, never a spacer's)."""
    if box.delta is None or not comps:
        return None
    best = max(comps, key=lambda c: c.delta_eff)
    lo = box.delta.min if box.delta.min is not None else box.delta.target
    if lo is None or best.delta_eff >= lo:
        return None
    bound_value, at_ceiling = _switch_n_bound(best.switch, box)
    needed = None
    if not at_ceiling:
        pss = best.switch.pss if best.switch.pss is not None else 1.0
        if pss > 0 and best.switch.delta is not None and best.switch.delta > 0:
            needed_n = max(1, math.ceil((lo / pss) / best.switch.delta))
            needed = needed_n if needed_n <= _HARD_MAX else None
    return _reachability_note(
        "delta",
        box.delta,
        unit,
        best.delta_eff,
        best.switch.handle,
        "n_max",
        bound_value,
        at_ceiling,
        needed,
    )


def render_compositions(
    rows: list[Composition | LeverComposition],
    box: ComposeBox,
    want_specs: dict[str, WantSpec],
    *,
    compose_repr: Any,
    wants_repr: Any,
    counts: dict[str, int],
    unknown: list[str],
    capped: bool,
    narrow_note: str = "",
    source_note: str = "",
    bound_notes: list[str] | None = None,
    page_size: int = 20,
) -> str:
    header = f"# {len(rows)} composition(s) ranked for compose={compose_repr!r}"
    if want_specs:
        header += f" wants={wants_repr!r}"
    if narrow_note:
        header += f"  {narrow_note}"
    lines = [header]
    if source_note:
        lines.append(source_note)
    facts = (
        f"{counts['switches']} switch(es) × {counts['spacers']} spacer(s) from "
        f"{counts['blocks']} library block(s); {counts['rotary']} rotary unit(s)"
    )
    if counts.get("rotary_skipped"):
        facts += f" ({counts['rotary_skipped']} skipped, no envelope)"
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
    for note in bound_notes or []:
        lines.append(note)
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
    :func:`precis_se.library.render_search` dispatches to. ``compose`` is
    a dict box or Decision 3's string form (``'<design>#<block>'`` /
    ``'<design>#<block>/<from>-><to>'``, :func:`resolve_compose`), which
    reads the box off a block's declared transition and layers its own
    ``requires`` under ``wants=`` (explicit ``wants=`` wins on the
    derived ``stimulus`` key; any other overlap is the block's
    requirement owning that key)."""
    source_note = ""
    if isinstance(compose, str):
        box, wants_from_requires, source_note = resolve_compose(store, compose)
        wants = _merge_requires_wants(wants, wants_from_requires)
    else:
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
    rotaries: list[RotaryUnit] = []
    skipped = 0
    derive_cache: dict[int, se_kinematics.DeriveResult] = {}
    for cand in candidates:
        unit = resolve_unit(cand, cache, conditions=box.conditions)
        if unit.is_switch:
            switches.append(unit)
        elif unit.is_spacer:
            spacers.append(unit)
        else:
            skipped += 1
        # A block keeps BOTH roles when it earns them — the rotary walk
        # is independent of the switch/spacer classification above (R3:
        # "a block that is both linear switch and rotary keeps both
        # roles").
        rotary = resolve_rotary(cand, cache, derive_cache)
        if rotary is not None:
            rotaries.append(rotary)
    counts: dict[str, int] = {
        "blocks": len(candidates),
        "switches": len(switches),
        "spacers": len(spacers),
        "skipped": skipped,
        "rotary": len(rotaries),
        "rotary_skipped": 0,
    }
    units = {key: unit_label(cache, key) for key in (DELTA_KEY, LENGTH_KEY)}
    numeric_keys = _numeric_keys(box, want_specs)
    unknown = library.unknown_keys(store, want_specs, cache) if want_specs else []

    if box.swing is not None:
        # swing box: rotary units and series of them only — a switch's
        # chain has no angle to rank on (R3: "with swing in the box…").
        if not rotaries:
            return (
                f"no library block derives a swing or carries a "
                f"{se_kinematics.STEP_ANGLE_KEY} row — nothing to compose "
                f"({counts['blocks']} block(s) inspected)"
                f"{'  ' + narrow_note if narrow_note else ''}\n\n"
                "Next: declare_states/declare_transitions with a "
                "port_pose_overrides rot delta on a posed port, or "
                f"put(kind='material', id='<mat>', "
                f"property='{se_kinematics.STEP_ANGLE_KEY}', value=<rad>, "
                "unit='rad') and link the block made-of it"
            )
        levers, capped, counts["rotary_skipped"] = enumerate_levers(
            rotaries, spacers, box
        )
        levers = score_levers(
            store,
            levers,
            box,
            want_specs,
            cache,
            delta_unit=units[DELTA_KEY],
            span_unit=units[LENGTH_KEY],
        )
        ordered_levers = library.order_rows(levers, numeric_keys)
        rows: list[Composition | LeverComposition] = list(ordered_levers)
        bound_notes: list[str] = []
    else:
        # A rotary-only library only has something to rank when this box
        # will actually grow a lever row off it (``box.delta`` — R3); a
        # span-only box with no switch stays the pre-R3 refusal, never a
        # silently empty "0 composition(s)" list.
        if not switches and not (rotaries and box.delta is not None):
            return (
                f"no library block carries a {DELTA_KEY} row — nothing to compose "
                f"({counts['blocks']} block(s) inspected, {counts['spacers']} "
                "spacer(s))"
                f"{'  ' + narrow_note if narrow_note else ''}\n\n"
                f"Next: put(kind='material', id='<mat>', property='{DELTA_KEY}', "
                "value=<Å>, unit='Å') and link the switch's design or component "
                "made-of it"
            )
        comps, chain_capped = enumerate_compositions(switches, spacers, box)
        chain_rows = score_compositions(
            store, comps, box, want_specs, cache, units=units
        )
        # Header honesty (gr356739): say when the effective n_max/m_max
        # still can't reach the box's own lower edge, off the FULL
        # enumerated set — before it gets merged with any lever rows or
        # paged down to page_size below.
        bound_notes = [
            note
            for note in (
                _span_reachability_note(comps, box, units[LENGTH_KEY]),
                _delta_reachability_note(comps, box, units[DELTA_KEY]),
            )
            if note is not None
        ]
        lever_rows: list[LeverComposition] = []
        lever_capped = False
        if box.delta is not None and rotaries:
            lever_rows, lever_capped, counts["rotary_skipped"] = enumerate_levers(
                rotaries, spacers, box
            )
            lever_rows = score_levers(
                store,
                lever_rows,
                box,
                want_specs,
                cache,
                delta_unit=units[DELTA_KEY],
                span_unit=units[LENGTH_KEY],
            )
        capped = chain_capped or lever_capped
        if lever_rows:
            # Only label the family when there is a real choice between
            # the two on this list — R3: "family named on each row: lever
            # / chain", in the merged-ranking scenario the wording is
            # about.
            for c in chain_rows:
                c.notes.append(f"family: {c.family}")
            combined: list[Composition | LeverComposition] = [*chain_rows, *lever_rows]
            rows = list(library.order_rows(combined, numeric_keys))
        else:
            rows = list(chain_rows)

    return render_compositions(
        rows,
        box,
        want_specs,
        compose_repr=compose,
        wants_repr=wants,
        counts=counts,
        unknown=unknown,
        capped=capped,
        narrow_note=narrow_note,
        source_note=source_note,
        bound_notes=bound_notes,
        page_size=page_size,
    )


__all__ = [
    "ComposeBox",
    "Composition",
    "LeverComposition",
    "RotaryUnit",
    "Unit",
    "enumerate_compositions",
    "enumerate_levers",
    "ops_script",
    "parse_compose",
    "parse_requires",
    "render_compose",
    "render_compositions",
    "resolve_compose",
    "resolve_rotary",
    "resolve_unit",
    "score_compositions",
    "score_levers",
]
