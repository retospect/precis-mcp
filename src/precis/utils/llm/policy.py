"""Model selection policy — deterministic requirement → model (llm-catalog slice 4).

The split of labor the proposal insists on: an **LLM maps a task → a requirement
vector** (judgement it's good at — slice 5), and this **deterministic function maps
the requirement → a concrete model** via the catalog's facts (a lookup + Pareto
rank the LLM is price-blind and self-biased about). Never hand the raw catalog to
the model to pick from.

:func:`select_offering` is a **decision-point** operation (a spawn / a planner
tick / a reviewer — ~hundreds a day), *not* the hot path: it ranks, so it is
gated to where a model actually gets chosen. The hot-path guardrail is `admit`
(slice 2), which is unconditional; ranking never runs per-item.

Degrade-to-floor is the invariant: with an **empty catalog** (or no candidate
that fits) it returns ``resolve_model(tier_floor)`` — byte-identical to today's
behaviour, so wiring a call site through it is safe before any card exists.

Mechanism:

1. **Hard filter** — window (`admit`), budget band (`gate_tier`), availability (a
   runnable card), and required flags (`supported_parameters`: tools / structured).
2. **Rank** — survivors meeting the dominant axis's minimum ordinal, cheapest wins.
3. **"Next better"** — the next model up the ordinal as a **Pareto step over
   (capability, cost)**, reusing :func:`precis.quest.frontier.pareto_split`. Every
   escalation still passes the budget gate (candidates are gate-filtered first).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.utils.llm.admit import admit, window_for
from precis.utils.llm.router import Tier, resolve_model

if TYPE_CHECKING:
    from precis.store import Store

#: supported_parameters values that satisfy a "needs structured output" flag.
_STRUCTURED_PARAMS = frozenset({"structured_outputs", "response_format"})


@dataclass(frozen=True, slots=True)
class Requirement:
    """What a task needs of a model — the vector the LLM infers (slice 5) and
    this policy consumes. ``tier_floor`` is both the budget band and the
    degrade target; the rest narrow the choice."""

    tier_floor: Tier
    axis: str | None = None
    min_ordinal: int = 1
    max_input: int | None = None
    needs_tools: bool = False
    needs_structured: bool = False
    transport: str | None = None


@dataclass(frozen=True, slots=True)
class Selection:
    """The policy's answer: a concrete model (+ the offering to run it on), the
    Pareto ``next_better`` rung for an escalation, and whether it came from the
    catalog or degraded to the ``Tier`` floor."""

    model: str
    offering: dict[str, Any] | None
    next_better: str | None
    from_catalog: bool
    reason: str


def _tier_of(meta: dict[str, Any]) -> Tier | None:
    tf = meta.get("tier_floor")
    if not tf:
        return None
    try:
        return Tier(tf)
    except ValueError:
        return None


def _axis_ordinal(meta: dict[str, Any], axis: str | None) -> int:
    if not axis:
        return 0
    v = (meta.get("capability") or {}).get(axis)
    if v is None:
        return 0
    score = v.get("score") if isinstance(v, dict) else v
    return int(score) if score is not None else 0


def _model_price(meta: dict[str, Any]) -> float:
    """A comparable per-1M input price. Local tiers are free (0); a cloud model
    with no known price sorts last (``inf``) so an unknown never wins on cost."""
    if (meta.get("tier_floor") or "").startswith("local"):
        return 0.0
    prices = [
        o["price_in"]
        for o in (meta.get("offerings") or [])
        if isinstance(o, dict) and o.get("price_in") is not None
    ]
    if prices:
        return float(min(prices))
    fo = meta.get("facts_openrouter") or {}
    if fo.get("price_in") is not None:
        return float(fo["price_in"])
    return float("inf")


def _pick_offering(
    meta: dict[str, Any], transport: str | None
) -> dict[str, Any] | None:
    offerings = meta.get("offerings") or []
    if transport:
        for o in offerings:
            if isinstance(o, dict) and o.get("transport") == transport:
                return o
    return offerings[0] if offerings else None


def _runnable(meta: dict[str, Any]) -> bool:
    """Availability proxy: we know how to run it (an offering or reconciled
    facts). A bare card with neither is skipped."""
    return bool(meta.get("offerings") or meta.get("facts_openrouter"))


def _passes(store: Store, meta: dict[str, Any], req: Requirement) -> bool:
    if not _runnable(meta):
        return False
    # Window: refuse only a *known* too-small window (unknown → admit catches it
    # at dispatch). Uses the same headroom-aware check as the hot path.
    if req.max_input is not None:
        window = window_for(meta, req.transport)
        if window is not None and not admit(req.max_input, window).fits:
            return False
    # Required capability flags — lenient when supported_parameters is unknown.
    sp = (meta.get("facts_openrouter") or {}).get("supported_parameters")
    if sp is not None:
        params = set(sp)
        if req.needs_tools and "tools" not in params:
            return False
        if req.needs_structured and not (_STRUCTURED_PARAMS & params):
            return False
    # Budget band: exclude a candidate whose tier is currently gated.
    from precis.budget import breaker

    tier = _tier_of(meta)
    if tier is not None and breaker.gate_tier(tier, store=store) is not None:
        return False
    return True


def _candidates(store: Store) -> list[tuple[str, dict[str, Any]]]:
    out: list[tuple[str, dict[str, Any]]] = []
    for c in store.list_refs(kind="llm", limit=1000):
        meta = c.meta or {}
        mid = meta.get("model_id")
        if mid:
            out.append((mid, meta))
    return out


def _next_better(
    survivors: list[tuple[str, dict[str, Any]]],
    chosen_meta: dict[str, Any],
    axis: str | None,
) -> str | None:
    """The cheapest survivor strictly more capable than the chosen model, taken
    from the Pareto frontier over (capability↑, cost↓). ``None`` when the chosen
    is already the most capable, or no axis is set."""
    if not axis:
        return None
    from precis.quest.frontier import Candidate, pareto_split

    cands = [
        Candidate(
            ref_id=i,
            handle=mid,
            name=mid,
            measures={"cap": _axis_ordinal(meta, axis), "cost": _model_price(meta)},
            converged=True,
        )
        for i, (mid, meta) in enumerate(survivors)
        if _model_price(meta) != float("inf")
    ]
    frontier = pareto_split(cands, [("cap", "max"), ("cost", "min")]).frontier
    chosen_ord = _axis_ordinal(chosen_meta, axis)
    ups = sorted(
        (c for c in frontier if c.measures["cap"] > chosen_ord),
        key=lambda c: c.measures["cost"],
    )
    return ups[0].handle if ups else None


def select_offering(store: Store, req: Requirement) -> Selection:
    """Pick a concrete ``(model, offering)`` for ``req`` from the catalog, with a
    Pareto ``next_better`` rung. Degrades to ``resolve_model(req.tier_floor)`` —
    byte-identical to today — when the catalog is empty or nothing fits.
    """
    floor_model = resolve_model(req.tier_floor)
    survivors = [
        (mid, meta) for mid, meta in _candidates(store) if _passes(store, meta, req)
    ]
    if req.axis:
        survivors = [
            (mid, meta)
            for mid, meta in survivors
            if _axis_ordinal(meta, req.axis) >= req.min_ordinal
        ]
    if not survivors:
        return Selection(
            model=floor_model,
            offering=None,
            next_better=None,
            from_catalog=False,
            reason="no catalog candidate fits — using the tier floor",
        )
    # cheapest; ties broken toward the more capable, then a stable name order.
    survivors.sort(
        key=lambda it: (_model_price(it[1]), -_axis_ordinal(it[1], req.axis), it[0])
    )
    chosen_mid, chosen_meta = survivors[0]
    return Selection(
        model=chosen_mid,
        offering=_pick_offering(chosen_meta, req.transport),
        next_better=_next_better(survivors, chosen_meta, req.axis),
        from_catalog=True,
        reason=(
            f"cheapest {req.axis or 'model'}"
            + (f" ≥{req.min_ordinal}" if req.axis else "")
            + " that fits window/budget/flags"
        ),
    )


__all__ = ["Requirement", "Selection", "select_offering"]
