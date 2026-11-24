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
- :func:`queue_asked_term` records the ask and returns its submission token.
  Highlight captures queue on a concept owned by the requesting login, with
  pending context in ``anki_highlight_context``. Shared concepts and taxa
  remain graph references.
- :func:`unqueue_asked_term` reverses one submission under the concept row lock.
  The forge uses the same lock when it publishes a batch.

``meta.asked_count`` is the repeated-ask signal in the morning brief.
``meta.asked_mint`` marks a concept created by the queue. The undo ledger on
the concept records the capture door and time. The selected text is stored
separately and read by the forge for the matching concept and owner.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from psycopg.types.json import Jsonb

from precis.reading.concepts import (
    concept_card_text,
    initial_concept_meta,
    normalize_name,
)
from precis.store import Tag

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

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


def _named_term(
    conn: Connection,
    term: str,
    *,
    owner_login: str | None = None,
    owned_only: bool = False,
) -> tuple[str, int, str, dict[str, Any]] | None:
    """Match names and aliases within the shared graph and this login's refs."""
    row = conn.execute(
        """
        SELECT kind, ref_id, COALESCE(meta->>'name', title), meta FROM refs
        WHERE kind IN ('concept', 'taxon') AND retired_at IS NULL
          AND (owner_login = %s OR (owner_login IS NULL AND NOT %s))
          AND (NOT %s OR kind = 'concept')
          AND (
                meta->>'norm_name' = %s
             OR lower(meta->>'name') = lower(%s)
             OR EXISTS (
                    SELECT 1 FROM jsonb_array_elements_text(
                        COALESCE(meta->'aliases', '[]'::jsonb)) a
                    WHERE lower(a) = lower(%s))
          )
        ORDER BY CASE WHEN kind = 'concept' THEN 0 ELSE 1 END,
                 owner_login NULLS LAST, ref_id
        LIMIT 1
        """,
        (owner_login, owned_only, owned_only, normalize_name(term), term, term),
    ).fetchone()
    return (str(row[0]), int(row[1]), row[2] or term, row[3] or {}) if row else None


def find_named_term(store: Store, term: str) -> tuple[str, int] | None:
    """Return the matching shared concept or taxon, with concepts first."""
    with store.pool.connection() as conn:
        found = _named_term(conn, term)
    return (found[0], found[1]) if found is not None else None


# Both link orientations occur in the graph. An unowned forge candidate
# retains the legacy check for any live card.
LIVE_CARD_SQL = """
    SELECT 1 FROM links l
    JOIN refs a ON a.kind = 'anki' AND a.retired_at IS NULL
               AND a.ref_id = CASE WHEN l.src_ref_id = {concept}
                                   THEN l.dst_ref_id ELSE l.src_ref_id END
    WHERE ((l.src_ref_id = {concept} AND l.relation = 'represents')
        OR (l.dst_ref_id = {concept} AND l.relation = 'represented-by'))
      AND ({owner} IS NULL OR a.owner_login = {owner})
"""


# Only an explicit drop starts the cooldown; sync, rework and concept
# retirement also leave tombstones. The owner lives on the dropped card.
DROPPED_WALK_SQL = """
    SELECT 1 FROM links l
    JOIN refs a ON a.kind = 'anki' AND a.owner_login = {owner}
               AND a.ref_id = CASE WHEN l.src_ref_id = {concept}
                                   THEN l.dst_ref_id ELSE l.src_ref_id END
    WHERE ((l.src_ref_id = {concept} AND l.relation = 'represents')
        OR (l.dst_ref_id = {concept} AND l.relation = 'represented-by'))
      AND a.retired_at = (a.meta->>'walk_dropped_at')::timestamptz
      AND a.retired_at >= {cutoff}
"""


def live_card_exists(conn: Connection, concept_id: int, owner: str | None) -> bool:
    sql = LIVE_CARD_SQL.format(concept="%(concept)s", owner="%(owner)s::text")
    return (
        conn.execute(sql, {"concept": concept_id, "owner": owner}).fetchone()
        is not None
    )


def _submission_entry(
    *,
    owner_login: str | None,
    source: str,
    conv_ref_id: int | None,
) -> dict[str, Any]:
    """One ``meta.asked_submissions`` record — the ask's undo capability
    and (for a capture that lands on a pre-existing node, where the
    mint-path ``definition``/``source`` stamps don't apply) its provenance."""
    entry: dict[str, Any] = {
        "owner": owner_login or "",
        "at": datetime.now(UTC).isoformat(),
    }
    if source:
        entry["source"] = source
    if conv_ref_id is not None:
        entry["ref"] = conv_ref_id
    return entry


def _record_ask(
    ref_id: int,
    *,
    token: str,
    entry: dict[str, Any],
    owner_login: str | None,
    conn: Connection,
) -> None:
    """One ask on a pre-existing node: bump ``meta.asked_count``, file the
    submission under ``meta.asked_submissions``, and re-point
    ``meta.asked_by`` at the latest asker — all in the one jsonb merge so
    a concurrent ask can't lose either side."""
    fields = (
        "'asked_count', COALESCE((meta->>'asked_count')::int, 0) + 1, "
        "'asked_submissions', "
        "  COALESCE(meta->'asked_submissions', '{}'::jsonb) || %s::jsonb"
    )
    params: list[Any] = [Jsonb({token: entry})]
    if owner_login:
        fields += ", 'asked_by', %s::text"
        params.append(owner_login)
    params.append(ref_id)
    conn.execute(
        f"UPDATE refs SET meta = meta || jsonb_build_object({fields}) "
        "WHERE ref_id = %s AND retired_at IS NULL",
        params,
    )


def queue_asked_term(
    store: Store,
    term: str,
    *,
    owner_login: str | None = None,
    conv_ref_id: int | None = None,
    definition: str = "",
    source: str = "",
) -> dict[str, Any]:
    """Queue a term; return ``{kind, ref_id, name, created, submission}``.

    ``source="highlight"`` requires ``owner_login`` (``ValueError``
    otherwise) and stores ``definition`` as pending context. ``submission``
    is the owner-bound token :func:`unqueue_asked_term` takes to undo it.
    """
    highlight = source == "highlight"
    if highlight and not owner_login:
        raise ValueError("highlight capture requires an owner")
    submission = uuid4().hex
    entry = _submission_entry(
        owner_login=owner_login, source=source, conv_ref_id=conv_ref_id
    )
    tag = Tag.open(ASKED_TAG)
    with store.tx() as conn:
        # Serialize first capture for a login, including different aliases of
        # the same shared term. Updates then take the ref lock before the tag.
        if owner_login:
            conn.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"anki-asked:{owner_login}",),
            )
        match = _named_term(conn, term, owner_login=owner_login)
        name = match[2] if match else term
        found = (
            _named_term(conn, name, owner_login=owner_login, owned_only=True)
            if highlight
            else match
        )
        if (
            found
            and conn.execute(
                "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR NO KEY UPDATE",
                (found[1],),
            ).fetchone()
            is None
        ):
            found = None

        if found:
            kind, ref_id, name, _ = found
            _record_ask(
                ref_id,
                token=submission,
                entry=entry,
                owner_login=None if highlight else owner_login,
                conn=conn,
            )
        else:
            kind = "concept"
            extra: dict[str, Any] = {
                "asked_count": 1,
                "asked_mint": True,
                "asked_submissions": {submission: entry},
            }
            if source:
                extra["source"] = source
            if owner_login and not highlight:
                extra["asked_by"] = owner_login
            if highlight and match and match[3].get("cohorts"):
                extra["cohorts"] = match[3]["cohorts"]
            base_definition = "" if highlight else definition.strip()
            if highlight and match:
                base_definition = str(match[3].get("definition") or "")
            ref = store.insert_ref(
                kind="concept",
                slug=None,
                title=name.strip(),
                meta=initial_concept_meta(name, base_definition, extra=extra),
                owner_login=owner_login,
                conn=conn,
            )
            ref_id = int(ref.id)
            store.chunks.upsert_card_combined(
                ref_id, concept_card_text(name, base_definition), conn=conn
            )

        if highlight:
            conn.execute(
                "INSERT INTO anki_highlight_context "
                "(concept_id, owner_login, submission, context) VALUES (%s, %s, %s, %s)",
                (ref_id, owner_login, submission, definition.strip()),
            )
        store.add_tag(ref_id, tag, conn=conn)
        if highlight and match and match[1] != ref_id and not found:
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=match[1],
                relation="related-to",
                conn=conn,
            )
        if conv_ref_id is not None and conv_ref_id != ref_id:
            # add_link keeps existing edge metadata. Only a new page edge
            # belongs to the submissions ledger and can be withdrawn by Undo.
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=conv_ref_id,
                relation="related-to",
                meta={"asked_page": True},
                conn=conn,
            )
    return {
        "kind": kind,
        "ref_id": ref_id,
        "name": name,
        "created": found is None,
        "submission": submission,
    }


def unqueue_asked_term(
    store: Store,
    ref_id: int,
    *,
    submission: str,
    owner_login: str | None = None,
) -> dict[str, Any] | None:
    """Reverse an owner-bound submission while it is still queued."""
    with store.tx() as conn:
        row = conn.execute(
            "SELECT kind, owner_login, meta, retired_at FROM refs "
            "WHERE ref_id = %s FOR NO KEY UPDATE",
            (ref_id,),
        ).fetchone()
        if row is None:
            return None
        kind, ref_owner, meta = str(row[0]), row[1], (row[2] or {})
        if kind not in ("concept", "taxon"):
            return None
        if row[3] is not None or (ref_owner is not None and ref_owner != owner_login):
            return {"undone": False}
        subs = meta.get("asked_submissions")
        subs = dict(subs) if isinstance(subs, dict) else {}
        entry = subs.get(submission)
        if not isinstance(entry, dict) or (
            (entry.get("owner") or "") != (owner_login or "")
        ):
            return {"undone": False}
        if entry.get("source") == "highlight":
            removed = conn.execute(
                "DELETE FROM anki_highlight_context "
                "WHERE concept_id = %s AND owner_login = %s AND submission = %s",
                (ref_id, owner_login, submission),
            )
            if removed.rowcount == 0:
                return {"undone": False}
        subs.pop(submission)
        page_id = entry.get("ref")
        if isinstance(page_id, int) and not any(
            isinstance(other, dict) and other.get("ref") == page_id
            for other in subs.values()
        ):
            conn.execute(
                "DELETE FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
                "AND src_chunk_id IS NULL AND dst_chunk_id IS NULL "
                "AND relation = 'related-to' AND meta @> '{\"asked_page\": true}'",
                (ref_id, page_id),
            )
        try:
            count = max(0, int(meta.get("asked_count") or 0) - 1)
        except (TypeError, ValueError):
            count = 0
        meta["asked_count"] = count
        meta["asked_submissions"] = subs
        latest = max(
            (
                e
                for e in subs.values()
                if isinstance(e, dict) and e.get("source") != "highlight"
            ),
            key=lambda e: str(e.get("at") or ""),
            default=None,
        )
        if entry.get("source") != "highlight":
            if latest and latest.get("owner"):
                meta["asked_by"] = latest["owner"]
            else:
                meta.pop("asked_by", None)
        conn.execute(
            "UPDATE refs SET meta = %s, updated_at = now() WHERE ref_id = %s",
            (Jsonb(meta), ref_id),
        )
        if (
            meta.get("asked_mint")
            and ref_owner is not None
            and ref_owner == owner_login
            and count == 0
            and not live_card_exists(conn, ref_id, None)
        ):
            store.retire_ref(ref_id, conn=conn)
            return {"undone": True, "retired": True}
        if count == 0:
            store.remove_tag(ref_id, Tag.open(ASKED_TAG), conn=conn)
        return {"undone": True, "retired": False, "asked_count": count}


__all__ = [
    "ASKED_TAG",
    "extract_asked_term",
    "find_named_term",
    "first_sentence",
    "queue_asked_term",
    "unqueue_asked_term",
]
