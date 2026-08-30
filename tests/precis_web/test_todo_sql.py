"""Real-PG regression tests for the todo route's raw SQL.

The route-level tests in ``test_todo.py`` run against the web
``FakeStore``, which does *not* parse SQL — so ``_load_tags``'s
``status_since`` subselect and ``_child_jobs``'s ``created_at`` /
``meta->>'started_at'`` columns are never actually exercised there.
This file uses the live ``store`` fixture (see ``test_status_sql.py``)
to run the real queries.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from precis.store import Tag
from precis_web.routes.todo import _child_jobs, _load_tags


def test_load_tags_status_since_matches_status_tag_write(store: Any) -> None:
    """``status_since`` is the ``ref_tags.created_at`` of the STATUS row —
    non-``None`` and close to the moment the tag was written."""
    todo = store.insert_ref(kind="todo", slug=None, title="write the report", meta={})
    before = datetime.now(UTC)
    store.add_tag(todo.id, Tag.closed("STATUS", "doing"), replace_prefix=True)
    after = datetime.now(UTC)

    tags = _load_tags(store, [todo.id])

    assert tags[todo.id]["status"] == "doing"
    since = tags[todo.id]["status_since"]
    assert since is not None
    if since.tzinfo is None:
        since = since.replace(tzinfo=UTC)
    assert before <= since <= after


def test_load_tags_no_status_tag_defaults_open_with_none_since(store: Any) -> None:
    """A todo with no ``STATUS:*`` tag reads ``status='open'`` and
    ``status_since=None`` — there's no tag row to have a ``created_at``."""
    todo = store.insert_ref(kind="todo", slug=None, title="untagged", meta={})

    tags = _load_tags(store, [todo.id])

    assert tags[todo.id]["status"] == "open"
    assert tags[todo.id]["status_since"] is None


def test_child_jobs_returns_created_at(store: Any) -> None:
    """``_child_jobs`` surfaces ``created_at`` (queued-at) for each job."""
    todo = store.insert_ref(kind="todo", slug=None, title="parent", meta={})
    job = store.insert_ref(
        kind="job", slug=None, title="attempt", parent_id=todo.id, meta={}
    )

    jobs = _child_jobs(store, [todo.id])

    by_id = {j["id"]: j for j in jobs}
    assert job.id in by_id
    assert by_id[job.id]["created_at"] is not None


def test_child_jobs_returns_started_at_from_meta(store: Any) -> None:
    """``started_at`` reads back ``meta['started_at']`` when present, and
    is ``None`` when that meta key is absent (a job never claimed, or
    claimed before the stamp shipped)."""
    todo = store.insert_ref(kind="todo", slug=None, title="parent", meta={})
    claimed = store.insert_ref(
        kind="job", slug=None, title="claimed", parent_id=todo.id, meta={}
    )
    unclaimed = store.insert_ref(
        kind="job", slug=None, title="unclaimed", parent_id=todo.id, meta={}
    )
    stamp = datetime.now(UTC).isoformat()
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('started_at', %s::text) "
            "WHERE ref_id = %s",
            (stamp, claimed.id),
        )
        conn.commit()

    jobs = _child_jobs(store, [todo.id])

    by_id = {j["id"]: j for j in jobs}
    assert by_id[claimed.id]["started_at"] == stamp
    assert by_id[unclaimed.id]["started_at"] is None
