"""bib_mark — extract inline citation markers into ``chunk_citations``
(the shipped citation-taproot-resolve proposal, git history).

The base slice ``bib_parse`` (``workers/bib_parse.py``) parses each held
paper's *bibliography* into ``paper_bib_entries`` (``marker -> fields ->
DOI -> held_ref_id``). This pass does the complementary half: it scans the
paper's *body* for where those markers are used inline — ``[126]``,
``[129,130]``, ``[129-130]`` (ranges expanded), ``<sup>``-wrapped included
— and records each ``(chunk_id, marker) -> bib_entry_id`` in
``chunk_citations``, so ``taproot.resolve.resolve_citation`` can answer
"what paper does this claim's ``[N]`` cite?".

**False-positive guard (decided):** only numbers that exist as a parsed
bib marker *for that paper* are accepted. A bracketed number above the
paper's max marker (a figure ref, an equation label, an OCR artefact) is
dropped — the marker set from ``paper_bib_entries`` is the whitelist.

Same drain-and-converge done-marker idiom as ``chase_trigger``'s
``CHASETRIG:<version>`` (own, independently bumpable tag): a swept chunk
carries a ``BIBMARK:<version>`` chunk tag; the claim query skips
already-tagged chunks; bumping :data:`BIBMARK_VERSION` re-sweeps the
corpus lazily (e.g. after a marker-regex change). One coupling to note: a
``BIB_PARSE_VERSION`` bump re-mints ``paper_bib_entries.id``s, which
``ON DELETE CASCADE``s the dependent ``chunk_citations`` rows away — pair a
bib re-parse with a ``BIBMARK_VERSION`` bump to repopulate. Pure text /
regex — no LLM, no external call, no embedding dependency (unlike
``chase_trigger``'s ANN); the sweep needs only the paper's body chunks and
its parsed bib markers.

Default-ON (``_SYS`` profile) like ``bib_parse``: the ``BIBMARK`` marker
converges (a swept chunk is never re-claimed at the same version) so
normal cadence drains the backlog; ``--only bib_mark`` is the fast-path
burst.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any

from precis.store.types import Tag

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bump to force a lazy re-sweep of the whole corpus (every body chunk of a
#: paper with parsed bib entries re-scanned for inline markers).
BIBMARK_VERSION = "1"
_BIBMARK_NS = "BIBMARK"

#: One bracketed citation group: a run of digits / commas / ranges inside
#: ``[...]``. ``<sup>[126]</sup>`` is covered — the ``[126]`` core still
#: matches. Letters inside the brackets (``[H2O]``, ``[see 12]``) don't
#: match, so only citation-shaped groups reach the marker whitelist. The
#: en-dash ``–`` and hyphen both denote ranges. Each marker is capped
#: at 4 digits, mirroring ``bib_parse``'s ``paper_bib_entries.marker``
#: int4 cap.
_CITE_GROUP_RE = re.compile(r"\[\s*(\d{1,4}(?:\s*[,–-]\s*\d{1,4})*)\s*\]")
#: A single ``N-M`` / ``N–M`` range inside a citation group.
_RANGE_RE = re.compile(r"^(\d{1,4})\s*[–-]\s*(\d{1,4})$")
#: A range wider than this is treated as a non-citation (a page span, a
#: numeric interval) and skipped whole — a real citation range is short,
#: and the whitelist would reject the middle anyway; the cap just bounds
#: the expansion work.
_MAX_RANGE_SPAN = 100


def _extract_markers(text: str, valid: frozenset[int]) -> set[int]:
    """Inline citation markers present in ``text`` that are also real bib
    markers for the paper (``valid``). Ranges are expanded; commas split;
    anything outside ``valid`` is dropped (the false-positive guard)."""
    found: set[int] = set()
    for group in _CITE_GROUP_RE.findall(text):
        for part in group.split(","):
            part = part.strip()
            rng = _RANGE_RE.match(part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                if lo <= hi and hi - lo <= _MAX_RANGE_SPAN:
                    found.update(range(lo, hi + 1))
            elif part.isdigit():
                found.add(int(part))
    return found & set(valid)


def _batch_size_default() -> int:
    import os

    try:
        return int(os.environ.get("PRECIS_BIBMARK_BATCH_SIZE", "200"))
    except ValueError:
        return 200


# ── DB: claim a chunk batch, read markers, write citations ─────────────


def _claim_chunks_to_sweep(
    conn: Any, *, batch_size: int
) -> list[tuple[int, int, int, str]]:
    """Up to ``batch_size`` ``(chunk_id, ref_id, ord, text)`` rows for body
    chunks of a ``paper`` that has parsed ``paper_bib_entries`` rows and
    isn't yet swept at :data:`BIBMARK_VERSION`.

    No lease table — the ``BIBMARK:<version>`` chunk tag written after the
    scan IS the durable done-marker (mirrors ``chase_trigger`` /
    ``classify``). ``FOR UPDATE OF c SKIP LOCKED`` keeps concurrent ``_SYS``
    instances off the same batch; the lock is held through write + mark +
    commit so the marker lands before release.
    """
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.ref_id, c.ord, c.text
          FROM chunks c
          JOIN refs r ON r.ref_id = c.ref_id
         WHERE r.kind = 'paper'
           AND r.deleted_at IS NULL
           AND c.ord >= 0
           AND c.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM paper_bib_entries pbe
                  WHERE pbe.ref_id = c.ref_id
               )
           AND NOT EXISTS (
                 SELECT 1 FROM chunk_tags ct JOIN tags t USING (tag_id)
                  WHERE ct.chunk_id = c.chunk_id
                    AND t.namespace = %(ns)s
                    AND t.value = %(val)s
               )
         ORDER BY c.chunk_id
         LIMIT %(limit)s
           FOR UPDATE OF c SKIP LOCKED
        """,
        {"ns": _BIBMARK_NS, "val": BIBMARK_VERSION, "limit": batch_size},
    ).fetchall()
    return [(int(r[0]), int(r[1]), int(r[2]), str(r[3] or "")) for r in rows]


def _paper_marker_entries(conn: Any, ref_id: int) -> dict[int, int]:
    """``marker -> bib_entry_id`` for one paper's parsed bibliography."""
    rows = conn.execute(
        "SELECT marker, id FROM paper_bib_entries WHERE ref_id = %s",
        (ref_id,),
    ).fetchall()
    return {int(r[0]): int(r[1]) for r in rows}


def _write_chunk_citations(
    conn: Any, chunk_id: int, marker_to_entry: dict[int, int], markers: set[int]
) -> int:
    """Re-write ``chunk_citations`` for one chunk: clear its old rows (so a
    ``BIBMARK_VERSION`` re-sweep with a shrunk marker set doesn't leave
    stale rows) then insert one row per extracted marker. Returns the count
    written."""
    conn.execute("DELETE FROM chunk_citations WHERE chunk_id = %s", (chunk_id,))
    written = 0
    for marker in sorted(markers):
        conn.execute(
            "INSERT INTO chunk_citations (chunk_id, marker, bib_entry_id) "
            "VALUES (%s, %s, %s) ON CONFLICT (chunk_id, marker) DO NOTHING",
            (chunk_id, marker, marker_to_entry[marker]),
        )
        written += 1
    return written


def run_bib_mark_pass(store: Store, *, batch_size: int | None = None) -> dict[str, int]:
    """One pass: claim a body-chunk batch, extract whitelisted inline
    markers, write ``chunk_citations``, mark every claimed chunk swept.

    (b)+(c)+(d) run in ONE transaction (all-or-nothing, same as
    ``chase_trigger``): a mid-batch failure rolls back the whole sweep so a
    chunk is never marked swept without its citations written, and it
    re-claims next pass. The lock from :func:`_claim_chunks_to_sweep` is
    held across the write + mark so the ``BIBMARK`` marker lands before
    release.

    Returns ``{chunks_swept, citations, failed}``.
    """
    resolved_batch_size = (
        batch_size if batch_size is not None else _batch_size_default()
    )

    chunks_swept = 0
    citations = 0
    failed = 0
    try:
        with store.pool.connection() as conn:
            claimed = _claim_chunks_to_sweep(conn, batch_size=resolved_batch_size)
            if claimed:
                # marker whitelist per paper (fetched once per distinct ref)
                marker_maps: dict[int, dict[int, int]] = {}
                for chunk_id, ref_id, ord_, text in claimed:
                    if ref_id not in marker_maps:
                        marker_maps[ref_id] = _paper_marker_entries(conn, ref_id)
                    marker_to_entry = marker_maps[ref_id]
                    markers = _extract_markers(text, frozenset(marker_to_entry.keys()))
                    citations += _write_chunk_citations(
                        conn, chunk_id, marker_to_entry, markers
                    )
                    store.add_tag(
                        ref_id,
                        Tag.closed(_BIBMARK_NS, BIBMARK_VERSION),
                        set_by="system",
                        pos=ord_,
                        conn=conn,
                    )
                    chunks_swept += 1
                conn.commit()
    except Exception:
        log.warning(
            "bib_mark: sweep batch failed -- rolled back, retried next pass",
            exc_info=True,
        )
        failed += 1
        chunks_swept = 0
        citations = 0

    return {"chunks_swept": chunks_swept, "citations": citations, "failed": failed}


__all__ = ["BIBMARK_VERSION", "run_bib_mark_pass"]
