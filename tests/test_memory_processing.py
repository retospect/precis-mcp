"""Current-revision memory processing evidence on isolated dev-DB fixtures."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from psycopg import Connection

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers._memory_processing import (
    ProcessingReport,
    Stage,
    processing_report,
)
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag
from precis.workers.chunk_keywords import write_chunk_keywords
from precis.workers.embed import EmbedHandler
from precis.workers.summarize import RakeLemmaHandler
from tests.conftest import id_of

BODY = (
    "Memory retrieval should wait for the current body and header embeddings. "
    "A successful write stores the text immediately while derived artifacts arrive "
    "later. Keyword and summary evidence must belong to this revision before a "
    "recall experiment can distinguish retrieval quality from processing lag."
)


@pytest.fixture
def handler(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _chunks(store: Store, ref_id: int) -> dict[str, int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_kind, chunk_id FROM chunks WHERE ref_id = %s "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {kind: int(chunk_id) for kind, chunk_id in rows}


def _default_embedder(store: Store) -> str:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT name FROM embedders WHERE is_default").fetchone()
    assert row is not None
    return str(row[0])


def _embed(store: Store, chunk_ids: list[int], *, model: str | None = None) -> None:
    worker = EmbedHandler(
        MockEmbedder(model=model or _default_embedder(store), dim=store.embedding_dim())
    )
    vector = [1.0] + [0.0] * (store.embedding_dim() - 1)
    with store.tx() as conn:
        for chunk_id in chunk_ids:
            worker.write_ok(conn, chunk_id, vector)


def _finish(store: Store, ref_id: int) -> None:
    """Seed the outcomes the existing workers persist, without running a worker."""
    chunks = _chunks(store, ref_id)
    _embed(store, list(chunks.values()))
    model = _default_embedder(store)
    rake = RakeLemmaHandler()
    with store.tx() as conn:
        for chunk_id in chunks.values():
            rake.write_ok(conn, chunk_id, "memory retrieval; current revision")
        body_id = chunks["memory_body"]
        write_chunk_keywords(conn, body_id, keywords=[], embedder_name=model)
        conn.execute(
            "INSERT INTO chunk_summaries "
            "(chunk_id, summarizer, text, status, content_sha) "
            "SELECT chunk_id, 'llm-v1', 'Current revision readiness', 'ok', content_sha "
            "FROM chunks WHERE chunk_id = %s",
            (body_id,),
        )


def _stage(report: ProcessingReport, prefix: str, kind: str | None = None) -> Stage:
    matches = [
        stage
        for stage in report.stages
        if stage.name.startswith(prefix) and (kind is None or kind in stage.name)
    ]
    assert len(matches) == 1, [(s.name, s.state) for s in report.stages]
    return matches[0]


def test_new_memory_reports_each_applicable_stage_and_current_revision(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Readiness note").body)
    report = processing_report(store, ref_id)
    assert _stage(report, "keywords").state == "pending"
    assert _stage(report, "embed:", "memory_body").state == "pending"
    assert _stage(report, "embed:", "card_combined").state == "pending"
    assert _stage(report, "summary/summarize:", "memory_body").state == "pending"
    assert _stage(report, "summary/llm_summarize").state == "pending"
    assert _stage(report, "categorization").state == "not applicable"
    assert report.outstanding
    rendered = handler.get(id=ref_id, view="processing").body
    assert f"me{ref_id}" in rendered
    assert hashlib.sha256(BODY.encode("utf-8")).hexdigest() in rendered
    assert "Oldest outstanding" in rendered
    assert "content_sha" in rendered
    assert "refs.title + meta.hook" in rendered


def test_pending_stage_shows_phase_lane_and_queue_position(
    handler: MemoryHandler, store: Store
) -> None:
    """gr563702: the view separates eligibility, queueing and execution —
    a queued stage names its lane and how much work claims ahead of it."""
    ref_id = id_of(handler.put(text=BODY, title="Lane note").body)
    rendered = handler.get(id=ref_id, view="processing").body
    assert "phase=queued" in rendered
    assert "lane=memory" in rendered
    assert "queue_ahead≈" in rendered
    assert "Queue policy" in rendered


def test_queued_position_counts_pending_work_ahead(
    handler: MemoryHandler, store: Store
) -> None:
    """A second memory's queue_ahead counts the still-pending chunks of the
    first, which claims before it in lane order (ref_id ascending)."""
    first = id_of(handler.put(text=BODY, title="First note").body)
    second = id_of(handler.put(text=BODY, title="Second note").body)
    report = processing_report(store, second)
    stage = _stage(report, "embed:", "memory_body")
    assert stage.state == "pending"
    assert "phase=queued" in stage.detail
    match = re.search(r"queue_ahead≈(\d+)", stage.detail)
    assert match is not None
    ahead = int(match.group(1))
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunks c "
            "WHERE c.ref_id = %s AND c.retired_at IS NULL",
            (first,),
        ).fetchone()
    assert row is not None
    assert ahead >= int(row[0])


def test_queue_ahead_counts_pending_despite_shadow_model_artifact(
    handler: MemoryHandler, store: Store
) -> None:
    """An artifact row under a different embedder whose content_sha
    matches must not satisfy the pending-work predicate — unparsed, the
    interpolated OR escaped the chunk/model constraints."""
    first = id_of(handler.put(text=BODY, title="First note").body)
    second = id_of(handler.put(text=BODY, title="Second note").body)
    first_body = _chunks(store, first)["memory_body"]
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO embedders (name, dim, is_default) VALUES (%s, %s, false)",
            ("memory-processing-shadow", store.embedding_dim()),
        )
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, status, content_sha) "
            "SELECT chunk_id, %s, 'ok', content_sha "
            "FROM chunks WHERE chunk_id = %s",
            ("memory-processing-shadow", first_body),
        )
    try:
        stage = _stage(processing_report(store, second), "embed:", "memory_body")
        match = re.search(r"queue_ahead≈(\d+)", stage.detail)
        assert match is not None
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT count(*) FROM chunks c "
                "WHERE c.ref_id = %s AND c.retired_at IS NULL",
                (first,),
            ).fetchone()
        assert row is not None
        # first's chunks plus second's own header card (ord -1) claim
        # before second's body; the shadow-model row must not hide
        # first's body chunk.
        assert int(match.group(1)) == int(row[0]) + 1
    finally:
        with store.tx() as conn:
            conn.execute(
                "DELETE FROM chunk_embeddings WHERE embedder = %s",
                ("memory-processing-shadow",),
            )
            conn.execute(
                "DELETE FROM embedders WHERE name = %s",
                ("memory-processing-shadow",),
            )


def test_keywords_blocked_on_body_embedding_is_not_queued(
    handler: MemoryHandler, store: Store
) -> None:
    """The keyword claim gates on a current body embedding, so a body
    without one is an eligibility delay, not a lane position."""
    ref_id = id_of(handler.put(text=BODY, title="Blocked keywords").body)
    stage = _stage(processing_report(store, ref_id), "keywords")
    assert stage.state == "pending"
    assert "phase=queued" not in stage.detail
    assert "phase=awaiting-embedding" in stage.detail
    _embed(store, [_chunks(store, ref_id)["memory_body"]])
    stage = _stage(processing_report(store, ref_id), "keywords")
    assert "phase=queued" in stage.detail
    assert "lane=interactive tier 1" in stage.detail


def test_claim_lease_marks_execution_phase(
    handler: MemoryHandler, store: Store
) -> None:
    """A live chunk_claims row is execution evidence — claimed phase, never
    distinguishable from abandoned by stored evidence."""
    ref_id = id_of(handler.put(text=BODY, title="Claim note").body)
    body_id = _chunks(store, ref_id)["memory_body"]
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact) "
            "SELECT %s, name FROM embedders WHERE is_default",
            (body_id,),
        )
        conn.commit()
    stage = _stage(processing_report(store, ref_id), "embed:", "memory_body")
    assert stage.state == "pending"
    assert "phase=claimed" in stage.detail


def test_put_nudges_an_embed_job_for_the_fresh_chunks(
    handler: MemoryHandler, store: Store
) -> None:
    """gr563702: the write path queues a small embed_batch so fresh memory
    vectors land within seconds, not on the materializer cadence."""
    handler.put(text=BODY, title="Nudge note")
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT r.ref_id FROM refs r "
            "WHERE r.kind = 'job' AND r.retired_at IS NULL "
            "AND r.meta->>'job_type' = 'embed_batch' "
            "AND r.meta->>'idem_key' LIKE 'embed-nudge:%' "
            "ORDER BY r.ref_id DESC LIMIT 1"
        ).fetchone()
    assert row is not None, "put should nudge a fresh-embed job"


def test_ready_memory_requires_both_body_and_header_under_active_embedder(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Ready header").body)
    chunks = _chunks(store, ref_id)
    _embed(store, [chunks["memory_body"]])
    report = processing_report(store, ref_id)
    assert _stage(report, "embed:", "memory_body").state == "complete"
    assert _stage(report, "embed:", "card_combined").state == "pending"
    _embed(store, [chunks["card_combined"]])
    assert (
        _stage(processing_report(store, ref_id), "embed:", "card_combined").state
        == "complete"
    )

    original_model = _default_embedder(store)
    try:
        with store.tx() as conn:
            conn.execute(
                "INSERT INTO embedders (name, dim, is_default) VALUES (%s, %s, false)",
                ("memory-processing-alternate", store.embedding_dim()),
            )
            conn.execute("UPDATE embedders SET is_default = false WHERE is_default")
            conn.execute(
                "UPDATE embedders SET is_default = true WHERE name = %s",
                ("memory-processing-alternate",),
            )
        report = processing_report(store, ref_id)
        assert report.embedder == "memory-processing-alternate"
        assert _stage(report, "embed:", "memory_body").state == "pending"
        assert _stage(report, "embed:", "card_combined").state == "pending"
    finally:
        # Seeded vocab survives the store fixture's per-test data cleanup.
        with store.tx() as conn:
            conn.execute(
                "DELETE FROM embedders WHERE name = %s",
                ("memory-processing-alternate",),
            )
            conn.execute(
                "UPDATE embedders SET is_default = true WHERE name = %s",
                (original_model,),
            )


def test_body_edit_invalidates_old_artifacts_but_retains_unchanged_header(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Stable title").body)
    old = _chunks(store, ref_id)
    _finish(store, ref_id)
    assert not processing_report(store, ref_id).outstanding

    replacement = BODY.replace("Memory retrieval", "Semantic retrieval")
    receipt = handler.edit(id=ref_id, mode="replace", text=replacement)
    current = _chunks(store, ref_id)
    assert current["memory_body"] != old["memory_body"]
    assert current["card_combined"] == old["card_combined"]
    report = processing_report(store, ref_id)
    for prefix in ("keywords", "embed:", "summary/summarize:", "summary/llm_summarize"):
        assert _stage(report, prefix, "memory_body").state == "pending"
    assert _stage(report, "embed:", "card_combined").state == "complete"
    assert hashlib.sha256(replacement.encode("utf-8")).hexdigest() in report.render()
    assert f"pc{old['memory_body']}:" not in report.render()
    assert receipt.body.count("view='processing'") == 1


def test_existing_put_link_has_no_processing_hint_when_artifacts_are_ready(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Finished memory").body)
    target_id = id_of(handler.put(text="Related memory", title="Link target").body)
    _finish(store, ref_id)
    receipt = handler.edit(
        id=ref_id,
        mode="find-replace",
        find="Memory retrieval",
        text="Semantic retrieval",
    )
    # A body edit produces a new chunk revision; finish it before linking.
    assert processing_report(store, ref_id).outstanding
    assert "view='processing'" in receipt.body
    _finish(store, ref_id)
    linked = handler.put(id=ref_id, link=f"memory:{target_id}", rel="related-to")
    assert not processing_report(store, ref_id).outstanding
    assert "view='processing'" not in linked.body


def test_stale_hashes_on_current_chunk_do_not_count_as_complete(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Hash comparison").body)
    _finish(store, ref_id)
    body_id = _chunks(store, ref_id)["memory_body"]
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunk_embeddings SET content_sha = 'older-revision' WHERE chunk_id = %s",
            (body_id,),
        )
        conn.execute(
            "UPDATE chunk_summaries SET content_sha = 'older-revision' WHERE chunk_id = %s",
            (body_id,),
        )
        conn.execute(
            "UPDATE chunks SET keywords_meta = keywords_meta || "
            '\'{"content_sha": "older-revision"}\'::jsonb WHERE chunk_id = %s',
            (body_id,),
        )
    report = processing_report(store, ref_id)
    for prefix in ("keywords", "embed:", "summary/summarize:", "summary/llm_summarize"):
        assert _stage(report, prefix, "memory_body").state == "pending"


def test_embedding_of_stale_header_does_not_count(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Original header").body)
    _finish(store, ref_id)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s", ("Changed header", ref_id)
        )
    report = processing_report(store, ref_id)
    assert _stage(report, "embed:", "card_combined").state == "pending"
    assert _stage(report, "summary/summarize:", "card_combined").state == "pending"
    assert "differs from current refs.title + meta.hook" in report.render()


def test_failed_current_embedding_has_recorded_error_and_next_step(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Failure note").body)
    body_id = _chunks(store, ref_id)["memory_body"]
    worker = EmbedHandler(MockEmbedder(model=_default_embedder(store)))
    with store.tx() as conn:
        worker.write_failed(conn, body_id, "fixture embedding failure")
    stage = _stage(processing_report(store, ref_id), "embed:", "memory_body")
    assert stage.state == "failed"
    assert "fixture embedding failure" in stage.detail
    assert "next:" in stage.detail
    assert f"pc{body_id}" in stage.detail
    assert "artifact_created_at=" in stage.detail


def test_short_and_no_index_bodies_are_not_silently_pending(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text="Short memory", title="Short note").body)
    report = processing_report(store, ref_id)
    assert _stage(report, "keywords").state == "not applicable"
    assert _stage(report, "summary/llm_summarize").state == "not applicable"
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunks SET meta = meta || '{\"no_index\": true}'::jsonb "
            "WHERE ref_id = %s",
            (ref_id,),
        )
    report = processing_report(store, ref_id)
    assert _stage(report, "embed:", "memory_body").state == "disabled"
    assert _stage(report, "keywords").state == "disabled"
    assert "meta.no_index=true" in report.render()


def test_claim_is_coarse_pending_and_outstanding_age_remains_visible(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Claimed note").body)
    body_id = _chunks(store, ref_id)["memory_body"]
    model = _default_embedder(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE chunks SET created_at = now() - interval '3 hours' WHERE chunk_id = %s",
            (body_id,),
        )
        conn.execute(
            "INSERT INTO chunk_claims (chunk_id, artifact, claimed_at, attempts) "
            "VALUES (%s, %s, now() - interval '2 hours', 2)",
            (body_id, model),
        )
    report = processing_report(store, ref_id)
    stage = _stage(report, "embed:", "memory_body")
    assert stage.state == "pending"
    assert "claimed_at=" in stage.detail
    assert "claim_attempts=2" in stage.detail
    rendered = report.render()
    age = re.search(r"age (\d+)s", rendered)
    assert age is not None and int(age.group(1)) >= 10800
    assert "cannot distinguish" in rendered


def test_receipts_hint_once_and_previews_do_not_hint(
    handler: MemoryHandler,
) -> None:
    preview = handler.put(text=BODY, title="Receipt note", dry_run=True)
    assert "view='processing'" not in preview.body
    created = handler.put(text=BODY, title="Receipt note")
    ref_id = id_of(created.body)
    assert created.body.count("view='processing'") == 1
    preview_edit = handler.edit(
        id=ref_id,
        mode="find-replace",
        find="Memory retrieval",
        text="Semantic retrieval",
        dry_run=True,
    )
    assert "view='processing'" not in preview_edit.body
    edited = handler.edit(
        id=ref_id,
        mode="find-replace",
        find="Memory retrieval",
        text="Semantic retrieval",
    )
    assert edited.body.count("view='processing'") == 1
    metadata = handler.edit(id=ref_id, hook="A changed search header")
    assert metadata.body.count("view='processing'") == 1


def test_old_categorization_marker_cannot_certify_edited_body(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(
        handler.put(text=BODY, title="Dream note", tags=["DREAM:speculative"]).body
    )
    store.add_tag(ref_id, Tag.closed("MOVE", "analogy"), set_by="agent")
    store.add_tag(ref_id, Tag.closed("MOVECASCADE", "1"), set_by="agent")
    with store.tx() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - interval '1 day' "
            "WHERE ref_id = %s AND tag_id IN "
            "(SELECT tag_id FROM tags WHERE namespace = 'MOVECASCADE')",
            (ref_id,),
        )
    handler.edit(id=ref_id, mode="replace", text=BODY + " Revised inference.")
    stage = _stage(processing_report(store, ref_id), "categorization")
    assert stage.state == "pending"
    assert "marker predates current body" in stage.detail
    assert "blocks automatic reclassification" in stage.detail
    assert "next:" in stage.detail


@pytest.mark.parametrize("unavailable", [False, True])
def test_batch_edit_receipt_names_outstanding_memories_once(
    handler: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    unavailable: bool,
) -> None:
    ids: list[str | int] = [
        id_of(handler.put(text=BODY, title=title).body)
        for title in ("First batch memory", "Second batch memory")
    ]
    atomic = store.atomic
    events: list[str | int] = []
    in_transaction = False

    @contextmanager
    def observed_atomic() -> Iterator[Connection]:
        nonlocal in_transaction
        with atomic() as conn:
            in_transaction = True
            events.append("begin")
            yield conn
        in_transaction = False
        events.append("committed")

    def observed_report(store: Store, ref_id: int) -> ProcessingReport:
        events.append(ref_id)
        if in_transaction:
            raise RuntimeError("processing queried before batch commit")
        if unavailable and ref_id == ids[0]:
            raise RuntimeError("processing status unavailable")
        return processing_report(store, ref_id)

    monkeypatch.setattr(store, "atomic", observed_atomic)
    monkeypatch.setattr(
        "precis.handlers._memory_processing.processing_report", observed_report
    )
    preview = handler.edit(
        id=ids,
        mode="find-replace",
        find="Memory retrieval",
        text="Semantic retrieval",
        dry_run=True,
    )
    assert "view='processing'" not in preview.body
    assert events == ["begin", "committed"]
    events.clear()
    edited = handler.edit(
        id=ids,
        mode="find-replace",
        find="Memory retrieval",
        text="Semantic retrieval",
    )
    assert events == ["begin", "committed", *ids]
    assert edited.body.count("view='processing'") == 1
    hint = edited.body.split("Check processing for ", 1)[1]
    for ref_id in ids:
        assert str(ref_id) in edited.body
        assert f"me{ref_id}" in hint


def test_categorization_matches_worker_eligibility_for_expired_dream_tag(
    handler: MemoryHandler, store: Store
) -> None:
    """The worker's tags_any gate includes stored tags even after expiry."""
    ref_id = id_of(
        handler.put(
            text=BODY, title="Expired dream tag", tags=["DREAM:speculative"]
        ).body
    )
    with store.tx() as conn:
        conn.execute(
            "UPDATE ref_tags SET expires_at = now() - interval '1 day' "
            "WHERE ref_id = %s AND tag_id IN "
            "(SELECT tag_id FROM tags WHERE namespace = 'DREAM')",
            (ref_id,),
        )
    assert _stage(processing_report(store, ref_id), "categorization").state == "pending"


def test_processing_view_does_not_heat_chunks(
    handler: MemoryHandler, store: Store
) -> None:
    ref_id = id_of(handler.put(text=BODY, title="Read-only note").body)
    with store.pool.connection() as conn:
        before = conn.execute(
            "SELECT chunk_id, last_seen, accesses FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
    handler.get(id=ref_id, view="processing")
    with store.pool.connection() as conn:
        after = conn.execute(
            "SELECT chunk_id, last_seen, accesses FROM chunks WHERE ref_id = %s ORDER BY chunk_id",
            (ref_id,),
        ).fetchall()
    assert after == before
