"""The `chunk_review` memoized approval ledger (paper-writing pipeline rung
3, docs/design/paper-writing-pipeline.md §"Review — the memoized approval
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
    ref, title = store.create_draft(name="rv", title="T", project_ref_id=proj)
    p = store.add_chunks(
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
    sha1 = store.record_review(p.chunk_id, "human", verdict="approved")
    assert sha1 == content_sha("original text")

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT approved_sha, verdict FROM chunk_review "
            "WHERE chunk_id=%s AND checker=%s",
            (p.chunk_id, "human"),
        ).fetchone()
    assert row == (sha1, "approved")

    # a second call on the same (chunk, checker) overwrites — not a new row.
    store.edit_text(p.handle, "changed text")
    sha2 = store.record_review(p.chunk_id, "human", verdict="changes")
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
    store.insert_blocks(ref.id, [BlockInsert(pos=0, text="body text", embedding=None)])
    with store.pool.connection() as conn:
        chunk_id = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id=%s ORDER BY ord LIMIT 1",
            (ref.id,),
        ).fetchone()[0]

    with pytest.raises(BadInput, match="content_sha"):
        store.record_review(chunk_id, "human")


# ---------------------------------------------------------------------------
# Store: chunks_requiring_review
# ---------------------------------------------------------------------------


def test_chunks_requiring_review_never_reviewed_is_flagged(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    dirty = store.chunks_requiring_review(ref_id, "human")
    assert p.chunk_id in {r["chunk_id"] for r in dirty}


def test_chunks_requiring_review_clears_after_record_and_reflags_after_edit(
    store: Store,
) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.record_review(p.chunk_id, "human")
    dirty = {r["chunk_id"] for r in store.chunks_requiring_review(ref_id, "human")}
    assert p.chunk_id not in dirty

    # a weave bumps content_sha → dirty again for every checker (spec: "the
    # chunk goes dirty for every checker")
    store.edit_text(p.handle, "edited text bumps the sha")
    dirty = {r["chunk_id"] for r in store.chunks_requiring_review(ref_id, "human")}
    assert p.chunk_id in dirty

    # re-recording at the new sha clears it again
    store.record_review(p.chunk_id, "human")
    dirty = {r["chunk_id"] for r in store.chunks_requiring_review(ref_id, "human")}
    assert p.chunk_id not in dirty


def test_chunks_requiring_review_scoped_per_checker(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.record_review(p.chunk_id, "human")
    # 'human' is clean, but 'cites' has never reviewed this chunk
    dirty_cites = {
        r["chunk_id"] for r in store.chunks_requiring_review(ref_id, "cites")
    }
    assert p.chunk_id in dirty_cites
    dirty_human = {
        r["chunk_id"] for r in store.chunks_requiring_review(ref_id, "human")
    }
    assert p.chunk_id not in dirty_human


# ---------------------------------------------------------------------------
# Store: review_status_for_chunk
# ---------------------------------------------------------------------------


def test_review_status_for_chunk_dirty_bit(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    store.record_review(p.chunk_id, "human")
    statuses = store.review_status_for_chunk(p.chunk_id)
    assert len(statuses) == 1
    assert statuses[0]["checker"] == "human"
    assert statuses[0]["dirty"] is False

    store.edit_text(p.handle, "now different")
    statuses = store.review_status_for_chunk(p.chunk_id)
    assert statuses[0]["dirty"] is True


# ---------------------------------------------------------------------------
# Store: review_diff_since
# ---------------------------------------------------------------------------


def test_review_diff_since_produces_diff_after_edit(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    since_sha = store.record_review(p.chunk_id, "human")
    store.edit_text(p.handle, "brand new text")

    diff = store.review_diff_since(p.chunk_id, since_sha)
    assert "-original text" in diff
    assert "+brand new text" in diff


def test_review_diff_since_empty_when_unchanged(store: Store) -> None:
    ref_id, p = _draft_with_paragraph(store)
    sha = store.record_review(p.chunk_id, "human")
    assert store.review_diff_since(p.chunk_id, sha) == ""


# ---------------------------------------------------------------------------
# Handler: edit(kind='draft', review=…)
# ---------------------------------------------------------------------------


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def test_edit_review_records(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.store)
    draft.put(id="rv", title="T", project=proj)
    order = hub.store.reading_order(hub.store.get_ref(kind="draft", id="rv").id)
    title = order[0]
    r = draft.edit(id=f"¶{title.handle}", review="human")
    assert "human" in r.body and title.dc in r.body

    statuses = hub.store.review_status_for_chunk(title.chunk_id)
    assert statuses[0]["checker"] == "human"
    assert statuses[0]["verdict"] == "approved"

    # explicit verdict
    r2 = draft.edit(id=f"¶{title.handle}", review="cites", verdict="fail")
    assert "fail" in r2.body
    statuses = hub.store.review_status_for_chunk(title.chunk_id)
    by_checker = {s["checker"]: s for s in statuses}
    assert by_checker["cites"]["verdict"] == "fail"


def test_edit_review_dry_run_rejected(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.store)
    draft.put(id="rv", title="T", project=proj)
    order = hub.store.reading_order(hub.store.get_ref(kind="draft", id="rv").id)
    title = order[0]
    with pytest.raises(BadInput):
        draft.edit(id=f"¶{title.handle}", review="human", dry_run=True)


# ---------------------------------------------------------------------------
# Handler: get(view='review' / 'review-diff')
# ---------------------------------------------------------------------------


def test_view_review_renders_dirty_for_human(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.store)
    draft.put(id="rv", title="T", project=proj)
    ref_id = hub.store.get_ref(kind="draft", id="rv").id
    title = hub.store.reading_order(ref_id)[0]

    # never reviewed → shows up as dirty-for-human
    out = draft.get(id="rv", view="review").body
    assert "dirty-for-human" in out
    assert title.dc in out

    # approve it → clears
    draft.edit(id=f"¶{title.handle}", review="human")
    out = draft.get(id="rv", view="review").body
    assert "nothing dirty-for-human" in out

    # edit again → dirty-for-human again
    hub.store.edit_text(title.handle, "T (revised)")
    out = draft.get(id="rv", view="review").body
    assert "dirty-for-human" in out and "nothing dirty-for-human" not in out


def test_view_review_diff_shows_change_since_human_approval(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _project(hub.store)
    draft.put(id="rv", title="T", project=proj)
    ref_id = hub.store.get_ref(kind="draft", id="rv").id
    title = hub.store.reading_order(ref_id)[0]

    out = draft.get(id=title.dc, view="review-diff").body
    assert "never approved" in out

    draft.edit(id=f"¶{title.handle}", review="human")
    hub.store.edit_text(title.handle, "T (revised)")
    out = draft.get(id=title.dc, view="review-diff").body
    assert "-T" in out
    assert "+T (revised)" in out


def test_unknown_draft_view_still_lists_review(draft: DraftHandler, hub: Hub) -> None:
    proj = _project(hub.store)
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

    proj = _project(hub.store)
    draft = DraftHandler(hub=hub)
    draft.put(id="rv-wire", title="T", project=proj)
    ref_id = hub.store.get_ref(kind="draft", id="rv-wire").id
    title = hub.store.reading_order(ref_id)[0]

    assert title.chunk_id in {
        r["chunk_id"] for r in hub.store.chunks_requiring_review(ref_id, "human")
    }

    out = core.edit(kind="draft", id=title.dc, review="human")
    assert isinstance(out, str) and "human" in out

    assert title.chunk_id not in {
        r["chunk_id"] for r in hub.store.chunks_requiring_review(ref_id, "human")
    }
