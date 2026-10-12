"""The budget degradation ladder — what a caller tries when its tier pauses.

The budget breaker (:mod:`precis.budget.breaker`) refuses a paid tier before
any provider runs: the claude quota taper, a parked quota window, a dollar
cap. Most work should stop there — a long agentic run or a taproot pass on a
smaller model wastes the call. A caller whose work *is* worth answering on a
smaller model opts in here and walks :func:`degrade_steps`: each smaller
claude tier, then a free served local model. Nothing in the router walks this
ladder on its own; the breaker is unchanged.

Callers today: asa's Discord turn (``asa_bot.claude_invoke``, gr479851). A
scheduled deliverable (the morning briefing) is the next candidate: same
helpers, its own opt-in.

The caller owns dispatch: it sends each step as an :class:`LlmRequest` with
the step's ``tier``/``model``/``placement`` and ``degraded_from`` set (the
route-log feature the Budget tab counts), stops at the first result that is
not :func:`is_budget_pause`, and tells its reader which tier answered.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from precis.utils.llm.router import PLANNER_TIER_BY_ALIAS, Tier, resolve_model

log = logging.getLogger(__name__)

#: The claude tiers, largest first — the order the ladder walks down.
CLAUDE_TIERS: tuple[Tier, ...] = (Tier.FRONTIER, Tier.BIG, Tier.MEDIUM)

#: Pause classes the ladder degrades on: the breaker's quota taper/park and
#: dollar caps (a cloud throttle is ``budget`` too). A ``rate`` pause — every
#: local slot busy — is capacity, not budget, and is not degraded.
DEGRADE_CLASSES = frozenset({"quota", "budget"})


@dataclass(frozen=True, slots=True)
class DegradeStep:
    """One rung of the ladder. ``placement='cloud'`` keeps a claude step off a
    tier chain's local rungs; the local step (``placement='local'``) carries
    ``model=None`` — resolve it with :func:`local_fallback_model` when reached,
    since that reads the catalog."""

    tier: Tier
    model: str | None
    placement: str

    @property
    def local(self) -> bool:
        return self.placement == "local"


def is_budget_pause(paused: bool, reason_class: str | None) -> bool:
    """True when a result is a breaker refusal the ladder should step past."""
    return paused and reason_class in DEGRADE_CLASSES


def tier_for_model(model: str) -> Tier:
    """The tier a model value asks for: an alias (``local``, ``opus``, ``big``
    …) via :data:`PLANNER_TIER_BY_ALIAS`, a concrete claude id via the tier
    whose live model it is, anything else (a custom id) FRONTIER."""
    tier = PLANNER_TIER_BY_ALIAS.get(model.strip().lower())
    if tier is not None:
        return tier
    for t in CLAUDE_TIERS:
        if resolve_model(t) == model:
            return t
    return Tier.FRONTIER


def degrade_steps(tier: Tier, *, opt_in: bool) -> list[DegradeStep]:
    """The ladder below ``tier``: each smaller claude tier, then local.

    Empty unless the caller opts in — the default for any work is to pause.
    """
    if not opt_in:
        return []
    below = CLAUDE_TIERS[CLAUDE_TIERS.index(tier) + 1 :] if tier in CLAUDE_TIERS else ()
    steps = [DegradeStep(t, resolve_model(t), "cloud") for t in below]
    steps.append(DegradeStep(Tier.BIG, None, "local"))
    return steps


def _card_has_tools(meta: dict[str, Any]) -> bool:
    """Whether an ``llm`` catalog card says the model takes tool calls: a
    ``tools`` entry in its (OpenRouter-reconciled) ``supported_parameters``."""
    for params in (
        meta.get("supported_parameters"),
        (meta.get("facts_openrouter") or {}).get("supported_parameters"),
    ):
        if isinstance(params, list) and "tools" in params:
            return True
    return False


def local_fallback_model(*, tools_needed: bool) -> str | None:
    """The catalog ``model_id`` of a free local model this host can dispatch to
    (its ``served_by`` slot is here or LAN-routable), or ``None``.

    Pair it with ``placement='local'`` and the router builds the local rung
    itself (``router._served_local_rung``). ``tools_needed`` keeps only cards
    that declare tool support. The largest catalog context window wins (a
    conversational system prompt plus tool set is long), ties to the lowest
    ``model_id``. Any lookup failure (no store, catalog unreadable) is ``None``.
    """
    from precis.budget import meter
    from precis.utils.llm import local_serving as _local

    store = meter.active_store()
    if store is None:
        return None
    try:
        cards = store.list_refs(kind="llm", limit=1000)
    except Exception:
        log.debug("local_fallback_model: catalog read failed", exc_info=True)
        return None
    found: list[tuple[int, str]] = []
    for card in cards:
        meta = getattr(card, "meta", None) or {}
        model_id = meta.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            continue
        if tools_needed and not _card_has_tools(meta):
            continue
        if _local.served_locally(model_id):
            ctx = (meta.get("facts_openrouter") or {}).get("context_length")
            found.append((-ctx if isinstance(ctx, int) else 0, model_id))
    return min(found)[1] if found else None


__all__ = [
    "CLAUDE_TIERS",
    "DEGRADE_CLASSES",
    "DegradeStep",
    "degrade_steps",
    "is_budget_pause",
    "local_fallback_model",
    "tier_for_model",
]
