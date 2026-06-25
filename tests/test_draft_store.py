"""Draft store ops — create / add / reading-order (ADR 0033, Phase 3b)."""

from __future__ import annotations

import pytest

from precis.store.store import Store


def _project(store: Store) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title="Nanotrans project")
    return ref.id


def _order(store: Store, ref_id: int) -> list[tuple[str, str, int]]:
    return [(c.chunk_kind, c.text, c.depth) for c in store.reading_order(ref_id)]


def test_create_draft_is_never_empty_and_linked(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(
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
    store.create_draft(name="d1", title="One", project_ref_id=proj)
    with pytest.raises(ValueError, match="already has a draft"):
        store.create_draft(name="d2", title="Two", project_ref_id=proj)


def test_universal_chunk_resolves_any_chunk_by_handle(store: Store) -> None:
    from precis.utils import handle_registry

    proj = _project(store)
    ref, title = store.create_draft(name="uc", title="UC Title", project_ref_id=proj)
    h = handle_registry.format_handle("draft", title.chunk_id, chunk=True)  # dc<id>
    uc = store.universal_chunk(h)
    assert uc is not None
    assert uc["kind"] == "draft"
    assert uc["ref_id"] == ref.id
    assert uc["chunk_kind"] == "heading"
    assert uc["text"] == "UC Title"
    # a record (non-chunk) handle → None; a dangling chunk id → None
    assert store.universal_chunk("me5") is None
    assert store.universal_chunk("dc999999999") is None


def test_soft_delete_draft_is_atomic_and_recoverable(store: Store) -> None:
    proj = _project(store)
    ref, _title = store.create_draft(name="doomed", title="Doomed", project_ref_id=proj)
    store.add_chunks(ref_id=ref.id, chunk_kind="paragraph", text="body one\n\nbody two")
    n_live = len(store.reading_order(ref.id))
    assert n_live >= 3  # title heading + two paragraphs

    retired = store.soft_delete_draft(ref.id)
    assert retired == n_live
    # ref is soft-deleted (hidden from the kind lookup) and all chunks retired
    assert store.get_ref(kind="draft", id=ref.id) is None
    assert store.reading_order(ref.id) == []
    with store.pool.connection() as conn:
        dref = conn.execute(
            "SELECT deleted_at FROM refs WHERE ref_id=%s", (ref.id,)
        ).fetchone()
        live_chunks = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id=%s AND retired_at IS NULL",
            (ref.id,),
        ).fetchone()
    assert dref[0] is not None
    assert live_chunks[0] == 0

    # idempotent / guards a non-live draft
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        store.soft_delete_draft(ref.id)


def test_add_chunks_positions_and_hierarchy(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="Title", project_ref_id=proj)

    # a section heading after the title
    intro = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="heading",
        text="Introduction",
        at={"after": title.handle},
    )[0]
    # two paragraphs inside it (one put, split at the blank line)
    paras = store.add_chunks(
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


def test_insert_before_reorders(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="Title", project_ref_id=proj)
    b = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": title.handle}
    )[0]
    # insert A before B → order Title, A, B
    store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"before": b.handle}
    )
    assert [t for _, t, _ in _order(store, ref.id)] == ["Title", "A", "B"]


def test_handles_are_unique_and_addressable(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="Title", project_ref_id=proj)
    extra = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="x", at={"after": title.handle}
    )[0]
    assert title.handle != extra.handle
    # round-trip by handle, with and without the ¶ sigil
    assert store.get_draft_chunk(extra.handle).text == "x"
    assert store.get_draft_chunk("¶" + extra.handle).chunk_id == extra.chunk_id


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
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="old", at={"after": title.handle}
    )[0]
    upd = store.edit_text(p.handle, "new text")
    assert upd.text == "new text"
    assert upd.handle == p.handle  # handle survives
    # created + edited(prev_text='old')
    assert _events(store, p.chunk_id) == [("created", None), ("edited", "old")]


def test_move_reorder_and_reparent(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    a = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    # reorder: B before A → T, B, A
    store.move_chunk(b.handle, {"before": a.handle})
    assert [t for _, t, _ in _order(store, ref.id)] == ["T", "B", "A"]
    assert _events(store, b.chunk_id)[-1][0] == "moved"
    # reparent: move A into B → A becomes B's child
    store.move_chunk(a.handle, {"into": b.handle, "last": True})
    assert _order(store, ref.id) == [
        ("heading", "T", 0),
        ("heading", "B", 0),
        ("heading", "A", 1),
    ]
    assert _events(store, a.chunk_id)[-1][0] == "reparented"


def test_move_cycle_guard(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    child = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )[0]
    with pytest.raises(ValueError, match="under itself or its own subtree"):
        store.move_chunk(h.handle, {"into": child.handle})


def test_retire_leaf_and_last_chunk_guard(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    p = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="x", at={"after": title.handle}
    )[0]
    store.retire_chunk(p.handle)
    assert [t for _, t, _ in _order(store, ref.id)] == ["T"]
    # title is now the last live chunk — cannot retire it
    with pytest.raises(ValueError, match="last live chunk"):
        store.retire_chunk(title.handle)


def test_retire_heading_requires_mode(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )
    with pytest.raises(ValueError, match="requires"):
        store.retire_chunk(h.handle)


def test_retire_cascade_and_promote(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="c2",
        at={"into": h.handle, "last": True},
    )
    # promote: H gone, c1/c2 lifted to root (depth 0) in H's slot
    store.retire_chunk(h.handle, mode="promote")
    assert _order(store, ref.id) == [
        ("heading", "T", 0),
        ("paragraph", "c1", 0),
        ("paragraph", "c2", 0),
    ]


def test_retire_cascade_deletes_subtree(store: Store) -> None:
    proj = _project(store)
    ref, title = store.create_draft(name="nt", title="T", project_ref_id=proj)
    h = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="H", at={"after": title.handle}
    )[0]
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.retire_chunk(h.handle, mode="cascade")
    assert [t for _, t, _ in _order(store, ref.id)] == ["T"]


# --- placement coverage: one test per `at` / `move` variant -------------


def _titled(store: Store):
    proj = _project(store)
    return store.create_draft(name="nt", title="T", project_ref_id=proj)


def _texts(store: Store, ref_id: int) -> list[str]:
    return [c.text for c in store.reading_order(ref_id)]


def _add(store, ref_id, text, **at):
    return store.add_chunks(ref_id=ref_id, chunk_kind="heading", text=text, at=at)[0]


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
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="c2",
        at={"into": h.handle, "last": True},
    )
    assert _texts(store, ref.id) == ["T", "H", "c1", "c2"]


def test_at_into_first(store: Store) -> None:
    ref, title = _titled(store)
    h = _add(store, ref.id, "H", after=title.handle)
    store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c1", at={"into": h.handle}
    )
    store.add_chunks(
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
    store.move_chunk(a.handle, {"after": b.handle})  # T, B, A
    assert _texts(store, ref.id) == ["T", "B", "A"]


def test_move_into_first(store: Store) -> None:
    ref, title = _titled(store)
    h = _add(store, ref.id, "H", after=title.handle)
    c = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="c", at={"into": h.handle}
    )[0]
    x = _add(store, ref.id, "X", after=h.handle)
    store.move_chunk(x.handle, {"into": h.handle, "first": True})
    assert _texts(store, ref.id) == ["T", "H", "X", "c"]
    assert c.handle  # silence unused


def test_move_to_root_first(store: Store) -> None:
    ref, title = _titled(store)
    a = _add(store, ref.id, "A", after=title.handle)
    store.move_chunk(a.handle, {"first": True})  # A, T
    assert _texts(store, ref.id) == ["A", "T"]
