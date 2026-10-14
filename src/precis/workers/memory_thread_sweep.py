"""The daily ``section:threads`` retirement sweep (gr485358 item 1) —
fired from the ``memory_thread_sweep`` scheduler cadence.

Why it exists: thread-node retirement was manual only —
``scripts/memory-lint`` listed candidates and the reconsolidation pass
ran only when a session acted on its nag, so dead resume nodes piled
into ``search(kind='memory')`` recall (2026-10-14: 22 live threads, four
of them one session's resume chain, each saying "Supersedes meN" in
prose with no edge). The rules are the ones the repo-dev memory policy
already wrote down; this cadence is just their executor:

* **rule a** — the body's first line is ``Left: none``: the campaign
  declared itself finished.
* **rule b** — another *live memory* links ``supersedes`` to it: the
  replacement is on record, the old node is clutter.
* **rule c** — untouched for :data:`~precis.utils.memory_threads.STALE_DAYS`
  days (the same clock ``precis memory index`` calls "live"). When the
  node's ``Left:`` line names open work, a todo carrying that line —
  linked back to the retired node — is minted in the same transaction
  first, so the sweep forgets the *state* without dropping the *work*.

Scope is deliberately narrow: ``kind='memory'`` rows tagged
``section:threads`` only — never other kinds, never ``section:index``
hubs (excluded even if someone tags one ``threads`` too), never
runbook/gotcha/workflow/reference sections. Retirement is the normal
soft-delete (:meth:`Store.retire_ref`), recoverable at the SQL layer —
never a hard delete. Every retirement logs one greppable line with the
rule letter::

    memory_thread_sweep: retired me<id> rule=c — untouched 21d (todo=td<n>)

``dry_run=True`` plans only: each candidate is logged as
``would retire me<id> rule=<r>`` and nothing is written — what
``precis memory sweep`` prints. Per-candidate failures are logged and
counted, never abort the pass (one poisoned row must not strand the
rest), and the run is idempotent: a retired node is gone from the
candidate query, so the next fire has nothing to re-act on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.utils.memory_threads import (
    INDEX_TAG,
    STALE_DAYS,
    THREADS_TAG,
    first_left,
    left_is_done,
)
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store import Store

log = logging.getLogger(__name__)

#: Live memories tagged ``section:threads`` (never ``section:index``
#: hubs), with their body chunk's first lines evaluated in Python — the
#: ``Left:`` grammar lives in :mod:`precis.utils.memory_threads`, not in
#: SQL.
_THREAD_CANDIDATES = """
SELECT r.ref_id, r.title, r.updated_at,
       (SELECT c.text FROM chunks c
         WHERE c.ref_id = r.ref_id AND c.chunk_kind = 'memory_body'
         ORDER BY c.ord LIMIT 1) AS body
  FROM refs r
 WHERE r.kind = 'memory' AND r.retired_at IS NULL
   AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                WHERE rt.ref_id = r.ref_id
                  AND t.namespace = 'OPEN' AND t.value = %s)
   AND NOT EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = r.ref_id
                      AND t.namespace = 'OPEN' AND t.value = %s)
 ORDER BY r.ref_id
"""

#: ``{superseded ref_id: superseding live memory id}`` — the *edge*, never
#: the prose: a body that merely says "supersedes meN" retires nothing
#: (the lint's job to flag), only a real ``supersedes`` link counts.
_SUPERSEDED_BY = """
SELECT l.dst_ref_id, min(l.src_ref_id)
  FROM links l
  JOIN refs s ON s.ref_id = l.src_ref_id
             AND s.retired_at IS NULL AND s.kind = 'memory'
 WHERE l.relation = 'supersedes' AND l.dst_ref_id = ANY(%s)
   AND l.src_ref_id <> l.dst_ref_id
 GROUP BY l.dst_ref_id
"""


@dataclass(frozen=True)
class SweepCandidate:
    """One thread node the sweep would retire, with its rule and the
    carry-over state a rule-c retirement must not lose."""

    ref_id: int
    title: str
    rule: str  # "a" | "b" | "c"
    reason: str  # human-readable why, for the log line and dry-run list
    #: Rule c only: the ``Left:`` remainder when it names open work —
    #: minted into a carry-over todo before the node retires.
    left: str | None = None
    #: Rule b only: the live memory that supersedes this node (stamped
    #: onto ``meta.superseded_by``, the same breadcrumb the ``supersede``
    #: verb leaves).
    superseded_by: int | None = None


def plan_thread_sweep(
    store: Store, *, now: datetime | None = None
) -> list[SweepCandidate]:
    """Every live ``section:threads`` node due for retirement, oldest id
    first. Pure read — the dry-run mode is this plan printed."""
    with store.pool.connection() as conn:
        rows = conn.execute(_THREAD_CANDIDATES, (THREADS_TAG, INDEX_TAG)).fetchall()
        ids = [int(r[0]) for r in rows]
        superseded: dict[int, int] = {}
        if ids:
            superseded = {
                int(d): int(s)
                for d, s in conn.execute(_SUPERSEDED_BY, (ids,)).fetchall()
            }
    cutoff = (now or datetime.now(UTC)) - timedelta(days=STALE_DAYS)
    plan: list[SweepCandidate] = []
    for ref_id, title, updated_at, body in rows:
        ref_id = int(ref_id)
        title = str(title or "")
        left = first_left(str(body or ""))
        if left is not None and left_is_done(left):
            plan.append(
                SweepCandidate(ref_id, title, "a", "first line is 'Left: none'")
            )
        elif ref_id in superseded:
            plan.append(
                SweepCandidate(
                    ref_id,
                    title,
                    "b",
                    f"superseded by me{superseded[ref_id]}",
                    superseded_by=superseded[ref_id],
                )
            )
        elif updated_at is not None and updated_at < cutoff:
            age = ((now or datetime.now(UTC)) - updated_at).days
            carry = left if left else None
            plan.append(
                SweepCandidate(
                    ref_id,
                    title,
                    "c",
                    f"untouched {age}d",
                    left=carry,
                )
            )
    return plan


def _mint_leftover_todo(store: Store, item: SweepCandidate, conn: Connection) -> int:
    """The rule-c carry-over: a todo holding the retired node's ``Left:``
    line, linked back to it — open work survives the sweep. Returns the
    todo's ref id."""
    from precis.utils import handle_registry

    me = handle_registry.try_format("memory", item.ref_id) or f"me{item.ref_id}"
    title = item.left or ""
    if len(title) > 140:
        title = title[:139].rstrip() + "…"
    ref = store.insert_ref(
        kind="todo",
        slug=None,
        title=title or f"leftover work from {me}",
        meta={"memory_thread_sweep_from": item.ref_id},
        conn=conn,
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system", conn=conn)
    store.add_tag(ref.id, Tag.open("memory-sweep"), set_by="system", conn=conn)
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text=(
                    f"Left: {item.left}\n\n"
                    f"Carried over when the thread sweep retired {me} "
                    f"({item.title!r}, untouched ≥{STALE_DAYS}d). The retired "
                    "node still answers get() — restore or work it, then "
                    "close this."
                ),
                meta={"chunk_kind": TODO_BODY_KIND},
            )
        ],
        conn=conn,
    )
    store.add_link(
        src_ref_id=ref.id,
        dst_ref_id=item.ref_id,
        relation="related-to",
        set_by="system",
        conn=conn,
    )
    return int(ref.id)


def run_memory_thread_sweep_pass(
    store: Store, *, dry_run: bool = False, now: datetime | None = None
) -> BatchResult:
    """One sweep tick: plan, then retire each candidate.

    ``claimed`` = thread nodes due this fire; ``ok`` = actually retired
    (0 in dry-run); ``failed`` = candidates whose retire raised (logged,
    the pass continues)."""
    plan = plan_thread_sweep(store, now=now)
    retired = 0
    failed = 0
    for item in plan:
        if dry_run:
            log.info(
                "memory_thread_sweep: would retire me%d rule=%s — %s",
                item.ref_id,
                item.rule,
                item.reason,
            )
            continue
        try:
            todo_id: int | None = None
            with store.tx() as conn:
                if item.rule == "c" and item.left:
                    todo_id = _mint_leftover_todo(store, item, conn)
                if item.superseded_by is not None:
                    store.stamp_ref_meta(
                        item.ref_id,
                        {"superseded_by": item.superseded_by},
                        conn=conn,
                    )
                store.retire_ref(item.ref_id, conn=conn)
            retired += 1
            log.info(
                "memory_thread_sweep: retired me%d rule=%s%s — %s",
                item.ref_id,
                item.rule,
                f" todo=td{todo_id}" if todo_id else "",
                item.reason,
            )
        except Exception:
            log.exception("memory_thread_sweep: retire failed for me%d", item.ref_id)
            failed += 1
    return BatchResult(
        handler="memory_thread_sweep",
        claimed=len(plan),
        ok=retired,
        failed=failed,
    )


__all__ = [
    "SweepCandidate",
    "plan_thread_sweep",
    "run_memory_thread_sweep_pass",
]
