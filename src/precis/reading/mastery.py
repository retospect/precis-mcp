"""Mastery-from-Anki — fold card retention stats into concept mastery
(reading-prep loop, the "mastery field" slice).

Each concept is `represents`-linked to the anki cards that render it; those
cards carry `meta.anki_stats` (interval / ease / lapses, refreshed by every
`precis anki-sync`). This pass folds them into the concept's continuous
``meta.mastery`` ∈ [0,1] and derives ``meta.state`` from it — the scalar v1 of
the design doc's mastery field (the event-sourced vector stays the deferred
richer option). Cheap SQL + arithmetic, no LLM. See
docs/design/reading-prep-loop.md §Mastery as a field.

The card-strength model, deliberately simple:

- an unreviewed card knows nothing (0.0);
- strength grows linearly with the weakest deletion's interval, saturating at
  ``PRECIS_MASTERY_INTERVAL_DAYS`` (a card Anki trusts for three weeks is
  treated as solid);
- a leech (the `/leeches` heuristic: lapses ≥ 4 or ease ≤ 2.0) is capped low —
  a card the human keeps failing must not certify mastery, whatever its
  interval history.

Concept mastery = mean strength over its live cards (several renderings
average out); ``state`` thresholds from it (``mastered`` at
``PRECIS_MASTERY_THRESHOLD``). A concept with no cards is left untouched —
candidate/active remain the promotion/release passes' business.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.reading.concepts import STATE_ACTIVE, STATE_MASTERED

log = logging.getLogger(__name__)

#: Interval (days) at which a card counts as fully known. Anki's exponential
#: schedule reaches ~21d after a handful of clean reviews.
DEFAULT_MASTERY_INTERVAL_DAYS = 21.0
#: Concept mastery at/above this is `mastered`.
DEFAULT_MASTERY_THRESHOLD = 0.7
#: Ceiling on a leech card's strength — failing cards can't certify mastery.
LEECH_STRENGTH_CAP = 0.3
#: The `/leeches` heuristic, mirrored from `handlers/anki.py`.
LEECH_LAPSES = 4
LEECH_EASE = 2.0
#: The proving window (days): how long a card gets to prove itself before the
#: rework ladder may touch it — and how old a reviewed, healthy card must be
#: before it counts as *proven* for the recovery reset below. One constant, so
#: "eligible for rework" and "evidence of recovery" stay the same bar.
#: Env: ``PRECIS_CARD_REWORK_MIN_DAYS``. (Reto's "3-4 days".)
DEFAULT_MIN_AGE_DAYS = 4

#: Ignore mastery deltas below this — avoids a per-day update storm rewriting
#: every concept row for float noise.
_EPSILON = 0.005


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "") or default)
    except ValueError:
        return default


def is_leech(stats: dict[str, Any] | None) -> bool:
    """The `/leeches` heuristic over one card's ``anki_stats``."""
    if not stats:
        return False
    lapses = stats.get("lapses_total")
    ease = stats.get("ease_min")
    return (isinstance(lapses, int | float) and lapses >= LEECH_LAPSES) or (
        isinstance(ease, int | float) and ease <= LEECH_EASE
    )


def card_strength(stats: dict[str, Any] | None) -> float:
    """One card's contribution to its concept's mastery, from ``anki_stats``."""
    if not stats:
        return 0.0
    if stats.get("unreviewed"):
        return 0.0
    try:
        interval = float(stats.get("interval_min") or 0)
    except (TypeError, ValueError):
        interval = 0.0
    horizon = _env_float("PRECIS_MASTERY_INTERVAL_DAYS", DEFAULT_MASTERY_INTERVAL_DAYS)
    strength = max(0.0, min(1.0, interval / max(horizon, 1.0)))
    return min(strength, LEECH_STRENGTH_CAP) if is_leech(stats) else strength


def concept_mastery(card_stats: list[dict[str, Any] | None]) -> float:
    """Mean card strength — a concept rendered by several cards averages them."""
    if not card_stats:
        return 0.0
    return sum(card_strength(s) for s in card_stats) / len(card_stats)


def _cards_by_concept(
    store: Any,
) -> dict[int, list[tuple[dict[str, Any] | None, datetime]]]:
    """``{concept_ref_id: [(anki_stats, card_created_at), …]}`` over live
    `represents` links, written from either side (concept `represents` card, or
    card `represented-by` concept). ``created_at`` feeds the proving-window
    check in the recovery reset."""
    sql = """
        SELECT c.ref_id, a.meta->'anki_stats', a.created_at
        FROM refs c
        JOIN links l ON (
                 (l.src_ref_id = c.ref_id AND l.relation = 'represents')
              OR (l.dst_ref_id = c.ref_id AND l.relation = 'represented-by'))
        JOIN refs a ON a.ref_id = CASE WHEN l.src_ref_id = c.ref_id
                                       THEN l.dst_ref_id ELSE l.src_ref_id END
        WHERE c.kind = 'concept' AND c.deleted_at IS NULL
          AND a.kind = 'anki' AND a.deleted_at IS NULL
    """
    out: dict[int, list[tuple[dict[str, Any] | None, datetime]]] = {}
    with store.pool.connection() as conn:
        for concept_id, stats, created_at in conn.execute(sql).fetchall():
            out.setdefault(int(concept_id), []).append((stats, created_at))
    return out


def _has_recovered(
    cards: list[tuple[dict[str, Any] | None, datetime]],
    *,
    now: datetime,
    min_age_days: float,
) -> bool:
    """A concept's card set counts as *recovered* when no live card is a leech
    AND at least one card is proven — reviewed and past the proving window. The
    proven requirement is what keeps the streak honest: a fresh rewrite is
    never a leech *yet*, so mere absence-of-leech the morning after a rewrite
    would reset the streak and the escalation cap could never engage."""
    if any(is_leech(s) for s, _ in cards):
        return False
    horizon = now - timedelta(days=min_age_days)
    return any(
        s is not None and not s.get("unreviewed") and created <= horizon
        for s, created in cards
    )


def run_mastery_pass(store: Any, *, now: datetime | None = None) -> dict[str, int]:
    """Recompute mastery + state for every concept that has cards, and run the
    **recovery reset**: a concept whose cards have proven healthy again gets its
    ``remunge_streak`` zeroed and ``escalated_at`` cleared, so the rework
    ladder's escalation is a state, not a life sentence (gripe 161957). Returns
    ``{concepts, updated, mastered, recovered}``."""
    now = now or datetime.now(UTC)
    threshold = _env_float("PRECIS_MASTERY_THRESHOLD", DEFAULT_MASTERY_THRESHOLD)
    min_age_days = _env_float("PRECIS_CARD_REWORK_MIN_DAYS", DEFAULT_MIN_AGE_DAYS)
    by_concept = _cards_by_concept(store)
    updated = mastered = recovered = 0
    for concept_id, cards in by_concept.items():
        stats_list = [s for s, _ in cards]
        mastery = round(concept_mastery(stats_list), 3)
        state = STATE_MASTERED if mastery >= threshold else STATE_ACTIVE
        if state == STATE_MASTERED:
            mastered += 1
        ref = store.get_ref(kind="concept", id=concept_id)
        if ref is None:
            continue
        meta = ref.meta or {}
        patch: dict[str, Any] = {}
        prev = meta.get("mastery")
        prev_val = float(prev) if isinstance(prev, int | float) else -1.0
        if abs(prev_val - mastery) >= _EPSILON or meta.get("state") != state:
            patch.update(
                mastery=mastery, mastery_updated_at=now.isoformat(), state=state
            )
        if (meta.get("remunge_streak") or meta.get("escalated_at")) and _has_recovered(
            cards, now=now, min_age_days=min_age_days
        ):
            patch.update(remunge_streak=0, escalated_at=None)
            recovered += 1
        if not patch:
            continue
        store.update_ref(concept_id, meta_patch=patch)
        updated += 1
    log.info(
        "mastery pass: %d concept(s) with cards, %d updated, %d mastered, %d recovered",
        len(by_concept),
        updated,
        mastered,
        recovered,
    )
    return {
        "concepts": len(by_concept),
        "updated": updated,
        "mastered": mastered,
        "recovered": recovered,
    }


__all__ = [
    "DEFAULT_MASTERY_INTERVAL_DAYS",
    "DEFAULT_MASTERY_THRESHOLD",
    "DEFAULT_MIN_AGE_DAYS",
    "card_strength",
    "concept_mastery",
    "is_leech",
    "run_mastery_pass",
]
