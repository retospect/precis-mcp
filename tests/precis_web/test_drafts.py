"""Drafts tab routes (ADR 0033, Tier-A web viewer/editor).

Self-contained: a draft-aware fake store (chunks / TOC / links) wrapped
in the conftest ``FakeRuntime`` + a TestClient — no Postgres. Exercises
the reader, the ``¶`` handle redirect, the chunk preview popover, and
the change-request POST.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime, FakeStore, make_ref

_DRAFT = make_ref(id=500, kind="draft", slug="nt", title="Nano draft")


def _chunk(handle, kind, text, depth, chunk_id, parent_chunk_id=None, ref_id=500):
    return SimpleNamespace(
        handle=handle,
        chunk_kind=kind,
        text=text,
        depth=depth,
        chunk_id=chunk_id,
        parent_chunk_id=parent_chunk_id,
        ref_id=ref_id,
    )


class DraftFakeStore(FakeStore):
    def __init__(self) -> None:
        super().__init__()
        # BBBBBB is parented under the AAAAAA heading → ancestors=[AAAAAA],
        # so collapsing AAAAAA hides it (collapse mechanics).
        self._chunks = [
            _chunk("AAAAAA", "heading", "Nano draft", 0, chunk_id=1),
            _chunk(
                "BBBBBB",
                "paragraph",
                "Intro; see [the title](¶AAAAAA) and paper:smith2024.",
                1,
                chunk_id=2,
                parent_chunk_id=1,
            ),
        ]
        # draft-of → project todo 1; related-to → memory 20
        self._links = [
            SimpleNamespace(
                src_ref_id=500, dst_ref_id=1, dst_pos=None, relation="draft-of", meta={}
            ),
            SimpleNamespace(
                src_ref_id=500,
                dst_ref_id=20,
                dst_pos=None,
                relation="related-to",
                meta={"auto": "mention"},
            ),
        ]

    def get_ref(self, *, kind, id):
        if kind == "draft" and id in ("nt", 500):
            return _DRAFT
        return super().get_ref(kind=kind, id=id)

    def list_refs(self, *, kind=None, limit=50, offset=0, **kw):
        if kind == "draft":
            return [_DRAFT]
        return super().list_refs(kind=kind, limit=limit, offset=offset, **kw)

    def reading_order(self, ref_id):
        return list(self._chunks)

    def search_blocks_semantic(self, *, query_vec, scope_ref_id, limit, max_distance):
        # Rank the heading ahead of the intro para (best-first), keyed by
        # chunk_id so the route's chunk_id→handle map resolves them.
        return [
            (SimpleNamespace(id=1), _DRAFT, 0.10),
            (SimpleNamespace(id=2), _DRAFT, 0.42),
        ]

    def draft_toc(self, ref_id, *, root_handle=None):
        return [
            SimpleNamespace(
                handle="AAAAAA", depth=0, title="Nano draft", keywords=[], gist=None
            )
        ]

    def get_draft_chunk(self, handle):
        for c in self._chunks:
            if c.handle == handle:
                return c
        return None

    def links_for(self, ref_id, *, direction="both", relation=None):
        out = [ln for ln in self._links if relation is None or ln.relation == relation]
        if direction == "out":
            out = [ln for ln in out if ln.src_ref_id == ref_id]
        elif direction == "in":
            out = [ln for ln in out if ln.dst_ref_id == ref_id]
        return out

    def fetch_refs_by_ids(self, ids, *, include_deleted=False):
        extra = {20: self.memories[0]}  # memory:20 'A decision'
        base = super().fetch_refs_by_ids(ids, include_deleted=include_deleted)
        base.update({i: extra[i] for i in ids if i in extra})
        return base


@pytest.fixture
def draft_runtime() -> FakeRuntime:
    return FakeRuntime(DraftFakeStore())


@pytest.fixture
def draft_client(draft_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(runtime=draft_runtime, web_config=WebConfig(corpus_dir=tmp_path))
    return TestClient(app)


def test_index_lists_drafts(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts")
    assert r.status_code == 200
    assert "Nano draft" in r.text and "/drafts/nt" in r.text


def test_reader_renders_per_block_grid(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    # one row per block, anchored by handle
    assert 'id="c-AAAAAA"' in r.text and 'id="c-BBBBBB"' in r.text
    # raw source linkified in the content column: paper ref → resolver,
    # ¶ ref → chunk route
    assert "/r/paper/smith2024" in r.text
    assert 'href="/c/AAAAAA"' in r.text
    # collapse mechanics: heading carries data-heading; BBBBBB carries its
    # ancestor heading so it hides when AAAAAA collapses
    assert 'data-heading="AAAAAA"' in r.text
    assert "collapse all" in r.text
    assert '["AAAAAA"]' in r.text  # BBBBBB's ancestors json
    # per-block change box posts to the anchored-todo route
    assert 'action="/drafts/nt/request"' in r.text


def test_singular_alias_redirects(draft_client: TestClient) -> None:
    r = draft_client.get("/draft/nt", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt"


def test_row_fragment_renders_single_block(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt/row/BBBBBB")
    assert r.status_code == 200
    assert 'id="c-BBBBBB"' in r.text and 'action="/drafts/nt/request"' in r.text
    # only the one row — the other block's id is absent
    assert 'id="c-AAAAAA"' not in r.text


def test_version_endpoint_returns_token(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt/version")
    assert r.status_code == 200
    assert "version" in r.json()


def test_rows_fragment_has_all_blocks_no_chrome(draft_client: TestClient) -> None:
    # The live-refresh poll swaps this into #doc — rows only, no <h1>/nav.
    r = draft_client.get("/drafts/nt/rows")
    assert r.status_code == 200
    assert 'id="c-AAAAAA"' in r.text and 'id="c-BBBBBB"' in r.text
    assert "<h1" not in r.text and "draftDoc(" not in r.text


def test_chunk_handle_redirects_into_reader(draft_client: TestClient) -> None:
    r = draft_client.get("/c/BBBBBB", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt#c-BBBBBB"


def test_unknown_chunk_handle_404s(draft_client: TestClient) -> None:
    r = draft_client.get("/c/ZZZZZZ", follow_redirects=False)
    assert r.status_code == 404


def test_chunk_preview_fragment(draft_client: TestClient) -> None:
    r = draft_client.get("/preview/chunk/BBBBBB")
    assert r.status_code == 200
    assert "BBBBBB" in r.text and "paragraph" in r.text


def test_chunk_preview_missing_is_graceful(draft_client: TestClient) -> None:
    r = draft_client.get("/preview/chunk/ZZZZZZ")
    assert r.status_code == 200
    assert "no such" in r.text  # popover 'missing' branch


def test_change_request_dispatches_anchored_todo(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/request",
        data={"handle": "BBBBBB", "text": "tighten this"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt#c-BBBBBB"
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["kind"] == "todo"
    assert args["parent_id"] == 1  # the draft-of project
    assert args["meta"]["anchor"] == "¶BBBBBB"


def test_ref_chips_dedup_sigil_and_kindref() -> None:
    """§kong24~2 and paper:kong24~2 are the same target → one chip."""
    from precis_web.routes.drafts import _ref_chips

    chips = _ref_chips("see [§kong24~2] and also paper:kong24~2 again")
    assert len(chips) == 1
    html = str(chips[0])
    assert "/r/paper/kong24?chunk=2" in html
    # chip carries the lazy quote-preview popover
    assert "/preview/paper/kong24?chunk=2" in html


def test_ref_chips_distinct_chunks_stay_separate() -> None:
    from precis_web.routes.drafts import _ref_chips

    chips = _ref_chips("[§kong24~2] [§kong24~21] [§kong24~22]")
    assert len(chips) == 3


def test_delete_change_request_dispatches_todo_delete(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post("/drafts/nt/todo/777/delete", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "delete" and args["kind"] == "todo" and args["id"] == 777


def test_tasks_gist_summarises_long_bodies_only() -> None:
    """A multi-line / long todo body gets a 3-keyword RAKE gist; a short
    single-line one is shown verbatim (no gist)."""
    from precis_web.routes.tasks import _gist

    assert _gist("tighten this") == ""  # short single line → no gist
    long_body = (
        "Amine functionalization via post-synthetic impregnation graft "
        "polyethyleneimine onto a mixed-ligand framework by wet impregnation.\n"
        "The resulting material shows high carbon dioxide uptake capacity."
    )
    g = _gist(long_body)
    assert g and " · " in g  # joined keyword phrases


def test_reader_has_view_slider(draft_client: TestClient) -> None:
    """The body/summary/keywords 3-stop slider + per-block view spans."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "setView(" in r.text  # the radio control
    assert "view === 'summary'" in r.text and "view === 'keywords'" in r.text


def test_review_dropdown_and_dispatch(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """Heading rows offer a review ▾ menu; selecting one files an anchored
    review-todo (parented on the project) via the put verb."""
    page = draft_client.get("/drafts/nt")
    assert "review ▾" in page.text and "/drafts/nt/review" in page.text
    r = draft_client.post(
        "/drafts/nt/review",
        data={"handle": "AAAAAA", "reviewer": "structural"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["kind"] == "todo"
    assert args["parent_id"] == 1 and args["meta"]["anchor"] == "¶AAAAAA"
    assert (
        args["meta"]["review"] == "structural" and "Structural review" in args["text"]
    )


def test_find_verbatim_doc_order(draft_client: TestClient) -> None:
    """Verbatim find = case-insensitive substring in document order."""
    r = draft_client.get("/drafts/nt/find", params={"q": "intro", "mode": "verbatim"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "verbatim"
    assert body["handles"] == ["BBBBBB"]  # only the intro para matches
    # a miss returns no handles
    assert (
        draft_client.get("/drafts/nt/find", params={"q": "zzzzz"}).json()["handles"]
        == []
    )


def test_find_semantic_degrades_without_embedder(draft_client: TestClient) -> None:
    """No embedder wired (the fake runtime has no hub) → semantic falls
    back to a verbatim find rather than 500ing."""
    r = draft_client.get("/drafts/nt/find", params={"q": "intro", "mode": "semantic"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "verbatim" and body["handles"] == ["BBBBBB"]


def test_find_semantic_ranked(tmp_path) -> None:
    """With an embedder, semantic find returns the draft's chunks
    cosine-ranked (best-first), mapped chunk_id→handle."""

    class _Emb:
        def embed_one(self, q):
            return [0.1, 0.2, 0.3]

    rt = FakeRuntime(DraftFakeStore())
    rt.hub = SimpleNamespace(embedder=_Emb())
    client = TestClient(
        create_app(runtime=rt, web_config=WebConfig(corpus_dir=tmp_path))
    )
    r = client.get("/drafts/nt/find", params={"q": "nano", "mode": "semantic"})
    assert r.status_code == 200
    body = r.json()
    assert body["mode"] == "semantic"
    # search_blocks_semantic ranked chunk 1 (AAAAAA) before chunk 2 (BBBBBB)
    assert body["handles"] == ["AAAAAA", "BBBBBB"]
