"""cite_resolve — chunk→paper ``cites`` links from in-text citation
markers (gr513768, docs/backlog/citation-resolution-skill.md slices 1–2).

Where ``bib_mark`` (``workers/bib_mark.py``) records *which* inline
``[N]`` markers a body chunk uses (``chunk_citations``) and
``bib_parse.ensure_bib_cites_links`` mints *paper-level* ``cites``
edges from matched bib entries, this pass extends the same write to
chunk scope on the citing side: a chunk that says "as shown before
[32]" gets a chunk→paper ``cites`` link to the paper its bib entry 32
resolved to. With reference chunks permanently unembedded (gr513749),
the marker is the reader's first hop to the cited work.

Two deterministic layers (no LLM, no external call):

- **Numeric** (slice 1): ``chunk_citations`` markers whose
  ``paper_bib_entries`` row carries a ``held_ref_id`` — nothing to
  re-derive, the chunk's own sweep rows are the whitelist. Markers
  whose entry is DOI-matched but not held write nothing (the gr513755
  trickle owns stub minting).
- **Author-year** (slice 2): ``Miller 1921``, ``(Smith et al., 2004)``,
  ``Smith and Jones (2004a)`` — parsed *in memory only* (Reto
  2026-10-24: "we don't store, this is temporary for resolution"): no
  table, no ``chunk_citations`` rows, the only persisted result is the
  link. Candidates are whitelisted the ``bib_mark`` way — a surname +
  year counts only when it matches exactly one of the citing paper's
  own ``paper_bib_entries`` (``authors`` / ``year``); a zero or
  two-plus match writes nothing.

**Marker order (decided).** Every link carries ``meta.marker`` (the
label as written — ``"32"``, ``"[3-5]"``, ``"Miller 1921"``) and
``meta.marker_order``, the 0-based ordinal of the citation's first
occurrence within the chunk text. A range ``[3-5]`` writes one link
per cited paper, each stamped ``meta.marker='[3-5]'`` with its own
``marker_order`` in expansion order (Reto ruling, spec decisions
log). And since all of one chunk's links are inserted in one
transaction in ``marker_order`` sequence, ``links_for``'s
``ORDER BY created_at, link_id`` already yields that order.
``marker_order`` numbers *resolved* occurrences, not written links —
where an occurrence's target is skipped (unheld, retired, self-cite,
or a duplicate paper) the written sequence keeps its ordinals, so
gaps are expected: the order is monotonic, not contiguous.

**Writer identity (decided).** Links write ``set_by='system'`` with
``meta.source='cite_resolve'`` and a ``meta.writer`` annotation
naming the pass and version (``cite_resolve:<CITERES_VERSION>``) —
deterministic writes carry no model identity, so the machine-edge
gate does not apply to them (``_links_ops._machine_write_context``).

**Dark + convergent.** The pass claims only papers tagged
``CITERES:pending`` (slice 5 wires the first-``get`` trigger — until
then nothing marks a paper and the claim set is empty), and only once
``bib_mark`` has swept them — every live body chunk carries its
``BIBMARK:<version>`` tag, or the paper has no parsed bib entries at
all (spec ruling: ``chunk_citations`` is the numeric whitelist, so
sweeping earlier would mint a permanent gap a later ``bib_mark`` row
can't redo). The gate is IN the claim ``WHERE`` — an un-ready paper
keeps ``CITERES:pending`` but is never claimed, so a backlog of them
can't starve a ready paper out of a batch slot. It sweeps their body
chunks lacking a ``CITERES:<version>`` chunk tag (the
drain-and-converge done-marker idiom — bumping
:data:`CITERES_VERSION` re-sweeps lazily), then transitions the paper
``CITERES:pending → CITERES:swept``. Per-paper isolation: each paper
runs in its own savepoint — a failure rolls back only that paper and
stamps ``CITERES:error:<n>`` (the durable record; the claim sorts
error-tagged papers last — the backoff) while the rest of the batch
commits. After :data:`_MAX_ERROR_ATTEMPTS` failures the paper is
stamped ``CITERES:error:terminal``, logged once at the transition,
and never claimed again (the ``pending → swept`` ``replace_prefix``
flip clears every ``CITERES`` tag, so a recovered paper — say after a
``CITERES_VERSION`` bump by hand — loses its error history too).
DEFAULT-OFF: registered via ``enable_env`` with no
``default_profiles``, so a ``service_config`` row (or ``--only
cite_resolve``) is the only thing that runs it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg import Connection

from precis.store.types import Tag
from precis.workers.bib_mark import (
    _BIBMARK_NS,
    _CITE_GROUP_RE,
    _MAX_RANGE_SPAN,
    _RANGE_RE,
    BIBMARK_VERSION,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Bump to re-sweep chunks already stamped ``CITERES:<version>`` (the
#: same lazy re-sweep contract as ``BIBMARK_VERSION``).
CITERES_VERSION = "1"
_CITERES_NS = "CITERES"
_PENDING = "pending"
_SWEPT = "swept"
#: Failure record, stamped alongside ``pending`` as ``error:1``,
#: ``error:2``, … — each value is a committed attempt, so the tag count
#: IS the attempt count. The claim sorts any ``error%``-tagged paper
#: behind clean ones (the backoff); at :data:`_MAX_ERROR_ATTEMPTS` the
#: stamp is ``error:terminal`` instead and the claim excludes the
#: paper outright. A successful sweep's ``replace_prefix`` flip clears
#: the whole namespace, error history included.
_ERROR = "error"
_ERROR_TERMINAL = "error:terminal"
_MAX_ERROR_ATTEMPTS = 3

#: ``links.meta.source`` for this pass's ``cites`` rows — a distinct
#: value, same role as ``bib_parse``'s / ``inbound_chase``'s, so each
#: machine writer's claim queries and audits stay disjoint.
_LINK_SOURCE = "cite_resolve"


@dataclass(frozen=True, slots=True)
class BibEntry:
    """The columns of one ``paper_bib_entries`` row this pass reads."""

    id: int
    marker: int
    authors: str | None
    year: int | None
    held_ref_id: int | None


@dataclass(frozen=True, slots=True)
class _Occurrence:
    """One in-text citation resolved to a bib entry, before dedupe."""

    pos: int  # text offset of the citation's start
    key: tuple[Any, ...]  # first-occurrence dedupe key
    label: str  # meta.marker value
    entry_id: int  # paper_bib_entries.id


# ── marker occurrences (numeric) ─────────────────────────────────────


def _canonical_group_label(parts: list[str]) -> str:
    """Normalise one bracket group's interior for ``meta.marker``:
    ``" 129 - 130 "`` → ``"[129-130]"``; ranges keep a single ``-``."""
    canon: list[str] = []
    for part in parts:
        rng = _RANGE_RE.match(part)
        if rng:
            canon.append(f"{int(rng.group(1))}-{int(rng.group(2))}")
        else:
            canon.append(part.strip())
    return "[" + ",".join(canon) + "]"


def _numeric_occurrences(
    text: str, marker_to_entry: dict[int, int]
) -> list[_Occurrence]:
    """``[N]`` / ``[N,M]`` / ``[N-M]`` occurrences resolved to
    ``chunk_citations``' marker→entry map (the whitelist). Each expanded
    marker of a multi-marker group is its own occurrence sharing the
    group's text position; the label is the bare number for a
    single-marker group, the canonical bracketed group otherwise
    (``"32"``, ``"[3-5]"``, ``"[7,9]"`` — spec decision 3)."""
    out: list[_Occurrence] = []
    for m in _CITE_GROUP_RE.finditer(text):
        parts = [p.strip() for p in m.group(1).split(",")]
        markers: list[int] = []
        for part in parts:
            rng = _RANGE_RE.match(part)
            if rng:
                lo, hi = int(rng.group(1)), int(rng.group(2))
                if lo <= hi and hi - lo <= _MAX_RANGE_SPAN:
                    markers.extend(range(lo, hi + 1))
            elif part.isdigit():
                markers.append(int(part))
        markers = [mk for mk in markers if mk in marker_to_entry]
        if not markers:
            continue
        single = len(markers) == 1
        label = str(markers[0]) if single else _canonical_group_label(parts)
        for mk in markers:
            out.append(
                _Occurrence(
                    pos=m.start(),
                    key=("n", mk),
                    label=label,
                    entry_id=marker_to_entry[mk],
                )
            )
    return out


# ── marker occurrences (author-year, in memory only) ─────────────────

#: A surname as written in running text — capitalised, allowing
#: hyphens/apostrophes and accented letters, optionally preceded by a
#: lowercase nobiliary particle (``van der Waals``). The particle's
#: lookbehind requires a non-letter (or string start) before it —
#: without it ``consider Smith`` / ``under Miller`` / ``made Chen``
#: match ``der Smith`` / ``de Chen`` as particle+surname and the real
#: surname is never tried.
_NAME_CHARS = r"[A-Za-zÀ-ÖØ-öø-ÿ'’\-]"
_PARTICLE = (
    r"(?:(?<![^\W\d_])"
    r"(?:van\s+der|van|von|der|den|de\s+la|de|di|del|della|la|le)\s+)?"
)
_SURNAME = rf"(?:{_PARTICLE}[A-Z]{_NAME_CHARS}+)"
_AUTHOR_GROUP = (
    rf"(?P<surname>{_SURNAME})"
    rf"(?P<rest>\s+(?:et\s+al\.?|and\s+{_SURNAME}|&\s+{_SURNAME}))?"
)
#: ``Surname (2004)``, ``Surname 2004``, ``(Surname, 2004)``,
#: ``Smith and Jones (2004a)``, ``(Smith et al., 2004)`` and
#: ``;``-separated parenthetical lists all reduce to "surname … year"
#: adjacency — the over-match is deliberate: the paper's own bib
#: entries are the whitelist, exactly as ``bib_mark`` treats ``[N]``.
_AUTHOR_YEAR_RE = re.compile(
    rf"{_AUTHOR_GROUP},?\s*\(?(?P<year>(?:18|19|20)\d{{2}}[a-z]?)\)?"
)

_NAME_STRIP_RE = re.compile(r"[^a-z'\-]+")


def _norm_name(name: str) -> str:
    """Surname compare key: NFKD-folded, lowercased, stripped to
    letters + ``'``/``-`` so ``O'Brien`` matches ``O’Brien``."""
    folded = (
        unicodedata.normalize("NFKD", name)
        .replace("’", "'")
        .encode("ascii", "ignore")
        .decode()
        .lower()
    )
    return _NAME_STRIP_RE.sub("", folded)


def _first_surname(authors: str | None) -> str | None:
    """First-author surname of a ``paper_bib_entries.authors`` string.

    Two stored shapes: ACS ``"Miller, A. B.; Jones, C."`` (surname
    before the first comma) and free-form ``"A. B. Miller and
    C. Jones"`` (last word of the first-author segment)."""
    if not authors:
        return None
    first = re.split(r";|\s+and\s+|\s*&\s*|,\s*and\s+", authors, maxsplit=1)[0].strip()
    if not first:
        return None
    if "," in first:
        surname = first.split(",", 1)[0].strip()
    else:
        surname = first.split()[-1].strip()
    return surname or None


def _author_year_index(
    entries: list[BibEntry],
) -> dict[tuple[str, int], list[BibEntry]]:
    """``(normalised first-author surname, year)`` → matching entries —
    the author-year whitelist. A candidate resolves only on exactly one
    entry (``>1`` is ambiguous → no link, spec decision)."""
    index: dict[tuple[str, int], list[BibEntry]] = {}
    for e in entries:
        surname = _first_surname(e.authors)
        if surname is None or e.year is None:
            continue
        key = (_norm_name(surname), e.year)
        index.setdefault(key, []).append(e)
    return index


def _author_year_occurrences(
    text: str, index: dict[tuple[str, int], list[BibEntry]]
) -> list[_Occurrence]:
    """Author-year candidates that uniquely match one of the paper's
    bib entries. Unmatched and ambiguous candidates yield no
    occurrence — nothing is persisted either way (slice 2's
    in-memory-only contract). ``2004a``/``2004b`` letters are ignored
    for matching: they disambiguate *within* a pair the bibliography
    itself orders, which the stored entry does not record — so a
    two-entry surname+year collision is ambiguous even with a letter."""
    out: list[_Occurrence] = []
    for m in _AUTHOR_YEAR_RE.finditer(text):
        surname = _norm_name(m.group("surname"))
        year = int(m.group("year")[:4])
        matches = index.get((surname, year))
        if not matches or len(matches) > 1:
            continue
        rest = (m.group("rest") or "").strip()
        label = " ".join(f"{m.group('surname')} {rest} {m.group('year')}".split())
        out.append(
            _Occurrence(
                pos=m.start(),
                key=("ay", surname, year),
                label=label,
                entry_id=matches[0].id,
            )
        )
    return out


# ── the chunk→paper writes ───────────────────────────────────────────


def _ordered_occurrences(
    text: str,
    marker_to_entry: dict[int, int],
    ay_index: dict[tuple[str, int], list[BibEntry]],
) -> list[tuple[int, str, int]]:
    """``(marker_order, label, entry_id)`` — all resolved citations of
    one chunk in first-occurrence order.

    Numeric and author-year occurrences merge on text position into one
    sequence; a citation repeated later keeps its first position (the
    ``key`` dedupe). Chunk markers the regex somehow misses still link
    — appended after the positioned occurrences, ordered by marker —
    so the write set is exactly the chunk's ``chunk_citations`` rows
    plus resolved author-year citations.
    """
    occurrences = _numeric_occurrences(
        text, marker_to_entry
    ) + _author_year_occurrences(text, ay_index)
    occurrences.sort(key=lambda o: o.pos)

    seen_keys: set[tuple[str, Any]] = set()
    ordered: list[tuple[int, str, int]] = []
    for occ in occurrences:
        if occ.key in seen_keys:
            continue
        seen_keys.add(occ.key)
        ordered.append((len(ordered), occ.label, occ.entry_id))

    covered = {entry_id for _, _, entry_id in ordered}
    for marker in sorted(marker_to_entry):
        entry_id = marker_to_entry[marker]
        if entry_id not in covered:
            ordered.append((len(ordered), str(marker), entry_id))
            covered.add(entry_id)
    return ordered


def ensure_chunk_cites_links(
    store: Store,
    ref_id: int,
    ord_: int,
    text: str,
    marker_to_entry: dict[int, int],
    entries: list[BibEntry],
    *,
    conn: Connection,
) -> int:
    """Ensure one chunk→paper ``cites`` link per distinct cited paper
    this chunk's markers resolve to.

    ``marker_to_entry`` is the chunk's ``chunk_citations`` rows
    (marker → ``paper_bib_entries.id``); ``entries`` the citing paper's
    full bib — it feeds both the held-target lookup and the author-year
    whitelist. Only entries with a ``held_ref_id`` link; unheld (and
    retired-target) entries write nothing, self-cites are skipped, and
    two markers resolving to the same paper collapse to the
    earlier-positioned one. Idempotent: ``add_link`` dedupes on the
    unique tuple, so a re-run inserts nothing. Returns the number of
    ``add_link`` calls made.

    ``meta.marker_order`` numbers the chunk's *resolved* citation
    occurrences, not the written links — each skip above leaves a gap
    in the ordinals a reader sees on the link rows (monotonic, not
    contiguous).
    """
    held = {
        e.id: e.held_ref_id
        for e in entries
        if e.held_ref_id is not None and e.held_ref_id != ref_id
    }
    if not held:
        return 0
    live = {
        int(r[0])
        for r in conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND retired_at IS NULL",
            (list(set(held.values())),),
        ).fetchall()
    }
    ordered = _ordered_occurrences(text, marker_to_entry, _author_year_index(entries))
    seen_dst: set[int] = set()
    ensured = 0
    for order, label, entry_id in ordered:
        dst = held.get(entry_id)
        if dst is None or dst not in live or dst in seen_dst:
            continue
        seen_dst.add(dst)
        store.add_link(
            src_ref_id=ref_id,
            src_pos=ord_,
            dst_ref_id=dst,
            relation="cites",
            set_by="system",
            meta={
                "source": _LINK_SOURCE,
                "writer": f"{_LINK_SOURCE}:{CITERES_VERSION}",
                "marker": label,
                "marker_order": order,
                "bib_entry_id": entry_id,
            },
            conn=conn,
        )
        ensured += 1
    return ensured


# ── the drain pass ───────────────────────────────────────────────────


def _claim_pending_papers(conn: Connection, *, limit: int) -> list[int]:
    """Up to ``limit`` claimable ``CITERES:pending`` paper ref_ids,
    locked for the duration of the caller's transaction (``SKIP
    LOCKED`` keeps sibling workers off the same batch — the tag flip
    to ``swept`` is the durable claim, so the lock need only span the
    write).

    The ``WHERE`` carries both gates, so unclaimable papers never take
    a batch slot: ``bib_mark`` must have swept the paper first (every
    live body chunk carries ``BIBMARK:<version>``, or the paper has no
    ``paper_bib_entries`` at all — spec ruling: the
    ``chunk_citations`` rows are the numeric whitelist and sweeping
    earlier would mint a permanent gap) and the paper must not be
    ``error:terminal`` (the retry cap). Papers carrying an ``error:*``
    tag (a previous failed attempt) sort last — the backoff: a
    once-failed paper is never re-claimed ahead of a clean pending
    one."""
    rows = conn.execute(
        """
        SELECT r.ref_id
          FROM refs r
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                  WHERE rt.ref_id = r.ref_id
                    AND t.namespace = %(ns)s AND t.value = %(pending)s
               )
           AND NOT EXISTS (
                 SELECT 1 FROM ref_tags xt JOIN tags xtg USING (tag_id)
                  WHERE xt.ref_id = r.ref_id
                    AND xtg.namespace = %(ns)s AND xtg.value = %(terminal)s
               )
           AND (
                 NOT EXISTS (
                       SELECT 1
                         FROM chunks c
                        WHERE c.ref_id = r.ref_id
                          AND c.ord >= 0
                          AND c.retired_at IS NULL
                          AND NOT EXISTS (
                                SELECT 1
                                  FROM chunk_tags ct
                                  JOIN tags bt USING (tag_id)
                                 WHERE ct.chunk_id = c.chunk_id
                                   AND bt.namespace = %(bns)s
                                   AND bt.value = %(bver)s
                              )
                     )
              OR NOT EXISTS (
                       SELECT 1 FROM paper_bib_entries pbe
                        WHERE pbe.ref_id = r.ref_id
                     )
               )
         ORDER BY EXISTS (
                 SELECT 1 FROM ref_tags et JOIN tags etg USING (tag_id)
                  WHERE et.ref_id = r.ref_id
                    AND etg.namespace = %(ns)s
                    AND etg.value LIKE %(errorlike)s
               ), r.ref_id
         LIMIT %(limit)s
           FOR UPDATE OF r SKIP LOCKED
        """,
        {
            "ns": _CITERES_NS,
            "pending": _PENDING,
            "terminal": _ERROR_TERMINAL,
            "bns": _BIBMARK_NS,
            "bver": BIBMARK_VERSION,
            "errorlike": f"{_ERROR}%",
            "limit": limit,
        },
    ).fetchall()
    return [int(r[0]) for r in rows]


def _error_stamp(conn: Connection, ref_id: int) -> str:
    """The ``CITERES`` error tag this paper's next stamp gets:
    ``error:{n}`` for the n'th committed failure below
    :data:`_MAX_ERROR_ATTEMPTS`, then ``error:terminal`` (the claim
    excludes it). Counts only committed ``error:<n>`` tags, so a
    re-stamp after a batch-level rollback re-derives the same value —
    the stamp is exactly the attempt number the failed write would
    have recorded."""
    row = conn.execute(
        "SELECT count(*) FROM ref_tags rt JOIN tags t USING (tag_id) "
        "WHERE rt.ref_id = %s AND t.namespace = %s AND t.value LIKE %s",
        (ref_id, _CITERES_NS, f"{_ERROR}:%"),
    ).fetchone()
    attempts = int(row[0]) + 1 if row else 1
    if attempts >= _MAX_ERROR_ATTEMPTS:
        return _ERROR_TERMINAL
    return f"{_ERROR}:{attempts}"


def _paper_entries(conn: Connection, ref_id: int) -> list[BibEntry]:
    rows = conn.execute(
        "SELECT id, marker, authors, year, held_ref_id "
        "FROM paper_bib_entries WHERE ref_id = %s",
        (ref_id,),
    ).fetchall()
    return [
        BibEntry(
            id=int(r[0]),
            marker=int(r[1]),
            authors=r[2],
            year=int(r[3]) if r[3] is not None else None,
            held_ref_id=int(r[4]) if r[4] is not None else None,
        )
        for r in rows
    ]


def _unswept_chunks(conn: Connection, ref_id: int) -> list[tuple[int, int, str]]:
    """``(chunk_id, ord, text)`` of the paper's body chunks not yet
    stamped ``CITERES:<version>``."""
    rows = conn.execute(
        """
        SELECT c.chunk_id, c.ord, c.text
          FROM chunks c
         WHERE c.ref_id = %s
           AND c.ord >= 0
           AND c.retired_at IS NULL
           AND NOT EXISTS (
                 SELECT 1 FROM chunk_tags ct JOIN tags t USING (tag_id)
                  WHERE ct.chunk_id = c.chunk_id
                    AND t.namespace = %s
                    AND t.value = %s
               )
         ORDER BY c.ord
        """,
        (ref_id, _CITERES_NS, CITERES_VERSION),
    ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2] or "")) for r in rows]


def _chunk_marker_map(
    conn: Connection, chunk_ids: list[int]
) -> dict[int, dict[int, int]]:
    """``chunk_id → {marker → bib_entry_id}`` from ``chunk_citations``."""
    if not chunk_ids:
        return {}
    rows = conn.execute(
        "SELECT chunk_id, marker, bib_entry_id FROM chunk_citations "
        "WHERE chunk_id = ANY(%s)",
        (chunk_ids,),
    ).fetchall()
    out: dict[int, dict[int, int]] = {}
    for chunk_id, marker, entry_id in rows:
        out.setdefault(int(chunk_id), {})[int(marker)] = int(entry_id)
    return out


def _sweep_paper(store: Store, ref_id: int, conn: Connection) -> tuple[int, int]:
    """Sweep one claimed paper inside the caller's savepoint: write its
    chunks' ``cites`` links, stamp ``CITERES:<version>`` per chunk,
    flip the paper ``pending → swept`` (``replace_prefix`` clears any
    ``CITERES:error`` an earlier failed attempt left). Returns
    ``(chunks_swept, links_ensured)``."""
    entries = _paper_entries(conn, ref_id)
    chunks = _unswept_chunks(conn, ref_id)
    marker_maps = _chunk_marker_map(conn, [c[0] for c in chunks])
    ensured = 0
    for chunk_id, ord_, text in chunks:
        ensured += ensure_chunk_cites_links(
            store,
            ref_id,
            ord_,
            text,
            marker_maps.get(chunk_id, {}),
            entries,
            conn=conn,
        )
        store.add_tag(
            ref_id,
            Tag.closed(_CITERES_NS, CITERES_VERSION),
            set_by="system",
            pos=ord_,
            conn=conn,
        )
    store.add_tag(
        ref_id,
        Tag.closed(_CITERES_NS, _SWEPT),
        set_by="system",
        replace_prefix=True,
        conn=conn,
    )
    return len(chunks), ensured


def run_cite_resolve_pass(
    store: Store, *, batch_size: int | None = None
) -> dict[str, int]:
    """One pass: claim ``CITERES:pending`` papers, write their chunks'
    ``cites`` links, stamp ``CITERES:<version>`` per chunk, flip each
    paper to ``CITERES:swept``.

    ``batch_size`` bounds *papers* claimed (default 8 — a landmark
    paper's chunks all process in its own savepoint, so the claim, not
    the chunk count, is the batch unit). Each paper sweeps inside its
    own ``conn.transaction()`` savepoint (spec ruling): a failing
    paper rolls back only itself — links, chunk stamps and the flip
    alike — is stamped ``CITERES:error:<n>`` (the durable record; the
    claim sorts it last next pass — the backoff — and at
    :data:`_MAX_ERROR_ATTEMPTS` ``error:terminal`` stops it being
    claimed at all), and the batch still commits the survivors. Only a
    claim- or commit-level failure rolls the whole batch back — and
    takes the batch's error stamps with it, so they are re-stamped in
    a fresh transaction (the stamp counts committed ``error:<n>``
    tags, so the re-stamp re-derives the same value).

    Returns ``{papers_swept, chunks_swept, links_ensured, failed}`` —
    ``failed`` counts each failed paper once (a batch-level rollback
    adds one only when no per-paper failure already accounted for it).
    """
    papers_swept = 0
    chunks_swept = 0
    links_ensured = 0
    failed_ids: list[int] = []
    batch_failed = False
    try:
        with store.pool.connection() as conn:
            claimed = _claim_pending_papers(conn, limit=batch_size or 8)
            for ref_id in claimed:
                try:
                    with conn.transaction():  # savepoint: isolate this paper
                        swept, ensured = _sweep_paper(store, ref_id, conn)
                except Exception:
                    stamp = _error_stamp(conn, ref_id)
                    if stamp == _ERROR_TERMINAL:
                        log.warning(
                            "cite_resolve: paper %s failed %d sweeps -- "
                            "tagged %s:%s, no longer claimed",
                            ref_id,
                            _MAX_ERROR_ATTEMPTS,
                            _CITERES_NS,
                            _ERROR_TERMINAL,
                            exc_info=True,
                        )
                    else:
                        log.warning(
                            "cite_resolve: paper %s failed -- rolled "
                            "back, tagged %s:%s, backed off behind "
                            "clean papers",
                            ref_id,
                            _CITERES_NS,
                            stamp,
                            exc_info=True,
                        )
                    failed_ids.append(ref_id)
                    store.add_tag(
                        ref_id,
                        Tag.closed(_CITERES_NS, stamp),
                        set_by="system",
                        conn=conn,
                    )
                    continue
                papers_swept += 1
                chunks_swept += swept
                links_ensured += ensured
            conn.commit()
    except Exception:
        batch_failed = True
        log.warning(
            "cite_resolve: sweep batch failed -- rolled back, retried next pass",
            exc_info=True,
        )
        # The rollback took this batch's error stamps with it —
        # re-stamp them so the backoff/terminal record survives.
        if failed_ids:
            try:
                with store.pool.connection() as conn:
                    for ref_id in failed_ids:
                        store.add_tag(
                            ref_id,
                            Tag.closed(_CITERES_NS, _error_stamp(conn, ref_id)),
                            set_by="system",
                            conn=conn,
                        )
                    conn.commit()
            except Exception:
                log.warning(
                    "cite_resolve: could not re-stamp error tags",
                    exc_info=True,
                )
        papers_swept = 0
        chunks_swept = 0
        links_ensured = 0

    return {
        "papers_swept": papers_swept,
        "chunks_swept": chunks_swept,
        "links_ensured": links_ensured,
        "failed": max(len(failed_ids), 1) if batch_failed else len(failed_ids),
    }


__all__ = [
    "CITERES_VERSION",
    "BibEntry",
    "ensure_chunk_cites_links",
    "run_cite_resolve_pass",
]
