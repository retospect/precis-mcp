"""Deterministic paper-hygiene heals — the low-crud, self-healing sweeps.

These are the belt to the dedup braces: pure DB repairs with no judgment
and no network, safe to run unattended on a cadence (the ``paper_reconcile``
worker pass drives them). Each fixes a class of *legacy residue* left by
ingestion/edit bugs that the current code no longer produces:

* :func:`heal_drifted_cards` — a paper whose title was repaired but whose
  embedded ``card_combined`` search chunk was never rebuilt, so search
  still matches the old junk text. (All *current* write paths call
  ``rewrite_cards``; this heals history and makes the drift transient even
  if some future path forgets.)
* :func:`collapse_superseded_chains` — a retired ref whose
  ``meta.superseded_by`` points at *another* retired ref instead of the
  final live survivor (the "stub points at a stub" dereference chain).
* :func:`migrate_dangling_paper_links` — a non-``supersedes`` graph edge
  still pointing at a soft-deleted paper; repoint it to the survivor.
* :func:`requeue_stranded_fetches` — a stub that logged ``fetch_ok`` but
  never ingested (``pdf_sha256`` still NULL): the pre-2026-06-19 inbox
  misconfig black-holed the download, and the exponential fetch backoff
  then parked the stub ~30 days out. Clears the backoff **once** so the
  now-fixed pipeline re-fetches it.
* :func:`requeue_front_matter_only_papers` — a live paper whose only
  body is Elsevier's entitlement-limited preview page (title,
  affiliations, abstract, first paragraphs of the intro, printed
  footer — no references), fingerprinted by the footer boilerplate
  plus the absence of a reference list. Unlike the stranded-fetch class
  above, this ref DOES carry a ``pdf_sha256`` (the preview downloaded
  fine), so it's invisible to both ``requeue_stranded_fetches`` and the
  fetcher's own claim query until pinned. Clears the fetch backoff and
  stamps a one-shot ``meta.markup_refetch`` pin (gr372781 item 3) that
  :func:`precis.workers.fetch_oa.claim_stubs_to_fetch` admits despite
  the existing PDF and :func:`precis.ingest.db_writer.
  register_aliases_and_maybe_upgrade` uses to gate a body *replacement*
  once the re-fetch lands.
* :func:`requeue_placeholder_title_papers` — a live paper still carrying
  the ``PLACEHOLDER_TITLE`` sentinel a DOI-only acquire mints it with,
  even though ``paper_meta_enrich`` already visited it and kept the rest
  of the Crossref record. That pass never wrote ``refs.title``/``year``
  until it was taught to; clearing its ``meta.authors_resolved_at``
  idempotency stamp is the whole heal — the ref falls back into
  ``_claim_batch``'s predicate and the (now title-filling) pass re-runs
  over it. No network here: this only re-arms.
* :func:`heal_bodiless_pdfs` — a live paper that carries a ``pdf_sha256``
  but no body chunk (``ord >= 0``): the PDF leg promoted the stub and
  wrote no text, and nothing journalled why (gr453860). The one heal
  here that is not pure SQL: for a locally held, readable PDF it re-runs
  the body extraction (Marker, in a killable subprocess) and writes the
  chunks; everything else it *judges* once — Elsevier entitlement
  preview, file missing on every node, corrupt or text-less PDF — and
  journals the verdict as a ``ref_events`` row (``source='heal:bodiless'``)
  so the next pass skips what it already judged. Still no network.

All are dry-run by default and idempotent: a clean corpus yields empty
results and the next pass is a cheap no-op.

:func:`metadata_hygiene_stats` is a different animal — pure read-only
corpus-quality *counters* (no remediation, no dry-run flag), so a human
or ``/whatneedsdoing`` can see the author-format-drift backfill's
progress (and catch a future ingest path that bypasses the normalizer
again) without an ad-hoc prod sample. See its docstring for the
individual counters.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

from precis.corpus_layout import resolve_local_pdf
from precis.identity import PLACEHOLDER_TITLE
from precis.ingest.cards import rewrite_cards
from precis.ingest.db_writer import PaperToWrite, register_aliases_and_maybe_upgrade
from precis.store import Store
from precis.store._body_predicate import has_body_sql
from precis.utils.authors import author_display, author_names, is_junk_author_name

if TYPE_CHECKING:
    from collections.abc import Callable

    from precis.ingest.pipeline import BodyExtraction

log = logging.getLogger(__name__)

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def _norm(s: str | None) -> str:
    """Lowercase + strip non-alphanumerics — punctuation/markup-insensitive."""
    return _NON_ALNUM.sub("", (s or "").lower())


# ---------------------------------------------------------------------------
# Card drift
# ---------------------------------------------------------------------------


def heal_drifted_cards(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Rebuild ``card_combined`` chunks whose text lost the current title.

    A cheap SQL prefilter finds papers whose ``card_combined`` doesn't
    contain the title's first 25 chars; each candidate is then verified in
    Python against a punctuation-insensitive match (so an en-dash / markup
    difference is *not* treated as drift) before ``rewrite_cards`` rebuilds
    it from the live metadata. Returns the healed ``ref_id``s.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id "
            "FROM refs r "
            "JOIN chunks c ON c.ref_id = r.ref_id "
            "               AND c.chunk_kind = 'card_combined' "
            "WHERE r.kind = 'paper' AND r.retired_at IS NULL "
            "  AND r.title IS NOT NULL AND btrim(r.title) <> '' "
            "  AND position(lower(left(r.title, 25)) in lower(c.text)) = 0 "
            "ORDER BY r.ref_id"
        ).fetchall()
    candidates = [int(r[0]) for r in rows]
    if limit:
        candidates = candidates[:limit]

    healed: list[int] = []
    for rid in candidates:
        ref = store.fetch_refs_by_ids([rid]).get(rid)
        if ref is None or not ref.title:
            continue
        title = ref.title
        # Shape-tolerant: ``ref.authors`` may hold ``{"name"}`` or the
        # canonical ``{"given", "family"}`` shape — a bare ``.get("name")``
        # filter would silently drop the latter from the rebuilt card.
        authors_display = author_names(ref.authors)
        meta = ref.meta or {}
        abstract = meta.get("abstract", "")
        abstract = abstract if isinstance(abstract, str) else ""
        kw_raw = meta.get("keywords", [])
        keywords = list(kw_raw) if isinstance(kw_raw, list) else []

        with store.pool.connection() as conn:
            got = conn.execute(
                "SELECT text FROM chunks "
                "WHERE ref_id = %s AND chunk_kind = 'card_combined' LIMIT 1",
                (rid,),
            ).fetchone()
        if got is None:
            continue
        tnorm = _norm(title)[:60]
        # Genuine drift only: the current title isn't in the card even after
        # normalising punctuation/markup away — so the card carries a
        # different (stale) title, not just a formatting variant.
        if tnorm and tnorm in _norm(got[0]):
            continue
        if dry_run:
            healed.append(rid)
            continue
        with store.tx() as conn:
            rewrite_cards(
                conn,
                rid,
                title=title,
                author_names=authors_display,
                abstract=abstract,
                keywords=keywords,
            )
        healed.append(rid)
    if healed and not dry_run:
        log.info("paper_hygiene: rebuilt %d drifted card(s)", len(healed))
    return healed


# ---------------------------------------------------------------------------
# Superseded-chain collapse
# ---------------------------------------------------------------------------


def _terminal_survivor(conn: Any, start_ref_id: int) -> int | None:
    """Follow ``meta.superseded_by`` to the ref that has none (the final
    survivor). Returns None on a cycle or a broken pointer."""
    seen: set[int] = set()
    cur = start_ref_id
    while True:
        if cur in seen:
            return None  # cycle guard
        seen.add(cur)
        row = conn.execute(
            "SELECT meta->>'superseded_by' FROM refs WHERE ref_id = %s", (cur,)
        ).fetchone()
        if row is None:
            return None
        if row[0] is None:
            return cur
        cur = int(row[0])


def collapse_superseded_chains(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[tuple[int, int]]:
    """Repoint ``meta.superseded_by`` chains at the final live survivor.

    Finds retired refs whose ``superseded_by`` target is *itself* retired
    (superseded) and rewrites the pointer to the terminal survivor, so a
    dereference is always one hop. Returns ``(ref_id, terminal)`` pairs.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, (r.meta->>'superseded_by')::bigint "
            "FROM refs r "
            "JOIN refs s ON s.ref_id = (r.meta->>'superseded_by')::bigint "
            "WHERE r.kind = 'paper' AND r.meta ? 'superseded_by' "
            "  AND s.meta ? 'superseded_by' "
            "ORDER BY r.ref_id"
        ).fetchall()
    pairs = [(int(a), int(b)) for a, b in rows]
    if limit:
        pairs = pairs[:limit]

    fixed: list[tuple[int, int]] = []
    for rid, mid in pairs:
        with store.pool.connection() as conn:
            terminal = _terminal_survivor(conn, mid)
        if terminal is None or terminal in (mid, rid):
            continue
        if not dry_run:
            with store.tx() as conn:
                store.stamp_ref_meta(rid, {"superseded_by": terminal}, conn=conn)
        fixed.append((rid, terminal))
    if fixed and not dry_run:
        log.info("paper_hygiene: collapsed %d superseded chain(s)", len(fixed))
    return fixed


# ---------------------------------------------------------------------------
# Dangling links to soft-deleted papers
# ---------------------------------------------------------------------------


def migrate_dangling_paper_links(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Repoint non-``supersedes`` edges off soft-deleted papers.

    A ``supersedes`` edge legitimately points at the retired ref (it's the
    audit record); every *other* relation pointing at a soft-deleted paper
    is a dangling dereference and is moved to the survivor
    (``meta.superseded_by``), dropping the row instead when the move would
    self-loop or collide with an existing survivor edge. Returns the
    ``link_id``s acted on.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT l.link_id, l.src_ref_id, l.relation, "
            "       (tgt.meta->>'superseded_by')::bigint AS surv "
            "FROM links l "
            "JOIN refs tgt ON tgt.ref_id = l.dst_ref_id "
            "WHERE tgt.kind = 'paper' AND tgt.retired_at IS NOT NULL "
            "  AND l.relation <> 'supersedes' AND tgt.meta ? 'superseded_by' "
            "ORDER BY l.link_id"
        ).fetchall()
    dangling = [(int(r[0]), r[1], r[2], int(r[3])) for r in rows]
    if limit:
        dangling = dangling[:limit]

    acted: list[int] = []
    for link_id, src, relation, surv in dangling:
        if not dry_run:
            with store.tx() as conn:
                # A self-loop (src already the survivor) or an existing
                # equivalent survivor edge → drop; otherwise repoint.
                collides = (
                    src == surv
                    or conn.execute(
                        "SELECT 1 FROM links "
                        "WHERE src_ref_id IS NOT DISTINCT FROM %s AND dst_ref_id = %s "
                        "  AND relation = %s AND link_id <> %s LIMIT 1",
                        (src, surv, relation, link_id),
                    ).fetchone()
                    is not None
                )
                if collides:
                    conn.execute("DELETE FROM links WHERE link_id = %s", (link_id,))
                else:
                    conn.execute(
                        "UPDATE links SET dst_ref_id = %s WHERE link_id = %s",
                        (surv, link_id),
                    )
        acted.append(link_id)
    if acted and not dry_run:
        log.info("paper_hygiene: migrated %d dangling link(s)", len(acted))
    return acted


# ---------------------------------------------------------------------------
# Stranded OA fetches
# ---------------------------------------------------------------------------

#: Minimum age a ``fetch_ok`` must reach before a still-stub paper counts
#: as *stranded* (env ``PRECIS_OA_STRANDED_HOURS``). Comfortably past the
#: watcher's ingest latency (minutes) so a just-downloaded PDF mid-ingest
#: is never swept, yet far under the fetcher's ~30-day backoff cap so the
#: stub is rescued long before it would retry on its own.
_STRANDED_HOURS_DEFAULT = 48


def _stranded_hours() -> int:
    try:
        return max(1, int(os.environ.get("PRECIS_OA_STRANDED_HOURS", "").strip()))
    except (TypeError, ValueError):
        return _STRANDED_HOURS_DEFAULT


def requeue_stranded_fetches(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Re-queue stubs that logged ``fetch_ok`` but never ingested.

    The signature — ``kind='paper'``, ``pdf_sha256 IS NULL``, and a
    ``fetcher:%`` ``fetch_ok`` event older than
    ``PRECIS_OA_STRANDED_HOURS`` (default 48h) — is the fingerprint of
    the pre-2026-06-19 inbox misconfig (stuck stub #34736): the bytes
    downloaded (``fetch_ok``) but landed in a directory no watcher
    scanned, so nothing ingested, and the exponential fetch backoff then
    parked the stub ~30 days out — it will not self-recover promptly.

    The heal clears the backoff **once**: it deletes the stub's
    ``fetcher:%`` events (so :func:`claim_stubs_to_fetch` sees zero
    attempts and re-qualifies it on the next fetch pass, feeding it back
    through the now-fixed pipeline) and stamps ``meta.oa_requeued`` as a
    one-shot guard. A stub that fails *again* after re-queue re-enters
    normal backoff but — carrying the marker — is never re-queued a
    second time, so this cannot spin. An ``oa_requeued`` breadcrumb
    (source ``paper_reconcile``, deliberately **not** ``fetcher:%`` so it
    doesn't re-arm the backoff) preserves the audit trail the delete
    removes.

    Returns the ref_ids re-queued.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, fe.attempts, fe.last_ok
              FROM refs r
              JOIN LATERAL (
                    SELECT count(*) AS attempts,
                           max(e.ts) FILTER (WHERE e.event = 'fetch_ok') AS last_ok
                      FROM ref_events e
                     WHERE e.ref_id = r.ref_id AND e.source LIKE 'fetcher:%%'
              ) fe ON TRUE
             WHERE r.kind = 'paper'
               AND r.pdf_sha256 IS NULL
               AND r.retired_at IS NULL
               AND NOT (r.meta ? 'oa_requeued')
               AND fe.last_ok IS NOT NULL
               AND fe.last_ok < now() - make_interval(hours => %s)
             ORDER BY r.ref_id
            """,
            (_stranded_hours(),),
        ).fetchall()
    stranded = [(int(r[0]), int(r[1]), r[2]) for r in rows]
    if limit:
        stranded = stranded[:limit]

    requeued: list[int] = []
    for ref_id, attempts, last_ok in stranded:
        if not dry_run:
            last_ok_iso = last_ok.isoformat() if last_ok is not None else None
            with store.tx() as conn:
                conn.execute(
                    "DELETE FROM ref_events "
                    "WHERE ref_id = %s AND source LIKE 'fetcher:%%'",
                    (ref_id,),
                )
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (
                        Jsonb(
                            {
                                "oa_requeued": {
                                    "at": datetime.now(UTC).isoformat(),
                                    "prior_attempts": attempts,
                                    "last_ok": last_ok_iso,
                                }
                            }
                        ),
                        ref_id,
                    ),
                )
                store.append_event(
                    ref_id,
                    source="paper_reconcile",
                    event="oa_requeued",
                    payload={"prior_attempts": attempts, "last_ok": last_ok_iso},
                    conn=conn,
                )
        requeued.append(ref_id)
    if requeued and not dry_run:
        log.info("paper_hygiene: re-queued %d stranded OA fetch(es)", len(requeued))
    return requeued


# ---------------------------------------------------------------------------
# Front-matter-only Elsevier previews
# ---------------------------------------------------------------------------

#: Cap on body-chunk count for a candidate front-matter-only preview.
#: Elsevier's entitlement-limited preview PDF — title, affiliations,
#: keywords, abstract, the first paragraphs of the intro, and the
#: printed page footer — ingests as ~8 body chunks; a real full-text
#: paper runs into the dozens-to-hundreds. 12 sits comfortably above
#: the observed preview shape and comfortably below any genuine paper.
_FRONT_MATTER_MAX_CHUNKS = 12

#: Elsevier's printed-page-footer boilerplate — present on the preview
#: PDF's one page (it's running-header/footer furniture, not article
#: content) and never its own chunk in a genuine full-text ingest. Any
#: one hit fingerprints the entitlement-limited preview.
_ELSEVIER_FOOTER_SIGNATURES = (
    "%Contents lists available at ScienceDirect%",
    "%journal homepage: www.elsevier.com%",
    "%including those for text and data mining%",
)

#: A real full-text paper has a reference list; its absence alongside
#: an Elsevier footer chunk is the front-matter-only fingerprint — the
#: preview stops after the intro, long before References /
#: Acknowledgements.
_BACK_MATTER_SIGNATURES = (
    "%References%",
    "%Bibliography%",
    "%Acknowledg%",
)


def requeue_front_matter_only_papers(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Re-queue live papers whose only body is an Elsevier entitlement preview.

    Elsevier's Article Retrieval API can return a well-formed,
    complete ``%PDF-`` response that is nonetheless only the
    entitlement-limited preview page — title, affiliations, keywords,
    abstract, the first paragraphs of the intro, and the printed-page
    footer — with no error and nothing to distinguish it from a
    genuine full-text fetch. It ingests silently as a handful (~8) of
    front-matter chunks, carrying a real ``pdf_sha256`` — invisible to
    :func:`requeue_stranded_fetches` (which keys on a NULL hash) and to
    the fetcher's own claim query (same reason). ~7800 prod papers are
    affected (gr372781 item 3); the markup-first XML leg
    (``PRECIS_FETCH_MARKUP`` / ``fetch_oa._try_elsevier_markup``) fixes
    this going forward but does nothing for what's already ingested.

    The signature: a live paper that (a) has a ``fetcher:elsevier``
    ``fetch_ok`` but no ``fetcher:elsevier_xml`` one — the markup leg
    can't be fooled by this failure mode (a non-entitled DOI answers
    its XML request with an error body, not a truncated-but-valid
    one), so its presence would mean the markup body already landed;
    (b) between 1 and :data:`_FRONT_MATTER_MAX_CHUNKS` body chunks;
    (c) one of which matches the Elsevier printed-footer boilerplate
    (:data:`_ELSEVIER_FOOTER_SIGNATURES`); and (d) none of which look
    like a reference list (:data:`_BACK_MATTER_SIGNATURES`) — the
    footer without a References section is what a genuine short paper
    never looks like.

    The heal deletes the ref's ``fetcher:%`` events (clears the
    backoff, same mechanism and reasoning as
    :func:`requeue_stranded_fetches`) and stamps a one-shot
    ``meta.markup_refetch`` pin that two other pieces of this backfill
    key on: :func:`precis.workers.fetch_oa.claim_stubs_to_fetch` admits
    the ref back into the fetch claim *despite* its existing PDF, and
    :func:`precis.ingest.db_writer.register_aliases_and_maybe_upgrade`
    uses the pin to gate a body **replacement** (never an in-place
    update — see that function) once the re-fetch lands. A
    ``paper_reconcile``/``markup_refetch_queued`` breadcrumb (source
    deliberately not ``fetcher:%``, so it doesn't re-arm the backoff it
    just cleared) preserves the audit trail the delete removes.

    Returns the ref_ids selected (dry-run returns what it *would* act
    on, without writing).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, cnt.body_chunks
              FROM refs r
              JOIN LATERAL (
                    SELECT count(*) AS body_chunks
                      FROM chunks c
                     WHERE c.ref_id = r.ref_id AND c.ord >= 0
              ) cnt ON TRUE
             WHERE r.kind = 'paper'
               AND r.retired_at IS NULL
               AND r.pdf_sha256 IS NOT NULL
               AND NOT (r.meta ? 'markup_refetch')
               AND cnt.body_chunks BETWEEN 1 AND %s
               AND EXISTS (
                     SELECT 1 FROM ref_events e
                      WHERE e.ref_id = r.ref_id
                        AND e.source = 'fetcher:elsevier' AND e.event = 'fetch_ok'
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM ref_events e
                      WHERE e.ref_id = r.ref_id
                        AND e.source = 'fetcher:elsevier_xml' AND e.event = 'fetch_ok'
                   )
               AND EXISTS (
                     SELECT 1 FROM chunks c
                      WHERE c.ref_id = r.ref_id AND c.ord >= 0
                        AND c.text ILIKE ANY(%s)
                   )
               AND NOT EXISTS (
                     SELECT 1 FROM chunks c
                      WHERE c.ref_id = r.ref_id AND c.ord >= 0
                        AND c.text ILIKE ANY(%s)
                   )
             ORDER BY r.ref_id
            """,
            (
                _FRONT_MATTER_MAX_CHUNKS,
                list(_ELSEVIER_FOOTER_SIGNATURES),
                list(_BACK_MATTER_SIGNATURES),
            ),
        ).fetchall()
    candidates = [(int(r[0]), int(r[1])) for r in rows]
    if limit:
        candidates = candidates[:limit]

    queued: list[int] = []
    for ref_id, body_chunks in candidates:
        if not dry_run:
            with store.tx() as conn:
                conn.execute(
                    "DELETE FROM ref_events "
                    "WHERE ref_id = %s AND source LIKE 'fetcher:%%'",
                    (ref_id,),
                )
                conn.execute(
                    "UPDATE refs SET meta = meta || %s WHERE ref_id = %s",
                    (
                        Jsonb(
                            {
                                "markup_refetch": {
                                    "at": datetime.now(UTC).isoformat(),
                                    "reason": "front-matter-only body",
                                    "body_chunks": body_chunks,
                                }
                            }
                        ),
                        ref_id,
                    ),
                )
                store.append_event(
                    ref_id,
                    source="paper_reconcile",
                    event="markup_refetch_queued",
                    payload={"body_chunks": body_chunks},
                    conn=conn,
                )
        queued.append(ref_id)
    if queued and not dry_run:
        log.info(
            "paper_hygiene: re-queued %d front-matter-only Elsevier paper(s)",
            len(queued),
        )
    return queued


# ---------------------------------------------------------------------------
# Bodiless PDFs — held bytes, no extracted text
# ---------------------------------------------------------------------------

#: ``ref_events.source`` every bodiless-heal verdict is journalled under.
#: One row per (ref, pdf_sha256) judged; the candidate query excludes a ref
#: whose *current* sha already has a verdict, so a re-fetch that lands a
#: different file is judged afresh while a settled verdict is never
#: revisited. Deliberately not ``fetcher:%`` (it must not touch the fetch
#: backoff) and not ``paper_reconcile`` (the other heals' breadcrumb
#: source) so ``view='log'`` and the stats split read it unambiguously.
BODILESS_HEAL_SOURCE = "heal:bodiless"

#: Fetch legs whose ``fetch_ok`` means "Elsevier's Article Retrieval API
#: answered" — for a non-entitled DOI that is a well-formed one-page
#: preview, and re-running Marker over it would turn a bodiless paper into
#: a preview-body one (worse: it then looks done). A bodiless ref whose
#: newest ``fetch_ok`` came from one of these gets the ``preview`` verdict
#: and is never re-extracted; the entitlement fix (td462729) re-fetches
#: through the markup leg, which lands a body the normal way.
_PREVIEW_FETCH_SOURCES: frozenset[str] = frozenset(
    {"fetcher:elsevier", "fetcher:elsevier_xml"}
)

#: Default Marker wall-clock budget per PDF in the heal — the same 900 s the
#: watcher uses (``precis ingest --watch --marker-timeout-s``). Non-zero so
#: Marker always runs in a killable child here: the heal runs inside the
#: ``paper_reconcile`` pass, which must not wedge on one bad file.
BODILESS_MARKER_TIMEOUT_S = 900.0

_EXTRACT_PER_PASS_DEFAULT = 5


def bodiless_extract_per_pass() -> int:
    """How many Marker runs one unattended ``paper_reconcile`` pass may spend
    on bodiless PDFs (env ``PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS``, default
    5; ``0`` = judge only, never extract). The cheap verdicts (preview,
    missing, unreadable) are not capped — only the heavy leg is, so a
    24-hourly pass drains the readable tail a few papers at a time without
    pinning a node's memory for an hour; ``precis bodiless-heal --apply``
    is the operator's way to do the bulk in one go."""
    try:
        return max(0, int(os.environ["PRECIS_BODILESS_HEAL_EXTRACT_PER_PASS"].strip()))
    except (KeyError, ValueError):
        return _EXTRACT_PER_PASS_DEFAULT


@dataclass(frozen=True)
class BodilessOutcome:
    """One bodiless paper's verdict from :func:`heal_bodiless_pdfs`.

    ``outcome`` is one of:

    * ``extracted`` — body chunks written from the stored PDF (journalled).
    * ``preview`` — Elsevier entitlement preview; not re-extracted (journalled).
    * ``missing_file`` — no node holds the bytes (journalled).
    * ``unreadable`` — the PDF does not open, has no pages, has no text
      layer, or the extractor returned no body; ``reason`` says which
      (journalled).
    * ``readable`` — dry-run only: would be extracted.
    * ``deferred`` — readable, but this pass's extraction cap is spent or
      the file lives on another node; retried next pass (not journalled).
    """

    ref_id: int
    pdf_sha256: str
    outcome: str
    reason: str = ""
    chunks_written: int = 0
    path: str | None = None

    def line(self) -> str:
        bits = [f"ref_id={self.ref_id}", self.outcome]
        if self.chunks_written:
            bits.append(f"chunks={self.chunks_written}")
        if self.reason:
            bits.append(f"({self.reason})")
        return "  ".join(bits)


@dataclass(frozen=True)
class _BodilessCandidate:
    ref_id: int
    pdf_sha256: str
    title: str | None
    storage_path: str
    cite_keys: tuple[str, ...]
    last_fetch_source: str | None
    paper_id: str


def _bodiless_candidates(
    store: Store, *, limit: int | None
) -> list[_BodilessCandidate]:
    """Live papers with a ``pdf_sha256`` and no body chunk whose current
    sha has no ``heal:bodiless`` verdict yet, oldest ref first."""
    sql = f"""
        SELECT r.ref_id, r.pdf_sha256, r.title,
               coalesce(p.storage_path, '') AS storage_path,
               array_remove(array_agg(DISTINCT ri.id_value), NULL) AS cite_keys,
               lf.source AS last_fetch_source,
               (SELECT pid.id_value FROM ref_identifiers pid
                 WHERE pid.ref_id = r.ref_id AND pid.id_kind = 'paper_id'
                 LIMIT 1) AS paper_id
          FROM refs r
          LEFT JOIN pdfs p ON p.pdf_sha256 = r.pdf_sha256
          LEFT JOIN ref_identifiers ri
                 ON ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
          LEFT JOIN LATERAL (
                SELECT e.source FROM ref_events e
                 WHERE e.ref_id = r.ref_id AND e.event = 'fetch_ok'
                   AND starts_with(e.source, 'fetcher:')
                 ORDER BY e.ts DESC LIMIT 1
          ) lf ON TRUE
         WHERE r.kind = 'paper'
           AND r.retired_at IS NULL
           AND r.pdf_sha256 IS NOT NULL
           AND NOT {has_body_sql("r")}
           AND NOT EXISTS (
                 SELECT 1 FROM ref_events j
                  WHERE j.ref_id = r.ref_id AND j.source = %s
                    AND j.payload->>'pdf_sha256' = r.pdf_sha256::text
               )
         GROUP BY r.ref_id, r.pdf_sha256, r.title, p.storage_path, lf.source
         ORDER BY r.ref_id
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (BODILESS_HEAL_SOURCE,)).fetchall()
    out: list[_BodilessCandidate] = []
    for ref_id, sha, title, storage_path, cite_keys, last_src, paper_id in rows:
        # The slug is one of the ``cite_key`` identifiers (v2: ``refs.slug``
        # is gone), so the aggregate already carries it.
        keys = sorted(k for k in (cite_keys or ()) if k)
        out.append(
            _BodilessCandidate(
                ref_id=int(ref_id),
                pdf_sha256=str(sha).strip(),
                title=title,
                storage_path=str(storage_path or ""),
                cite_keys=tuple(keys),
                last_fetch_source=last_src,
                paper_id=str(paper_id) if paper_id else f"ref{int(ref_id)}",
            )
        )
    if limit:
        out = out[:limit]
    return out


def _probe_pdf(path: Path) -> tuple[int, int]:
    """``(page_count, pages_with_text)`` for the PDF at ``path`` — a cheap
    PyMuPDF pass. Raises whatever fitz raises on a file that does not open
    (the caller records that as ``unreadable``)."""
    import fitz

    with fitz.open(str(path)) as doc:
        pages = int(doc.page_count)
        with_text = sum(1 for page in doc if page.get_text("text").strip())
    return pages, with_text


def _default_body_extractor(
    marker_timeout_s: float | None,
) -> Callable[[Path, str], BodyExtraction]:
    from precis.ingest.pipeline import extract_body

    def _run(path: Path, paper_id: str) -> BodyExtraction:
        return extract_body(path, paper_id, marker_timeout_s=marker_timeout_s)

    return _run


def _journal_bodiless(
    store: Store, cand: _BodilessCandidate, outcome: BodilessOutcome, *, conn: Any
) -> None:
    payload: dict[str, Any] = {"pdf_sha256": cand.pdf_sha256}
    if outcome.reason:
        payload["reason"] = outcome.reason
    if outcome.path:
        payload["path"] = outcome.path
    if outcome.outcome == "extracted":
        payload["chunks"] = outcome.chunks_written
    store.append_event(
        cand.ref_id,
        source=BODILESS_HEAL_SOURCE,
        event=outcome.outcome,
        payload=payload,
        conn=conn,
    )


def heal_bodiless_pdfs(
    store: Store,
    *,
    corpus_dirs: tuple[Path, ...] = (),
    dry_run: bool = True,
    limit: int | None = None,
    extract_limit: int | None = None,
    marker_timeout_s: float | None = BODILESS_MARKER_TIMEOUT_S,
    extractor: Callable[[Path, str], BodyExtraction] | None = None,
) -> list[BodilessOutcome]:
    """Judge — and where possible heal — live papers that hold a PDF but no
    body text (gr453860 parts 2 and 3).

    The predicate is ``pdf_sha256 IS NOT NULL AND NOT has_body`` (no chunk
    at ``ord >= 0``; the ``ord < 0`` cards a stub carries do not count), the
    class :func:`requeue_stranded_fetches` cannot see (it keys on a NULL
    sha) and :func:`requeue_front_matter_only_papers` excludes by
    construction (it needs 1..N body chunks). Each candidate is judged in
    order, cheapest first, and the verdict is journalled as a
    ``ref_events`` row (:data:`BODILESS_HEAL_SOURCE`, ``event`` = the
    outcome, ``payload.pdf_sha256`` = the sha judged) so the candidate
    query skips it from then on — until a re-fetch lands a *different*
    file, which is judged afresh:

    1. ``preview`` — the newest ``fetch_ok`` came from an Elsevier leg
       (:data:`_PREVIEW_FETCH_SOURCES`): the stored PDF is the
       entitlement-limited one-page preview. Never re-extracted (that
       would mint a preview *body*, which looks done and is not); the
       entitlement fix re-fetches these through the markup leg.
    2. ``missing_file`` — the PDF resolves on no node:
       :func:`~precis.corpus_layout.resolve_local_pdf` finds nothing under
       this node's ``corpus_dirs`` and the ``pdf_locations`` ledger has no
       fresh row from another host (``Store.pdf_held_anywhere``). A file
       another node does hold is ``deferred`` here, not judged, so a
       single-runner pass on the wrong node cannot mis-file it.
    3. ``unreadable`` — the file does not open in PyMuPDF, has zero pages,
       or has no text layer on any page (``reason`` distinguishes the
       three: corrupt / empty / scanned). Also the verdict when Marker
       runs and returns no body chunk.
    4. ``extracted`` — otherwise the body leg re-runs over the stored file
       (:func:`precis.ingest.pipeline.extract_body`: Marker in a killable
       subprocess when ``marker_timeout_s`` is set, fitz fallback on a
       Marker failure, glyph-health forensics) and the chunks land through
       :func:`precis.ingest.db_writer.register_aliases_and_maybe_upgrade`
       — the same no-body branch a stub upgrade takes, so the healed ref is
       indistinguishable from one ingested normally (content_hash alias,
       body-owned meta, ``markup_refetch`` pin spent). No metadata cascade
       and no network: the ref already has its identity.

    ``extract_limit`` caps step 4 per call (``None`` = unbounded;
    :func:`bodiless_extract_per_pass` supplies the unattended default);
    readable candidates past the cap are ``deferred`` and picked up next
    pass. A dry run (the default) performs steps 1-3's read-only probes
    and reports step-4 candidates as ``readable``, writing nothing.
    ``extractor`` is the Marker seam for tests.

    Returns one :class:`BodilessOutcome` per candidate considered, in
    ``ref_id`` order.
    """
    candidates = _bodiless_candidates(store, limit=limit)
    if not candidates:
        return []
    extract = extractor or _default_body_extractor(marker_timeout_s)
    extracted_n = 0
    outcomes: list[BodilessOutcome] = []

    def _settle(cand: _BodilessCandidate, out: BodilessOutcome) -> None:
        outcomes.append(out)
        if dry_run or out.outcome in ("readable", "deferred"):
            return
        with store.tx() as conn:
            _journal_bodiless(store, cand, out, conn=conn)

    for cand in candidates:
        rid, sha = cand.ref_id, cand.pdf_sha256
        if cand.last_fetch_source in _PREVIEW_FETCH_SOURCES:
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "preview",
                    reason=f"Elsevier entitlement preview ({cand.last_fetch_source})",
                ),
            )
            continue

        path = resolve_local_pdf(corpus_dirs, cand.storage_path, cand.cite_keys)
        if path is None:
            if store.pdf_held_anywhere(sha):
                _settle(
                    cand,
                    BodilessOutcome(
                        rid, sha, "deferred", reason="held on another node"
                    ),
                )
            else:
                _settle(
                    cand,
                    BodilessOutcome(
                        rid,
                        sha,
                        "missing_file",
                        reason=f"not on disk (storage_path={cand.storage_path or '-'})",
                    ),
                )
            continue

        try:
            pages, with_text = _probe_pdf(path)
        except Exception as exc:
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "unreadable",
                    reason=f"corrupt: {str(exc)[:160]}",
                    path=str(path),
                ),
            )
            continue
        if pages == 0:
            _settle(
                cand,
                BodilessOutcome(
                    rid, sha, "unreadable", reason="empty: 0 pages", path=str(path)
                ),
            )
            continue
        if with_text == 0:
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "unreadable",
                    reason=f"scanned: {pages} page(s), no text layer",
                    path=str(path),
                ),
            )
            continue

        if dry_run:
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "readable",
                    reason=f"{with_text}/{pages} text pages",
                    path=str(path),
                ),
            )
            continue
        if extract_limit is not None and extracted_n >= extract_limit:
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "deferred",
                    reason="extraction cap reached",
                    path=str(path),
                ),
            )
            continue

        extracted_n += 1
        try:
            body = extract(path, cand.paper_id)
        except Exception as exc:
            # extract_blocks_marker already absorbs a Marker failure into
            # the fitz fallback, so a raise here is infrastructure (a
            # killed subprocess, a full disk), not a verdict on the file:
            # log it and leave the ref unjudged for the next pass.
            log.warning(
                "paper_hygiene: bodiless ref_id=%s extraction raised on %s: %s",
                rid,
                path,
                exc,
            )
            _settle(
                cand,
                BodilessOutcome(
                    rid,
                    sha,
                    "deferred",
                    reason=f"extraction raised: {str(exc)[:160]}",
                    path=str(path),
                ),
            )
            continue
        if not body.chunks:
            reason = "extractor returned no body chunks"
            fb = body.meta.get("extract_fallback_reason")
            if fb:
                reason += f" (fallback: {str(fb)[:120]})"
            _settle(
                cand,
                BodilessOutcome(rid, sha, "unreadable", reason=reason, path=str(path)),
            )
            continue

        paper = PaperToWrite(
            title=cand.title or "",
            authors=[],
            year=None,
            provider=BODILESS_HEAL_SOURCE,
            paper_id=cand.paper_id,
            pdf_sha256=sha,
            content_hash=body.content_hash,
            pdf_pages_first=body.page_first,
            pdf_pages_last=body.page_last,
            pdf_role="main",
            pdf_storage_path=str(path),
            pdf_page_count=body.page_count or pages,
            pdf_size_bytes=path.stat().st_size,
            meta=body.meta,
            chunks=body.chunks,
        )
        with store.tx() as conn:
            written = register_aliases_and_maybe_upgrade(rid, paper, conn=conn)
            out = BodilessOutcome(
                rid,
                sha,
                "extracted",
                reason=(
                    "fitz fallback body"
                    if body.meta.get("extract_used_fallback")
                    else (
                        "body landed from another ingest first" if not written else ""
                    )
                ),
                chunks_written=len(body.chunks) if written else 0,
                path=str(path),
            )
            _journal_bodiless(store, cand, out, conn=conn)
        outcomes.append(out)
        log.info(
            "paper_hygiene: bodiless ref_id=%s extracted %d body chunk(s) from %s",
            rid,
            out.chunks_written,
            path,
        )

    if not dry_run:
        counts: dict[str, int] = {}
        for o in outcomes:
            counts[o.outcome] = counts.get(o.outcome, 0) + 1
        log.info(
            "paper_hygiene: bodiless PDFs judged %d — %s",
            len(outcomes),
            ", ".join(f"{k}={v}" for k, v in sorted(counts.items())),
        )
    return outcomes


# ---------------------------------------------------------------------------
# Metadata hygiene counters (read-only)
# ---------------------------------------------------------------------------

#: Default cap on how many authored papers :func:`metadata_hygiene_stats`
#: walks in Python to count junk author entries (the one counter
#: :func:`is_junk_author_name` — a pure-Python check — can't do in SQL).
#: The other counters are single aggregate queries and scan the whole
#: corpus regardless.
_JUNK_SAMPLE_LIMIT_DEFAULT = 5000

# A save-as / "print to PDF" stamp leaking a filename into the title
# field: "Microsoft Word - manuscript_v3.docx", a trailing document
# extension left over from an extractor that fell back to the file's
# ``/Title`` field, InDesign's ``.indd``. Anchored to the start/end of
# the (stripped) string, not a substring match — a genuine title that
# happens to end in ".pdf" as prose wouldn't trip this, but nothing in
# a real title does.
_MS_WORD_STAMP_RE = re.compile(r"^microsoft\s+word\s*-", re.IGNORECASE)
_FILENAME_EXT_RE = re.compile(r"\.(docx?|pdf|indd)$", re.IGNORECASE)

# Elsevier's PII (Publisher Item Identifier) stamp, e.g.
# "PII: S0021-9797(20)31234-5" — leaks in when a scraper grabs the
# running header instead of the title block.
_PII_STAMP_RE = re.compile(r"^pii:?\s*s\d", re.IGNORECASE)

# A title that's nothing but a bare DOI — the extractor found an
# identifier but no title text.
_DOI_ONLY_RE = re.compile(r"^doi\s*:\s*10\.\d", re.IGNORECASE)

# Bare extraction placeholders — matched against the *whole* string
# (after stripping trailing punctuation), not a substring, so a real
# title mentioning "no job name" in a sentence wouldn't trip this
# (extremely unlikely, but the same conservatism as
# :func:`~precis.utils.authors.is_junk_author_name`).
_FILENAME_PLACEHOLDER_STOPWORDS = frozenset(
    {
        "untitled",
        "untitled document",
        "no job name",
    }
)


def is_filename_like_title(title: str) -> bool:
    """True when *title* is an obvious filename/extraction artifact.

    Catches the junk that leaks through PDF/DOCX metadata scraping when
    the extractor falls back to the file's ``/Title`` field (often the
    original filename) or a bare placeholder: a Word "save as" stamp
    ("Microsoft Word - manuscript_v3.docx"), a trailing document
    extension (``.docx`` / ``.doc`` / ``.pdf`` / ``.indd``), a bare
    "Untitled" / "No Job Name" placeholder, an Elsevier PII stamp
    ("PII: S0021-9797(20)31234-5"), or a title that's nothing but a DOI
    string ("doi:10.1016/..."). Conservative — every pattern anchors to
    the start or end of the (stripped) string, not a substring, so a
    genuine title never trips this by coincidence. Blank input is not
    considered filename-like (that's a *missing*-title signal, a
    different metric). Pure — never raises. Sibling to
    :func:`~precis.utils.authors.is_junk_author_name`.
    """
    s = title.strip()
    if not s:
        return False
    if _MS_WORD_STAMP_RE.match(s):
        return True
    if _FILENAME_EXT_RE.search(s):
        return True
    if _PII_STAMP_RE.match(s):
        return True
    if _DOI_ONLY_RE.match(s):
        return True
    if s.strip(".,:;- ").lower() in _FILENAME_PLACEHOLDER_STOPWORDS:
        return True
    return False


def _pct(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 1) if denominator else 0.0


@dataclass
class MetadataHygieneStats:
    """Corpus-quality counters for ``kind='paper'`` metadata — pure read,
    no remediation (see :func:`metadata_hygiene_stats`)."""

    total_papers: int = 0
    authored_papers: int = 0
    structured_authors_papers: int = 0
    entry_type_papers: int = 0
    journal_papers: int = 0
    heuristic_source_papers: int = 0
    junk_author_entries: int = 0
    junk_sample_papers: int = 0
    junk_sample_bounded: bool = False
    filename_like_titles: int = 0
    title_sample_papers: int = 0
    title_sample_bounded: bool = False

    @property
    def structured_authors_pct(self) -> float:
        """% of *authored* papers whose author list is fully structured
        (every entry has non-blank ``given``+``family``)."""
        return _pct(self.structured_authors_papers, self.authored_papers)

    @property
    def entry_type_pct(self) -> float:
        return _pct(self.entry_type_papers, self.total_papers)

    @property
    def journal_pct(self) -> float:
        return _pct(self.journal_papers, self.total_papers)


def metadata_hygiene_stats(
    store: Store, *, junk_sample_limit: int = _JUNK_SAMPLE_LIMIT_DEFAULT
) -> MetadataHygieneStats:
    """Corpus-quality visibility for the author-format-drift backfill.

    Pure read over ``refs`` (``kind='paper' AND retired_at IS NULL``); makes
    no writes. Counts:

    * ``structured_authors_papers`` / ``_pct`` — of the *authored* papers
      (non-empty ``authors``), how many have every entry in the canonical
      ``{"given", "family"}`` shape vs. still carrying a flat ``{"name"}``
      (or a mix).
    * ``entry_type_papers`` / ``_pct``, ``journal_papers`` / ``_pct`` — of
      *all* papers, how many carry ``meta.entry_type`` / ``meta.journal``
      (stamped by :func:`precis.ingest.paper_meta_enrich.enrich_paper`).
    * ``heuristic_source_papers`` — papers whose current authors came from
      the no-network comma-split heuristic (``meta.authors_source ==
      'heuristic'``) rather than a resolved Crossref record — the backlog
      still awaiting real resolution.
    * ``junk_author_entries`` — count of individual author entries
      :func:`~precis.utils.authors.is_junk_author_name` flags (an email,
      a section heading, a mis-split affiliation). :func:`is_junk_author_name`
      is pure Python, so this counter walks authored papers row-by-row
      rather than aggregating in SQL like the others; ``junk_sample_papers``
      /  ``junk_sample_bounded`` record how many papers were actually
      walked and whether the corpus was larger than ``junk_sample_limit``
      (default 5000) — a bounded sample, not a full-corpus count, in that
      case.
    * ``filename_like_titles`` — count of papers whose title
      :func:`is_filename_like_title` flags (a "Microsoft Word - ..."
      save-as stamp, a trailing ``.docx``/``.pdf`` extension, a bare
      "Untitled" placeholder, a PII stamp, a bare DOI). Same
      bounded-sample-walk shape as ``junk_author_entries`` (pure Python,
      can't aggregate in SQL); ``title_sample_papers`` /
      ``title_sample_bounded`` record the walk's extent. Unlike the
      author-junk sample, this one isn't restricted to *authored*
      papers — a filename-title stub commonly has no authors either.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT "
            "  count(*) AS total, "
            "  count(*) FILTER ("
            "    WHERE jsonb_array_length(coalesce(r.authors, '[]'::jsonb)) > 0"
            "  ) AS authored, "
            "  count(*) FILTER ("
            "    WHERE jsonb_array_length(coalesce(r.authors, '[]'::jsonb)) > 0"
            "      AND NOT EXISTS ("
            "        SELECT 1 FROM jsonb_array_elements(r.authors) a "
            "        WHERE NOT ("
            "          coalesce(a->>'given', '') <> '' "
            "          AND coalesce(a->>'family', '') <> ''"
            "        )"
            "      )"
            "  ) AS structured, "
            "  count(*) FILTER (WHERE r.meta ? 'entry_type') AS with_entry_type, "
            "  count(*) FILTER (WHERE r.meta ? 'journal') AS with_journal, "
            "  count(*) FILTER ("
            "    WHERE r.meta->>'authors_source' = 'heuristic'"
            "  ) AS heuristic_source "
            "FROM refs r "
            "WHERE r.kind = 'paper' AND r.retired_at IS NULL"
        ).fetchone()
    assert row is not None
    total, authored, structured, with_entry_type, with_journal, heuristic = (
        int(n) for n in row
    )

    with store.pool.connection() as conn:
        sample_rows = conn.execute(
            "SELECT authors FROM refs "
            "WHERE kind = 'paper' AND retired_at IS NULL "
            "  AND jsonb_array_length(coalesce(authors, '[]'::jsonb)) > 0 "
            "ORDER BY ref_id "
            "LIMIT %s",
            (junk_sample_limit + 1,),
        ).fetchall()
    bounded = len(sample_rows) > junk_sample_limit
    sample_rows = sample_rows[:junk_sample_limit]

    junk = 0
    for (authors_raw,) in sample_rows:
        entries = authors_raw if isinstance(authors_raw, list) else []
        for entry in entries:
            name = author_display(entry)
            if name and is_junk_author_name(name):
                junk += 1

    with store.pool.connection() as conn:
        title_rows = conn.execute(
            "SELECT title FROM refs "
            "WHERE kind = 'paper' AND retired_at IS NULL "
            "  AND title IS NOT NULL AND btrim(title) <> '' "
            "ORDER BY ref_id "
            "LIMIT %s",
            (junk_sample_limit + 1,),
        ).fetchall()
    title_bounded = len(title_rows) > junk_sample_limit
    title_rows = title_rows[:junk_sample_limit]

    filename_like = sum(
        1 for (title,) in title_rows if title and is_filename_like_title(title)
    )

    return MetadataHygieneStats(
        total_papers=total,
        authored_papers=authored,
        structured_authors_papers=structured,
        entry_type_papers=with_entry_type,
        journal_papers=with_journal,
        heuristic_source_papers=heuristic,
        junk_author_entries=junk,
        junk_sample_papers=len(sample_rows),
        junk_sample_bounded=bounded,
        filename_like_titles=filename_like,
        title_sample_papers=len(title_rows),
        title_sample_bounded=title_bounded,
    )


def requeue_placeholder_title_papers(
    store: Store, *, dry_run: bool = True, limit: int | None = None
) -> list[int]:
    """Re-arm ``paper_meta_enrich`` over papers stuck at the no-title sentinel.

    A DOI-only acquire (``Store.acquire_paper_stub``) mints its stub with
    :data:`precis.identity.PLACEHOLDER_TITLE` and a NULL year, because at
    mint time only the identifier is known. Filling those in is
    ``paper_meta_enrich``'s job — but until it was taught to write
    ``refs.title``/``refs.year`` it normalized Crossref's answer, kept the
    journal/ISSN/abstract/byline out of it, and dropped the title. The row
    then pinned itself shut: that pass stamps ``meta.authors_resolved_at``
    on every ref it visits, hit or miss, and claims only rows where the
    stamp is NULL, so a once-visited ref is never reconsidered.

    The heal is therefore just the stamp: delete it, and the ref re-enters
    :func:`precis.workers.paper_meta_enrich._claim_batch` on the next pass
    (hourly by default), which now fills the title. Nothing is fetched
    here — the re-fetch is the worker's, under its existing rate budget.

    Only papers carrying a DOI are selected: a DOI-less placeholder has
    nothing for Crossref to resolve, so re-arming it would burn a claim
    slot to reach the same no-op. Those rows need a human or a fresh
    identifier and are left alone (``metadata_hygiene_stats`` still counts
    them).

    A ``paper_reconcile``/``title_backfill_queued`` breadcrumb records the
    re-arm, so ``view='log'`` shows why a long-settled ref was visited
    twice. Returns the ref_ids selected; a dry run returns what it *would*
    act on without writing.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'paper'
               AND r.retired_at IS NULL
               AND (r.title IS NULL OR btrim(r.title) IN ('', %s))
               AND r.meta ? 'authors_resolved_at'
               AND EXISTS (
                     SELECT 1 FROM ref_identifiers ri
                      WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi'
                   )
             ORDER BY r.ref_id
            """,
            (PLACEHOLDER_TITLE,),
        ).fetchall()
    candidates = [int(r[0]) for r in rows]
    if limit:
        candidates = candidates[:limit]

    if not dry_run:
        for ref_id in candidates:
            with store.tx() as conn:
                conn.execute(
                    "UPDATE refs SET meta = meta - 'authors_resolved_at' "
                    "WHERE ref_id = %s",
                    (ref_id,),
                )
                store.append_event(
                    ref_id,
                    source="paper_reconcile",
                    event="title_backfill_queued",
                    payload={"reason": "placeholder title"},
                    conn=conn,
                )
        log.info(
            "requeue_placeholder_title_papers: re-armed %d paper(s)",
            len(candidates),
        )
    return candidates


#: Tag + ``meta.source`` marking a "fix this paper's title" human todo.
TITLE_FIX_TAG = "paper-title-fix"
TITLE_FIX_SOURCE = "paper_hygiene:title_fix"
#: A junk-titled paper younger than this is left to ingest/enrich first.
_TITLE_FIX_GRACE_DAYS = 3


def raise_junk_title_papers(
    store: Store,
    *,
    dry_run: bool = True,
    limit: int | None = None,
    grace_days: int = _TITLE_FIX_GRACE_DAYS,
) -> list[int]:
    """File a ``waiting-for:<login>`` todo per junk-titled paper automation can't fix.

    A paper whose stored title is junk (``"__"``, a filename, PII, blank,
    or the no-title sentinel) still resolves in citations, so an untitled
    paper silently stands in for a real one (gr477964). Automation that
    *can* fix it (a placeholder with a DOI is re-armed by
    :func:`requeue_placeholder_title_papers`; ``paper_meta_enrich`` fills
    it) is left alone; the rest is raised to the paper's owner
    (``refs.owner_login``, else the deployment owner ``PRECIS_OWNER``)
    as a todo tagged ``waiting-for:<login>`` + ``paper-title-fix``.

    Skipped: retired papers; placeholder-titled papers that carry a DOI
    (the enrich route owns them); papers younger than ``grace_days``.

    Idempotent: ``meta.source``/``meta.paper_ref_id`` identify the todo,
    and a paper with an open todo, or one closed with any status other
    than ``STATUS:done`` (``wontfix``: a human chose to leave it), is never
    re-filed. When every prior todo is ``STATUS:done`` and the title is
    still junk, the fix did not take, so one new todo is filed (and holds
    until it is closed in turn). Returns the paper ref_ids a todo was (or, dry-run, would be)
    filed for.
    """
    from precis.config import load_config
    from precis.identity import is_placeholder_title
    from precis.ingest.pdf_sidecar import is_garbage_title, is_pii
    from precis.store.types import Tag
    from precis.utils.handle_registry import format_handle

    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.owner_login,
                   COALESCE(ck.id_value, ''),
                   EXISTS (SELECT 1 FROM ref_identifiers d
                            WHERE d.ref_id = r.ref_id AND d.id_kind = 'doi')
              FROM refs r
              LEFT JOIN ref_identifiers ck
                     ON ck.ref_id = r.ref_id AND ck.id_kind = 'cite_key'
             WHERE r.kind = 'paper'
               AND r.retired_at IS NULL
               AND r.created_at < now() - make_interval(days => %s)
               AND NOT EXISTS (
                     SELECT 1 FROM refs t
                      WHERE t.kind = 'todo'
                        AND t.meta->>'source' = %s
                        AND t.meta->>'paper_ref_id' = r.ref_id::text
                        AND NOT EXISTS (
                              SELECT 1 FROM ref_tags rt
                                JOIN tags tg USING (tag_id)
                               WHERE rt.ref_id = t.ref_id
                                 AND tg.namespace = 'STATUS'
                                 AND tg.value = 'done'))
             ORDER BY r.ref_id
            """,
            (grace_days, TITLE_FIX_SOURCE),
        ).fetchall()

    todo: list[tuple[int, str, str | None, str]] = []
    for ref_id, title, owner, cite_key, has_doi in rows:
        t = (title or "").strip()
        if is_placeholder_title(t):
            if has_doi:
                continue  # paper_meta_enrich / requeue_placeholder_title_papers
        elif not (is_pii(t) or is_garbage_title(t)):
            continue
        todo.append((int(ref_id), t, owner, cite_key))
    if limit:
        todo = todo[:limit]
    if dry_run:
        return [t[0] for t in todo]

    default_owner = load_config().owner
    for ref_id, t, owner, cite_key in todo:
        login = owner or default_owner
        handle = f"paper:{format_handle('paper', ref_id)}"
        text = (
            f"Fix title/metadata of {handle} (cite_key {cite_key or '?'}): "
            f"stored title {t!r} is junk and automation could not repair it. "
            "Set the real title (and authors/year) on the paper; until then "
            "citations resolve to an untitled paper."
        )
        with store.tx() as conn:
            todo_ref = store.insert_ref(
                kind="todo",
                slug=None,
                title=text,
                meta={"source": TITLE_FIX_SOURCE, "paper_ref_id": ref_id},
                conn=conn,
            )
            store.add_tag(
                todo_ref.id,
                Tag.closed("STATUS", "open"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            for tag in (f"waiting-for:{login}", TITLE_FIX_TAG):
                store.add_tag(
                    todo_ref.id, Tag.parse_strict(tag), set_by="system", conn=conn
                )
    if todo:
        log.info("raise_junk_title_papers: filed %d title-fix todo(s)", len(todo))
    return [t[0] for t in todo]


def requeue_papers_for_enrich(
    store: Store, ref_ids: list[int], *, dry_run: bool = True
) -> list[int]:
    """Re-arm ``paper_meta_enrich`` over the named papers (``precis enrich-rearm``).

    The enrich pass visits each paper once and stamps
    ``meta.authors_resolved_at``; a paper visited before the pass learned a
    new field (volume/number/pages, say) never gets it. This clears that
    stamp on the given ``ref_ids`` so the pass re-claims them on its next
    cycle. Nothing is fetched or written beyond the stamp and a
    ``paper_reconcile``/``enrich_rearmed`` breadcrumb.

    Selected: live papers that carry a DOI (nothing for Crossref to resolve
    otherwise) and currently have the stamp (an unstamped one is claimable
    already). A re-visit is safe: the pass never overwrites
    human-verified authors or any meta field the ref already carries
    (fill-blanks-only).

    Returns the selected ref_ids, in the order given, de-duplicated; a dry
    run returns what it *would* act on without writing.
    """
    wanted = list(dict.fromkeys(int(i) for i in ref_ids))
    if not wanted:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.ref_id = ANY(%s)
               AND r.kind = 'paper'
               AND r.retired_at IS NULL
               AND r.meta ? 'authors_resolved_at'
               AND EXISTS (
                     SELECT 1 FROM ref_identifiers ri
                      WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'doi'
                   )
            """,
            (wanted,),
        ).fetchall()
    eligible = {int(r[0]) for r in rows}
    selected = [i for i in wanted if i in eligible]

    if not dry_run:
        for ref_id in selected:
            with store.tx() as conn:
                conn.execute(
                    "UPDATE refs SET meta = meta - 'authors_resolved_at' "
                    "WHERE ref_id = %s",
                    (ref_id,),
                )
                store.append_event(
                    ref_id,
                    source="paper_reconcile",
                    event="enrich_rearmed",
                    payload={"reason": "operator re-arm (enrich-rearm)"},
                    conn=conn,
                )
        log.info("requeue_papers_for_enrich: re-armed %d paper(s)", len(selected))
    return selected


__all__ = [
    "BODILESS_HEAL_SOURCE",
    "BodilessOutcome",
    "MetadataHygieneStats",
    "bodiless_extract_per_pass",
    "collapse_superseded_chains",
    "heal_bodiless_pdfs",
    "heal_drifted_cards",
    "is_filename_like_title",
    "metadata_hygiene_stats",
    "migrate_dangling_paper_links",
    "requeue_front_matter_only_papers",
    "requeue_papers_for_enrich",
    "requeue_placeholder_title_papers",
    "requeue_stranded_fetches",
]
