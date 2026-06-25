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
                "Intro; see [the title](¶AAAAAA) and paper:smith2024. Uses PEI.",
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

    def get_chunk_blob(self, handle):
        if handle == "FIGFIG":
            return (b"\x89PNG\r\n\x1a\n", "image/png")
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


def test_new_draft_form_toggles_and_offers_doctype(draft_client: TestClient) -> None:
    """The '+ New draft' button drives an Alpine ``open`` flag on a shared
    wrapper (not a stale ``$refs`` on a sibling), and the form offers the
    document-type select."""
    r = draft_client.get("/drafts")
    assert r.status_code == 200
    # the toggle is wired to a single x-data scope, not a dangling $ref.
    assert "open = !open" in r.text
    assert 'x-show="open"' in r.text
    assert "$refs.newDraft" not in r.text
    # document-type selector with the patent option present.
    assert 'name="doctype"' in r.text
    assert "Patent application" in r.text


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


def test_figure_renders_img_and_origin_chip(draft_client: TestClient) -> None:
    # ADR 0034 — a figure block renders an <img> pointed at the blob route,
    # an origin chip, and a clearance badge (original ⇒ cleared).
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert 'src="/drafts/blob/FIGFIG"' in r.text
    assert "original" in r.text and "cleared" in r.text


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
    assert verb == "edit" and args["kind"] == "draft" and args["id"] == "FIGTPF"
    assert args["origin"] == "third_party"
    assert args["permission"]["publisher"] == "Elsevier"
    assert args["permission"]["permission_id"] == "EL-999"


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
    assert 'id="dr-win"' in r.text and 'id="dr-top"' in r.text and 'id="dr-bot"' in r.text
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
    assert handles == ["AAAAAA", "BBBBBB", "FIGFIG", "FIGTPF"]
    # BBBBBB is nested under the AAAAAA heading (collapse ancestry preserved)
    bbb = next(b for b in data["skeleton"] if b["h"] == "BBBBBB")
    assert bbb["anc"] == ["AAAAAA"]


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
    assert args["meta"]["anchor"] == "BBBBBB"


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

    monkeypatch.setattr(drafts_mod, "_pdf_cache_dir", lambda ref_id, version: tmp_path)
    (tmp_path / "main.pdf").write_bytes(b"%PDF-1.4 fake\n%%EOF\n")
    r = draft_client.get("/drafts/nt/pdf", follow_redirects=False)
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"


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
    )
    ref = SimpleNamespace(ident="nt")
    tmpl = templates.env.get_template("drafts/_row.html.j2")
    return tmpl.module.draft_row(r, ref)  # type: ignore[attr-defined]  # Jinja macro, runtime-defined


def _req(ref_id: int, *, started: bool, done: bool, failed: bool, status: str):
    return SimpleNamespace(
        ref_id=ref_id,
        status=status,
        title=f"req {ref_id}",
        started=started,
        done=done,
        failed=failed,
        asking="",
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
    assert '<span class="pa-pop">polyethyleneimine</span>' in r.text
    assert ".pa>.pa-pop" in r.text  # the instant-tooltip CSS shipped


def test_reader_shows_connections_and_edits(draft_client: TestClient) -> None:
    """The Connections surface: graph links (memory:20) render as chips
    with a count, and the edit-churn chip shows 'changed 2×'."""
    r = draft_client.get("/drafts/nt")
    assert r.status_code == 200
    assert "1 connection" in r.text
    assert "/r/memory/20" in r.text and "A decision" in r.text
    assert "changed 2×" in r.text
