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

All are dry-run by default and idempotent: a clean corpus yields empty
results and the next pass is a cheap no-op.
"""

from __future__ import annotations

import logging
import os
import re
from datetime import UTC, datetime
from typing import Any

from psycopg.types.json import Jsonb

from precis.ingest.cards import rewrite_cards
from precis.store import Store

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
            "WHERE r.kind = 'paper' AND r.deleted_at IS NULL "
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
        author_names = [a.get("name", "") for a in (ref.authors or []) if a.get("name")]
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
                author_names=author_names,
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
            "WHERE tgt.kind = 'paper' AND tgt.deleted_at IS NOT NULL "
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
               AND r.deleted_at IS NULL
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


__all__ = [
    "collapse_superseded_chains",
    "heal_drifted_cards",
    "migrate_dangling_paper_links",
]
