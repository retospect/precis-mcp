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
from precis.workers.sweeper import run_sweeper_pass


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
) -> int:
    """Insert a ``kind='job'`` ref, tag STATUS:running, backdate the tag."""
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
