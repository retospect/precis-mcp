"""Paper notes (gr521094): ``put(id=<paper>, text=…)`` appends an
append-only ``paper_note`` — the gripe-comment shape applied to papers.

Notes live in their own ``ord < 0`` band (migration 0262): every
``ord >= 0`` body predicate — search, count, TOC, ``has_body``,
find-in-ref, and the ``DELETE ... WHERE ord >= 0`` body rewrites —
excludes them by construction, so a note can never be read or lost as
body text.
"""

from __future__ import annotations

import pytest

import precis.handlers.paper as paper_mod
from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.paper import _NOTE_KIND, _NOTE_ORD_BASE, PaperHandler
from precis.runtime import PrecisRuntime
from precis.store import ChunkInsert, Store


@pytest.fixture(autouse=True)
def _offline(monkeypatch: pytest.MonkeyPatch) -> None:
    """The note tests never mint stubs through the network: the S2
    lookup and the DOI-registry fallback are stubbed out (same shape as
    tests/test_acquire.py)."""
    monkeypatch.setattr(
        paper_mod, "_lookup_acquire_metadata", lambda *_: {"_resolved": True}
    )
    monkeypatch.setattr(paper_mod, "_lookup_doi_registry", lambda *_: None)


@pytest.fixture
def handler(hub: Hub) -> PaperHandler:
    return PaperHandler(hub=hub)


def _seed(store: Store, slug: str = "wang2020state", n_blocks: int = 3) -> int:
    ref = store.insert_ref(
        kind="paper", slug=slug, title=f"Title of {slug}", provider="manual", meta={}
    )
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=i, text=f"body block {i}") for i in range(n_blocks)],
    )
    return ref.id


def _note_ords(store: Store, ref_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ord FROM chunks WHERE ref_id = %s AND chunk_kind = %s ORDER BY ord",
            (ref_id, _NOTE_KIND),
        ).fetchall()
    return [int(r[0]) for r in rows]


# ── append + read ───────────────────────────────────────────────────


def test_append_then_get_shows_newest_note(handler: PaperHandler, store: Store) -> None:
    _seed(store)
    ack = handler.put(id="wang2020state", text="first note").body
    assert "appended comment 1 to paper" in ack
    ack = handler.put(id="wang2020state", text="second note").body
    assert "appended comment 2 to paper" in ack

    out = handler.get(id="wang2020state").body
    # The overview shows the newest note in full plus the older one as a
    # one-liner, and never the body-chunk count inflated by notes.
    assert "## comment 2" in out and "second note" in out
    assert "comment 1" in out and "first note" in out
    assert "3 blocks" in out
    assert "view='comments'" in out


def test_notes_view_lists_all_and_selects(handler: PaperHandler, store: Store) -> None:
    rid = _seed(store)
    for i in range(1, 4):
        handler.put(id=rid, text=f"note-{i}")

    full = handler.get(id="wang2020state", view="comments").body
    assert full.index("note-1") < full.index("note-2") < full.index("note-3")
    assert full.count("## comment ") == 3

    one = handler.get(id="wang2020state", view="comments", args={"comment": 2}).body
    assert "note-2" in one and "note-1" not in one
    last = handler.get(id="wang2020state", view="comments", args={"comments": 2}).body
    assert "note-3" in last and "note-2" in last and "note-1" not in last
    window = handler.get(
        id="wang2020state", view="comments", args={"comments": "1-2"}
    ).body
    assert "note-1" in window and "note-2" in window and "note-3" not in window

    # 'notes' is the same view under an alias.
    assert handler.get(id="wang2020state", view="notes").body == full
    # A note-free paper names the append call instead of a blank page.
    rid2 = _seed(store, slug="empty2024")
    assert "no comments" in handler.get(id="empty2024", view="comments").body
    assert rid2 != rid


def test_note_selectors_refuse_off_view(handler: PaperHandler, store: Store) -> None:
    _seed(store)
    handler.put(id="wang2020state", text="n")
    with pytest.raises(BadInput, match="only apply to view='comments'"):
        handler.get(id="wang2020state", view="abstract", args={"comment": 1})
    with pytest.raises(BadInput, match="no comment 99"):
        handler.get(id="wang2020state", view="comments", args={"comment": 99})


def test_hide_and_restore_note(handler: PaperHandler, store: Store) -> None:
    rid = _seed(store)
    for i in range(1, 3):
        handler.put(id=rid, text=f"note-{i}")

    ack = handler.delete(id="wang2020state", args={"comment": 2}).body
    assert "hid comment 2" in ack
    assert store.fetch_refs_by_ids({rid})[rid].meta["hidden_comments"] == [2]
    # Bare get drops it; the full view keeps it marked.
    assert "note-2" not in handler.get(id="wang2020state").body
    full = handler.get(id="wang2020state", view="comments").body
    assert "note-2" in full and "[hidden]" in full

    assert (
        "already hidden" in handler.delete(id="wang2020state", args={"comment": 2}).body
    )
    restored = handler.delete(
        id="wang2020state", args={"comment": 2, "restore": True}
    ).body
    assert "restored comment 2" in restored
    assert "note-2" in handler.get(id="wang2020state").body


def test_delete_refusals(handler: PaperHandler, store: Store) -> None:
    rid = _seed(store)
    handler.put(id=rid, text="only")
    body_ord = next(
        c.ord
        for c in store.chunks.list_chunks_for_ref(rid)
        if c.chunk_kind == "paragraph"
    )
    # The K that maps to a body ord inside the band numbering still
    # names the chunk kind, not a comment.
    with pytest.raises(BadInput, match="no comment 99"):
        handler.delete(id="wang2020state", args={"comment": 99})
    with pytest.raises(BadInput, match="not a comment"):
        handler.delete(
            id="wang2020state", args={"comment": body_ord - _NOTE_ORD_BASE + 1}
        )
    with pytest.raises(BadInput):
        handler.delete(id="wang2020state", args={"comment": "abc"})
    # A bare delete refuses — a paper retires through edit.
    with pytest.raises(BadInput, match="not deletable"):
        handler.delete(id="wang2020state")
    with pytest.raises(BadInput, match="requires id="):
        handler.delete()


# ── notes are not body text ─────────────────────────────────────────


def test_note_text_invisible_to_body_search(
    handler: PaperHandler, store: Store
) -> None:
    rid = _seed(store)
    handler.put(id=rid, text="quixotic zebra token note body")

    # Lexical body search never reaches the ord<0 band.
    out = handler.search(q="quixotic", mode="lexical").body
    assert "wang2020state" not in out and f"pa{rid}" not in out
    # find-in-ref scans body chunks only.
    miss = handler.get(id="wang2020state", q="quixotic").body
    assert "no match" in miss
    # The chunk-range read and the body count see only the body.
    assert "quixotic" not in handler.get(id="wang2020state~0..2").body
    assert store.chunks.count_chunks(rid) == 3


def test_notes_live_in_the_negative_ord_band(
    handler: PaperHandler, store: Store
) -> None:
    rid = _seed(store, n_blocks=5)
    handler.put(id=rid, text="n1")
    handler.put(id=rid, text="n2")
    assert _note_ords(store, rid) == [_NOTE_ORD_BASE, _NOTE_ORD_BASE + 1]
    # meta.no_index keeps the embed/summarize claim predicates out too.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (rid, _NOTE_KIND),
        ).fetchone()
    assert row is not None and row[0]["no_index"] == "true"


def test_body_rewrite_keeps_notes_and_never_collides(
    handler: PaperHandler, store: Store
) -> None:
    """The reason the band exists: a body rewrite DELETEs every
    ``ord >= 0`` chunk and re-INSERTs at 0..M — a note at ``ord >= 0``
    would die or collide. At ``ord < 0`` neither happens."""
    rid = _seed(store, n_blocks=2)
    handler.put(id=rid, text="note survives")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE ref_id = %s AND ord >= 0", (rid,))
    store.chunks.insert_chunks(
        rid, [ChunkInsert(ord=i, text=f"new body {i}") for i in range(10)]
    )
    # The note is still there, numbered 1, and a second append still
    # lands in-band — never on an ord the new body took.
    assert _note_ords(store, rid) == [_NOTE_ORD_BASE]
    handler.put(id=rid, text="after rewrite")
    assert _note_ords(store, rid) == [_NOTE_ORD_BASE, _NOTE_ORD_BASE + 1]
    full = handler.get(id="wang2020state", view="comments").body
    assert "note survives" in full and "after rewrite" in full


# ── put doors around the note path ──────────────────────────────────


def test_put_text_without_id_still_refused(handler: PaperHandler) -> None:
    with pytest.raises(Unsupported, match="never writes bodies"):
        handler.put(text="a body rewrite attempt")


def test_put_text_through_dispatch_appends_note(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """The strict-kwargs gate admits ``text=`` — it is a declared
    ``put`` parameter, so ``put(kind='paper', id=…, text=…)`` through
    hub dispatch reaches the note door (an undeclared kwarg would die
    in ``**_kw`` as "does not accept ['text']")."""
    rid = _seed(store)
    out = runtime_with_store.dispatch(
        "put", {"kind": "paper", "id": "wang2020state", "text": "via dispatch"}
    )
    assert "[error" not in out, out
    assert "appended comment 1 to paper" in out
    assert _note_ords(store, rid) == [_NOTE_ORD_BASE]
    # ``args={'text': …}`` — the slim-schema spelling — promotes to the
    # same top-level kwarg instead of dying on the ARGS_KEYS gate.
    out = runtime_with_store.dispatch(
        "put",
        {
            "kind": "paper",
            "id": "wang2020state",
            "__extras__": {"text": "via args"},
        },
    )
    assert "appended comment 2 to paper" in out
    assert _note_ords(store, rid) == [_NOTE_ORD_BASE, _NOTE_ORD_BASE + 1]
    # text= without an id still refuses through dispatch too.
    out = runtime_with_store.dispatch("put", {"kind": "paper", "text": "x"})
    assert "never writes bodies" in out


def test_put_note_refuses_combined_fields(handler: PaperHandler, store: Store) -> None:
    rid = _seed(store)
    for kw in (
        {"doi": "10.1/x"},
        {"mode": "fetch-si"},
        {"tags": ["reading-list"]},
        {"record_type": "software"},
        {"args": {"pdf_url": "https://example.org/x.pdf"}},
    ):
        with pytest.raises(BadInput, match="does not combine"):
            handler.put(id="wang2020state", text="note", **kw)
    with pytest.raises(BadInput, match="non-empty text="):
        handler.put(id="wang2020state", text="   ")
    assert _note_ords(store, rid) == []


def test_existing_put_paths_unchanged(handler: PaperHandler, store: Store) -> None:
    """``put(id=N)`` without ``text=`` keeps its doors: the stub mint
    is untouched, and a bare ``put`` still names the mint fields."""
    resp = handler.put(doi="10.1038/nature10352")
    assert "id=" in resp.body
    with pytest.raises(BadInput, match="mints a stub"):
        handler.put()
