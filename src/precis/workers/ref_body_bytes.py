"""The ``ref_body_bytes`` maintenance passes — the write-path-free way
migration 0266's stored per-ref body byte count stays current.

Why a pass, not a trigger: keeping ``sum(octet_length(text))`` over a
ref's live body chunks on the ``refs`` row with chunk triggers put a
NO KEY UPDATE lock on that row for every body-chunk write, held to
commit — a long ingest transaction then failed any concurrent
``UPDATE refs WHERE ref_id=…`` on prod's 5 s ``lock_timeout`` (the
gr537784 class 0265 fixed), and every apply re-fired the whole refs
trigger stack plus non-HOT tuple churn on the GIN indexes. The sum is a
display hint — minutes of staleness are fine — so the writes moved off
the hot path entirely: chunk writes pay nothing; this pass finds the
refs they touched and recomputes the side table
(:data:`ref_body_bytes`) in small ``ref_id``-ordered batches.

Two cadences drive it:

* ``ref_body_bytes`` (the incremental fire) — refs with a chunk
  ``created_at`` or ``retired_at`` past the stored watermark
  (``app_settings[ref_body_bytes:watermark]``; covers INSERT, retire,
  and replace's insert half). The new watermark is
  ``scan-start − _SCAN_OVERLAP``: a transaction still open when the
  dirty scan ran commits with a ``created_at`` *before* the scan
  instant, so the overlap re-finds it next fire — idempotent, just
  recompute.
* ``ref_body_bytes_verify`` (daily) — re-walks every ref in batches;
  the net for what no timestamp can see: a pure chunk DELETE, or an
  ingest transaction open longer than the overlap.

Both modes recompute identically (:func:`recompute_body_byte_counts`)
— one grouped ``SUM(octet_length(text))`` per batch, upserted
``INSERT … ON CONFLICT DO UPDATE``, in short transactions with a
``SET LOCAL lock_timeout`` so a wedged row can't hold the pass. The
same helper backs ``precis backfill-ref-bytes`` — there is exactly one
recompute.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from psycopg import Connection

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: ``app_settings`` key holding the incremental scan's high-water mark —
#: ISO-8601 UTC. Same ``app_settings`` idiom ``workers/memory_recall.py``
#: and ``workers/revisions_unrecorded.py`` use for their durable markers.
WATERMARK_KEY = "ref_body_bytes:watermark"

#: Recompute batch — refs per short transaction.
BATCH = 200

#: Per-batch lock wait — a batch that can't get its locks in this window
#: is skipped (counted failed), not allowed to wedge the cadence behind
#: a long reader. Matches the migration's own bound.
BATCH_LOCK_TIMEOUT = "5s"

#: Per-batch statement bound — one grouped sum over ~200 refs' chunks.
BATCH_STATEMENT_TIMEOUT = "60s"

#: How far back past the scan instant the stored watermark reaches —
#: covers transactions open-but-uncommitted when the dirty scan ran
#: (their ``created_at`` is the transaction's *start*, so it predates
#: the scan). Anything open longer than this is left to the daily
#: verify pass — staleness is a display hint, not correctness.
_SCAN_OVERLAP = timedelta(minutes=15)

#: Refs whose body chunks changed since the watermark — INSERTs via
#: ``created_at``, retires (and any other ``retired_at`` write) via
#: ``retired_at``. The UNION keeps each side a clean range probe on its
#: 0266 index; replace (DELETE + INSERT) shows up on the INSERT half.
_DIRTY_REFS = """
SELECT ref_id FROM chunks WHERE created_at > %s
UNION
SELECT ref_id FROM chunks WHERE retired_at > %s
ORDER BY ref_id
"""

#: Page every ref for the verify pass — ``ref_id`` order, keyset-paged.
_ALL_REFS_PAGE = """
SELECT ref_id FROM refs WHERE ref_id > %s ORDER BY ref_id LIMIT %s
"""

#: One grouped recompute for a batch of ref ids — the INNER JOIN to
#: ``refs`` drops a dirty id whose ref vanished between scan and write
#: (the FK would reject it), the LEFT JOIN to ``chunks`` gives a
#: chunk-less ref its honest 0 (a real row, not left absent to be
#: re-picked forever). The ``IS DISTINCT FROM`` guard skips the write
#: when the stored value is already right: the daily verify then costs
#: reads only, no dead-tuple churn over unchanged refs.
_RECOMPUTE = """
INSERT INTO ref_body_bytes (ref_id, body_bytes, computed_at)
SELECT b.ref_id, COALESCE(SUM(octet_length(c.text)), 0)::bigint, now()
  FROM (SELECT unnest(%s::bigint[]) AS ref_id) b
  JOIN refs r ON r.ref_id = b.ref_id
  LEFT JOIN chunks c
    ON c.ref_id = b.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
 GROUP BY b.ref_id
ON CONFLICT (ref_id) DO UPDATE
   SET body_bytes = EXCLUDED.body_bytes,
       computed_at = EXCLUDED.computed_at
 WHERE ref_body_bytes.body_bytes IS DISTINCT FROM EXCLUDED.body_bytes
"""


def recompute_body_byte_counts(conn: Connection, ref_ids: list[int]) -> int:
    """Recompute and upsert ``ref_body_bytes`` for ``ref_ids`` inside the
    caller's transaction. Returns rows written (``rowcount``) — an
    unchanged verify row is skipped by the ``IS DISTINCT FROM`` guard and
    counts nothing. The one recompute shape — this pass and
    ``precis backfill-ref-bytes`` share it."""
    if not ref_ids:
        return 0
    cur = conn.execute(_RECOMPUTE, (list(ref_ids),))
    return int(cur.rowcount or 0)


def _recompute_batched(store: Store, ref_ids: list[int]) -> tuple[int, int]:
    """Recompute ``ref_ids`` in :data:`BATCH`-sized transactions; returns
    ``(rows written, refs that failed)``. A failed batch is logged and
    skipped — the next fire (or verify) re-picks it."""
    written = 0
    failed = 0
    for i in range(0, len(ref_ids), BATCH):
        batch = ref_ids[i : i + BATCH]
        try:
            with store.pool.connection() as conn:
                conn.execute(
                    "SELECT set_config('lock_timeout', %s, true)",
                    (BATCH_LOCK_TIMEOUT,),
                )
                conn.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (BATCH_STATEMENT_TIMEOUT,),
                )
                written += recompute_body_byte_counts(conn, batch)
        except Exception:
            log.exception(
                "ref_body_bytes: recompute batch of %d refs failed (first id %d)",
                len(batch),
                batch[0],
            )
            failed += len(batch)
    return written, failed


def _read_watermark(store: Store, fallback: datetime) -> datetime:
    """The stored scan watermark; ``fallback`` (scan-start − overlap) when
    absent or unparseable — a first fire then covers only the recent
    window, with the daily verify and ``precis backfill-ref-bytes``
    filling the rest."""
    from precis.budget import settings as app_settings

    raw = app_settings.get_setting(store, WATERMARK_KEY)
    if not raw:
        return fallback
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        log.warning(
            "ref_body_bytes: unparseable watermark %r — rescanning from %s",
            raw,
            fallback.isoformat(),
        )
        return fallback
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _run_incremental(store: Store) -> BatchResult:
    """One incremental fire: dirty refs since the watermark, recomputed,
    then the watermark advances to ``scan-start − _SCAN_OVERLAP`` — only
    after a clean recompute pass, so a mid-fire failure just rescans."""
    from precis.budget import settings as app_settings

    watermark = _read_watermark(store, datetime.now(UTC) - _SCAN_OVERLAP)
    with store.pool.connection() as conn:
        row = conn.execute("SELECT now()").fetchone()
        assert row is not None  # now() never returns empty
        scan_start = row[0]
        if scan_start.tzinfo is None:
            scan_start = scan_start.replace(tzinfo=UTC)
        watermark = min(watermark, scan_start)
        dirty = [
            int(r[0])
            for r in conn.execute(_DIRTY_REFS, (watermark, watermark)).fetchall()
        ]
    written, failed = _recompute_batched(store, dirty)
    if not failed:
        app_settings.set_setting(
            store, WATERMARK_KEY, (scan_start - _SCAN_OVERLAP).isoformat()
        )
    log.info(
        "ref_body_bytes: %d dirty ref(s), %d row(s) written, %d failed",
        len(dirty),
        written,
        failed,
    )
    return BatchResult(
        handler="ref_body_bytes",
        claimed=len(dirty),
        ok=len(dirty) - failed,
        failed=failed,
        extra={"rows_written": written},
    )


def _run_verify(store: Store) -> BatchResult:
    """One full-verification fire: every ref re-walked in keyset pages —
    the net for pure chunk DELETEs (no timestamp anywhere records one)
    and for refs the incremental window missed. Unchanged rows cost a
    read only (the ``IS DISTINCT FROM`` guard)."""
    scanned = 0
    written = 0
    failed = 0
    last = 0
    while True:
        with store.pool.connection() as conn:
            page = [
                int(r[0])
                for r in conn.execute(_ALL_REFS_PAGE, (last, BATCH)).fetchall()
            ]
        if not page:
            break
        w, f = _recompute_batched(store, page)
        written += w
        failed += f
        scanned += len(page)
        last = page[-1]
    log.info(
        "ref_body_bytes_verify: %d ref(s) scanned, %d row(s) written, %d failed",
        scanned,
        written,
        failed,
    )
    return BatchResult(
        handler="ref_body_bytes_verify",
        claimed=scanned,
        ok=scanned - failed,
        failed=failed,
        extra={"rows_written": written},
    )


def run_ref_body_bytes_pass(store: Store, *, verify: bool = False) -> BatchResult:
    """One fire of the ``ref_body_bytes`` maintenance pass.

    ``verify=False`` — the incremental path (dirty refs since the
    stored watermark). ``verify=True`` — the daily full re-walk. See
    the module docstring for the two-cadence shape."""
    if verify:
        return _run_verify(store)
    return _run_incremental(store)


__all__ = [
    "BATCH",
    "WATERMARK_KEY",
    "recompute_body_byte_counts",
    "run_ref_body_bytes_pass",
]
