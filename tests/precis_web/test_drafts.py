"""Drafts tab routes (Tier-A web viewer/editor).

Self-contained: a draft-aware fake store (chunks / TOC / links) wrapped
in the conftest ``FakeRuntime`` + a TestClient — no Postgres. Exercises
the reader, the ``¶`` handle redirect, the chunk preview popover, and
the change-request POST.
"""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from precis.store._draft_ops import ChunkReviewEntry, DraftReviewRow
from precis.utils import handle_registry
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime, FakeStore, make_ref

_DRAFT = make_ref(id=500, kind="draft", slug="nt", title="Nano draft")


def _chunk(
    handle, kind, text, depth, chunk_id, parent_chunk_id=None, ref_id=500, meta=None
):
    return SimpleNamespace(
        handle=handle,
        chunk_kind=kind,
        text=text,
        depth=depth,
        chunk_id=chunk_id,
        parent_chunk_id=parent_chunk_id,
        ref_id=ref_id,
        meta=meta,
        # the universal dc<id> handle — smartdraft's ?focus=
        # anchor scheme, distinct from `handle`'s base-58 DOM key.
        dc=handle_registry.format_handle("draft", chunk_id, chunk=True),
    )


class _WSCursor:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _WSConn:
    """Serves ``SELECT meta FROM refs WHERE ref_id = %s`` from
    ``store._live_meta``; everything else degrades to empty (matching the
    base ``FakeStore``'s ``_FakeConn``). The base fake always returns empty
    regardless of query, which is fine for every OTHER route here, but the
    ``/workspace`` route's partial-update semantics (a field param of
    ``None`` must leave that key untouched) need a real read-your-writes
    round trip to verify."""

    def __init__(self, store: DraftFakeStore) -> None:
        self._store = store

    def execute(self, sql, params=None):
        if "SELECT meta FROM refs" in sql and params:
            meta = self._store._live_meta.get(params[0])
            return _WSCursor([(meta,)] if meta is not None else [])
        return _WSCursor([])

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None


class _WorkspacePool:
    def __init__(self, store: DraftFakeStore) -> None:
        self._store = store

    @contextmanager
    def connection(self):
        yield _WSConn(self._store)


class DraftFakeStore(FakeStore):
    blocks = property(
        lambda self: self
    )  # blocks carve: flat fake doubles as its own sub-store

    def __init__(self) -> None:
        super().__init__()
        # Mirrors refs.meta for the /workspace route's read-modify-write
        # round trip (see _WSConn above). {} for a ref not yet seeded.
        self._live_meta: dict[int, dict] = {}
        self.pool = _WorkspacePool(self)
        # BBBBBB is parented under the AAAAAA heading → ancestors=[AAAAAA],
        # so collapsing AAAAAA hides it (collapse mechanics).
        self._chunks = [
            _chunk("AAAAAA", "heading", "Nano draft", 0, chunk_id=1),
            _chunk(
                "BBBBBB",
                "paragraph",
                # smith2024 is a paper we hold (local → sky §); ghost404 is an
                # external reference (amber ↗). See DraftFakeStore.live_paper_cites.
                "Intro; see [the title](¶AAAAAA) and paper:smith2024 vs "
                "paper:ghost404. Uses PEI.",
                1,
                chunk_id=2,
                parent_chunk_id=1,
            ),
            # a figure — origin chip + <img> from /drafts/blob
            _chunk(
                "FIGFIG",
                "figure",
                "Fig 1. A diagram.",
                0,
                chunk_id=3,
                meta={"figure": {"origin": "original"}},
            ),
            # a third-party figure with a permission paper-trail (badge
            # hover popover + click-to-edit).
            _chunk(
                "FIGTPF",
                "figure",
                "Fig 2 (after Smith).",
                0,
                chunk_id=4,
                meta={
                    "figure": {
                        "origin": "third_party",
                        "permission": {
                            "publisher": "Springer Nature",
                            "permission_id": "SNCSC-2026-0451",
                            "status": "granted",
                            "granted_at": "2026-06-18",
                            "source_paper": "smith19",
                        },
                    }
                },
            ),
            # a data table — canonical meta.table + caption,
            # rendered as a real <table> (not the derived pipe markdown).
            _chunk(
                "TBLTBL",
                "table",
                "**Issue register**\n| ID | Title |\n| --- | --- |\n| I1 | alpha |",
                0,
                chunk_id=5,
                meta={
                    "table": {"header": ["ID", "Title"], "rows": [["I1", "alpha"]]},
                    "caption": "Issue register",
                },
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

    def soft_delete_draft(self, ref_id):
        self.soft_deleted_drafts = getattr(self, "soft_deleted_drafts", [])
        self.soft_deleted_drafts.append(ref_id)
        return len(self._chunks)

    def search_blocks_semantic(
        self, *, query_vec, scope_ref_id=None, limit=None, max_distance=None, **kw
    ):
        # Rank the heading ahead of the intro para (best-first), keyed by
        # chunk_id so the route's chunk_id→handle map resolves them.
        return [
            (SimpleNamespace(id=1), _DRAFT, 0.10),
            (SimpleNamespace(id=2), _DRAFT, 0.42),
        ]

    def block_views(self, ref_id, handles=None):
        # BBBBBB has a summary; the heading has neither (→ first-line).
        return {"BBBBBB": {"summary": "Intro gist.", "keywords": "pei, nano"}}

    def defined_abbrevs(self, ref_id):
        return {"PEI": "polyethyleneimine"}

    def chunk_connections(self, ref_id, handles):
        return {
            "BBBBBB": [
                {
                    "relation": "derived-from",
                    "direction": "out",
                    "kind": "memory",
                    "ident": "20",
                    "title": "A decision",
                }
            ]
        }

    def chunk_edit_stats(self, ref_id, handles):
        return {"BBBBBB": {"edits": 2, "last_at": None}}

    def live_paper_cites(self, handles, slugs):
        # smith2024 (+ the pc77 chunk) is a paper we hold; everything else —
        # e.g. ghost404 — is external. Drives the §/↗ colour split.
        local = {"smith2024", "pc77"}
        return (set(handles) | set(slugs)) & local

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

    def review_status_for_draft(self, ref_id):
        # Every chunk is reviewable-but-never-reviewed by default (mirrors
        # the real store's LEFT JOIN: no chunk_review row still yields one
        # sentinel entry per chunk, checker=None). ReviewFakeStore below
        # overrides this to actually track recorded reviews.
        return [
            DraftReviewRow(
                chunk_id=c.chunk_id,
                handle=c.handle,
                chunk_kind=c.chunk_kind,
                section_chunk_id=None,
                checker=None,
                approved_sha=None,
                verdict=None,
                at=None,
                dirty=True,
            )
            for c in self._chunks
        ]

    def review_status_for_chunk(self, chunk_id):
        return []

    def authored_provenance(self, ref_id):
        # Mirrors the real query's shape: any live chunk seeded with
        # meta.authored_by (the new-chunk stamp) appears.
        out = {}
        for c in self._chunks:
            by = (getattr(c, "meta", None) or {}).get("authored_by")
            if by:
                out[c.chunk_id] = by
        return out

    def draft_authoring_enabled(self, ref_id):
        return getattr(self, "_authoring_enabled", False)

    def universal_chunk(self, handle):
        # pc77 = a paper chunk (ref 10, ord 3); anything else unknown.
        if handle == "pc77":
            return {
                "kind": "paper",
                "ref_id": 10,
                "ord": 3,
                "chunk_kind": "paragraph",
                "text": "A cited passage about nanoscale transport.",
            }
        return None

    def get_chunk_blob(self, handle):
        if handle == "FIGFIG":
            return (b"\x89PNG\r\n\x1a\n", "image/png")
        return None

    def chunk_blob_version(self, chunk_id) -> str | None:
        # Both fixture figures are real blob-backed images (medium
        # resolver): FIGFIG (original) + FIGTPF (third-party granted).
        found = any(
            c.chunk_id == chunk_id and c.chunk_kind == "figure" for c in self._chunks
        )
        return f"fixturesha{chunk_id:04d}" if found else None

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

    def stamp_ref_meta(self, ref_id, updates, *, conn=None):
        # Records the genre/brief/voice workspace writes (the /workspace
        # route). `meta_writes` is declared on the base FakeStore.
        self.meta_writes.append((ref_id, updates))
        if "authoring_enabled" in updates:
            self._authoring_enabled = bool(updates["authoring_enabled"])
        # Mirror the real store's shallow top-level merge (`meta || updates`)
        # into `_live_meta` so a subsequent /workspace POST's read sees it.
        self._live_meta.setdefault(ref_id, {}).update(updates)


@pytest.fixture
def draft_runtime() -> FakeRuntime:
    return FakeRuntime(DraftFakeStore())


@pytest.fixture
def draft_client(draft_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(runtime=draft_runtime, web_config=WebConfig(corpus_dir=tmp_path))
    return TestClient(app)


def test_index_redirects_to_drive_kind_draft(draft_client: TestClient) -> None:
    """``/drafts`` (the list) is retired into the unified Drive surface
    (nav restructure, mirroring ``routes/papers.py``'s WS1b retirement) —
    it 307-redirects to the ``kind=draft`` facet preset. The reader
    (``/drafts/{ident}``) and "+ New draft" creation (``/drafts/new``,
    tested below and via ``test_drive_new_dropdown_offers_draft_doctype``
    in ``test_routes.py``) are unaffected."""
    r = draft_client.get("/drafts", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=draft&submitted=1"


def test_index_slash_also_redirects_to_drive(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/", follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=draft&submitted=1"


def test_index_redirect_preserves_query(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts", params={"q": "widget"}, follow_redirects=False)
    assert r.status_code in (302, 307, 308)
    assert r.headers["location"] == "/drive?k=draft&submitted=1&q=widget"


def test_new_draft_seeds_planner_prompt_and_doctype_brief(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """Creating a draft mints the project todo carrying the workspace. The
    chosen document type lands as ``meta.workspace.doc_type`` and its
    guidance IS the brief (the planner's standing ``## Project context``).
    The user's description becomes the todo body (the planner's initial
    prompt), and ``meta.llm_tier='opus'`` is the auto-run signal that starts it."""
    draft_client.post(
        "/drafts/new",
        data={
            "title": "Widget Patent",
            "doctype": "patent",
            "summary": "A widget that folds itself.",
        },
        follow_redirects=False,
    )
    verb, args = draft_runtime.calls[0]
    assert verb == "put" and args["kind"] == "todo"
    ws = args["meta"]["workspace"]
    assert ws["doc_type"] == "patent"
    # doc-type guidance is the brief — and ONLY the guidance, not the
    # description (which is the task, not standing context).
    assert "patent application" in ws["brief"].lower()
    assert "folds itself" not in ws["brief"]
    # the description is the planner's initial prompt (the todo body), and
    # meta.llm_tier is what makes the dispatcher auto-run the first tick.
    assert args["text"] == "A widget that folds itself."
    assert args["meta"]["llm_tier"] == "opus"


def test_new_draft_blank_description_rejected(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """A title alone can't drive the writer: with no description the create
    is rejected (400) and nothing is dispatched — no project todo, no
    ``meta.llm_tier='opus'`` auto-writer armed from just the title."""
    r = draft_client.post(
        "/drafts/new",
        data={"title": "Widget Patent", "doctype": "patent", "summary": ""},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert "description is required" in r.text.lower()
    # nothing was created — the guard fires before any dispatch.
    assert draft_runtime.calls == []


def test_new_draft_whitespace_description_rejected(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """A whitespace-only description is treated as blank (stripped)."""
    r = draft_client.post(
        "/drafts/new",
        data={"title": "Widget Patent", "doctype": "patent", "summary": "   \n "},
        follow_redirects=False,
    )
    assert r.status_code == 400
    assert draft_runtime.calls == []


def test_papers_zip_route_streams_zip(
    draft_client: TestClient, monkeypatch, tmp_path
) -> None:
    """``GET /papers.zip`` delegates to ``build_sources_zip`` and streams the
    result as application/zip. We stub the builder (unit-tested elsewhere) to
    keep the route test store-agnostic."""
    import precis.export.sources as src

    def _fake_zip(store, ref, out_path, **kw):
        import zipfile

        with zipfile.ZipFile(out_path, "w") as zf:
            zf.writestr("manifest.txt", "x")
        return src.ZipResult(path=out_path, bundle=src.SourceBundle())

    monkeypatch.setattr(src, "build_sources_zip", _fake_zip)
    r = draft_client.get("/drafts/nt/papers.zip")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "nt-papers.zip" in r.headers.get("content-disposition", "")


def test_edit_table_dispatches_structured_edit(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # The grid editor POSTs JSON; the route resolves the bare handle to dc<id>
    # and dispatches edit(table=…, caption=…) — single-sourced with MCP/CLI.
    r = draft_client.post(
        "/drafts/nt/table",
        json={
            "handle": "TBLTBL",
            "base_sha": "sha0",
            "header": ["element", "gap_eV"],
            "rows": [["Si", "1.12"], ["Ge", "0.67"]],
            "caption": "Band gaps",
        },
    )
    assert r.status_code == 200 and r.json()["ok"] is True
    verb, args = draft_runtime.calls[-1]
    assert verb == "edit" and args["kind"] == "draft" and args["id"] == "dc5"
    assert args["table"]["header"] == ["element", "gap_eV"]
    # numeric-looking cells coerce to numbers (numerics index); text stays text
    assert args["table"]["rows"] == [["Si", 1.12], ["Ge", 0.67]]
    assert args["caption"] == "Band gaps"
    assert args["base_sha"] == "sha0"


def test_edit_table_coerces_only_clean_numbers(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # Round-trip guard: "007" / "1e3" keep their string form (not mangled to
    # 7 / 1000.0); a blank cell becomes null; a clean float coerces.
    draft_client.post(
        "/drafts/nt/table",
        json={
            "handle": "TBLTBL",
            "header": ["a", "b", "c", "d"],
            "rows": [["007", "1e3", "", "3.5"]],
            "caption": "",
        },
    )
    _, args = draft_runtime.calls[-1]
    assert args["table"]["rows"] == [["007", "1e3", None, 3.5]]


def test_edit_table_bad_block_404(draft_client: TestClient) -> None:
    r = draft_client.post(
        "/drafts/nt/table",
        json={"handle": "NOPExx", "header": ["a"], "rows": [["1"]]},
    )
    assert r.status_code == 404


def test_edit_table_surfaces_linter_error_422(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # A handler-rejected table (ragged / empty header) bounces 422 with the
    # linter message so the grid keeps the box open. Force the edit verb to
    # fail via the fake's error_verbs hook.
    draft_runtime.error_verbs.add("edit")
    r = draft_client.post(
        "/drafts/nt/table",
        json={"handle": "TBLTBL", "header": ["a", "b"], "rows": [["1"]]},
    )
    assert r.status_code == 422
    assert r.json()["ok"] is False and "rejected by handler" in r.json()["error"]


def test_blob_route_serves_bytes_with_mime(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/blob/FIGFIG")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_blob_route_404_when_no_blob(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/blob/AAAAAA")  # a heading — no blob
    assert r.status_code == 404


def test_edit_figure_permission_dispatches_edit(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/figure/FIGTPF/permission",
        data={
            "origin": "third_party",
            "publisher": "Elsevier",
            "permission_id": "EL-999",
            "status": "granted",
            "source_paper": "jones20",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt#c-FIGTPF"
    verb, args = draft_runtime.calls[-1]
    # bare ``chunks.handle`` posted → resolved to the canonical ``dc<id>``.
    assert verb == "edit" and args["kind"] == "draft" and args["id"] == "dc4"
    assert args["origin"] == "third_party"
    assert args["permission"]["publisher"] == "Elsevier"
    assert args["permission"]["permission_id"] == "EL-999"


def test_set_workspace_writes_genre_and_brief(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # Setting genre + brief stamps meta.workspace on BOTH the draft (500)
    # and its owning project todo (1), so _doc_type (project) + the prompt
    # preview (draft) agree.
    r = draft_client.post(
        "/drafts/nt/workspace",
        data={"doctype": "report", "brief": "Be concise and concrete."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt"
    writes = draft_runtime.store.meta_writes
    targets = {rid for rid, _ in writes}
    assert targets == {500, 1}
    for _rid, updates in writes:
        ws = updates["workspace"]
        assert ws["doc_type"] == "report"
        assert ws["brief"] == "Be concise and concrete."


def test_set_workspace_rejects_unknown_genre(draft_client: TestClient) -> None:
    r = draft_client.post(
        "/drafts/nt/workspace",
        data={"doctype": "bogus", "brief": ""},
        follow_redirects=False,
    )
    assert r.status_code == 400


def test_clear_workspace_removes_keys(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # Empty doctype + brief clears both keys from the workspace.
    draft_client.post(
        "/drafts/nt/workspace",
        data={"doctype": "", "brief": ""},
        follow_redirects=False,
    )
    _rid, updates = draft_runtime.store.meta_writes[-1]
    assert "doc_type" not in updates["workspace"]
    assert "brief" not in updates["workspace"]


def test_set_workspace_partial_update_voice_only_keeps_brief(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # Posting ONLY `voice` (the style ▾ popover) must not clobber a prior
    # genre + brief (the genre ▾ popover) — a field param of `None` (not
    # present in the posted form) means "leave unchanged", not "clear".
    store = cast(DraftFakeStore, draft_runtime.store)
    for rid in (500, 1):
        store._live_meta[rid] = {
            "workspace": {"doc_type": "report", "brief": "Be concise."}
        }
    r = draft_client.post(
        "/drafts/nt/workspace",
        data={"voice": "light-hearted, colloquial, occasional puns"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    writes = draft_runtime.store.meta_writes
    targets = {rid for rid, _ in writes}
    assert targets == {500, 1}
    for _rid, updates in writes:
        ws = updates["workspace"]
        assert ws["doc_type"] == "report"
        assert ws["brief"] == "Be concise."
        assert ws["voice"] == "light-hearted, colloquial, occasional puns"


def test_set_workspace_partial_update_brief_only_keeps_voice(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # Posting ONLY `brief` (the genre ▾ popover) must not clobber a prior
    # voice (the style ▾ popover).
    store = cast(DraftFakeStore, draft_runtime.store)
    for rid in (500, 1):
        store._live_meta[rid] = {"workspace": {"voice": "wry and terse"}}
    r = draft_client.post(
        "/drafts/nt/workspace",
        data={"brief": "Focus on catalysis."},
        follow_redirects=False,
    )
    assert r.status_code == 303
    writes = draft_runtime.store.meta_writes
    for _rid, updates in writes:
        ws = updates["workspace"]
        assert ws["voice"] == "wry and terse"
        assert ws["brief"] == "Focus on catalysis."
        assert "doc_type" not in ws


def test_list_markers_numbers_olist_and_bullets_ulist() -> None:
    from precis_web.routes.drafts import _list_markers

    chunks = [
        _chunk("OL", "olist", "", 0, chunk_id=10),
        _chunk("OL1", "item", "first", 1, chunk_id=11, parent_chunk_id=10),
        _chunk("OL2", "item", "second", 1, chunk_id=12, parent_chunk_id=10),
        _chunk("UL", "ulist", "", 0, chunk_id=20),
        _chunk("UL1", "item", "alpha", 1, chunk_id=21, parent_chunk_id=20),
        _chunk("UL2", "item", "beta", 1, chunk_id=22, parent_chunk_id=20),
    ]
    marker, ordered = _list_markers(chunks)
    assert marker["OL1"] == "1." and marker["OL2"] == "2."
    assert ordered["OL1"] and ordered["OL2"]
    assert marker["UL1"] == "•" and marker["UL2"] == "•"
    assert not ordered["UL1"]


def test_list_markers_honours_olist_start_and_nesting() -> None:
    from precis_web.routes.drafts import _list_markers

    chunks = [
        _chunk("OL", "olist", "", 0, chunk_id=10, meta={"start": 5}),
        _chunk("OL1", "item", "x", 1, chunk_id=11, parent_chunk_id=10),
        # a nested olist under the first item restarts its own counter
        _chunk("NEST", "olist", "", 1, chunk_id=30, parent_chunk_id=11),
        _chunk("N1", "item", "n", 2, chunk_id=31, parent_chunk_id=30),
        _chunk("OL2", "item", "y", 1, chunk_id=12, parent_chunk_id=10),
    ]
    marker, _ = _list_markers(chunks)
    assert marker["OL1"] == "5." and marker["OL2"] == "6."
    assert marker["N1"] == "1."  # nested list restarts


# A DISTINCT draft ref (id 701, slug "lst") so its chunks never collide
# with the base "nt"/500 draft in the per-(ref_id, version) reading-order
# cache the reader shares process-wide across tests.
_LIST_DRAFT = make_ref(id=701, kind="draft", slug="lst", title="Lists")


class ListDraftStore(DraftFakeStore):
    def __init__(self) -> None:
        super().__init__()
        self._chunks = [
            _chunk("HEAD", "heading", "Lists", 0, chunk_id=701001, ref_id=701),
            _chunk(
                "OL", "olist", "list", 0, 701010, parent_chunk_id=701001, ref_id=701
            ),
            _chunk(
                "OL1", "item", "first", 1, 701011, parent_chunk_id=701010, ref_id=701
            ),
            _chunk(
                "OL2", "item", "second", 1, 701012, parent_chunk_id=701010, ref_id=701
            ),
            _chunk(
                "UL", "ulist", "list", 0, 701020, parent_chunk_id=701001, ref_id=701
            ),
            _chunk(
                "UL1", "item", "a point", 1, 701021, parent_chunk_id=701020, ref_id=701
            ),
        ]

    def get_ref(self, *, kind, id):
        if kind == "draft" and id in ("lst", 701):
            return _LIST_DRAFT
        return super().get_ref(kind=kind, id=id)

    def list_refs(self, *, kind=None, limit=50, offset=0, **kw):
        if kind == "draft":
            return [_LIST_DRAFT]
        return super().list_refs(kind=kind, limit=limit, offset=offset, **kw)

    def reading_order(self, ref_id):
        return list(self._chunks)


@pytest.fixture
def list_runtime() -> FakeRuntime:
    return FakeRuntime(ListDraftStore())


@pytest.fixture
def list_client(list_runtime: FakeRuntime, tmp_path) -> TestClient:
    app = create_app(runtime=list_runtime, web_config=WebConfig(corpus_dir=tmp_path))
    return TestClient(app)


def test_figure_upload_dispatches_put(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    import base64 as _b64

    png = b"\x89PNG\r\n\x1a\n"
    r = draft_client.post(
        "/drafts/nt/figure",
        data={"handle": "BBBBBB", "caption": "Fig 1.", "origin": "original"},
        files={"file": ("x.png", png, "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt#c-BBBBBB"
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["kind"] == "draft"
    assert args["chunk_kind"] == "figure" and args["origin"] == "original"
    assert args["image"] == _b64.b64encode(png).decode()
    assert args["at"] == {"after": "BBBBBB"}
    assert args["mime"] == "image/png"
    assert "permission" not in args


def test_figure_upload_third_party_assembles_permission(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/figure",
        data={
            "handle": "BBBBBB",
            "caption": "Fig 2 (after Smith).",
            "origin": "third_party",
            "publisher": "Springer Nature",
            "permission_id": "SNCSC-2026-0451",
            "status": "granted",
            "source_paper": "smith19",
        },
        files={"file": ("x.png", b"\x89PNG\r\n", "image/png")},
        follow_redirects=False,
    )
    assert r.status_code == 303
    _verb, args = draft_runtime.calls[-1]
    perm = args["permission"]
    assert perm["publisher"] == "Springer Nature"
    assert perm["permission_id"] == "SNCSC-2026-0451"
    assert perm["status"] == "granted" and perm["source_paper"] == "smith19"
    # blank optional fields are dropped, not sent as empty strings
    assert "expires_at" not in perm and "scope" not in perm


def test_singular_alias_redirects(draft_client: TestClient) -> None:
    r = draft_client.get("/draft/nt", follow_redirects=False)
    assert r.status_code == 307
    assert r.headers["location"] == "/smartdraft/nt"


def test_delete_draft_with_matching_name_soft_deletes(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # title is "Nano draft" — typing it deletes (soft) and lands on /drafts.
    r = draft_client.post(
        "/drafts/nt/delete", data={"confirm": "Nano draft"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts"
    assert 500 in getattr(draft_runtime.store, "soft_deleted_drafts", [])


def test_delete_draft_accepts_slug_too(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/delete", data={"confirm": "  NT "}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts"
    assert 500 in getattr(draft_runtime.store, "soft_deleted_drafts", [])


def test_delete_draft_wrong_name_does_nothing(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/delete", data={"confirm": "not the name"}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt"  # bounced back to the reader
    assert not getattr(draft_runtime.store, "soft_deleted_drafts", [])


def test_delete_draft_blank_confirm_does_nothing(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post(
        "/drafts/nt/delete", data={"confirm": ""}, follow_redirects=False
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt"
    assert not getattr(draft_runtime.store, "soft_deleted_drafts", [])


def test_chunk_handle_redirects_into_reader(draft_client: TestClient) -> None:
    r = draft_client.get("/c/BBBBBB", follow_redirects=False)
    assert r.status_code == 303
    location = r.headers["location"]
    assert location.startswith("/smartdraft/nt")
    assert "focus=" in location


def test_unknown_chunk_handle_404s(draft_client: TestClient) -> None:
    r = draft_client.get("/c/ZZZZZZ", follow_redirects=False)
    assert r.status_code == 404


def test_paper_chunk_handle_redirects_through_resolver(
    draft_client: TestClient,
) -> None:
    # /c/<pc-handle> resolves a PAPER chunk (not a draft chunk) → the /r
    # resolver at that chunk (paper → its PDF page via ?chunk=ord).
    r = draft_client.get("/c/pc77", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/r/paper/10?chunk=3"


def test_paper_chunk_preview_shows_quote(draft_client: TestClient) -> None:
    # Hovering a paper-chunk handle resolves its quote "whatever it is",
    # not a dead/missing card.
    r = draft_client.get("/preview/chunk/pc77")
    assert r.status_code == 200
    assert "A cited passage about nanoscale transport." in r.text
    # No fake "click to open →" line — it was a plain <p>, not a link;
    # the anchor the popover hangs off is the click target.
    assert "click to open" not in r.text


def test_unknown_universal_chunk_preview_is_missing(draft_client: TestClient) -> None:
    # A dangling paper-chunk handle degrades to a graceful 'missing' card.
    r = draft_client.get("/preview/chunk/pc999")
    assert r.status_code == 200
    assert "no such" in r.text


def test_chunk_preview_fragment(draft_client: TestClient) -> None:
    # A chunk hover leads with the content + a friendly *source-kind*
    # label ("draft"), not the raw handle or the machine chunk_kind
    # ("paragraph #BBBBBB") it used to show.
    r = draft_client.get("/preview/chunk/BBBBBB")
    assert r.status_code == 200
    assert "Uses PEI." in r.text  # the chunk's own text (the quote)
    assert "draft" in r.text  # friendly source-kind chip
    assert "paragraph" not in r.text  # machine chunk_kind dropped
    assert "BBBBBB" not in r.text  # raw handle no longer surfaced as id/title


def test_chunk_preview_missing_is_graceful(draft_client: TestClient) -> None:
    r = draft_client.get("/preview/chunk/ZZZZZZ")
    assert r.status_code == 200
    assert "no such" in r.text  # popover 'missing' branch


class _NoProjectStore(DraftFakeStore):
    """A draft with no live ``draft-of`` project todo (the project was never
    linked, or was soft-deleted). ``_project_id`` → None → the job anchors on
    the draft ref itself instead of 400ing."""

    def get_ref(self, *, kind, id):
        if kind == "todo" and id == 1:
            return None
        return super().get_ref(kind=kind, id=id)


def test_export_pdf_parents_on_project_when_linked(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post("/drafts/nt/export.pdf", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "draft_export"
    assert args["parent_id"] == 1  # the draft-of project todo


def test_export_pdf_falls_back_to_draft_ref_when_no_project(tmp_path) -> None:
    """A project-less draft must still export — the job anchors on the draft
    ref (a valid JOB_PARENT_KINDS member), not hard-400."""
    runtime = FakeRuntime(_NoProjectStore())
    app = create_app(runtime=runtime, web_config=WebConfig(corpus_dir=tmp_path))
    r = TestClient(app).post("/drafts/nt/export.pdf", follow_redirects=False)
    assert r.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "draft_export"
    assert args["parent_id"] == 500  # the draft ref itself


# ────────────────────────── retraction gate ───────────────────────────
# The gate consumes ``precis.export.retraction.draft_retraction_report`` /
# ``cited_paper_refs`` — stubbed here rather than exercised end-to-end
# (that module's own tests own the walk itself; a real run would need a DB
# and, for ``check=True``, a live Crossref call, which must never happen in
# a test). Stubbing at the ``precis.export.retraction`` module attribute
# works because ``routes/drafts.py`` imports these names locally inside
# each route body, so the patched binding is picked up fresh per request.

import precis.export.retraction as retraction_mod
import precis_web.routes.drafts as drafts_mod
from precis.export.retraction import CitedPaper, DraftRetractionReport


def _cited(
    slug: str,
    status: str | None,
    *,
    checked_at: object = None,
    doi: str | None = None,
    doi_status: str | None = None,
    doi_validated_at: object = None,
) -> CitedPaper:
    return CitedPaper(
        ref_id=10,
        slug=slug,
        title=f"Paper {slug}",
        status=status,
        checked_at=checked_at,
        doi=doi,
        doi_status=doi_status,
        doi_validated_at=doi_validated_at,
    )


def test_export_docx_blocked_when_cite_retracted(
    draft_client: TestClient, monkeypatch
) -> None:
    """A ``retracted`` cite hard-blocks the docx download (409), naming the
    offending paper, and the block is a pure read — no network, so the fake
    ``draft_retraction_report`` here need not (and must not) touch Crossref."""

    def fake_report(store, ref, **kw):
        assert kw.get("check", False) is False  # export never live-checks
        return DraftRetractionReport(
            papers=[_cited("smith2024", "retracted", checked_at="2026-01-01")],
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.get("/drafts/nt/export.docx")
    assert r.status_code == 409
    assert "smith2024" in r.text
    assert "retracted" in r.text.lower()
    assert "ignore_retractions" in r.text  # tells the user the override name


def test_crossref_mailto_falls_back_to_unpaywall_email(monkeypatch) -> None:
    """The retraction walk must reach Crossref's *polite* pool. Without any
    mailto a ``_RETRACTION_CHECK_CAP``-wide walk hits the throttled anonymous
    pool, overruns ``_RETRACTION_CHECK_BUDGET_S`` (measured 94s vs 13s,
    2026-08-12), and the button loops. ``PRECIS_CROSSREF_MAILTO`` wins when
    set; otherwise the already-configured ``PRECIS_UNPAYWALL_EMAIL`` is used —
    never ``None`` when either contact exists."""
    monkeypatch.delenv("PRECIS_CROSSREF_MAILTO", raising=False)
    monkeypatch.delenv("PRECIS_UNPAYWALL_EMAIL", raising=False)
    assert drafts_mod._crossref_mailto() is None

    monkeypatch.setenv("PRECIS_UNPAYWALL_EMAIL", "ops@example.org")
    assert drafts_mod._crossref_mailto() == "ops@example.org"  # fallback

    monkeypatch.setenv("PRECIS_CROSSREF_MAILTO", "crossref@example.org")
    assert drafts_mod._crossref_mailto() == "crossref@example.org"  # specific wins


def test_export_docx_override_bypasses_block(
    draft_client: TestClient, monkeypatch
) -> None:
    """``?ignore_retractions=1`` overrides the block and the export proceeds
    — a deliberate, explicit act, never a default."""
    import precis.export.docx as docx_mod

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[_cited("smith2024", "retracted", checked_at="2026-01-01")],
        )

    def fake_export_docx(store, ref, *, target_path, citations="plain", doc_type=None):
        target_path.write_bytes(b"PK\x03\x04fake-docx")
        return docx_mod.DocxResult(path=target_path, cited_slugs=["smith2024"])

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    monkeypatch.setattr(docx_mod, "export_docx", fake_export_docx)
    r = draft_client.get("/drafts/nt/export.docx?ignore_retractions=1")
    assert r.status_code == 200
    assert r.content.startswith(b"PK")


def test_export_docx_soft_status_does_not_block(
    draft_client: TestClient, monkeypatch
) -> None:
    """``corrected`` / ``expression_of_concern`` annotate but never block —
    the export proceeds with no override needed."""
    import precis.export.docx as docx_mod

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[_cited("smith2024", "corrected", checked_at="2026-01-01")],
        )

    def fake_export_docx(store, ref, *, target_path, citations="plain", doc_type=None):
        target_path.write_bytes(b"PK\x03\x04fake-docx")
        return docx_mod.DocxResult(path=target_path, cited_slugs=["smith2024"])

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    monkeypatch.setattr(docx_mod, "export_docx", fake_export_docx)
    r = draft_client.get("/drafts/nt/export.docx")
    assert r.status_code == 200


def test_export_docx_unchecked_does_not_block(
    draft_client: TestClient, monkeypatch
) -> None:
    """A cite nobody has ever checked (``checked_at is None``) is a warning,
    never a block — most cites live here under the sparse trigger model."""
    import precis.export.docx as docx_mod

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[_cited("smith2024", None, checked_at=None)]
        )

    def fake_export_docx(store, ref, *, target_path, citations="plain", doc_type=None):
        target_path.write_bytes(b"PK\x03\x04fake-docx")
        return docx_mod.DocxResult(path=target_path, cited_slugs=["smith2024"])

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    monkeypatch.setattr(docx_mod, "export_docx", fake_export_docx)
    r = draft_client.get("/drafts/nt/export.docx")
    assert r.status_code == 200


def test_export_docx_override_records_trace_in_sources_appendix(
    draft_client: TestClient, monkeypatch
) -> None:
    """``?sources=1&ignore_retractions=1`` forwards the overridden
    ``CitedPaper``(s) into ``build_sources_zip`` — the artifact-level trace
    ``docs/backlog/retraction-override-appendix-trace.md`` shipped for
    (without ``?sources=1`` there's no appendix to record it in — see the
    plain-override test above, which asserts only the log line fires)."""
    import precis.export.docx as docx_mod
    import precis.export.sources as src_mod

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[_cited("smith2024", "retracted", checked_at="2026-01-01")],
        )

    def fake_export_docx(store, ref, *, target_path, citations="plain", doc_type=None):
        target_path.write_bytes(b"PK\x03\x04fake-docx")
        return docx_mod.DocxResult(path=target_path, cited_slugs=["smith2024"])

    captured: dict[str, Any] = {}

    def fake_zip(store, ref, out_path, **kw):
        import zipfile

        captured.update(kw)
        with zipfile.ZipFile(out_path, "w") as zf:
            zf.writestr("manifest.txt", "x")
        return src_mod.ZipResult(path=out_path, bundle=src_mod.SourceBundle())

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    monkeypatch.setattr(docx_mod, "export_docx", fake_export_docx)
    monkeypatch.setattr(src_mod, "build_sources_zip", fake_zip)
    r = draft_client.get("/drafts/nt/export.docx?sources=1&ignore_retractions=1")
    assert r.status_code == 200
    assert [p.slug for p in captured["retraction_override"]] == ["smith2024"]


def test_export_pdf_blocked_when_cite_retracted(
    draft_client: TestClient, draft_runtime: FakeRuntime, monkeypatch
) -> None:
    """The PDF-job POST gates the same way: blocked, and — since the block
    fires before dispatch — no job is enqueued."""

    def fake_report(store, ref, **kw):
        assert kw.get("check", False) is False
        return DraftRetractionReport(
            papers=[_cited("smith2024", "retracted", checked_at="2026-01-01")],
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.post("/drafts/nt/export.pdf", follow_redirects=False)
    assert r.status_code == 409
    assert "smith2024" in r.text
    assert draft_runtime.calls == []  # never reached the dispatch


def test_export_pdf_override_dispatches_job(
    draft_client: TestClient, draft_runtime: FakeRuntime, monkeypatch
) -> None:
    """The ``ignore_retractions`` form field overrides the block and the
    ``draft_export`` job is enqueued as usual."""

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[_cited("smith2024", "retracted", checked_at="2026-01-01")],
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.post(
        "/drafts/nt/export.pdf",
        data={"ignore_retractions": "1"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "draft_export"
    # the job carries the override flag so it can re-derive the report and
    # record the trace in the sources appendix (see draft_export.py::_dispatch)
    assert args["params"]["ignore_retractions"] is True


def test_export_pdf_no_override_omits_ignore_retractions_param(
    draft_client: TestClient, draft_runtime: FakeRuntime, monkeypatch
) -> None:
    """A clean export (nothing retracted) never sets ``ignore_retractions``
    in the job params — the job's re-derived report would find nothing
    overridden anyway, but the param shouldn't even be there."""

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(papers=[])

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.post("/drafts/nt/export.pdf", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "draft_export"
    assert "ignore_retractions" not in args["params"]


def test_retraction_status_route_reads_only_no_network(
    draft_client: TestClient, monkeypatch
) -> None:
    """The passive status route is a pure read (``check=False``) — backs the
    export pane's "N of M never checked" warning without ever touching
    Crossref."""

    def fake_report(store, ref, **kw):
        assert "check" not in kw or kw["check"] is False
        return DraftRetractionReport(
            papers=[
                _cited("smith2024", "retracted", checked_at="2026-01-01"),
                _cited("ghost404", None, checked_at=None),
            ],
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.get("/drafts/nt/retraction-status")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["blocks_export"] is True
    assert data["total"] == 2
    assert [p["slug"] for p in data["retracted"]] == ["smith2024"]
    assert [p["slug"] for p in data["unchecked"]] == ["ghost404"]
    # The full cited-paper set (item C) — the export pane's citation-health
    # summary links its leading "N cited papers" segment to this list, not
    # just the retracted/unchecked sublists.
    assert [p["slug"] for p in data["papers"]] == ["smith2024", "ghost404"]


def test_retraction_status_route_surfaces_doi_completeness(
    draft_client: TestClient, monkeypatch
) -> None:
    """DOI completeness/validity (docs/backlog/draft-doi-completeness-check.md)
    rides the same status route JSON — missing DOI and never-validated DOI
    are both first-class, distinct buckets, and neither blocks export."""

    def fake_report(store, ref, **kw):
        return DraftRetractionReport(
            papers=[
                _cited("no-doi", None, doi=None),
                _cited(
                    "has-doi",
                    None,
                    doi="10.1/x",
                    doi_status=None,
                    doi_validated_at=None,
                ),
            ],
        )

    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.get("/drafts/nt/retraction-status")
    assert r.status_code == 200
    data = r.json()
    assert data["blocks_export"] is False
    assert [p["slug"] for p in data["missing_doi"]] == ["no-doi"]
    assert [p["slug"] for p in data["doi_unvalidated"]] == ["has-doi"]
    assert "missing DOI" in data["summary"]
    assert "DOI never validated" in data["summary"]


def test_retraction_check_route_reports_per_paper_status(
    draft_client: TestClient, monkeypatch
) -> None:
    """The watch button's route: per-paper status back as JSON. The check
    walk itself is stubbed — this test is about the route's contract, not
    Crossref (which the retraction-module's own tests cover)."""

    def fake_cited_paper_refs(store, ref, **kw):
        return (
            [
                SimpleNamespace(id=1, slug="smith2024", retraction_checked_at=None),
                SimpleNamespace(
                    id=2, slug="corrected-paper", retraction_checked_at=None
                ),
            ],
            [],
        )

    def fake_report(store, ref, *, cited_slugs=None, check=False, force=False, **kw):
        assert check is True  # the button DOES live-check
        return DraftRetractionReport(
            papers=[
                _cited("smith2024", "retracted", checked_at="2026-01-01"),
                _cited("corrected-paper", "corrected", checked_at="2026-01-01"),
            ],
            checked=True,
        )

    monkeypatch.setattr(retraction_mod, "cited_paper_refs", fake_cited_paper_refs)
    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    r = draft_client.post("/drafts/nt/retraction-check")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["checked"] is True
    assert [p["slug"] for p in data["retracted"]] == ["smith2024"]
    assert [p["slug"] for p in data["soft"]] == ["corrected-paper"]
    assert data["truncated"] is False
    assert data["checked_now"] == 2


def test_retraction_check_route_rotates_through_a_capped_draft(
    draft_client: TestClient, monkeypatch
) -> None:
    """A draft with more cites than the per-press cap must hand the walk the
    *neediest* cites, and still report on all of them.

    The bug this pins: the route used to slice ``refs[:cap]``, so every press
    re-picked the same head — those came back TTL-fresh, later presses were
    no-ops, and the tail was unreachable through the only user-facing trigger
    there is (observed on a 95-cite draft in prod, 2026-08-12).
    """
    seen: dict = {}
    now = datetime.now(UTC)

    def fake_cited_paper_refs(store, ref, **kw):
        # Head is freshly checked, tail never was — a head slice would pick
        # exactly the wrong two.
        return (
            [
                SimpleNamespace(id=1, slug="fresh-a", retraction_checked_at=now),
                SimpleNamespace(id=2, slug="fresh-b", retraction_checked_at=now),
                SimpleNamespace(id=3, slug="never-c", retraction_checked_at=None),
                SimpleNamespace(id=4, slug="never-d", retraction_checked_at=None),
            ],
            [],
        )

    def fake_report(store, ref, *, cited_slugs=None, check_slugs=None, **kw):
        seen["cited_slugs"] = list(cited_slugs or [])
        seen["check_slugs"] = list(check_slugs or [])
        return DraftRetractionReport(
            papers=[_cited(s, None, checked_at=None) for s in (cited_slugs or [])],
            checked=True,
        )

    monkeypatch.setattr(retraction_mod, "cited_paper_refs", fake_cited_paper_refs)
    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    monkeypatch.setattr(drafts_mod, "_RETRACTION_CHECK_CAP", 2)

    r = draft_client.post("/drafts/nt/retraction-check")
    assert r.status_code == 200
    data = r.json()

    # Only the never-checked pair went to Crossref...
    assert seen["check_slugs"] == ["never-c", "never-d"]
    # ...but the report — and so the pane's "N of M" prompt — covers all four.
    assert seen["cited_slugs"] == ["fresh-a", "fresh-b", "never-c", "never-d"]
    assert data["total"] == 4
    assert data["truncated"] is True
    assert data["truncated_total"] == 4
    assert data["checked_now"] == 2
    assert "press again to continue" in data["summary"]


def test_retraction_check_route_force_flag_passed_through(
    draft_client: TestClient, monkeypatch
) -> None:
    """``force=1`` reaches ``draft_retraction_report`` — otherwise pressing
    the button twice in a day is a silent no-op (TTL short-circuit)."""
    seen = {}

    def fake_cited_paper_refs(store, ref, **kw):
        return (
            [SimpleNamespace(id=1, slug="smith2024", retraction_checked_at=None)],
            [],
        )

    def fake_report(store, ref, *, cited_slugs=None, check=False, force=False, **kw):
        seen["force"] = force
        return DraftRetractionReport(
            papers=[_cited("smith2024", None, checked_at="2026-01-01")], checked=True
        )

    monkeypatch.setattr(retraction_mod, "cited_paper_refs", fake_cited_paper_refs)
    monkeypatch.setattr(retraction_mod, "draft_retraction_report", fake_report)
    draft_client.post("/drafts/nt/retraction-check", data={"force": "1"})
    assert seen["force"] is True


def test_remarkable_send_falls_back_to_draft_ref_when_no_project(
    tmp_path, monkeypatch
) -> None:
    """The reMarkable send must not 400 on a project-less draft either — same
    draft-ref fallback as export. (Credential armed via env so the gate opens.)"""
    monkeypatch.setenv("REMARKABLE_TOKEN", "test-device-token")
    runtime = FakeRuntime(_NoProjectStore())
    app = create_app(runtime=runtime, web_config=WebConfig(corpus_dir=tmp_path))
    r = TestClient(app).post("/drafts/nt/remarkable", follow_redirects=False)
    assert r.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "remarkable_send"
    assert args["parent_id"] == 500  # the draft ref itself


def test_remarkable_send_400s_without_credential(
    draft_client: TestClient, monkeypatch
) -> None:
    """No device credential → the route declines (the button is hidden, but a
    stale page must not enqueue a doomed job)."""
    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    r = draft_client.post("/drafts/nt/remarkable", follow_redirects=False)
    assert r.status_code == 400


def test_remarkable_send_threads_the_signed_in_login_into_job_params(
    draft_client: TestClient, draft_runtime: FakeRuntime, monkeypatch
) -> None:
    """A signed-in user's login rides the job so it resolves *their* paired
    device first — proves the route reads ``current_user`` and forwards it
    as ``params.user``, not just gating on a global credential."""
    from precis.users import WebUser

    def _fake_user(request):
        return WebUser(
            id=1,
            login="reto",
            abbrev="rs",
            full_name=None,
            email=None,
            disabled_at=None,
            last_login_at=None,
            created_at=None,
            updated_at=None,
        )

    monkeypatch.setattr(drafts_mod, "current_user", _fake_user)
    # The route gates with login="reto" — a per-user credential (no global
    # one) must be enough to pass, and must be what lands in job params.
    from precis import secrets as vault_mod

    monkeypatch.delenv("REMARKABLE_TOKEN", raising=False)
    monkeypatch.delenv("REMARKABLE_RMAPI_CONFIG", raising=False)
    box = {"REMARKABLE_RMAPI_CONFIG:reto": "devicetoken: t\n"}
    monkeypatch.setattr(vault_mod, "is_available", lambda n, *, store=None: n in box)

    r = draft_client.post("/drafts/nt/remarkable", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "remarkable_send"
    assert args["params"]["user"] == "reto"
    # Login-scoped idem_key — a signed-in user's send must not coalesce
    # with another user's (or the shared) send of the same draft.
    assert args["idem_key"] == "remarkable_send:nt:reto"


def test_remarkable_send_omits_user_param_when_signed_out(
    draft_client: TestClient, draft_runtime: FakeRuntime, monkeypatch
) -> None:
    """Auth off / nobody signed in → no ``params.user`` at all, same
    deployment-wide-only behaviour as before this feature existed."""
    monkeypatch.setenv("REMARKABLE_TOKEN", "test-device-token")
    r = draft_client.post("/drafts/nt/remarkable", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "put" and args["job_type"] == "remarkable_send"
    assert "user" not in args["params"]
    # Signed-out send falls back to the shared idem_key, not None/empty.
    assert args["idem_key"] == "remarkable_send:nt:shared"


# ── hand-driven working set: pen/eye marks + request-ws ──


class WsFakeStore(DraftFakeStore):
    """DraftFakeStore that actually *persists* the sticky working set meta and
    resolves ``dc<id>`` chunk handles (the real store's ``get_draft_chunk``
    contract), so the pen/eye routes round-trip."""

    def __init__(self) -> None:
        super().__init__()
        self._ref_meta: dict[int, dict] = {}

    def get_draft_chunk(self, handle, *, kind="draft"):
        from precis.utils import handle_registry

        p = handle_registry.parse(handle)
        if p and p[1]:  # dc<id> universal chunk handle → look up by chunk_id
            return next((c for c in self._chunks if c.chunk_id == p[2]), None)
        return super().get_draft_chunk(handle)

    def stamp_ref_meta(self, ref_id, updates, *, conn=None):
        super().stamp_ref_meta(ref_id, updates)  # keeps meta_writes for other tests
        self._ref_meta.setdefault(ref_id, {}).update(updates)

    def fetch_refs_by_ids(self, ids, *, include_deleted=False):
        base = super().fetch_refs_by_ids(ids, include_deleted=include_deleted)
        if 500 in ids and 500 not in base:
            base[500] = SimpleNamespace(
                id=500,
                kind="draft",
                title="Nano draft",
                meta=self._ref_meta.get(500, {}),
                deleted_at=None,
            )
        return base


def _ws_client(tmp_path):
    rt = FakeRuntime(WsFakeStore())
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=tmp_path))
    return rt, TestClient(app)


def test_marks_pen_round_trips_and_auto_opens_an_eye(tmp_path) -> None:
    _rt, client = _ws_client(tmp_path)
    r = client.post("/drafts/nt/marks", json={"op": "pen", "handles": ["dc2"]})
    assert r.status_code == 200
    marks = r.json()["marks"]
    assert "dc2" in marks["pens"]
    assert any(e["handle"] == "dc2" for e in marks["eyes"])  # pen implies eye


def test_marks_clear_wipes_the_set(tmp_path) -> None:
    _rt, client = _ws_client(tmp_path)
    client.post("/drafts/nt/marks", json={"op": "pen", "handles": ["dc2"]})
    r = client.post("/drafts/nt/marks", json={"op": "clear"})
    assert r.json()["marks"] == {"pens": [], "eyes": []}


def test_request_ws_422_without_any_marks(tmp_path) -> None:
    _rt, client = _ws_client(tmp_path)
    r = client.post("/drafts/nt/request-ws", json={"text": "tighten"})
    assert r.status_code == 422


def test_request_ws_falls_back_to_focus_anchor_when_nothing_pinned(tmp_path) -> None:
    # Nothing pinned + a focus anchor → the request still files, anchored on the
    # current para (no working_set) so the ask "just works on the current context".
    rt, client = _ws_client(tmp_path)
    r = client.post("/drafts/nt/request-ws", json={"text": "tighten", "anchor": "dc2"})
    assert r.status_code == 200 and r.json()["ok"]
    verb, args = rt.calls[-1]
    assert verb == "put"
    assert args["meta"]["anchor"] == "BBBBBB"  # dc2 → its base-58 anchor
    assert "working_set" not in args["meta"]  # anchor-only, no working set


def test_request_ws_files_todo_carrying_the_working_set(tmp_path) -> None:
    rt, client = _ws_client(tmp_path)
    client.post("/drafts/nt/marks", json={"op": "pen", "handles": ["dc2"]})
    r = client.post(
        "/drafts/nt/request-ws", json={"text": "tighten this", "model": "opus"}
    )
    assert r.status_code == 200 and r.json()["ok"]
    verb, args = rt.calls[-1]
    assert verb == "put" and args["kind"] == "todo"
    assert args["parent_id"] == 1  # the draft-of project
    assert args["meta"]["working_set"]["edit_hint"] == ["dc2"]  # the pen hint
    assert args["meta"]["anchor"] == "BBBBBB"  # dc2 → its base-58 anchor
    assert "llm_select" not in args["meta"]  # no structured knob → no dict
    assert args["prio"] == 3  # human-authored ask jumps the default queue


def test_request_ws_defaults_to_big_tier_when_model_omitted(tmp_path) -> None:
    """Product decision: default is 'big' (local-first), not the legacy
    'opus'."""
    rt, client = _ws_client(tmp_path)
    r = client.post("/drafts/nt/request-ws", json={"text": "tighten", "anchor": "dc2"})
    assert r.status_code == 200 and r.json()["ok"]
    _verb, args = rt.calls[-1]
    assert args["meta"]["llm_tier"] == "big"


def test_request_ws_threads_structured_selection_onto_llm_select(tmp_path) -> None:
    rt, client = _ws_client(tmp_path)
    r = client.post(
        "/drafts/nt/request-ws",
        json={
            "text": "tighten",
            "anchor": "dc2",
            "placement": "local",
            "reasoning": "high",
            "temperature": 0.4,
        },
    )
    assert r.status_code == 200 and r.json()["ok"]
    _verb, args = rt.calls[-1]
    assert args["meta"]["llm_select"] == {
        "placement": "local",
        "thinking": True,
        "effort": "high",
        "temperature": 0.4,
    }


def test_request_ws_surfaces_the_eye_cap_overflow_note(tmp_path) -> None:
    """gr55762: when the curated eye set overflows ``_EYE_CAP`` on save, the
    response carries the "+N more not eyed" note (not just buried in
    ``meta.working_set`` where nothing renders it to the caller)."""
    from precis_web import draft_eyes

    rt, client = _ws_client(tmp_path)
    n = draft_eyes._EYE_CAP + 5
    handles = [f"dc{9000 + i}" for i in range(n)]
    r = client.post("/drafts/nt/marks", json={"op": "eye", "handles": handles})
    assert r.status_code == 200
    r = client.post("/drafts/nt/request-ws", json={"text": "tighten"})
    assert r.status_code == 200 and r.json()["ok"]
    assert r.json()["note"] == draft_eyes.capped_note(n - draft_eyes._EYE_CAP)
    verb, args = rt.calls[-1]
    assert verb == "put"
    assert args["meta"]["working_set"]["note"] == r.json()["note"]


def test_request_ws_omits_note_when_under_the_cap(tmp_path) -> None:
    rt, client = _ws_client(tmp_path)
    client.post("/drafts/nt/marks", json={"op": "pen", "handles": ["dc2"]})
    r = client.post("/drafts/nt/request-ws", json={"text": "tighten this"})
    assert r.status_code == 200 and r.json()["ok"]
    assert "note" not in r.json()


def test_request_ws_junk_knobs_leave_llm_select_absent(tmp_path) -> None:
    """A websocket ask must not 500 on a junk knob — it just degrades to no
    ``llm_select`` at all."""
    rt, client = _ws_client(tmp_path)
    r = client.post(
        "/drafts/nt/request-ws",
        json={
            "text": "tighten",
            "anchor": "dc2",
            "placement": "moon",
            "reasoning": "extreme",
            "temperature": "hot",
        },
    )
    assert r.status_code == 200 and r.json()["ok"]
    _verb, args = rt.calls[-1]
    assert "llm_select" not in args["meta"]


# ── human sign-off checkbox (migration 0086 review ledger) ──────────


class ReviewFakeStore(WsFakeStore):
    """``WsFakeStore`` (dc<id> handle resolution) that also tracks recorded
    reviews in-memory, mirroring ``Store.record_review`` /
    ``review_status_for_{chunk,draft}``. The fake ``edit`` verb dispatch
    (``FakeRuntime.dispatch_with_status``) never touches the store — real
    write-through-the-verb behaviour is a store-level concern tested
    elsewhere — so these tests assert the *route*'s contract (dispatches the
    right ``edit`` args; reads back through the same store methods the row
    renderer uses) rather than an end-to-end DB round trip."""

    def __init__(self) -> None:
        super().__init__()
        self._reviews: dict[int, dict[str, dict]] = {}

    def record_review(self, chunk_id, checker, *, verdict="approved"):
        self._reviews.setdefault(chunk_id, {})[checker] = {
            "approved_sha": "shaX",
            "verdict": verdict,
            "at": None,
            "dirty": False,
        }
        return "shaX"

    def retract_review(self, chunk_id, checker):
        existed = checker in self._reviews.get(chunk_id, {})
        self._reviews.get(chunk_id, {}).pop(checker, None)
        return existed

    def review_status_for_chunk(self, chunk_id):
        return [
            ChunkReviewEntry(checker=checker, **status)
            for checker, status in self._reviews.get(chunk_id, {}).items()
        ]

    def review_status_for_draft(self, ref_id):
        out = []
        for c in self._chunks:
            revs = self._reviews.get(c.chunk_id, {})
            if not revs:
                out.append(
                    DraftReviewRow(
                        chunk_id=c.chunk_id,
                        handle=c.handle,
                        chunk_kind=c.chunk_kind,
                        section_chunk_id=None,
                        checker=None,
                        approved_sha=None,
                        verdict=None,
                        at=None,
                        dirty=True,
                    )
                )
                continue
            for checker, status in revs.items():
                out.append(
                    DraftReviewRow(
                        chunk_id=c.chunk_id,
                        handle=c.handle,
                        chunk_kind=c.chunk_kind,
                        section_chunk_id=None,
                        checker=checker,
                        **status,
                    )
                )
        return out


def _review_client(tmp_path):
    rt = FakeRuntime(ReviewFakeStore())
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=tmp_path))
    return rt, TestClient(app)


def test_human_review_route_dispatches_edit_verb(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/human-review", json={"dc": "dc2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    verb, args = _rt.calls[-1]
    # The web layer never calls Store.record_review directly — it always
    # goes through the edit verb, so the ledger write stays single-sourced
    # with the MCP/CLI path.
    assert verb == "edit"
    assert args == {
        "kind": "draft",
        "id": "dc2",
        "review": "human",
        "verdict": "approved",
    }
    # The fake edit dispatch only records the call (doesn't mutate the
    # store), so the freshly-read-back status is still empty here.
    assert body["review"] == {}


def test_human_review_route_accepts_base58_handle_and_custom_verdict(
    tmp_path,
) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post(
        "/drafts/nt/human-review", json={"dc": "BBBBBB", "verdict": "needs-rework"}
    )
    assert r.status_code == 200
    verb, args = _rt.calls[-1]
    assert verb == "edit"
    assert args["id"] == "dc2"  # base-58 handle normalised to the dc address
    assert args["verdict"] == "needs-rework"


def test_human_review_route_404s_for_unknown_handle(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/human-review", json={"dc": "dc999"})
    assert r.status_code == 404
    assert _rt.calls == []  # never reached the edit verb


def test_human_review_route_404s_for_missing_draft(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/no-such-draft/human-review", json={"dc": "dc2"})
    assert r.status_code == 404


# ── un-review (the review-retract door) ─────────────────────────────────


def test_retract_review_route_deletes_row_and_reverts_indicator(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    _rt.store.drafts.record_review(2, "human", verdict="approved")  # dc2 == BBBBBB
    assert [r.checker for r in _rt.store.drafts.review_status_for_chunk(2)] == ["human"]

    r = client.post("/drafts/nt/review/retract", json={"dc": "dc2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["review"] == {}
    assert body["rollup"] == {"done": 0, "total": 1}  # only BBBBBB is prose

    # A re-GET of the chunk's status shows the indicator reverted (empty).
    assert _rt.store.drafts.review_status_for_chunk(2) == []


def test_retract_review_route_defaults_checker_to_human(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    _rt.store.drafts.record_review(2, "human")
    _rt.store.drafts.record_review(2, "flow")

    r = client.post("/drafts/nt/review/retract", json={"dc": "dc2"})
    assert r.status_code == 200
    remaining = {row.checker for row in _rt.store.drafts.review_status_for_chunk(2)}
    assert remaining == {"flow"}  # only 'human' (the default) was retracted


def test_retract_review_route_retracts_named_checker(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    _rt.store.drafts.record_review(2, "human")
    _rt.store.drafts.record_review(2, "flow")

    r = client.post("/drafts/nt/review/retract", json={"dc": "dc2", "checker": "flow"})
    assert r.status_code == 200
    remaining = {row.checker for row in _rt.store.drafts.review_status_for_chunk(2)}
    assert remaining == {"human"}


def test_retract_review_route_404s_when_no_row_to_retract(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/review/retract", json={"dc": "dc2"})
    assert r.status_code == 404


def test_retract_review_route_404s_for_unknown_handle(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/review/retract", json={"dc": "dc999"})
    assert r.status_code == 404


def test_retract_review_route_404s_for_missing_draft(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/no-such-draft/review/retract", json={"dc": "dc2"})
    assert r.status_code == 404


# ── document rollup badge (item 8) — prose-only denominator ─────────────


class RollupFakeStore(ReviewFakeStore):
    """3 prose (paragraph) + 2 heading chunks — the rollup badge's
    acceptance-criterion fixture: ``N/M`` counts PROSE chunks only, so the
    2 headings never enter the denominator."""

    def __init__(self) -> None:
        super().__init__()
        self._chunks = [
            _chunk("HEAD01", "heading", "Intro", 0, chunk_id=101),
            _chunk("PARA01", "paragraph", "one", 1, chunk_id=102, parent_chunk_id=101),
            _chunk("PARA02", "paragraph", "two", 1, chunk_id=103, parent_chunk_id=101),
            _chunk("HEAD02", "heading", "Body", 0, chunk_id=104),
            _chunk(
                "PARA03", "paragraph", "three", 1, chunk_id=105, parent_chunk_id=104
            ),
        ]


def _rollup_client(tmp_path):
    rt = FakeRuntime(RollupFakeStore())
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=tmp_path))
    return rt, TestClient(app)


def test_rollup_badge_zero_of_three_then_review_complete(tmp_path) -> None:
    rt, client = _rollup_client(tmp_path)

    r = client.post("/drafts/nt/human-review", json={"dc": "dc102"})
    assert r.status_code == 200
    assert r.json()["rollup"] == {"done": 0, "total": 3}

    # The fake `edit` dispatch never mutates the store (route-contract tests
    # only — see ReviewFakeStore's docstring); approve all three prose
    # chunks directly, the way a real edit(review='human') write would land.
    for cid in (102, 103, 105):
        rt.store.drafts.record_review(cid, "human")

    r2 = client.post("/drafts/nt/human-review", json={"dc": "dc102"})
    assert r2.status_code == 200
    assert r2.json()["rollup"] == {"done": 3, "total": 3}  # review-complete


class _DatetimeReviewStore(ReviewFakeStore):
    """A review ledger that stamps a real ``datetime`` ``at`` — what the DB
    actually returns — unlike ``ReviewFakeStore``'s ``at=None``. This is the
    shape that used to 500 the reader once a chunk had been reviewed."""

    def record_review(self, chunk_id, checker, *, verdict="approved"):
        self._reviews.setdefault(chunk_id, {})[checker] = {
            "approved_sha": "shaX",
            "verdict": verdict,
            "at": datetime(2026, 7, 26, 12, 23, 34, tzinfo=UTC),
            "dirty": False,
        }
        return "shaX"


def test_fork_route_dispatches_put_copy_of(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """POST /drafts/<ident>/fork routes through the same
    ``put(copy_of=, project=)`` verb the MCP/CLI fork uses — the source
    draft as ``copy_of`` and the typed project name — then redirects (to the
    new copy when the ack names it)."""
    r = draft_client.post(
        "/drafts/nt/fork",
        data={"project": "nanobuds-review", "title": "Nanobuds (review copy)"},
        follow_redirects=False,
    )
    assert r.status_code in (302, 303)
    verb, args = draft_runtime.calls[-1]
    assert verb == "put"
    assert args["kind"] == "draft"
    assert args["copy_of"]  # the source draft's slug/id
    assert args["project"] == "nanobuds-review"
    assert args["title"] == "Nanobuds (review copy)"


def test_fork_route_blank_project_is_a_noop(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """A blank project name never dispatches the fork (the copy must bind to
    a project) — it just redirects back to the source draft."""
    r = draft_client.post(
        "/drafts/nt/fork", data={"project": "   "}, follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert r.headers["location"] == "/drafts/nt"
    assert not any(v == "put" and "copy_of" in a for v, a in draft_runtime.calls)


def test_set_authoring_route_writes_ref_meta(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """``POST /drafts/{ident}/authoring`` (3e) writes
    ``refs.meta.authoring_enabled`` via ``stamp_ref_meta``."""
    r = draft_client.post(
        "/drafts/nt/authoring", data={"enabled": "1"}, follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert draft_runtime.store.meta_writes[-1] == (500, {"authoring_enabled": True})

    r = draft_client.post(
        "/drafts/nt/authoring", data={"enabled": "0"}, follow_redirects=False
    )
    assert r.status_code in (302, 303)
    assert draft_runtime.store.meta_writes[-1] == (500, {"authoring_enabled": False})


def test_smartdraft_index_lists_drafts(draft_client: TestClient) -> None:
    # The parallel /smartdraft index lists drafts via list_refs (the same source
    # the classic /drafts index uses) — not a nonexistent list_drafts().
    r = draft_client.get("/smartdraft")
    assert r.status_code == 200
    assert "Nano draft" in r.text
    assert "/smartdraft/nt" in r.text


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


def test_ref_chips_missing_pdf_marker() -> None:
    """A cited paper whose PDF is flagged missing carries a red ▲; others don't."""
    from precis_web.routes.drafts import _ref_chips

    chips = _ref_chips("see paper:kong24 here", lambda kind, ident: ident == "kong24")
    html = str(chips[0])
    assert "&#9650;" in html  # the red triangle glyph
    assert "text-rose-600" in html
    # tooltip is present (apostrophe HTML-escaped, so match the stable prefix)
    assert 'title="PDF missing' in html
    # a present paper (predicate False) gets a plain chip, no marker
    ok = _ref_chips("see paper:kong24 here", lambda kind, ident: False)
    assert "&#9650;" not in str(ok[0])
    # no predicate at all (the historical one-arg call) also stays plain
    assert "&#9650;" not in str(_ref_chips("see paper:kong24 here")[0])


def test_ref_chips_missing_pdf_marker_on_pa_handle() -> None:
    """The ``[pa5]`` universal-handle cite form flags too (the draft's cite form)."""
    from precis_web.routes.drafts import _ref_chips

    # pa5 → ('paper', False, 5); its chip target is /r/paper/5, ident "5".
    chips = _ref_chips("[pa5]", lambda kind, ident: kind == "paper" and ident == "5")
    assert "/r/paper/5" in str(chips[0])
    assert "&#9650;" in str(chips[0])


def test_ref_chips_carry_structured_kind_discriminant() -> None:
    """gr171761: every chip is tagged with its structured ``(kind, is_chunk)``
    (:class:`precis_web.routes.drafts.RefChip`) — a caller like smartdraft's
    ``_cited_sources`` filters on that, not the rendered href. A ``§`` paper
    citation is ``kind="paper", is_chunk=False``; a ``¶`` intra-draft xref
    (not a paper citation at all) is excluded by that predicate even though
    it also navigates via an anchor."""
    from precis_web.routes.drafts import RefChip, _ref_chips

    chips = _ref_chips("see [§kong24~2] and also [¶abc123]")
    by_kind = {c.kind: c for c in chips}
    assert isinstance(by_kind["paper"], RefChip)
    assert by_kind["paper"].is_chunk is False
    # the ¶ xref is not a paper citation — the "cited sources" predicate
    # (kind == "paper" and not is_chunk) must reject it despite it also
    # rendering as an anchor chip.
    assert by_kind["chunk"].is_chunk is True
    selected = [c for c in chips if c.kind == "paper" and not c.is_chunk]
    assert len(selected) == 1
    assert selected[0] is by_kind["paper"]


def test_ref_chips_paper_chunk_handle_excluded_from_paper_source_filter() -> None:
    """A universal *chunk*-form paper handle (``pc10``) still has
    ``kind == "paper"`` but navigates into the chunk (``/c/pc10``), not a
    whole-paper view — the structured filter must key on ``is_chunk`` too,
    not just ``kind``, to match the historical ``/r/paper/`` href-only
    behaviour."""
    from precis.utils import handle_registry
    from precis_web.routes.drafts import _ref_chips

    h = handle_registry.format_handle("paper", 10, chunk=True)
    chips = _ref_chips(f"[{h}]")
    assert chips[0].kind == "paper"
    assert chips[0].is_chunk is True
    assert [c for c in chips if c.kind == "paper" and not c.is_chunk] == []


def test_provenance_state_sourced_pending_unsourced() -> None:
    """The smartdraft reader's per-paragraph grounding marker: a corpus
    paper/patent citation → "sourced"; a ``[fi<id>]`` finding (source still
    being chased) → "pending"; no citation at all → "unsourced"."""
    from precis.utils import handle_registry
    from precis_web.routes.drafts import provenance_state

    paper_chunk = handle_registry.format_handle("paper", 10, chunk=True)
    assert provenance_state(f"[{paper_chunk}] foo") == "sourced"
    finding = handle_registry.format_handle("finding", 42, chunk=False)
    assert provenance_state(f"[{finding}] foo") == "pending"
    assert provenance_state("plain text") == "unsourced"
    patent_chunk = handle_registry.format_handle("patent", 5, chunk=True)
    assert provenance_state(f"[{patent_chunk}] foo") == "sourced"


def test_provenance_state_computational_evidence() -> None:
    """qu164903 dossier audit, slice A item 1: a paragraph citing a
    simulation structure ([stNNN]) — or a calc/math/pathway record — is real
    grounding, not "cites nothing"; it must classify as "sourced", not
    the red "unsourced" bar. A handle-less numeric paragraph (no citation at
    all) must stay "unsourced"."""
    from precis.utils import handle_registry
    from precis_web.routes.drafts import provenance_state

    structure = handle_registry.format_handle("structure", 245406, chunk=False)
    assert provenance_state(f"[{structure}] the relaxed geometry") == "sourced"
    # A handle-less numeric paragraph (e.g. a bare barrier reading with no
    # citation at all) stays unsourced — the acceptance-criteria contrast.
    assert provenance_state("0.479 eV, measured twice.") == "unsourced"


def test_cited_sources_filters_by_kind_not_href_shape(monkeypatch) -> None:
    """gr171761: ``_cited_sources`` selects chips by their structured
    ``(kind, is_chunk)`` tag, not by sniffing ``'href="/r/paper/'`` out of
    the rendered HTML — so a chip whose href is constructed completely
    differently still classifies correctly as long as it's a non-chunk
    paper chip, and a same-shaped-looking href on a non-paper kind (or a
    chunk-form paper handle) is still excluded."""
    from markupsafe import Markup

    import precis_web.routes.drafts as drafts_mod
    from precis_web.routes import smartdraft
    from precis_web.routes.drafts import RefChip

    weird_paper_chip = RefChip(
        "paper", False, Markup('<a href="/totally/different/path">weird</a>')
    )
    lookalike_href_chip = RefChip(
        "memory", False, Markup('<a href="/r/paper/not-really">lookalike</a>')
    )
    chunk_paper_chip = RefChip("paper", True, Markup('<a href="/c/pc10">chunk</a>'))
    fake_chips = [weird_paper_chip, lookalike_href_chip, chunk_paper_chip]

    def _fake_ref_chips(text: str, is_missing=None) -> list[RefChip]:
        return fake_chips

    monkeypatch.setattr(drafts_mod, "_ref_chips", _fake_ref_chips)

    result = smartdraft._cited_sources(object(), "irrelevant text")
    assert result == [weird_paper_chip]


def test_cited_sources_includes_computational_evidence(monkeypatch) -> None:
    """qu164903 dossier audit, slice A item 4: the "Cited sources"
    rail widens past ``kind == "paper"`` to also surface a cited simulation
    structure / calc / math / pathway record — real evidence the writer
    grounded the paragraph in, just not a literature source."""
    from markupsafe import Markup

    import precis_web.routes.drafts as drafts_mod
    from precis_web.routes import smartdraft
    from precis_web.routes.drafts import RefChip

    paper_chip = RefChip("paper", False, Markup('<a href="/r/paper/1">p</a>'))
    structure_chip = RefChip(
        "structure", False, Markup('<a href="/r/structure/245406">st</a>')
    )
    calc_chip = RefChip("calc", False, Markup('<a href="/r/calc/9">c</a>'))
    memory_chip = RefChip("memory", False, Markup('<a href="/r/memory/5">m</a>'))
    fake_chips = [paper_chip, structure_chip, calc_chip, memory_chip]

    def _fake_ref_chips(text: str, is_missing=None) -> list[RefChip]:
        return fake_chips

    monkeypatch.setattr(drafts_mod, "_ref_chips", _fake_ref_chips)

    result = smartdraft._cited_sources(object(), "irrelevant text")
    assert result == [paper_chip, structure_chip, calc_chip]


class _FakeRef:
    def __init__(
        self,
        slug: str | None,
        pdf_sha256: str | None,
        *,
        id: int = 1,
        aliases: tuple[str, ...] = (),
    ) -> None:
        self.slug = slug
        self.pdf_sha256 = pdf_sha256
        self.id = id
        self.aliases = aliases


class _FakePaperStore:
    """A ledger-backed stand-in: ``get_ref`` returns the paper, and
    ``pdf_missing`` answers from a set of shas the (mocked) corpus-presence
    ledger reports as held-but-missing."""

    def __init__(
        self, ref: _FakeRef | None, *, missing_shas: tuple[str, ...] = ()
    ) -> None:
        self._ref = ref
        self._missing = set(missing_shas)

    def get_ref(self, *, kind: str, id: object) -> _FakeRef | None:
        assert kind == "paper"
        return self._ref

    def ref_cite_keys(self, ref_id: int) -> list[str]:
        return list(self._ref.aliases) if self._ref else []

    def pdf_missing(self, sha: str, *, ttl_days: int | None = None) -> bool:
        return sha in self._missing


def test_paper_pdf_missing() -> None:
    """Marker ⇔ held (pdf_sha256 set) AND the ledger reports it missing.

    Post-Step-2 the marker is a pure DB read (``Store.pdf_missing``): no
    corpus roots, no request-time filesystem stat."""
    from precis_web.routes.drafts import _paper_pdf_missing

    # held (pdf_sha256 set) + ledger says no node holds it → the anomaly
    held_missing = _FakePaperStore(_FakeRef("kong24", "abc"), missing_shas=("abc",))
    assert _paper_pdf_missing(held_missing, "kong24") is True
    # held but the ledger reports a fresh copy somewhere → no flag
    held_present = _FakePaperStore(_FakeRef("kong24", "abc"))
    assert _paper_pdf_missing(held_present, "kong24") is False
    # a stub (no pdf_sha256) is a known state, never the anomaly — even if a
    # stray ledger row existed for some sha
    stub = _FakePaperStore(_FakeRef("kong24", None), missing_shas=("abc",))
    assert _paper_pdf_missing(stub, "kong24") is False
    # a vanished ref asserts nothing
    assert (
        _paper_pdf_missing(_FakePaperStore(None, missing_shas=("abc",)), "5") is False
    )


def test_draft_pdf_503_without_latexmk(draft_client: TestClient, monkeypatch) -> None:
    """No TeX toolchain on the host → a friendly 503, not a 500."""
    monkeypatch.setenv("PRECIS_LATEXMK_BIN", "definitely-no-such-binary-xyz")
    r = draft_client.get("/drafts/nt/pdf", follow_redirects=False)
    assert r.status_code == 503
    assert "latexmk is not installed" in r.text


def test_draft_pdf_serves_cached(
    draft_client: TestClient, monkeypatch, tmp_path
) -> None:
    """A previously-compiled PDF for the current version is served from
    the cache without recompiling."""
    from precis_web.routes import drafts as drafts_mod

    monkeypatch.setattr(
        drafts_mod, "_pdf_cache_dir", lambda ref_id, version, *, sources=False: tmp_path
    )
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4 fake\n%%EOF\n")
    r = draft_client.get("/drafts/nt/pdf", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


def test_pdf_cache_token_includes_ref_updated_at(monkeypatch) -> None:
    """Regression: the PDF cache token folds in the ref's ``updated_at``,
    not just the chunk-level version. A metadata-only edit (setting the
    author via the Authors panel bumps ``refs.updated_at`` but emits no
    ``chunk_event``) must bust the cache — else the stale pre-edit PDF, with
    the fallback ``precis`` byline, is served for the new author."""
    from datetime import datetime

    from precis_web.routes import drafts as drafts_mod

    monkeypatch.setattr(drafts_mod, "_draft_version", lambda store, ref_id: 42)
    ref0 = SimpleNamespace(id=500, updated_at=datetime(2026, 7, 7, 10, tzinfo=UTC))
    ref1 = SimpleNamespace(id=500, updated_at=datetime(2026, 7, 8, 0, 9, tzinfo=UTC))

    tok0 = drafts_mod._pdf_cache_token(None, ref0)
    assert tok0.startswith("42.")  # chunk version still present
    assert tok0 != drafts_mod._pdf_cache_token(None, ref1)  # later edit → new token
    # a missing updated_at degrades to a stable ".0" suffix, never raises
    no_ts = SimpleNamespace(id=1, updated_at=None)
    assert drafts_mod._pdf_cache_token(None, no_ts) == "42.0"


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


def test_parse_author_lines_pipe_delimited() -> None:
    from precis_web.routes.drafts import _parse_author_lines

    text = (
        "Doe, Jane | MIT | https://ror.org/x\n"
        "Roe, John | Caltech\n"
        "Solo Author\n"
        "   \n"  # blank → dropped
        " | Orphan Affil"  # no name → dropped
    )
    assert _parse_author_lines(text) == [
        {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
        {"name": "Roe, John", "affiliation": "Caltech"},
        {"name": "Solo Author"},
    ]


def test_draft_author_lines_round_trips() -> None:
    from precis_web.routes.drafts import _draft_author_lines, _parse_author_lines

    ref = make_ref(
        kind="draft",
        slug="nt",
        authors=[
            {"name": "Doe, Jane", "affiliation": "MIT", "ror": "https://ror.org/x"},
            {"name": "Roe, John", "affiliation": "Caltech"},
            {"name": "Solo Author"},
        ],
    )
    lines = _draft_author_lines(ref)
    assert lines.splitlines()[0] == "Doe, Jane | MIT | https://ror.org/x"
    assert lines.splitlines()[1] == "Roe, John | Caltech"
    assert lines.splitlines()[2] == "Solo Author"
    # editing round-trips back to the same entries
    assert _parse_author_lines(lines) == ref.authors


def test_draft_author_lines_empty() -> None:
    from precis_web.routes.drafts import _draft_author_lines

    assert _draft_author_lines(make_ref(kind="draft", authors=None)) == ""


# ── lens-run endpoint (the incremental review fanout) ──────────────────
#
# The fanout's own minting behaviour (only_dirty, subtree scope,
# unsettled-skip, lens x chunk-kind mapping) is unit-tested against a real
# store in tests/test_review_fanout_writeback.py; these assert the ROUTE's
# contract — it resolves dc/lens/only_dirty and calls
# quest.review_fanout.mint_review_fanout with the right arguments (incl.
# the structural/deep_review alias mapping and the scoped-toc 400) — via a
# monkeypatched fanout that just records its call.


def _fake_fanout_recorder(calls: list[dict[str, Any]]):
    def _fanout(
        store,
        ref_id,
        *,
        lenses,
        doc_lenses,
        author=False,
        only_dirty=False,
        scope=None,
    ):
        calls.append(
            {
                "ref_id": ref_id,
                "lenses": lenses,
                "doc_lenses": doc_lenses,
                "only_dirty": only_dirty,
                "scope": scope,
            }
        )
        return {
            "parent_id": 1,
            "minted": [42],
            "skipped": 0,
            "unsettled_skipped": 0,
            "author_minted": 0,
            "chunks_seen": 1,
        }

    return _fanout


def test_review_route_alias_maps_and_scopes_to_dc_chunk(tmp_path, monkeypatch) -> None:
    import precis_web.routes.drafts as drafts_mod

    _rt, client = _review_client(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(drafts_mod, "mint_review_fanout", _fake_fanout_recorder(calls))

    r = client.post("/drafts/nt/review", json={"lens": "structural", "dc": "dc2"})
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["minted"] == [42]
    assert len(calls) == 1
    call = calls[0]
    assert call["ref_id"] == 500
    assert call["lenses"] == ("structure",)  # 'structural' alias -> 'structure'
    assert call["doc_lenses"] == ()
    assert call["scope"] == 2  # dc2 -> chunk_id 2
    assert call["only_dirty"] is False  # a dc-scoped call defaults to re-run


def test_review_route_deep_review_alias_maps_to_adversarial(
    tmp_path, monkeypatch
) -> None:
    import precis_web.routes.drafts as drafts_mod

    _rt, client = _review_client(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(drafts_mod, "mint_review_fanout", _fake_fanout_recorder(calls))

    r = client.post("/drafts/nt/review", json={"lens": "deep_review"})
    assert r.status_code == 200
    assert calls[0]["lenses"] == ("adversarial",)
    assert calls[0]["scope"] is None  # no dc -> whole draft
    assert calls[0]["only_dirty"] is True  # whole-draft call defaults to incremental


def test_review_route_all_lens_whole_draft_includes_doc_lenses(
    tmp_path, monkeypatch
) -> None:
    import precis_web.routes.drafts as drafts_mod
    from precis.quest.review_fanout import ALL_LENSES, DOC_LENSES

    _rt, client = _review_client(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(drafts_mod, "mint_review_fanout", _fake_fanout_recorder(calls))

    r = client.post("/drafts/nt/review", json={"lens": "all"})
    assert r.status_code == 200
    assert calls[0]["lenses"] == ALL_LENSES
    assert calls[0]["doc_lenses"] == DOC_LENSES
    assert calls[0]["scope"] is None


def test_review_route_all_lens_scoped_still_passes_doc_lenses_through(
    tmp_path, monkeypatch
) -> None:
    # mint_review_fanout itself gates doc_lenses to scope=None (a no-op for a
    # scoped call) — the route doesn't need to special-case this, just pass
    # both through.
    import precis_web.routes.drafts as drafts_mod
    from precis.quest.review_fanout import ALL_LENSES, DOC_LENSES

    _rt, client = _review_client(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(drafts_mod, "mint_review_fanout", _fake_fanout_recorder(calls))

    r = client.post("/drafts/nt/review", json={"lens": "all", "dc": "dc2"})
    assert r.status_code == 200
    assert calls[0]["lenses"] == ALL_LENSES
    assert calls[0]["doc_lenses"] == DOC_LENSES
    assert calls[0]["scope"] == 2


def test_review_route_toc_lens_scoped_to_dc_is_rejected_400(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/review", json={"lens": "toc", "dc": "dc2"})
    assert r.status_code == 400


def test_review_route_toc_lens_whole_draft_mints_doc_lens_only(
    tmp_path, monkeypatch
) -> None:
    import precis_web.routes.drafts as drafts_mod

    _rt, client = _review_client(tmp_path)
    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(drafts_mod, "mint_review_fanout", _fake_fanout_recorder(calls))

    r = client.post("/drafts/nt/review", json={"lens": "toc"})
    assert r.status_code == 200
    assert calls[0]["lenses"] == ()
    assert calls[0]["doc_lenses"] == ("toc",)


def test_review_route_unknown_lens_400(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/review", json={"lens": "bogus"})
    assert r.status_code == 400


def test_review_route_unknown_dc_404(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/nt/review", json={"lens": "flow", "dc": "dc999"})
    assert r.status_code == 404


def test_review_route_missing_draft_404(tmp_path) -> None:
    _rt, client = _review_client(tmp_path)
    r = client.post("/drafts/no-such-draft/review", json={"lens": "flow"})
    assert r.status_code == 404


# ── convert to living cites (item 5b) — real-store integration, injected
# cascade fns mirroring tests/test_taproot_backfill.py's pattern ────────


@pytest.fixture
def convert_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _extract_const(sentence: str):
    """Fake ``ExtractFn``: a single atomic claim, no compound — mirrors a
    real :func:`~precis.taproot.canon.extract_claim` result for a chunk
    that decomposes to exactly one atom (``ClaimExtraction`` is the
    real-world return shape; these routes never see decomposition)."""
    from precis.taproot.canon import CanonicalClaim, ClaimExtraction

    return lambda span: ClaimExtraction(
        atoms=(CanonicalClaim(sentence=sentence, scope={}),),
        compound=None,
        not_claims=(),
    )


def _block_none(claim, store, embedder):
    return []


def _seed_convert_draft(runtime_with_store) -> tuple[str, str]:
    """A draft ``cvdraft`` with one paragraph citing ``[pc<chunk>]`` on a
    seeded paper chunk; returns ``(draft slug, paragraph dc<id> handle)``."""
    from precis.handlers.draft import DraftHandler
    from tests.workers._helpers import seed_chunk, seed_ref

    store = runtime_with_store.hub.store
    paper = seed_ref(store, title="src paper", kind="paper")
    # Real body prose: a two-word stub reads as title/author front matter and
    # is refused as evidence grounding (gripe 245842).
    pc = seed_chunk(
        store,
        ref_id=paper,
        text="The measured ribbons remain semiconducting at room temperature.",
    )

    proj = store.insert_ref(kind="todo", slug=None, title="Proj").id
    draft = DraftHandler(hub=runtime_with_store.hub)
    draft.put(id="cvdraft", title="T", project=proj)
    draft_ref = store.get_ref(kind="draft", id="cvdraft")
    title_h = store.drafts.reading_order(draft_ref.id)[0].handle
    draft.put(
        id="cvdraft",
        chunk_kind="paragraph",
        text=f"Ribbons are semiconducting [pc{pc}].",
        at={"after": "¶" + title_h},
    )
    para = store.drafts.reading_order(draft_ref.id)[-1]
    return "cvdraft", para.dc


def test_convert_cites_dry_run_then_apply_stales_approval(
    convert_client: TestClient, runtime_with_store, monkeypatch
) -> None:
    import precis_web.routes.drafts as drafts_mod

    monkeypatch.setattr(
        drafts_mod,
        "_backfill_extract_claim",
        _extract_const("Ribbons are semiconducting."),
    )
    monkeypatch.setattr(drafts_mod, "_backfill_block", _block_none)

    slug, dc = _seed_convert_draft(runtime_with_store)
    store = runtime_with_store.hub.store
    chunk_id = int(dc[2:])

    # Human-approve the chunk at its pre-convert sha.
    store.drafts.record_review(chunk_id, "human")
    assert store.drafts.review_status_for_chunk(chunk_id)[0].dirty is False

    # dry_run=True (also the default): a preview, nothing written.
    r = convert_client.post(
        f"/drafts/{slug}/cites/convert", json={"dc": dc, "dry_run": True}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dry_run"] is True
    assert body["chunks"][0]["groups"][0]["action"] == "new"
    assert store.drafts.get_draft_chunk(dc).text.startswith(
        "Ribbons are semiconducting [pc"
    )
    assert store.drafts.review_status_for_chunk(chunk_id)[0].dirty is False  # unchanged

    # apply: rewrites [pc<id>] -> [fi<hub>] through the normal edit door.
    r2 = convert_client.post(
        f"/drafts/{slug}/cites/convert", json={"dc": dc, "dry_run": False}
    )
    assert r2.status_code == 200
    body2 = r2.json()
    assert body2["dry_run"] is False
    rewritten = body2["chunks"][0]["rewritten_text"]
    assert rewritten is not None and "[fi" in rewritten
    assert store.drafts.get_draft_chunk(dc).text == rewritten

    # Acceptance criterion: the chunk's approval is now stale (content_sha
    # bumped through the edit door).
    assert store.drafts.review_status_for_chunk(chunk_id)[0].dirty is True


def test_convert_cites_dry_run_default_true(
    convert_client: TestClient, runtime_with_store, monkeypatch
) -> None:
    import precis_web.routes.drafts as drafts_mod

    monkeypatch.setattr(
        drafts_mod, "_backfill_extract_claim", _extract_const("A claim.")
    )
    monkeypatch.setattr(drafts_mod, "_backfill_block", _block_none)

    slug, dc = _seed_convert_draft(runtime_with_store)
    store = runtime_with_store.hub.store
    before = store.drafts.get_draft_chunk(dc).text

    r = convert_client.post(f"/drafts/{slug}/cites/convert", json={"dc": dc})
    assert r.status_code == 200
    assert r.json()["dry_run"] is True
    assert store.drafts.get_draft_chunk(dc).text == before  # nothing written


def test_convert_cites_unknown_dc_404(convert_client: TestClient) -> None:
    r = convert_client.post(
        "/drafts/no-such-draft/cites/convert", json={"dc": "dc999999"}
    )
    assert r.status_code == 404
