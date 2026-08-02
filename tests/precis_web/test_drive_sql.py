"""Real-PG regression tests for the /drive route's raw SQL (ADR 0045).

The FakeStore suite doesn't parse SQL, so the folder-tree / children /
unfiled / breadcrumb queries are exercised here against the live
``store`` fixture — same posture as ``test_structure_sql.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis.dispatch import Hub
from precis.handlers.folder import FolderHandler
from precis_web.routes.drive import (
    _breadcrumb,
    _children,
    _flatten_tree,
    _folder_tree,
    _unfiled,
)
from tests.conftest import id_of


@pytest.fixture
def seeded(store):
    """Projects/Hardware nesting + one cad design inside, one unfiled."""
    folder = FolderHandler(hub=Hub(store=store))
    top = id_of(folder.put(text="Projects").body)
    sub = id_of(folder.put(text="Hardware").body)
    folder.link(id=sub, target=f"folder:{top}", rel="parent")
    store.insert_ref(kind="cad", slug="bracket", title="a bracket", meta={})
    store.insert_ref(kind="cad", slug="loose", title="a loose part", meta={})
    from precis.handlers.cad import CadHandler

    CadHandler(hub=Hub(store=store)).link(
        id="bracket", target=f"folder:{sub}", rel="parent"
    )
    return {"top": top, "sub": sub}


def test_folder_tree_nests(store, seeded):
    roots = _folder_tree(store)
    assert [r["title"] for r in roots] == ["Projects"]
    assert [c["title"] for c in roots[0]["children"]] == ["Hardware"]
    flat = _flatten_tree(roots)
    assert [(f["title"], f["depth"]) for f in flat] == [
        ("Projects", 0),
        ("Hardware", 1),
    ]
    # child counts: Projects holds Hardware; Hardware holds the cad ref
    assert flat[0]["n_children"] == 1
    assert flat[1]["n_children"] == 1


def test_children_rows_carry_slug_and_reader_fields(store, seeded):
    rows = _children(store, seeded["sub"])
    assert len(rows) == 1
    (row,) = rows
    assert row["kind"] == "cad"
    assert row["ident"] == "bracket"
    assert row["handler_id"] == "bracket"


def test_unfiled_lists_only_parentless_artifacts(store, seeded):
    rows = _unfiled(store, ["draft", "structure", "cad", "todo"])
    idents = [r["ident"] for r in rows]
    assert "loose" in idents
    assert "bracket" not in idents  # filed → not unfiled


def test_breadcrumb_walks_up(store, seeded):
    crumbs = _breadcrumb(store, seeded["sub"])
    assert [c["title"] for c in crumbs] == ["Projects", "Hardware"]


def test_recent_refs_has_chunks_filter(store):
    """``has_chunks`` narrows ``/drive``'s ``state=chunked``/``unchunked``
    facet to refs with (or without) a body chunk (``ord >= 0``)."""
    from precis.embedder import MockEmbedder
    from precis.store import BlockInsert

    emb = MockEmbedder(dim=store.embedding_dim())
    with_chunk = store.insert_ref(kind="web", slug="has-a-chunk", title="chunked")
    store.insert_blocks(
        with_chunk.id,
        [BlockInsert(pos=0, text="some body text", embedding=emb.embed_one("x"))],
    )
    without_chunk = store.insert_ref(kind="web", slug="no-chunk", title="unchunked")

    chunked = {r.id for r in store.recent_refs(["web"], has_chunks=True)}
    assert with_chunk.id in chunked
    assert without_chunk.id not in chunked

    unchunked = {r.id for r in store.recent_refs(["web"], has_chunks=False)}
    assert without_chunk.id in unchunked
    assert with_chunk.id not in unchunked


def test_recent_refs_oldest_reverses_order(store):
    """``oldest=True`` flips the browse order to oldest-first (the
    ``/drive?sort=oldest`` facet) — the exact reverse of the default."""
    a = store.insert_ref(kind="paper", slug="ord-a", title="A")  # oldest
    b = store.insert_ref(kind="paper", slug="ord-b", title="B")
    c = store.insert_ref(kind="paper", slug="ord-c", title="C")  # newest

    newest_first = [r.id for r in store.recent_refs(["paper"])]
    oldest_first = [r.id for r in store.recent_refs(["paper"], oldest=True)]
    # These three appear in opposite relative order under the two sorts.
    assert newest_first.index(c.id) < newest_first.index(a.id)
    assert oldest_first.index(a.id) < oldest_first.index(c.id)
    assert [x for x in oldest_first if x in {a.id, b.id, c.id}] == [a.id, b.id, c.id]


def test_count_recent_refs_matches_list_under_same_filters(store):
    """``count_recent_refs`` is the exact denominator for the ``/drive``
    browse — same filter set as ``recent_refs``, so "N of K" never lies."""
    from precis.embedder import MockEmbedder
    from precis.store import BlockInsert

    emb = MockEmbedder(dim=store.embedding_dim())
    # Two chunked, one stub (no PDF, no chunk) — distinct kind to isolate.
    for slug in ("cnt-chunked-1", "cnt-chunked-2"):
        r = store.insert_ref(kind="wikipedia", slug=slug, title=slug)
        store.insert_blocks(
            r.id, [BlockInsert(pos=0, text="body", embedding=emb.embed_one("x"))]
        )
    store.insert_ref(kind="wikipedia", slug="cnt-bare", title="bare")

    assert store.count_recent_refs(["wikipedia"]) == 3
    assert store.count_recent_refs(["wikipedia"], has_chunks=True) == 2
    assert store.count_recent_refs(["wikipedia"], has_chunks=False) == 1
    # An empty kind set / empty allow-list counts nothing.
    assert store.count_recent_refs([]) == 0
    assert store.count_recent_refs(["wikipedia"], ref_ids=[]) == 0
    # Count agrees with the length of the (unpaged) list under the same filter.
    listed = store.recent_refs(["wikipedia"], has_chunks=True, limit=1000)
    assert store.count_recent_refs(["wikipedia"], has_chunks=True) == len(listed)


def test_recent_refs_ref_ids_allow_list(store):
    """``ref_ids`` restricts the browse to an explicit id set (the
    ``/drive?cited_by=<draft>`` fetch-worklist scope): ``None`` = no
    restriction, a list = only those ids, an empty list = nothing."""
    a = store.insert_ref(kind="paper", slug="ref-a", title="Paper A")
    b = store.insert_ref(kind="paper", slug="ref-b", title="Paper B")
    c = store.insert_ref(kind="paper", slug="ref-c", title="Paper C")

    unrestricted = {r.id for r in store.recent_refs(["paper"], ref_ids=None)}
    assert {a.id, b.id, c.id} <= unrestricted

    scoped = {r.id for r in store.recent_refs(["paper"], ref_ids=[a.id, c.id])}
    assert scoped == {a.id, c.id}  # exactly the allow-list, b excluded

    # An empty allow-list restricts to nothing (a draft with an empty
    # worklist shows an empty queue, not the whole corpus).
    assert store.recent_refs(["paper"], ref_ids=[]) == []


def test_conv_chat_turn_surfaces_as_drive_search_hit(store):
    """A captured conversation's turn (the Discord / Slack bridge threads
    are stored as ``conv`` with each turn an embedded body chunk) surfaces
    on ``/drive`` search like any other source: the matching chat message
    is the row preview, and the row opens the transcript at
    ``/refs/conv/{id}``. Guards ``conv`` being a first-class source kind."""
    from precis.embedder import MockEmbedder
    from precis.store import BlockInsert
    from precis_web.routes.items import _run_search

    emb = MockEmbedder(dim=store.embedding_dim())
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="conv", slug="discord/1/2/3", title="#general", meta={}, conn=conn
        )
        store.insert_blocks(
            ref.id,
            [
                BlockInsert(
                    pos=0,
                    text="did the reingest finish yet?",
                    meta={"author": "alice"},
                    embedding=emb.embed_one("reingest"),
                )
            ],
            conn=conn,
        )

    rows, _ = _run_search(
        store,
        emb,
        kinds=["conv"],
        q="reingest",
        sort="relevance",
        since=None,
        until=None,
        tags=[],
        offset=0,
    )
    hit = next(r for r in rows if r["id"] == ref.id)
    assert hit["kind"] == "conv"
    assert hit["open_url"] == f"/refs/conv/{ref.id}"
    assert "reingest" in hit["preview"]
