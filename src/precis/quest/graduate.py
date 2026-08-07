"""Quest graduation — the in-silico ceiling (slice 4e).

The autonomous loop only goes so far: a simulation is not the world. When a
candidate on the Pareto frontier crosses the bar the quest has set for itself, it
**graduates** — it stops being "keep optimising" and becomes "this one is worth a
real-world experiment", a gap surfaced for a human / lab rather than something the
loop pretends to close. Graduation is also a **deed** (a `milestone`): the honest
medieval sense of progress toward the unreachable striving.

The bar is explicit, not guessed — a quest declares it in ``meta.graduation``::

    {"key": "energy", "sense": "min", "threshold": -15.0}

A frontier candidate whose measure meets the threshold is tagged
``needs-experiment`` (once) and logged as a `milestone`; the slice-3 gaps then
surface it as a ``needs-experiment`` item. With no rule set, nothing graduates —
so this ships dark until a quest opts in by declaring its ceiling.

**Graduation is per-candidate, never terminal for the quest.** It tags and
logs one crossing candidate — it never touches the quest's own STATUS, never
appears in the coordinator's (:mod:`precis.workers.job_types.quest_tick`)
rest condition, and the forced-experiment floor + explorer's-creed prompt
block (:mod:`precis.quest.explore`, :func:`precis.quest.tick._explorers_creed`)
both keep pushing after a graduation — the new floor is a *moving* champion,
not a finish line.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.quest.compute import (
    _AUTOCATPATH_BARRIER_KEYS,
    _AUTOCATPATH_SPAN_KEYS,
    _TIER_VERIFY,
    _tier_ladder_enabled,
)
from precis.quest.logbook import append_entry
from precis.store import Tag

if TYPE_CHECKING:
    from precis.store import Store

GRADUATED_TAG = "needs-experiment"
_VALID_SENSES = frozenset({"min", "max"})

#: Graduation keys that are autocatpath-measured barriers — a candidate crossing
#: the ceiling on one of these is only as good as the pathway it was measured
#: over, so :func:`graduate_frontier` gates on the pathway quality verdict
#: (:func:`precis.quest.compute._pathway_quality`) harvest stamped onto the
#: candidate's own meta. Reuses ``compute``'s ``_AUTOCATPATH_BARRIER_KEYS`` /
#: ``_AUTOCATPATH_SPAN_KEYS`` (the single source of truth for these key spellings)
#: rather than re-deriving them — energy-only graduation (``key="energy"``)
#: is untouched.
_AUTOCATPATH_GATED_KEYS = frozenset(
    {*_AUTOCATPATH_BARRIER_KEYS, *_AUTOCATPATH_SPAN_KEYS}
)


def graduation_rule(store: Store, quest_id: int) -> tuple[str, str, float] | None:
    """``(key, sense, threshold)`` from ``meta.graduation``, or ``None``."""
    ref = store.get_ref(kind="quest", id=quest_id)
    raw = (ref.meta or {}).get("graduation") if ref else None
    if not isinstance(raw, dict):
        return None
    key = str(raw.get("key") or "").strip()
    sense = str(raw.get("sense") or "min").strip().lower()
    threshold = raw.get("threshold")
    if not key or sense not in _VALID_SENSES or not isinstance(threshold, (int, float)):
        return None
    return key, sense, float(threshold)


def _meets(value: float, sense: str, threshold: float) -> bool:
    return value <= threshold if sense == "min" else value >= threshold


def graduate_frontier(store: Store, quest_id: int, *, by: str = "agent") -> list[int]:
    """Graduate frontier candidates that cross the quest's ceiling.

    Returns the structure ref ids newly graduated this call. A no-op (``[]``)
    when the quest declares no ``meta.graduation`` rule, or nothing meets it, or
    the crossing candidates are already tagged.
    """
    rule = graduation_rule(store, quest_id)
    if rule is None:
        return []
    key, sense, threshold = rule

    from precis.quest.frontier import quest_frontier

    fr = quest_frontier(store, quest_id)
    ladder_on = _tier_ladder_enabled(store, quest_id)
    graduated: list[int] = []
    for c in fr.frontier:
        value = c.measures.get(key)
        if value is None or not _meets(value, sense, threshold):
            continue
        if any(str(t) == GRADUATED_TAG for t in store.tags_for(c.ref_id)):
            continue
        if key in _AUTOCATPATH_GATED_KEYS and c.flags.get("barrier_trusted") is False:
            n = c.flags.get("barrier_neb_failed") or 0
            m = c.flags.get("barrier_desorbed") or 0
            w = c.flags.get("barrier_wrong_site") or 0
            append_entry(
                store,
                quest_id,
                text=(
                    f"held back {c.handle} ({c.name}) — barrier {value:g} meets "
                    f"ceiling but pathway is untrusted ({n} NEB edge(s) "
                    f"failed / {m} adsorbate(s) desorbed / {w} mis-bound); needs "
                    "a re-run before graduation"
                ),
                entry_type="note",
                by=by,
            )
            continue
        # Tier-ladder quests: an in-silico barrier still owes a coadsorbed
        # (verify-tier) re-run before it earns the real-world-experiment
        # deed — the parked/neb tier is a fragment-parking approximation.
        # `barrier_tier` (:func:`precis.quest.compute._canonicalize_barrier`)
        # tracks which tier the candidate's CURRENT canonical `barrier` came
        # from; a ladder-off quest (no `meta.tier_ladder`) is exempt —
        # today's straight-to-NEB behaviour is unaffected.
        if (
            ladder_on
            and key in _AUTOCATPATH_GATED_KEYS
            and not (
                c.flags.get("barrier_tier") == _TIER_VERIFY
                and c.flags.get("barrier_trusted") is True
            )
        ):
            append_entry(
                store,
                quest_id,
                text=(
                    f"pending verify: {c.handle} ({c.name}) — {key} {value:g} meets "
                    f"the ceiling on a {c.flags.get('barrier_tier') or 'parked'}-tier "
                    "barrier, but this quest's tier ladder requires a trusted "
                    "verify-tier (coadsorbed) run before graduation"
                ),
                entry_type="note",
                by=by,
            )
            continue
        store.add_tag(c.ref_id, Tag.open(GRADUATED_TAG), set_by="system")
        append_entry(
            store,
            quest_id,
            text=(
                f"graduated {c.handle} ({c.name}) — {key}={value:g} meets the "
                f"ceiling ({sense} {threshold:g}); needs a real-world experiment"
            ),
            entry_type="milestone",
            by=by,
        )
        graduated.append(c.ref_id)
    return graduated


__all__ = ["GRADUATED_TAG", "graduate_frontier", "graduation_rule"]
