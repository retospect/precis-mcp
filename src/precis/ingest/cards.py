"""Shared rewrite of a paper's derived *card* chunks.

Cards (``card_title`` / ``card_authors`` / ``card_abstract`` /
``card_combined``) are synthetic search chunks derived from a paper's
bibliographic metadata (see :func:`precis.ingest.pipeline._build_cards`).
They are what semantic search actually matches a title/author query
against — so whenever the metadata is repaired (operator edit, the
``fix-metadata`` remediation), the cards must be rewritten too or search
keeps returning the stale junk text.

This module is the one place that knows the card text shapes, used by
both :mod:`precis.ingest.remediate` (CLI) and
:meth:`precis.handlers.paper.PaperHandler.edit` (operator/agent edits).
DB-only (no corpus / filesystem) so it is safe to call from any process.
"""

from __future__ import annotations

from typing import Any

#: Card chunk kinds whose text is derived from the bibliographic
#: metadata, so they must be rewritten when the metadata is repaired.
CARD_KINDS = ("card_title", "card_authors", "card_abstract", "card_combined")


def combined_card_text(
    title: str, author_names: list[str], abstract: str, keywords: list[str]
) -> str:
    """Mirror :func:`precis.ingest.pipeline._build_cards`'s ``card_combined``."""
    parts: list[str] = []
    if title:
        parts.append(title)
    if author_names:
        parts.append("; ".join(author_names))
    if abstract:
        parts.append(abstract)
    if keywords:
        parts.append("; ".join(keywords))
    return "\n\n".join(parts).strip() or "[no metadata]"


def rewrite_cards(
    conn: Any,
    ref_id: int,
    *,
    title: str,
    author_names: list[str],
    abstract: str,
    keywords: list[str],
) -> int:
    """Rewrite the derived card chunks + drop their embeddings/keywords.

    Updates only the card rows that already exist (cards are derived
    search helpers — the ``refs`` columns are the source of truth). Drops
    the matching ``chunk_embeddings`` rows and nulls ``keywords`` /
    ``keywords_meta`` so the embed / chunk_keywords workers re-claim them.
    Must run inside a transaction (``conn``). Returns the number of chunk
    rows touched.
    """
    text_by_kind = {
        "card_title": title,
        "card_authors": "; ".join(author_names) if author_names else "",
        "card_abstract": abstract,
        "card_combined": combined_card_text(title, author_names, abstract, keywords),
    }
    touched: list[int] = []
    for kind in CARD_KINDS:
        text = text_by_kind[kind]
        if not text:
            continue
        rows = conn.execute(
            "UPDATE chunks SET text = %s, keywords = NULL, keywords_meta = NULL "
            "WHERE ref_id = %s AND chunk_kind = %s RETURNING chunk_id",
            (text, ref_id, kind),
        ).fetchall()
        touched.extend(int(r[0]) for r in rows)
    if touched:
        conn.execute(
            "DELETE FROM chunk_embeddings WHERE chunk_id = ANY(%s)", (touched,)
        )
    return len(touched)


__all__ = ["CARD_KINDS", "combined_card_text", "rewrite_cards"]
