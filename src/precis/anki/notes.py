"""Pure, anki-free helpers for the Anki sync — conventions + data mapping.

No `anki` import here, so this is safe to import anywhere (tests, the CLI arg
layer, the handler). The heavy pylib lives in `sync`.

Identity convention: every precis-authored note carries a **deterministic guid
keyed on the precis ref_id** (`precis:<ref_id>`). That is what makes a re-push
*update* the existing note in place — preserving its Anki scheduling history —
instead of creating a duplicate. It is NEVER derived from the card text (a text
edit must not re-guid, or Anki would treat it as a new card and reset the
forgetting curve). Notes also carry a human-findable `precis::<ref_id>` tag.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: The deck every precis-authored card lands in. Adopted, not schema-changing.
PRECIS_DECK = "Precis"

#: guid prefix — `precis:<ref_id>`. Stable across text edits (keyed on identity,
#: not content) so scheduling history survives a reword.
GUID_PREFIX = "precis:"

#: Applied to every note precis owns, so a query / the guard can scope to
#: "our" notes and never touch a foreign one.
MANAGED_TAG = "precis::managed"


def guid_for(ref_id: int) -> str:
    """Deterministic Anki guid for a precis `anki` ref."""
    return f"{GUID_PREFIX}{ref_id}"


def ref_id_from_guid(guid: str) -> int | None:
    """Inverse of `guid_for`; None for a foreign (non-precis) guid."""
    if guid.startswith(GUID_PREFIX):
        try:
            return int(guid[len(GUID_PREFIX) :])
        except ValueError:
            return None
    return None


def precis_tag(ref_id: int) -> str:
    """The human-findable per-ref tag on our notes."""
    return f"precis::{ref_id}"


@dataclass(frozen=True)
class AnkiCardSpec:
    """One precis-authored note to upsert into the mirror."""

    ref_id: int
    fields: dict[str, str]  # e.g. {"Text": "<cloze markup>", "Back Extra": "…"}
    deck: str = PRECIS_DECK  # meta.deck; a `Precis::<topic>` sub-deck or Precis


def spec_from_ref(ref: Any) -> AnkiCardSpec | None:
    """Build a card spec for pushing a precis-*authored* card to Anki. Returns
    None (filtered out) for:

    - a **read-only projection** of a hand-made card (`meta.source ==
      'anki-foreign'` / `meta.readonly`) — pushing these back to Anki creates
      duplicates of the user's own cards (the 2026-07 incident). ONLY authored
      cards go up.
    - a non-cloze notetype (slice 2 only syncs Cloze)."""
    meta = ref.meta or {}
    if meta.get("source") == "anki-foreign" or meta.get("readonly"):
        return None
    if meta.get("notetype", "Cloze") != "Cloze":
        return None
    fields_meta = meta.get("fields") or {}
    text = fields_meta.get("Text") or ref.title
    out: dict[str, str] = {"Text": text}
    extra = fields_meta.get("Back Extra")
    if extra:
        out["Back Extra"] = extra
    return AnkiCardSpec(ref_id=ref.id, fields=out, deck=meta.get("deck") or PRECIS_DECK)


def aggregate_stats(
    card_rows: list[tuple[int, int, int, int, int, int]],
) -> dict[str, Any]:
    """Fold the per-card stats of one note (a cloze note has N cards, one per
    deletion) into a single per-ref retention signal.

    Each row is ``(ivl, factor, reps, lapses, due, queue)``. A card is only as
    *known* as its weakest deletion, so we surface the minimum interval/ease and
    the totals. ``factor`` is Anki's ease ×1000.
    """
    if not card_rows:
        return {}
    ivls = [r[0] for r in card_rows]
    factors = [r[1] for r in card_rows if r[1]]
    reps = [r[2] for r in card_rows]
    lapses = [r[3] for r in card_rows]
    dues = [r[4] for r in card_rows]
    queues = [r[5] for r in card_rows]
    return {
        "interval_min": min(ivls),
        "interval_max": max(ivls),
        "ease_min": round(min(factors) / 1000, 2) if factors else None,
        "reps_total": sum(reps),
        "lapses_total": sum(lapses),
        "due_min": min(dues),
        "cards": len(card_rows),
        "unreviewed": all(q == 0 for q in queues),
    }
