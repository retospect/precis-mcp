"""Auto-check worker pass — Slice 1b of ``docs/design/todo-tree-plan.md``.

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
    """Drain up to ``limit`` open auto-task leaves.

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

    One round-trip; small queries because the auto-check population
    stays bounded (asks + paper-waits are leaf counts, not chunk
    counts).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta->'auto_check'
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.deleted_at IS NULL
               AND r.meta ? 'auto_check'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt
                        JOIN tags t ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id
                         AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) = ANY(%s)
             ORDER BY r.ref_id
             LIMIT %s
            """,
            (sorted(_OPEN_STATUSES), limit),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


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
        _flip_status(store, ref_id, to="done", event="auto-resolved")
        log.info("auto_check: todo id=%d → STATUS:done (auto-resolved)", ref_id)
        return "done"
    return "pending"


def _flip_status(store: Store, ref_id: int, *, to: str, event: str) -> None:
    """Atomically replace the STATUS tag and append a ``ref_events`` row.

    Uses the existing closed-prefix replace semantics
    (``replace_prefix=True``) so any prior STATUS value is removed
    in the same tx.
    """
    target = Tag.closed("STATUS", to)
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


__all__ = ["run_auto_check_pass"]
