"""The `chunk_review` memoized approval ledger (paper-writing pipeline rung
3, docs/backlog/paper-writing-pipeline.md §"Review — the memoized approval
ledger"). Migration 0086; store ops in `_draft_ops.py`; MCP surface via
`edit(kind='draft', review=…)` and `view='review'` / `'review-diff'`."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.draft import DraftHandler
from precis.store import BlockInsert
from precis.store._draft_ops import DraftChunk, content_sha
from precis.store.store import Store


def _project(store: Store) -> int:
    return store.insert_ref(kind="todo", slug=None, title="Review project").id


def _draft_with_paragraph(store: Store) -> tuple[int, DraftChunk]:
    """A fresh draft with one paragraph chunk under its title heading."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="rv", title="T", project_ref_id=proj)
    p = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="original text",
        at={"after": title.handle},
    )[0]
    return ref.id, p


# ---------------------------------------------------------------------------
# Store: record_review
# ---------------------------------------------------------------------------


def test_record_review_upserts(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    sha1 = store.drafts.record_review(p.chunk_id, "human", verdict="approved")
    assert sha1 == content_sha("original text")

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT approved_sha, verdict FROM chunk_review "
            "WHERE chunk_id=%s AND checker=%s",
            (p.chunk_id, "human"),
        ).fetchone()
    assert row == (sha1, "approved")

    # a second call on the same (chunk, checker) overwrites — not a new row.
    store.drafts.edit_text(p.handle, "changed text")
    sha2 = store.drafts.record_review(p.chunk_id, "human", verdict="changes")
    assert sha2 != sha1
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT approved_sha, verdict FROM chunk_review WHERE chunk_id=%s",
            (p.chunk_id,),
        ).fetchall()
    assert rows == [(sha2, "changes")]  # still exactly one row


def test_record_review_rejects_body_chunk(store: Store) -> None:
    """A body/paper chunk has NULL content_sha — the ledger is draft-family
    only; recording a review for it must raise BadInput, not silently
    write a row that can never go dirty."""
    ref = store.insert_ref(kind="paper", slug="rv-body", title="Body")
    store.blocks.insert_blocks(
        ref.id, [BlockInsert(pos=0, text="body text", embedding=None)]
    )
    with store.pool.connection() as conn:
        chunk_row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id=%s ORDER BY ord LIMIT 1",
            (ref.id,),
        ).fetchone()
        assert chunk_row is not None
        chunk_id = chunk_row[0]

    with pytest.raises(BadInput, match="content_sha"):
        store.drafts.record_review(chunk_id, "human")


# ---------------------------------------------------------------------------
# Store: chunks_requiring_review
# ---------------------------------------------------------------------------


def test_chunks_requiring_review_never_reviewed_is_flagged(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    dirty = store.drafts.chunks_requiring_review(ref_id, "human")
    assert p.chunk_id in {r["chunk_id"] for r in dirty}


def test_chunks_requiring_review_clears_after_record_and_reflags_after_edit(
    store: Store,
) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.drafts.record_review(p.chunk_id, "human")
    dirty = {
        r["chunk_id"] for r in store.drafts.chunks_requiring_review(ref_id, "human")
    }
    assert p.chunk_id not in dirty

    # a weave bumps content_sha → dirty again for every checker (spec: "the
    # chunk goes dirty for every checker")
    store.drafts.edit_text(p.handle, "edited text bumps the sha")
    dirty = {
        r["chunk_id"] for r in store.drafts.chunks_requiring_review(ref_id, "human")
    }
    assert p.chunk_id in dirty

    # re-recording at the new sha clears it again
    store.drafts.record_review(p.chunk_id, "human")
    dirty = {
        r["chunk_id"] for r in store.drafts.chunks_requiring_review(ref_id, "human")
    }
    assert p.chunk_id not in dirty


def test_chunks_requiring_review_scoped_per_checker(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.drafts.record_review(p.chunk_id, "human")
    # 'human' is clean, but 'cites' has never reviewed this chunk
    dirty_cites = {
        r["chunk_id"] for r in store.drafts.chunks_requiring_review(ref_id, "cites")
    }
    assert p.chunk_id in dirty_cites
    dirty_human = {
        r["chunk_id"] for r in store.drafts.chunks_requiring_review(ref_id, "human")
    }
    assert p.chunk_id not in dirty_human


# ---------------------------------------------------------------------------
# Store: review_status_for_chunk
# ---------------------------------------------------------------------------


def test_review_status_for_chunk_dirty_bit(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.drafts.record_review(p.chunk_id, "human")
    statuses = store.drafts.review_status_for_chunk(p.chunk_id)
    assert len(statuses) == 1
    assert statuses[0]["checker"] == "human"
    assert statuses[0]["dirty"] is False

    store.drafts.edit_text(p.handle, "now different")
    statuses = store.drafts.review_status_for_chunk(p.chunk_id)
    assert statuses[0]["dirty"] is True


# ---------------------------------------------------------------------------
# Store: review_diff_since
# ---------------------------------------------------------------------------


def test_review_diff_since_produces_diff_after_edit(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    since_sha = store.drafts.record_review(p.chunk_id, "human")
    store.drafts.edit_text(p.handle, "brand new text")

    diff = store.drafts.review_diff_since(p.chunk_id, since_sha)
    assert "-original text" in diff
    assert "+brand new text" in diff


def test_review_diff_since_empty_when_unchanged(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    sha = store.drafts.record_review(p.chunk_id, "human")
    assert store.drafts.review_diff_since(p.chunk_id, sha) == ""


# ---------------------------------------------------------------------------
# Store: retract_review
# ---------------------------------------------------------------------------


def test_retract_review_deletes_and_reports_existence(store: Store) -> None:
    _ref_id, p = _draft_with_paragraph(store)
    # nothing to retract yet
    assert store.drafts.retract_review(p.chunk_id, "human") is False

    store.drafts.record_review(p.chunk_id, "human")
    statuses = store.drafts.review_status_for_chunk(p.chunk_id)
    assert statuses and statuses[0]["checker"] == "human"

    assert store.drafts.retract_review(p.chunk_id, "human") is True
    assert store.drafts.review_status_for_chunk(p.chunk_id) == []
    # a second retract is a no-op — nothing left to delete
    assert store.drafts.retract_review(p.chunk_id, "human") is False


# ---------------------------------------------------------------------------
# Store: approved_pairs_at_current_sha
# ---------------------------------------------------------------------------


def test_approved_pairs_at_current_sha_tracks_edits(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.drafts.record_review(p.chunk_id, "flow")
    assert (p.chunk_id, "flow") in store.drafts.approved_pairs_at_current_sha(ref_id)

    store.drafts.edit_text(p.handle, "edited — stale now")
    assert (p.chunk_id, "flow") not in store.drafts.approved_pairs_at_current_sha(
        ref_id
    )

    store.drafts.record_review(p.chunk_id, "flow")
    assert (p.chunk_id, "flow") in store.drafts.approved_pairs_at_current_sha(ref_id)


# ---------------------------------------------------------------------------
# Store: review_subtree_chunk_ids
# ---------------------------------------------------------------------------


def test_review_subtree_chunk_ids_includes_heading_and_descendants_in_order(
    store: Store,
) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="subtree", title="T", project_ref_id=proj
    )
    section = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Section",
        at={"after": title.handle},
    )[0]
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="p1", at={"into": section.handle}
    )[0]
    p2 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="p2", at={"after": p1.handle}
    )[0]
    # a sibling section outside the subtree must not appear
    other = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Other", at={"after": section.handle}
    )[0]

    ids = store.drafts.review_subtree_chunk_ids(ref.id, section.chunk_id)

    assert ids == [section.chunk_id, p1.chunk_id, p2.chunk_id]
    assert other.chunk_id not in ids


def test_review_subtree_chunk_ids_unknown_chunk_is_empty(store: Store) -> None:
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="subtree-empty", title="T", project_ref_id=proj
    )
    assert store.drafts.review_subtree_chunk_ids(ref.id, 999999) == []


# ---------------------------------------------------------------------------
# Store: toc_digest
# ---------------------------------------------------------------------------


def test_toc_digest_unaffected_by_paragraph_edit_but_changed_by_heading_edit(
    store: Store,
) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="tocdig", title="T", project_ref_id=proj
    )
    section_a = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    para = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="body",
        at={"into": section_a.handle},
    )[0]

    digest0 = store.drafts.toc_digest(ref.id)

    # a paragraph body edit never touches the digest
    store.drafts.edit_text(para.handle, "revised body")
    assert store.drafts.toc_digest(ref.id) == digest0

    # renaming a heading does
    store.drafts.edit_text(section_a.handle, "A (renamed)")
    digest1 = store.drafts.toc_digest(ref.id)
    assert digest1 != digest0


def test_toc_digest_changes_on_heading_reorder(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="tocdig-reorder", title="T", project_ref_id=proj
    )
    section_a = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    section_b = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": section_a.handle}
    )[0]

    digest0 = store.drafts.toc_digest(ref.id)

    store.drafts.move_chunk(section_b.handle, {"before": title.handle})
    digest1 = store.drafts.toc_digest(ref.id)
    assert digest1 != digest0
    # the heading set is unchanged, only the order — a same-membership,
    # different-order digest must still differ (the hash is order-sensitive)
    assert {
        c.chunk_id
        for c in store.drafts.reading_order(ref.id)
        if c.chunk_kind == "heading"
    } == {
        title.chunk_id,
        section_a.chunk_id,
        section_b.chunk_id,
    }


# ---------------------------------------------------------------------------
# Store: review_status_for_draft — section_chunk_id + toc entry
# ---------------------------------------------------------------------------


def test_review_status_for_draft_carries_section_chunk_id(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="section-id", title="T", project_ref_id=proj
    )
    section = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Section", at={"after": title.handle}
    )[0]
    para = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="p", at={"into": section.handle}
    )[0]

    rows = {r["chunk_id"]: r for r in store.drafts.review_status_for_draft(ref.id)}
    assert rows[para.chunk_id]["section_chunk_id"] == section.chunk_id
    # a top-level heading has no enclosing heading of its own
    assert rows[section.chunk_id]["section_chunk_id"] is None
    assert rows[title.chunk_id]["section_chunk_id"] is None


def test_review_status_for_draft_toc_entry_pins_to_digest_not_content_sha(
    store: Store,
) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="toc-entry", title="T", project_ref_id=proj
    )
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )

    # never reviewed yet — a synthetic dirty 'toc' row on the first chunk
    rows = [
        r for r in store.drafts.review_status_for_draft(ref.id) if r["checker"] == "toc"
    ]
    assert len(rows) == 1
    assert rows[0]["chunk_id"] == title.chunk_id
    assert rows[0]["dirty"] is True

    # approve at the current digest — pinned to the digest, not this
    # chunk's own content_sha (which never changed)
    digest = store.drafts.toc_digest(ref.id)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunk_review (chunk_id, checker, approved_sha, verdict) "
            "VALUES (%s, 'toc', %s, 'approved')",
            (title.chunk_id, digest),
        )
        conn.commit()

    rows = [
        r for r in store.drafts.review_status_for_draft(ref.id) if r["checker"] == "toc"
    ]
    assert rows[0]["dirty"] is False
    assert rows[0]["approved_sha"] == digest

    # editing the title's OWN text (not a section rename) still changes the
    # digest, since the title itself is a heading counted in it
    store.drafts.edit_text(title.handle, "T (renamed)")
    rows = [
        r for r in store.drafts.review_status_for_draft(ref.id) if r["checker"] == "toc"
    ]
    assert rows[0]["dirty"] is True


# ---------------------------------------------------------------------------
# Store: review_rollup_for_draft
# ---------------------------------------------------------------------------


def test_review_rollup_for_draft_counts_prose_chunks_only(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="rollup", title="T", project_ref_id=proj
    )
    section = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Section", at={"after": title.handle}
    )[0]
    paras = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="p1\n\np2\n\np3",
        at={"into": section.handle},
    )
    assert len(paras) == 3

    rollup = store.drafts.review_rollup_for_draft(ref.id)
    assert rollup == {"done": 0, "total": 3}  # 2 headings excluded from denominator

    for p in paras:
        store.drafts.record_review(p.chunk_id, "human")

    rollup = store.drafts.review_rollup_for_draft(ref.id)
    assert rollup == {"done": 3, "total": 3}


# ---------------------------------------------------------------------------
# Handler: edit(kind='draft', review=…)
# ---------------------------------------------------------------------------


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def test_edit_review_records(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.live_store)
    draft.put(id="rv", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="rv")
    assert ref is not None
    order = hub.live_store.drafts.reading_order(ref.id)
    title = order[0]
    r = draft.edit(id=f"¶{title.handle}", review="human")
    assert "human" in r.body and title.dc in r.body

    statuses = hub.live_store.drafts.review_status_for_chunk(title.chunk_id)
    assert statuses[0]["checker"] == "human"
    assert statuses[0]["verdict"] == "approved"

    # explicit verdict
    r2 = draft.edit(id=f"¶{title.handle}", review="cites", verdict="fail")
    assert "fail" in r2.body
    statuses = hub.live_store.drafts.review_status_for_chunk(title.chunk_id)
    by_checker = {s["checker"]: s for s in statuses}
    assert by_checker["cites"]["verdict"] == "fail"


def test_edit_review_dry_run_rejected(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.live_store)
    draft.put(id="rv", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="rv")
    assert ref is not None
    order = hub.live_store.drafts.reading_order(ref.id)
    title = order[0]
    with pytest.raises(BadInput):
        draft.edit(id=f"¶{title.handle}", review="human", dry_run=True)


# ---------------------------------------------------------------------------
# Handler: get(view='review' / 'review-diff')
# ---------------------------------------------------------------------------


def test_view_review_renders_dirty_for_human(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.live_store)
    draft.put(id="rv", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="rv")
    assert ref is not None
    ref_id = ref.id
    title = hub.live_store.drafts.reading_order(ref_id)[0]

    # never reviewed → shows up as dirty-for-human
    out = draft.get(id="rv", view="review").body
    assert "dirty-for-human" in out
    assert title.dc in out

    # approve it → clears
    draft.edit(id=f"¶{title.handle}", review="human")
    out = draft.get(id="rv", view="review").body
    assert "nothing dirty-for-human" in out

    # edit again → dirty-for-human again
    hub.live_store.drafts.edit_text(title.handle, "T (revised)")
    out = draft.get(id="rv", view="review").body
    assert "dirty-for-human" in out and "nothing dirty-for-human" not in out


def test_view_review_diff_shows_change_since_human_approval(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _project(hub.live_store)
    draft.put(id="rv", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="rv")
    assert ref is not None
    ref_id = ref.id
    title = hub.live_store.drafts.reading_order(ref_id)[0]

    out = draft.get(id=title.dc, view="review-diff").body
    assert "never approved" in out

    draft.edit(id=f"¶{title.handle}", review="human")
    hub.live_store.drafts.edit_text(title.handle, "T (revised)")
    out = draft.get(id=title.dc, view="review-diff").body
    assert "-T" in out
    assert "+T (revised)" in out


def test_unknown_draft_view_still_lists_review(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.live_store)
    draft.put(id="rv", title="T", project=proj)
    with pytest.raises(BadInput) as ei:
        draft.get(id="rv", view="bogus")
    assert "view='review'" in (ei.value.next or "")


# ---------------------------------------------------------------------------
# Wire-level: edit(kind='draft', review=…) through precis.tools.core
# ---------------------------------------------------------------------------
#
# Regression: `review=`/`verdict=` were missing from the ``edit()`` tool
# function's own parameter list + payload in ``precis.tools.core`` (a real
# MCP client's kwarg silently dropped before reaching the handler), even
# though every other test here calls ``DraftHandler.edit()`` directly and so
# never exercised that layer. Mirrors ``test_mermaid.py``'s
# ``test_mcp_edit_tool_persists_vocab_and_notes`` pattern.


def test_mcp_edit_tool_records_review(
    monkeypatch, hub: Hub, runtime_with_store
) -> None:
    import precis.tools.core as core

    monkeypatch.setattr(core, "_runtime", runtime_with_store)

    proj = _project(hub.live_store)
    draft = DraftHandler(hub=hub)
    draft.put(id="rv-wire", title="T", project=proj)
    ref = hub.live_store.get_ref(kind="draft", id="rv-wire")
    assert ref is not None
    ref_id = ref.id
    title = hub.live_store.drafts.reading_order(ref_id)[0]

    assert title.chunk_id in {
        r["chunk_id"]
        for r in hub.live_store.drafts.chunks_requiring_review(ref_id, "human")
    }

    out = core.edit(kind="draft", id=title.dc, review="human")
    assert isinstance(out, str) and "human" in out

    assert title.chunk_id not in {
        r["chunk_id"]
        for r in hub.live_store.drafts.chunks_requiring_review(ref_id, "human")
    }
