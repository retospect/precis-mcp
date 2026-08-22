"""Smartdraft's document header, rendered — the small/big metadata strip.

The panel is the reader's only surface for the ref's own metadata (its
``meta``, its whole-document edges) and for renaming the draft. These pin
that it actually reaches the HTML, since the assembly is unit-tested
elsewhere (``tests/test_smartdraft_header.py``) and a template that silently
drops a variable would still pass those.
"""

from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.errors import BadInput, NotFound
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime
from .test_smartdraft_reader import _DRAFT, SmartDraftFakeStore

#: A COPY of the reader fixture's draft — `_DRAFT` is module-level and shared
#: with test_smartdraft_reader, so stamping meta onto it in place would leak
#: into that file's expectations.
_META_DRAFT = SimpleNamespace(
    **{
        **vars(_DRAFT),
        "meta": {
            "workspace": {"doc_type": "paper", "path": "/w/sdt"},
            "voice": "bm_george",
            "audio_failed_at": "2026-08-06T14:44:41+00:00",
            "newfangled_key": "nobody taught the UI about this one",
        },
    }
)


class MetaFakeStore(SmartDraftFakeStore):
    """A draft carrying the shapes the panel exists for: a known key, an
    unknown one, a failure stamp, and an inbound concern."""

    def get_ref(self, *, kind, id):
        if kind == "draft" and id in ("sdt", 700):
            return _META_DRAFT
        return super().get_ref(kind=kind, id=id)

    def list_refs(self, *, kind=None, limit=50, offset=0, **kw):
        if kind == "draft":
            return [_META_DRAFT]
        return super().list_refs(kind=kind, limit=limit, offset=offset, **kw)

    def fetch_refs_by_ids(self, ids, *, include_deleted=False):
        base = super().fetch_refs_by_ids(ids, include_deleted=include_deleted)
        if 700 in ids:
            base[700] = _META_DRAFT
        return base

    def ref_connections(self, ref_id):
        return [
            {
                "relation": "draft-of",
                "direction": "out",
                "kind": "todo",
                "ident": "42",
                "title": "The project",
            },
            {
                "relation": "raises-concern-about",
                "direction": "in",
                "kind": "gripe",
                "ident": "9001",
                "title": "Section 3 contradicts the abstract",
            },
        ]


@pytest.fixture
def meta_client(tmp_path) -> TestClient:
    app = create_app(
        runtime=FakeRuntime(MetaFakeStore()), web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def test_header_renders_known_and_unknown_meta(meta_client: TestClient) -> None:
    html = meta_client.get("/smartdraft/sdt").text
    assert "TTS voice" in html and "bm_george" in html  # labelled
    assert "newfangled_key" in html  # fallthrough, under its raw name
    assert "nobody taught the UI about this one" in html
    assert "paper" in html and "/w/sdt" in html  # workspace, flattened


def test_header_surfaces_a_failure_stamp(meta_client: TestClient) -> None:
    # The motivating case — an `audio_failed_at` that no UI ever showed.
    html = meta_client.get("/smartdraft/sdt").text
    assert "audio failed" in html
    assert "2026-08-06T14:44:41+00:00" in html


def test_header_shows_document_edges_and_flags_concerns(
    meta_client: TestClient,
) -> None:
    html = meta_client.get("/smartdraft/sdt").text
    assert "raises-concern-about" in html
    assert "draft-of" in html
    assert "⚑ 1" in html  # the collapsed strip's concern chip


def test_a_long_relation_reaches_the_html_in_full(tmp_path) -> None:
    """A briefing's bibliography must be *reachable*, not summarised into a
    count. The assembly hands over every edge (unit-tested next door); this
    pins that the template renders them all and bounds height by scrolling
    instead of re-truncating."""

    class ManyConnStore(MetaFakeStore):
        def ref_connections(self, ref_id):
            return [
                {
                    "relation": "cites",
                    "direction": "out",
                    "kind": "paper",
                    "ident": f"p{i}",
                    "title": f"Paper {i}",
                }
                for i in range(30)
            ]

    client = TestClient(
        create_app(
            runtime=FakeRuntime(ManyConnStore()),
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )
    html = client.get("/smartdraft/sdt").text
    for i in range(30):
        assert f"paper:p{i}" in html, f"chip {i} never reached the page"
    assert "more</span>" not in html  # no overflow stub left behind
    assert "overflow-y-auto" in html  # …because the box scrolls instead


def test_header_offers_a_rename(meta_client: TestClient) -> None:
    html = meta_client.get("/smartdraft/sdt").text
    assert 'id="sd-title"' in html
    assert "/title`" in html  # the POST target in sdHeader().save()


def test_rename_script_carries_its_own_ident(meta_client: TestClient) -> None:
    """`sdHeader()` must build the POST URL from an ident rendered into its
    OWN <script>.

    The nav script's ``IDENT`` looks global but is a ``const`` inside that
    script's IIFE. `save()` lives in a different <script>, so reaching for
    it threw a ReferenceError — and with ``@submit.prevent`` having already
    swallowed the native submit, the rename was a silent no-op: the box
    stayed open, nothing was written, no error was shown. Renaming was dead
    from the day it shipped.
    """
    html = meta_client.get("/smartdraft/sdt").text
    blocks = re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S)
    header = [b for b in blocks if "function sdHeader" in b]
    assert len(header) == 1, "sdHeader() should live in exactly one <script>"
    script = header[0]
    assert '"sdt"' in script, "the ident is not rendered into the rename script"
    # Comments stripped: this file's own prose names `IDENT` to explain it.
    code = re.sub(r"//.*", "", script)
    assert "IDENT" not in code, "save() reaches for the nav IIFE's const"


def test_a_draft_with_no_meta_still_renders(tmp_path) -> None:
    # The base fixture's draft has an empty `meta` and no edges — the panel
    # must degrade to its empty states, not blow up.
    client = TestClient(
        create_app(
            runtime=FakeRuntime(SmartDraftFakeStore()),
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )
    resp = client.get("/smartdraft/sdt")
    assert resp.status_code == 200
    assert "No metadata stamped." in resp.text
    assert "No document-level links." in resp.text


def test_rename_writes_through_and_answers_json(tmp_path) -> None:
    store = MetaFakeStore()
    calls: list[tuple[int, str]] = []

    def fake_set(ref_id, title, *, source=None):
        calls.append((ref_id, title))
        return "Smartdraft reader draft", True

    store.set_draft_title = fake_set  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
        )
    )
    resp = client.post("/drafts/sdt/title", data={"title": "  A new name  "})
    assert resp.status_code == 200
    assert resp.json() == {
        "ok": True,
        "title": "A new name",
        "old": "Smartdraft reader draft",
        "heading_synced": True,
    }
    assert calls == [(700, "  A new name  ")]


def test_rename_of_a_missing_draft_is_404(meta_client: TestClient) -> None:
    resp = meta_client.post("/drafts/nope/title", data={"title": "x"})
    assert resp.status_code == 404


def test_rename_is_rejected_blank(tmp_path) -> None:
    store = MetaFakeStore()

    def fake_set(ref_id, title, *, source=None):
        raise BadInput("a draft title can't be blank")

    store.set_draft_title = fake_set  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
        )
    )
    resp = client.post("/drafts/sdt/title", data={"title": "   "})
    assert resp.status_code == 422
    assert resp.json()["ok"] is False


def test_a_draft_deleted_mid_rename_is_404_not_500(tmp_path) -> None:
    """The lookup isn't in the write transaction, so a concurrent delete
    (worker or sibling agent — routine here) surfaces as ``NotFound`` from
    the store. That must degrade to the same 404 as a missed lookup."""
    store = MetaFakeStore()

    def fake_set(ref_id, title, *, source=None):
        raise NotFound(f"no draft ref {ref_id}")

    store.set_draft_title = fake_set  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
        )
    )
    resp = client.post("/drafts/sdt/title", data={"title": "Renamed"})
    assert resp.status_code == 404
    assert resp.json()["ok"] is False


def test_rename_drops_the_node_cache(monkeypatch, tmp_path) -> None:
    """The heading is edited IN PLACE, so smartdraft's cache token (chunk
    count + max chunk_id) doesn't move — without an explicit invalidate the
    renamed heading renders stale until the 45s TTL backstop."""
    from precis_web import smartdraft as sd

    dropped: list[int] = []
    monkeypatch.setattr(sd, "invalidate", dropped.append)

    store = MetaFakeStore()
    store.set_draft_title = lambda ref_id, title, *, source=None: ("old", True)  # type: ignore[method-assign]
    client = TestClient(
        create_app(
            runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
        )
    )
    client.post("/drafts/sdt/title", data={"title": "Renamed"})
    assert dropped == [700]
