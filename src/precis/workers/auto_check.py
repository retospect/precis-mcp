"""Auto-check worker pass — Slice 1b of ``docs/backlog/todo-tree-plan.md``.

Polls open todos whose ``meta.auto_check`` is non-null, dispatches
each to the registered evaluator, and either:

* flips ``STATUS:open|doing|blocked`` → ``STATUS:done`` when the
  evaluator returns ``True``, appending an ``auto-resolved`` event
  on ``ref_events``; or
* flips ``STATUS:...`` → ``STATUS:auto-timeout`` when
  ``meta.auto_check.timeout_at`` is in the past, appending an
  ``auto-timeout`` event.

A leaf that resolves and a leaf that times out are mutually
exclusive on any single pass; the timeout check fires first so a
leaf whose evaluator would also resolve doesn't get double-stamped.

This runs as a :class:`precis.workers.runner.RefPass` so the
existing ``precis worker`` cadence drains it alongside everything
else. The plan's 60-second poll interval is realised by the
worker's ``idle_seconds`` setting — the pass itself just chews
through whatever rows it finds and returns.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from precis.store import Store
from precis.store.types import Tag
from precis.workers.auto_check_evaluators import REGISTRY
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)


#: Statuses we'll act on. Refs in done / won't-do / auto-timeout
#: stay where they are even if the spec is still attached — auto-
#: resolving an already-closed leaf would lose the closure provenance.
_OPEN_STATUSES: frozenset[str] = frozenset({"open", "doing", "blocked", "paused"})


def run_auto_check_pass(store: Store, *, limit: int = 50) -> BatchResult:
    """Drain up to ``limit`` open auto_check leaves.

    Returns a :class:`BatchResult` whose:

    * ``claimed`` = number of leaves inspected this pass
    * ``ok`` = number flipped to ``STATUS:done`` (evaluator true)
    * ``failed`` = number flipped to ``STATUS:auto-timeout``

    The naming is slightly twisted to match the shared
    ``BatchResult`` schema (``ok`` / ``failed`` reads as "happy" /
    "unhappy" here, not "succeeded" / "failed" in the worker-error
    sense). The plan's accounting cares about counts, not labels.
    """
    candidates = _fetch_candidates(store, limit=limit)
    if not candidates:
        return BatchResult(handler="auto_check", claimed=0, ok=0, failed=0)

    n_ok = 0
    n_timeout = 0
    for ref_id, spec in candidates:
        try:
            handled = _process_one(store, ref_id, spec)
        except Exception:
            # An evaluator throwing on bad spec is a write-time bug
            # (validate_auto_check_spec should have caught it). Log
            # loudly so the bad row gets noticed; don't crash the
            # whole pass — the next leaf may be fine.
            log.exception("auto_check: evaluator raised on todo id=%d", ref_id)
            continue
        if handled == "done":
            n_ok += 1
        elif handled == "timeout":
            n_timeout += 1
        # ``"pending"`` is the common case — leaf stays open, no
        # writes happen. No counter bump.

    return BatchResult(
        handler="auto_check",
        claimed=len(candidates),
        ok=n_ok,
        failed=n_timeout,
    )


def _fetch_candidates(store: Store, *, limit: int) -> list[tuple[int, dict[str, Any]]]:
    """Find todos with non-null ``meta.auto_check`` and an open status.

    One round-trip. **Sampled at random, not ordered by ref_id.** The
    population was assumed to stay small ("asks + paper-waits are leaf counts"),
    and while it did, `ORDER BY ref_id LIMIT n` was harmless. It stopped being
    small — and a stable ascending order over an oversized set is a
    head-of-line starvation trap, because the leaves that pile up at the head
    are exactly the ones that *never resolve* (a parent whose job failed, or
    that still has a live child todo, evaluates "pending" forever and never
    leaves the set). On 2026-08-07 prod held 169 open auto_check leaves against
    a limit of 50: the pass re-evaluated the same lowest-ranked leaves every
    time, resolving nothing, and everything past the cutoff was never looked at
    once. The morning-brief tick sat at rank 161 with a succeeded job under it,
    so it never flipped to ``STATUS:done`` — and a recurring watch won't spawn
    its next tick while the last one is open, which silently killed both daily
    casts. Random sampling gives every leaf the same chance each pass, so no
    leaf can be starved by the ones in front of it, whatever the population
    does next.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->'auto_check'
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.retired_at IS NULL
               AND r.meta ? 'auto_check'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id
                         AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) = ANY(%s)
             ORDER BY random()
             LIMIT %s
            """,
            (sorted(_OPEN_STATUSES), limit),
        ).fetchall()
    if len(rows) >= limit:
        # Sampling removes the starvation, but not the latency: at N leaves and
        # a limit of L, a given leaf waits ~N/L passes to be looked at. Say so,
        # so a population that has quietly grown is visible before it becomes a
        # cadence outage again rather than after.
        log.warning(
            "auto_check: candidate set is at the %d-leaf pass limit — each leaf "
            "is now sampled roughly every %d passes; if this persists, the "
            "backlog of never-resolving leaves needs triage",
            limit,
            max(1, _open_auto_check_total(store) // limit),
        )
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _open_auto_check_total(store: Store) -> int:
    """Total open auto_check leaves — for the saturation warning only."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM refs r
             WHERE r.kind = 'todo'
               AND r.retired_at IS NULL
               AND r.meta ? 'auto_check'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id
                         AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) = ANY(%s)
            """,
            (sorted(_OPEN_STATUSES),),
        ).fetchone()
    return int(row[0]) if row else 0


def _process_one(store: Store, ref_id: int, spec: dict[str, Any]) -> str:
    """Inspect one leaf. Returns ``"done"`` / ``"timeout"`` / ``"pending"``."""
    # Timeout wins over resolve so the operator sees the timed-out
    # state even when the evaluator would also have resolved on the
    # same tick.
    timeout_raw = spec.get("timeout_at")
    if isinstance(timeout_raw, str):
        try:
            t_at = datetime.fromisoformat(timeout_raw)
            if t_at.tzinfo is None:
                t_at = t_at.replace(tzinfo=UTC)
            if datetime.now(UTC) >= t_at:
                _flip_status(store, ref_id, to="auto-timeout", event="auto-timeout")
                log.info("auto_check: todo id=%d → STATUS:auto-timeout", ref_id)
                return "timeout"
        except ValueError:
            # Malformed timeout_at: log + keep evaluating; the
            # evaluator might still resolve. The write-time validator
            # would have caught this on a fresh put — preserve forward
            # progress for refs that pre-date the validation.
            log.warning(
                "auto_check: todo id=%d has unparseable timeout_at=%r",
                ref_id,
                timeout_raw,
            )
    type_name = spec.get("type")
    evaluator = REGISTRY.get(type_name) if isinstance(type_name, str) else None
    if evaluator is None:
        log.warning(
            "auto_check: todo id=%d has unknown auto_check.type=%r — skipping",
            ref_id,
            type_name,
        )
        return "pending"
    # All evaluators receive ``ref_id`` as a kwarg so tree-scoped
    # evaluators (Slice-5 ``child_job_succeeded``) can look up
    # children of the calling leaf. Evaluators that don't need it
    # (``time_past``, ``paper_ingested``, etc.) accept and ignore it
    # via ``**_kw``.
    verdict = evaluator(store, spec, ref_id=ref_id)
    if verdict is True:
        # Success clears stale bubbles (parked-leaf-recovery, docs/
        # backlog/parked-leaf-recovery.md): a parent can carry
        # ``child-failed:<job_id>`` tags from EARLIER failed siblings
        # even though the child that just resolved this leaf
        # succeeded — without this the row survives done-but-parked-
        # looking. Only this evaluator's own success path clears them;
        # a leaf resolved some other way (e.g. ``tag_present``) leaves
        # the bubble alone, since that resolution says nothing about
        # whether the earlier failure was ever actually addressed.
        removed = _flip_status(
            store,
            ref_id,
            to="done",
            event="auto-resolved",
            clear_child_failed=(type_name == "child_job_succeeded"),
        )
        log.info("auto_check: todo id=%d → STATUS:done (auto-resolved)", ref_id)
        if removed:
            log.info(
                "auto_check: todo id=%d cleared %d stale child-failed "
                "tag(s) on resolve",
                ref_id,
                removed,
            )
        return "done"
    return "pending"


def _flip_status(
    store: Store,
    ref_id: int,
    *,
    to: str,
    event: str,
    clear_child_failed: bool = False,
) -> int:
    """Atomically replace the STATUS tag and append a ``ref_events`` row.

    Uses the existing closed-prefix replace semantics
    (``replace_prefix=True``) so any prior STATUS value is removed
    in the same tx. With ``clear_child_failed`` the stale
    ``child-failed:*`` bubble tags are deleted in that SAME tx — a
    crash between the flip and a separate cleanup would otherwise
    leave a done leaf permanently carrying the tags (gr202263).
    Returns the number of bubble tags removed (0 unless asked).
    """
    from precis.handlers._job_bubble import remove_child_failed_tags

    target = Tag.closed("STATUS", to)
    removed = 0
    with store.tx() as conn:
        store.add_tag(
            ref_id,
            target,
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.append_event(
            ref_id,
            source="auto-check",
            event=event,
            conn=conn,
        )
        if clear_child_failed:
            removed = remove_child_failed_tags(store, ref_id, conn=conn)
    return removed


__all__ = ["run_auto_check_pass"]
