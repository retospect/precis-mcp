"""cite_resolve — chunk→paper ``cites`` links from in-text citation
markers (gr513768, docs/backlog/citation-resolution-skill.md slices 1–2, 4).

Where ``bib_mark`` (``workers/bib_mark.py``) records *which* inline
``[N]`` markers a body chunk uses (``chunk_citations``) and
``bib_parse.ensure_bib_cites_links`` mints *paper-level* ``cites``
edges from matched bib entries, this pass extends the same write to
chunk scope on the citing side: a chunk that says "as shown before
[32]" gets a chunk→paper ``cites`` link to the paper its bib entry 32
resolved to. With reference chunks permanently unembedded (gr513749),
the marker is the reader's first hop to the cited work.

Two deterministic layers (no LLM), one OpenAlex leg, one LLM layer:

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
- **OpenAlex hop** (slice 4): an entry the matcher left DOI-less and
  unheld resolves against the citing paper's cached
  ``meta.openalex.referenced_works`` (held refs, matched on
  title/year), else a live :func:`search_openalex_works` title lookup
  through the shared ``openalex`` limiter. A held hit writes
  ``held_ref_id`` (the sweep then links it); an unheld hit writes only
  ``openalex_id`` — the stub *request*, which the gr513755 trickle
  mints under its own budget (this path never calls
  ``upsert_stub_paper``); a miss stamps ``match_conf``
  :data:`_MATCH_CONF_OPENALEX_MISS` on rows already decided. A limiter
  refusal defers the paper — it is never error-stamped.
- **Passage** (slice 6, LLM): ``PRECIS_CITE_RESOLVE_PASSAGE_ENABLED``
  lights :func:`run_passage_sweep` at the end of the pass — each minted
  ref-level link's citing sentence is embedded, the cited paper's top-5
  embedded body chunks go to ``cite_resolve:passage`` (the BIG tier
  Reto steered to castor's local lane, same as ``chase:verify``), and a
  pick upgrades the link to chunk→chunk while ``none`` memoizes the
  pair (``links.meta.passage``) so it is not retried.

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
``CITERES:pending`` — :func:`mark_paper_for_cite_resolve`, the
first-``get`` access mark (slice 5), is the only writer — and only once
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
``CITERES:pending → CITERES:swept``.

**Claim = lease, not lock.** Since slice 4 the pass spends network
per paper, so a transaction can never span the work: the claim
transaction commits after stamping each claimed paper's
``ref_lease`` attempt lease (``CITERESATTEMPT`` — the durable claim a
sibling's claim ``WHERE`` respects, mirroring ``bib_parse``'s
documented pattern), then each paper resolves and sweeps in its own
short transactions. Success clears the lease in the same write as the
``pending → swept`` flip; failure leaves it to brake re-billing for
:data:`ref_lease.ATTEMPT_COOLDOWN_MIN` (self-healing on a crash), and
also stamps ``CITERES:error:<n>`` — the counted backoff record; the
claim sorts error-tagged papers last and at
:data:`_MAX_ERROR_ATTEMPTS` ``error:terminal`` stops the paper being
claimed at all (the ``pending → swept`` ``replace_prefix`` flip
clears every ``CITERES`` tag, so a recovered paper — say after a
``CITERES_VERSION`` bump by hand — loses its error history too).
DEFAULT-OFF: registered via ``enable_env`` with no
``default_profiles``, so a ``service_config`` row (or ``--only
cite_resolve``) is the only thing that runs it.
"""

from __future__ import annotations

import logging
import os
import re
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from psycopg import Connection
from psycopg.types.json import Jsonb

from precis.ingest import openalex_meta
from precis.store.types import Tag
from precis.utils.db_retry import retry_locked
from precis.utils.rate_limit import RateLimited
from precis.workers import ref_lease
from precis.workers.bib_mark import (
    _BIBMARK_NS,
    _CITE_GROUP_RE,
    _MAX_RANGE_SPAN,
    _RANGE_RE,
    BIBMARK_VERSION,
)
from precis.workers.bib_parse import _held_ref_for_doi, ensure_bib_cites_links

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

#: ``paper_bib_entries.match_conf`` sentinel for "the OpenAlex hop ran
#: and resolved nothing" — the stamped miss (spec slice 4). It is only
#: ever written over a non-NULL ``match_conf`` (a decided row): a NULL
#: row is crossref-undecided and stays NULL so
#: ``bib_parse.run_bib_rematch_sweep`` keeps its retry; its OA outcome
#: is simply re-derived on a re-sweep.
_MATCH_CONF_OPENALEX_MISS = -2.0

#: Live-lookup cap per paper per sweep — a pathology bibliography of
#: DOI-less entries can't burn unbounded shared-limiter tokens inside
#: one paper's leg; the remainder resolves on later sweeps.
_OA_SEARCH_MAX_PER_PAPER = 25

#: ``raw_text`` slice sent to ``/works?search=`` — a citation line is
#: short; the cap just keeps a pathological blob off the wire.
_OA_QUERY_MAX_CHARS = 300


@dataclass(frozen=True, slots=True)
class BibEntry:
    """The columns of one ``paper_bib_entries`` row this pass reads."""

    id: int
    marker: int
    authors: str | None
    year: int | None
    held_ref_id: int | None
    raw_text: str = ""
    doi: str | None = None
    openalex_id: str | None = None
    source: str = "bib_parse"
    match_conf: float | None = None


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


# ── the access mark (slice 5) ────────────────────────────────────────


def cite_resolve_enabled() -> bool:
    """``PRECIS_CITE_RESOLVE_ENABLED`` — the read-path mark's dark gate
    (default OFF).

    The pass itself is gated per cycle by ``service_config`` (§L: the
    env no longer feeds the pass default); this flag gates only the
    mark, mirroring ``inbound_chase_enabled`` — no tag write while the
    feature is dark. Lighting the feature is therefore two deliberate
    acts: the env on the MCP server (marks start landing) and a
    ``service prio`` row (the pass claims them).
    """
    return bool(int(os.environ.get("PRECIS_CITE_RESOLVE_ENABLED", "0") or "0"))


def mark_paper_for_cite_resolve(store: Store, ref: Any) -> None:
    """Mark paper ``ref`` ``CITERES:pending`` the first time it is read.

    No-op while the feature is dark, when ``ref`` isn't a paper, or when
    the paper already carries ``CITERES:pending`` or ``CITERES:swept``
    — the mark-once contract. Called from ``PaperHandler.get`` beside
    ``inbound_chase.mark_paper_active`` — the same "read via get is the
    engagement event" pick. The pass's claim query is the mark's only
    reader, so while the pass stays dark the marks just accumulate as
    the access record they were filed to be.
    """
    if not cite_resolve_enabled():
        return
    if getattr(ref, "kind", None) != "paper":
        return
    if store.has_tag(ref.id, _CITERES_NS, _PENDING) or store.has_tag(
        ref.id, _CITERES_NS, _SWEPT
    ):
        return
    store.add_tag(ref.id, Tag.closed(_CITERES_NS, _PENDING), set_by="system")


# ── the drain pass ───────────────────────────────────────────────────


def _claim_pending_papers(conn: Connection, *, limit: int) -> list[int]:
    """Up to ``limit`` claimable ``CITERES:pending`` paper ref_ids,
    locked for the duration of the caller's transaction (``SKIP
    LOCKED`` keeps sibling claimants off the same batch while their
    attempt leases are stamped in that same transaction — the lease,
    not the lock, is the durable claim once the lock releases at
    commit).

    The ``WHERE`` carries every gate, so unclaimable papers never take
    a batch slot: ``bib_mark`` must have swept the paper first (every
    live body chunk carries ``BIBMARK:<version>``, or the paper has no
    ``paper_bib_entries`` at all — spec ruling: the
    ``chunk_citations`` rows are the numeric whitelist and sweeping
    earlier would mint a permanent gap), the paper must not be
    ``error:terminal`` (the retry cap), and it must not sit under an
    unexpired ``ref_lease`` attempt lease (a sibling's in-flight sweep
    or a braked failure). Papers carrying an ``error:*``
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
         """
        + ref_lease.exclude_clause("r.ref_id", "attempt_ns")
        + """
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
            "attempt_ns": ref_lease.attempt_ns(_CITERES_NS),
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
        "SELECT id, marker, authors, year, held_ref_id, raw_text, doi, "
        "openalex_id, source, match_conf "
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
            raw_text=str(r[5] or ""),
            doi=r[6],
            openalex_id=r[7],
            source=str(r[8] or "bib_parse"),
            match_conf=float(r[9]) if r[9] is not None else None,
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


# ── the OpenAlex leg (slice 4) ───────────────────────────────────────

_TITLE_NORM_RE = re.compile(r"[^a-z0-9]+")


def _norm_title(text: str) -> str:
    """Title compare key: NFKD-folded, lowercased, alphanumeric words —
    a title quoted verbatim into a citation line survives markup,
    case, accents and punctuation differences."""
    folded = (
        unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    )
    return _TITLE_NORM_RE.sub(" ", folded).strip()


def _work_first_surname(work: dict[str, Any]) -> str | None:
    """Normalised first-author surname of an OpenAlex work object, or
    ``None`` when the payload carries no authorships."""
    authorships = work.get("authorships")
    if not isinstance(authorships, list) or not authorships:
        return None
    first = authorships[0]
    name = str(((first or {}).get("author") or {}).get("display_name") or "")
    name = name.split()[-1].strip() if name.split() else ""
    return _norm_name(name) if name else None


def _entry_matches(
    entry: BibEntry,
    norm_raw: str,
    *,
    title: str | None,
    year: int | None,
    surname: str | None,
) -> bool:
    """The conservative resolve gate shared by the cached-refs and
    live-lookup legs: the candidate title must appear verbatim
    (post-normalisation) inside the citation's raw text, the year must
    equal the entry's when both sides carry one, and a first-author
    surname parseable on both sides must agree. A title-less citation
    style simply never matches — the miss is stamped rather than a
    wrong link written (spec: unmatched is acceptable, wrong is not).
    """
    norm_title = _norm_title(title or "")
    if not norm_title or norm_title not in norm_raw:
        return False
    if entry.year is not None and year is not None and entry.year != year:
        return False
    entry_surname = _first_surname(entry.authors)
    if entry_surname is not None and surname is not None:
        if _norm_name(entry_surname) != surname:
            return False
    return True


def _held_candidates(
    conn: Connection, ref_id: int
) -> list[tuple[int, str, str | None, int | None]]:
    """``(held_ref_id, w_id, title, year)`` — the citing paper's cached
    ``meta.openalex.referenced_works`` resolved to live held refs (the
    ``openalex`` identifier kind, lowercased probes per the
    ``ref_identifiers`` canonical-case rule)."""
    row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)).fetchone()
    meta = (row[0] if row else None) or {}
    oa = meta.get("openalex") if isinstance(meta, dict) else None
    w_ids = [str(w) for w in ((oa or {}).get("referenced_works") or []) if w]
    if not w_ids:
        return []
    w_form = {w.lower(): w for w in w_ids}
    rows = conn.execute(
        "SELECT DISTINCT ON (lower(ri.id_value)) "
        "lower(ri.id_value), ri.ref_id, r.title, r.year "
        "FROM ref_identifiers ri "
        "JOIN refs r ON r.ref_id = ri.ref_id "
        "WHERE ri.id_kind = 'openalex' "
        "AND lower(ri.id_value) = ANY(%s) "
        "AND r.kind = 'paper' AND r.retired_at IS NULL "
        "ORDER BY lower(ri.id_value), ri.ref_id",
        (list(w_form),),
    ).fetchall()
    return [
        (
            int(r[1]),
            w_form.get(str(r[0]), str(r[0])),
            r[2],
            int(r[3]) if r[3] is not None else None,
        )
        for r in rows
    ]


def _held_for_openalex_id(conn: Connection, openalex_id: str) -> int | None:
    """Live held paper carrying ``openalex_id`` (``ref_identifiers``,
    case-insensitive probe), else ``None``."""
    row = conn.execute(
        "SELECT ri.ref_id FROM ref_identifiers ri "
        "JOIN refs r ON r.ref_id = ri.ref_id "
        "WHERE ri.id_kind = 'openalex' AND lower(ri.id_value) = %s "
        "AND r.retired_at IS NULL",
        (openalex_id.lower(),),
    ).fetchone()
    return int(row[0]) if row else None


def _write_oa_outcome(
    store: Store,
    ref_id: int,
    entry_id: int,
    *,
    openalex_id: str | None = None,
    doi: str | None = None,
    held_ref_id: int | None = None,
    missed: bool = False,
) -> None:
    """One entry's OA-leg outcome in its own short transaction
    (:func:`retry_locked`-wrapped, the ``bib_parse`` match-write idiom —
    the leg holds no connection across its lookups, so a sibling's
    write can collide for an instant).

    ``missed=True`` stamps :data:`_MATCH_CONF_OPENALEX_MISS` — but only
    on a row whose ``match_conf`` is already non-NULL (the WHERE keeps
    a crossref-undecided row NULL so the rematch sweep still owns it).
    A held target also mints the paper-level ``cites`` edge through the
    shared gr513755 writer, exactly as ``bib_parse``'s match write
    does.
    """

    def _write() -> None:
        with store.pool.connection() as conn:
            if missed:
                conn.execute(
                    "UPDATE paper_bib_entries SET match_conf = %s "
                    "WHERE id = %s AND match_conf IS NOT NULL",
                    (_MATCH_CONF_OPENALEX_MISS, entry_id),
                )
            else:
                conn.execute(
                    "UPDATE paper_bib_entries "
                    "SET openalex_id = COALESCE(openalex_id, %s), "
                    "    doi = COALESCE(doi, %s), "
                    "    held_ref_id = %s "
                    "WHERE id = %s",
                    (openalex_id, doi, held_ref_id, entry_id),
                )
            if held_ref_id is not None:
                ensure_bib_cites_links(
                    store, ref_id, [(entry_id, held_ref_id)], conn=conn
                )
            conn.commit()

    retry_locked(_write, label=f"cite_resolve oa entry_id={entry_id}")


def _resolve_paper_openalex(
    store: Store, ref_id: int, *, email: str = ""
) -> dict[str, int]:
    """Resolve one paper's unheld bib entries through the OpenAlex leg
    (spec slice 4). Holds no connection across a lookup: each leg
    phase reads in its own short transaction, each outcome writes in
    its own. Returns ``{resolved, missed, looked_up}`` —
    ``looked_up`` counts the live ``search_openalex_works`` calls.
    :class:`RateLimited` propagates so the pass can defer the paper
    unstamped; it is never an error.
    """
    with store.pool.connection() as conn:
        unresolved = [e for e in _paper_entries(conn, ref_id) if e.held_ref_id is None]
        held_cands = _held_candidates(conn, ref_id) if unresolved else []
        conn.commit()

    resolved = missed = looked_up = 0
    for entry in unresolved:
        # Identifier-carrying rows (a ``doi`` from the matcher, an
        # ``openalex_id`` from the oa_refs backfill or an earlier hit)
        # need only a local re-check — the target may have become held
        # since the identifier was recorded.
        if entry.doi or entry.openalex_id:
            with store.pool.connection() as conn:
                held = _held_ref_for_doi(conn, entry.doi) if entry.doi else None
                if held is None and entry.openalex_id:
                    held = _held_for_openalex_id(conn, entry.openalex_id)
                conn.commit()
            if held is not None and held != ref_id:
                _write_oa_outcome(
                    store,
                    ref_id,
                    entry.id,
                    openalex_id=entry.openalex_id,
                    doi=entry.doi,
                    held_ref_id=held,
                )
                resolved += 1
            continue

        if entry.source != "bib_parse" or (
            entry.match_conf is not None
            and entry.match_conf == _MATCH_CONF_OPENALEX_MISS
        ):
            continue

        norm_raw = _norm_title(entry.raw_text)

        # Step 1 — the citing paper's cached ``referenced_works`` ∩ held
        # refs, matched on title/year (zero network).
        hits = [
            c
            for c in held_cands
            if c[0] != ref_id
            and _entry_matches(entry, norm_raw, title=c[2], year=c[3], surname=None)
        ]
        if len(hits) == 1:
            _write_oa_outcome(
                store,
                ref_id,
                entry.id,
                openalex_id=hits[0][1],
                held_ref_id=hits[0][0],
            )
            resolved += 1
            continue
        if len(hits) > 1:
            _write_oa_outcome(store, ref_id, entry.id, missed=True)
            missed += 1
            continue

        # Step 2 — the live title lookup through the existing client.
        if looked_up >= _OA_SEARCH_MAX_PER_PAPER:
            continue  # unstamped — a later sweep retries it
        works = openalex_meta.search_openalex_works(
            entry.raw_text[:_OA_QUERY_MAX_CHARS], year=entry.year, email=email
        )
        looked_up += 1
        w_hits = [
            w
            for w in works
            if _entry_matches(
                entry,
                norm_raw,
                title=str(w.get("title") or w.get("display_name") or ""),
                year=w.get("publication_year"),
                surname=_work_first_surname(w),
            )
        ]
        if len(w_hits) != 1:
            _write_oa_outcome(store, ref_id, entry.id, missed=True)
            missed += 1
            continue

        work = w_hits[0]
        w_id = str(work.get("id") or "").rstrip("/").rsplit("/", 1)[-1]
        w_doi = str(work.get("doi") or "").removeprefix("https://doi.org/").lower()
        with store.pool.connection() as conn:
            held = _held_ref_for_doi(conn, w_doi) if w_doi else None
            if held is None and w_id:
                held = _held_for_openalex_id(conn, w_id)
            conn.commit()
        _write_oa_outcome(
            store,
            ref_id,
            entry.id,
            openalex_id=w_id or None,
            doi=w_doi or None,
            held_ref_id=held if held != ref_id else None,
        )
        resolved += 1
    return {"resolved": resolved, "missed": missed, "looked_up": looked_up}


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


# ── the passage layer (slice 6) ──────────────────────────────────────

#: Links one passage sweep attempts per pass — each is one routed LLM
#: call, so this bounds the layer's spend per cycle the way
#: ``_OA_SEARCH_MAX_PER_PAPER`` bounds the OpenAlex leg.
_PASSAGE_MAX_PER_PASS = 8

#: Candidate body chunks of the cited paper a verdict chooses among
#: (spec: "top-5 embedded chunks").
_PASSAGE_CANDIDATES = 5

#: Char caps for the prompt — the citing context window and each
#: candidate chunk's text.
_PASSAGE_CITE_MAX_CHARS = 1200
_PASSAGE_CHUNK_MAX_CHARS = 1500

_PASSAGE_PROMPT = """A citation marker {marker} in one paper cites another paper. Decide
which of the candidate passages from the cited paper the citation most
plausibly points at — or that none of them is a plausible source.

Citing passage (the marker {marker} appears in it):
<citing>
{citing}
</citing>

Candidate passages of the cited paper:
{candidates}

Reply with exactly one JSON object and no other text:
{{"pick": <1-{n}> or null, "caveat": <short note or null>}}
- "pick" is the 1-based index of the passage the citation supports, or
  null when no candidate is a plausible source.
- "caveat" is null normally; set a short note like "cited passage does
  not support the claim" when the best passage contradicts the citing
  sentence rather than backing it."""


def passage_enabled() -> bool:
    """``PRECIS_CITE_RESOLVE_PASSAGE_ENABLED`` — the LLM layer's own dark
    gate (default OFF).

    The spec's slice-5 contract: "deterministic layer first, LLM layer
    only when its flag is on". Independent of
    :func:`cite_resolve_enabled` (the read-path mark) and of the pass's
    ``service_config`` lighting — lighting the feature is three
    deliberate acts: the env on the MCP server (marks), a
    ``service prio`` row (the pass claims), and this env on the worker
    (the passage pick spends a routed call per link)."""
    return bool(int(os.environ.get("PRECIS_CITE_RESOLVE_PASSAGE_ENABLED", "0") or "0"))


def _claim_passage_links(conn: Connection, *, limit: int) -> list[dict[str, Any]]:
    """Up to ``limit`` ref-level ``cites`` links this pass minted that a
    passage verdict has never touched.

    A link is a candidate only while ``meta.passage`` is absent — the
    done-marker (``verdict='chunk'`` on upgrade, ``'none'`` when the
    model declines, ``'skipped'`` when the pair can't be judged this
    version) — and only when the cited paper carries at least one
    embedded body chunk under the default embedder: a cited stub has no
    passage to find, and it becomes a candidate on its own when its
    body lands. ``FOR UPDATE OF l SKIP LOCKED`` keeps a sibling's sweep
    off the same batch; the lock releases at commit, well before the
    LLM calls — the ``meta.passage`` write is the durable claim.
    """
    rows = conn.execute(
        """
        SELECT l.link_id, l.src_ref_id, l.src_chunk_id, l.dst_ref_id,
               l.meta->>'marker' AS marker, sc.text AS citing_text
          FROM links l
          JOIN chunks sc ON sc.chunk_id = l.src_chunk_id
          JOIN refs dr ON dr.ref_id = l.dst_ref_id
         WHERE l.relation = 'cites'
           AND l.src_chunk_id IS NOT NULL
           AND l.dst_chunk_id IS NULL
           AND l.meta->>'source' = %(source)s
           AND NOT (l.meta ? 'passage')
           AND dr.kind = 'paper' AND dr.retired_at IS NULL
           AND sc.retired_at IS NULL
           AND EXISTS (
                 SELECT 1 FROM chunks dc
                  JOIN chunk_embeddings ce ON ce.chunk_id = dc.chunk_id
                  JOIN embedders de ON de.name = ce.embedder AND de.is_default
                 WHERE dc.ref_id = l.dst_ref_id
                   AND dc.ord >= 0 AND dc.retired_at IS NULL
                   AND ce.status = 'ok' AND ce.vector IS NOT NULL
               )
         ORDER BY l.link_id
         LIMIT %(limit)s
           FOR UPDATE OF l SKIP LOCKED
        """,
        {"source": _LINK_SOURCE, "limit": limit},
    ).fetchall()
    return [
        {
            "link_id": int(r[0]),
            "src_ref_id": int(r[1]),
            "src_chunk_id": int(r[2]),
            "dst_ref_id": int(r[3]),
            "marker": str(r[4] or ""),
            "citing_text": str(r[5] or ""),
        }
        for r in rows
    ]


def _citing_passage(text: str, marker: str) -> str:
    """The window of the citing chunk around the marker — the "citing
    sentence" the verdict embeds and the model reads.

    Numeric labels (``"32"``, ``"[3-5]"``, ``"[7,9]"``) look for a
    bracket group containing one of the label's numbers; author-year
    labels look for the surname. A window of ±600 chars around the
    first hit is the sentence-plus-context the passage pick needs;
    nothing found → the chunk head, capped the same.
    """
    pos = -1
    if marker.startswith("[") or marker.isdigit():
        for m in _CITE_GROUP_RE.finditer(text):
            inner = set(re.findall(r"\d+", m.group(0)))
            if inner & set(re.findall(r"\d+", marker)):
                pos = m.start()
                break
    else:
        surname = marker.split()[0] if marker.split() else ""
        if surname:
            hit = re.search(re.escape(surname), text, re.IGNORECASE)
            if hit is not None:
                pos = hit.start()
    half = _PASSAGE_CITE_MAX_CHARS // 2
    if pos < 0:
        return text[:_PASSAGE_CITE_MAX_CHARS]
    start = max(0, pos - half)
    return text[start : pos + half]


def _passage_candidates(
    conn: Connection, dst_ref_id: int, query_vec: list[float]
) -> list[tuple[int, int, str]]:
    """``(chunk_id, ord, text)`` — the cited paper's top-``_PASSAGE_
    CANDIDATES`` embedded body chunks nearest the citing passage (the
    semantic-search-scoped-to-one-paper leg; body chunks are embedded,
    reference chunks are not — gr513749)."""
    from pgvector import Vector

    rows = conn.execute(
        """
        SELECT c.chunk_id, c.ord, c.text
          FROM chunks c
          JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
          JOIN embedders e ON e.name = ce.embedder AND e.is_default
         WHERE c.ref_id = %(ref)s
           AND c.ord >= 0 AND c.retired_at IS NULL
           AND ce.status = 'ok' AND ce.vector IS NOT NULL
         ORDER BY ce.vector <=> %(qv)s::vector
         LIMIT %(limit)s
        """,
        {"ref": dst_ref_id, "qv": Vector(query_vec), "limit": _PASSAGE_CANDIDATES},
    ).fetchall()
    return [(int(r[0]), int(r[1]), str(r[2] or "")) for r in rows]


def _passage_verdict(
    marker: str, citing: str, candidates: list[tuple[int, int, str]]
) -> tuple[int | None, str | None, str | None]:
    """``(pick_index_0based | None, caveat, model)`` — the small-model
    call behind ``cite_resolve:passage`` (tier BIG; spec slice 6).

    The caller splits two ``pick_index=None`` cases on ``model``: a
    genuine verdict keeps the answering model's name (``(None, caveat,
    model)`` = "no candidate supports this" → memo ``'none'``), while an
    unusable answer — transport error, unparseable or out-of-range —
    returns ``(None, None, None)`` and the pair is left un-memoed so a
    later sweep retries it."""
    from precis.utils.llm.router import LlmRequest, Tier, route

    cand_block = "\n\n".join(
        f"[{i + 1}] (ord {ord_}) {text[:_PASSAGE_CHUNK_MAX_CHARS]}"
        for i, (_cid, ord_, text) in enumerate(candidates)
    )
    res = route(
        LlmRequest(
            tier=Tier.BIG,
            prompt=_PASSAGE_PROMPT.format(
                marker=marker or "(?)",
                citing=citing[:_PASSAGE_CITE_MAX_CHARS],
                candidates=cand_block,
                n=len(candidates),
            ),
            source="cite_resolve:passage",
            max_tokens=400,
        )
    )
    if res.error or res.data is None or not isinstance(res.data, dict):
        if res.error:
            log.warning("cite_resolve: passage verdict failed: %s", res.error)
        return None, None, None
    pick = res.data.get("pick")
    caveat = res.data.get("caveat")
    model = getattr(res, "model", None)
    if pick is None:
        return None, (str(caveat) if caveat else None), model
    try:
        idx = int(pick) - 1
    except (TypeError, ValueError):
        return None, None, None
    if not 0 <= idx < len(candidates):
        return None, None, None
    return idx, (str(caveat) if caveat else None), model


def _write_passage_outcome(
    conn: Connection,
    link: dict[str, Any],
    *,
    pick_chunk_id: int | None,
    pick_ord: int | None,
    caveat: str | None,
    model: str | None,
) -> str:
    """Apply one verdict inside the caller's transaction; returns
    ``'chunk'`` (upgraded), ``'none'`` (declined + memoed) or
    ``'skipped'`` (a sibling already set ``dst_chunk_id`` — the claim
    predicate itself excludes the row now, so nothing is written).

    Upgrade = move the minted ref-level row onto the chosen dst chunk
    (``UPDATE dst_chunk_id``) — unless a chunk→chunk row for that exact
    tuple already exists (an agent's by-hand link, or a later manual
    write), in which case the verdict merges onto it and the ref-level
    row is deleted: the chunk edge subsumes it. Either way the
    ``meta.passage`` annotation is the ruling's writer record — model,
    pass version, and one ``why`` sentence — plus the caveat when the
    model flagged non-support."""
    meta: dict[str, Any] = {
        "v": CITERES_VERSION,
        "writer": f"{_LINK_SOURCE}:passage:{CITERES_VERSION}",
        "model": model,
        "why": (
            f"marker {link['marker'] or '?'} resolved to the cited paper's "
            f"ord {pick_ord}"
            if pick_chunk_id is not None
            else f"no candidate passage of the cited paper backs marker "
            f"{link['marker'] or '?'}"
        ),
    }
    if caveat:
        meta["caveat"] = caveat
    if pick_chunk_id is not None:
        meta["verdict"] = "chunk"
        dup = conn.execute(
            "SELECT link_id FROM links WHERE src_ref_id = %(s)s "
            "AND src_chunk_id = %(sc)s AND dst_ref_id = %(d)s "
            "AND dst_chunk_id = %(dc)s AND relation = 'cites' LIMIT 1",
            {
                "s": link["src_ref_id"],
                "sc": link["src_chunk_id"],
                "d": link["dst_ref_id"],
                "dc": pick_chunk_id,
            },
        ).fetchone()
        if dup is not None:
            conn.execute(
                "UPDATE links SET meta = meta || %(m)s::jsonb WHERE link_id = %(id)s",
                {"m": Jsonb({"passage": meta}), "id": int(dup[0])},
            )
            conn.execute(
                "DELETE FROM links WHERE link_id = %(id)s",
                {"id": link["link_id"]},
            )
            return "chunk"
        cur = conn.execute(
            "UPDATE links SET dst_chunk_id = %(dc)s, "
            "meta = meta || %(m)s::jsonb WHERE link_id = %(id)s "
            "AND dst_chunk_id IS NULL",
            {"dc": pick_chunk_id, "m": Jsonb({"passage": meta}), "id": link["link_id"]},
        )
        if cur.rowcount == 0:
            # A sibling upgraded the row between claim and write — the
            # claim's ``dst_chunk_id IS NULL`` predicate already keeps it
            # out of future batches, so no memo is needed.
            return "skipped"
        return "chunk"
    conn.execute(
        "UPDATE links SET meta = meta || %(m)s::jsonb WHERE link_id = %(id)s",
        {"m": Jsonb({"passage": {**meta, "verdict": "none"}}), "id": link["link_id"]},
    )
    return "none"


def run_passage_sweep(
    store: Store, embedder: Any, *, limit: int = _PASSAGE_MAX_PER_PASS
) -> dict[str, int]:
    """One bounded sweep of the passage layer (spec slice 6): claim
    ref-level ``cites`` links this pass minted, ask the model which
    embedded body chunk of the cited paper each citing marker supports,
    and upgrade the link to chunk→chunk — or memo ``verdict='none'`` so
    the pair is not retried.

    ``embedder is None`` no-ops the sweep (mirrors ``conflict_search``'s
    embedder-unavailable degrade): a ref-level link is already a good
    landing, the passage upgrade is a bonus. Each link's embed + call +
    write runs with no connection held across the model hop — the claim
    transaction commits first (the lease idiom the OA leg uses), and a
    pair whose embed or call fails is simply not memoed, so it retries
    next sweep.

    Returns ``{claimed, chunk, none, skipped, failed}``."""
    out = {"claimed": 0, "chunk": 0, "none": 0, "skipped": 0, "failed": 0}
    if embedder is None:
        log.warning("cite_resolve: passage sweep no-ops — no embedder wired")
        return out
    with store.pool.connection() as conn:
        links = _claim_passage_links(conn, limit=limit)
        conn.commit()
    out["claimed"] = len(links)
    for link in links:
        try:
            citing = _citing_passage(link["citing_text"], link["marker"])
            query_vec = embedder.embed_one(citing)
            with store.pool.connection() as conn:
                candidates = _passage_candidates(conn, link["dst_ref_id"], query_vec)
                conn.commit()
            if not candidates:
                continue  # dst lost its embeddings — retry next sweep
            idx, caveat, model = _passage_verdict(link["marker"], citing, candidates)
            if model is None and idx is None:
                out["failed"] += 1
                continue
            pick_id = candidates[idx][0] if idx is not None else None
            pick_ord = candidates[idx][1] if idx is not None else None
            outcome = "none"
            with store.pool.connection() as conn:
                with conn.transaction():
                    outcome = _write_passage_outcome(
                        conn,
                        link,
                        pick_chunk_id=pick_id,
                        pick_ord=pick_ord,
                        caveat=caveat,
                        model=model,
                    )
            out[outcome] += 1
        except Exception:
            log.warning(
                "cite_resolve: passage verdict failed for link %s",
                link["link_id"],
                exc_info=True,
            )
            out["failed"] += 1
    return out


# ── the on-request door (slice 7) ────────────────────────────────────


def _ambiguous_author_year_count(
    text: str, index: dict[tuple[str, int], list[BibEntry]]
) -> int:
    """Author-year candidates dropped for matching **2+** bib entries —
    the job-report's ``ambiguous`` leg. Zero-match candidates are not
    counted: per the whitelist rule they were never citations of *this*
    paper's bibliography, so calling them ambiguous would inflate the
    report with plain prose years ("in 2004 …")."""
    return sum(
        1
        for m in _AUTHOR_YEAR_RE.finditer(text)
        if len(
            index.get((_norm_name(m.group("surname")), int(m.group("year")[:4])), [])
        )
        > 1
    )


def _paper_outcome_counts(conn: Connection, ref_id: int) -> dict[str, int]:
    """``{resolved, ambiguous, unheld}`` over one paper's body chunks —
    the per-paper count the ``cite_resolve`` job finishes with (spec
    slice 7's acceptance). ``resolved`` = resolved citation occurrences
    whose entry names a live held paper; ``unheld`` = occurrences whose
    entry has no held target (stub-requested or unresolved); ``ambiguous``
    = author-year candidates the whitelist dropped for matching several
    entries."""
    entries = _paper_entries(conn, ref_id)
    ay_index = _author_year_index(entries)
    held = {
        e.id: e.held_ref_id
        for e in entries
        if e.held_ref_id is not None and e.held_ref_id != ref_id
    }
    chunks = [
        (int(r[0]), int(r[1]), str(r[2] or ""))
        for r in conn.execute(
            "SELECT chunk_id, ord, text FROM chunks WHERE ref_id = %s "
            "AND ord >= 0 AND retired_at IS NULL ORDER BY ord",
            (ref_id,),
        ).fetchall()
    ]
    if not chunks or not held:
        return {"resolved": 0, "ambiguous": 0, "unheld": 0}
    live = {
        int(r[0])
        for r in conn.execute(
            "SELECT ref_id FROM refs WHERE ref_id = ANY(%s) AND retired_at IS NULL",
            (list(set(held.values())),),
        ).fetchall()
    }
    marker_maps = _chunk_marker_map(conn, [c[0] for c in chunks])
    resolved = unheld = ambiguous = 0
    for chunk_id, _ord, text in chunks:
        for _order, _label, entry_id in _ordered_occurrences(
            text, marker_maps.get(chunk_id, {}), ay_index
        ):
            dst = held.get(entry_id)
            if dst is None or dst not in live:
                unheld += 1
            else:
                resolved += 1
        ambiguous += _ambiguous_author_year_count(text, ay_index)
    return {"resolved": resolved, "ambiguous": ambiguous, "unheld": unheld}


def resolve_one_paper(
    store: Store,
    ref_id: int,
    *,
    email: str = "",
    embedder: Any = None,
) -> dict[str, Any]:
    """Run one named paper through the whole pipeline synchronously —
    the ``cite_resolve`` job's per-paper leg (gr513768 slice 7): the
    OpenAlex entry-resolution leg, the chunk sweep, the outcome counts,
    and the passage sweep over its links when that layer is lit.

    The attempt lease is stamped first so a concurrent
    :func:`run_cite_resolve_pass` claim skips the paper — the job *is*
    the high-priority claim, which is exactly how an on-request paper
    outranks the access queue: it never waits behind ``CITERES:pending``
    papers, it bypasses the mark entirely (no ``CITERES:pending`` tag is
    written; ``_sweep_paper``'s ``replace_prefix`` flip still lands
    ``CITERES:swept``, so a later ``get`` doesn't re-mark it).
    ``bib_parse``/``bib_mark`` prerequisites are the caller's — the job
    runs them for the named papers before this leg.

    Returns ``{chunks_swept, links_ensured, resolved, ambiguous,
    unheld, entries_resolved, entries_missed, passage}``."""
    out: dict[str, Any] = {
        "chunks_swept": 0,
        "links_ensured": 0,
        "resolved": 0,
        "ambiguous": 0,
        "unheld": 0,
        "entries_resolved": 0,
        "entries_missed": 0,
    }
    with store.pool.connection() as conn:
        ref_lease.stamp_attempt(store, ref_id, _CITERES_NS, conn=conn)
        conn.commit()
    leg = _resolve_paper_openalex(store, ref_id, email=email)
    out["entries_resolved"] = leg["resolved"]
    out["entries_missed"] = leg["missed"]
    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                swept, ensured = _sweep_paper(store, ref_id, conn)
                ref_lease.clear_attempt(store, ref_id, _CITERES_NS, conn=conn)
                counts = _paper_outcome_counts(conn, ref_id)
        out["chunks_swept"] = swept
        out["links_ensured"] = ensured
        out.update(counts)
    except Exception:
        # Same failure shape as the pass: the lease stays to brake a
        # re-bill, the error tag records the attempt.
        with store.pool.connection() as econn:
            stamp = _error_stamp(econn, ref_id)
            store.add_tag(
                ref_id,
                Tag.closed(_CITERES_NS, stamp),
                set_by="system",
                conn=econn,
            )
            econn.commit()
        raise
    if passage_enabled():
        out["passage"] = run_passage_sweep(store, embedder)
    return out


def run_cite_resolve_pass(
    store: Store,
    *,
    batch_size: int | None = None,
    email: str = "",
    embedder: Any = None,
) -> dict[str, int]:
    """One pass: claim ``CITERES:pending`` papers, run each through the
    OpenAlex leg then write its chunks' ``cites`` links, stamp
    ``CITERES:<version>`` per chunk, flip the paper to
    ``CITERES:swept``.

    ``batch_size`` bounds *papers* claimed (default 8). The claim
    transaction stamps each claimed paper's :mod:`ref_lease` attempt
    lease and commits — the ``FOR UPDATE`` lock never spans the
    OpenAlex lookups; the lease is the durable claim a sibling's claim
    excludes and the failure brake (:mod:`ref_lease`'s contract). Each
    paper then resolves and sweeps in its own short transactions, so
    one failing paper costs itself alone: it is stamped
    ``CITERES:error:<n>`` (the claim sorts it last — the backoff — and
    at :data:`_MAX_ERROR_ATTEMPTS` ``error:terminal`` stops it being
    claimed at all) while the batch still processes the survivors.

    A :class:`RateLimited` refusal from the shared ``openalex`` limiter
    defers the whole rest of the batch (every later paper would be
    refused too): the refused paper keeps its pending tag and its
    lease — it *was* attempted, the lease brakes re-billing — while
    the papers never attempted get their leases back so they re-claim
    next pass (``bib_parse``'s documented idiom). Deferred is never
    error-stamped.

    The passage layer (spec slice 6) runs at the end of the same call
    when its own flag is lit (:func:`passage_enabled`) — one bounded
    :func:`run_passage_sweep` after the paper loop, upgrading minted
    ref-level links to chunk→chunk.

    Returns ``{papers_swept, chunks_swept, links_ensured,
    entries_resolved, entries_missed, deferred, failed, passage}`` —
    ``passage`` is the sweep's own ``{claimed, chunk, none, skipped,
    failed}`` dict, present only when the layer ran.
    """
    papers_swept = chunks_swept = links_ensured = 0
    entries_resolved = entries_missed = 0
    failed_ids: list[int] = []
    deferred = 0
    try:
        with store.pool.connection() as conn:
            claimed = _claim_pending_papers(conn, limit=batch_size or 8)
            for ref_id in claimed:
                ref_lease.stamp_attempt(store, ref_id, _CITERES_NS, conn=conn)
            conn.commit()
    except Exception:
        log.warning(
            "cite_resolve: claim failed -- nothing processed, retried next pass",
            exc_info=True,
        )
        return {
            "papers_swept": 0,
            "chunks_swept": 0,
            "links_ensured": 0,
            "entries_resolved": 0,
            "entries_missed": 0,
            "deferred": 0,
            "failed": 1,
        }

    def _error_tag(ref_id: int) -> None:
        try:
            with store.pool.connection() as econn:
                stamp = _error_stamp(econn, ref_id)
                store.add_tag(
                    ref_id,
                    Tag.closed(_CITERES_NS, stamp),
                    set_by="system",
                    conn=econn,
                )
                econn.commit()
        except Exception:
            log.warning(
                "cite_resolve: error-stamp failed for %s", ref_id, exc_info=True
            )
            failed_ids.append(ref_id)
            return
        if stamp == _ERROR_TERMINAL:
            log.warning(
                "cite_resolve: paper %s failed %d sweeps -- "
                "tagged %s:%s, no longer claimed",
                ref_id,
                _MAX_ERROR_ATTEMPTS,
                _CITERES_NS,
                _ERROR_TERMINAL,
            )
        else:
            log.warning(
                "cite_resolve: paper %s failed -- tagged %s:%s, "
                "backed off behind clean papers",
                ref_id,
                _CITERES_NS,
                stamp,
            )
        failed_ids.append(ref_id)

    for i, ref_id in enumerate(claimed):
        try:
            leg = _resolve_paper_openalex(store, ref_id, email=email)
            entries_resolved += leg["resolved"]
            entries_missed += leg["missed"]
        except RateLimited:
            log.info(
                "cite_resolve: shared openalex limiter refused at ref_id=%s; "
                "deferring the rest of the batch",
                ref_id,
            )
            deferred += 1
            try:
                with store.pool.connection() as conn:
                    for skipped_id in claimed[i + 1 :]:
                        ref_lease.clear_attempt(
                            store, skipped_id, _CITERES_NS, conn=conn
                        )
                    conn.commit()
            except Exception:
                log.warning(
                    "cite_resolve: could not release skipped leases",
                    exc_info=True,
                )
            break
        except Exception:
            log.warning(
                "cite_resolve: openalex leg failed for paper %s",
                ref_id,
                exc_info=True,
            )
            _error_tag(ref_id)
            continue
        try:
            with store.pool.connection() as conn:
                with conn.transaction():
                    swept, ensured = _sweep_paper(store, ref_id, conn)
                    ref_lease.clear_attempt(store, ref_id, _CITERES_NS, conn=conn)
            papers_swept += 1
            chunks_swept += swept
            links_ensured += ensured
        except Exception:
            log.warning(
                "cite_resolve: sweep failed for paper %s -- rolled back",
                ref_id,
                exc_info=True,
            )
            _error_tag(ref_id)

    out: dict[str, Any] = {
        "papers_swept": papers_swept,
        "chunks_swept": chunks_swept,
        "links_ensured": links_ensured,
        "entries_resolved": entries_resolved,
        "entries_missed": entries_missed,
        "deferred": deferred,
        "failed": len(failed_ids),
    }
    if passage_enabled():
        out["passage"] = run_passage_sweep(store, embedder)
    return out


__all__ = [
    "CITERES_VERSION",
    "BibEntry",
    "ensure_chunk_cites_links",
    "resolve_one_paper",
    "run_cite_resolve_pass",
    "run_passage_sweep",
]
