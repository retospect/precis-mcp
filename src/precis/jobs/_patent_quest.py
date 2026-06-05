"""Compose a ``quest`` ref-row body for a watch pass.

The watch runner opens **one quest per pass that produced new hits**.
Body shape:

    Watch '<name>' found N new patent(s) on <date>.

    CQL: <cql>

    New publications:
      - EP1234567B1 — <title…> · <applicant> · <pub-date>
        https://worldwide.espacenet.com/.../EP1234567B1
      - WO2023123456A1 — …
      - …

    Triage: get(kind='patent', id='ep1234567b1') to ingest, or close
    this quest with put(kind='quest', id='<slug>', tags=['STATUS:done']).

The quest itself goes into the ``"default"`` corpus (matching the
``QuestHandler`` convention) with default tag ``STATUS:open``.
``set_by="system"`` because the watch runner is unattended.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.handlers._patent_xml import OpsHit
from precis.store import Tag
from precis.utils.slug import slug_from_text

if TYPE_CHECKING:
    from precis.store import Store

# Same corpus the QuestHandler uses (see handlers/quest.py::_CORPUS_SLUG).
_QUEST_CORPUS_SLUG = "default"


@dataclass(frozen=True, slots=True)
class QuestCreated:
    """Outcome of opening one quest from a watch pass."""

    quest_ref_id: int
    quest_slug: str
    body: str  # full body, for logging/dry-run inspection


def open_quest_for_hits(
    store: Store,
    *,
    watch_name: str,
    cql: str,
    hits: list[OpsHit],
) -> QuestCreated:
    """Open a single quest summarising ``hits``.

    Caller is responsible for dropping the OPS-known-hit set into
    the watch's ``last_seen_pn`` *separately* via
    ``patent_watch_db.record_pass`` — this function only writes the
    quest row + tags.

    Args:
        watch_name: lowercased canonical name of the watch.
        cql:        the watch's stored CQL — included verbatim in the
                    quest body so the operator can rerun by hand.
        hits:       OpsHit list from this pass's diff (caller has
                    already filtered it down to *new* hits).

    Raises:
        ValueError: ``hits`` is empty. Don't call this on a no-hit
                    pass; the runner should bump ``last_run_at``
                    without opening a quest.
    """
    if not hits:
        raise ValueError(
            "open_quest_for_hits called with empty hit list - "
            "skip this pass, don't create an empty quest"
        )

    body = _format_quest_body(watch_name=watch_name, cql=cql, hits=hits)
    title = body.splitlines()[0]  # the headline, used as ref.title

    # Slug: ``patent-<watch-name>-<YYYYMMDD>``. Collisions on multiple
    # passes-per-day get a numeric suffix, mirroring QuestHandler._create.
    today = datetime.now(UTC).strftime("%Y%m%d")
    base_slug = (
        slug_from_text(
            f"patent-{watch_name}-{today}",
            max_len=60,
        )
        or f"patent-watch-{today}"
    )

    slug = base_slug
    if store.get_ref(kind="quest", id=slug) is not None:
        for n in range(2, 1000):
            candidate = f"{base_slug}-{n}"
            if store.get_ref(kind="quest", id=candidate) is None:
                slug = candidate
                break

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="quest",
            slug=slug,
            title=title,
            meta={"body": body, "watch_name": watch_name, "patent_count": len(hits)},
            conn=conn,
        )
    # STATUS:open + a topic tag pointing back at the watch — lets the
    # operator find every quest spawned by a given watch via
    # search(kind='quest', tags=['topic:patent-watch-<name>']).
    store.add_tag(
        ref.id,
        Tag.parse("STATUS:open"),
        set_by="system",
        replace_prefix=True,
    )
    store.add_tag(
        ref.id,
        Tag.parse(f"topic:patent-watch-{watch_name}"),
        set_by="system",
    )
    return QuestCreated(quest_ref_id=ref.id, quest_slug=slug, body=body)


# ---------------------------------------------------------------------------
# Body formatter
# ---------------------------------------------------------------------------


def _format_quest_body(
    *,
    watch_name: str,
    cql: str,
    hits: list[OpsHit],
) -> str:
    """Render the quest's markdown body.

    Headline format mirrors what's used as the ref title — the
    QuestHandler's listing UI (``get(kind='quest', id='/open')``)
    truncates titles at 80 chars, so keep the headline tight.
    """
    n = len(hits)
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    plural = "patent" if n == 1 else "patents"

    lines: list[str] = [
        f"Watch '{watch_name}' found {n} new {plural} on {today}.",
        "",
        f"CQL: {cql}",
        "",
        "New publications:",
    ]
    for h in hits:
        applicants = ", ".join(h.applicants[:2])
        meta_parts = [p for p in (applicants, h.publication_date or "") if p]
        meta_line = f" · {' · '.join(meta_parts)}" if meta_parts else ""
        title = h.title or "(untitled)"
        lines.append(f"  - {h.docdb_id.upper()} - {title}{meta_line}")
        lines.append(f"    {_espacenet_url(h.docdb_id)}")
    lines.append("")
    lines.append(
        "Triage: get(kind='patent', id='<docdb>') to ingest, "
        "or close with put(kind='quest', id='<this-slug>', tags=['STATUS:done'])."
    )
    return "\n".join(lines)


def _espacenet_url(slug: str) -> str:
    """Single-record Espacenet deep-link (no family lookup needed here)."""
    return f"https://worldwide.espacenet.com/patent/search?q={slug.upper()}"


__all__ = ["QuestCreated", "open_quest_for_hits"]
