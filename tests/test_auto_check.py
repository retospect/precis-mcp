"""Slice-1b auto-check tests: evaluators, poll-pass, timeout path.

Three layers:

* the evaluator registry's write-time validator
  (``validate_auto_check_spec``) catches typos and bad timeout shapes;
* each evaluator's positive and negative case;
* the poll pass flips ``STATUS:done`` on a true verdict and
  ``STATUS:auto-timeout`` when the spec's ``timeout_at`` has passed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.workers.auto_check import run_auto_check_pass
from precis.workers.auto_check_evaluators import (
    REGISTRY,
    discord_reply_received,
    paper_ingested,
    tag_present,
    time_past,
    validate_auto_check_spec,
)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    return int(body.split("id=")[1].split()[0].rstrip(",.()"))


# ── validator ──────────────────────────────────────────────────────


def test_validate_rejects_non_dict() -> None:
    with pytest.raises(BadInput, match="must be a dict"):
        validate_auto_check_spec("not a dict")


def test_validate_rejects_unknown_type() -> None:
    with pytest.raises(BadInput, match="not a registered evaluator"):
        validate_auto_check_spec({"type": "frobnicate"})


def test_validate_accepts_known_type() -> None:
    validate_auto_check_spec({"type": "time_past", "at": "2099-01-01T00:00:00"})


def test_validate_rejects_bad_timeout_at() -> None:
    with pytest.raises(BadInput, match="timeout_at"):
        validate_auto_check_spec(
            {
                "type": "time_past",
                "at": "2099-01-01T00:00:00",
                "timeout_at": "not a date",
            }
        )


def test_validate_known_registry_keys() -> None:
    assert set(REGISTRY) == {
        "paper_ingested",
        "discord_reply_received",
        "time_past",
        "tag_present",
        "child_job_succeeded",
        "all_child_findings_resolved",
    }


# ── put-time validation surfaces through TodoHandler ───────────────


def test_put_with_unknown_auto_check_type_rejected(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="not a registered evaluator"):
        handler.put(
            text="bad spec",
            meta={"auto_check": {"type": "doesnotexist"}},
        )


def test_put_with_valid_auto_check_stores_meta(handler: TodoHandler) -> None:
    r = handler.put(
        text="wait on something",
        meta={
            "auto_check": {
                "type": "time_past",
                "at": "2099-01-01T00:00:00+00:00",
            }
        },
    )
    rid = _id_of(r.body)
    ref = handler.store.get_ref(kind="todo", id=rid)
    assert ref is not None
    assert ref.meta.get("auto_check", {}).get("type") == "time_past"


# ── time_past evaluator ────────────────────────────────────────────


def test_time_past_true_when_at_in_past(store: Store) -> None:
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    assert time_past.evaluate(store, {"type": "time_past", "at": past}) is True


def test_time_past_false_when_at_in_future(store: Store) -> None:
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    assert time_past.evaluate(store, {"type": "time_past", "at": future}) is False


def test_time_past_rejects_missing_at(store: Store) -> None:
    with pytest.raises(BadInput, match="time_past"):
        time_past.evaluate(store, {"type": "time_past"})


# ── tag_present evaluator ──────────────────────────────────────────


def test_tag_present_open_tag_match(handler: TodoHandler, store: Store) -> None:
    r = handler.put(text="t", tags=["topic:co2-capture"])
    rid = _id_of(r.body)
    assert (
        tag_present.evaluate(store, {"type": "tag_present", "tag": "topic:co2-capture"})
        is True
    )
    _ = rid


def test_tag_present_kind_narrowing(handler: TodoHandler, store: Store) -> None:
    handler.put(text="t", tags=["topic:beta"])
    # No paper carries the tag → False with kind='paper'.
    assert (
        tag_present.evaluate(
            store, {"type": "tag_present", "tag": "topic:beta", "kind": "paper"}
        )
        is False
    )
    # Match against the todo kind.
    assert (
        tag_present.evaluate(
            store, {"type": "tag_present", "tag": "topic:beta", "kind": "todo"}
        )
        is True
    )


def test_tag_present_false_when_no_match(store: Store) -> None:
    assert (
        tag_present.evaluate(
            store, {"type": "tag_present", "tag": "topic:no-such-thing"}
        )
        is False
    )


# ── discord_reply_received evaluator ───────────────────────────────


def test_discord_reply_false_until_memory_tagged(store: Store) -> None:
    spec = {"type": "discord_reply_received", "ask_message_id": "999"}
    assert discord_reply_received.evaluate(store, spec) is False
    mem = store.insert_ref(kind="memory", slug=None, title="answer text", meta={})
    from precis.store.types import Tag

    store.add_tag(mem.id, Tag.open("replied-to:999"), set_by="agent")
    assert discord_reply_received.evaluate(store, spec) is True


# ── paper_ingested evaluator ───────────────────────────────────────


def test_paper_ingested_requires_chunk_embedding(store: Store) -> None:
    # Mint a stub paper with the DOI but no embedded chunk → False.
    paper = store.insert_ref(
        kind="paper", slug="test-2026", title="A test paper", meta={}
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id) "
            "VALUES (%s, %s, %s)",
            ("doi", "10.1234/test-paper", paper.id),
        )
        conn.commit()
    spec = {"type": "paper_ingested", "doi": "10.1234/test-paper"}
    assert paper_ingested.evaluate(store, spec) is False

    # Add a chunk with an embedding → True.
    with store.pool.connection() as conn:
        chunk_row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', 'body') RETURNING chunk_id",
            (paper.id,),
        ).fetchone()
        assert chunk_row is not None
        # The embedders table seeds 'bge-m3' as the default; the FK
        # constraint rejects anything that isn't registered.
        conn.execute(
            "INSERT INTO chunk_embeddings (chunk_id, embedder, status) "
            "VALUES (%s, %s, 'ok')",
            (int(chunk_row[0]), "bge-m3"),
        )
        conn.commit()
    assert paper_ingested.evaluate(store, spec) is True


def test_paper_ingested_rejects_no_identifier(store: Store) -> None:
    with pytest.raises(BadInput, match="paper_ingested needs an identifier"):
        paper_ingested.evaluate(store, {"type": "paper_ingested"})


# ── child_job_succeeded evaluator ──────────────────────────────────


def test_child_job_succeeded_false_with_no_child(
    handler: TodoHandler, store: Store
) -> None:
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent todo")
    rid = _id_of(r.body)
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is False
    )


def test_child_job_succeeded_false_when_child_is_queued(
    handler: TodoHandler, store: Store
) -> None:
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent")
    rid = _id_of(r.body)
    # Mint a job under it (bypass JobHandler so we don't need
    # repo / executor setup).
    job = store.insert_ref(
        kind="job", slug=None, title="child job", meta={}, parent_id=rid
    )
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "queued"),
        set_by="agent",
        replace_prefix=True,
    )
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is False
    )


def test_child_job_succeeded_true_when_child_succeeded(
    handler: TodoHandler, store: Store
) -> None:
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job = store.insert_ref(
        kind="job", slug=None, title="child job", meta={}, parent_id=rid
    )
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is True
    )


def test_child_job_succeeded_skips_planner_coroutine(
    handler: TodoHandler, store: Store
) -> None:
    """An LLM:*-tagged parent (plan_tick coroutine) is never auto-closed
    by a succeeded child job — it drives its own STATUS. Guard 1."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="planner brief", tags=["LLM:opus"])
    rid = _id_of(r.body)
    job = store.insert_ref(kind="job", slug=None, title="tick", meta={}, parent_id=rid)
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is None
    )


def test_child_job_succeeded_skips_with_live_child_todo(
    handler: TodoHandler, store: Store
) -> None:
    """A succeeded child job does not resolve the parent while a sibling
    child todo is still open. Guard 2 (mirrors the STATUS:done guardrail)."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job = store.insert_ref(kind="job", slug=None, title="job", meta={}, parent_id=rid)
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )
    # An open child todo still in flight (no STATUS tag → COALESCE 'open').
    store.insert_ref(kind="todo", slug=None, title="open child", meta={}, parent_id=rid)
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is None
    )


def test_child_job_succeeded_resolves_when_child_todos_done(
    handler: TodoHandler, store: Store
) -> None:
    """Guard 2 only blocks on *live* child todos — a done child todo
    alongside a succeeded job still resolves a deterministic parent."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent")
    rid = _id_of(r.body)
    job = store.insert_ref(kind="job", slug=None, title="job", meta={}, parent_id=rid)
    store.add_tag(
        job.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    child = store.insert_ref(
        kind="todo", slug=None, title="finished child", meta={}, parent_id=rid
    )
    store.add_tag(
        child.id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is True
    )


def test_child_job_succeeded_ignores_other_kinds(
    handler: TodoHandler, store: Store
) -> None:
    """A child todo with STATUS:succeeded doesn't count — must be kind='job'."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="parent")
    rid = _id_of(r.body)
    other = store.insert_ref(
        kind="memory", slug=None, title="memory", meta={}, parent_id=rid
    )
    store.add_tag(other.id, Tag.closed("STATUS", "succeeded"), set_by="agent")
    assert (
        child_job_succeeded.evaluate(store, {"type": "child_job_succeeded"}, ref_id=rid)
        is False
    )


def test_pass_resolves_via_child_job_succeeded(
    handler: TodoHandler, store: Store
) -> None:
    """End-to-end: parent todo with meta.auto_check resolves on job success."""
    from precis.store.types import Tag

    r = handler.put(
        text="dispatch me",
        meta={"auto_check": {"type": "child_job_succeeded"}},
    )
    rid = _id_of(r.body)
    job = store.insert_ref(kind="job", slug=None, title="ok", meta={}, parent_id=rid)
    store.add_tag(
        job.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )
    result = run_auto_check_pass(store, limit=50)
    assert result.ok >= 1
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:done" in tags


# ── poll pass ──────────────────────────────────────────────────────


def test_pass_resolves_time_past_leaf(handler: TodoHandler, store: Store) -> None:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    r = handler.put(
        text="should resolve",
        meta={"auto_check": {"type": "time_past", "at": past}},
    )
    rid = _id_of(r.body)
    result = run_auto_check_pass(store, limit=50)
    assert result.claimed >= 1
    assert result.ok >= 1
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:done" in tags
    assert "STATUS:open" not in tags
    events = store.events_for(rid)
    assert any(e.event == "auto-resolved" and e.source == "auto-check" for e in events)


def test_pass_skips_pending_leaf(handler: TodoHandler, store: Store) -> None:
    future = (datetime.now(UTC) + timedelta(days=30)).isoformat()
    r = handler.put(
        text="still pending",
        meta={"auto_check": {"type": "time_past", "at": future}},
    )
    rid = _id_of(r.body)
    run_auto_check_pass(store, limit=50)
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:open" in tags
    assert "STATUS:done" not in tags


def test_pass_flips_to_auto_timeout(handler: TodoHandler, store: Store) -> None:
    timed_out = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    future = (datetime.now(UTC) + timedelta(days=365)).isoformat()
    r = handler.put(
        text="timed out",
        meta={
            "auto_check": {
                "type": "time_past",
                # `at` is in the future so the evaluator alone wouldn't
                # resolve — but the timeout has passed.
                "at": future,
                "timeout_at": timed_out,
            }
        },
    )
    rid = _id_of(r.body)
    result = run_auto_check_pass(store, limit=50)
    assert result.failed >= 1  # = timeout count under the BatchResult naming
    tags = {str(t) for t in store.tags_for(rid)}
    assert "STATUS:auto-timeout" in tags
    events = store.events_for(rid)
    assert any(e.event == "auto-timeout" and e.source == "auto-check" for e in events)


def test_pass_skips_done_leaves(handler: TodoHandler, store: Store) -> None:
    """An already-resolved leaf must not be touched even if the spec still matches."""
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    r = handler.put(
        text="already done",
        meta={"auto_check": {"type": "time_past", "at": past}},
    )
    rid = _id_of(r.body)
    handler.tag(id=rid, add=["STATUS:done"])
    # Capture event count before the pass; the pass should not append
    # another auto-resolved event.
    events_before = [e for e in store.events_for(rid) if e.source == "auto-check"]
    run_auto_check_pass(store, limit=50)
    events_after = [e for e in store.events_for(rid) if e.source == "auto-check"]
    assert len(events_after) == len(events_before)
