"""Nursery worker — Slice 3 of ``docs/design/todo-tree-plan.md``.

Pattern-matches the todo tree (and the worker fleet) for local
incoherence and raises a ``kind='alert'`` per condition through
:mod:`precis.alerts`. The detectors are SQL-only — no LLM call, no
opus / sonnet budget; Settled decision #5 in the plan ("Nursery model
= sonnet") was written assuming an LLM tier, but the actual detection
rules are deterministic pattern matches that don't need reasoning.

Detector catalogue (each is one SQL query, returns a list of
finding rows):

* **orphans** — open todos that have no ``level:strategic`` ancestor
  (knob #6: strategic invariant; every open leaf must root under
  *some* strategic).
* **stale claims** — leaves carrying ``claimed-by:<x>`` for more
  than ``STALE_CLAIM_HOURS=3`` without a status change. The
  claim's age is read from ``ref_tags.created_at`` — the same
  source ADR 0016 uses for the ingest claim TTL.
* **long waits** — leaves carrying ``waiting-for:*`` for more than
  ``LONG_WAIT_DAYS=7``.
* **stuck doable** — open leaves with no claim, no waiting tag, and
  ``created_at`` older than ``STUCK_DOABLE_HOURS=24``. The
  rotation should have picked these up; if they're still here, the
  doable filter is rejecting them for a reason worth surfacing.
* **stalled recurrings** — ``level:recurring`` refs whose most
  recent spawned child has been open more than the schedule's
  period. The Slice-4 collision-skip leaves the prior tick on the
  queue; without nursery surfacing, the operator can't see why
  ticks have stopped piling up.
* **spin loops** — any ``(ref_id, source)`` emitting more than
  ``SPIN_LOOP_EVENTS_24H`` ``ref_events`` in 24h (a derived-queue
  worker re-claiming the same ref every pass).

Each finding becomes an ``alert`` under ``alert_source =
nursery:<category>``, deduped on ``fingerprint = "<category>:<ref_id>"``
(see :mod:`precis.alerts`). A repeat sighting bumps the alert's
``seen_count``; a finding that disappears auto-resolves its alert on
the next pass (``resolve_stale_alerts`` per source). This replaced the
old per-minute ``kind='memory'`` digest, which conflated ops telemetry
with reflective thought and — because the spin-loop finding set churns
every second — spun on itself writing thousands of near-dup memories a
day. Alerts dedup per *condition* instead, and are surfaced by the
``/alerts`` web tab, not semantic search.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from precis.alerts import raise_alert, resolve_stale_alerts
from precis.handlers._todo_guards import todo_root_sql
from precis.store import Store
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


# Threshold knobs (hours / days). Mirrored in the skill so the
# operator can find the canonical values without reading code.
STALE_CLAIM_HOURS = 3
LONG_WAIT_DAYS = 7
STUCK_DOABLE_HOURS = 24

#: A single (ref_id, source) emitting more than this many ``ref_events``
#: in 24h is almost certainly a worker spin loop — a derived-queue claim
#: re-picking the same ref every pass because a no-op / terminal-but-
#: retryable outcome never clears the claim predicate (the fetcher
#: retry-window-on-disabled-provider bug and the chase chunk-less-stub
#: loop were both ~150–1300/day per ref). A healthy ref sees a handful
#: of events a day, so 200 is comfortably above the noise floor.
SPIN_LOOP_EVENTS_24H = 200

#: Per-category alert severity (drives sort + colour on the /alerts
#: tab). Spin loops and stuck claims/recurrings burn resources or block
#: progress → ``warn``; orphans / long-waits / stuck-doable are hygiene
#: nudges → ``info``. None are ``critical`` — nursery flags drift, not
#: outages.
_SEVERITY: dict[str, str] = {
    "spin-loop": "warn",
    "orphan": "info",
    "stale-claim": "warn",
    "long-wait": "info",
    "stuck-doable": "info",
    "stalled-recurring": "warn",
}


@dataclass(frozen=True, slots=True)
class Finding:
    """One nursery hit. ``ref_id`` + ``category`` is the dedup key."""

    category: str
    ref_id: int
    title: str
    detail: str  # one-line human summary for the alert


#: Detectors in catalogue order, each paired with its category. The
#: category is both the alert sub-source (``nursery:<category>``) and
#: the dedup-fingerprint prefix. Each detector self-limits to 50 hits.
_DETECTORS: tuple[tuple[str, Callable[[Store], list[Finding]]], ...] = (
    ("spin-loop", lambda s: _detect_spin_loops(s)),
    ("orphan", lambda s: _detect_orphans(s)),
    ("stale-claim", lambda s: _detect_stale_claims(s)),
    ("long-wait", lambda s: _detect_long_waits(s)),
    ("stuck-doable", lambda s: _detect_stuck_doable(s)),
    ("stalled-recurring", lambda s: _detect_stalled_recurrings(s)),
)


def run_nursery_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Detect; raise/refresh an alert per finding; auto-resolve cleared.

    Counters in the returned ``BatchResult``:

    * ``claimed`` = number of findings surfaced this pass (raised or
      refreshed alerts)
    * ``ok`` = number of alerts auto-resolved this pass (conditions
      that cleared)
    * ``failed`` = 0 (no failure mode in the SQL detectors)

    Per detector: raise an ``alert`` for every current finding (deduped
    on ``"<category>:<ref_id>"`` so a repeat just bumps ``seen_count``),
    then resolve any open alert of that source whose fingerprint is no
    longer present. Empty findings still run the resolve sweep, so a
    fixed problem disappears from the open list on the next pass.
    """
    raised = 0
    resolved = 0
    for category, detect in _DETECTORS:
        source = f"nursery:{category}"
        severity = _SEVERITY.get(category, "warn")
        findings = detect(store)
        live: list[str] = []
        for f in findings:
            fp = f"{f.category}:{f.ref_id}"
            live.append(fp)
            raise_alert(
                store,
                source=source,
                fingerprint=fp,
                title=f"[{f.category}] {f.title}",
                detail=f.detail,
                severity=severity,
                subject_ref_id=f.ref_id,
            )
        raised += len(findings)
        resolved += resolve_stale_alerts(store, source=source, live_fingerprints=live)
    if raised or resolved:
        log.info("nursery: %d alerts raised/refreshed, %d resolved", raised, resolved)
    return BatchResult(handler="nursery", claimed=raised, ok=resolved, failed=0)


# ── orphans ────────────────────────────────────────────────────────


def _detect_orphans(store: Store) -> list[Finding]:
    """Open todos whose ancestor chain has no ``level:strategic`` root.

    Walks ``parent_id`` to the topmost ancestor. If that ancestor
    doesn't carry the ``level:strategic`` open tag, the todo is an
    orphan. Recurring subtrees (under ``level:recurring`` roots) are
    excluded — they're scheduled work, not strategic work, and the
    plan explicitly carves them out of the strategic invariant.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            WITH RECURSIVE walk(ref_id, parent_id, root_id) AS (
                SELECT ref_id, parent_id, ref_id
                  FROM refs
                 WHERE kind = 'todo' AND deleted_at IS NULL
                UNION ALL
                SELECT w.ref_id, r.parent_id, r.ref_id
                  FROM walk w
                  JOIN refs r ON r.ref_id = w.parent_id
                 WHERE r.kind = 'todo' AND r.deleted_at IS NULL
            ),
            roots AS (
                -- ADR 0045: the root is the topmost *todo* — a folder
                -- parent above it is placement, not tree membership.
                SELECT DISTINCT ON (w.ref_id) w.ref_id AS leaf_id,
                       w.root_id
                  FROM walk w
                  JOIN refs r ON r.ref_id = w.root_id
                 WHERE {todo_root_sql("r")}
                 ORDER BY w.ref_id, w.root_id
            )
            SELECT r.ref_id, r.title
              FROM refs r
              JOIN roots rt ON rt.leaf_id = r.ref_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                       WHERE rtg.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
               -- Root is not strategic
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                    WHERE rtg.ref_id = rt.root_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:strategic'
               )
               -- And not in a recurring subtree (root is not recurring either)
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rtg JOIN tags t ON t.tag_id = rtg.tag_id
                    WHERE rtg.ref_id = rt.root_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
             ORDER BY r.ref_id
             LIMIT 50
            """,
        ).fetchall()
    return [
        Finding(
            category="orphan",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                "open todo with no strategic ancestor — root needs "
                "a ``level:strategic`` tag or this leaf needs to be "
                "re-parented under one"
            ),
        )
        for r in rows
    ]


# ── stale claims ──────────────────────────────────────────────────


def _detect_stale_claims(store: Store) -> list[Finding]:
    """Leaves with ``claimed-by:<x>`` older than ``STALE_CLAIM_HOURS``.

    The claim's age is ``ref_tags.created_at`` on the open tag row.
    A claim older than the threshold without a STATUS change probably
    means the worker died mid-task (process crash, network split, OOM)
    — the leaf is stuck under a phantom claim.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, t.value AS claim, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'claimed-by:%%'
               AND rt.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{STALE_CLAIM_HOURS} hours",),
        ).fetchall()
    out: list[Finding] = []
    for r in rows:
        claim = str(r[2])
        hours = _hours_since(r[3])
        out.append(
            Finding(
                category="stale-claim",
                ref_id=int(r[0]),
                title=_first_line(r[1]),
                detail=(
                    f"claimed {hours:.0f}h ago by {claim.removeprefix('claimed-by:')}; "
                    f"if the worker died mid-task, release the claim or "
                    f"mark STATUS:auto-timeout"
                ),
            )
        )
    return out


# ── long waits ────────────────────────────────────────────────────


def _detect_long_waits(store: Store) -> list[Finding]:
    """Leaves with ``waiting-for:*`` tagged more than ``LONG_WAIT_DAYS``.

    The wait may still be legitimate (a slow API, a paper that takes
    weeks to ingest) but past the threshold the operator probably
    wants to know about it. The detail line names the wait target so
    triage doesn't require an extra ``get``.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, t.value AS wait, rt.created_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND t.value LIKE 'waiting-for:%%'
               AND rt.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2 JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{LONG_WAIT_DAYS} days",),
        ).fetchall()
    out: list[Finding] = []
    for r in rows:
        wait = str(r[2])
        days = _days_since(r[3])
        out.append(
            Finding(
                category="long-wait",
                ref_id=int(r[0]),
                title=_first_line(r[1]),
                detail=(
                    f"waiting {days:.0f}d on {wait.removeprefix('waiting-for:')}; "
                    f"check whether the dependency is still alive"
                ),
            )
        )
    return out


# ── stuck doable ──────────────────────────────────────────────────


def _detect_stuck_doable(store: Store) -> list[Finding]:
    """Open leaves with no claim, no wait, created >24h ago.

    These are leaves the doable rotation *could* be picking but isn't.
    Causes are usually: PRIO 10 buried by louder strategics, paused
    ancestor that the operator forgot about, or a tag mistake. The
    digest can't diagnose; it just surfaces the existence so the
    operator notices.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.created_at
              FROM refs r
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND r.created_at < now() - %s::interval
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) IN ('open', 'doing')
               -- Leaf (no children)
               AND NOT EXISTS (
                   SELECT 1 FROM refs c
                    WHERE c.parent_id = r.ref_id
                      AND c.deleted_at IS NULL
               )
               -- No claim, no wait, no asking
               AND NOT EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN'
                      AND (t.value LIKE 'claimed-by:%%'
                           OR t.value LIKE 'waiting-for:%%'
                           OR t.value = 'ask-user'
                           OR t.value LIKE 'ask-user:%%'
                           OR t.value = 'level:recurring')
               )
               -- Not blocked
               AND NOT EXISTS (
                   SELECT 1 FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
                    WHERE l.src_ref_id = r.ref_id
                      AND l.relation = 'blocked-by'
                      AND b.deleted_at IS NULL
                      AND COALESCE(
                            (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                              WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                            'open'
                          ) NOT IN ('done', 'won''t-do')
               )
             ORDER BY r.ref_id
             LIMIT 50
            """,
            (f"{STUCK_DOABLE_HOURS} hours",),
        ).fetchall()
    return [
        Finding(
            category="stuck-doable",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"doable for {_hours_since(r[2]):.0f}h with no claim, no wait, "
                f"no blocker — check the strategic rotation or its PRIO"
            ),
        )
        for r in rows
    ]


# ── stalled recurrings ────────────────────────────────────────────


def _detect_stalled_recurrings(store: Store) -> list[Finding]:
    """``level:recurring`` refs whose most recent spawned child has been
    open more than ~1.5x the recurring's natural cadence.

    The Slice-4 collision-skip leaves the prior tick on the queue
    when it stalls; without nursery surfacing the operator can't
    see why ticks have stopped piling up. We approximate the
    "1.5x cadence" as: child has been open for at least 1h, or
    since the recurring's previous spawn event — whichever is
    longer. The 1h floor catches near-immediate stalls (a daily
    recurring that crashed on its first tick).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT
              rec.ref_id AS rec_id,
              rec.title AS rec_title,
              child.ref_id AS child_id,
              child.title AS child_title,
              child.created_at AS child_created
              FROM refs rec
              JOIN refs child ON child.parent_id = rec.ref_id
                              AND child.deleted_at IS NULL
                              AND child.meta ? 'spawned_for_tick'
             WHERE rec.kind = 'todo' AND rec.deleted_at IS NULL
               AND EXISTS (
                   SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = rec.ref_id
                      AND t.namespace = 'OPEN'
                      AND t.value = 'level:recurring'
               )
               AND child.created_at < now() - interval '1 hour'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = child.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do', 'auto-timeout')
               AND child.created_at = (
                   SELECT max(c2.created_at) FROM refs c2
                    WHERE c2.parent_id = rec.ref_id
                      AND c2.deleted_at IS NULL
                      AND c2.meta ? 'spawned_for_tick'
               )
             ORDER BY rec.ref_id
             LIMIT 50
            """,
        ).fetchall()
    return [
        Finding(
            category="stalled-recurring",
            ref_id=int(r[0]),
            title=_first_line(r[1]),
            detail=(
                f"recurring #{int(r[0])} stalled: last spawn "
                f"(child #{int(r[2])}) has been open "
                f"{_hours_since(r[4]):.0f}h — collision-skip will keep "
                f"new ticks from piling up; resolve or auto-timeout"
            ),
        )
        for r in rows
    ]


# ── spin loops ────────────────────────────────────────────────────


def _detect_spin_loops(store: Store) -> list[Finding]:
    """Refs a background worker is hammering — >N events/24h, one source.

    Catches the failure mode where a derived-queue worker re-claims the
    same ref every pass because its no-op / retryable outcome never
    clears the claim predicate. The detail names the source + event +
    rate so triage starts at the worker, not the ref. ``category`` is
    ``spin-loop`` and the dedup key is ``(ref_id, source)`` collapsed
    onto the ref — a loop on the same ref from the same source is one
    finding regardless of how the count drifts pass-to-pass.

    Cheap: a single grouped scan of the last 24h of ``ref_events``,
    which is GIN/btree-indexed on ``ts``. Capped at 50 like the others.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, source,
                   (array_agg(event ORDER BY ts DESC))[1] AS last_event,
                   count(*)::int AS n
              FROM ref_events
             WHERE ts > now() - interval '24 hours'
             GROUP BY ref_id, source
            HAVING count(*) > %s
             ORDER BY count(*) DESC
             LIMIT 50
            """,
            (SPIN_LOOP_EVENTS_24H,),
        ).fetchall()
    out: list[Finding] = []
    for r in rows:
        ref_id, source, last_event, n = int(r[0]), str(r[1]), r[2], int(r[3])
        out.append(
            Finding(
                category="spin-loop",
                ref_id=ref_id,
                title=f"{source} on #{ref_id}",
                detail=(
                    f"{n} {source} events in 24h (last: {last_event or '?'}) "
                    f"— a worker is re-claiming this ref every pass; check "
                    f"the {source} claim predicate's backoff/retry window"
                ),
            )
        )
    return out


# ── small helpers ─────────────────────────────────────────────────


def _first_line(title: str | None) -> str:
    """Trim to one line for digest readability."""
    if not title:
        return "(no title)"
    head = title.split("\n", 1)[0]
    if len(head) > 80:
        head = head[:80].rstrip() + "…"
    return head


def _hours_since(ts: datetime | None) -> float:
    if ts is None:
        return 0.0
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return (datetime.now(UTC) - ts).total_seconds() / 3600.0


def _days_since(ts: datetime | None) -> float:
    return _hours_since(ts) / 24.0


__all__ = [
    "LONG_WAIT_DAYS",
    "SPIN_LOOP_EVENTS_24H",
    "STALE_CLAIM_HOURS",
    "STUCK_DOABLE_HOURS",
    "Finding",
    "run_nursery_pass",
]
