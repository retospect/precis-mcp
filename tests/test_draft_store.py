"""Draft store ops — create / add / reading-order."""

from __future__ import annotations

import psycopg
import pytest

from precis.errors import BadInput
from precis.store.store import Store


def _project(store: Store) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title="Nanotrans project")
    return ref.id


def _order(store: Store, ref_id: int) -> list[tuple[str, str, int]]:
    return [(c.chunk_kind, c.text, c.depth) for c in store.drafts.reading_order(ref_id)]


def test_create_draft_is_never_empty_and_linked(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="nanotrans",
        title="Nanoscale Transistors",
        project_ref_id=proj,
    )
    # born with exactly one chunk: the title heading
    assert title.chunk_kind == "heading"
    assert title.text == "Nanoscale Transistors"
    assert _order(store, ref.id) == [("heading", "Nanoscale Transistors", 0)]
    # draft-of link to the project
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM links WHERE src_ref_id=%s AND dst_ref_id=%s "
            "AND relation='draft-of'",
            (ref.id, proj),
        ).fetchone()
    assert row is not None


def test_one_draft_per_project(store: Store) -> None:
    proj = _project(store)
    store.drafts.create_draft(name="d1", title="One", project_ref_id=proj)
    with pytest.raises(ValueError, match="already has a draft"):
        store.drafts.create_draft(name="d2", title="Two", project_ref_id=proj)


def test_universal_chunk_resolves_any_chunk_by_handle(store: Store) -> None:
    from precis.utils import handle_registry

    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="uc", title="UC Title", project_ref_id=proj
    )
    h = handle_registry.format_handle("draft", title.chunk_id, chunk=True)  # dc<id>
    uc = store.drafts.universal_chunk(h)
    assert uc is not None
    assert uc["kind"] == "draft"
    assert uc["ref_id"] == ref.id
    assert uc["chunk_kind"] == "heading"
    assert uc["text"] == "UC Title"
    # a record (non-chunk) handle → None; a dangling chunk id → None
    assert store.drafts.universal_chunk("me5") is None
    assert store.drafts.universal_chunk("dc999999999") is None


def test_soft_delete_draft_is_atomic_and_recoverable(store: Store) -> None:
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="doomed", title="Doomed", project_ref_id=proj
    )
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="body one\n\nbody two"
    )
    n_live = len(store.drafts.reading_order(ref.id))
    assert n_live >= 3  # title heading + two paragraphs

    retired = store.drafts.soft_delete_draft(ref.id)
    assert retired == n_live
    # ref is soft-deleted (hidden from the kind lookup) and all chunks retired
    assert store.get_ref(kind="draft", id=ref.id) is None
    assert store.drafts.reading_order(ref.id) == []
    with store.pool.connection() as conn:
        dref = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id=%s", (ref.id,)
        ).fetchone()
        live_chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id=%s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert dref is not None
    assert live_chunks is not None
    assert dref[0] is not None
    assert live_chunks[0] == 0

    # idempotent / guards a non-live draft
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        store.drafts.soft_delete_draft(ref.id)


def test_add_chunks_positions_and_hierarchy(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="nt", title="Title", project_ref_id=proj
    )

    # a section heading after the title
    intro = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Introduction",
        at={"after": title.handle},
    )[0]
    # two paragraphs inside it (one put, split at the blank line)
    paras = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="Para A.\n\nPara B.",
        at={"into": intro.handle, "last": True},
    )
    assert len(paras) == 2

    assert _order(store, ref.id) == [
        ("heading", "Title", 0),
        ("heading", "Introduction", 0),
        ("paragraph", "Para A.", 1),
        ("paragraph", "Para B.", 1),
    ]


def test_add_chunks_unknown_anchor_raises_notfound(store: Store) -> None:
    """A typo'd / stale `at=` anchor surfaces as a typed NotFound.

    Before the fix these two paths raised a raw ``ValueError`` that the
    handler rendered as the opaque "internal error in put: ValueError"
    fallback — the same gripe #45083 class as the edit/move/retire ops.
    """
    from precis.errors import NotFound

    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="nf", title="Title", project_ref_id=proj
    )

    with pytest.raises(NotFound, match="unknown chunk handle"):
        store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="orphan",
            at={"after": "¶missing"},
        )
    with pytest.raises(NotFound, match="unknown parent handle"):
        store.drafts.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="orphan",
            at={"into": "¶missing", "last": True},
        )


def _list_fixture(store: Store) -> tuple[int, str]:
    """A draft with a ulist container + two items under the title. Returns
    ``(ref_id, container_handle)``."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="lst", title="Title", project_ref_id=proj
    )
    ul = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="ulist", text="", at={"after": title.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="item",
        text="alpha\n\nbeta",
        at={"into": ul.handle, "last": True},
    )
    return ref.id, ul.handle


def test_set_list_kind_flips_container_in_place(store: Store) -> None:
    ref_id, ul = _list_fixture(store)
    store.drafts.set_list_kind(ul, "olist")
    kinds = [k for k, _, _ in _order(store, ref_id)]
    assert "olist" in kinds and "ulist" not in kinds
    # items untouched (still two items under the container)
    assert kinds.count("item") == 2


def test_set_list_kind_normal_dissolves_to_paragraphs(store: Store) -> None:
    ref_id, ul = _list_fixture(store)
    store.drafts.set_list_kind(ul, "normal")
    order = _order(store, ref_id)
    # the container is gone; its items are now top-level paragraphs
    assert [(k, t) for k, t, _ in order] == [
        ("heading", "Title"),
        ("paragraph", "alpha"),
        ("paragraph", "beta"),
    ]
    # promoted to the title's depth (the container's old parent = root)
    assert all(d == 0 for _, _, d in order)


def test_set_list_kind_rejects_non_list(store: Store) -> None:
    from precis.errors import BadInput

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="x", title="T", project_ref_id=proj)
    with pytest.raises(BadInput):
        store.drafts.set_list_kind(title.handle, "olist")


def test_insert_before_reorders(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="nt", title="Title", project_ref_id=proj
    )
    b = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": title.handle}
    )[0]
    # insert A before B → order Title, A, B
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"before": b.handle}
    )
    assert [t for _, t, _ in _order(store, ref.id)] == ["Title", "A", "B"]


def test_handles_are_unique_and_addressable(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(
        name="nt", title="Title", project_ref_id=proj
    )
    extra = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="x", at={"after": title.handle}
    )[0]
    assert title.handle != extra.handle
    # round-trip by handle, with and without the ¶ sigil
    fetched = store.drafts.get_draft_chunk(extra.handle)
    assert fetched is not None
    assert fetched.text == "x"
    fetched_sigil = store.drafts.get_draft_chunk("¶" + extra.handle)
    assert fetched_sigil is not None
    assert fetched_sigil.chunk_id == extra.chunk_id


def _events(store: Store, chunk_id: int) -> list[tuple[str, str | None]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT event_kind, prev_text FROM chunk_events "
            "WHERE chunk_id=%s ORDER BY event_id",
            (chunk_id,),
        ).fetchall()
    return [(r[0], r[1]) for r in rows]


def test_edit_text_in_place(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="old", at={"after": title.handle}
    )[0]
    upd = store.drafts.edit_text(p.handle, "new text")
    assert upd is not None
    assert upd.text == "new text"
    assert upd.handle == p.handle  # handle survives
    # created + edited(prev_text='old')
    assert _events(store, p.chunk_id) == [("created", None), ("edited", "old")]


def test_edit_text_stale_base_sha_raises(store: Store) -> None:
    """gr176088: the caller reads a chunk (capturing its content_sha), a
    concurrent writer then edits the chunk (simulated here by a force
    edit_text with no base_sha), and the caller's now-stale base_sha must
    raise BadInput rather than silently clobbering the concurrent write."""
    from precis.store._draft_ops import content_sha

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="v1", at={"after": title.handle}
    )[0]
    stale = content_sha(p.text)  # what our caller "saw" on read
    store.drafts.edit_text(p.handle, "v2")  # a concurrent writer lands in between
    with pytest.raises(BadInput, match="changed since you read"):
        store.drafts.edit_text(p.handle, "v3", base_sha=stale)
    # the concurrent writer's text survives untouched — no clobber
    survived = store.drafts.get_draft_chunk(p.handle)
    assert survived is not None
    assert survived.text == "v2"


def test_merge_prev_block_happy_path(store: Store) -> None:
    """Backspace-merge: text appends onto ``prev`` and the merged-away chunk
    retires, both in one call (gr176088 part 2b)."""
    from precis.store._draft_ops import content_sha

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Hello", at={"after": title.handle}
    )[0]
    # add_chunks trims a bare block's trailing space; edit_text preserves it
    store.drafts.edit_text(p1.handle, "Hello ")
    p2 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + p1.handle},
    )[0]
    merged = store.drafts.merge_prev_block(
        p2.handle, p1.handle, "world", base_sha=content_sha("Hello ")
    )
    assert merged is not None
    assert merged.text == "Hello world"
    retired = store.drafts.get_draft_chunk(p2.handle)
    assert retired is not None and retired.retired  # retired, not deleted


def test_merge_prev_block_stale_base_sha_raises_and_leaves_both_unchanged(
    store: Store,
) -> None:
    """gr176088 part 2b: a concurrent edit to ``prev`` between the caller's
    read and the merge must raise BadInput, leaving BOTH the retiree and prev
    untouched — no partial write (this would be RED against the prior
    two-op ``retire_chunk`` + ``edit_text`` implementation, which retires
    first and only then discovers the stale edit, orphaning the retire)."""
    from precis.store._draft_ops import content_sha

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Hello ", at={"after": title.handle}
    )[0]
    p2 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + p1.handle},
    )[0]
    stale = content_sha(p1.text or "")  # what the caller "saw" on read
    store.drafts.edit_text(p1.handle, "Hello there ")  # a concurrent writer lands
    with pytest.raises(BadInput, match="changed since you read"):
        store.drafts.merge_prev_block(p2.handle, p1.handle, "world", base_sha=stale)
    # the concurrent writer's text survives — no clobber
    prev_after = store.drafts.get_draft_chunk(p1.handle)
    assert prev_after is not None
    assert prev_after.text == "Hello there "
    # and the retire never happened — p2 is still live, its text intact
    retiree_after = store.drafts.get_draft_chunk(p2.handle)
    assert retiree_after is not None
    assert retiree_after.text == "world"
    assert not retiree_after.retired


def test_merge_prev_block_postlock_race_raises_and_leaves_both_unchanged(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr176088 part 2b, the second race window: a concurrent edit that
    commits while the merge is BLOCKED on the section lock. The merge's
    pre-lock read saw the old text, so its state checks must re-read under
    the lock — comparing base_sha against the stale pre-lock copy would
    pass and silently clobber the concurrent append. Simulated by injecting
    the edit inside ``_lock_sections`` (locks already held → the edit is
    "what committed while we waited")."""
    from precis.store._draft_ops import DraftStore, content_sha

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Hello ", at={"after": title.handle}
    )[0]
    p2 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="world",
        at={"after": "¶" + p1.handle},
    )[0]
    caller_sha = content_sha(p1.text or "")  # fresh at call time — passes pre-lock
    real_lock = DraftStore._lock_sections
    fired = False

    def lock_then_racing_edit(
        self: DraftStore,
        conn: psycopg.Connection,
        ref_id: int,
        *parents: int | None,
    ) -> None:
        nonlocal fired
        real_lock(self, conn, ref_id, *parents)
        if not fired:
            fired = True
            # Commits on its own pooled connection; edit_text takes no
            # section lock, so it lands while the merge txn holds the locks.
            store.drafts.edit_text(p1.handle, "Hello there ")

    monkeypatch.setattr(DraftStore, "_lock_sections", lock_then_racing_edit)
    with pytest.raises(BadInput, match="changed since you read"):
        store.drafts.merge_prev_block(
            p2.handle, p1.handle, "world", base_sha=caller_sha
        )
    prev_after = store.drafts.get_draft_chunk(p1.handle)
    assert prev_after is not None
    assert prev_after.text == "Hello there "  # the racing edit survives
    retiree_after = store.drafts.get_draft_chunk(p2.handle)
    assert retiree_after is not None
    assert retiree_after.text == "world"
    assert not retiree_after.retired


def test_merge_prev_block_childless_guard_raises_and_leaves_both_unchanged(
    store: Store,
) -> None:
    """The retiree must still be a childless leaf: a child added concurrently
    (or just present) raises BadInput rather than partial-merging a
    subtree — mirrors ``retire_chunk``'s own guard, now checked inside the
    same transaction as the append."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="Hello", at={"after": title.handle}
    )[0]
    heading = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="Sec", at={"after": "¶" + p1.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="kid", at={"into": heading.handle}
    )
    with pytest.raises(BadInput, match="requires"):
        store.drafts.merge_prev_block(heading.handle, p1.handle, "Sec")
    unchanged = store.drafts.get_draft_chunk(p1.handle)
    assert unchanged is not None and unchanged.text == "Hello"
    heading_after = store.drafts.get_draft_chunk(heading.handle)
    assert heading_after is not None and not heading_after.retired


def test_edit_text_invalidates_embedding_and_summary_cascade(store: Store) -> None:
    """td48771 Phase 3: unlike markdown/plaintext/tex (whose re-ingest path
    DELETEs + re-INSERTs chunks, cascading away stale derived rows),
    ``edit_text`` mutates the chunk row in place — the invariant it must
    hold instead is that ``chunks.content_sha`` bumps so the embed/summarize
    workers' staleness check (``chunk_embeddings.content_sha IS NOT
    DISTINCT FROM chunks.content_sha`` — see embed.py's
    ``unembedded_chunk_count``) sees a mismatch and re-derives."""
    from precis.store._draft_ops import content_sha

    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="v1", at={"after": title.handle}
    )[0]
    old_sha = content_sha(p.text)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT name FROM embedders WHERE is_default = TRUE LIMIT 1"
        ).fetchone()
        assert row is not None
        embedder_name = row[0]
        conn.execute(
            "INSERT INTO chunk_embeddings "
            "(chunk_id, embedder, vector, status, content_sha) "
            "VALUES (%s, %s, %s, 'ok', %s)",
            (p.chunk_id, embedder_name, [0.0] * store.embedding_dim(), old_sha),
        )
        conn.execute(
            "INSERT INTO chunk_summaries "
            "(chunk_id, summarizer, text, status, content_sha) "
            "VALUES (%s, 'llm-v1', 'old summary', 'ok', %s)",
            (p.chunk_id, old_sha),
        )
        conn.commit()

    store.drafts.edit_text(p.handle, "v2")

    with store.pool.connection() as conn:
        chunks_row = conn.execute(
            "SELECT content_sha FROM chunks WHERE chunk_id = %s", (p.chunk_id,)
        ).fetchone()
        emb_row = conn.execute(
            "SELECT content_sha FROM chunk_embeddings WHERE chunk_id = %s",
            (p.chunk_id,),
        ).fetchone()
        summ_row = conn.execute(
            "SELECT content_sha FROM chunk_summaries WHERE chunk_id = %s",
            (p.chunk_id,),
        ).fetchone()
    assert chunks_row is not None
    assert emb_row is not None
    assert summ_row is not None
    new_sha, emb_sha, summ_sha = chunks_row[0], emb_row[0], summ_row[0]
    assert new_sha != old_sha
    # The chunk row is still in place (in-place edit, not delete+insert —
    # the computed chunks "computed chunks" model), but its content_sha has moved
    # on from the embedding/summary rows still on file — the derived rows
    # are now stale and the worker will re-derive them.
    assert emb_sha == old_sha
    assert emb_sha != new_sha
    assert summ_sha == old_sha
    assert summ_sha != new_sha


def test_move_reorder_and_reparent(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    a = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    # reorder: B before A → T, B, A
    store.drafts.move_chunk(b.handle, {"before": a.handle})
    assert [t for _, t, _ in _order(store, ref.id)] == ["T", "B", "A"]
    assert _events(store, b.chunk_id)[-1][0] == "moved"
    # reparent: move A into B → A becomes B's child
    store.drafts.move_chunk(a.handle, {"into": b.handle, "last": True})
    assert _order(store, ref.id) == [
        ("heading", "T", 0),
        ("heading", "B", 0),
        ("heading", "A", 1),
    ]
    assert _events(store, a.chunk_id)[-1][0] == "reparented"


def test_move_cycle_guard(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    child = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )[0]
    with pytest.raises(BadInput, match="under itself or its own subtree"):
        store.drafts.move_chunk(h.handle, {"into": child.handle})


def test_retire_leaf_and_last_chunk_guard(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="x", at={"after": title.handle}
    )[0]
    store.drafts.retire_chunk(p.handle)
    assert [t for _, t, _ in _order(store, ref.id)] == ["T"]
    # title is now the last live chunk — cannot retire it
    with pytest.raises(BadInput, match="last live chunk"):
        store.drafts.retire_chunk(title.handle)


def test_retire_heading_requires_mode(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )
    with pytest.raises(BadInput, match="requires"):
        store.drafts.retire_chunk(h.handle)


def test_retire_cascade_and_promote(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="c2",
        at={"into": h.handle, "last": True},
    )
    # promote: H gone, c1/c2 lifted to root (depth 0) in H's slot
    store.drafts.retire_chunk(h.handle, mode="promote")
    assert _order(store, ref.id) == [
        ("heading", "T", 0),
        ("paragraph", "c1", 0),
        ("paragraph", "c2", 0),
    ]


def test_retire_cascade_deletes_subtree(store: Store) -> None:
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.drafts.retire_chunk(h.handle, mode="cascade")
    assert [t for _, t, _ in _order(store, ref.id)] == ["T"]


# --- placement coverage: one test per `at` / `move` variant -------------


def _titled(store: Store):
    proj = _project(store)
    return store.drafts.create_draft(name="nt", title="T", project_ref_id=proj)


def _texts(store: Store, ref_id: int) -> list[str]:
    return [c.text for c in store.drafts.reading_order(ref_id)]


def _add(store, ref_id, text, **at):
    return store.drafts.add_chunks(
        ref_id=ref_id, chunk_kind="heading", text=text, at=at
    )[0]


def test_at_first_at_root(store: Store) -> None:
    ref, _ = _titled(store)
    _add(store, ref.id, "X", first=True)
    assert _texts(store, ref.id) == ["X", "T"]


def test_at_last_at_root(store: Store) -> None:
    ref, _ = _titled(store)
    _add(store, ref.id, "X", last=True)
    assert _texts(store, ref.id) == ["T", "X"]


def test_at_after_sibling(store: Store) -> None:
    ref, title = _titled(store)
    a = _add(store, ref.id, "A", after=title.handle)
    _add(store, ref.id, "B", after=a.handle)
    assert _texts(store, ref.id) == ["T", "A", "B"]


def test_at_before_sibling(store: Store) -> None:
    ref, title = _titled(store)
    a = _add(store, ref.id, "A", after=title.handle)
    _add(store, ref.id, "Z", before=a.handle)
    assert _texts(store, ref.id) == ["T", "Z", "A"]


def test_at_into_last(store: Store) -> None:
    ref, title = _titled(store)
    h = _add(store, ref.id, "H", after=title.handle)
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="c2",
        at={"into": h.handle, "last": True},
    )
    assert _texts(store, ref.id) == ["T", "H", "c1", "c2"]


def test_at_into_first(store: Store) -> None:
    ref, title = _titled(store)
    h = _add(store, ref.id, "H", after=title.handle)
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="c0",
        at={"into": h.handle, "first": True},
    )
    assert _texts(store, ref.id) == ["T", "H", "c0", "c1"]


def test_move_after_sibling(store: Store) -> None:
    ref, title = _titled(store)
    a = _add(store, ref.id, "A", after=title.handle)
    b = _add(store, ref.id, "B", after=a.handle)
    store.drafts.move_chunk(a.handle, {"after": b.handle})  # T, B, A
    assert _texts(store, ref.id) == ["T", "B", "A"]


def test_move_into_first(store: Store) -> None:
    ref, title = _titled(store)
    h = _add(store, ref.id, "H", after=title.handle)
    c = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )[0]
    x = _add(store, ref.id, "X", after=h.handle)
    store.drafts.move_chunk(x.handle, {"into": h.handle, "first": True})
    assert _texts(store, ref.id) == ["T", "H", "X", "c"]
    assert c.handle  # silence unused


def test_move_to_root_first(store: Store) -> None:
    ref, title = _titled(store)
    a = _add(store, ref.id, "A", after=title.handle)
    store.drafts.move_chunk(a.handle, {"first": True})  # A, T
    assert _texts(store, ref.id) == ["A", "T"]


def test_live_paper_cites_splits_local_vs_external(store: Store) -> None:
    """The draft-reader colouring signal: only citation tokens that resolve
    to a live paper we hold come back (slug cite_key, ``pc`` chunk handle,
    ``pa`` record handle); unknown, non-paper, and soft-deleted targets are
    external. Mirrors ``§slug`` / ``[pc..]`` / ``[pa..]`` inline forms."""
    from precis.store.types import BlockInsert
    from precis.utils import handle_registry

    paper = store.insert_ref(kind="paper", slug="miller23", title="Paper")
    store.blocks.insert_blocks(
        paper.id, [BlockInsert(pos=0, text="We measured 12% FE.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_row = conn.execute(
            "SELECT chunk_id FROM chunks WHERE ref_id=%s ORDER BY ord LIMIT 1",
            (paper.id,),
        ).fetchone()
        assert chunk_row is not None
        chunk_id = int(chunk_row[0])
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)  # pc<id>
    pa = handle_registry.format_handle("paper", paper.id)  # pa<id> record
    # a non-paper record (memory) whose handle must NOT count as a paper cite
    mem = store.insert_ref(kind="memory", slug=None, title="note").id
    me = handle_registry.format_handle("memory", mem)

    live = store.drafts.live_paper_cites(
        {pc, pa, me, "pc999999"}, {"miller23", "ghost404"}
    )
    assert live == {pc, pa, "miller23"}  # the paper's slug + both live handles
    assert "ghost404" not in live and "pc999999" not in live and me not in live

    # soft-deleting the paper flips every one of its tokens to external
    store.soft_delete_ref(paper.id)
    assert store.drafts.live_paper_cites({pc, pa}, {"miller23"}) == set()


# ---------------------------------------------------------------------------
# Retired-chunk / "ghost" handling (gripe 49153)
#
# A retired draft chunk keeps its tsv/embedding + pos. Two faults it caused:
#   A. search still surfaced it (handle returned yet uneditable);
#   B. inserting/moving relative to it raised StopIteration.
# ---------------------------------------------------------------------------


def _kinds_texts(store: Store, ref_id: int) -> list[tuple[str, str]]:
    return [(k, t) for k, t, _d in _order(store, ref_id)]


def test_search_excludes_retired_draft_chunk(store: Store) -> None:
    """Fix A: a retired draft chunk must drop out of search (its live sibling
    with the same term stays)."""
    proj = _project(store)
    ref, _title = store.drafts.create_draft(
        name="gh", title="Ghost", project_ref_id=proj
    )
    p1 = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="xenophilus alpha"
    )[0]
    p2 = store.drafts.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="xenophilus beta",
        at={"after": p1.handle},
    )[0]
    texts = {
        b.text
        for b, _r, _s in store.blocks.search_blocks_lexical(
            q="xenophilus", kind="draft"
        )
    }
    assert {"xenophilus alpha", "xenophilus beta"} <= texts

    store.drafts.retire_chunk(
        p1.handle
    )  # p1 now retired (p2 keeps the draft non-empty)
    texts2 = {
        b.text
        for b, _r, _s in store.blocks.search_blocks_lexical(
            q="xenophilus", kind="draft"
        )
    }
    assert "xenophilus alpha" not in texts2  # the ghost is gone
    assert "xenophilus beta" in texts2  # its live sibling stays
    assert p2  # silence unused-var lint


def test_add_after_retired_anchor_recovers(store: Store) -> None:
    """Fix B: `add(after=<retired>)` recovers into the ghost slot instead of
    raising StopIteration."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="ga", title="T", project_ref_id=proj)
    a = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="C", at={"after": b.handle}
    )
    store.drafts.retire_chunk(b.handle)  # order now T, A, [B ghost], C

    store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="X", at={"after": b.handle}
    )
    assert _kinds_texts(store, ref.id) == [
        ("heading", "T"),
        ("heading", "A"),
        ("heading", "X"),  # landed in B's ghost slot, between A and C
        ("heading", "C"),
    ]


def test_move_relative_to_retired_anchor_recovers(store: Store) -> None:
    """Fix B (move path): `move(before=<retired>)` recovers rather than
    raising StopIteration."""
    proj = _project(store)
    ref, title = store.drafts.create_draft(name="gmv", title="T", project_ref_id=proj)
    a = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    c = store.drafts.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="C", at={"after": b.handle}
    )[0]
    store.drafts.retire_chunk(b.handle)  # order now T, A, [B ghost], C

    store.drafts.move_chunk(c.handle, {"before": b.handle})  # must not raise
    assert _kinds_texts(store, ref.id) == [
        ("heading", "T"),
        ("heading", "A"),
        ("heading", "C"),
    ]


def test_job_fail_reason_falls_back_to_job_event(store: Store) -> None:
    """A job with only a ``job_event`` chunk (the common case — most
    plugin dispatchers write ``job_summary`` only on their SUCCESS tail,
    so a failed run never gets one) now yields a reason instead of
    ``None``: the first line of the latest ``job_event`` chunk. The
    event's multi-line ``--- tail ---`` block of raw subprocess output
    must be dropped, not surfaced in the UI reason."""
    from precis.store.types import BlockInsert

    job = store.insert_ref(kind="job", slug=None, title="attempt", meta={})
    store.blocks.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0,
                text=(
                    "autocatpath_seed: run failed: child process exited "
                    "without writing result.json\n"
                    "--- tail ---\n"
                    "Traceback (most recent call last):\n"
                    "  ...lots of raw subprocess output..."
                ),
                meta={"chunk_kind": "job_event"},
            )
        ],
    )
    reason = store.drafts.job_fail_reason(job.id)
    assert reason == (
        "autocatpath_seed: run failed: child process exited without writing result.json"
    )
    assert "tail" not in reason
    assert "Traceback" not in reason


def test_job_fail_reason_prefers_job_summary_over_job_event(store: Store) -> None:
    """A ``job_summary`` chunk still wins when both exist — ``job_event``
    is only a fallback for a job that never got a summary."""
    from precis.store.types import BlockInsert

    job = store.insert_ref(kind="job", slug=None, title="attempt", meta={})
    store.blocks.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0,
                text="runner: killed at wall-clock deadline",
                meta={"chunk_kind": "job_event"},
            ),
            BlockInsert(
                pos=1,
                text="API Error: unable to respond",
                meta={"chunk_kind": "job_summary"},
            ),
        ],
    )
    assert store.drafts.job_fail_reason(job.id) == "API Error: unable to respond"


def test_job_fail_reason_picks_latest_job_event(store: Store) -> None:
    """With several ``job_event`` chunks and no ``job_summary``, the
    LATEST one (highest ``ord``) is used, not the first."""
    from precis.store.types import BlockInsert

    job = store.insert_ref(kind="job", slug=None, title="attempt", meta={})
    store.blocks.insert_blocks(
        job.id,
        [
            BlockInsert(
                pos=0, text="first attempt died", meta={"chunk_kind": "job_event"}
            ),
            BlockInsert(
                pos=1, text="second attempt died", meta={"chunk_kind": "job_event"}
            ),
        ],
    )
    assert store.drafts.job_fail_reason(job.id) == "second attempt died"


def test_job_fail_reason_none_when_no_chunks(store: Store) -> None:
    """No job_summary and no job_event chunk → still None, not an error."""
    job = store.insert_ref(kind="job", slug=None, title="attempt", meta={})
    assert store.drafts.job_fail_reason(job.id) is None
