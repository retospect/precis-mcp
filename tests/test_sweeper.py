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
    _reopen_transient_failed_embeds,
    _reopen_transient_failed_summaries,
    run_sweeper_pass,
)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


def _mint_running_job(
    store: Store,
    parent_id: int | None,
    *,
    backdate_hours: float,
    lease_offset_hours: float | None = None,
) -> int:
    """Insert a ``kind='job'`` ref, tag STATUS:running, backdate the tag.

    ``lease_offset_hours`` (optional) stamps ``meta.lease_until`` at
    ``now() + offset``: a positive value gives a *live* lease (worker
    still owns the job — must not be swept), a negative value an
    *expired* one. ``None`` leaves the meta lease-less (legacy job).
    """
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick test job",
        meta={"job_type": "plan_tick", "executor": "claude_inproc"},
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
    """Stale STATUS:running → STATUS:failed + swept tag + parent bubble."""
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
    assert f"child-failed:{job_id}" in parent_tags


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
