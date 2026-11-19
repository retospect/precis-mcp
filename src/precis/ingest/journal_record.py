"""ISSN journal records shared by metadata enrichment and corpus repair.

Crossref's /journals record owns the spelling; /works container-title and
metrics providers do not. Successful records are persisted in app_state by
ISSN, avoiding a schema change for this small external-record cache. Records
and paper resolution receipts expire after 30 days so missing registries and
changed titles are revisited without an hourly network loop. Exports read the
materialized meta.journal and never perform network requests.

Only equivalent spelling differences (case, punctuation, ampersands) are
settled automatically. A substantive disagreement gets one review todo per
ISSN and pair of names; neither a metrics title nor a casing heuristic can
settle it. Human-verified papers retain their journal.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
from datetime import UTC, datetime, timedelta

from precis.ingest.crossref import fetch_journal
from precis.store import Store
from precis.store.types import Tag

RESOLUTION_VERSION = 1


def venue_key(name: str) -> str:
    """Compare spellings without manufacturing a display title.

    Crossref works serves ``container-title`` HTML-escaped and sometimes
    ALL CAPS (gr685099: ``ENERGY &amp; ENVIRONMENTAL MATERIALS``), so the
    key decodes entities before case-folding — otherwise an escaped
    ampersand never matches the record's plain ``&`` and the paper files
    a spurious disagreement instead of settling.

        >>> venue_key("ENERGY &amp; ENVIRONMENTAL MATERIALS")
        'energyandenvironmentalmaterials'
        >>> venue_key("Energy & Environmental Materials")
        'energyandenvironmentalmaterials'
    """
    plain = html.unescape(name)
    return "".join(c for c in plain.casefold().replace("&", "and") if c.isalnum())


def title_of_record(store: Store, issn: str, mailto: str = "") -> str | None:
    """Return a cached authoritative title, refreshing a stale journal record."""
    issn = issn.strip().upper()
    if not re.fullmatch(r"\d{4}-\d{3}[\dX]", issn):
        return None
    key = f"journal-record:crossref:{issn}"
    cached = store.get_setting(key)
    if cached:
        record = json.loads(cached)
        if datetime.fromisoformat(record["resolved_at"]) > datetime.now(
            UTC
        ) - timedelta(days=30):
            return str(record["title"]) or None
    message = fetch_journal(issn, mailto)
    title = str((message or {}).get("title") or "").strip()
    # An unexpected ISSN is not evidence for the requested journal.
    if message and issn not in message.get("ISSN", []):
        raise ValueError(f"Crossref journal record does not contain ISSN {issn}")
    record_json = json.dumps(
        {
            "title": title,
            "source": "crossref-journals",
            "issn": issn,
            "resolved_at": datetime.now(UTC).isoformat(),
        }
    )
    # A journal's print and electronic ISSNs share this same registry record.
    # Persist aliases too, so neither format needs a work-title fallback.
    aliases = {issn, *((message or {}).get("ISSN") or [])}
    for alias in sorted(aliases):
        if re.fullmatch(r"\d{4}-\d{3}[\dX]", alias):
            store.set_setting(f"journal-record:crossref:{alias}", record_json)
    return title or None


def flag_disagreement(
    store: Store, issn: str, raw: str, canonical: str, *, paper_ref_id: int
) -> None:
    """File one durable, open review todo for an unresolved pair of names."""
    digest = hashlib.sha256(
        json.dumps([issn, venue_key(raw), venue_key(canonical)]).encode("utf-8")
    ).hexdigest()
    with store.tx() as conn:
        # Serialize duplicate reports from concurrent enrichment workers.
        conn.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (digest,))
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'todo' "
            "AND meta->>'journal_disagreement' = %s",
            (digest,),
        ).fetchone()
        if row:
            return
        todo = store.insert_ref(
            kind="todo",
            slug=None,
            title=f"Resolve journal identity for ISSN {issn}: {raw} / {canonical}",
            meta={
                "source": "journal-record",
                "journal_disagreement": digest,
                "issn": issn,
                "raw_title": raw,
                "record_title": canonical,
                "paper_ref_id": paper_ref_id,
                "record_url": f"https://api.crossref.org/journals/{issn}",
            },
            conn=conn,
        )
        store.add_tag(todo.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn)
