"""Draft store ops — create / add / reading-order (ADR 0033, Phase 3b)."""

from __future__ import annotations

import pytest

from precis.errors import BadInput
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


def test_add_chunks_unknown_anchor_raises_notfound(store: Store) -> None:
    """A typo'd / stale `at=` anchor surfaces as a typed NotFound.

    Before the fix these two paths raised a raw ``ValueError`` that the
    handler rendered as the opaque "internal error in put: ValueError"
    fallback — the same gripe #45083 class as the edit/move/retire ops.
    """
    from precis.errors import NotFound

    proj = _project(store)
    ref, _title = store.create_draft(name="nf", title="Title", project_ref_id=proj)

    with pytest.raises(NotFound, match="unknown chunk handle"):
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="orphan",
            at={"after": "¶missing"},
        )
    with pytest.raises(NotFound, match="unknown parent handle"):
        store.add_chunks(
            ref_id=ref.id,
            chunk_kind="paragraph",
            text="orphan",
            at={"into": "¶missing", "last": True},
        )


def _list_fixture(store: Store) -> tuple[int, str]:
    """A draft with a ulist container + two items under the title. Returns
    ``(ref_id, container_handle)``."""
    proj = _project(store)
    ref, title = store.create_draft(name="lst", title="Title", project_ref_id=proj)
    ul = store.add_chunks(
        ref_id=ref.id, chunk_kind="ulist", text="", at={"after": title.handle}
    )[0]
    store.add_chunks(
        ref_id=ref.id,
        chunk_kind="item",
        text="alpha\n\nbeta",
        at={"into": ul.handle, "last": True},
    )
    return ref.id, ul.handle


def test_set_list_kind_flips_container_in_place(store: Store) -> None:
    ref_id, ul = _list_fixture(store)
    store.set_list_kind(ul, "olist")
    kinds = [k for k, _, _ in _order(store, ref_id)]
    assert "olist" in kinds and "ulist" not in kinds
    # items untouched (still two items under the container)
    assert kinds.count("item") == 2


def test_set_list_kind_normal_dissolves_to_paragraphs(store: Store) -> None:
    ref_id, ul = _list_fixture(store)
    store.set_list_kind(ul, "normal")
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
    ref, title = store.create_draft(name="x", title="T", project_ref_id=proj)
    with pytest.raises(BadInput):
        store.set_list_kind(title.handle, "olist")


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
    with pytest.raises(BadInput, match="under itself or its own subtree"):
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
    with pytest.raises(BadInput, match="last live chunk"):
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
    with pytest.raises(BadInput, match="requires"):
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


def test_live_paper_cites_splits_local_vs_external(store: Store) -> None:
    """The draft-reader colouring signal: only citation tokens that resolve
    to a live paper we hold come back (slug cite_key, ``pc`` chunk handle,
    ``pa`` record handle); unknown, non-paper, and soft-deleted targets are
    external. Mirrors ``§slug`` / ``[pc..]`` / ``[pa..]`` inline forms."""
    from precis.store.types import BlockInsert
    from precis.utils import handle_registry

    paper = store.insert_ref(kind="paper", slug="miller23", title="Paper")
    store.insert_blocks(
        paper.id, [BlockInsert(pos=0, text="We measured 12% FE.", meta={})]
    )
    with store.pool.connection() as conn:
        chunk_id = int(
            conn.execute(
                "SELECT chunk_id FROM chunks WHERE ref_id=%s ORDER BY ord LIMIT 1",
                (paper.id,),
            ).fetchone()[0]
        )
    pc = handle_registry.format_handle("paper", chunk_id, chunk=True)  # pc<id>
    pa = handle_registry.format_handle("paper", paper.id)  # pa<id> record
    # a non-paper record (memory) whose handle must NOT count as a paper cite
    mem = store.insert_ref(kind="memory", slug=None, title="note").id
    me = handle_registry.format_handle("memory", mem)

    live = store.live_paper_cites({pc, pa, me, "pc999999"}, {"miller23", "ghost404"})
    assert live == {pc, pa, "miller23"}  # the paper's slug + both live handles
    assert "ghost404" not in live and "pc999999" not in live and me not in live

    # soft-deleting the paper flips every one of its tokens to external
    store.soft_delete_ref(paper.id)
    assert store.live_paper_cites({pc, pa}, {"miller23"}) == set()


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
    ref, _title = store.create_draft(name="gh", title="Ghost", project_ref_id=proj)
    p1 = store.add_chunks(
        ref_id=ref.id, chunk_kind="paragraph", text="xenophilus alpha"
    )[0]
    p2 = store.add_chunks(
        ref_id=ref.id,
        chunk_kind="paragraph",
        text="xenophilus beta",
        at={"after": p1.handle},
    )[0]
    texts = {
        b.text
        for b, _r, _s in store.search_blocks_lexical(q="xenophilus", kind="draft")
    }
    assert {"xenophilus alpha", "xenophilus beta"} <= texts

    store.retire_chunk(p1.handle)  # p1 now retired (p2 keeps the draft non-empty)
    texts2 = {
        b.text
        for b, _r, _s in store.search_blocks_lexical(q="xenophilus", kind="draft")
    }
    assert "xenophilus alpha" not in texts2  # the ghost is gone
    assert "xenophilus beta" in texts2  # its live sibling stays
    assert p2  # silence unused-var lint


def test_add_after_retired_anchor_recovers(store: Store) -> None:
    """Fix B: `add(after=<retired>)` recovers into the ghost slot instead of
    raising StopIteration."""
    proj = _project(store)
    ref, title = store.create_draft(name="ga", title="T", project_ref_id=proj)
    a = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="C", at={"after": b.handle}
    )
    store.retire_chunk(b.handle)  # order now T, A, [B ghost], C

    store.add_chunks(
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
    ref, title = store.create_draft(name="gmv", title="T", project_ref_id=proj)
    a = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="A", at={"after": title.handle}
    )[0]
    b = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="B", at={"after": a.handle}
    )[0]
    c = store.add_chunks(
        ref_id=ref.id, chunk_kind="heading", text="C", at={"after": b.handle}
    )[0]
    store.retire_chunk(b.handle)  # order now T, A, [B ghost], C

    store.move_chunk(c.handle, {"before": b.handle})  # must not raise
    assert _kinds_texts(store, ref.id) == [
        ("heading", "T"),
        ("heading", "A"),
        ("heading", "C"),
    ]
