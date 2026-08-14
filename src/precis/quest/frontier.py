"""Quest Pareto frontier — the non-dominated candidate materials.

Slice 4b of the quest layer (``quest-layer`` (git-only) §Materials are
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

A quest may additionally declare a **composite** objective — a weighted sum of
other measures, human-set at seed time (the potential-lever rubric): ``meta.rubric_composite = {"key": "score", "weights": {"barrier":
1.0, "U_L_abs": 0.5}}``. :func:`_apply_rubric_composite` computes it onto each
candidate at frontier-assembly time (only when every weighted component is
present — no partial sums) so ``rubric_objectives`` can reference the
composite's ``key`` like any other measure.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
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
    #: Non-ranking diagnostic flags stamped by harvest (e.g. the pathway
    #: quality verdict — ``barrier_trusted``/``barrier_neb_failed``/
    #: ``barrier_desorbed``, see :func:`precis.quest.compute._pathway_quality`).
    #: An untrusted barrier is excluded from ``measures`` entirely (it must
    #: not rank or dominate — "noise should be excluded from ranking"); its
    #: raw value survives here as ``barrier_untrusted_value`` purely for
    #: display (:func:`leaderboard`). Rides along for the leaderboard's
    #: quality column and :func:`precis.quest.graduate.graduate_frontier`'s
    #: belt-and-suspenders gate; never a measure.
    flags: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class FrontierResult:
    objectives: list[tuple[str, str]]
    frontier: list[Candidate] = field(default_factory=list)  # non-dominated
    dominated: list[Candidate] = field(default_factory=list)  # explored + beaten
    unevaluated: list[Candidate] = field(default_factory=list)  # no measures yet


#: Quest hub v2 / Cycle C J4 — the Pareto-scatter axis choice. A starter pick
#: (Reto, 2026-07-25), explicitly changeable later: swap the two ``*_MEASURE``
#: keys (+ their ``*_LABEL``) and every caller (route + template) follows —
#: nothing else moves.
#:
#: X = "highest barrier" → the autocatpath rate-limiting barrier
#: :func:`compute._autocatpath_measures_from_job` stamps onto a candidate's own
#: ``meta`` (harvested by :func:`_candidate_from_structure` above as the
#: ``"barrier"`` measure) — an exact match, no substitution needed.
#:
#: Y = "highest intermediate energy" → no separate "intermediate" concept
#: exists on a candidate yet (a quest's candidates ARE the structures, one
#: `structure` server per candidate material/intermediate — see the module
#: docstring), so the closest available measure is the candidate's own
#: relaxed ``"energy"`` (from ``struct_runs``, the default quest objective —
#: see :data:`DEFAULT_OBJECTIVES`): a candidate's relax energy literally IS
#: that intermediate's energy.
PARETO_X_MEASURE = "barrier"
PARETO_X_LABEL = "Barrier (eV)"
PARETO_Y_MEASURE = "energy"
PARETO_Y_LABEL = "Relaxed energy (eV)"

#: Minimum plottable candidates (both axis measures present) before the
#: scatter is worth drawing — below this a two-point line/point cloud with
#: no real shape isn't more legible than the text frontier already below it.
_SCATTER_MIN_POINTS = 2

#: Default SVG geometry (px) — a `viewBox` this size, `pad` reserved on every
#: edge for axis labels/breathing room around the outermost points.
_SVG_WIDTH = 480.0
_SVG_HEIGHT = 260.0
_SVG_PAD = 36.0

#: Fraction of the data span added as padding on each side of the axis range,
#: so the extreme points never sit flush against the plot border.
_RANGE_PAD_FRACTION = 0.1


@dataclass(frozen=True)
class FrontierScatter:
    """A plottable Pareto scatter — geometry pre-computed, template-ready.

    ``points`` are plain dicts (not a dataclass) since the caller may stamp
    an ``open_url`` onto each before handing them to Jinja; every point
    already carries pixel-space ``cx``/``cy`` so the template does no math.
    """

    points: list[dict[str, Any]]
    x_label: str
    y_label: str
    x_min: float
    x_max: float
    y_min: float
    y_max: float
    width: float = _SVG_WIDTH
    height: float = _SVG_HEIGHT


def build_frontier_scatter(
    candidates: Sequence[Candidate],
    *,
    x_measure: str = PARETO_X_MEASURE,
    y_measure: str = PARETO_Y_MEASURE,
    x_label: str = PARETO_X_LABEL,
    y_label: str = PARETO_Y_LABEL,
    open_url_for: Callable[[Candidate], str] | None = None,
    width: float = _SVG_WIDTH,
    height: float = _SVG_HEIGHT,
    pad: float = _SVG_PAD,
) -> FrontierScatter | None:
    """Extract + scale an (x, y) scatter over ``candidates``, or ``None``.

    Pure geometry: no store, no Jinja. A candidate is plottable only when
    *both* axis measures are present (``_dominates``'s own "missing a measure
    ⇒ not comparable" rule, mirrored here as "not comparable ⇒ not
    plottable"); fewer than :data:`_SCATTER_MIN_POINTS` plottable candidates
    returns ``None`` so the caller falls back to the text-only frontier.
    An all-equal axis (every point shares one x or y) would otherwise divide
    by zero scaling to the viewBox — guarded by substituting a span of
    ``1.0`` so the points simply plot along a flat line instead.
    """
    plottable = [
        c
        for c in candidates
        if c.measures.get(x_measure) is not None
        and c.measures.get(y_measure) is not None
    ]
    if len(plottable) < _SCATTER_MIN_POINTS:
        return None

    xs = [c.measures[x_measure] for c in plottable]
    ys = [c.measures[y_measure] for c in plottable]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)

    x_span = (x_max - x_min) or 1.0
    y_span = (y_max - y_min) or 1.0
    x_lo = x_min - x_span * _RANGE_PAD_FRACTION
    x_hi = x_max + x_span * _RANGE_PAD_FRACTION
    y_lo = y_min - y_span * _RANGE_PAD_FRACTION
    y_hi = y_max + y_span * _RANGE_PAD_FRACTION
    x_range = (x_hi - x_lo) or 1.0
    y_range = (y_hi - y_lo) or 1.0

    plot_w = width - 2 * pad
    plot_h = height - 2 * pad

    def _cx(v: float) -> float:
        return pad + (v - x_lo) / x_range * plot_w

    def _cy(v: float) -> float:
        # SVG y grows downward; flip so the higher value plots higher up.
        return pad + (1.0 - (v - y_lo) / y_range) * plot_h

    points: list[dict[str, Any]] = []
    for c in plottable:
        x = c.measures[x_measure]
        y = c.measures[y_measure]
        point: dict[str, Any] = {
            "ref_id": c.ref_id,
            "handle": c.handle,
            "name": c.name,
            "x": x,
            "y": y,
            "converged": c.converged,
            "cx": round(_cx(x), 2),
            "cy": round(_cy(y), 2),
        }
        if open_url_for is not None:
            point["open_url"] = open_url_for(c)
        points.append(point)

    return FrontierScatter(
        points=points,
        x_label=x_label,
        y_label=y_label,
        x_min=x_min,
        x_max=x_max,
        y_min=y_min,
        y_max=y_max,
        width=width,
        height=height,
    )


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


def _rubric_composite_for(store: Store, quest_id: int) -> dict[str, Any] | None:
    """The quest's declared composite objective, or ``None`` (feature off).

    ``meta.rubric_composite = {"key": "<name>", "weights": {"<measure>":
    <float>, ...}}`` — a **human-set rubric field** (decided: "the agent may not tune its
    own objective"). Written only by :func:`precis.quest.catalyst_seed.
    seed_catalyst_quest` at seed time; no quest-tick or LLM code path writes
    or modifies it (verified: the tick's only quest-meta writes are
    ``ticks_since_experiment`` and the weave-body marker — neither touches
    this key). Malformed/empty shapes (no ``key``, no usable numeric
    weights) are treated as absent rather than raising, matching
    :func:`_objectives_for`'s defensive parse.
    """
    ref = store.get_ref(kind="quest", id=quest_id)
    raw = (ref.meta or {}).get("rubric_composite") if ref else None
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    raw_weights = raw.get("weights")
    if not key or not isinstance(raw_weights, dict):
        return None
    weights = {k: v for k, v in ((k, _numeric(v)) for k, v in raw_weights.items())}
    weights = {k: v for k, v in weights.items() if v is not None}
    if not weights:
        return None
    return {"key": key, "weights": weights}


def _apply_rubric_composite(
    candidates: Sequence[Candidate], composite: dict[str, Any] | None
) -> None:
    """Stamp the quest's declared composite measure onto each candidate that
    has EVERY weighted component — in place, mutating ``Candidate.measures``
    (a plain dict field on an otherwise frozen dataclass) so the composite is
    referenceable from ``rubric_objectives`` like any other measure.

    A candidate missing any one component gets no partial sum and no
    fabricated value — it simply has no ``key`` measure, so it ranks
    ``unevaluated`` on any objective that names it, exactly like a missing
    measure today (:func:`pareto_split`'s existing "missing a declared
    objective" path).
    """
    if not composite:
        return
    key = composite["key"]
    weights: dict[str, float] = composite["weights"]
    for c in candidates:
        total = 0.0
        for mkey, w in weights.items():
            v = c.measures.get(mkey)
            if v is None:
                break
            total += w * v
        else:
            c.measures[key] = total


#: struct_runs columns that are bookkeeping, not measures — never rank on these
#: (``converged`` is a bool and ``status``/``fidelity``/``model``/``created_at``
#: are non-numeric, so ``_numeric`` already filters them; these are the numeric
#: ones we must exclude by name).
_RUN_NON_MEASURE: frozenset[str] = frozenset({"id", "ref_id", "on_version"})

#: structure.meta bookkeeping keys that are never a ranking measure —
#: ``version``/``label_hi`` are ``structure_save``'s own housekeeping and
#: ``quest_harvested_upto``/``quest_autocatpath_harvested_upto`` are the
#: idempotency bookmarks ``harvest_measures`` (compute.py) stamps onto the
#: candidate. ``label_hi``/``lattice``/``pbc`` are non-numeric already
#: (``_numeric`` filters them), so only the numeric ones need listing here.
#: ``barrier_neb_failed``/``barrier_desorbed``/``barrier_wrong_site`` are the
#: pathway-quality warning counts harvest_measures also stamps (``barrier_trusted``/
#: ``barrier_low_confidence`` are bools, already excluded by ``_numeric``) —
#: diagnostics for :func:`leaderboard`'s quality flag, never a rank measure.
#: ``adsorption_barrier`` is the tether's reseat barrier: a trust/annotation
#: signal (activated-adsorption sniff), likewise not a Pareto objective.
#: ``barrier_screen`` is the tier-ladder's superseded (parked/neb-tier)
#: barrier once a higher-fidelity (verify/coadsorbed-tier) one lands
#: (:func:`precis.quest.compute._canonicalize_barrier`) — calibration data
#: (the screen→verify delta), never a ranking axis.
_META_NON_MEASURE: frozenset[str] = frozenset(
    {
        "version",
        "quest_harvested_upto",
        "quest_autocatpath_harvested_upto",
        "barrier_neb_failed",
        "barrier_desorbed",
        "barrier_wrong_site",
        "adsorption_barrier",
        "barrier_screen",
    }
)


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
       reaction **barrier** reaches the frontier: a autocatpath run over the
       candidate is harvested onto the candidate's own ``meta`` (Slice 3), so
       the frontier reads a plain scalar — no autocatpath import, no graph
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
        if k == "params" or k in _META_NON_MEASURE:
            continue
        fv = _numeric(v)
        if fv is not None:
            measures.setdefault(k, fv)  # runs win on collision

    raw_params = meta.get("params")
    params = dict(raw_params) if isinstance(raw_params, dict) else {}

    flags: dict[str, Any] = {}
    if "barrier_trusted" in meta:
        flags["barrier_trusted"] = bool(meta.get("barrier_trusted"))
    if "barrier_neb_failed" in meta:
        flags["barrier_neb_failed"] = meta.get("barrier_neb_failed")
    if "barrier_desorbed" in meta:
        flags["barrier_desorbed"] = meta.get("barrier_desorbed")
    if "barrier_wrong_site" in meta:
        flags["barrier_wrong_site"] = meta.get("barrier_wrong_site")
    if "adsorption_barrier" in meta:
        flags["adsorption_barrier"] = meta.get("adsorption_barrier")
    if "barrier_screen" in meta:
        flags["barrier_screen"] = meta.get("barrier_screen")
    # Selectivity/poisoning naming context (catpath >= 0.5.2 harvests —
    # compute._AUTOCATPATH_SELECTIVITY_CONTEXT_KEYS): the most competitive
    # side product, the deepest kinetic-trap state, per-species poison
    # verdicts, and (>= 0.6.0) the scorecard's limiting axis + one-line
    # worst-problem statement. Strings/dicts, so the `_numeric` filter
    # already keeps them out of `measures`; surfaced as flags for the
    # leaderboard + tick prompt.
    for k in (
        "side_worst",
        "trap_worst",
        "poison_verdicts",
        "limiting_factor",
        "worst_problem",
    ):
        if k in meta:
            flags[k] = meta.get(k)
    # The tier-ladder rung the candidate's CURRENT canonical `barrier` came
    # from (:func:`precis.quest.compute._canonicalize_barrier`) — read by
    # :mod:`precis.quest.graduate`'s verify-only gate; absent on a candidate
    # with no autocatpath barrier harvested yet.
    if "barrier_tier" in meta:
        flags["barrier_tier"] = meta.get("barrier_tier")
    # The candidate's OWN tier-ladder rung (highest tier with a completed
    # run, :func:`precis.quest.compute._bump_tier_stamp` — distinct from
    # ``barrier_tier`` above, which tracks the ranked barrier specifically).
    # Display-only: the leaderboard's glyph column reads this
    # (:func:`leaderboard`); never a ranking measure (already excluded from
    # ``measures`` — ``_META_NON_MEASURE``/``_numeric`` filter the string).
    if "tier" in meta:
        flags["tier"] = meta.get("tier")

    # An untrusted barrier (its pathway had non-converged NEB edges / desorbed
    # or mis-bound adsorbates) is noise, not a measurement — exclude it (and
    # span, plus the CHE electro scalars U_L/U_L_abs/U_opt/span_at_Uopt/
    # P_side — all measured over the SAME pathway, see
    # compute._AUTOCATPATH_ELECTRO_KEYS) from ranking entirely so none of
    # them can dominate or be dominated; each falls to `unevaluated` via the
    # existing "missing a declared objective" path. The raw values survive in
    # `flags` so the leaderboard can still show what was measured, just
    # marked excluded.
    if flags.get("barrier_trusted") is False:
        excluded_barrier = measures.pop("barrier", None)
        measures.pop("span", None)
        if excluded_barrier is not None:
            flags["barrier_untrusted_value"] = excluded_barrier
        for k in (
            "U_L",
            "U_L_abs",
            "U_opt",
            "span_at_Uopt",
            "P_side",
            # selectivity/poisoning scalars — measured over the same
            # untrusted pathway, so they are excluded with it
            "selectivity_margin",
            "trap_margin",
            "poison_margin",
            # legacy 0.5.2-era keys — may still sit on candidates measured
            # pre-swap, before the engine-scorecard rename
            "side_span_margin",
            "trap_depth",
        ):
            excluded = measures.pop(k, None)
            if excluded is not None:
                flags[f"{k}_untrusted_value"] = excluded

    return Candidate(
        ref_id=s.id,
        handle=handle,
        name=name[:70],
        measures=measures,
        converged=converged,
        params=params,
        flags=flags,
    )


#: Tier-ladder glyph column (tier-ladder UX item 4) — one character per rung,
#: read off ``Candidate.flags['tier']`` (the candidate's OWN highest-attained
#: tier, not ``barrier_tier``). A candidate with no tier stamp at all (a
#: pre-ladder quest, or one that opted out — ``tier_ladder=False``) gets no
#: glyph rather than a fabricated one. The word each glyph stands for isn't
#: repeatable in a plain TOON cell (no title-attribute equivalent), so
#: ``QuestHandler._render_leaderboard`` prints the legend once in the
#: header instead of on every row.
TIER_GLYPH: dict[str, str] = {"screening": "○", "neb": "◐", "verify": "●"}


def leaderboard(
    fr: FrontierResult, *, graduated: frozenset[int] | set[int] = frozenset()
) -> tuple[list[dict[str, Any]], list[str]]:
    """Rows + TOON schema for the **by-total** design leaderboard (§7.3).

    One row per candidate design: identity, the objective vector, its Pareto
    ``band`` (``frontier`` / ``dominated`` / ``awaiting``), and a graduation
    flag. Ordered frontier → dominated → awaiting, and within each band sorted
    by the primary objective (best first). Pure over a :class:`FrontierResult`
    so it is trivially testable; the handler renders it via ``toon.dump``. This
    is the striving's authoritative leaderboard — autocatpath's own ``compare`` view
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
            row: dict[str, Any] = {
                "design": c.handle,
                "name": c.name,
                "tier": TIER_GLYPH.get(str(c.flags.get("tier")), ""),
                "band": band,
            }
            untrusted_value = c.flags.get("barrier_untrusted_value")
            for key in obj_keys:
                v = c.measures.get(key)
                if v is None and key == "barrier" and untrusted_value is not None:
                    # Excluded from ranking (noise), but still shown — a
                    # reader should see "we measured this, it just doesn't
                    # count", not a bare unexplained "—".
                    row[key] = f"{untrusted_value:g} (excluded)"
                else:
                    row[key] = f"{v:g}" if isinstance(v, (int, float)) else "—"
            row["graduated"] = "★" if c.ref_id in graduated else ""
            quality = (
                "⚠ non-converged" if c.flags.get("barrier_trusted") is False else ""
            )
            # A geometry that duplicates an earlier candidate (:func:`_flag_geom_duplicates`)
            # — flagged only, not excluded, so it still ranks; the leaderboard just
            # marks it "dup" alongside any other quality note.
            if c.flags.get("duplicate_of"):
                quality = f"{quality} dup".strip()
            row["quality"] = quality
            out.append(row)
        return out

    rows = (
        _rows(fr.frontier, "frontier")
        + _rows(fr.dominated, "dominated")
        + _rows(fr.unevaluated, "awaiting")
    )
    schema = ["design", "name", "tier", *obj_keys, "band", "graduated", "quality"]
    return rows, schema


def _flag_geom_duplicates(
    candidates: Sequence[Candidate], structures: Sequence[Any]
) -> None:
    """Flag a later-created candidate that shares its ``geom_hash`` (stamped
    at candidate-creation time — :func:`precis.quest.compute._geom_hash`) with
    an earlier one — a proposer re-discovering the same material under a new
    name. **Non-exclusionary**: ``flags['duplicate_of']`` is display-only (the
    earlier candidate's handle); the flagged candidate still ranks normally.
    Mutates ``candidates`` in place (``Candidate.flags`` is a plain dict, so
    this is safe on an otherwise-frozen dataclass).
    """
    by_id = {c.ref_id: c for c in candidates}
    seen: dict[str, str] = {}  # geom_hash -> first-seen handle
    for s in sorted(structures, key=lambda s: s.id):
        gh = (getattr(s, "meta", None) or {}).get("geom_hash")
        if not isinstance(gh, str) or not gh:
            continue
        c = by_id.get(s.id)
        if c is None:
            continue
        first = seen.get(gh)
        if first is None:
            seen[gh] = c.handle
        else:
            c.flags["duplicate_of"] = first


def _candidate_lineage_markers(store: Store, c: Candidate) -> list[str]:
    """Trust/ruled-out/dup markers for one candidate's frontier-tree line."""
    markers: list[str] = []
    ruled = next(
        (str(t) for t in store.tags_for(c.ref_id) if str(t).startswith("ruled-out:")),
        None,
    )
    if ruled is not None:
        markers.append(ruled)
    if c.flags.get("barrier_trusted") is False:
        markers.append("untrusted")
    dup_of = c.flags.get("duplicate_of")
    if dup_of:
        markers.append(f"dup-of {dup_of}")
    if not c.converged and not ruled:
        markers.append("unconverged")
    return markers


def _candidate_key_measure(c: Candidate) -> str:
    """The candidate's headline measure for the frontier-tree line — the
    quest hub's own axis pick (:data:`PARETO_X_MEASURE`, the barrier) when
    present, else the default objective (:data:`PARETO_Y_MEASURE`, energy).

    Tier-ladder UX item 4: when the candidate's CANONICAL barrier came from a
    verify-tier pathway that superseded an earlier parked(neb)-tier one
    (``flags['barrier_tier'] == 'verify'`` + a kept ``barrier_screen`` —
    :func:`precis.quest.compute._canonicalize_barrier`), show the
    screen→verify delta itself (``"screen 0.84 → verified 0.96"``) in place
    of the single barrier figure — the headline calibration signal, not just
    the latest number.
    """
    screen = c.flags.get("barrier_screen")
    verified = c.measures.get(PARETO_X_MEASURE)
    if (
        c.flags.get("barrier_tier") == "verify"
        and screen is not None
        and verified is not None
    ):
        return f"screen {screen:g} → verified {verified:g}"
    v = c.measures.get(PARETO_X_MEASURE)
    label = PARETO_X_LABEL
    if v is None:
        v = c.measures.get(PARETO_Y_MEASURE)
        label = PARETO_Y_LABEL
    if v is None:
        return "no measure yet"
    return f"{label}={v:g}"


def render_frontier_tree(store: Store, quest_id: int) -> str:
    """Render the quest's candidate lineage as an indented markdown tree —
    the CODE-regenerated ``meta.pinned='frontier-tree'`` dossier chunk
    (:func:`precis.quest.dossier.update_frontier_tree`).

    Roots are candidates with no resolved ``derived-from`` parent among this
    quest's own candidate set (a link to something outside it — e.g. an
    ancestor design from a prior quest — does not count as in-tree lineage);
    each child nests one level under its parent. One line per candidate:
    name/handle, its key measure (:func:`_candidate_key_measure`), and any
    trust/ruled-out/dup markers (:func:`_candidate_lineage_markers`). Pure
    read over data :func:`quest_frontier` already loads — no new query shape,
    just a different assembly (a tree instead of a Pareto split).
    """
    from precis.quest.gaps import _live_servers

    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    if not structures:
        return "_(No candidates yet.)_\n"

    candidates = {s.id: _candidate_from_structure(store, s) for s in structures}
    _flag_geom_duplicates(list(candidates.values()), structures)
    _apply_rubric_composite(
        list(candidates.values()), _rubric_composite_for(store, quest_id)
    )

    # child ref_id -> parent ref_id, restricted to this quest's own candidates
    # (`derived-from` is child -> parent, the same relation
    # StructureHandler.derive uses).
    parent_of: dict[int, int] = {}
    for sid in candidates:
        for link in store.links_for(sid, direction="out", relation="derived-from"):
            if link.dst_ref_id in candidates:
                parent_of[sid] = int(link.dst_ref_id)
                break
    children: dict[int, list[int]] = {}
    for child, parent in parent_of.items():
        children.setdefault(parent, []).append(child)
    roots = sorted(sid for sid in candidates if sid not in parent_of)

    lines: list[str] = []

    def _render(sid: int, depth: int) -> None:
        c = candidates[sid]
        markers = _candidate_lineage_markers(store, c)
        marker_s = f" [{', '.join(markers)}]" if markers else ""
        lines.append(
            f"{'  ' * depth}- {c.name} [{c.handle}]: "
            f"{_candidate_key_measure(c)}{marker_s}"
        )
        for child_id in sorted(children.get(sid, [])):
            _render(child_id, depth + 1)

    for rid in roots:
        _render(rid, 0)
    return "\n".join(lines) + "\n"


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
    _flag_geom_duplicates(candidates, structures)
    _apply_rubric_composite(candidates, _rubric_composite_for(store, quest_id))
    return pareto_split(candidates, objs)


__all__ = [
    "DEFAULT_OBJECTIVES",
    "PARETO_X_LABEL",
    "PARETO_X_MEASURE",
    "PARETO_Y_LABEL",
    "PARETO_Y_MEASURE",
    "TIER_GLYPH",
    "Candidate",
    "FrontierResult",
    "FrontierScatter",
    "build_frontier_scatter",
    "leaderboard",
    "pareto_split",
    "quest_frontier",
    "render_frontier_tree",
]
