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
from precis.workers import auto_check
from precis.workers.auto_check import run_auto_check_pass
from precis.workers.auto_check_evaluators import (
    REGISTRY,
    derived_job_succeeded,
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
        "derived_job_succeeded",
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


# ── derived_job_succeeded + compute-lane failure bubble ──


def _requested_job(store: Store, *, status: str) -> tuple[int, int]:
    """A structure artifact owning a ``struct_relax`` job at ``status``, plus a
    todo that ``requested`` it. Returns ``(todo_id, job_id)``. Built with raw
    store inserts so the test doesn't drag in the ssh_node executor."""
    from precis.store.types import Tag

    art = store.insert_ref(kind="structure", slug="pd-req", title="pd", meta={})
    todo = store.insert_ref(kind="todo", slug=None, title="wants relax", meta={})
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="struct_relax",
        meta={"job_type": "struct_relax"},
        parent_id=art.id,
    )
    store.add_tag(
        job.id, Tag.parse_strict(f"STATUS:{status}", kind="job"), set_by="agent"
    )
    store.add_link(src_ref_id=todo.id, dst_ref_id=job.id, relation="requested")
    return todo.id, job.id


def test_derived_job_succeeded_waits_then_resolves(store: Store) -> None:
    from precis.store.types import Tag

    todo_id, job_id = _requested_job(store, status="running")
    spec = {"type": "derived_job_succeeded"}
    # Not yet — the requested job is still running.
    assert derived_job_succeeded.evaluate(store, spec, ref_id=todo_id) is False
    # It converges: flip the job to succeeded.
    store.add_tag(
        job_id,
        Tag.parse_strict("STATUS:succeeded", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    assert derived_job_succeeded.evaluate(store, spec, ref_id=todo_id) is True


def test_derived_job_succeeded_ignores_unrelated_jobs(store: Store) -> None:
    # A todo with no ``requested`` link never resolves off someone else's job.
    todo = store.insert_ref(kind="todo", slug=None, title="idle", meta={})
    spec = {"type": "derived_job_succeeded"}
    assert derived_job_succeeded.evaluate(store, spec, ref_id=todo.id) is False


def test_compute_lane_failure_bubbles_to_requester(store: Store) -> None:
    """A derived job parents on its artifact, so its failure has no todo to
    tag directly — the bubble follows the ``requested`` link to the requester."""
    from precis.handlers._job_bubble import bubble_job_failure

    todo_id, job_id = _requested_job(store, status="failed")
    bubble_job_failure(store, job_id)
    tags = {str(t) for t in store.tags_for(todo_id)}
    assert f"child-failed:{job_id}" in tags


def test_compute_lane_failure_without_requester_is_noop(store: Store) -> None:
    """A pure direct-manipulation build (no requester todo) has nowhere to
    bubble — the failure just sits on the artifact's runs. No crash, no tag."""
    from precis.handlers._job_bubble import bubble_job_failure

    art = store.insert_ref(kind="structure", slug="pd-noreq", title="pd", meta={})
    job = store.insert_ref(
        kind="job", slug=None, title="struct_relax", meta={}, parent_id=art.id
    )
    bubble_job_failure(store, job.id)  # must not raise
    assert not store.tags_for(art.id)


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
    """A meta.llm_tier-set parent (plan_tick coroutine) is never
    auto-closed by a succeeded child job — it drives its own STATUS.
    Guard 1."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="planner brief", meta={"llm_tier": "opus"})
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


def test_child_job_succeeded_skips_recurring_watch(
    handler: TodoHandler, store: Store
) -> None:
    """A recurring watch (``meta.schedule`` set) is never
    auto-closed by its first spawned child job succeeding — it owns its
    own terminal state (cron never resolves; a one-shot self-tags
    STATUS:done). Regression for the 2-day news_poll / cast-watch
    outage: guard 3."""
    from precis.store.types import Tag
    from precis.workers.auto_check_evaluators import child_job_succeeded

    r = handler.put(text="news_poll watch", meta={"schedule": {"cron": "*/30 * * * *"}})
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
        is False
    )
    # Sanity: the ordinary one-shot deterministic-parent case still
    # resolves — this guard must not regress the intended behaviour.
    r2 = handler.put(text="plain parent")
    rid2 = _id_of(r2.body)
    job2 = store.insert_ref(kind="job", slug=None, title="job", meta={}, parent_id=rid2)
    store.add_tag(
        job2.id,
        Tag.closed("STATUS", "succeeded"),
        set_by="agent",
        replace_prefix=True,
    )
    assert (
        child_job_succeeded.evaluate(
            store, {"type": "child_job_succeeded"}, ref_id=rid2
        )
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


# ── all_child_findings_resolved evaluator ────────────────────────────
# Acquisition-mode AC #4: a lit-hunt todo whose children
# are all `acquiring` is NOT resolved (poll-again), same as `tracing`;
# a todo whose children reached a terminal finding status resolves
# exactly as before. No code change was needed for the exclusion (any
# status outside the resolved set already polls again by construction)
# — these tests verify that, they don't exercise a new branch.


def _seed_child_finding(store: Store, *, parent_id: int, status: str | None) -> int:
    from precis.store.types import Tag

    ref = store.insert_ref(
        kind="finding", slug=None, title="child claim", meta={}, parent_id=parent_id
    )
    if status is not None:
        store.add_tag(
            ref.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
        )
    return ref.id


def test_all_child_findings_resolved_false_when_acquiring(
    handler: TodoHandler, store: Store
) -> None:
    from precis.workers.auto_check_evaluators import all_child_findings_resolved

    r = handler.put(text="lit hunt")
    rid = _id_of(r.body)
    _seed_child_finding(store, parent_id=rid, status="acquiring")
    assert (
        all_child_findings_resolved.evaluate(
            store, {"type": "all_child_findings_resolved"}, ref_id=rid
        )
        is False
    )


def test_all_child_findings_resolved_false_when_mixed_acquiring_and_established(
    handler: TodoHandler, store: Store
) -> None:
    """One resolved child does not outvote a still-acquiring sibling."""
    from precis.workers.auto_check_evaluators import all_child_findings_resolved

    r = handler.put(text="lit hunt")
    rid = _id_of(r.body)
    _seed_child_finding(store, parent_id=rid, status="established")
    _seed_child_finding(store, parent_id=rid, status="acquiring")
    assert (
        all_child_findings_resolved.evaluate(
            store, {"type": "all_child_findings_resolved"}, ref_id=rid
        )
        is False
    )


def test_all_child_findings_resolved_true_when_all_terminal(
    handler: TodoHandler, store: Store
) -> None:
    """Unchanged behaviour: established / dead_chain / multi_candidate
    children resolve the todo exactly as before."""
    from precis.workers.auto_check_evaluators import all_child_findings_resolved

    r = handler.put(text="lit hunt")
    rid = _id_of(r.body)
    _seed_child_finding(store, parent_id=rid, status="established")
    _seed_child_finding(store, parent_id=rid, status="dead_chain")
    _seed_child_finding(store, parent_id=rid, status="multi_candidate")
    assert (
        all_child_findings_resolved.evaluate(
            store, {"type": "all_child_findings_resolved"}, ref_id=rid
        )
        is True
    )


# ── candidate sampling: no leaf may be starved by the ones ahead of it ──


def _stuck_leaf(store: Store, title: str) -> int:
    """An open todo carrying `child_job_succeeded` with NO succeeded child job
    — it evaluates "pending" on every pass and so never leaves the candidate
    set. This is the shape that piled up at the head of the old ref_id
    ordering."""
    from precis.store.types import Tag

    r = store.insert_ref(
        kind="todo",
        slug=None,
        title=title,
        meta={"auto_check": {"type": "child_job_succeeded"}},
    )
    store.add_tag(r.id, Tag.closed("STATUS", "open"), set_by="system")
    return int(r.id)


def _resolvable_leaf(store: Store, title: str) -> int:
    """An open todo whose child job has already succeeded — the pass MUST
    resolve it once it actually looks at it."""
    from precis.store.types import Tag

    todo = _stuck_leaf(store, title)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="done work",
        meta={"job_type": "fix_gripe"},
        parent_id=todo,
    )
    store.add_tag(
        job.id, Tag.parse_strict("STATUS:succeeded", kind="job"), set_by="agent"
    )
    return todo


def test_resolvable_leaf_behind_the_batch_limit_is_not_starved(
    store: Store,
) -> None:
    """Regression (2026-08-07): the candidate query was `ORDER BY ref_id LIMIT
    n`, so an oversized population meant the pass re-read the same lowest-ranked
    leaves forever and never looked past the cutoff. The leaves that pile up at
    the head are precisely the ones that never resolve, so the block was
    permanent: prod had 169 open leaves against a limit of 50, with the
    morning-brief tick stranded at rank 161 — its job had succeeded, but the
    tick never flipped to STATUS:done, and a recurring watch won't spawn its
    next tick while the last one is open. Both daily casts silently stopped.

    Here: many never-resolving leaves are created FIRST (so they hold every low
    ref_id), then one resolvable leaf last. Under the old ordering it sat
    outside every batch and could never resolve; with random sampling the pass
    reaches it.
    """
    for i in range(12):
        _stuck_leaf(store, f"never resolves {i}")
    target = _resolvable_leaf(store, "has a succeeded job")

    # A limit smaller than the stuck population — under ref_id ordering the
    # target is unreachable no matter how many passes run. The pass budget is
    # sized off the LIVE population rather than a constant: this suite shares
    # one DB, so sibling tests add auto_check leaves and lengthen the odds of
    # any single sampled batch containing the target. At `limit` of N drawn
    # from a population of P, each pass hits it with probability N/P, so
    # 40 * P/N passes makes a false red vanishingly unlikely while a genuine
    # regression (target unreachable, probability 0) still fails every time.
    limit = 4
    population = auto_check._open_auto_check_total(store)
    for _ in range(40 * max(1, -(-population // limit))):
        run_auto_check_pass(store, limit=limit)
        tags = {str(t) for t in store.tags_for(target)}
        if "STATUS:done" in tags:
            break

    assert "STATUS:done" in {str(t) for t in store.tags_for(target)}, (
        "a resolvable leaf behind the batch cutoff must still get evaluated"
    )
