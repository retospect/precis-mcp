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

from datetime import UTC, datetime

from fastapi.testclient import TestClient

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
                # smith2024 is a paper we hold (local → sky §); ghost404 is an
                # external reference (amber ↗). See DraftFakeStore.live_paper_cites.
                "Intro; see [the title](¶AAAAAA) and paper:smith2024 vs "
                "paper:ghost404. Uses PEI.",
                1,
                chunk_id=2,
                parent_chunk_id=1,
            ),
            # a figure (ADR 0034) — origin chip + <img> from /drafts/blob
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
            # a data table (ADR 0035 §1) — canonical meta.table + caption,
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
            {
                "chunk_id": c.chunk_id,
                "handle": c.handle,
                "chunk_kind": c.chunk_kind,
                "checker": None,
                "approved_sha": None,
                "verdict": None,
                "at": None,
                "dirty": True,
            }
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

    def has_chunk_blob(self, chunk_id) -> bool:
        # Both fixture figures are real blob-backed images (ADR 0058 medium
        # resolver): FIGFIG (original) + FIGTPF (third-party granted).
        return any(
            c.chunk_id == chunk_id and c.chunk_kind == "figure" for c in self._chunks
        )

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
        # Records the genre/brief workspace writes (the /workspace route).
        # `meta_writes` is declared on the base FakeStore.
        self.meta_writes.append((ref_id, updates))
        if "authoring_enabled" in updates:
            self._authoring_enabled = bool(updates["authoring_enabled"])


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
    prompt), and ``LLM:opus`` is the auto-run signal that starts it."""
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
    # the LLM tag is what makes the dispatcher auto-run the first tick.
    assert args["text"] == "A widget that folds itself."
    assert "LLM:opus" in args["tags"]
    assert "level:strategic" in args["tags"]


def test_new_draft_blank_description_falls_back(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """With no description, the todo body falls back to a bare instruction
    so the planner still has something to act on."""
    draft_client.post(
        "/drafts/new",
        data={"title": "Widget Patent", "doctype": "patent", "summary": ""},
        follow_redirects=False,
    )
    _, args = draft_runtime.calls[0]
    assert args["text"] == 'Write a patent titled "Widget Patent".'
    assert "LLM:opus" in args["tags"]


def test_reader_stamps_last_viewed(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """Opening the reader stamps the draft's access (drives the drafts
    list's most-recently-opened order). The full page-load does it; the
    poll/skeleton/version endpoints must not."""
    store = draft_runtime.store
    assert store.viewed == []
    assert draft_client.get("/drafts/nt").status_code == 200
    assert store.viewed == [500]
    # the live-poll endpoints don't re-stamp (else an open tab pins it).
    draft_client.get("/drafts/nt/skeleton")
    draft_client.get("/drafts/nt/version")
    assert store.viewed == [500]


def test_reader_renders_per_block_grid(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    # one row per block, anchored by handle
    assert 'id="c-AAAAAA"' in r.text and 'id="c-BBBBBB"' in r.text
    # raw source linkified in the content column: paper ref → resolver,
    # ¶ ref → chunk route
    assert "/r/paper/smith2024" in r.text
    assert 'href="/c/AAAAAA"' in r.text
    # collapse mechanics (vanilla, imperative — no per-node Alpine binding):
    # the heading carries data-heading + a data-toggle caret; BBBBBB carries
    # its ancestor heading in data-anc so the JS hides it when AAAAAA
    # collapses. The anc JSON MUST be single-quoted (tojson emits double
    # quotes; a double-quoted attribute would terminate mid-array).
    assert 'data-heading="AAAAAA"' in r.text
    assert 'data-toggle="AAAAAA"' in r.text
    assert "collapse all" in r.text
    assert "data-anc='[\"AAAAAA\"]'" in r.text  # BBBBBB's ancestors json
    assert 'data-anc="["AAAAAA"]"' not in r.text
    # the old Alpine per-node collapse binding is gone (the 10k-block lag)
    assert "x-show='vis(" not in r.text
    # rows are tagged for the collapse/observer machinery
    assert "dr-block" in r.text
    # per-block change box posts to the anchored-todo route
    assert 'action="/drafts/nt/request"' in r.text
    # ADR 0036: the gray per-block indicator shows the universal handle
    # (dc<chunk_id>), not the legacy ¶<base58> anchor. The DOM/nav key stays
    # the base-58 handle (id="c-…"), so only the visible label changes.
    assert ">dc1<" in r.text and ">dc2<" in r.text
    assert ">¶AAAAAA<" not in r.text
    # Compact-mode fix: a ref hover-popover must escape the scrolled meta
    # column (overflow-y:auto clips X too) so it pops over the change column
    # rather than hiding under it. The :has() rule lifts the clipping while a
    # popover is open.
    assert '.ref-popover:not([style*="display: none"])' in r.text


def test_citation_colour_splits_local_vs_external(draft_client: TestClient) -> None:
    """Compact paper cites colour local (in-corpus) vs external: a paper we
    hold renders a sky ``§``, an external reference an amber ``↗`` — so a
    reader sees at a glance which citations are grounded (the color-pc-refs
    feature). BBBBBB cites smith2024 (local) and ghost404 (external)."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    block = r.text.split('id="c-BBBBBB"', 1)[1].split('id="c-', 1)[0]
    # local cite → sky § anchor (class precedes href; glyph is the anchor body)
    smith = block.split('href="/r/paper/smith2024"', 1)
    assert len(smith) == 2, "smith2024 cite not rendered"
    assert "text-sky-700" in smith[0].rsplit("<a ", 1)[1]
    assert smith[1].split("</a>", 1)[0].endswith(">§")
    # external cite → amber ↗ anchor
    ghost = block.split('href="/r/paper/ghost404"', 1)
    assert len(ghost) == 2, "ghost404 cite not rendered"
    assert "text-amber-600" in ghost[0].rsplit("<a ", 1)[1]
    assert ghost[1].split("</a>", 1)[0].endswith(">↗")


def test_reader_has_include_sources_controls(draft_client: TestClient) -> None:
    """The export toolbar offers the include-referenced-sources checkbox and
    the download-papers zip link (the two new affordances)."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'x-model="withSources"' in r.text
    assert "/drafts/nt/papers.zip" in r.text
    assert 'name="sources"' in r.text  # threaded into the export→project form


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


def test_figure_renders_img_and_origin_chip(draft_client: TestClient) -> None:
    # ADR 0034 — a figure block renders an <img> pointed at the blob route,
    # an origin chip, and a clearance badge (original ⇒ cleared).
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'src="/drafts/blob/FIGFIG"' in r.text
    assert "original" in r.text and "cleared" in r.text


def test_table_renders_as_html_table(draft_client: TestClient) -> None:
    # ADR 0035 §1 — a chunk_kind='table' renders as a real <table> with a
    # header row + the caption, not the raw pipe markdown.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'id="c-TBLTBL"' in r.text
    # isolate the table block's content column (up to its raw "cookie")
    block = r.text.split('id="c-TBLTBL"', 1)[1].split('x-show="raw"', 1)[0]
    assert "<table" in block and "<thead>" in block
    assert "<th" in block and ">ID<" in block and ">Title<" in block
    assert ">I1<" in block and ">alpha<" in block
    assert "Issue register" in block
    # the rendered body is a real table, not the dumped pipe markdown
    assert "| ID | Title |" not in block


def test_table_offers_grid_editor(draft_client: TestClient) -> None:
    # ADR 0035 §1 — the table block carries the ⊞ grid editor (tableEditor
    # scope + edit button), the structured-only edit affordance.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    block = r.text.split('id="c-TBLTBL"', 1)[1].split('x-show="raw"', 1)[0]
    assert "tableEditor(" in block
    assert "⊞ edit table" in block
    # the editor seeds from the canonical data (header names present in x-data)
    assert '"ID"' in block and '"Title"' in block


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


def test_reader_shows_all_clear_note(draft_client: TestClient) -> None:
    # Both fixture figures are cleared (original + granted third-party), so
    # the end-of-document all-clear note shows and no warning banner.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "cleared to ship" in r.text
    assert "not cleared to ship" not in r.text


def test_blob_route_serves_bytes_with_mime(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/blob/FIGFIG")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    assert r.content.startswith(b"\x89PNG")


def test_blob_route_404_when_no_blob(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/blob/AAAAAA")  # a heading — no blob
    assert r.status_code == 404


def test_figure_permission_popover_and_edit_form(draft_client: TestClient) -> None:
    # The third-party figure's badge shows the paper-trail (hover popover)
    # and a prefilled edit form posting to the permission edit route.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    # provenance details visible (popover)
    assert "Springer Nature" in r.text and "SNCSC-2026-0451" in r.text
    assert "2026-06-18" in r.text and "smith19" in r.text
    # click-to-edit form points at the edit route, prefilled
    assert 'action="/drafts/nt/figure/FIGTPF/permission"' in r.text
    assert 'value="SNCSC-2026-0451"' in r.text


def test_upload_form_has_field_legends(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert "Date requested" in r.text and "Date granted" in r.text
    assert "Publisher permission" in r.text


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


def test_set_section_style_dispatches_edit(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    # The per-heading "style ▾" dropdown → edit(kind='draft', style=…) (ADR 0037).
    r = draft_client.post(
        "/drafts/nt/style",
        data={"handle": "AAAAAA", "style": "patent-claim"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt#c-AAAAAA"
    verb, args = draft_runtime.calls[-1]
    assert verb == "edit"
    # bare ``chunks.handle`` posted → resolved to the canonical ``dc<id>``.
    assert args["kind"] == "draft" and args["id"] == "dc1"
    assert args["style"] == "patent-claim"


def test_clear_section_style_dispatches_empty(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    draft_client.post(
        "/drafts/nt/style",
        data={"handle": "AAAAAA", "style": ""},
        follow_redirects=False,
    )
    verb, args = draft_runtime.calls[-1]
    assert verb == "edit" and args["style"] == ""


def test_reader_has_genre_editor(draft_client: TestClient) -> None:
    # The header carries the post-creation genre + project-context editor.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'action="/drafts/nt/workspace"' in r.text
    assert 'name="doctype"' in r.text and 'name="brief"' in r.text


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


def test_set_list_kind_dispatches_edit(
    list_client: TestClient, list_runtime: FakeRuntime
) -> None:
    r = list_client.post(
        "/drafts/lst/listkind",
        data={"handle": "OL", "kind": "olist"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/lst#c-OL"
    verb, args = list_runtime.calls[-1]
    assert verb == "edit"
    # The reader posts the bare ``chunks.handle``; the route resolves it to
    # the canonical ``dc<chunk_id>`` address edit(kind='draft') requires.
    assert args["kind"] == "draft" and args["id"] == "dc701010"
    assert args["list_kind"] == "olist"


def test_dissolve_list_redirects_to_top(
    list_client: TestClient, list_runtime: FakeRuntime
) -> None:
    # A 'normal' dissolve retires the container, so we don't anchor at it.
    r = list_client.post(
        "/drafts/lst/listkind",
        data={"handle": "OL", "kind": "normal"},
        follow_redirects=False,
    )
    assert r.headers["location"] == "/drafts/lst"
    _verb, args = list_runtime.calls[-1]
    assert args["id"] == "dc701010" and args["list_kind"] == "normal"


def test_reader_renders_list_markers_and_toggle(list_client: TestClient) -> None:
    r = list_client.get("/drafts/lst")
    assert r.status_code == 200
    # ordered items numbered, unordered bulleted
    assert ">1.<" in r.text and ">2.<" in r.text
    assert ">•<" in r.text
    # container rows name the list type and host the ul/ol/normal toggle
    assert "numbered list" in r.text and "bullet list" in r.text
    assert 'action="/drafts/lst/listkind"' in r.text


def test_reader_has_add_figure_control(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'action="/drafts/nt/figure"' in r.text
    assert 'enctype="multipart/form-data"' in r.text
    assert 'name="origin"' in r.text  # the origin selector


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


def test_small_draft_renders_fully_in_the_window(draft_client: TestClient) -> None:
    # The fixture has fewer blocks than INITIAL_WINDOW, so every block is in
    # the server-rendered window — full content, and the spacer is zero.
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    # the virtual-scroll shell is present
    assert (
        'id="dr-win"' in r.text and 'id="dr-top"' in r.text and 'id="dr-bot"' in r.text
    )
    assert 'id="dr-skel"' in r.text  # embedded skeleton
    # full content present (linkified ref proves the row is server-rendered)
    assert "/r/paper/smith2024" in r.text
    # nothing off-window → bottom spacer is zero height
    assert 'id="dr-bot" style="height:0px"' in r.text


def test_reader_windows_only_first_blocks(
    draft_client: TestClient, monkeypatch
) -> None:
    # Shrink the initial window to 1: only the first block (the AAAAAA
    # heading) is server-rendered in #dr-win; the rest live ONLY in the
    # skeleton (no DOM node — the whole point), with a non-zero bottom
    # spacer reserving their space.
    from precis_web.routes import drafts as drafts_mod

    monkeypatch.setattr(drafts_mod, "INITIAL_WINDOW", 1)
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    # first block is a real server-rendered row (its change box is present)
    assert 'id="c-AAAAAA"' in r.text
    win = r.text.split('id="dr-win"', 1)[1].split('id="dr-bot"', 1)[0]
    assert 'action="/drafts/nt/request"' in win  # the one window row is hydrated
    # BBBBBB is NOT a DOM node — it lives in the skeleton JSON only
    assert 'id="c-BBBBBB"' not in r.text
    assert '"h": "BBBBBB"' in r.text or '"h":"BBBBBB"' in r.text
    # bottom spacer reserves the off-window blocks (non-zero height)
    assert 'id="dr-bot" style="height:0px"' not in r.text


def test_skeleton_endpoint_returns_blocks_and_version(draft_client: TestClient) -> None:
    # The live poll refetches this to re-window after an edit.
    r = draft_client.get("/drafts/nt/skeleton")
    assert r.status_code == 200
    data = r.json()
    assert "version" in data
    handles = [b["h"] for b in data["skeleton"]]
    assert handles == ["AAAAAA", "BBBBBB", "FIGFIG", "FIGTPF", "TBLTBL"]
    # BBBBBB is nested under the AAAAAA heading (collapse ancestry preserved)
    bbb = next(b for b in data["skeleton"] if b["h"] == "BBBBBB")
    assert bbb["anc"] == ["AAAAAA"]


def test_skeleton_carries_view_aware_short_estimate(draft_client: TestClient) -> None:
    # The summary / keywords views collapse each body block to one line, so
    # the scroller needs a *short* per-view estimate (estS) — without it the
    # body-length estimate over-reserves space and the bottom of the doc is
    # stranded behind a giant spacer (the "doesn't scroll in summary mode"
    # bug). Headings / figures render identically across views → estS == est.
    data = draft_client.get("/drafts/nt/skeleton").json()
    blocks = {b["h"]: b for b in data["skeleton"]}
    for b in blocks.values():
        assert "estS" in b
    bbb = blocks["BBBBBB"]  # a body paragraph
    assert bbb["estS"] < bbb["est"]  # collapses to one line in summary/keywords
    aaa = blocks["AAAAAA"]  # a heading
    assert aaa["estS"] == aaa["est"]
    fig = blocks["FIGFIG"]  # a figure
    assert fig["estS"] == fig["est"]


def test_row_route_hydrates_a_windowed_block(draft_client: TestClient) -> None:
    # The fragment the scroller fetches as a block enters the window — the
    # full, enriched row for one block (linkified refs + change box).
    r = draft_client.get("/drafts/nt/row/BBBBBB")
    assert r.status_code == 200
    assert 'id="c-BBBBBB"' in r.text
    assert "/r/paper/smith2024" in r.text  # linkified
    assert 'action="/drafts/nt/request"' in r.text  # change box
    assert 'id="c-AAAAAA"' not in r.text  # only the one block


def test_rows_batch_hydrates_multiple_blocks_in_one_request(
    draft_client: TestClient,
) -> None:
    # The reader hydrates a whole window of placeholders in ONE request
    # (?handles=a,b) instead of one HTTP per block. Rows come back in
    # document order, no page chrome.
    r = draft_client.get("/drafts/nt/rows?handles=BBBBBB,AAAAAA")
    assert r.status_code == 200
    assert 'id="c-AAAAAA"' in r.text and 'id="c-BBBBBB"' in r.text
    assert "/r/paper/smith2024" in r.text  # BBBBBB hydrated + linkified
    assert "<h1" not in r.text and "draftDoc(" not in r.text
    # a figure block is not in the batch
    assert 'id="c-FIGFIG"' not in r.text


def test_reader_has_delete_button_and_name_confirm(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'action="/drafts/nt/delete"' in r.text
    assert 'name="confirm"' in r.text
    # the form prompts for the draft's title
    assert "Type the draft name" in r.text


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
    assert r.headers["location"] == "/drafts/nt#c-BBBBBB"


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
    assert args["meta"]["anchor"] == "BBBBBB"


def test_change_request_omits_parent_when_project_soft_deleted(tmp_path) -> None:
    """A draft whose ``draft-of`` project todo was soft-deleted must NOT
    parent the change request on the dead todo (``put`` rejects a
    soft-deleted ``parent_id`` with NotFound). ``_project_id`` skips it,
    so the anchored todo files as a root instead of 400ing."""

    class DeadProjectStore(DraftFakeStore):
        def get_ref(self, *, kind, id):
            # The project todo (id=1) was soft-deleted → no live row.
            if kind == "todo" and id == 1:
                return None
            return super().get_ref(kind=kind, id=id)

    runtime = FakeRuntime(DeadProjectStore())
    app = create_app(runtime=runtime, web_config=WebConfig(corpus_dir=tmp_path))
    client = TestClient(app)
    r = client.post(
        "/drafts/nt/request",
        data={"handle": "BBBBBB", "text": "tighten this"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "put" and args["kind"] == "todo"
    assert "parent_id" not in args  # filed as a root, not parented on the dead todo
    assert args["meta"]["anchor"] == "BBBBBB"


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


# ── hand-driven working set: pen/eye marks + request-ws (ADR 0051 §6) ──


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

    def review_status_for_chunk(self, chunk_id):
        return [
            {"checker": checker, **status}
            for checker, status in self._reviews.get(chunk_id, {}).items()
        ]

    def review_status_for_draft(self, ref_id):
        out = []
        for c in self._chunks:
            revs = self._reviews.get(c.chunk_id, {})
            if not revs:
                out.append(
                    {
                        "chunk_id": c.chunk_id,
                        "handle": c.handle,
                        "chunk_kind": c.chunk_kind,
                        "checker": None,
                        "approved_sha": None,
                        "verdict": None,
                        "at": None,
                        "dirty": True,
                    }
                )
                continue
            for checker, status in revs.items():
                out.append(
                    {
                        "chunk_id": c.chunk_id,
                        "handle": c.handle,
                        "chunk_kind": c.chunk_kind,
                        "checker": checker,
                        **status,
                    }
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


def test_row_review_status_reflects_the_ledger() -> None:
    """The reader's per-row payload (``_build_rows``, via ``_one_row``)
    carries the review ledger's status: never-reviewed is a present-but-empty
    dict (the ✓ shows, unclicked); a recorded human review shows non-dirty;
    editing after review would go dirty (exercised at the store level, not
    re-derived here since this fake doesn't model content_sha)."""
    from precis_web.routes.drafts import _one_row

    store = ReviewFakeStore()
    row = _one_row(store, _DRAFT, "BBBBBB")
    assert row is not None
    assert row["review"] == {}  # reviewable, never reviewed

    store.record_review(2, "human", verdict="approved")  # BBBBBB is chunk_id=2
    row = _one_row(store, _DRAFT, "BBBBBB")
    assert row["review"]["human"]["dirty"] is False
    assert row["review"]["human"]["verdict"] == "approved"


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


def test_reader_survives_datetime_review_at(tmp_path) -> None:
    """Regression: the reader embeds the whole-draft review ledger in the
    virtual-scroll skeleton and ``tojson``s it (and serves it raw from
    ``/skeleton``). A recorded review carries a real ``datetime`` ``at``, so
    the raw value 500'd the page (``TypeError: Object of type datetime is not
    JSON serializable``). ``_review_entry`` ISO-stringifies ``at`` at the
    single source both the row and skeleton read, keeping both serializable."""
    store = _DatetimeReviewStore()
    store.record_review(2, "human")  # BBBBBB / dc2
    app = create_app(
        runtime=FakeRuntime(store), web_config=WebConfig(corpus_dir=tmp_path)
    )
    client = TestClient(app)

    # The inline skeleton (`{{ skeleton | tojson }}` in detail.html.j2).
    r = client.get("/drafts/nt")
    assert r.status_code == 200  # was 500

    # The /skeleton JSON endpoint serializes the same ledger.
    skel = client.get("/drafts/nt/skeleton")
    assert skel.status_code == 200
    rv = next(
        row["rv"]
        for row in skel.json()["skeleton"]
        if (row.get("rv") or {}).get("human")
    )
    assert (
        rv["human"]["at"] == "2026-07-26T12:23:34+00:00"
    )  # ISO string, not a datetime


def test_reader_renders_the_human_review_checkbox(draft_client: TestClient) -> None:
    """The ✓ gutter button is wired into the reader's markup — dispatches
    ``draft-review`` (the Alpine listener ``onReview`` posts through)."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "dr-review" in r.text
    assert "$dispatch('draft-review', {dc:'dc2'})" in r.text


def test_reader_has_duplicate_button(draft_client: TestClient) -> None:
    """The reader toolbar exposes the web fork affordance (Phase-1 fork): a
    ⧉ duplicate control whose reveal-form posts to /drafts/<ident>/fork with
    a project name."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "duplicate" in r.text
    assert 'action="/drafts/nt/fork"' in r.text
    assert 'name="project"' in r.text


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


def test_checker_flag_strip_renders_per_lens_state(tmp_path) -> None:
    """The meta-column checker strip (rung 3d) renders one glyph per lens:
    current (✓) when reviewed at the live text, stale (~) when the
    approved sha is behind the current one, unreviewed (–) otherwise. No
    "findings" glyph — reviewers don't stamp which lens filed a request."""
    rt, client = _review_client(tmp_path)
    store = rt.store
    store.record_review(2, "cites", verdict="approved")  # cites: current
    store._reviews[2]["structure"] = {
        "approved_sha": "stale-sha",
        "verdict": "approved",
        "at": None,
        "dirty": True,
    }  # structure: stale (approved sha is behind)
    # flow / adversarial: never reviewed
    r = client.get("/drafts/nt")
    assert r.status_code == 200
    html = r.text
    assert 'title="cites: reviewed at the current text">C✓' in html
    assert 'title="structure: reviewed, but changed since — stale">S~' in html
    assert 'title="flow: never reviewed">F–' in html
    assert 'title="adversarial: never reviewed">A–' in html


def test_authored_badge_and_border_render_for_machine_authored_chunk(
    tmp_path,
) -> None:
    """A chunk carrying ``meta.authored_by`` (the grounded-authoring
    reviewer's new-chunk stamp) renders the amber "authored" badge and its
    content column gets an amber left border — the reviewer's cue that this
    prose is machine-written and starts unreviewed."""
    store = DraftFakeStore()
    store._chunks[1].meta = {"authored_by": "review:cites"}  # BBBBBB, chunk_id=2
    rt = FakeRuntime(store)
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=tmp_path))
    client = TestClient(app)
    r = client.get("/drafts/nt")
    assert r.status_code == 200
    assert "✎ authored (cites)" in r.text
    assert "border-l-4 border-amber-300 pl-1" in r.text


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


def test_reader_shows_auto_author_toggle_state(draft_client: TestClient) -> None:
    """The reader defaults to ``auto-author: OFF`` and flips to ``ON`` once
    the toggle route has been posted (the toolbar control, 3e)."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "auto-author: OFF" in r.text

    draft_client.post("/drafts/nt/authoring", data={"enabled": "1"})
    r = draft_client.get("/drafts/nt")
    assert "auto-author: ON" in r.text


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


def test_delete_change_request_dispatches_todo_delete(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    r = draft_client.post("/drafts/nt/todo/777/delete", follow_redirects=False)
    assert r.status_code == 303
    verb, args = draft_runtime.calls[-1]
    assert verb == "delete" and args["kind"] == "todo" and args["id"] == 777


def test_retry_change_request_dispatches_job_retry(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """▶ restart posts the failed *job* id → put(kind='job', mode='retry')
    and redirects back to the draft (not the tasks page)."""
    r = draft_client.post("/drafts/nt/todo/888/retry", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/drafts/nt"
    verb, args = draft_runtime.calls[-1]
    assert verb == "put"
    assert args["kind"] == "job" and args["id"] == 888 and args["mode"] == "retry"
    # No model swap when the picker is left on "same".
    assert "model" not in args


def test_retry_change_request_forwards_model_swap(
    draft_client: TestClient, draft_runtime: FakeRuntime
) -> None:
    """Picking a tier threads ``model=`` into the retry so the re-minted
    tick runs on a different model."""
    r = draft_client.post(
        "/drafts/nt/todo/888/retry", data={"model": "sonnet"}, follow_redirects=False
    )
    assert r.status_code == 303
    _verb, args = draft_runtime.calls[-1]
    assert args["model"] == "sonnet"


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


def _render_row(requests: list[SimpleNamespace]) -> str:
    """Render the ``draft_row`` macro with a synthetic block carrying the
    given change-request chips (the raw-SQL request loader is bypassed in
    the fake store, so drive the template directly)."""
    from precis_web.deps import templates

    r = SimpleNamespace(
        handle="BBBBBB",
        is_heading=False,
        ancestors=[],
        depth=1,
        chunk_kind="paragraph",
        text="Some prose.",
        summary="",
        keywords="",
        refs=[],
        connections=[],
        nearby=[],
        edits=0,
        edited_at=None,
        abbrevs={},
        requests=requests,
        review=None,
        authored=None,
    )
    ref = SimpleNamespace(ident="nt")
    tmpl = templates.env.get_template("drafts/_row.html.j2")
    return tmpl.module.draft_row(r, ref)  # type: ignore[attr-defined]  # Jinja macro, runtime-defined


def _req(
    ref_id: int,
    *,
    started: bool,
    done: bool,
    failed: bool,
    status: str,
    audit: str = "",
):
    return SimpleNamespace(
        ref_id=ref_id,
        status=status,
        title=f"req {ref_id}",
        started=started,
        done=done,
        failed=failed,
        asking="",
        audit=audit,
    )


def test_change_request_close_x_on_terminal_and_unstarted_only() -> None:
    """The close-X (delete form) shows on not-yet-started, done, and
    failed requests, but NOT on a request that's actively running."""
    rows = _render_row(
        [
            _req(1, started=False, done=False, failed=False, status="open"),
            _req(2, started=True, done=False, failed=False, status="doing"),
            _req(3, started=True, done=True, failed=False, status="done"),
            _req(4, started=True, done=False, failed=True, status="failed"),
        ]
    )
    assert "/drafts/nt/todo/1/delete" in rows  # unstarted → cancel
    assert "/drafts/nt/todo/3/delete" in rows  # done → close
    assert "/drafts/nt/todo/4/delete" in rows  # failed → close
    assert "/drafts/nt/todo/2/delete" not in rows  # running → no X


def test_audit_category_badge_renders_on_chunk() -> None:
    """A change-request todo carrying an AUDIT:<category> tag renders its
    category as a ⚑ badge on the chunk — so a content-QA audit finding is
    visible in the draft reader (not buried in the gripe bug-tracker)."""
    rows = _render_row(
        [
            _req(
                7,
                started=False,
                done=False,
                failed=False,
                status="open",
                audit="missing-citation",
            )
        ]
    )
    assert "⚑ missing-citation" in rows

    # A plain change request (no audit tag) shows no ⚑ badge.
    plain = _render_row(
        [_req(8, started=False, done=False, failed=False, status="open")]
    )
    assert "⚑" not in plain


def test_inline_editor_xdata_is_single_quoted() -> None:
    """The per-block inline editor's `x-data="draftEdit(...)"` must pass its
    string args **single-quoted**. Rendering them via `| tojson` emits DOUBLE
    quotes (`draftEdit("nt", …)`) which terminate the double-quoted `x-data`
    attribute, so Alpine never builds the component and every `editing`/`raw`/
    `err` reference throws `Can't find variable` (the click-to-edit-dead bug).
    A plain substring check for `draftEdit(` passes even on the broken form,
    so assert the quoting explicitly."""
    rows = _render_row([])
    assert "x-data=\"draftEdit('nt', 'BBBBBB'" in rows  # single-quoted args
    assert 'draftEdit("' not in rows  # the tojson double-quote that broke it


def test_wordcount_badge_xdata_is_attribute_safe(draft_client: TestClient) -> None:
    """The word-count badge embeds a JSON object in `x-data`. Rendered via
    `| tojson` alone, its double quotes terminate the double-quoted attribute
    (`x-data="{ wc: {"` → Alpine 'Unexpected token'), so the live poll is dead.
    It must be `forceescape`d so the browser decodes valid JSON. Regression:
    the badge renders and is NOT in the attribute-breaking bare-quote form."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "{ wc:" in r.text  # the badge renders
    assert 'x-data="{ wc: {"' not in r.text  # not the bare-quote (broken) form


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


def test_hydrated_rows_reprocess_htmx(draft_client: TestClient) -> None:
    """Each row the scroller fetches into the window must have htmx re-wire
    its injected hover-preview chips — else citation/¶ mouseovers open an
    empty slot. The virtual scroller htmx.process()es each inserted node."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "htmx.process(node)" in r.text


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
    assert args["parent_id"] == 1 and args["meta"]["anchor"] == "AAAAAA"
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


def test_reader_highlights_defined_abbrev(draft_client: TestClient) -> None:
    """Recall: a defined abbreviation (PEI) is wrapped in an instant-tooltip
    <abbr.pa> whose .pa-pop carries the definition (no laggy native title);
    the .pa tooltip CSS is present on the page."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert '<abbr class="pa"' in r.text
    # The definition rides in a .pa-def span inside the .pa-pop tooltip (ADR
    # 0052 rich hover — a part additionally shows MPN/manufacturer/datasheet).
    assert '<span class="pa-def">polyethyleneimine</span>' in r.text
    assert ".pa>.pa-pop" in r.text  # the instant-tooltip CSS shipped


def test_reader_shows_connections_and_edits(draft_client: TestClient) -> None:
    """The Connections surface: graph links (memory:20) render as chips
    with a count, and the edit-churn chip shows 'changed 2×'."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "1 connection" in r.text
    assert "/r/memory/20" in r.text and "A decision" in r.text
    assert "changed 2×" in r.text


def test_reader_shows_links_in_out_and_anchored_flag(
    draft_client: TestClient,
    draft_runtime: FakeRuntime,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The In/Out link-edges block (gripe 178766): an outbound edge AND an
    inbound edge on the same chunk render split by direction — the split
    ``precis_web.draft_links.chunk_links`` derives from
    ``store.chunk_connections`` (which already returns both directions;
    the merged Connections disclosure above just discards ``direction``).
    The anchored change-request ("flag") still renders as its own card too
    — both surfaces read the SAME ``store.anchored_todos`` data."""
    store = draft_runtime.store

    def fake_conns(
        ref_id: object, handles: object
    ) -> dict[str, list[dict[str, object]]]:
        return {
            "BBBBBB": [
                {
                    "relation": "derived-from",
                    "direction": "out",
                    "kind": "memory",
                    "ident": "20",
                    "title": "A decision",
                },
                {
                    "relation": "cites",
                    "direction": "in",
                    "kind": "memory",
                    "ident": "21",
                    "title": "A citing dream",
                },
            ]
        }

    def fake_flags(handles: object) -> dict[str, list[dict[str, object]]]:
        return {
            "BBBBBB": [
                {
                    "ref_id": 99,
                    "title": "tighten this claim",
                    "request": "tighten this claim",
                    "status": "open",
                    "done": False,
                    "started": False,
                    "asking": "",
                    "ask_tag": "",
                    "failed": False,
                    "fail_reason": "",
                    "fail_job_id": None,
                    "audit": "",
                }
            ]
        }

    monkeypatch.setattr(store, "chunk_connections", fake_conns)
    monkeypatch.setattr(store, "anchored_todos", fake_flags)

    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    body = r.text
    assert "/r/memory/20" in body and "A decision" in body  # out edge
    assert "/r/memory/21" in body and "A citing dream" in body  # in edge
    assert "tighten this claim" in body  # anchored flag's change-request card


# ── author byline editor (pipe-delimited textarea) ────────────────────


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


def test_reader_shows_author_editor_form(draft_client: TestClient) -> None:
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'action="/drafts/nt/authors"' in r.text
    assert 'name="authors"' in r.text
