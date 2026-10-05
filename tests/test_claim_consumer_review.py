"""Unsigned claim edits invalidate consumers atomically, without versions or spend."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import psycopg
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _finding_edit
from precis.handlers._review_view import render_review_view
from precis.handlers.todo import TodoHandler
from precis.store import ChunkInsert
from precis.taproot.canon import CanonicalClaim
from precis.taproot.hub import HubFrozenError, mint_hub, refine_claim_sentence
from tests.workers._helpers import seed_ref


def _hub(store: Any, title: str = "DFT predicts the original result.") -> int:
    return mint_hub(store, CanonicalClaim(sentence=title, scope={}))


def _link(
    store: Any, src: int, dst: int, relation: str = "related-to", **kwargs: Any
) -> None:
    store.add_link(
        src_ref_id=src, dst_ref_id=dst, relation=relation, set_by="system", **kwargs
    )


def _draft(store: Any, name: str, text: str) -> tuple[int, int]:
    project = seed_ref(store, kind="todo", title="Review project")
    draft, title = store.drafts.create_draft(
        name=name, title=name, project_ref_id=project
    )
    chunk = store.drafts.add_chunks(
        ref_id=draft.id, chunk_kind="paragraph", text=text, at={"after": title.handle}
    )[0]
    store.drafts.record_review(chunk.chunk_id, "human")
    return draft.id, chunk.chunk_id


def _snapshot(store: Any) -> list[Any]:
    with psycopg.connect(store.pool.conninfo) as conn:
        return [
            conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
            for table in (
                "refs",
                "chunks",
                "links",
                "ref_identifiers",
                "ref_events",
                "nanopub_publish",
                "nanopub_artifacts",
                "chunk_review",
                "reviews",
            )
        ]


def test_candidate_edit_lists_flags_and_preserves_every_consumer(store: Any) -> None:
    claim = _hub(store)
    row = store.nanopub_create_publish_row(claim)
    draft, chunk = _draft(store, "linked", f"We rely on [fi{claim}].")
    _link(
        store,
        draft,
        claim,
        relation="cites",
        src_pos=1,
        meta={"cited_title": "original citation pin", "cited_pub_id": "old-pin"},
    )
    consumers = {draft}
    for relation in (
        "establishes",
        "corroborates",
        "contradicts",
        "refines",
        "conjunct-of",
    ):
        finding = _hub(store, f"DFT predicts the {relation} result.")
        _link(store, claim, finding, relation)
        consumers.add(finding)
    for kind in ("pathway", "quest", "todo"):
        with store.pool.connection() as conn:
            conn.execute(
                "INSERT INTO kinds(slug, is_numeric, title) VALUES (%s,true,%s) ON CONFLICT DO NOTHING",
                (kind, kind),
            )
        ref = seed_ref(store, kind=kind, title=f"consumer {kind}")
        store.update_ref(ref, meta_patch={"keep": "unchanged"})
        _link(store, ref, claim)
        _link(store, claim, ref)  # duplicate/inverse must still list once
        consumers.add(ref)
    retired = seed_ref(store, kind="todo", title="retired")
    _link(store, retired, claim)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (retired,))
        before_links = conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()
        assert (
            conn.execute("SELECT to_regclass('nanopub_supersessions')").fetchone()[0]
            is None
        )
    unrelated, other_chunk = _draft(store, "unrelated", "No claim reference.")
    with store.pool.connection() as conn:
        before_links = conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()

    response = _finding_edit.edit(
        store, kind="finding", id=claim, title="DFT predicts the corrected result."
    )
    refs = store.fetch_refs_by_ids(consumers)
    for ref in refs.values():
        marker = ref.meta["claim_review_required"][f"fi{claim}"]
        assert marker["source"] == f"fi{claim}"
        assert marker["changed_at"].endswith("Z")
        assert marker["reason"] in response.body
        assert ref.title in response.body
        reviews = store.reviews_for("ref", ref.id)
        assert len(reviews) == 1 and reviews[0].verdict == "proposed"
        assert reviews[0].note == marker["reason"]
        if ref.kind in ("pathway", "quest", "todo"):
            assert ref.meta["keep"] == "unchanged"
    assert f"needs re-review ({len(consumers)})" in response.body
    assert (
        "claim_review_required"
        not in store.fetch_refs_by_ids([unrelated])[unrelated].meta
    )
    assert store.drafts.review_status_for_chunk(chunk) == []
    assert store.drafts.review_status_for_chunk(other_chunk)
    raw_todo = next(r for r in refs.values() if r.kind == "todo")
    raw = TodoHandler(hub=Hub(store=store)).get(kind="todo", id=raw_todo.id, view="raw")
    assert "needs re-review because" in raw.body
    draft_ref = store.get_ref(kind="draft", id=draft)
    assert (
        f"needs re-review because fi{claim}"
        in render_review_view(store, draft_ref).body
    )
    assert store.nanopub_publish_row(claim).id == row.id
    with store.pool.connection() as conn:
        assert conn.execute("SELECT count(*) FROM nanopub_publish").fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM nanopub_artifacts").fetchone()[0] == 0
        assert (
            conn.execute("SELECT * FROM links ORDER BY link_id").fetchall()
            == before_links
        )


def test_prose_fallback_and_retained_alias_without_materialized_links(
    store: Any,
) -> None:
    claim = _hub(store)
    alias = store.current_pub_ids([claim])[claim]
    draft, _ = _draft(store, "no-autolinks", f"Use [§{alias}].")
    todo = seed_ref(store, kind="todo", title="unlinked todo")
    store.chunks.insert_chunks(
        todo,
        [
            ChunkInsert(
                ord=0, text=f"Check finding:{claim}", meta={"chunk_kind": "todo_body"}
            )
        ],
    )
    other, _ = _draft(
        store, "lookalike", f"Do not confuse [fi{claim}9999] with this source."
    )
    result = refine_claim_sentence(store, claim, "DFT predicts a sharper result.")
    assert {r["ref_id"] for r in result["needs_review"]} == {draft, todo}
    second = refine_claim_sentence(
        store, claim, "DFT predicts another corrected result."
    )
    assert {r["ref_id"] for r in second["needs_review"]} == {draft, todo}
    assert "claim_review_required" not in store.fetch_refs_by_ids([other])[other].meta


def test_reviewed_row_reopens_in_place_and_noop_dryrun_do_nothing(store: Any) -> None:
    claim = _hub(store)
    row = store.nanopub_create_publish_row(claim)
    assert store.nanopub_approve(
        row.id,
        approved_title="Original.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )
    consumer = seed_ref(store, kind="todo")
    _link(store, consumer, claim)
    before = _snapshot(store)
    refine_claim_sentence(store, claim, "DFT predicts the original result.")
    assert _snapshot(store) == before
    _finding_edit.edit(
        store,
        kind="finding",
        id=claim,
        meta={"scope": {"material": "Pd"}},
        dry_run=True,
    )
    assert _snapshot(store) == before
    response = _finding_edit.edit(
        store, kind="finding", id=claim, meta={"scope": {"material": "Pd"}}
    )
    assert f"needs re-review because fi{claim}" in response.body
    updated = store.nanopub_publish_row(claim)
    assert updated.id == row.id and updated.state == "candidate"
    assert updated.grounding == {} and updated.approved_title is None


@pytest.mark.parametrize(
    "posture", ["signed", "anchored", "published", "reopened-signed"]
)
def test_signed_boundary_refuses_without_changes(store: Any, posture: str) -> None:
    claim = _hub(store)
    consumer = seed_ref(store, kind="todo")
    _link(store, consumer, claim)
    row = store.nanopub_create_publish_row(claim)
    if posture == "reopened-signed":
        store.nanopub_insert_artifact(
            publish_id=row.id,
            claim_ref_id=claim,
            artifact_type="claim",
            trig_bytes=b"# synthetic, not signed in test",
            trusty_uri="test:artifact",
            aida_uri="test:aida",
            claim_sha="x",
            signer="test:signer",
            key_fingerprint="test:key",
            dois=[],
        )
    else:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE nanopub_publish SET state = %s WHERE id = %s", (posture, row.id)
            )
    before = _snapshot(store)
    with pytest.raises(HubFrozenError):
        refine_claim_sentence(store, claim, "DFT predicts a forbidden correction.")
    assert _snapshot(store) == before


def test_propagation_error_rolls_back_every_write(store: Any, monkeypatch: Any) -> None:
    claim = _hub(store)
    for i in range(2):
        todo = seed_ref(store, kind="todo", title=f"consumer {i}")
        _link(store, todo, claim)
    original = store.record_target_review
    calls = 0

    def failing(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("synthetic propagation failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "record_target_review", failing)
    before = _snapshot(store)
    with pytest.raises(RuntimeError, match="propagation failure"):
        refine_claim_sentence(store, claim, "DFT predicts a rolled back result.")
    assert _snapshot(store) == before


def test_chunk_user_refuses_before_orphaning_links(store: Any) -> None:
    claim = _hub(store)
    todo = seed_ref(store, kind="todo")
    _link(store, todo, claim, dst_pos=0)
    before = _snapshot(store)
    with pytest.raises(BadInput, match="chunk-level users"):
        refine_claim_sentence(store, claim, "DFT predicts a changed result.")
    assert _snapshot(store) == before


def test_reciprocal_unsigned_edits_use_ordered_locks(store: Any) -> None:
    a, b = _hub(store, "First claim."), _hub(store, "Second claim.")
    _link(store, a, b, "refines")
    barrier = Barrier(2)

    def edit(ref_id: int) -> Any:
        barrier.wait(timeout=10)
        return refine_claim_sentence(store, ref_id, f"Changed claim {ref_id}.")

    with ThreadPoolExecutor(max_workers=2) as executor:
        with psycopg.connect(store.pool.conninfo) as blocker:
            blocker.execute(
                "SELECT ref_id FROM refs WHERE ref_id = %s FOR NO KEY UPDATE",
                (min(a, b),),
            )
            futures = [executor.submit(edit, ref_id) for ref_id in (a, b)]
            deadline = time.monotonic() + 10
            both_waiting = False
            while time.monotonic() < deadline:
                waiters_row = blocker.execute(
                    "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() AND wait_event_type='Lock' AND query LIKE 'SELECT ref_id FROM refs WHERE ref_id = ANY%' "
                ).fetchone()
                assert waiters_row is not None
                if waiters_row[0] == 2:
                    both_waiting = True
                    break
                blocker.execute("SELECT pg_stat_clear_snapshot()")
                time.sleep(0.01)
        results = [f.result(timeout=10) for f in futures]
        assert both_waiting
    assert [{u["ref_id"] for u in r["needs_review"]} for r in results] == [{b}, {a}]
    refs = store.fetch_refs_by_ids([a, b])
    assert f"fi{b}" in refs[a].meta["claim_review_required"]
    assert f"fi{a}" in refs[b].meta["claim_review_required"]


def _wait_for_lock(conn: Any, prefix: str) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        if conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database() "
            "AND wait_event_type = 'Lock' AND query LIKE %s",
            (prefix + "%",),
        ).fetchone()[0]:
            return
        time.sleep(0.01)
    raise AssertionError(f"no PG lock waiter for {prefix}")


def _fake_artifact(conn: Any, row_id: int, claim: int) -> int:
    return int(
        conn.execute(
            "INSERT INTO nanopub_artifacts (publish_id,claim_ref_id,artifact_type,trig_bytes,"
            "trusty_uri,aida_uri,claim_sha,signer,key_fingerprint,dois) "
            "VALUES (%s,%s,'claim',%s,'test:artifact','test:aida','x','test:signer','test:key','[]') RETURNING id",
            (row_id, claim, b"# synthetic bytes; no signing performed"),
        ).fetchone()[0]
    )


def test_unmaterialized_generated_chunk_citation_refuses(store: Any) -> None:
    claim = _hub(store)
    with store.pool.connection() as conn:
        chunk = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id = %s AND ord = 0", (claim,)
        ).fetchone()[0]
    _draft(store, "chunk-prose", f"See [fb{chunk}].")
    before = _snapshot(store)
    with pytest.raises(BadInput, match="chunk-level users"):
        refine_claim_sentence(store, claim, "Changed claim.")
    assert _snapshot(store) == before


def test_artifact_committed_during_publish_lock_wait_refuses_edit(store: Any) -> None:
    claim = _hub(store)
    row = store.nanopub_create_publish_row(claim)
    assert store.nanopub_approve(
        row.id,
        approved_title="Original.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )
    consumer = seed_ref(store, kind="todo")
    _link(store, consumer, claim)
    before = _snapshot(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(store.pool.conninfo) as signer:
            artifact_id = _fake_artifact(signer, row.id, claim)
            edit = executor.submit(
                refine_claim_sentence, store, claim, "Forbidden edit."
            )
            _wait_for_lock(signer, "SELECT p.id, p.state")
        with pytest.raises(HubFrozenError):
            edit.result(timeout=10)
    after = _snapshot(store)
    assert after[:6] == before[:6] and after[7:] == before[7:]
    artifact = store.nanopub_artifact(artifact_id)
    assert (
        artifact is not None
        and artifact.trig_bytes == b"# synthetic bytes; no signing performed"
    )


def test_edit_wins_before_artifact_insert_and_stale_sign_cas_fails(store: Any) -> None:
    claim = _hub(store)
    row = store.nanopub_create_publish_row(claim)
    assert store.nanopub_approve(
        row.id,
        approved_title="Original.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )

    def record() -> bool:
        with psycopg.connect(store.pool.conninfo) as signer:
            artifact = _fake_artifact(signer, row.id, claim)
        return store.nanopub_record_signed(
            row.id,
            trusty_uri="test:artifact",
            artifact_id=artifact,
            dependency_codes={},
        )

    with ThreadPoolExecutor(max_workers=1) as executor:
        with store.tx() as editing:
            refine_claim_sentence(store, claim, "Edited before signing.", conn=editing)
            sign = executor.submit(record)
            _wait_for_lock(editing, "INSERT INTO nanopub_artifacts")
        assert sign.result(timeout=10) is False
    updated = store.nanopub_publish_row(claim)
    assert (
        updated is not None
        and updated.state == "candidate"
        and updated.artifact_id is None
    )
    assert store.fetch_refs_by_ids([claim])[claim].title == "Edited before signing."


def test_consumer_added_while_waiting_is_rediscovered(store: Any) -> None:
    # A lower-id consumer requires releasing/reacquiring the whole ordered set.
    consumer = seed_ref(store, kind="todo", title="Concurrent consumer")
    claim = _hub(store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        with psycopg.connect(store.pool.conninfo) as blocker:
            blocker.execute(
                "SELECT ref_id FROM refs WHERE ref_id = %s FOR NO KEY UPDATE", (claim,)
            )
            edit = executor.submit(
                refine_claim_sentence, store, claim, "Changed after wait."
            )
            _wait_for_lock(blocker, "SELECT ref_id FROM refs WHERE ref_id = ANY")
            _link(store, consumer, claim)
        result = edit.result(timeout=10)
    assert [u["ref_id"] for u in result["needs_review"]] == [consumer]
    assert (
        f"fi{claim}"
        in store.fetch_refs_by_ids([consumer])[consumer].meta["claim_review_required"]
    )


def test_hypothesis_propagation_failure_rolls_back_payload_and_history(
    store: Any, monkeypatch: Any
) -> None:
    claim = _hub(store)
    store.update_ref(
        claim,
        meta_patch={
            "artifact_type": "hypothesis",
            "proposed_payload": {"testable_by": "old test", "motivation": "old reason"},
        },
    )
    row = store.nanopub_create_publish_row(claim, artifact_type="hypothesis")
    assert store.nanopub_approve(
        row.id,
        approved_title="Original.",
        claim_sha="x",
        aida_uri="test:aida",
        grounding={},
    )
    consumer = seed_ref(store, kind="todo")
    _link(store, consumer, claim)
    before = _snapshot(store)

    def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("synthetic hypothesis propagation failure")

    monkeypatch.setattr(store, "record_target_review", failing)
    with pytest.raises(RuntimeError, match="propagation failure"):
        _finding_edit.edit(
            store,
            kind="finding",
            id=claim,
            testable_by="new test",
            motivation="new reason",
        )
    assert _snapshot(store) == before
