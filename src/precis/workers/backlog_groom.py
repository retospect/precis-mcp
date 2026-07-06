"""Backlog groomer — promote open gripes into the acting queue.

The dark-factory north star is that declared repo dev work builds itself:
``/whatneedsdoing`` only *reads* the two work substrates; nothing turns a
gripe (substrate 1) into a ``kind='todo'`` the ``dispatch`` worker can act
on. This pass closes that loop for the **gripe** side.

For each open gripe with no fix already in flight, it mints one
``kind='todo'`` carrying ``meta.executor='claude_inproc'`` +
``meta.job_type='fix_gripe'`` + ``meta.params={'gripe_id': N}``. On the next
``dispatch`` sweep that todo mints a ``fix_gripe`` job under the
``claude_inproc`` executor (which clones the repo, runs ``claude -p`` on a
``gripe_<id>`` branch, and pushes a candidate fix). The todo hangs under a
single ``level:strategic`` groomer root so it satisfies the nursery's
strategic-ancestor invariant and shows up as one legible project subtree.

**Scope — gripes only (this slice).** The ``OPEN-ITEMS.md`` half of the
backlog is *not* groomed here, for two concrete reasons: (1) the file lives
at the repo root and is **not** packaged into the installed wheel, so a
deployed worker can't read it; and (2) there is no ``build_feature``
job_type for a free-text feature item to hand off to (``fix_gripe`` is
gripe-specific). Grooming OPEN-ITEMS needs a build executor + a packaged
source of the backlog first — filed as a follow-up, not built here.

Two guards, mirrored from ``paper_reconcile``:

* **Cadence throttle.** A ``backlog_groom:last_run`` marker in
  ``app_state`` gates the pass to once per
  ``PRECIS_BACKLOG_GROOM_REFRESH_HOURS`` (default 6). Between runs the pass
  is a single cheap ``app_state`` read.
* **Single-runner advisory lock.** A **transaction-scoped**
  ``pg_try_advisory_xact_lock`` held for the whole pass ensures only one
  cluster node mints in a given cycle even if two clear the throttle in the
  same tick, so the dedup check + mint can't race across nodes. A session
  lock is unsafe through pgbouncer ``pool_mode=transaction`` — see the
  ``paper_reconcile`` docstring for the full rationale.

Registered **default-OFF** in ``cli/worker.py`` (``--only backlog_groom`` or
``PRECIS_BACKLOG_GROOM_ENABLED=1``): once on it starts handing repo bugs to
the autonomous fixer substrate, so it is enabled deliberately, like the
classifier.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta

import psycopg

from precis.store import Store
from precis.store.types import Tag
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant ("bklggrm\x01"), namespaced away from other passes' lock keys.
_LOCK_KEY = 0x62_6B_6C_67_67_72_00_01 - 2**63
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "backlog_groom:last_run"

#: Meta marker on the strategic root the groomer hangs its todos under.
_ROOT_MARKER = "backlog_groom_root"
#: The executor + job_type each minted todo carries. ``fix_gripe`` is the
#: reference gripe→fix job_type; ``claude_inproc`` is its only compatible
#: executor (``fix_gripe.COMPATIBLE_EXECUTORS``).
_EXECUTOR = "claude_inproc"
_JOB_TYPE = "fix_gripe"
#: A gripe carrying this open tag is opted out of grooming by a human.
_OPT_OUT_TAG = "no-groom"


def _refresh_hours() -> float:
    """Minimum gap between grooming passes.

    ``PRECIS_BACKLOG_GROOM_REFRESH_HOURS`` (default 6.0, floor 0.1).
    """
    raw = os.environ.get("PRECIS_BACKLOG_GROOM_REFRESH_HOURS")
    if not raw:
        return 6.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 6.0


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    last = store.get_setting(_STATE_KEY)
    if not last:
        return True
    try:
        last_ts = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(UTC) - last_ts >= timedelta(hours=_refresh_hours())


def _groomed_gripe_ids(store: Store) -> set[int]:
    """Gripe ids that already have a (live) groomer todo.

    Dedup key is ``meta.params.gripe_id`` on any live ``kind='todo'`` — the
    same field ``dispatch`` threads into the minted job — so a gripe is not
    re-groomed even after its fix todo is marked done (the fix shipped, or a
    human is handling it). Prevents a spin where every pass re-mints the
    same handful of open gripes.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT (meta -> 'params' ->> 'gripe_id')
              FROM refs
             WHERE kind = 'todo'
               AND deleted_at IS NULL
               AND meta -> 'params' ? 'gripe_id'
            """
        ).fetchall()
    out: set[int] = set()
    for (raw,) in rows:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _ensure_root(store: Store) -> int:
    """Find-or-create the strategic root the groomer hangs todos under.

    Identified by ``meta.<_ROOT_MARKER>=True`` so it survives title edits.
    Tagged ``level:strategic`` so the minted children have a strategic
    ancestor (else the nursery flags them as orphans). Runs under the pass's
    advisory lock, so no concurrent double-create.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT ref_id FROM refs
             WHERE kind = 'todo'
               AND deleted_at IS NULL
               AND (meta ->> %s) = 'true'
             ORDER BY ref_id
             LIMIT 1
            """,
            (_ROOT_MARKER,),
        ).fetchone()
    if row is not None:
        return int(row[0])

    with store.tx() as conn:
        root = store.insert_ref(
            kind="todo",
            slug=None,
            title="Backlog groomer — auto-minted gripe fixes",
            meta={_ROOT_MARKER: True},
            parent_id=None,
            prio=5,
            conn=conn,
        )
        store.add_tag(
            root.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(root.id, Tag.open("level:strategic"), set_by="system", conn=conn)
    log.info("backlog_groom: created strategic root id=%d", root.id)
    return int(root.id)


def _mint_todo_for_gripe(
    store: Store, root_id: int, gripe_id: int, summary: str
) -> int:
    """Mint one dispatchable ``fix_gripe`` todo under the groomer root."""
    title = f"fix gr{gripe_id}: {summary}".strip()
    if len(title) > 160:
        title = title[:157].rstrip() + "…"
    with store.tx() as conn:
        child = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta={
                "executor": _EXECUTOR,
                "job_type": _JOB_TYPE,
                "params": {"gripe_id": gripe_id},
                "minted_from_gripe": gripe_id,
                "source": "backlog_groom",
            },
            parent_id=root_id,
            prio=4,
            conn=conn,
        )
        store.add_tag(
            child.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        store.add_tag(child.id, Tag.open("level:subtask"), set_by="system", conn=conn)
        store.add_tag(
            child.id, Tag.open("origin:backlog-groom"), set_by="system", conn=conn
        )
        store.append_event(
            root_id,
            source="backlog_groom",
            event="mint",
            payload={"gripe_id": gripe_id, "todo_id": int(child.id)},
            conn=conn,
        )
    log.info("backlog_groom: minted todo id=%d for gripe id=%d", child.id, gripe_id)
    return int(child.id)


def run_backlog_groom_pass(store: Store, *, batch_size: int = 16) -> BatchResult:
    """Groom open gripes into dispatchable ``fix_gripe`` todos, if due.

    Counters in the returned ``BatchResult``:

    * ``claimed`` = number of open, not-yet-groomed, not-opted-out gripes
      selected to mint this pass (bounded by ``batch_size``)
    * ``ok`` = todos successfully minted
    * ``failed`` = mints that raised (logged, skipped)

    Idle passes (throttled, no DSN, or lock-contended) return all zeros.
    """
    idle = BatchResult(handler="backlog_groom", claimed=0, ok=0, failed=0)
    if not store.dsn or not _due(store):
        return idle
    dsn = store.dsn

    # Single-runner lock: hold pg_try_advisory_xact_lock inside ONE open
    # transaction on a dedicated connection for the whole pass. The
    # grooming reads/writes run on the ``store`` pool; the dedicated conn
    # only pins the lock (transaction-scoped → survives pgbouncer
    # transaction pooling, auto-releases on commit).
    conn = psycopg.connect(dsn)
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,)
            ).fetchone()
            if not (row and row[0]):
                return idle  # another node owns the groom this cycle

            already = _groomed_gripe_ids(store)
            open_gripes = store.list_refs(
                kind="gripe",
                tags=["STATUS:open"],
                order_by="updated_desc",
                limit=200,
            )

            # Select eligible gripes: open, not already groomed, not
            # human-opted-out via the ``no-groom`` tag. Bound to batch_size.
            selected: list[tuple[int, str]] = []
            for g in open_gripes:
                if g.id in already:
                    continue
                if store.has_tag(int(g.id), "OPEN", _OPT_OUT_TAG):
                    continue
                selected.append((int(g.id), g.title or f"gripe {g.id}"))
                if len(selected) >= batch_size:
                    break

            if not selected:
                store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
                return idle

            root_id = _ensure_root(store)
            minted = 0
            failed = 0
            for gripe_id, summary in selected:
                try:
                    _mint_todo_for_gripe(store, root_id, gripe_id, summary)
                    minted += 1
                except Exception:  # pragma: no cover - defensive
                    log.exception(
                        "backlog_groom: failed to mint todo for gripe id=%d",
                        gripe_id,
                    )
                    failed += 1

            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
            if minted:
                log.info(
                    "backlog_groom: minted %d todo(s) from %d open gripe(s)",
                    minted,
                    len(open_gripes),
                )
            return BatchResult(
                handler="backlog_groom",
                claimed=len(selected),
                ok=minted,
                failed=failed,
            )
    finally:
        conn.close()
