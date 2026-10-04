"""Review ledger and revision log (migration 0185, local-mesh-upkeep §2/§2b).

Each test names the acceptance criterion it pins. The revision rows are
written by triggers and sealed at commit, so every write here runs in its
own transaction (a pooled ``with store.pool.connection()`` block) before
the assertion reads.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from precis.runtime import PrecisRuntime
from precis.store import Store
from precis.store.revision_context import (
    apply_revision_context,
    current_revision_context,
    revision_context,
)
from precis.store.types import ChunkInsert


def _finding(store: Store, title: str = "NO reduces to NH3 on Cu(100)") -> int:
    ref = store.insert_ref(kind="finding", slug=None, title=title, meta={"scope": "Cu"})
    return ref.id


def _memory(store: Store, body: str = "first body") -> int:
    """A memory with its body, created in one transaction as ``put`` does."""
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="memory", slug=None, title="a memory", meta={}, conn=conn
        )
        store.chunks.insert_chunks(ref.id, [_chunk(body)], conn=conn)
    return ref.id


def _chunk(text: str) -> ChunkInsert:
    return ChunkInsert(ord=0, text=text, meta={"chunk_kind": "memory_body"})


def _set_title(store: Store, ref_id: int, title: str) -> None:
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET title = %s WHERE ref_id = %s", (title, ref_id))


def _patch_link_meta(store: Store, link_id: int, patch: str) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET meta = meta || %s::jsonb WHERE link_id = %s",
            (patch, link_id),
        )


def _link(store: Store) -> int:
    a, b = _finding(store, "claim A"), _finding(store, "claim B")
    return store.add_link(
        src_ref_id=a, dst_ref_id=b, relation="related-to", meta={"support": "full"}
    ).id


# ── AC 4: one query answers who reviewed it, with what, and is it current ──


def test_reviews_for_chunk_ref_and_link(store: Store) -> None:
    fid = _finding(store)
    lid = _link(store)
    mid = _memory(store)
    chunk_id = store.chunks.list_chunks_for_ref(mid)[0].id

    store.record_target_review("ref", fid, actor="reto", verdict="approved")
    store.record_target_review(
        "link",
        lid,
        actor="mesh-upkeep",
        model="openai/gpt-oss-120b",
        version="v1",
        verdict="proposed",
    )
    store.record_target_review(
        "chunk", chunk_id, actor="opus-review", verdict="approved"
    )

    (ref_rev,) = store.reviews_for("ref", fid)
    assert (ref_rev.actor, ref_rev.model, ref_rev.verdict, ref_rev.current) == (
        "reto",
        None,
        "approved",
        True,
    )
    (link_rev,) = store.reviews_for("link", lid)
    assert (link_rev.model, link_rev.version, link_rev.verdict) == (
        "openai/gpt-oss-120b",
        "v1",
        "proposed",
    )
    assert store.reviews_for("chunk", chunk_id)[0].current


# ── AC 5: an edit makes a review stale with no extra write ─────────────────


def test_title_edit_stales_a_ref_review(store: Store) -> None:
    fid = _finding(store)
    store.record_target_review("ref", fid, actor="reto", verdict="approved")
    _set_title(store, fid, "NO reduces to NH3 on Cu(111)")
    assert not store.reviews_for("ref", fid)[0].current


def test_covered_link_meta_stales_bookkeeping_does_not(store: Store) -> None:
    lid = _link(store)
    store.record_target_review("link", lid, actor="reto", verdict="approved")
    _patch_link_meta(store, lid, '{"verified_at": "2026-10-03T12:00:00Z"}')
    assert store.reviews_for("link", lid)[0].current
    _patch_link_meta(store, lid, '{"support": "partial"}')
    assert not store.reviews_for("link", lid)[0].current


# ── AC 8: one revision per covered change, with prior state and reason ─────


def test_finding_title_edit_writes_one_revision(store: Store) -> None:
    fid = _finding(store)
    sha_before = store.target_sha("ref", fid)
    with revision_context("tighten the facet", actor="mesh-upkeep", model="m1"):
        _set_title(store, fid, "NO reduces to NH3 on Cu(111)")
    (rev,) = store.revisions_for("ref", fid)
    assert rev.event == "edited"
    assert (rev.reason, rev.actor, rev.model) == (
        "tighten the facet",
        "mesh-upkeep",
        "m1",
    )
    assert rev.prev_state["title"] == "NO reduces to NH3 on Cu(100)"
    assert rev.prev_sha == sha_before
    assert rev.new_sha == store.target_sha("ref", fid)


def test_memory_body_and_title_in_one_tx_is_one_revision(store: Store) -> None:
    mid = _memory(store, "first body")
    with revision_context("reword"), store.tx() as conn:
        store.chunks.replace_body_chunk(
            mid, "second body", chunk_kind="memory_body", conn=conn
        )
        conn.execute("UPDATE refs SET title = 'renamed' WHERE ref_id = %s", (mid,))
    (rev,) = store.revisions_for("ref", mid)
    assert rev.prev_state["title"] == "a memory"
    assert [c["text"] for c in rev.prev_state["chunks"]] == ["first body"]
    assert rev.new_sha == store.target_sha("ref", mid) != rev.prev_sha


def test_bookkeeping_meta_writes_no_revision(store: Store) -> None:
    fid = _finding(store)
    lid = _link(store)
    with store.pool.connection() as conn:
        conn.execute(
            'UPDATE refs SET meta = meta || \'{"tagline": "x"}\'::jsonb, '
            "updated_at = now() WHERE ref_id = %s",
            (fid,),
        )
    _patch_link_meta(store, lid, '{"verified_at": "2026-10-03T12:00:00Z"}')
    assert store.revisions_for("ref", fid) == []
    assert store.revisions_for("link", lid) == []


def test_kind_without_history_writes_no_revision(store: Store) -> None:
    job = store.insert_ref(
        kind="job", slug=None, title="a job", meta={"lease_until": "x"}
    )
    _set_title(store, job.id, "renamed job")
    assert store.revisions_for("ref", job.id) == []


def test_ref_moving_into_a_covered_kind_and_link_retarget_log(store: Store) -> None:
    """The columns-only triggers: ``UPDATE OF kind`` and ``UPDATE OF`` ends."""
    job = store.insert_ref(kind="job", slug=None, title="a job", meta={})
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET kind = 'memory' WHERE ref_id = %s", (job.id,))
    assert len(store.revisions_for("ref", job.id)) == 1
    lid = _link(store)
    other = _finding(store, "claim C")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE links SET dst_ref_id = %s WHERE link_id = %s", (other, lid)
        )
    assert len(store.revisions_for("link", lid)) == 1


def test_covered_kinds_change_refreshes_the_triggers(store: Store) -> None:
    """The WHEN lists are literals, rebuilt by a trigger on ``kinds``."""
    job = store.insert_ref(kind="job", slug=None, title="a job", meta={})
    try:
        with store.pool.connection() as conn:
            conn.execute("UPDATE kinds SET covered_meta = '{}' WHERE slug = 'job'")
        _set_title(store, job.id, "renamed once")
        assert len(store.revisions_for("ref", job.id)) == 1
    finally:
        with store.pool.connection() as conn:
            conn.execute("UPDATE kinds SET covered_meta = NULL WHERE slug = 'job'")
    _set_title(store, job.id, "renamed twice")
    assert len(store.revisions_for("ref", job.id)) == 1  # no new row once uncovered


_REVISION_TRIGGERS = (
    "refs_revision_update",
    "refs_revision_kind",
    "refs_revision_delete",
    "links_revision_update",
    "links_revision_ends",
)


def _trigger_state(store: Store) -> tuple[dict[str, int], object, object]:
    """(trigger name -> oid, refreshed_at, last_error) of the revision triggers."""
    with store.pool.connection() as conn:
        oids: dict[str, int] = {
            r[0]: r[1]
            for r in conn.execute(
                "SELECT tgname, oid::bigint FROM pg_trigger WHERE tgname = ANY(%s)",
                (list(_REVISION_TRIGGERS),),
            ).fetchall()
        }
        row = conn.execute(
            "SELECT refreshed_at, last_error FROM revision_trigger_state"
        ).fetchone()
    assert row is not None
    return oids, row[0], row[1]


def test_boot_kind_upsert_leaves_the_triggers_alone(store: Store) -> None:
    """upsert_kinds runs once per kind at every process boot: no DDL, no lock."""
    with store.pool.connection() as conn:
        conn.execute("SELECT precis_revision_triggers_refresh()")  # settle
    before = _trigger_state(store)
    assert set(before[0]) == set(_REVISION_TRIGGERS)
    with store.pool.connection() as conn:
        rows = conn.execute("SELECT slug, is_numeric, title, description FROM kinds")
        specs = [
            SimpleNamespace(kind=s, is_numeric=n, title=t, description=d)
            for s, n, t, d in rows.fetchall()
        ]
    specs.append(
        SimpleNamespace(kind="zz-new", is_numeric=False, title="t", description="d")
    )
    try:
        assert store.upsert_kinds(specs) == len(specs)  # conflicts + one new kind
    finally:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM kinds WHERE slug = 'zz-new'")
    assert _trigger_state(store) == before
    with store.pool.connection() as conn:  # and a no-change refresh is a no-op
        row = conn.execute("SELECT precis_revision_triggers_refresh()").fetchone()
    assert row is not None and row[0] is False


def test_covered_meta_change_rebuilds_the_triggers(store: Store) -> None:
    with store.pool.connection() as conn:
        conn.execute("SELECT precis_revision_triggers_refresh()")  # settle
    before = _trigger_state(store)
    try:
        with store.pool.connection() as conn:
            conn.execute("UPDATE kinds SET covered_meta = '{}' WHERE slug = 'job'")
        after = _trigger_state(store)
        assert after[0] != before[0]  # new trigger oids
        assert after[1] != before[1]  # refreshed_at moved
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 'job' = ANY(covered_kinds) FROM revision_trigger_state"
            ).fetchone()
        assert row is not None and row[0] is True
    finally:
        with store.pool.connection() as conn:
            conn.execute("UPDATE kinds SET covered_meta = NULL WHERE slug = 'job'")
    assert _trigger_state(store)[0] != after[0]


def test_failed_refresh_leaves_a_marker_and_does_not_fail_the_write(
    store: Store,
) -> None:
    with store.pool.connection() as conn:
        conn.execute("SELECT precis_revision_triggers_refresh()")  # settle
    try:
        with store.pool.connection() as conn:
            conn.execute("ALTER FUNCTION precis_refs_revision() RENAME TO zz_gone")
            conn.execute("UPDATE kinds SET covered_meta = '{}' WHERE slug = 'job'")
        assert _trigger_state(store)[2] is not None  # last_error recorded
    finally:
        with store.pool.connection() as conn:
            conn.execute("ALTER FUNCTION zz_gone() RENAME TO precis_refs_revision")
            conn.execute("UPDATE kinds SET covered_meta = NULL WHERE slug = 'job'")
    assert _trigger_state(store)[2] is None  # a good refresh clears it


def test_refresh_gives_up_on_a_held_lock(store: Store) -> None:
    """A busy refs bounds the refresh at lock_timeout; the kinds write succeeds."""
    with store.pool.connection() as conn:
        conn.execute("SELECT precis_revision_triggers_refresh()")  # settle
    before = _trigger_state(store)
    try:
        with store.pool.connection() as holder:
            holder.execute("SELECT 1 FROM refs LIMIT 1")  # ACCESS SHARE, tx open
            t0 = time.monotonic()
            with store.pool.connection() as conn:
                conn.execute("UPDATE kinds SET covered_meta = '{}' WHERE slug = 'job'")
            waited = time.monotonic() - t0
            assert 2.0 < waited < 15.0  # gave up at ~3 s, did not hang
            after = _trigger_state(store)
            assert after[0] == before[0]  # triggers untouched
            assert after[2] is not None and "lock timeout" in str(after[2])
    finally:
        with store.pool.connection() as conn:
            conn.execute("UPDATE kinds SET covered_meta = NULL WHERE slug = 'job'")
    assert _trigger_state(store)[2] is None


def test_created_and_edited_in_one_tx_is_a_creation(store: Store) -> None:
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="finding", slug=None, title="draft claim", meta={}, conn=conn
        )
        conn.execute(
            "UPDATE refs SET title = 'final claim' WHERE ref_id = %s", (ref.id,)
        )
    assert store.revisions_for("ref", ref.id) == []


def test_edit_and_undo_in_one_tx_leaves_no_row(store: Store) -> None:
    fid = _finding(store)
    with store.tx() as conn:
        conn.execute("UPDATE refs SET title = 'other' WHERE ref_id = %s", (fid,))
        conn.execute(
            "UPDATE refs SET title = 'NO reduces to NH3 on Cu(100)' WHERE ref_id = %s",
            (fid,),
        )
    assert store.revisions_for("ref", fid) == []


def test_retire_and_restore_are_events(store: Store) -> None:
    fid = _finding(store)
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = now() WHERE ref_id = %s", (fid,))
    with store.pool.connection() as conn:
        conn.execute("UPDATE refs SET retired_at = NULL WHERE ref_id = %s", (fid,))
    assert [r.event for r in store.revisions_for("ref", fid)] == ["retired", "restored"]


# ── AC 9: diff the reviewed version against now; the head never moves ─────


def test_state_at_the_reviewed_sha(store: Store) -> None:
    fid = _finding(store)
    lid = store.add_link(
        src_ref_id=_finding(store, "other"), dst_ref_id=fid, relation="related-to"
    ).id
    review = store.record_target_review("ref", fid, actor="reto", verdict="approved")
    _set_title(store, fid, "edit one")
    _set_title(store, fid, "edit two")
    state = store.state_at("ref", fid, review.content_sha)
    assert state is not None and state["title"] == "NO reduces to NH3 on Cu(100)"
    assert store.state_at("ref", fid, store.target_sha("ref", fid) or "") is None
    chain = store.revisions_for("ref", fid)
    assert [r.prev_sha for r in chain[1:]] == [r.new_sha for r in chain[:-1]]
    assert {lk.id for lk in store.links_for(fid)} >= {lid}


# ── AC 10: an unwrapped write still logs; a hard delete logs its last state ─


def test_write_without_context_is_unrecorded(store: Store) -> None:
    fid = _finding(store)
    _set_title(store, fid, "changed outside any context")
    (rev,) = store.revisions_for("ref", fid)
    assert rev.reason == "(unrecorded)"
    since = datetime.now(UTC) - timedelta(minutes=5)
    assert store.unrecorded_revision_count(since) >= 1


def test_hard_deleted_link_logs_its_last_state(store: Store) -> None:
    lid = _link(store)
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM links WHERE link_id = %s", (lid,))
    (rev,) = store.revisions_for("link", lid)
    assert rev.event == "deleted"
    assert rev.prev_state["meta"]["support"] == "full"
    assert rev.new_sha is None


# ── legacy stamps mirror into the ledger ───────────────────────────────────


def test_chunk_review_mirrors(store: Store) -> None:
    mid = _memory(store)
    chunk_id = store.chunks.list_chunks_for_ref(mid)[0].id
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_review (chunk_id, checker, approved_sha, verdict) "
            "VALUES (%s, 'flow', 'sha1', 'needs-rework: filler')",
            (chunk_id,),
        )
    (rev,) = store.reviews_for("chunk", chunk_id)
    assert (rev.actor, rev.verdict, rev.note) == (
        "flow",
        "rejected",
        "needs-rework: filler",
    )


def test_verified_by_and_hub_refine_stamps_mirror(store: Store) -> None:
    fid = _finding(store)
    lid = store.add_link(
        src_ref_id=_finding(store, "evidence"),
        dst_ref_id=fid,
        relation="related-to",
        meta={"verified_by": "opus-5/retro-verify"},
    ).id
    (link_rev,) = store.reviews_for("link", lid)
    assert (link_rev.actor, link_rev.model, link_rev.current) == (
        "retro-verify",
        "opus-5",
        True,
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "'last_refined_at', now()::text, 'last_refined_version', '7') "
            "WHERE ref_id = %s",
            (fid,),
        )
    (hub_rev,) = store.reviews_for("ref", fid)
    assert (hub_rev.actor, hub_rev.version, hub_rev.current) == (
        "hub-refine",
        "7",
        True,
    )
    assert store.revisions_for("ref", fid) == []  # a refine stamp is bookkeeping


# ── the reason reaches the trigger through dispatch ────────────────────────


@pytest.fixture
def memory_id(store: Store) -> Iterator[int]:
    yield _memory(store, "the old wording")


def test_edit_reason_reaches_the_revision(
    runtime_with_store: PrecisRuntime, store: Store, memory_id: int
) -> None:
    out = runtime_with_store.dispatch(
        "edit",
        {
            "kind": "memory",
            "id": memory_id,
            "mode": "replace",
            "text": "the new wording",
            "reason": "Reto ruling km-12",
        },
    )
    assert "replaced body" in out, out
    (rev,) = store.revisions_for("ref", memory_id)
    assert (rev.reason, rev.actor) == ("Reto ruling km-12", "agent")


def test_edit_without_reason_names_the_verb(
    runtime_with_store: PrecisRuntime, store: Store, memory_id: int
) -> None:
    runtime_with_store.dispatch(
        "edit",
        {"kind": "memory", "id": memory_id, "mode": "replace", "text": "again"},
    )
    (rev,) = store.revisions_for("ref", memory_id)
    assert rev.reason == "edit(kind='memory')"


# ── the context itself ─────────────────────────────────────────────────────


def test_context_nests_and_only_applies_inside(store: Store) -> None:
    assert current_revision_context() is None
    with revision_context("outer", actor="hub-refine"):
        with revision_context(model="m2") as inner:
            assert (inner.reason, inner.actor, inner.model) == (
                "outer",
                "hub-refine",
                "m2",
            )
            with store.pool.connection() as conn:
                row = conn.execute(
                    "SELECT current_setting('precis.reason', true)"
                ).fetchone()
                assert row is not None and row[0] == "outer"
    with store.pool.connection() as conn:
        apply_revision_context(conn)  # outside a context: sends nothing
        row = conn.execute("SELECT current_setting('precis.reason', true)").fetchone()
        assert row is None or row[0] in (None, "")


def test_event_override_is_merged_into_only() -> None:
    with pytest.raises(ValueError, match="merged-into"):
        with revision_context(event="deleted"):
            pass
