"""Backlog groomer — promote open gripes into the acting queue.

The dark-factory north star is that declared repo dev work builds itself:
``/whatneedsdoing`` only *reads* the two work substrates; nothing turns a
gripe (substrate 1) into a ``kind='todo'`` the ``dispatch`` worker can act
on. This pass closes that loop for the **gripe** side.

For each open gripe tagged ``OPEN:auto-fix`` (the key
``diagnose_gripe``'s confidence-gated auto-promotion writes — see
``job_types.diagnose_gripe``'s ``PRECIS_DIAGNOSE_AUTOPROMOTE`` — or a human
hand-tags) with no live fix already in flight, it mints one ``kind='todo'``
carrying ``meta.executor='claude_inproc'`` + ``meta.job_type='fix_gripe'`` +
``meta.params={'gripe_id': N, 'diagnosis_job_id': M}`` (the ``diagnosis_job_id``
key is present only when a succeeded ``diagnose_gripe`` job is on record for
the gripe — see :func:`_latest_succeeded_diagnosis_job_id`; ``fix_gripe``
reads it to narrow the agent's brief to the gripe body + that one diagnosis
instead of the full comment timeline). On the next ``dispatch`` sweep that
todo mints a ``fix_gripe`` job under the ``claude_inproc`` executor (which
clones the repo, runs ``claude -p`` on a ``gripe_<id>`` branch, and pushes a
candidate fix). The todo hangs under a single ``meta.rotation_root=True``
groomer root so it satisfies the nursery's strategic-ancestor invariant and
shows up as one legible project subtree.

**Scope — gripes only (this slice).** The ``OPEN-ITEMS.md`` half of the
backlog is *not* groomed here, for two concrete reasons: (1) the file lives
at the repo root and is **not** packaged into the installed wheel, so a
deployed worker can't read it; and (2) there is no ``build_feature``
job_type for a free-text feature item to hand off to (``fix_gripe`` is
gripe-specific). Grooming OPEN-ITEMS needs a build executor + a packaged
source of the backlog first — filed as a follow-up, not built here.

Guards:

* **Selection filter.** Only gripes carrying ``OPEN:auto-fix`` are
  candidates — a raw open gripe with no diagnosis (or a diagnosis below
  ``diagnose_gripe``'s confidence threshold, or an unset
  ``PRECIS_DIAGNOSE_AUTOPROMOTE``) is left alone. Within that set, a gripe
  is skipped when it already has a *live* (non-retired, ``STATUS`` not
  ``done``) ``fix_gripe`` todo, or carries the ``no-groom`` human opt-out
  tag. This replaced grooming every open gripe (v0), which was too
  indiscriminate to turn on in prod — 249 ``diagnose_gripe`` runs and 0
  ``fix_gripe`` runs in 14 days traced back to this pass never being
  enabled at all, for exactly that reason.
* **Mint cap.** At most ``PRECIS_BACKLOG_GROOM_MAX_MINTS`` (default 3)
  todos are minted per pass even when more gripes are eligible — bounds
  the FRONTIER-tier ``fix_gripe`` spend a single tick can trigger. The
  skipped count is logged, not lost silently.
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
classifier — now scoped tightly enough (auto-fix gate + mint cap) that a
prod deployment can actually turn it on.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime

import psycopg

from precis.store import Store
from precis.store.types import Tag
from precis.workers import _throttle
from precis.workers.job_types.diagnose_gripe import AUTOFIX_TAG
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant ("bklggrm\x01"), namespaced away from other passes' lock keys.
_LOCK_KEY = 0x62_6B_6C_67_67_72_00_01 - 2**63
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "backlog_groom:last_run"
#: Env var + default for the cadence throttle (see :func:`_throttle.due`).
_REFRESH_ENV_VAR = "PRECIS_BACKLOG_GROOM_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 6.0
#: Env var + default for the per-pass mint cap (see :func:`_max_mints`).
_MAX_MINTS_ENV_VAR = "PRECIS_BACKLOG_GROOM_MAX_MINTS"
_DEFAULT_MAX_MINTS = 3

#: Meta marker on the strategic root the groomer hangs its todos under.
_ROOT_MARKER = "backlog_groom_root"
#: The executor + job_type each minted todo carries. ``fix_gripe`` is the
#: reference gripe→fix job_type; ``claude_inproc`` is its only compatible
#: executor (``fix_gripe.COMPATIBLE_EXECUTORS``).
_EXECUTOR = "claude_inproc"
_JOB_TYPE = "fix_gripe"
#: A gripe carrying this open tag is opted out of grooming by a human.
_OPT_OUT_TAG = "no-groom"


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


def _max_mints() -> int:
    """How many todos this pass may mint, read from
    ``PRECIS_BACKLOG_GROOM_MAX_MINTS`` (default :data:`_DEFAULT_MAX_MINTS`).

    Unset, unparsable, or non-positive -> the default. Mirrors
    :func:`_throttle.refresh_hours`'s "bad input degrades to the default,
    never raises" shape.
    """
    raw = os.environ.get(_MAX_MINTS_ENV_VAR)
    if not raw:
        return _DEFAULT_MAX_MINTS
    try:
        val = int(raw)
    except ValueError:
        return _DEFAULT_MAX_MINTS
    return val if val > 0 else _DEFAULT_MAX_MINTS


def _live_fix_todo_gripe_ids(store: Store) -> set[int]:
    """Gripe ids that already have a LIVE ``fix_gripe`` groomer todo.

    Dedup key is ``meta.params.gripe_id`` on any ``kind='todo'`` whose
    ``meta.job_type`` is ``fix_gripe`` (the field this pass's own mints
    carry — see :data:`_JOB_TYPE`), that is non-retired AND whose
    ``STATUS`` tag isn't ``done`` — the same ``gripe_id`` field ``dispatch``
    threads into the minted job. Scoped to ``job_type='fix_gripe'`` so a
    future todo minter that happens to reuse the ``params.gripe_id`` shape
    for an unrelated job_type can't starve this pass's re-mints. Unlike the
    old every-gripe groomer, a *done* fix todo no longer blocks a re-mint:
    the fix shipped or a human closed it, and this pass's selection is
    already gated on ``OPEN:auto-fix``, so a gripe only reaches here again
    once something (a fresh diagnosis, a human) re-tags it. A
    ``running``/``queued``/plain ``open`` todo still suppresses a re-mint,
    preventing a spin where every pass re-mints the same in-flight gripe.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT DISTINCT (r.meta -> 'params' ->> 'gripe_id')
              FROM refs r
             WHERE r.kind = 'todo'
               AND r.retired_at IS NULL
               AND r.meta ->> 'job_type' = %s
               AND r.meta -> 'params' ? 'gripe_id'
               AND COALESCE(
                     (SELECT t.value FROM ref_tags rt JOIN tags t
                        ON t.tag_id = rt.tag_id
                       WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                       LIMIT 1),
                     'open'
                   ) != 'done'
            """,
            (_JOB_TYPE,),
        ).fetchall()
    out: set[int] = set()
    for (raw,) in rows:
        try:
            out.add(int(raw))
        except (TypeError, ValueError):
            continue
    return out


def _latest_succeeded_diagnosis_job_id(store: Store, gripe_id: int) -> int | None:
    """The newest succeeded ``diagnose_gripe`` job on record for ``gripe_id``.

    Feeds the minted fix todo's ``meta.params.diagnosis_job_id`` —
    ``fix_gripe`` resolves it back to the ``DIAGNOSIS (auto, job <id>):``
    gripe comment that job left (see
    ``job_types.diagnose_gripe._DIAGNOSIS_PREFIX``) and narrows its brief to
    that instead of the full comment timeline. ``None`` when no succeeded
    diagnosis is on record (e.g. the gripe was hand-tagged ``auto-fix``
    without ever running through ``diagnose_gripe``) — the fix todo mints
    without the param, and ``fix_gripe`` falls back to its full-timeline
    brief unchanged.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta ->> 'job_type' = 'diagnose_gripe'
               AND (r.meta -> 'params' ->> 'gripe_id')::bigint = %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t
                       ON t.tag_id = rt.tag_id
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS' AND t.value = 'succeeded'
                   )
             ORDER BY r.ref_id DESC
             LIMIT 1
            """,
            (gripe_id,),
        ).fetchone()
    return int(row[0]) if row is not None else None


def _ensure_root(store: Store) -> int:
    """Find-or-create the strategic root the groomer hangs todos under.

    Identified by ``meta.<_ROOT_MARKER>=True`` so it survives title edits.
    Stamped ``meta.rotation_root=True`` so the minted children have a
    strategic ancestor (else the nursery flags them as orphans). Runs
    under the pass's advisory lock, so no concurrent double-create.
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT ref_id FROM refs
             WHERE kind = 'todo'
               AND retired_at IS NULL
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
            meta={_ROOT_MARKER: True, "rotation_root": True},
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
    log.info("backlog_groom: created strategic root id=%d", root.id)
    return int(root.id)


#: Fallback prio for a minted fix todo when the gripe carries none — a hair
#: above the default 5, so unscored bug-fixes still edge out routine work.
_DEFAULT_FIX_PRIO = 4


def _mint_todo_for_gripe(
    store: Store,
    root_id: int,
    gripe_id: int,
    summary: str,
    gripe_prio: int | None,
    *,
    diagnosis_job_id: int | None,
) -> int:
    """Mint one dispatchable ``fix_gripe`` todo under the groomer root.

    The todo inherits the gripe's ``prio`` (so a human tagging a gripe
    ``PRIO:high`` actually moves its fix up the fixer's doable-view queue);
    falls back to ``_DEFAULT_FIX_PRIO`` when the gripe is unscored.
    ``diagnosis_job_id`` (see :func:`_latest_succeeded_diagnosis_job_id`),
    when not ``None``, rides along in ``meta.params`` so ``fix_gripe`` can
    narrow its brief to that diagnosis instead of the full comment timeline.
    """
    title = f"fix gr{gripe_id}: {summary}".strip()
    if len(title) > 160:
        title = title[:157].rstrip() + "…"
    params: dict[str, int] = {"gripe_id": gripe_id}
    if diagnosis_job_id is not None:
        params["diagnosis_job_id"] = diagnosis_job_id
    with store.tx() as conn:
        child = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta={
                "executor": _EXECUTOR,
                "job_type": _JOB_TYPE,
                "params": params,
                "minted_from_gripe": gripe_id,
                "source": "backlog_groom",
            },
            parent_id=root_id,
            prio=gripe_prio if gripe_prio is not None else _DEFAULT_FIX_PRIO,
            conn=conn,
        )
        store.add_tag(
            child.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
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
    """Groom ``OPEN:auto-fix`` gripes into dispatchable ``fix_gripe`` todos,
    if due.

    Counters in the returned ``BatchResult``:

    * ``claimed`` = number of open, ``auto-fix``-tagged, not-already-live,
      not-opted-out gripes selected this pass (bounded by ``batch_size``)
    * ``ok`` = todos successfully minted (bounded by
      :func:`_max_mints` — see below)
    * ``failed`` = mints that raised (logged, skipped)

    ``claimed`` can exceed ``ok + failed``: once :func:`_max_mints` todos
    have been minted, any further eligible gripes are left for the next
    pass rather than minted unboundedly; the skipped count is logged.

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

            already = _live_fix_todo_gripe_ids(store)
            open_gripes = store.list_refs(
                kind="gripe",
                tags=["STATUS:open", f"OPEN:{AUTOFIX_TAG}"],
                order_by="updated_desc",
                limit=200,
            )

            # Select eligible gripes: open, auto-fix-tagged, no live
            # fix_gripe todo already minted, not human-opted-out via the
            # ``no-groom`` tag. Bound to batch_size. Carry each gripe's
            # ``prio`` so the minted fix todo inherits it.
            selected: list[tuple[int, str, int | None]] = []
            for g in open_gripes:
                if g.id in already:
                    continue
                if store.has_tag(int(g.id), "OPEN", _OPT_OUT_TAG):
                    continue
                selected.append((int(g.id), g.title or f"gripe {g.id}", g.prio))
                if len(selected) >= batch_size:
                    break

            if not selected:
                store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
                return idle

            root_id = _ensure_root(store)
            max_mints = _max_mints()
            minted = 0
            failed = 0
            skipped = 0
            for gripe_id, summary, gripe_prio in selected:
                if minted + failed >= max_mints:
                    skipped += 1
                    continue
                try:
                    diagnosis_job_id = _latest_succeeded_diagnosis_job_id(
                        store, gripe_id
                    )
                    _mint_todo_for_gripe(
                        store,
                        root_id,
                        gripe_id,
                        summary,
                        gripe_prio,
                        diagnosis_job_id=diagnosis_job_id,
                    )
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
                    "backlog_groom: minted %d todo(s) from %d eligible gripe(s)",
                    minted,
                    len(selected),
                )
            if skipped:
                log.info(
                    "backlog_groom: %d eligible gripe(s) skipped this pass "
                    "(%s=%d reached)",
                    skipped,
                    _MAX_MINTS_ENV_VAR,
                    max_mints,
                )
            return BatchResult(
                handler="backlog_groom",
                claimed=len(selected),
                ok=minted,
                failed=failed,
            )
    finally:
        conn.close()
