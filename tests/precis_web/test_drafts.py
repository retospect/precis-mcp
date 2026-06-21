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
