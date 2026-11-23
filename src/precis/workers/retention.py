"""The ``retention`` pass — gr690338's first policy slice: soft-delete news
that was never used.

Reto's ruling (gr690338 comment 3, 2026-11-19) is the spec, and it says:
**soft-delete only, reversible, no hard purge anywhere.** This pass is the
first slice of that policy, run as a recurring worker cadence:

* **Selection** — live news older than 90 days with no read receipt,
  durable read stamp, link in either direction, or bracket citation
  in a live chunk. Receipts carry a one-hour TTL. Historical reads whose
  receipts were pruned before durable news tracking can only be inferred
  from remaining evidence.
* **First run is report-only** — the pass counts what it would act on,
  writes the per-kind counts as the fire's job summary (the
  ``BatchResult.extra`` payload that lands in ``worker_logs``, plus an
  ``app_settings`` marker so the report survives log rotation) and stops.
  Subsequent fires act. Two deliberate operator steps before any write:
  light the ``retention`` service (``prio > 0``), then let the first lit
  fire report.
* **Acting fires** lock each target and check eligibility again in the
  update that stamps ``retired_at`` and ``meta['retention']``. One
  transaction per ref isolates failures. The row and its vectors stay
  recoverable via ``restore_ref``.

Deferred by the same ruling: the second candidate (resolved alerts older
than 90 days) lands behind a config flag in a later slice, once the news
slice has run clean for one cycle — :data:`COUNTS_KINDS` carries the
per-kind shape so that addition is a small step. The pass should also
pull its first-run state from the unified worker stash once td730698
lands; until then the ``app_settings`` marker is the state.

Candidate pages use a ref-id cursor. The citation probe can use the
``chunks.tsv`` GIN index for handle tokens, then verifies bracket syntax
with the same delimiter grammar as findings.
spends=False: pure local SQL, no LLM.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from precis.store._refs_ops import CITE_LIST_SEP
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: The ``service_config`` service name an operator lights (``prio > 0``)
#: to arm the pass (the ``dream_consolidate`` dark-gate shape).
SERVICE_NAME = "retention"

#: A news ref older than this is retention-eligible.
NEWS_UNUSED_DAYS = 90

#: The reason stamped on refs this pass retires (the ruling's literal).
NEWS_RETIRE_REASON = "retention:news-unused-90d"

#: ``app_settings`` key whose presence arms acting fires — written by the
#: first lit fire with the counts it would have acted on.
FIRST_REPORT_KEY = "retention:first_report"

#: ``refs.meta`` key carrying the retention breadcrumb (reason + when).
META_RETENTION = "retention"

#: Cap on candidates per fire — a news backlog converges over days rather
#: than in one giant sweep inside the cadence deadline.
MAX_CANDIDATES_PER_FIRE = 2000

#: The kinds the policy currently acts on (the alert candidate joins in a
#: later slice, per the ruling) — the per-kind counts dict uses these keys.
COUNTS_KINDS = ("news",)

# Shared by selection and the locked check immediately before retirement.
_NEWS_UNUSED_SQL = """
       r.kind = 'news'
   AND r.retired_at IS NULL
   AND r.created_at < now() - make_interval(days => %(days)s)
   AND r.last_viewed_at IS NULL
   AND r.last_recalled_at IS NULL
   AND NOT EXISTS (SELECT 1 FROM ref_reads rr WHERE rr.ref_id = r.ref_id)
   AND NOT EXISTS (
           SELECT 1 FROM read_receipts rr
            WHERE rr.identity LIKE 'news:' || r.ref_id::text || ':%%'
       )
   AND NOT EXISTS (
           SELECT 1 FROM links l
            WHERE l.src_ref_id = r.ref_id OR l.dst_ref_id = r.ref_id
       )
"""

_CANDIDATES_SQL = f"""
SELECT r.ref_id
  FROM refs r
 WHERE r.ref_id > %(after)s AND ({_NEWS_UNUSED_SQL})
 ORDER BY r.ref_id
 LIMIT %(lim)s
"""

_CITATIONS_SQL = """
SELECT c.text
  FROM chunks c
 WHERE c.retired_at IS NULL
   AND c.tsv @@ to_tsquery('english', %(query)s)
   AND c.text ~ %(pattern)s
"""

_HANDLE = r"[a-z]{2}[0-9]+"


def _citation_pattern(handle: str) -> str:
    return rf"\[(?:{_HANDLE}{CITE_LIST_SEP})*{handle}(?:{CITE_LIST_SEP}{_HANDLE})*\]"


def retention_enabled(store: Store) -> bool:
    """The dark gate: some ``service_config`` row lights ``retention``."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (SERVICE_NAME,),
        ).fetchone()
    return row is not None


def _news_retention_candidates(store: Store) -> list[int]:
    """Page through unused news until the post-citation cap is filled."""
    targets: list[int] = []
    after = 0
    with store.pool.connection() as conn:
        while len(targets) < MAX_CANDIDATES_PER_FIRE:
            rows = conn.execute(
                _CANDIDATES_SQL,
                {
                    "days": NEWS_UNUSED_DAYS,
                    "lim": MAX_CANDIDATES_PER_FIRE,
                    "after": after,
                },
            ).fetchall()
            if not rows:
                break
            page = [int(row[0]) for row in rows]
            cited = _cited_news_ids(conn, page)
            targets.extend(ref_id for ref_id in page if ref_id not in cited)
            after = page[-1]
            if len(page) < MAX_CANDIDATES_PER_FIRE:
                break
    return targets[:MAX_CANDIDATES_PER_FIRE]


def _cited_news_ids(conn: Connection, ref_ids: list[int]) -> set[int]:
    """Collect every news handle from each valid bracketed citation."""
    rows = conn.execute(
        _CITATIONS_SQL,
        {
            "query": " | ".join(f"nw{ref_id}" for ref_id in ref_ids),
            "pattern": _citation_pattern(r"nw[0-9]+"),
        },
    )
    wanted = set(ref_ids)
    return {
        ref_id
        for (text,) in rows
        for bracket in re.findall(_citation_pattern(r"nw[0-9]+"), text)
        for handle in re.findall(r"nw([0-9]+)", bracket)
        if (ref_id := int(handle)) in wanted
    }


def _first_report_done(store: Store) -> bool:
    from precis.budget import settings as app_settings

    return app_settings.get_setting(store, FIRST_REPORT_KEY) is not None


def _write_first_report(store: Store, counts: dict[str, int]) -> None:
    import json

    from precis.budget import settings as app_settings

    doc = {"at": datetime.now(UTC).isoformat(), "counts": counts}
    try:
        app_settings.set_setting(store, FIRST_REPORT_KEY, json.dumps(doc))
    except Exception:
        # The marker is the state that arms acting fires — without it the
        # next lit fire reports again, which is the safe direction.
        log.exception("retention: first-report marker write failed")


def _retire_one(store: Store, ref_id: int) -> bool:
    """Lock the ref, then recheck use against a fresh statement snapshot."""
    with store.tx() as conn:
        locked = conn.execute(
            "SELECT 1 FROM refs WHERE ref_id = %s AND retired_at IS NULL FOR UPDATE",
            (ref_id,),
        ).fetchone()
        if locked is None:
            return False
        retired = conn.execute(
            f"""
            UPDATE refs r
               SET retired_at = now(), updated_at = now(),
                   meta = r.meta || jsonb_build_object(
                       %(meta_key)s::text, jsonb_build_object(
                           'reason', %(reason)s::text, 'at', %(at)s::text
                       )
                   )
             WHERE r.ref_id = %(ref_id)s AND ({_NEWS_UNUSED_SQL})
               AND NOT EXISTS ({_CITATIONS_SQL})
            """,
            {
                "ref_id": ref_id,
                "days": NEWS_UNUSED_DAYS,
                "query": f"nw{ref_id}",
                "pattern": _citation_pattern(f"nw{ref_id}"),
                "meta_key": META_RETENTION,
                "reason": NEWS_RETIRE_REASON,
                "at": datetime.now(UTC).isoformat(),
            },
        ).rowcount
    return retired == 1


def run_retention_pass(store: Store) -> BatchResult:
    """One retention tick: report the counts on the first lit fire, retire
    eligible news on the ones after.

    ``claimed`` = targets considered this fire; in report-only mode it is
    set with ``report_only=True`` so the loop does not hot-spin
    re-counting the same unstamped rows (gr514421). ``extra['counts']``
    carries the per-kind breakdown either way — the job summary."""
    if not retention_enabled(store):
        log.info("retention: dark — no live service_config row with prio > 0; no-op")
        return BatchResult(handler="retention", claimed=0, ok=0, failed=0)

    targets = _news_retention_candidates(store)
    counts = {kind: (len(targets) if kind == "news" else 0) for kind in COUNTS_KINDS}

    if not _first_report_done(store):
        _write_first_report(store, counts)
        log.warning(
            "retention: first run — report only, would soft-delete %d news ref(s) "
            "(%s); acting on subsequent fires",
            len(targets),
            ", ".join(f"{k}={v}" for k, v in counts.items()),
        )
        return BatchResult(
            handler="retention",
            claimed=len(targets),
            ok=0,
            failed=0,
            report_only=True,
            extra={"counts": counts, "first_run": True},
        )

    retired = 0
    failed = 0
    for ref_id in targets:
        try:
            if not _retire_one(store, ref_id):
                continue
            retired += 1
            log.info("retention: retired news ref #%d (%s)", ref_id, NEWS_RETIRE_REASON)
        except Exception:
            log.exception("retention: retire failed for news ref #%d", ref_id)
            failed += 1
    return BatchResult(
        handler="retention",
        claimed=len(targets),
        ok=retired,
        failed=failed,
        extra={"counts": counts},
    )


__all__ = [
    "COUNTS_KINDS",
    "FIRST_REPORT_KEY",
    "META_RETENTION",
    "NEWS_RETIRE_REASON",
    "NEWS_UNUSED_DAYS",
    "SERVICE_NAME",
    "retention_enabled",
    "run_retention_pass",
]
