"""``twin:asked`` — the "what is X?" term-capture half of the anki-twin loop.

A user asking what a term means is the cheapest signal that the term belongs
in their Anki deck (gr548276/gr548277). This module holds the shared pieces
every capture door uses:

- :func:`extract_asked_term` — the regex table over a user message ("what is
  entropy?", "remind me what a perovskite is", "perovskite?"). Pure; the
  Claude Code hook script (``scripts/hooks/twin-asked.sh``) reimplements the
  same table standalone — keep the two in step.
- :func:`find_named_term` — the case-insensitive concept/taxon name-or-alias
  lookup (drives both the asa capture and the ``/anki`` manual queue).
- :func:`queue_asked_term` — the store-side write the web route shares:
  an existing node gets the ``twin:asked`` tag and a ``meta.asked_count``
  bump; an unknown term is minted as a ``twin:asked`` concept (the next
  ``card_forge`` mints asked concepts first — see
  ``flashcards._cardless_concepts``).

``meta.asked_count`` is the "asked twice, no card yet" signal the morning
brief reports on (``reading/briefing_cast.py``'s recall lane).
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from precis.reading.concepts import concept_card_text, initial_concept_meta
from precis.store import Tag

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: The open tag marking a concept/taxon the user asked about — the forge's
#: mint pass sorts tagged concepts first among cardless candidates.
ASKED_TAG = "twin:asked"

#: ``what is/are/does/do <term> [mean][?]`` — article stripped. The ``[^?]``
#: term body keeps a mid-question '?' from matching inside the term.
_WHAT_RE = re.compile(
    r"^(?:what|what's|whats)\s+(?:is|are|does|do)\s+"
    r"(?:an?\s+|the\s+)?(?P<term>[^?]{2,60}?)(?:\s+mean)?\s*\??$",
    re.IGNORECASE,
)
#: ``remind me what <term> is[?]``.
_REMIND_RE = re.compile(
    r"^remind\s+me\s+what\s+(?:an?\s+|the\s+)?(?P<term>[^?]{2,60}?)\s+is\s*\??$",
    re.IGNORECASE,
)
#: A bare one-word question: ``entropy?``.
_BARE_RE = re.compile(r"^(?P<term>\S{2,40})\?$", re.IGNORECASE)

#: Ops/status phrasing the what-is shape also matches but is never a vocab
#: question ("what is the status of X"). Checked against the extracted term.
_EXCLUDE_RE = re.compile(r"^status\s+of\b", re.IGNORECASE)


def extract_asked_term(message: str) -> str | None:
    """The term a "what is X?"-shaped message asks about, or ``None``.

    Anchored whole-message matches only — a what-is buried mid-message is
    deliberately not captured (the cheap signal shouldn't fire on prose).
    """
    text = (message or "").strip()
    if not text:
        return None
    for pattern in (_WHAT_RE, _REMIND_RE, _BARE_RE):
        m = pattern.match(text)
        if m is None:
            continue
        term = " ".join(m.group("term").split())
        if term and not _EXCLUDE_RE.match(term):
            return term
    return None


def first_sentence(text: str) -> str:
    """The first sentence of a reply — the fallback definition for a
    freshly-minted asked concept (capped so a run-on can't bloat it)."""
    flat = " ".join((text or "").split())
    m = re.search(r"(?<=[.!?])\s", flat)
    sentence = flat[: m.start() + 1] if m else flat
    return sentence[:240].rstrip()


def find_named_term(store: Store, term: str) -> tuple[str, int] | None:
    """``(kind, ref_id)`` of a live concept/taxon named ``term``
    (case-insensitive, aliases included), concept preferred on a tie."""
    from precis.reading.concepts import normalize_name

    norm = normalize_name(term)
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT kind, ref_id FROM refs
            WHERE kind IN ('concept', 'taxon') AND retired_at IS NULL
              AND (
                    meta->>'norm_name' = %s
                 OR lower(meta->>'name') = lower(%s)
                 OR EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(
                            COALESCE(meta->'aliases', '[]'::jsonb)) a
                        WHERE lower(a) = lower(%s))
              )
            ORDER BY CASE WHEN kind = 'concept' THEN 0 ELSE 1 END, ref_id
            LIMIT 1
            """,
            (norm, term, term),
        ).fetchone()
    return (str(row[0]), int(row[1])) if row else None


def _bump_asked_count(ref_id: int, *, conn: Any) -> None:
    conn.execute(
        "UPDATE refs SET meta = meta || jsonb_build_object("
        "  'asked_count', COALESCE((meta->>'asked_count')::int, 0) + 1) "
        "WHERE ref_id = %s AND retired_at IS NULL",
        (ref_id,),
    )


def queue_asked_term(
    store: Store,
    term: str,
    *,
    owner_login: str | None = None,
    conv_ref_id: int | None = None,
    definition: str = "",
) -> dict[str, Any]:
    """Queue one asked term for the card forge.

    An existing concept/taxon named ``term`` gets the ``twin:asked`` tag and
    a ``meta.asked_count`` bump. An unknown term mints a ``twin:asked``
    concept (``owner_login`` when given, linked ``related-to`` the
    conversation ref when ``conv_ref_id`` is). Returns
    ``{kind, ref_id, name, created}``; empty terms are skipped by the caller.
    """
    tag = Tag.open(ASKED_TAG)
    found = find_named_term(store, term)
    if found is not None:
        kind, ref_id = found
        with store.tx() as conn:
            store.add_tag(ref_id, tag, conn=conn)
            _bump_asked_count(ref_id, conn=conn)
        ref = store.get_ref(kind=kind, id=ref_id)
        name = (
            ((ref.meta or {}).get("name") or getattr(ref, "title", "") or term)
            if ref
            else term
        )
        return {"kind": kind, "ref_id": ref_id, "name": name, "created": False}

    meta = initial_concept_meta(term, definition.strip(), extra={"asked_count": 1})
    card = concept_card_text(term, definition.strip())
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="concept",
            slug=None,
            title=term.strip(),
            meta=meta,
            owner_login=owner_login,
            conn=conn,
        )
        store.chunks.upsert_card_combined(ref.id, card, conn=conn)
        store.add_tag(ref.id, tag, conn=conn)
        if conv_ref_id is not None:
            store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=conv_ref_id,
                relation="related-to",
                conn=conn,
            )
    return {"kind": "concept", "ref_id": int(ref.id), "name": term, "created": True}


__all__ = [
    "ASKED_TAG",
    "extract_asked_term",
    "find_named_term",
    "first_sentence",
    "queue_asked_term",
]
