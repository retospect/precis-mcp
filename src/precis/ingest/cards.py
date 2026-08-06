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

from psycopg.types.json import Jsonb

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


def ensure_abstract_card(conn: Any, ref_id: int, *, set_by: str, abstract: str) -> bool:
    """Insert a ``card_abstract`` chunk when the ref doesn't have one yet.

    A paper ingested *without* an abstract never gets a ``card_abstract``
    row in the first place (:func:`precis.ingest.pipeline._build_cards`
    only emits one ``if abstract``), so a later abstract-fill (e.g.
    ``openalex_enrich``) needs to *mint* the missing card rather than rely
    on :func:`rewrite_cards`'s UPDATE-only semantics, which silently no-ops
    when the row doesn't exist. Returns ``False`` (no-op) when ``abstract``
    is blank or a ``card_abstract`` row already exists — in the latter case
    :func:`rewrite_cards` already handled it; don't double-insert.

    ``ord`` goes one below the ref's lowest existing ``ord`` (cards are
    negative; ``-1`` when the ref has no chunks at all yet). Embedding is
    left unset (NULL) for the embed worker to fill (ADR 0007 — this module
    never calls ``fill_embeddings`` directly). Must run inside a
    transaction (``conn``).
    """
    if not abstract:
        return False
    existing = conn.execute(
        "SELECT 1 FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_abstract' "
        "LIMIT 1",
        (ref_id,),
    ).fetchone()
    if existing:
        return False
    row = conn.execute(
        "SELECT min(ord) FROM chunks WHERE ref_id = %s", (ref_id,)
    ).fetchone()
    min_ord = row[0] if row and row[0] is not None else 0
    conn.execute(
        "INSERT INTO chunks "
        "(ref_id, set_by, ord, chunk_kind, text, section_path, "
        " page_first, page_last, token_count, meta, numerics) "
        "VALUES (%s, %s, %s, 'card_abstract', %s, %s, %s, %s, %s, %s, %s)",
        (ref_id, set_by, min_ord - 1, abstract, [], None, None, None, Jsonb({}), []),
    )
    return True


__all__ = ["CARD_KINDS", "combined_card_text", "ensure_abstract_card", "rewrite_cards"]
