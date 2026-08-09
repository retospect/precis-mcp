"""Real-PG regression tests for the /drive route's raw SQL.

The FakeStore suite doesn't parse SQL, so the folder-tree / children /
unfiled / breadcrumb queries are exercised here against the live
``store`` fixture — same posture as ``test_structure_sql.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.folder import FolderHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.drive import (
    _breadcrumb,
    _children,
    _flatten_tree,
    _folder_tree,
    _unfiled,
)
from tests.conftest import id_of


@pytest.fixture
def drive_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


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


def test_recent_refs_has_external_id_filter(store):
    """``has_external_id`` narrows to refs carrying a fetchable DOI/arXiv/S2 —
    what makes ``/drive``'s "Stubs (to get)" queue (``has_pdf=False`` +
    ``has_external_id=True``) show only *fetchable* stubs, matching
    ``stub_backlog``. A PDF-less paper with no external id is not a stub (its
    row renders no download link), so it must not surface in the download
    queue — the regression for the id-less papers that floated to the top of
    the "Untried first" sort."""
    fetchable, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/fetchable")], title="Has a DOI", set_by="system"
    )
    idless = store.insert_ref(kind="paper", slug="idless2024", title="No DOI", meta={})

    stub_queue = {
        r.id for r in store.recent_refs(["paper"], has_pdf=False, has_external_id=True)
    }
    assert fetchable in stub_queue  # a fetchable stub belongs in the queue
    assert idless.id not in stub_queue  # the id-less paper is excluded

    # The inverse selects exactly the id-less papers (the complement).
    without = {r.id for r in store.recent_refs(["paper"], has_external_id=False)}
    assert idless.id in without
    assert fetchable not in without

    # count_recent_refs applies the identical filter, so "N of K" can't lie.
    listed = store.recent_refs(
        ["paper"], has_pdf=False, has_external_id=True, limit=1000
    )
    assert store.count_recent_refs(
        ["paper"], has_pdf=False, has_external_id=True
    ) == len(listed)


def test_recent_refs_downloadable_first_ranks_doi_arxiv_ahead_of_s2(store):
    """``downloadable_first=True`` (the "Stubs (to get)" queue) floats rows
    with a hand-downloadable id (DOI/arXiv → a LibKey/arXiv PDF link) ahead of
    S2-only rows, so the download queue's first page fills with openable papers
    instead of S2-only stubs that render no download link. Regression for the
    S2-heavy import that buried 5,598 downloadable stubs under "Untried first"."""
    # An S2-only stub created NEWEST — under plain untried (created_at DESC) it
    # would sort first; downloadable_first must sink it below the DOI/arXiv rows.
    doi_stub, _ = store.upsert_stub_paper(
        identifiers=[("doi", "10.1/dl-doi")], title="DOI paper", set_by="system"
    )
    arxiv_stub, _ = store.upsert_stub_paper(
        identifiers=[("arxiv", "2401.00099")], title="arXiv paper", set_by="system"
    )
    s2_stub, _ = store.upsert_stub_paper(
        identifiers=[("s2", "deadbeef" * 5)], title="S2-only paper", set_by="system"
    )

    ordered = [
        r.id
        for r in store.recent_refs(
            ["paper"],
            has_pdf=False,
            has_external_id=True,
            untried=True,
            downloadable_first=True,
        )
        if r.id in {doi_stub, arxiv_stub, s2_stub}
    ]
    # Both downloadable stubs precede the S2-only one, despite it being newest.
    assert ordered.index(s2_stub) == max(
        ordered.index(doi_stub), ordered.index(arxiv_stub), ordered.index(s2_stub)
    )
    assert ordered[-1] == s2_stub


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


def test_recent_refs_untried_orders_never_tried_before_tried(store):
    """``untried=True`` (the ``/drive?sort=untried`` downloads-queue
    order) puts never-manually-opened refs before previously-opened
    ones, freshest-added first within the untried group and
    oldest-attempt-first within the tried group — so a fresh page load
    surfaces the next un-attempted batch, and "Open all downloads"
    marking the current page tried sinks it to the back next time."""
    old_untried = store.insert_ref(kind="paper", slug="ut-old-untried", title="A")
    new_untried = store.insert_ref(kind="paper", slug="ut-new-untried", title="B")
    tried_long_ago = store.insert_ref(kind="paper", slug="ut-tried-long-ago", title="C")
    tried_recently = store.insert_ref(kind="paper", slug="ut-tried-recent", title="D")

    store.append_event(tried_long_ago.id, source="manual:open", event="opened")
    store.append_event(tried_recently.id, source="manual:open", event="opened")
    # A second, more recent attempt on tried_long_ago's ref would flip its
    # position — assert the ordering keys off MAX(ts), not any row.
    store.append_event(
        tried_recently.id, source="fetcher:unpaywall", event="fetch_failed"
    )

    ordered = [
        r.id
        for r in store.recent_refs(["paper"], untried=True)
        if r.id
        in {old_untried.id, new_untried.id, tried_long_ago.id, tried_recently.id}
    ]
    # Both untried refs precede both tried refs.
    untried_positions = [ordered.index(old_untried.id), ordered.index(new_untried.id)]
    tried_positions = [
        ordered.index(tried_long_ago.id),
        ordered.index(tried_recently.id),
    ]
    assert max(untried_positions) < min(tried_positions)
    # Within "untried", freshest-added (new_untried) sorts first.
    assert ordered.index(new_untried.id) < ordered.index(old_untried.id)
    # Within "tried", oldest-attempt-first: tried_long_ago before tried_recently.
    assert ordered.index(tried_long_ago.id) < ordered.index(tried_recently.id)


def test_recent_refs_untried_sinks_after_a_fresh_manual_open(store):
    """A ref's second ``manual:open`` event (a later "Open all downloads"
    click) pushes it further back — the untried sort keys off the
    *latest* attempt, so re-opening resets the re-check clock."""
    a = store.insert_ref(kind="paper", slug="ut-sink-a", title="A")
    b = store.insert_ref(kind="paper", slug="ut-sink-b", title="B")
    store.append_event(b.id, source="manual:open", event="opened")  # b tried first
    store.append_event(a.id, source="manual:open", event="opened")  # a tried after

    before = [
        r.id for r in store.recent_refs(["paper"], untried=True) if r.id in {a.id, b.id}
    ]
    assert before.index(b.id) < before.index(
        a.id
    )  # b's attempt is older → surfaces first

    # b is opened again — now the *more* recently attempted of the two.
    store.append_event(b.id, source="manual:open", event="opened")
    after = [
        r.id for r in store.recent_refs(["paper"], untried=True) if r.id in {a.id, b.id}
    ]
    assert after.index(a.id) < after.index(
        b.id
    )  # a is now the older (un-refreshed) attempt


def test_recent_refs_untried_composes_with_has_chunks_filter(store):
    """``untried=True`` composes with ``has_chunks=False`` (the
    ``paper_chunks=without`` facet — the URL the operator actually browses
    for the downloads/acquisition queue) rather than one silently
    overriding the other: a chunked ref stays excluded regardless of its
    manual-open history, and untried-first ordering still applies among
    the chunk-less survivors."""
    from precis.embedder import MockEmbedder
    from precis.store import BlockInsert

    emb = MockEmbedder(dim=store.embedding_dim())
    chunked = store.insert_ref(kind="paper", slug="ut-hc-chunked", title="Chunked")
    store.insert_blocks(
        chunked.id, [BlockInsert(pos=0, text="body", embedding=emb.embed_one("x"))]
    )
    untried_stub = store.insert_ref(kind="paper", slug="ut-hc-untried", title="Untried")
    tried_stub = store.insert_ref(kind="paper", slug="ut-hc-tried", title="Tried")
    store.append_event(tried_stub.id, source="manual:open", event="opened")

    rows = store.recent_refs(["paper"], has_chunks=False, untried=True)
    ids = [r.id for r in rows]

    assert chunked.id not in ids  # has_chunks=False still excludes it
    assert untried_stub.id in ids
    assert tried_stub.id in ids
    assert ids.index(untried_stub.id) < ids.index(tried_stub.id)


def test_mark_downloads_tried_writes_one_manual_open_event_per_ref(
    drive_client: TestClient, store
):
    """``POST /downloads/mark-tried`` — the "Open all downloads" button's
    beacon — writes one ``ref_events`` row per posted ref_id
    (``source='manual:open', event='opened'``), de-duped, and ignores an
    unknown id rather than failing the whole batch."""
    a = store.insert_ref(kind="paper", slug="mt-a", title="A")
    b = store.insert_ref(kind="paper", slug="mt-b", title="B")

    resp = drive_client.post(
        "/downloads/mark-tried",
        data={
            "ref_id": [
                str(a.id),
                str(b.id),
                str(a.id),  # duplicate — one event, not two
                "999999999",  # unknown ref — skipped, not fatal
            ]
        },
    )
    assert resp.status_code == 204

    events_a = store.events_for(a.id, source="manual:open")
    events_b = store.events_for(b.id, source="manual:open")
    assert len(events_a) == 1
    assert events_a[0].event == "opened"
    assert len(events_b) == 1


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
