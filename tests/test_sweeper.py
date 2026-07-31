"""Stuck-job sweeper tests — transition, dedup, race-skip, bubble.

The sweeper is SQL-only: any ``kind='job'`` whose current
``STATUS:running`` is older than the threshold flips to
``STATUS:failed`` with an ``swept:claim-orphaned`` open tag and the
parent's ``child-failed:<job_id>`` bubble fires.

Tests:

* fresh STATUS:running (< threshold) is left alone
* stale STATUS:running (> threshold) is transitioned, parent gets
  ``child-failed:<job>``, and a ``swept:claim-orphaned`` tag lands
* already-failed jobs are skipped (idempotent)
* bubble has no parent → no crash (orphan job edge case)

Mirrors ``test_nursery.py``'s SQL-backdate-via-``ref_tags.created_at``
pattern.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.store.types import Tag
from precis.workers.sweeper import (
    _REOPEN_MAX_ATTEMPTS,
    _WORKER_LOG_GC_LOCK,
    _gc_worker_logs,
    _reopen_transient_failed_embeds,
    _reopen_transient_failed_summaries,
    run_sweeper_pass,
)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _query(store: Store, sql: str, params: tuple) -> list:
    with store.pool.connection() as conn:
        return conn.execute(sql, params).fetchall()


def _insert_worker_log(store: Store, host: str, *, age_days: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, level, message) "
            "VALUES (now() - make_interval(days => %s), %s, 'INFO', 'gc-test')",
            (age_days, host),
        )
        conn.commit()


def _insert_worker_process_log(
    store: Store, host: str, process: str, *, age_minutes: float
) -> None:
    """Seed a ``worker_logs`` row for a continuous daemon process — the
    dead-node reap's "is the target_node worker alive" signal."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO worker_logs (ts, host, process, level, message) "
            "VALUES (now() - (%s || ' minutes')::interval, %s, %s, 'INFO', 'alive-test')",
            (age_minutes, host, process),
        )
        conn.commit()


def _upsert_host_heartbeat(store: Store, host: str, *, age_minutes: float) -> None:
    """Seed/refresh a ``host_heartbeat`` row — the dead-node reap's "is the
    host itself up" signal (PK on ``host``, so this is an upsert)."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) "
            "VALUES (%s, now() - (%s || ' minutes')::interval) "
            "ON CONFLICT (host) DO UPDATE SET ts = excluded.ts",
            (host, age_minutes),
        )
        conn.commit()


def test_gc_worker_logs_prunes_aged_and_is_single_flight(store: Store) -> None:
    # worker_logs is insert-only; the sweeper prunes rows past the 30d window,
    # single-flighted (the sweeper runs fleet-wide). Tag by a uuid host so the
    # shared precis_test DB doesn't perturb the assertion.
    from uuid import uuid4

    host = f"gc-{uuid4().hex[:8]}"

    def _count() -> int:
        with store.pool.connection() as conn:
            return conn.execute(
                "SELECT count(*) FROM worker_logs WHERE host = %s", (host,)
            ).fetchone()[0]

    for _ in range(3):
        _insert_worker_log(store, host, age_days=100)  # past the window
    for _ in range(2):
        _insert_worker_log(store, host, age_days=1)  # fresh — keep
    assert _count() == 5

    # A held lock makes the pruner fast-fail (0), deleting nothing — the guard
    # that stops the fleet from piling concurrent prunes onto the DB.
    with store.pool.connection() as holder:
        holder.execute("SELECT pg_advisory_lock(%s)", (_WORKER_LOG_GC_LOCK,))
        holder.commit()
        assert _gc_worker_logs(store) == 0
        assert _count() == 5
        holder.execute("SELECT pg_advisory_unlock(%s)", (_WORKER_LOG_GC_LOCK,))
        holder.commit()

    # Lock free: aged rows pruned, the 2 fresh rows survive.
    _gc_worker_logs(store)
    assert _count() == 2


def _mint_running_job(
    store: Store,
    parent_id: int | None,
    *,
    backdate_hours: float,
    lease_offset_hours: float | None = None,
    executor: str = "claude_inproc",
    job_type: str = "plan_tick",
    params: dict | None = None,
) -> int:
    """Insert a ``kind='job'`` ref, tag STATUS:running, backdate the tag.

    ``lease_offset_hours`` (optional) stamps ``meta.lease_until`` at
    ``now() + offset``: a positive value gives a *live* lease (worker
    still owns the job — must not be swept), a negative value an
    *expired* one. ``None`` leaves the meta lease-less (legacy job).

    ``executor`` sets ``meta.executor`` — ``ssh_node`` jobs are excluded
    from the sweep (that executor reclaims its own expired-lease running
    jobs), so this parameter drives the exclusion test.
    """
    meta: dict = {"job_type": job_type, "executor": executor}
    if params is not None:
        meta["params"] = params
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick test job",
        meta=meta,
        parent_id=parent_id,
    )
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "running"),
        set_by="system",
        replace_prefix=True,
    )
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags
               SET created_at = now() - %s::interval
             WHERE ref_id = %s
               AND tag_id IN (
                 SELECT tag_id FROM tags
                  WHERE namespace='STATUS' AND value='running'
               )
            """,
            (f"{backdate_hours} hours", job.id),
        )
        if lease_offset_hours is not None:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + %s::interval)::text) "
                "WHERE ref_id = %s",
                (f"{lease_offset_hours} hours", job.id),
            )
            conn.commit()
    return int(job.id)


def test_fresh_running_job_is_left_alone(handler: TodoHandler, store: Store) -> None:
    """A STATUS:running tag younger than the threshold is not swept."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=0.1)

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 0
    assert result.claimed == 0
    tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in tags
    assert "STATUS:failed" not in tags


def test_stale_running_job_is_swept_and_parent_bubbled(
    handler: TodoHandler, store: Store
) -> None:
    """Stale STATUS:running → STATUS:failed + swept tag.

    The parent bubble itself is infra-class bounded auto-retry (see
    ``test_infra_orphan_retry_*`` below) — a single sweep, under the
    retry cap, must NOT latch ``child-failed:`` on the parent. The old
    "always latches on the first sweep" behaviour is exactly the bug the
    2026-07-26→30 incident was filed against.
    """
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0)

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 1
    assert result.failed == 0
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags
    assert "STATUS:running" not in job_tags
    assert "swept:claim-orphaned" in job_tags
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job_id}" not in parent_tags


def test_stale_running_job_with_live_lease_is_left_alone(
    handler: TodoHandler, store: Store
) -> None:
    """A job past the hours threshold but with an unexpired ``lease_until``
    is still owned by a live worker — the sweeper must not touch it.

    This is the plan_tick case: the ``claude_inproc`` executor stamps a
    90-min lease so a long tick isn't false-swept at the 1h mark."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    # STATUS:running is 1.1h old (> threshold) but the lease still has
    # ~30 min to run, mirroring a plan_tick claimed ~60 min ago.
    job_id = _mint_running_job(store, rid, backdate_hours=1.1, lease_offset_hours=0.5)

    result = run_sweeper_pass(store, limit=10)

    assert result.claimed == 0
    assert result.ok == 0
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "STATUS:failed" not in job_tags
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job_id}" not in parent_tags


def test_stale_running_job_with_expired_lease_is_swept(
    handler: TodoHandler, store: Store
) -> None:
    """Once the lease has expired (and the hours threshold is past) the
    worker is presumed dead — the sweeper transitions it as before."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0, lease_offset_hours=-0.5)

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 1
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags
    assert "swept:claim-orphaned" in job_tags


# ── infra-class bounded auto-retry (2026-07-30 agent-lane stall) ──────


def test_infra_orphan_retry_leaves_parent_a_candidate_and_re_mints(
    handler: TodoHandler, store: Store
) -> None:
    """A single sweep-caused (infra-class) child failure, under the retry
    cap, must NOT latch ``child-failed:`` — the parent stays/re-becomes a
    dispatch candidate WITHOUT any manual tag removal, and a fresh child
    actually mints on the next dispatch tick (the whole point of the
    bounded-retry fix: re-dispatch must be real, not just "tag absent")."""
    from precis.workers.dispatch import _candidate_parent_ids, run_dispatch_pass

    r = handler.put(
        text="planner",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0)

    result = run_sweeper_pass(store, limit=10)
    assert result.ok == 1

    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job_id}" not in parent_tags
    assert rid in _candidate_parent_ids(store, limit=50)

    dispatch_result = run_dispatch_pass(store)
    assert dispatch_result.claimed == 1
    assert dispatch_result.ok == 1
    child_job_ids = {
        int(row[0])
        for row in _query(
            store,
            "SELECT ref_id FROM refs WHERE parent_id = %s AND kind = 'job' "
            "AND deleted_at IS NULL",
            (rid,),
        )
    }
    assert job_id in child_job_ids
    assert len(child_job_ids) == 2  # the swept job + the freshly minted one
    fresh_id = next(iter(child_job_ids - {job_id}))
    fresh_tags = {str(t) for t in store.tags_for(fresh_id)}
    assert "STATUS:queued" in fresh_tags


def test_infra_orphan_retry_latches_past_the_cap(
    handler: TodoHandler, store: Store
) -> None:
    """Repeated infra-class (sweeper) failures on the same parent, past
    ``ORPHAN_RETRY_CAP``, DO latch — a persistently-orphaned coordinator
    (dead executor, not a transient sweep) must stop instead of retrying
    forever. Latches with both ``child-failed:`` and
    ``halt:orphan-retry-cap`` for visibility."""
    from precis.handlers._job_bubble import ORPHAN_RETRY_CAP
    from precis.workers.dispatch import _candidate_parent_ids

    r = handler.put(
        text="chronically orphaned planner",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    rid = _id_of(r.body)

    last_job_id = None
    for i in range(ORPHAN_RETRY_CAP):
        last_job_id = _mint_running_job(store, rid, backdate_hours=2.0)
        result = run_sweeper_pass(store, limit=10)
        assert result.ok == 1
        if i < ORPHAN_RETRY_CAP - 1:
            parent_tags = {str(t) for t in store.tags_for(rid)}
            assert f"child-failed:{last_job_id}" not in parent_tags
            assert "halt:orphan-retry-cap" not in parent_tags

    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{last_job_id}" in parent_tags
    assert "halt:orphan-retry-cap" in parent_tags
    assert rid not in _candidate_parent_ids(store, limit=50)


def test_infra_orphan_retry_never_fires_on_live_lease(
    handler: TodoHandler, store: Store
) -> None:
    """A child still within its lease is not sweep-eligible at all, so the
    infra-retry counter must never bump — mirrors
    ``test_stale_running_job_with_live_lease_is_left_alone``, plus asserts
    the bounded-retry counter itself stayed untouched (lease-race safety:
    an auto-retry must never fire while a live worker could still be
    holding the job)."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=1.1, lease_offset_hours=0.5)

    result = run_sweeper_pass(store, limit=10)

    assert result.claimed == 0
    assert result.ok == 0
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "swept:claim-orphaned" not in job_tags
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'orphan_retry_count' FROM refs WHERE ref_id = %s", (rid,)
        ).fetchone()
    assert row is not None
    assert row[0] is None


def test_ssh_node_job_is_never_swept(handler: TodoHandler, store: Store) -> None:
    """An ``ssh_node``-executor job past the threshold with an *expired* lease
    is left alone — that executor reclaims and retries its own crashed jobs
    (lease-steal + attempt cap), so a sweeper failure here would race and win
    the steal, stranding the compute result instead of re-running it."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=2.0,
        lease_offset_hours=-0.5,
        executor="ssh_node",
    )

    result = run_sweeper_pass(store, limit=10)

    assert result.claimed == 0
    assert result.ok == 0
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags  # left for the executor to reclaim
    assert "STATUS:failed" not in job_tags
    parent_tags = {str(t) for t in store.tags_for(rid)}
    assert f"child-failed:{job_id}" not in parent_tags


def test_swept_job_kills_its_claude_docker_container(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swept ``claude_docker``-executor job's container is force-removed
    immediately (gripe 50905), not left for the lazy per-boot reconcile."""
    from precis.workers.executors import claude_docker

    killed: list[str] = []
    monkeypatch.setattr(claude_docker, "_reap", lambda name: killed.append(name))
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0, executor="claude_docker")

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 1
    assert killed == [claude_docker.container_name(job_id)]


def test_swept_struct_relax_job_kills_its_compute_container(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A swept ``struct_relax`` job's ``precis-job-<id>`` container is killed
    on its ``target_node`` (gripe 50905)."""
    from precis.workers.job_types import struct_relax

    killed: list[tuple[int, str | None]] = []
    monkeypatch.setattr(
        struct_relax,
        "kill_container",
        lambda ref_id, *, node=None, **kw: killed.append((ref_id, node)),
    )
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    # executor left at the default (claude_inproc) so this reaches the
    # generic timeout path — a real struct_relax job runs under ssh_node,
    # which is excluded above; this pins the job_type→kill_container wiring
    # itself, independent of that exclusion (belt-and-suspenders is the
    # stale-container watchdog, tested separately).
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=2.0,
        job_type="struct_relax",
        params={"target_node": "spark"},
    )

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 1
    assert killed == [(job_id, "spark")]


def test_kill_job_container_never_raises(
    handler: TodoHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A container-kill failure (docker/ssh down) must not crash the sweep —
    the DB-row transition already succeeded and must stay that way."""
    from precis.workers.job_types import struct_relax

    def _raise(*a, **kw):
        raise OSError("no docker")

    monkeypatch.setattr(struct_relax, "kill_container", _raise)
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0, job_type="struct_relax")

    result = run_sweeper_pass(store, limit=10)

    assert result.ok == 1  # the DB transition still succeeded
    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags


def test_stale_dft_container_watchdog_runs_only_on_dft_node(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-container watchdog is gated to the DFT node itself — it
    must not ssh-fan-out from every cluster node on every sweep."""
    from precis.workers.sweeper import _reap_stale_dft_containers

    calls: list[int] = []
    from precis.workers.job_types import struct_relax

    def _fake_reap(**kw: object) -> int:
        calls.append(1)
        return 3

    monkeypatch.setattr(struct_relax, "reap_stale_containers", _fake_reap)
    monkeypatch.setattr(struct_relax, "_NODE", "spark")

    monkeypatch.delenv("PRECIS_NODE", raising=False)
    assert _reap_stale_dft_containers() == 0
    assert calls == []

    monkeypatch.setenv("PRECIS_NODE", "spark")
    assert _reap_stale_dft_containers() == 3
    assert calls == [1]


def test_already_failed_job_is_skipped(handler: TodoHandler, store: Store) -> None:
    """STATUS:failed jobs are not re-swept (idempotency)."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(store, rid, backdate_hours=2.0)

    first = run_sweeper_pass(store, limit=10)
    assert first.ok == 1

    second = run_sweeper_pass(store, limit=10)
    assert second.ok == 0
    assert second.claimed == 0


def test_orphan_job_without_parent_does_not_crash(store: Store) -> None:
    """A job with ``parent_id IS NULL`` sweeps cleanly; bubble no-ops."""
    job_id = _mint_running_job(store, None, backdate_hours=2.0)

    result = run_sweeper_pass(store, limit=10)
    assert result.ok == 1

    tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in tags


def _seed_failed_embed(
    store: Store,
    *,
    last_error: str | None,
    status: str = "failed",
    attempts: int = 1,
) -> int:
    """Create a ref + one chunk + a single ``chunk_embeddings`` row; return the
    chunk_id. Used to seed the sweeper's transient-failed re-open scenarios."""
    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', %s) RETURNING chunk_id",
            (ref.id, "a passage of prose long enough to embed and keyword"),
        ).fetchone()
        assert row is not None
        cid = int(row[0])
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, status, attempts, last_error) "
            "VALUES (%s, 'bge-m3', %s, %s, %s)",
            (cid, status, attempts, last_error),
        )
        conn.commit()
    return int(cid)


def test_sweeper_reopens_transient_failed_embeds(store: Store) -> None:
    """The sweeper DELETEs transient-classified ``status='failed'`` embed rows
    (embedder-down / OOM) so the embed pass re-claims them — but leaves genuine
    faults and over-cap rows terminal, and never touches ``ok`` rows."""
    transient = _seed_failed_embed(
        store, last_error="all embedder endpoints failed (['http://127.0.0.1:8181'])"
    )
    oom = _seed_failed_embed(
        store, last_error="MPS backend out of memory (MPS allocated: 15.03 GiB)"
    )
    poison = _seed_failed_embed(
        store, last_error="embedding dimension mismatch: expected 1024 got 768"
    )
    over_cap = _seed_failed_embed(
        store,
        last_error="all embedder endpoints failed",
        attempts=_REOPEN_MAX_ATTEMPTS,
    )
    ok_row = _seed_failed_embed(store, last_error=None, status="ok")
    mine = [transient, oom, poison, over_cap, ok_row]

    n = _reopen_transient_failed_embeds(store, limit=1000)

    assert n >= 2  # at least my two transient rows (shared DB may add more)
    with store.pool.connection() as conn:
        surviving = {
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM chunk_embeddings WHERE chunk_id = ANY(%s)",
                (mine,),
            ).fetchall()
        }
    assert transient not in surviving  # transient outage → re-opened
    assert oom not in surviving  # OOM spike is transient → re-opened
    assert poison in surviving  # genuine per-chunk fault → stays terminal
    assert over_cap in surviving  # attempts at cap → not re-opened (no loop)
    assert ok_row in surviving  # ok row is never touched


def test_sweeper_embed_reopen_disabled_at_zero_limit(store: Store) -> None:
    """``limit=0`` (env off-switch) is a no-op — nothing re-opened."""
    transient = _seed_failed_embed(store, last_error="all embedder endpoints failed")
    assert _reopen_transient_failed_embeds(store, limit=0) == 0
    with store.pool.connection() as conn:
        still_there = conn.execute(
            "SELECT 1 FROM chunk_embeddings WHERE chunk_id = %s", (transient,)
        ).fetchone()
    assert still_there is not None


def _seed_failed_summary(
    store: Store,
    *,
    last_error: str | None,
    status: str = "failed",
    attempts: int = 3,
    summarizer: str = "llm-v1",
) -> int:
    """Create a ref + one chunk + a single ``chunk_summaries`` row; return the
    chunk_id. Mirrors ``_seed_failed_embed`` for the llm-v1 gloss re-open."""
    ref = store.insert_ref(kind="memory", slug=None, title="t", meta={})
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', %s) RETURNING chunk_id",
            (ref.id, "a passage of prose long enough to summarize"),
        ).fetchone()
        assert row is not None
        cid = int(row[0])
        conn.execute(
            "INSERT INTO chunk_summaries "
            "(chunk_id, summarizer, status, attempts, last_error) "
            "VALUES (%s, %s, %s, %s, %s)",
            (cid, summarizer, status, attempts, last_error),
        )
        conn.commit()
    return int(cid)


def test_sweeper_reopens_transient_failed_llm_summaries(store: Store) -> None:
    """The sweeper re-opens transient ``empty summary`` llm-v1 failures so the
    (now retry-capable) llm_summarize pass re-summarizes them — but leaves
    genuine faults, over-cap rows, ``ok`` rows, and *other* summarizers alone."""
    empty = _seed_failed_summary(store, last_error="empty summary")
    real = _seed_failed_summary(store, last_error="psycopg NUL byte write error")
    over_cap = _seed_failed_summary(
        store, last_error="empty summary", attempts=_REOPEN_MAX_ATTEMPTS
    )
    ok_row = _seed_failed_summary(store, last_error=None, status="ok")
    other = _seed_failed_summary(
        store, last_error="empty summary", summarizer="rake-lemma"
    )
    mine = [empty, real, over_cap, ok_row, other]

    n = _reopen_transient_failed_summaries(store, limit=1000)

    assert n >= 1  # at least my transient llm-v1 empty (shared DB may add more)
    with store.pool.connection() as conn:
        surviving = {
            r[0]
            for r in conn.execute(
                "SELECT chunk_id FROM chunk_summaries WHERE chunk_id = ANY(%s)",
                (mine,),
            ).fetchall()
        }
    assert empty not in surviving  # transient blank → re-opened
    assert real in surviving  # genuine fault → stays terminal
    assert over_cap in surviving  # attempts at cap → not re-opened (no loop)
    assert ok_row in surviving  # ok row is never touched
    assert other in surviving  # a different summarizer is out of scope


# ── dead-node compute-lane reap (gr172886 part-b) ──────────────────────
#
# Distinct from the ssh_node exclusion above: these jobs DO get reaped, but
# only when the target_node's own worker is provably dead (no live executor
# left to race via ``reclaim_stale_running``). Each test uses a uuid-tagged
# host so the shared ``precis_test`` DB's ``worker_logs`` / ``host_heartbeat``
# rows from other tests/passes can't perturb the liveness predicate.


def test_dead_node_orphan_is_reaped_when_target_node_is_dead(
    handler: TodoHandler, store: Store
) -> None:
    """Expired lease + no worker_logs/host_heartbeat evidence the target_node
    is alive → reaped as an infra death (failed, failure_class=infra, tagged
    reaped:dead-node-orphan, bubble fires). A co-existing, unrelated stale
    non-ssh_node job in the same pass is still swept by the generic path —
    the two reaps don't interfere."""
    from uuid import uuid4

    node = f"dead-{uuid4().hex[:8]}"
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=0.1,
        lease_offset_hours=-1.0,  # well past the 300s default grace
        executor="ssh_node",
        job_type="struct_relax",
        params={"target_node": node},
    )
    # An unrelated, ordinary stale job that the generic timeout sweep should
    # still catch in the same pass (no double-handling / no interference).
    other_rid = _id_of(handler.put(text="other parent").body)
    other_job_id = _mint_running_job(store, other_rid, backdate_hours=2.0)

    result = run_sweeper_pass(store, limit=10)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags
    assert "STATUS:running" not in job_tags
    assert "reaped:dead-node-orphan" in job_tags
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'failure_class' FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == "infra"
    events = store.events_for(job_id)
    assert any(e.event == "compute-reaped" for e in events)

    # The generic sweep still ran and caught the unrelated job.
    other_tags = {str(t) for t in store.tags_for(other_job_id)}
    assert "STATUS:failed" in other_tags
    assert "swept:claim-orphaned" in other_tags
    assert result.ok == 1  # only the generic-path job counts toward BatchResult
    assert result.claimed == 1


def test_dead_node_orphan_not_reaped_when_worker_logged_recently(
    handler: TodoHandler, store: Store
) -> None:
    """The target_node's own worker logged inside DEAD_WORKER_SILENCE_MIN —
    it's alive and owns its own crash recovery, so the reap must abstain."""
    from uuid import uuid4

    node = f"alive-{uuid4().hex[:8]}"
    _insert_worker_process_log(store, node, "precis-worker", age_minutes=1.0)
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=0.1,
        lease_offset_hours=-1.0,
        executor="ssh_node",
        job_type="struct_relax",
        params={"target_node": node},
    )

    run_sweeper_pass(store, limit=10)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "STATUS:failed" not in job_tags
    assert "reaped:dead-node-orphan" not in job_tags


def test_dead_node_orphan_not_reaped_when_lease_still_live(
    handler: TodoHandler, store: Store
) -> None:
    """The lease hasn't expired yet — a live worker could still hold the job
    even if the node currently looks quiet — so the reap must abstain."""
    from uuid import uuid4

    node = f"quiet-{uuid4().hex[:8]}"
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=0.1,
        lease_offset_hours=0.2,  # ~12 min still to run
        executor="ssh_node",
        job_type="struct_relax",
        params={"target_node": node},
    )

    run_sweeper_pass(store, limit=10)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "STATUS:failed" not in job_tags
    assert "reaped:dead-node-orphan" not in job_tags


def test_dead_node_orphan_not_reaped_when_target_node_is_null(
    handler: TodoHandler, store: Store
) -> None:
    """No ``target_node`` pin means no host to prove dead — leave it for the
    executor / the operator, never guess."""
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=0.1,
        lease_offset_hours=-1.0,
        executor="ssh_node",
        job_type="struct_relax",
        params={},  # no target_node
    )

    run_sweeper_pass(store, limit=10)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "STATUS:failed" not in job_tags
    assert "reaped:dead-node-orphan" not in job_tags


def test_dead_node_orphan_not_reaped_when_host_heartbeat_fresh(
    handler: TodoHandler, store: Store
) -> None:
    """No worker_logs evidence, but a fresh host_heartbeat means the host
    itself is up (some other process is alive) — a wedged single daemon is
    not the same as a dead node, so the reap must abstain."""
    from uuid import uuid4

    node = f"hb-{uuid4().hex[:8]}"
    _upsert_host_heartbeat(store, node, age_minutes=0.5)
    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job_id = _mint_running_job(
        store,
        rid,
        backdate_hours=0.1,
        lease_offset_hours=-1.0,
        executor="ssh_node",
        job_type="struct_relax",
        params={"target_node": node},
    )

    run_sweeper_pass(store, limit=10)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:running" in job_tags
    assert "STATUS:failed" not in job_tags
