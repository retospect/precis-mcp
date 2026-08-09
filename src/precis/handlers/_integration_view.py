"""``get(kind='draft', id=<dossier-slug>, view='integration')`` — the
integration ledger (paper-writing pipeline rung 2, docs/backlog/paper-writing-pipeline.md §"The integration ledger").

A topic dossier is a `draft` (`dossier-of` → its quest). This view answers
"what's been woven in, and what's still pending?" over that draft's
`topic:` tags:

* **INTEGRATED** — :meth:`Store.integration_ledger`'s rows, grouped by
  section then relation.
* **PENDING (unintegrated)** — :meth:`Store.unintegrated_papers` for the
  dossier's own `topic:` tags — the live "topic:X minus integrated-into"
  gap-review query.

Modelled on ``handlers/_argument_view.py``'s render-through-``Store``-API
style and ``handlers/_links_render.py``'s section-header wrapping — pure
read, no new store round-trips beyond the two ledger queries + one
``tags_for`` call.
"""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING

from precis.response import Response

if TYPE_CHECKING:
    from precis.store import Ref, Store

_NO_SECTION = "— whole document —"


def _dossier_topics(store: Store, ref: Ref) -> list[str]:
    """The dossier's own ``topic:<t>`` open tags, as bare ``<t>`` slugs."""
    topics: list[str] = []
    for t in store.tags_for(ref.id):
        s = str(t)
        if s.startswith("topic:"):
            topics.append(s.split(":", 1)[1])
    return sorted(set(topics))


def _title_of(title: str | None, paper_ref_id: int) -> str:
    if not title:
        return f"(untitled paper {paper_ref_id})"
    return title.splitlines()[0][:100]


def render_integration_view(store: Store, ref: Ref) -> Response:
    """Render ``view='integration'`` for a dossier ``draft``."""
    topics = _dossier_topics(store, ref)
    ledger = store.integration_ledger(ref.id)

    lines = [f"# {ref.slug or ref.id} — integration ledger", ""]

    # ── INTEGRATED — grouped by section, then by relation ──────────────
    lines.append("## INTEGRATED")
    if not ledger:
        lines.append("")
        lines.append("(nothing integrated yet)")
    else:
        by_section: dict[str, list[dict]] = defaultdict(list)
        for row in ledger:
            section = row["section_heading"] or _NO_SECTION
            by_section[section].append(row)
        for section in sorted(by_section, key=lambda s: (s == _NO_SECTION, s)):
            lines.append("")
            lines.append(f"### {section}")
            by_relation: dict[str, list[dict]] = defaultdict(list)
            for row in by_section[section]:
                by_relation[row["relation"]].append(row)
            for relation in sorted(by_relation):
                for row in sorted(
                    by_relation[relation], key=lambda r: r["paper_title"] or ""
                ):
                    title = _title_of(row["paper_title"], row["paper_ref_id"])
                    at = row["at"].strftime("%Y-%m-%d") if row["at"] else "?"
                    lines.append(f"- {title} · {relation} · {at}")

    # ── PENDING (unintegrated) ──────────────────────────────────────────
    lines.append("")
    lines.append("## PENDING (unintegrated)")
    if not topics:
        lines.append("")
        lines.append(
            "(no `topic:` tag on this dossier — pending set unavailable; "
            "tag it `topic:<slug>` to populate the gap list. Automated "
            "stamping at dossier creation is a later rung.)"
        )
    else:
        pending = store.unintegrated_papers(ref.id, topics)
        lines.append("")
        lines.append(f"topics: {', '.join(topics)}")
        lines.append("")
        if not pending:
            lines.append("(disposition-to-zero — nothing pending)")
        else:
            for row in sorted(pending, key=lambda r: r["title"] or ""):
                lines.append(f"- {_title_of(row['title'], row['paper_ref_id'])}")

    return Response(body="\n".join(lines))


__all__ = ["render_integration_view"]
