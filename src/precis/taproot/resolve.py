"""resolve_citation — taproot's one shared inline-marker resolution API
(the shipped citation-taproot-resolve proposal, git history).

Given a body chunk and one of its inline citation markers (``[126]``),
resolve it to the bibliographic identity it refers to: the parsed
``paper_bib_entries`` row plus its resolved ``doi`` / ``s2_id`` /
``held_ref_id``. The (chunk_id, marker) → bib-entry mapping is persisted
in ``chunk_citations`` by the ``bib_mark`` sweep (``workers/bib_mark.py``);
this reads it and joins the parsed entry.

**Naming note (decided — proposal In-scope):** unrelated to the
pre-existing worker-pass slug ``resolve_citation:s2`` (S2 metadata
enrichment for stub refs, migration 0001 seed data). Same words, different
namespace — this is a taproot Python API, not a pass slug; don't conflate.

``chunk_id`` is a raw int per codebase convention (``block.id`` /
``links.src_chunk_id``); a caller holding a ``pc<id>`` display handle
strips the prefix first.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from psycopg import Connection

if TYPE_CHECKING:
    from precis.store.protocols import PoolStore


@dataclass(frozen=True)
class BibResolution:
    """One inline marker resolved to its bibliographic identity.

    ``bib_entry_id`` FKs ``paper_bib_entries.id``; ``citing_ref_id`` is the
    paper whose bibliography carries the marker (the ``ref_id`` on that
    entry). ``held_ref_id`` is the *cited* paper when we hold it (else
    ``None`` — a resolved-but-not-held citation, surfaced for display but
    not fetched). ``doi``/``s2_id`` may be present even when not held.
    """

    bib_entry_id: int
    citing_ref_id: int
    marker: int
    raw_text: str
    doi: str | None
    s2_id: str | None
    held_ref_id: int | None
    authors: str | None
    journal: str | None
    year: int | None

    @property
    def is_held(self) -> bool:
        """True iff the cited paper is in the corpus (``held_ref_id`` set)."""
        return self.held_ref_id is not None


def resolve_citation(
    store: PoolStore, chunk_id: int, marker: int, *, conn: Connection | None = None
) -> BibResolution | None:
    """Resolve inline marker ``[marker]`` in body chunk ``chunk_id`` to its
    parsed bib entry, or ``None`` if the sweep recorded no such citation
    (the marker isn't a real bib marker for the chunk's paper, or the chunk
    hasn't been swept yet).

    Single-index lookup: ``chunk_citations`` is keyed ``(chunk_id, marker)``
    and joins ``paper_bib_entries`` for the resolved identity fields.

    ``conn`` (mirrors the store/taproot own-transaction convention): a
    caller already holding an open transaction — e.g. hub-refine's
    ``_citation_candidates`` loop — passes it so the read runs in the same
    snapshot without an extra pool checkout per call; ``None`` (default)
    opens its own read connection.
    """
    sql = """
        SELECT pbe.id, pbe.ref_id, pbe.marker, pbe.raw_text, pbe.doi,
               pbe.s2_id, pbe.held_ref_id, pbe.authors, pbe.journal,
               pbe.year
          FROM chunk_citations cc
          JOIN paper_bib_entries pbe ON pbe.id = cc.bib_entry_id
         WHERE cc.chunk_id = %s AND cc.marker = %s
    """
    if conn is not None:
        row = conn.execute(sql, (chunk_id, marker)).fetchone()
    else:
        with store.pool.connection() as c:
            row = c.execute(sql, (chunk_id, marker)).fetchone()
    if row is None:
        return None
    return BibResolution(
        bib_entry_id=int(row[0]),
        citing_ref_id=int(row[1]),
        marker=int(row[2]),
        raw_text=str(row[3] or ""),
        doi=row[4],
        s2_id=row[5],
        held_ref_id=int(row[6]) if row[6] is not None else None,
        authors=row[7],
        journal=row[8],
        year=int(row[9]) if row[9] is not None else None,
    )


__all__ = ["BibResolution", "resolve_citation"]
