"""Selected memory processing requests against isolated dev-DB fixtures."""

from __future__ import annotations

import hashlib
import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput, NotFound
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag
from precis.store.types import ChunkInsert
from precis.workers.embed import EmbedHandler
from tests.conftest import id_of

pytestmark = pytest.mark.db

BODY = (
    "A memory processing request must identify its current content revision. "
    "The body and header need separate embeddings before semantic recall can "
    "be evaluated. A queued work order records intent and does not establish "
    "that either embedding is ready."
)


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _memory(handler: MemoryHandler) -> int:
    return id_of(handler.put(text=BODY, title="Selected processing note").body)


def _request(handler: MemoryHandler, ref_id: int, **kwargs: Any) -> str:
    return handler.put(
        id=ref_id,
        mode="process",
        stages=["embeddings"],
        **kwargs,
    ).body


def _job_id(receipt: str) -> int:
    match = re.search(r"\bjo(\d+)\b", receipt)
    assert match is not None, receipt
    return int(match[1])


def _state(receipt: str) -> str:
    match = re.search(r"^embeddings: (\w+)", receipt, re.MULTILINE)
    assert match is not None, receipt
    return match[1]


def _jobs(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' "
            "AND meta->>'job_type' = 'embed_batch' ORDER BY ref_id"
        ).fetchall()
    return [int(row[0]) for row in rows]


def _chunks(store: Store, ref_id: int) -> dict[str, tuple[int, str, str | None]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_kind, chunk_id, text, content_sha FROM chunks "
            "WHERE ref_id = %s AND retired_at IS NULL ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
    return {kind: (int(chunk_id), text, sha) for kind, chunk_id, text, sha in rows}


def _model(store: Store) -> str:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default ORDER BY name LIMIT 1"
        ).fetchone()
    assert row is not None
    return str(row[0])


def _embed(store: Store, chunk_ids: list[int]) -> None:
    worker = EmbedHandler(MockEmbedder(model=_model(store), dim=store.embedding_dim()))
    vector = [1.0] + [0.0] * (store.embedding_dim() - 1)
    with store.tx() as conn:
        for chunk_id in chunk_ids:
            worker.write_ok(conn, chunk_id, vector)


def _artifacts(store: Store, ref_id: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT e.chunk_id, e.status, e.content_sha, e.attempts, e.created_at "
            "FROM chunk_embeddings e JOIN chunks c USING (chunk_id) "
            "WHERE c.ref_id = %s AND e.embedder = %s ORDER BY e.chunk_id",
            (ref_id, _model(store)),
        ).fetchall()


def _status(store: Store, job_id: int, status: str) -> None:
    store.add_tag(
        job_id, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )


def test_promote_targets_current_body_and_header_and_never_embeds_inline(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    chunks = _chunks(store, ref_id)
    receipt = _request(handler, ref_id, action="promote", wait_s=0)
    assert "queued" in receipt
    assert _state(receipt) == "queued"
    assert _artifacts(store, ref_id) == []

    job = store.get_ref(kind="job", id=_job_id(receipt))
    assert job is not None
    assert job.meta["job_type"] == "embed_batch"
    assert job.meta["executor"] == "job_inproc"
    assert job.meta["origin"] == "agent"
    assert job.prio == 1
    target = job.meta["params"]["memory_target"]
    assert target["ref_id"] == ref_id
    assert target["embedder"] == _model(store)
    assert {c["chunk_id"] for c in target["chunks"]} == {
        chunk[0] for chunk in chunks.values()
    }
    assert set(chunks) == {"memory_body", "card_combined"}
    for chunk in chunks.values():
        pinned = next(c for c in target["chunks"] if c["chunk_id"] == chunk[0])
        assert pinned["content_sha"] == chunk[2]
        assert (
            pinned["text_sha256"]
            == hashlib.sha256(chunk[1].encode("utf-8")).hexdigest()
        )
    assert "Next:" in receipt


@pytest.mark.parametrize("status", ["queued", "running"])
def test_repeat_and_cross_action_requests_reuse_active_job(
    handler: MemoryHandler, store: Store, status: str
) -> None:
    ref_id = _memory(handler)
    first = _job_id(_request(handler, ref_id, action="promote"))
    _status(store, first, status)
    for action in ("promote", "retry"):
        receipt = _request(handler, ref_id, action=action)
        assert _job_id(receipt) == first
        assert status in receipt
    assert _jobs(store) == [first]


@pytest.mark.parametrize("status", ["queued", "running"])
@pytest.mark.parametrize("artifacts_ready", [False, True])
@pytest.mark.parametrize("active_action", ["promote", "retry"])
def test_recompute_refuses_active_non_recompute_even_after_bulk_completion(
    handler: MemoryHandler,
    store: Store,
    status: str,
    artifacts_ready: bool,
    active_action: str,
) -> None:
    ref_id = _memory(handler)
    first = _job_id(_request(handler, ref_id, action="promote"))
    if active_action == "retry":
        _status(store, first, "failed")
        first = _job_id(_request(handler, ref_id, action="retry"))
    _status(store, first, status)
    chunks = _chunks(store, ref_id)
    if artifacts_ready:
        _embed(store, [chunk[0] for chunk in chunks.values()])
    before = _artifacts(store, ref_id)
    jobs = _jobs(store)

    receipt = _request(handler, ref_id, action="recompute", wait_s=1)

    assert _state(receipt) == "refused"
    assert f"{active_action} is active" in receipt
    assert _job_id(receipt) == first
    assert "to finish" in receipt
    assert "'action': 'recompute'" in receipt.split("Next:", 1)[1]
    assert _jobs(store) == jobs
    assert _artifacts(store, ref_id) == before

    if artifacts_ready:
        _status(store, first, "succeeded")
        accepted = _request(handler, ref_id, action="recompute")
        assert _state(accepted) == "queued"
        assert _job_id(accepted) != first
        assert _artifacts(store, ref_id) == []


def test_promote_preserves_complete_artifacts_and_requires_explicit_recompute(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    chunks = _chunks(store, ref_id)
    _embed(store, [chunk[0] for chunk in chunks.values()])
    before = _artifacts(store, ref_id)
    receipt = _request(handler, ref_id, action="promote")
    assert "ready" in receipt
    assert _jobs(store) == []
    assert _artifacts(store, ref_id) == before

    receipt = _request(handler, ref_id, action="recompute")
    job_id = _job_id(receipt)
    assert "queued" in receipt
    assert _artifacts(store, ref_id) == []
    assert _job_id(_request(handler, ref_id, action="recompute")) == job_id

    _embed(store, [chunk[0] for chunk in chunks.values()])
    _status(store, job_id, "succeeded")
    completed = _artifacts(store, ref_id)
    repeated = _request(handler, ref_id, action="recompute")
    assert "ready" in repeated
    assert _job_id(repeated) == job_id
    assert _jobs(store) == [job_id]
    assert _artifacts(store, ref_id) == completed


def test_failed_stage_requires_retry_and_preserves_other_completed_embedding(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    chunks = _chunks(store, ref_id)
    header_id = chunks["card_combined"][0]
    body_id = chunks["memory_body"][0]
    _embed(store, [header_id])
    worker = EmbedHandler(MockEmbedder(model=_model(store), dim=store.embedding_dim()))
    with store.tx() as conn:
        worker.write_failed(conn, body_id, "fixture embedding failure")
    before = _artifacts(store, ref_id)
    refused = _request(handler, ref_id, action="promote")
    assert "refused" in refused
    assert "retry" in refused
    assert "Next:" in refused
    assert _jobs(store) == []
    assert _artifacts(store, ref_id) == before

    receipt = _request(handler, ref_id, action="retry")
    job_id = _job_id(receipt)
    assert "queued" in receipt
    assert _artifacts(store, ref_id) == [row for row in before if row[0] == header_id]
    assert _job_id(_request(handler, ref_id, action="retry")) == job_id


def test_failed_job_without_artifact_can_be_retried_explicitly(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    failed_id = _job_id(_request(handler, ref_id, action="promote"))
    _status(store, failed_id, "failed")
    assert _artifacts(store, ref_id) == []
    retry_id = _job_id(_request(handler, ref_id, action="retry"))
    assert retry_id != failed_id
    assert _job_id(_request(handler, ref_id, action="retry")) == retry_id


@pytest.mark.parametrize("action", ["retry", "recompute"])
def test_pending_work_refusal_points_to_promote(
    handler: MemoryHandler, store: Store, action: str
) -> None:
    ref_id = _memory(handler)
    receipt = _request(handler, ref_id, action=action)
    assert _state(receipt) == "refused"
    next_section = receipt.split("Next:", 1)[1]
    assert "'action': 'promote'" in next_section
    assert "'action': 'retry'" not in next_section
    assert _jobs(store) == []


@pytest.mark.parametrize(
    "stage",
    [
        "keywords",
        "summary/summarize:rake-lemma",
        "summary/llm_summarize",
        "categorization",
    ],
)
def test_unsupported_stage_refuses_with_next_step_and_no_work_order(
    handler: MemoryHandler, store: Store, stage: str
) -> None:
    ref_id = _memory(handler)
    if stage == "categorization":
        store.add_tag(ref_id, Tag.closed("DREAM", "speculative"), set_by="agent")
    receipt = handler.put(
        id=ref_id, mode="process", stages=[stage], action="promote"
    ).body
    assert stage in receipt
    assert "refused" in receipt
    assert "Next:" in receipt
    assert _jobs(store) == []


def test_disabled_embedding_refuses_with_next_step(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunks SET meta = meta || '{\"no_index\":true}'::jsonb "
            "WHERE ref_id = %s AND chunk_kind = 'memory_body'",
            (ref_id,),
        )
    receipt = _request(handler, ref_id, action="promote")
    assert "refused" in receipt
    assert "no_index" in receipt
    assert "Next:" in receipt
    assert _jobs(store) == []


def test_existing_chunk_claim_is_not_stolen_or_reported_running(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    body_id = _chunks(store, ref_id)["memory_body"][0]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact) VALUES (%s, %s)",
            (body_id, _model(store)),
        )
        before = conn.execute(
            "SELECT artifact, claimed_at, attempts FROM chunk_claims WHERE chunk_id = %s",
            (body_id,),
        ).fetchall()
    receipt = _request(handler, ref_id, action="promote")
    assert "refused" in receipt
    assert "Next:" in receipt
    assert _jobs(store) == []
    with store.pool.connection() as conn:
        after = conn.execute(
            "SELECT artifact, claimed_at, attempts FROM chunk_claims WHERE chunk_id = %s",
            (body_id,),
        ).fetchall()
    assert after == before


def test_terminal_job_without_current_artifacts_is_never_ready(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    job_id = _job_id(_request(handler, ref_id, action="promote"))
    _status(store, job_id, "succeeded")
    receipt = _request(handler, ref_id, action="promote", wait_s=0)
    assert _state(receipt) != "ready"
    assert _artifacts(store, ref_id) == []


def test_body_edit_requests_new_revision_without_reusing_old_work_order(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    old_body_id = _chunks(store, ref_id)["memory_body"][0]
    old_job_id = _job_id(_request(handler, ref_id, action="promote"))
    handler.edit(id=ref_id, mode="replace", text=BODY + " The content has changed.")
    receipt = _request(handler, ref_id, action="promote")
    new_job_id = _job_id(receipt)
    assert new_job_id != old_job_id
    job = store.get_ref(kind="job", id=new_job_id)
    assert job is not None
    selected = {c["chunk_id"] for c in job.meta["params"]["memory_target"]["chunks"]}
    assert old_body_id not in selected
    assert _chunks(store, ref_id)["memory_body"][0] in selected


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stages": []},
        {"stages": ["unknown-stage"]},
        {"action": "force"},
        {"wait_s": -1},
        {"wait_s": 11},
        {"wait_s": float("nan")},
        {"wait_s": True},
        {"text": "silently changing a memory is forbidden"},
    ],
)
def test_invalid_process_request_changes_nothing(
    handler: MemoryHandler, store: Store, kwargs: dict[str, Any]
) -> None:
    ref_id = _memory(handler)
    before = _chunks(store, ref_id)
    params: dict[str, Any] = {"stages": ["embeddings"], "action": "promote", **kwargs}
    with pytest.raises(BadInput) as error:
        handler.put(id=ref_id, mode="process", **params)
    assert error.value.next or error.value.options
    assert _jobs(store) == []
    assert _chunks(store, ref_id) == before


def test_process_requires_one_memory_and_rejects_other_kinds(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    invalid_ids: list[Any] = [None, [ref_id], [ref_id, ref_id]]
    for invalid_id in invalid_ids:
        with pytest.raises(BadInput):
            handler.put(id=invalid_id, mode="process", stages=["embeddings"])
    foreign = store.insert_ref(kind="todo", slug=None, title="Not a memory")
    with pytest.raises(NotFound):
        _request(handler, int(foreign.id), action="promote")
    assert _jobs(store) == []


def test_selected_chunk_count_is_capped_before_queueing(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = _memory(handler)
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=n,
                text=f"Additional fixture memory chunk {n}",
                meta={"chunk_kind": "memory_body"},
            )
            for n in range(1, 33)
        ],
    )
    receipt = _request(handler, ref_id, action="promote")
    assert _state(receipt) == "refused"
    assert "32" in receipt
    assert "Next:" in receipt
    assert _jobs(store) == []


def test_wait_exits_at_bound_without_claiming_completion(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import _memory_process

    ref_id = _memory(handler)
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    monkeypatch.setattr(_memory_process.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_memory_process.time, "sleep", sleep)
    receipt = _request(handler, ref_id, action="promote", wait_s=1)
    assert _state(receipt) == "queued"
    assert sleeps
    assert sum(sleeps) == pytest.approx(1)
    assert _artifacts(store, ref_id) == []


def test_wait_reports_ready_only_after_both_current_artifacts_arrive(
    handler: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.handlers import _memory_process

    ref_id = _memory(handler)
    chunks = _chunks(store, ref_id)
    clock = [0.0]

    def finish(seconds: float) -> None:
        clock[0] += seconds
        _embed(store, [chunk[0] for chunk in chunks.values()])
        _status(store, _jobs(store)[0], "succeeded")

    monkeypatch.setattr(_memory_process.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(_memory_process.time, "sleep", finish)
    receipt = _request(handler, ref_id, action="promote", wait_s=1)
    assert _state(receipt) == "ready"
    assert clock[0] < 1
    assert len(_artifacts(store, ref_id)) == 2
