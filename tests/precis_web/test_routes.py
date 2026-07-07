"""Route-level tests for precis_web (no Postgres; fakes in conftest)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


# ── app skeleton ───────────────────────────────────────────────────


def test_root_redirects_to_drive(client) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/drive"


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_pres_editor_routes_registered(client) -> None:
    """The pres slide-deck editor wires four endpoints (reader / pdf /
    bibtex / edit). Guards the app-factory registration + path shapes."""
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert "/pres/{slug}" in paths
    assert "/pres/{ref_id}/pdf" in paths
    assert "/pres/{ref_id}/bibtex" in paths
    assert "/pres/{ref_id}/edit" in paths


def test_datasheet_reader_route_registered(client) -> None:
    """The /datasheets reader is a thin delegate to the paper renderer
    (datasheet joined _DOC_FAMILY). Guards the app-factory registration."""
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert "/datasheets/{ident}" in paths


def test_pres_reader_renders_shared_shell_and_attribution(client) -> None:
    """The /pres editor renders the shared two-pane reader (Navigate/Jump
    via paperDoc) with the pres attribution Meta panel. Guards the shared
    reader reuse (pres joined _DOC_FAMILY) + the doc/meta_panel wiring."""
    resp = client.get("/pres/2001-lecture01")
    assert resp.status_code == 200
    body = resp.text
    # Shared reader shell (paperDoc drives Navigate/Jump + jump-to-page).
    assert "paperDoc(" in body
    assert "/static/paper-viewer.js" in body
    assert "Navigate" in body and "Jump" in body
    # Pres-specific Meta panel: the attribution form posts to the edit verb.
    assert 'action="/pres/60/edit"' in body
    assert "Attribution" in body


def test_pres_edit_dispatches_edit_verb(client, runtime) -> None:
    """Saving the attribution form dispatches the pres ``edit`` verb with
    the form fields, then redirects back to the deck."""
    resp = client.post(
        "/pres/60/edit",
        data={
            "title": "Nuts and Bolts — Lecture 1",
            "authors": "Payne, M. C.",
            "venue": "CASTEP Workshop, Durham",
            "date": "2001",
            "url": "",
            "note": "",
            "bibtex_type": "misc",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pres/2001-lecture01"
    edits = [(v, a) for (v, a) in runtime.calls if v == "edit"]
    assert edits, "edit verb was not dispatched"
    _, args = edits[-1]
    assert args["kind"] == "pres"
    assert args["venue"] == "CASTEP Workshop, Durham"


# ── papers-needed (stub backlog) ───────────────────────────────────


def test_papers_needed_renders_backlog(client) -> None:
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    # Titled stub
    assert "Ballistic carbon nanotube" in resp.text
    # DOI link wraps the identifier with the publisher URL
    assert "https://doi.org/10.1038/nature01797" in resp.text
    # arXiv link
    assert "https://arxiv.org/abs/cond-mat/0410550" in resp.text
    # State badge text from the fake stub_backlog
    assert "never attempted" in resp.text


def test_papers_needed_uol_and_scholar_links(client) -> None:
    """Each DOI / arXiv row gets a UoL Primo + Google Scholar search link
    alongside the publisher link, with the identifier percent-encoded."""
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    # DOI row → UoL Primo (institution-scoped) + Scholar, slash encoded.
    # ``&`` query separators render as ``&amp;`` — Jinja autoescape in the
    # href attribute (correct HTML; browsers decode it back to ``&``).
    assert (
        "uol.primo.exlibrisgroup.com/discovery/search?"
        "vid=353UOL_INST:353UOL_VU1&amp;search_scope=MyInst_and_CI"
        "&amp;lang=en&amp;sortby=rank&amp;tab=TAB1"
        "&amp;query=any,contains,10.1038%2Fnature01797" in resp.text
    )
    assert (
        "scholar.google.com/scholar?hl=en&amp;as_sdt=0%2C5"
        "&amp;q=10.1038%2Fnature01797&amp;btnG=" in resp.text
    )
    # arXiv row → bare arXiv number searched (no 'arxiv:' prefix).
    assert "q=cond-mat/0410550".replace("/", "%2F") in resp.text
    assert ">UoL</a>" in resp.text
    assert ">Scholar</a>" in resp.text


def test_papers_needed_awaiting_filter(client) -> None:
    resp = client.get("/papers-needed?awaiting=1")
    assert resp.status_code == 200
    assert "Ballistic carbon nanotube" in resp.text


def test_papers_needed_relative_time_not_raw_iso(client) -> None:
    """The last-attempt column renders a relative '…ago' string with the
    absolute timestamp tucked into a hover title — not the raw ISO."""
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    # Relative form is shown…
    assert "ago" in resp.text
    # …and the absolute timestamp lives in the hover tooltip, not as
    # bare body text (the canned stub's last_attempt is 2026-06-13).
    assert 'title="2026-06-13 10:00 UTC"' in resp.text
    assert ">2026-06-13T10:00:00+00:00<" not in resp.text


def test_papers_needed_pager_preserves_awaiting(client) -> None:
    """Pager links carry the awaiting filter across pages."""
    resp = client.get("/papers-needed?awaiting=1")
    assert resp.status_code == 200
    assert "Page 1 of 1" in resp.text
    # Total stub count is surfaced, not just a next/prev probe.
    assert "Showing" in resp.text
    assert "stubs" in resp.text


# ── reading-intent flags (read-later / must-read / skim) ───────────


def test_papers_needed_shows_flag_buttons(client) -> None:
    """Each stub row carries the three flag toggle forms, posting to the
    kind-agnostic /flags/<kind>/<id> route."""
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    assert 'action="/flags/paper/90"' in resp.text
    assert "Read later" in resp.text
    assert "Must-read" in resp.text
    assert "Skim" in resp.text


def test_papers_needed_active_flag_renders_pressed_and_removes(runtime, client) -> None:
    """A ref already carrying OPEN:read-later renders that button active
    (aria-pressed) and armed to remove on the next click."""
    runtime.store.ref_open_values[90] = {"read-later"}
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    # The active button posts op=remove so a second click toggles off.
    assert 'aria-pressed="true"' in resp.text
    assert '<input type="hidden" name="op" value="remove" />' in resp.text


def test_flag_toggle_add_dispatches_tag(runtime, client) -> None:
    resp = client.post(
        "/flags/paper/90",
        data={"flag": "read-later", "op": "add", "return_to": "/papers-needed"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers-needed"
    assert (
        "tag",
        {"kind": "paper", "id": 90, "add": ["read-later"]},
    ) in runtime.calls


def test_flag_toggle_remove_dispatches_tag(runtime, client) -> None:
    resp = client.post(
        "/flags/paper/90",
        data={
            "flag": "must-read",
            "op": "remove",
            "return_to": "/papers-needed?page=2",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers-needed?page=2"
    assert (
        "tag",
        {"kind": "paper", "id": 90, "remove": ["must-read"]},
    ) in runtime.calls


def test_flag_toggle_rejects_unknown_flag(runtime, client) -> None:
    """An off-vocabulary flag is a no-op redirect, never a tag write."""
    resp = client.post(
        "/flags/paper/90",
        data={"flag": "delete-everything", "op": "add"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert runtime.calls == []


def test_flag_toggle_surfaces_handler_error(runtime, client) -> None:
    """A rejected tag dispatch renders the handler error, not a silent
    redirect (the untriage-bug lesson, applied to flags)."""
    runtime.error_verbs.add("tag")
    resp = client.post(
        "/flags/paper/90",
        data={"flag": "skim", "op": "add"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "invalid tag" in resp.text


def test_flag_toggle_blocks_open_redirect(runtime, client) -> None:
    """A non-local return_to falls back to /papers-needed."""
    resp = client.post(
        "/flags/paper/90",
        data={"flag": "read-later", "return_to": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers-needed"


# ── unified item view (/items) ─────────────────────────────────────


def test_items_empty_shows_form_and_recent(client) -> None:
    """The no-query landing shows the search form and a 'recently added'
    list of source items (with their flag buttons). No tag cloud."""
    resp = client.get("/items")
    assert resp.status_code == 200
    assert 'action="/items"' in resp.text
    assert "Recently added" in resp.text
    assert "A paper" in resp.text
    assert "A web page" in resp.text
    assert 'action="/flags/paper/10"' in resp.text
    assert "Browse by tag" not in resp.text  # cloud dropped


def test_items_stub_filter(runtime, client) -> None:
    """state=stub narrows the landing to PDF-less papers (the to-get
    queue) and relabels the heading."""
    resp = client.get("/items?state=stub")
    assert resp.status_code == 200
    assert runtime.store.recent_has_pdf is False
    assert "Stubs — papers to get" in resp.text


def test_items_rows_show_paper_lookup_links(client) -> None:
    """Paper rows carry off-site UoL + Scholar 'find:' links built from
    the paper's identifier."""
    resp = client.get("/items")
    assert "uol.primo.exlibrisgroup.com" in resp.text
    assert "scholar.google.com" in resp.text
    assert "doi.org/10.1038/nature01797" in resp.text


def test_items_stub_vs_ingested_badges(runtime, client) -> None:
    """A recent paper with no chunks shows the 'stub' badge; once it has
    chunks it shows 'chunks' instead."""
    resp = client.get("/items")
    assert ">stub<" in resp.text  # paper #10 has no pdf, no chunks
    runtime.store.ingested_ref_ids = {10}
    resp2 = client.get("/items")
    assert ">chunks<" in resp2.text
    assert ">stub<" not in resp2.text


def test_items_rows_show_per_item_tags(client) -> None:
    """Each row shows the item's own tags as chips — topical tags only;
    the reading-intent flags (buttons) and machine namespaces are hidden.

    Tested on the search view (?q=), which has no tag cloud, so the only
    source of these links is the per-row chips."""
    resp = client.get("/items?q=query")
    assert resp.status_code == 200
    # Paper #10's topical tag renders as a chip linking to the pivot.
    assert "/tags/refs?namespace=topic&amp;value=co2-capture" in resp.text
    # Its flag + machine tags do NOT appear as chips.
    assert "namespace=OPEN&amp;value=read-later" not in resp.text
    assert "namespace=DREAM&amp;value=spec" not in resp.text


def test_items_has_new_button(client) -> None:
    """The /items header carries a Drive-style '+ New' dropdown reusing
    the existing /drafts/new + /drive/new creation flows."""
    resp = client.get("/items")
    assert "+ New" in resp.text
    assert "/drafts/new" in resp.text
    assert "/drive/new" in resp.text
    assert "Draft (document)" in resp.text


def test_items_search_renders_cross_kind_rows(client) -> None:
    resp = client.get("/items?q=query")
    assert resp.status_code == 200
    # Both kinds surface through the one cross-kind primitive.
    assert "A paper" in resp.text
    assert "A web page" in resp.text
    # The matching chunk is the preview.
    assert "passage about the query" in resp.text
    # Per-kind open_url: paper → reader, web → generic refs detail.
    assert 'href="/papers/10"' in resp.text
    assert 'href="/refs/web/70"' in resp.text
    # Flag buttons ride along on each row.
    assert 'action="/flags/paper/10"' in resp.text


def test_items_kind_filter_narrows(client) -> None:
    resp = client.get("/items?q=query&submitted=1&k=paper")
    assert resp.status_code == 200
    assert "A paper" in resp.text
    assert "A web page" not in resp.text


def test_items_recency_sort_accepted(client) -> None:
    resp = client.get("/items?q=query&sort=recency")
    assert resp.status_code == 200
    assert "A paper" in resp.text


def test_items_kind_checkboxes_and_cookie(client) -> None:
    """Kinds render as toggle chips (checkboxes) with All/None; an explicit
    submit remembers the selection in a cookie."""
    resp = client.get("/items")
    assert 'name="k"' in resp.text
    assert ">All<" in resp.text and ">None<" in resp.text
    resp2 = client.get("/items?submitted=1&k=paper&k=web", follow_redirects=False)
    cookie = resp2.headers.get("set-cookie", "")
    # Starlette quotes the comma (round-trips fine on read); assert the
    # cookie is set and carries both chosen kinds.
    assert "items_kinds=" in cookie
    assert "paper" in cookie and "web" in cookie
    # Round-trip: the cookie drives kind selection on a fresh (unsubmitted)
    # visit — the client persists the cookie across requests.
    client.get("/items?submitted=1&k=paper", follow_redirects=False)
    resp3 = client.get("/items")
    assert '["paper"]' in resp3.text  # x-data seed reflects the remembered set


def test_items_tag_suggest_endpoint(client) -> None:
    """The autocomplete backend substring-matches tags; <2 chars is empty."""
    assert client.get("/items/tags/suggest?q=c").json() == []
    hits = client.get("/items/tags/suggest?q=co2").json()
    assert {"label": "topic:co2-capture", "tag": "topic:co2-capture"} in hits


def test_items_tag_filter_flows_to_search(runtime, client) -> None:
    """Selected tag chips (?tag=) narrow the search."""
    client.get("/items?q=query&tag=topic:co2-capture")
    assert runtime.store.search_tags == ["topic:co2-capture"]


def test_items_tag_filter_flows_to_recent(runtime, client) -> None:
    """On the no-query landing, tag chips narrow the recent list."""
    client.get("/items?tag=topic:co2-capture")
    assert runtime.store.recent_tags == ["topic:co2-capture"]


def _stub_paging_client(total: int):
    """A TestClient whose store reports ``total`` stubs and pages them.

    Exercises the ``/papers-needed`` route's numbered pager: the route
    calls ``stub_backlog_count`` (answered from ``total``) and
    ``stub_backlog`` (sliced by limit/offset), so the page-window
    arithmetic is what's under test.
    """
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    from .conftest import FakeRuntime, FakeStore

    rows = [
        {
            "ref_id": rid,
            "cite_key": f"key{rid}",
            "identifier": f"10.1/{rid}",
            "last_attempt": "",
            "last_source": "",
            "last_event": "",
            "state": "never attempted",
            "created_at": "2026-07-01T08:00:00+00:00",
            "requested_by": "dream",
            "attempts": 0,
        }
        for rid in range(total, 0, -1)
    ]

    class _Store(FakeStore):
        def stub_backlog(self, *, limit=50, offset=0, awaiting=False):
            return rows[offset : offset + limit]

        def stub_backlog_count(self, *, awaiting=False):
            return total

        def fetch_refs_by_ids(self, ids, include_deleted=False):
            return {}

    rt = FakeRuntime(_Store())
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def test_papers_needed_numbered_pager_shows_total_and_last_page() -> None:
    # 250 stubs at 100/page → 3 pages. First page shows the count, the
    # numbered window, and a jump-to-last link.
    client = _stub_paging_client(total=250)
    resp = client.get("/papers-needed")
    assert resp.status_code == 200
    assert "Page 1 of 3" in resp.text
    assert "1–100" in resp.text
    # Numbered links to pages 2 and 3 (the last page) are present.
    assert "page=2" in resp.text
    assert "page=3" in resp.text


def test_papers_needed_clamps_overshoot_to_last_page() -> None:
    # ?page far past the end clamps to the last page rather than 500ing
    # or rendering an empty body.
    client = _stub_paging_client(total=250)
    resp = client.get("/papers-needed?page=99")
    assert resp.status_code == 200
    assert "Page 3 of 3" in resp.text
    assert "201–250" in resp.text


# ── tags browser ───────────────────────────────────────────────────


def test_tags_index_renders_empty_state(client) -> None:
    """``/tags`` returns 200 even on a fresh DB (the FakeConn yields no rows)."""
    resp = client.get("/tags")
    assert resp.status_code == 200
    assert "Tags" in resp.text
    # Empty-state copy.
    assert "No tags match" in resp.text


def test_tags_index_accepts_query_filter(client) -> None:
    """``?q=foo`` flows through the route without crashing."""
    resp = client.get("/tags?q=tier")
    assert resp.status_code == 200
    # Query value echoed in the search box.
    assert 'value="tier"' in resp.text


# ── tags/refs pagination ───────────────────────────────────────────


def _paging_client(total: int):
    """A TestClient whose store pages a synthetic ``total``-row result.

    The ``/tags/refs`` route fires two queries: a ``count(*)`` and a
    ``... LIMIT %s OFFSET %s`` page fetch. This fake answers the count
    from ``total`` and slices a descending-id row list by the trailing
    (limit, offset) params, so the route's offset/limit arithmetic is
    what's under test — not the DB.
    """
    from contextlib import contextmanager

    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    from .conftest import FakeRuntime, FakeStore

    all_rows = [
        (("think"), rid, f"thought {rid}", False) for rid in range(total, 0, -1)
    ]

    class _PagingConn:
        def execute(self, sql: str, params):
            if "count(" in sql.lower():
                return _Cur([(total,)])
            limit, offset = int(params[-2]), int(params[-1])
            return _Cur(all_rows[offset : offset + limit])

    class _Cur:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

        def fetchone(self):
            return self._rows[0] if self._rows else None

    class _PagingPool:
        @contextmanager
        def connection(self):
            yield _PagingConn()

    store = FakeStore()
    store.pool = _PagingPool()
    rt = FakeRuntime(store)
    rt.store = store
    app = create_app(runtime=rt, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def test_refs_by_tag_first_page_has_next_not_prev() -> None:
    client = _paging_client(total=250)
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative")
    assert resp.status_code == 200
    assert "250 refs tagged" in resp.text
    assert "Page 1 of 3" in resp.text
    assert 'rel="next"' in resp.text
    # First page: prev is the disabled span, not an anchor.
    assert 'rel="prev"' not in resp.text
    # Range readout.
    assert "1–100 of 250" in resp.text


def test_refs_by_tag_has_numbered_window_with_last_jump() -> None:
    # A many-page result renders numbered page links and a jump to the
    # last page (…) — not just Prev/Next stepping.
    client = _paging_client(total=2000)  # 20 pages at 100/page
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page=10")
    assert resp.status_code == 200
    assert "Page 10 of 20" in resp.text
    # Window around page 10 plus first/last anchors.
    assert "page=7" in resp.text and "page=13" in resp.text
    assert "page=1" in resp.text and "page=20" in resp.text
    # Current page is rendered as a non-link marker, not an anchor.
    assert "page=10" not in resp.text


def test_refs_by_tag_middle_page_has_both_arrows() -> None:
    client = _paging_client(total=250)
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page=2")
    assert resp.status_code == 200
    assert "Page 2 of 3" in resp.text
    assert 'rel="prev"' in resp.text
    assert 'rel="next"' in resp.text
    assert "101–200 of 250" in resp.text
    # Filters are preserved in the pager links.
    assert "namespace=DREAM" in resp.text
    assert "value=speculative" in resp.text


def test_refs_by_tag_last_page_has_no_next() -> None:
    client = _paging_client(total=250)
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page=3")
    assert resp.status_code == 200
    assert "Page 3 of 3" in resp.text
    assert 'rel="prev"' in resp.text
    assert 'rel="next"' not in resp.text
    assert "201–250 of 250" in resp.text


def test_refs_by_tag_past_end_shows_back_link() -> None:
    client = _paging_client(total=250)
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page=9")
    assert resp.status_code == 200
    assert "past the end" in resp.text
    assert "back a page" in resp.text


def test_refs_by_tag_page_size_is_clamped() -> None:
    client = _paging_client(total=250)
    # 9999 clamps to the 500 ceiling → everything fits on one page.
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page_size=9999")
    assert resp.status_code == 200
    assert "250 refs tagged" in resp.text
    # Single page → no pager rendered.
    assert "Page 1 of 1" not in resp.text
    assert 'aria-label="Pagination"' not in resp.text


def test_refs_by_tag_custom_page_size_preserved_in_links() -> None:
    client = _paging_client(total=250)
    resp = client.get("/tags/refs?namespace=DREAM&value=speculative&page_size=50")
    assert resp.status_code == 200
    assert "Page 1 of 5" in resp.text
    # Non-default page_size rides along in the next link.
    assert "page_size=50" in resp.text


# ── tasks ──────────────────────────────────────────────────────────


def test_tasks_dashboard_renders_tree(client) -> None:
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert "Build the thing" in resp.text
    assert "Draft the spec" in resp.text
    # The doable panel is fed by a dispatch search.
    assert "[search] ok" in resp.text


def test_create_root_dispatches_put_with_level(client, runtime) -> None:
    resp = client.post(
        "/tasks/roots",
        data={"text": "New root", "level": "strategic"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "put"
    assert args["kind"] == "todo"
    assert args["text"] == "New root"
    assert args["tags"] == ["level:strategic"]
    assert args.get("parent_id") is None


def test_create_child_passes_parent_id(client, runtime) -> None:
    client.post(
        "/tasks/1/children", data={"text": "child task"}, follow_redirects=False
    )
    verb, args = runtime.calls[-1]
    assert verb == "put"
    assert args["parent_id"] == 1
    assert args["tags"] == ["level:subtask"]


def test_set_status_dispatches_tag(client, runtime) -> None:
    client.post("/tasks/2/status", data={"status": "done"}, follow_redirects=False)
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args["add"] == ["STATUS:done"]


def test_delete_dispatches_delete(client, runtime) -> None:
    client.post("/tasks/2/delete", follow_redirects=False)
    verb, args = runtime.calls[-1]
    assert verb == "delete"
    assert args == {"kind": "todo", "id": 2}


def test_ask_terminate_closes_and_clears_tags(client, runtime) -> None:
    """The X on an ask flips the todo to won't-do and strips ask-user
    tags in one tag call — it leaves the queue without an answer."""
    resp = client.post(
        "/asks/2/terminate",
        data={"remove": ["ask-user:which one?", "ask-user"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args["id"] == 2
    assert args["add"] == ["STATUS:won't-do"]
    assert args["remove"] == ["ask-user:which one?", "ask-user"]


def test_move_dispatches_parent_link(client, runtime) -> None:
    resp = client.post(
        "/tasks/2/move", data={"new_parent_id": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "link"
    assert args == {
        "kind": "todo",
        "id": 2,
        "target": "todo:1",
        "rel": "parent",
        "mode": "add",
    }


def test_move_empty_parent_detaches_to_root(client, runtime) -> None:
    client.post("/tasks/2/move", data={"new_parent_id": ""}, follow_redirects=False)
    verb, args = runtime.calls[-1]
    assert verb == "link"
    assert args == {"kind": "todo", "id": 2, "rel": "parent", "mode": "remove"}


# ── papers ─────────────────────────────────────────────────────────


def test_papers_index_lists(client) -> None:
    resp = client.get("/papers")
    assert resp.status_code == 200
    assert "A paper" in resp.text


def test_papers_search_query(client) -> None:
    resp = client.get("/papers", params={"q": "anything"})
    assert resp.status_code == 200
    assert "A paper" in resp.text


def test_papers_search_default_mode_is_keyword(client) -> None:
    """The literal title search stays the default — the mode toggle
    renders ``keyword`` selected when nothing is passed."""
    resp = client.get("/papers", params={"q": "anything"})
    assert resp.status_code == 200
    assert 'value="keyword" selected' in resp.text


def test_papers_search_semantic_ranks_by_body_content(client, runtime) -> None:
    """``mode=semantic`` embeds the query and ranks papers via a
    cross-paper block search, collapsing chunk hits to distinct papers."""
    from types import SimpleNamespace

    runtime.hub = SimpleNamespace(
        embedder=SimpleNamespace(embed_one=lambda _q: [0.1, 0.2, 0.3])
    )
    paper = runtime.store.papers[0]  # id=10, "A paper"
    block = SimpleNamespace(pos=0)
    # Two chunk hits on the same paper → collapse dedups to one row.
    runtime.store.nav_hits[None] = [(block, paper, 0.1), (block, paper, 0.2)]
    resp = client.get("/papers", params={"q": "mof embedding", "mode": "semantic"})
    assert resp.status_code == 200
    assert "A paper" in resp.text
    assert 'value="semantic" selected' in resp.text


def test_papers_search_semantic_degrades_without_embedder(client, runtime) -> None:
    """No embedder wired → the semantic leg degrades to the lexical
    title search, and the toggle reflects the mode that actually ran."""
    resp = client.get("/papers", params={"q": "anything", "mode": "semantic"})
    assert resp.status_code == 200
    # runtime.hub is None by default → embed_query returns None → keyword.
    assert 'value="keyword" selected' in resp.text


def test_paper_detail_renders(client) -> None:
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert "smith2024" in resp.text


def test_paper_detail_shows_ingest_timestamps(client) -> None:
    """The detail page surfaces the three-stage ingest timeline (ref /
    PDF / first chunk) as relative time with the absolute UTC on hover."""
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert "first chunk" in resp.text
    # Absolute timestamps from the canned ingest_timestamps land in
    # hover titles (the relative '…ago' text varies with wall clock).
    assert 'title="2026-06-14 09:00 UTC"' in resp.text  # ref minted
    assert 'title="2026-06-14 09:05 UTC"' in resp.text  # pdf landed
    assert 'title="2026-06-14 09:07 UTC"' in resp.text  # first chunk


def test_paper_edit_dispatches_changed_fields_only(client, runtime) -> None:
    """POST /papers/{id}/edit forwards only the non-empty fields to the
    edit verb so an unset value doesn't overwrite the existing one."""
    resp = client.post(
        "/papers/10/edit",
        data={
            "title": "New title",
            "year": "2024",
            "doi": "",  # blank → not sent
            "arxiv": "",
            "abstract": "",
            "authors": "",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "edit"
    assert args["kind"] == "paper"
    assert args["id"] == 10
    assert args["title"] == "New title"
    assert args["year"] == 2024
    # Blank fields not sent.
    assert "doi" not in args
    assert "arxiv" not in args
    assert "abstract" not in args
    assert "authors" not in args


def test_paper_edit_forwards_author_lines(client, runtime) -> None:
    """Newline-/semicolon-separated authors are forwarded as cleaned
    line strings; the paper edit handler canonicalises them to the
    stored ``{"name": …}`` shape (so the web layer no longer shapes
    family/given)."""
    resp = client.post(
        "/papers/10/edit",
        data={"authors": "Smith, Jane\nJones, Bob"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _, args = runtime.calls[-1]
    assert args["authors"] == ["Smith, Jane", "Jones, Bob"]


def test_paper_edit_authors_accepts_lastname_only_entries(client, runtime) -> None:
    """A single name (no comma) is forwarded verbatim so the form
    doesn't reject the common case of single-author papers."""
    resp = client.post(
        "/papers/10/edit",
        data={"authors": "Aristotle"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _, args = runtime.calls[-1]
    assert args["authors"] == ["Aristotle"]


def test_paper_edit_year_is_coerced_to_int(client, runtime) -> None:
    """The form sends ``year`` as a string; the route coerces to int
    so the schema gets the right type."""
    resp = client.post(
        "/papers/10/edit",
        data={"year": "1999"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _, args = runtime.calls[-1]
    assert args["year"] == 1999
    assert isinstance(args["year"], int)


def test_paper_edit_invalid_year_silently_dropped(client, runtime) -> None:
    """A non-numeric year is dropped rather than failing the whole edit
    — the title/abstract/etc. still land."""
    resp = client.post(
        "/papers/10/edit",
        data={"year": "soon", "title": "Hi"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    _, args = runtime.calls[-1]
    assert "year" not in args
    assert args["title"] == "Hi"


def test_paper_edit_error_surfaces_handler_message(client, runtime) -> None:
    """A handler-rejected edit renders the error page with the actual
    message — not an empty ``()`` heading. Regression: the route was
    passing ``body``/``is_error`` keys the error template never reads
    (it wants ``title``/``detail``/``status``), so every field rendered
    blank under ChainableUndefined."""
    runtime.error_verbs.add("edit")
    resp = client.post(
        "/papers/10/edit",
        data={"title": "New title"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "invalid edit: rejected by handler" in resp.text
    assert "Edit error" in resp.text
    # The empty-substitution symptom must not reappear.
    assert "()" not in resp.text


def test_paper_delete_missing_paper_surfaces_error(client, runtime) -> None:
    """Deleting an unknown paper renders the error page with a real
    message (correct ``title``/``detail``/``status`` keys — not the
    empty-substitution symptom)."""
    resp = client.post("/papers/999/delete", follow_redirects=False)
    assert resp.status_code == 404
    assert "Delete error" in resp.text
    assert "paper id=999 not found" in resp.text
    assert "()" not in resp.text


def test_paper_delete_soft_deletes_and_redirects_to_list(client, runtime) -> None:
    """The delete button POSTs to /papers/{id}/delete which soft-deletes
    via a DIRECT store call (web-only; not dispatched to the agent MCP
    surface) and bounces to the papers list by default."""
    store = runtime.store
    resp = client.post("/papers/10/delete", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers"
    assert 10 in store.deleted_ref_ids
    # Deletion never went through dispatch.
    assert not any(verb == "delete" for verb, _ in runtime.calls)


def test_paper_delete_honours_return_to(client, runtime) -> None:
    """``return_to`` lands the operator back where they were (triage
    queue), but only for local ``/papers`` paths."""
    resp = client.post(
        "/papers/10/delete",
        data={"return_to": "/papers/triage?page=2"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/triage?page=2"


def test_paper_delete_rejects_offsite_return_to(client, runtime) -> None:
    """An off-site ``return_to`` is ignored (no open redirect)."""
    resp = client.post(
        "/papers/10/delete",
        data={"return_to": "https://evil.example/x"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers"


def test_paper_untriage_clears_tag_and_redirects(client, runtime) -> None:
    """The untriage button dispatches a tag-remove of ``needs-triage``
    and returns to the triage queue."""
    resp = client.post("/papers/10/untriage", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/triage"
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args == {"kind": "paper", "id": 10, "remove": ["needs-triage"]}


def test_paper_untriage_surfaces_dispatch_error(client, runtime) -> None:
    """A failed tag-remove renders the handler error instead of silently
    redirecting. Regression for the original bug: the route swallowed a
    ``NotFound`` and 303'd, so the button looked like it worked while the
    flag survived. Now it goes through ``redirect_or_error``."""
    runtime.error_verbs.add("tag")
    resp = client.post("/papers/10/untriage", follow_redirects=False)
    assert resp.status_code == 400
    assert "invalid tag" in resp.text


def test_paper_edit_duplicate_identifier_renders_resolver(
    client, runtime, tmp_path
) -> None:
    """A duplicate-DOI edit error renders the resolver: it links to the
    owning paper (detail + PDF) and offers to delete this copy, rather
    than dumping the raw 400."""
    # Lay down the owner's PDF (shard layout <root>/<letter>/<key>.pdf) so
    # the resolver renders the "open PDF in a new tab" link.
    pdf = tmp_path / "j" / "jones2025.pdf"
    pdf.parent.mkdir(parents=True, exist_ok=True)
    pdf.write_bytes(b"%PDF-1.4\n")

    def _dup(verb, args):
        runtime.calls.append((verb, dict(args)))
        if verb == "edit":
            return (
                "[error:BadInput] doi='10.1234/example.2024' already "
                "belongs to ref id=11\n  next: resolve the duplicate",
                True,
            )
        return (f"[{verb}] ok", False)

    runtime.dispatch_with_status = _dup  # type: ignore[method-assign]
    resp = client.post(
        "/papers/10/edit",
        data={"doi": "10.1234/example.2024"},
        follow_redirects=False,
    )
    assert resp.status_code == 409
    assert "Duplicate identifier" in resp.text
    # Links to the owner's detail + PDF so it can be opened in a new tab.
    assert "/papers/11" in resp.text
    assert "/papers/11/pdf" in resp.text
    # Offers both merge directions (keep this / keep the owner), each
    # posting to the resolve-duplicate route.
    assert 'action="/papers/10/resolve-duplicate"' in resp.text
    assert 'value="this"' in resp.text
    assert 'value="other"' in resp.text
    # The pending edit is carried so "keep this" can re-apply it post-merge.
    assert 'name="doi" value="10.1234/example.2024"' in resp.text


def test_resolve_duplicate_keep_this_absorbs_owner_and_reapplies_edit(
    client, runtime
) -> None:
    """``keep=this`` merges the owner into this paper (link migration +
    identifier free-up + soft-delete) and re-applies the pending edit, which
    now succeeds since the clashing DOI is free."""
    store = runtime.store
    resp = client.post(
        "/papers/10/resolve-duplicate",
        data={
            "owner_id": "11",
            "keep": "this",
            "doi": "10.1234/example.2024",
            "title": "Recovered title",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/10"
    # Owner #11 absorbed into #10 and retired.
    assert (11, 10) in store.merges
    assert 11 in store.deleted_ref_ids
    # The pending edit was re-applied to the survivor.
    edit_calls = [a for v, a in runtime.calls if v == "edit"]
    assert edit_calls and edit_calls[-1]["doi"] == "10.1234/example.2024"
    assert edit_calls[-1]["title"] == "Recovered title"


def test_resolve_duplicate_keep_other_absorbs_this(client, runtime) -> None:
    """``keep=other`` keeps the existing paper and absorbs this copy into it
    (this paper's links migrate onto the owner; this copy is retired)."""
    store = runtime.store
    resp = client.post(
        "/papers/10/resolve-duplicate",
        data={"owner_id": "11", "keep": "other"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/11"
    assert (10, 11) in store.merges
    assert 10 in store.deleted_ref_ids
    # No metadata edit re-applied on this direction.
    assert not any(v == "edit" for v, _ in runtime.calls)


def test_resolve_duplicate_missing_owner_errors(client, runtime) -> None:
    """A merge against an unknown / already-deleted owner surfaces an error
    rather than silently doing nothing."""
    resp = client.post(
        "/papers/10/resolve-duplicate",
        data={"owner_id": "999", "keep": "this"},
        follow_redirects=False,
    )
    assert resp.status_code == 404
    assert "Merge error" in resp.text
    assert (999, 10) not in runtime.store.merges


def test_triage_queue_renders_numbered_pager(client) -> None:
    """The triage queue carries the same windowed numbered pager as the
    Papers Needed list — total count + 'Page N of M', not just Prev/Next."""
    resp = client.get("/papers/triage")
    assert resp.status_code == 200
    assert "Page 1 of" in resp.text
    assert "paper" in resp.text  # 'of N papers'
    # The first/only page is the current page chip (bg-slate-800).
    assert "bg-slate-800" in resp.text


def test_triaged_detail_opens_meta_tab(client, runtime) -> None:
    """A triaged paper opens straight on the Meta tab (where the triage
    panel + edit form live) — the sidebar component is seeded with 'Meta'."""
    runtime.store.triaged_ref_ids.add(10)
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert ", 'Meta')" in resp.text  # paperDoc(…, initialTab='Meta')


def test_detail_tab_query_param_selects_meta(client) -> None:
    """``?tab=meta`` opens the Meta tab even on a non-triaged paper."""
    resp = client.get("/papers/smith2024", params={"tab": "meta"})
    assert resp.status_code == 200
    assert ", 'Meta')" in resp.text


def test_detail_defaults_to_navigate_tab(client) -> None:
    """A plain paper opens on Navigate."""
    resp = client.get("/papers/smith2024")
    assert resp.status_code == 200
    assert ", 'Navigate')" in resp.text


def test_paper_tags_route_removes_tag_and_returns_to_meta(client, runtime) -> None:
    """The Meta-tab × button removes a tag via the tag verb and lands back
    on the Meta tab."""
    resp = client.post(
        "/papers/10/tags",
        data={"remove": "needs-triage"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/10?tab=Meta"
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args == {"kind": "paper", "id": 10, "remove": ["needs-triage"]}


def test_paper_tags_route_adds_tags(client, runtime) -> None:
    """The add box mints OPEN tags through the tag verb."""
    resp = client.post(
        "/papers/10/tags",
        data={"add": "foo, bar"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args["add"] == ["foo", "bar"]


def test_paper_tags_route_noop_redirects(client, runtime) -> None:
    """An empty submit just returns to the Meta tab without a dispatch."""
    resp = client.post("/papers/10/tags", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/10?tab=Meta"
    assert not any(v == "tag" for v, _ in runtime.calls)


def test_paper_detail_suggests_a_short_handle(client) -> None:
    """The edit form carries a cite_key field and suggests a system-format
    handle (surname + 2-digit year) derived from the paper's author+year."""
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert 'name="cite_key"' in resp.text
    assert "Short handle" in resp.text
    # Paper 10 is Smith / 2024 -> suggestion 'smith24' (its stored slug is
    # the 4-digit 'smith2024', so the suggestion differs and is offered).
    assert "smith24" in resp.text


def test_paper_edit_renames_slug_and_moves_pdf(client, runtime, tmp_path) -> None:
    """Submitting a new cite_key re-slugs the paper (set_ref_identifier)
    and moves its PDF to the new sharded path on disk."""
    store = runtime.store
    old = tmp_path / "s" / "smith2024.pdf"
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_bytes(b"%PDF-1.4\n")

    resp = client.post(
        "/papers/10/edit",
        data={"cite_key": "piela07"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/papers/10"
    assert (10, "cite_key", "piela07") in store.identifier_writes
    # PDF moved old -> new sharded path.
    assert not old.exists()
    assert (tmp_path / "p" / "piela07.pdf").is_file()


def test_paper_edit_slug_only_skips_metadata_dispatch(client, runtime) -> None:
    """A handle-only change doesn't dispatch an `edit` (which would reject
    an empty field set) — it goes straight to the rename path."""
    resp = client.post(
        "/papers/10/edit",
        data={"cite_key": "piela07"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert not any(verb == "edit" for verb, _ in runtime.calls)


def test_paper_edit_rejects_invalid_handle(client, runtime) -> None:
    """A handle with illegal characters is rejected before any write."""
    resp = client.post(
        "/papers/10/edit",
        data={"cite_key": "Piela 2007!"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "Rename error" in resp.text
    assert not runtime.store.identifier_writes


def test_paper_edit_handle_collision_surfaces_error(client, runtime) -> None:
    """A handle already owned by another paper surfaces the store's
    BadInput inline rather than silently clobbering."""
    runtime.store.taken_cite_keys.add("piela07")
    resp = client.post(
        "/papers/10/edit",
        data={"cite_key": "piela07"},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    assert "already belongs to ref" in resp.text


def test_paper_edit_nothing_to_change_is_rejected(client, runtime) -> None:
    """An all-blank edit (no fields, no handle change) is a clear 400."""
    resp = client.post("/papers/10/edit", data={}, follow_redirects=False)
    assert resp.status_code == 400
    assert "Nothing to change" in resp.text


def test_triage_queue_renders(client) -> None:
    """GET /papers/triage lists papers and highlights the Triage tab."""
    resp = client.get("/papers/triage")
    assert resp.status_code == 200
    assert "Triage queue" in resp.text


def test_paper_detail_shows_triage_panel_when_tagged(client, runtime) -> None:
    """A needs-triage paper's detail page opens the triage (paste-title) panel."""
    runtime.store.triaged_ref_ids.add(10)
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert "Needs triage" in resp.text
    assert "/papers/10/triage-lookup" in resp.text


def test_triage_lookup_prefills_from_s2(client) -> None:
    """Pasting a title runs an S2 lookup and pre-fills the edit form."""
    # patch() imports precis.ingest.lookup → precis.ingest.crossref →
    # habanero, which ships only in the [paper] extra (absent on the lean
    # host venv; present in the container gate / CI). Skip cleanly there.
    pytest.importorskip("habanero")
    from unittest.mock import patch

    hit = {
        "title": "Ballistic carbon nanotube field-effect transistors",
        "authors": [{"name": "Javey, Ali"}],
        "year": 2003,
        "doi": "10.1038/nature01797",
        "abstract": "We report ballistic transport.",
    }
    with patch("precis.ingest.lookup.lookup_title", return_value=hit):
        resp = client.post(
            "/papers/10/triage-lookup", data={"title": "ballistic carbon nanotube"}
        )
    assert resp.status_code == 200
    assert "Found on Semantic Scholar" in resp.text
    # The edit form is pre-filled with the looked-up title + DOI.
    assert "Ballistic carbon nanotube field-effect transistors" in resp.text
    assert "10.1038/nature01797" in resp.text


def test_triage_lookup_miss_shows_message(client) -> None:
    pytest.importorskip("habanero")  # [paper] extra — see prefills test above
    from unittest.mock import patch

    with patch("precis.ingest.lookup.lookup_title", return_value=None):
        resp = client.post("/papers/10/triage-lookup", data={"title": "nonsense xyz"})
    assert resp.status_code == 200
    assert "No Semantic Scholar match" in resp.text


def test_edit_clears_needs_triage_tag(client, runtime) -> None:
    """Saving an edit on a triaged paper dispatches a tag-remove for
    needs-triage so it leaves the queue."""
    runtime.store.triaged_ref_ids.add(10)
    resp = client.post(
        "/papers/10/edit", data={"title": "A real title"}, follow_redirects=False
    )
    assert resp.status_code == 303
    tag_calls = [
        args for verb, args in runtime.calls if verb == "tag" and "remove" in args
    ]
    assert tag_calls and tag_calls[-1]["remove"] == ["needs-triage"]


def test_edit_no_triage_tag_skips_tag_dispatch(client, runtime) -> None:
    """A normal (non-triaged) edit doesn't dispatch a tag removal."""
    resp = client.post(
        "/papers/10/edit", data={"title": "A real title"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert not [v for v, _ in runtime.calls if v == "tag"]


def test_paper_detail_has_edit_and_delete_forms(client) -> None:
    """Edit and Delete both render as working forms. Delete POSTs to the
    web-only soft-delete route (a real button, no longer disabled)."""
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert 'action="/papers/10/edit"' in resp.text
    assert "🗑 Delete paper" in resp.text
    assert 'action="/papers/10/delete"' in resp.text


def test_papers_index_hover_card_has_authors_and_abstract(client) -> None:
    resp = client.get("/papers")
    assert resp.status_code == 200
    # Author name (given + family) rendered in the hover card.
    assert "Jane Smith" in resp.text
    # Abstract with JATS/HTML tags stripped to plain text.
    assert "We study X in depth." in resp.text
    assert "<jats:p>" not in resp.text


def test_papers_index_abstract_backfilled_from_chunks(client) -> None:
    # Paper 11 has no meta abstract -> the body-chunk backfill fills the
    # hover card so it doesn't read "No abstract on file."
    resp = client.get("/papers")
    assert resp.status_code == 200
    assert "Body-derived abstract text for the second paper." in resp.text


def test_papers_index_hover_card_has_doi_link(client) -> None:
    # Paper 10 carries a DOI -> the hover card surfaces a clickable
    # doi.org link for quick verification.
    resp = client.get("/papers")
    assert resp.status_code == 200
    assert "https://doi.org/10.1234/example.2024" in resp.text


def test_papers_index_hover_card_has_arxiv_link(client) -> None:
    resp = client.get("/papers")
    assert resp.status_code == 200
    assert "https://arxiv.org/abs/2501.01234" in resp.text


def test_paper_detail_shows_doi_link(client) -> None:
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert "https://doi.org/10.1234/example.2024" in resp.text


def test_paper_pdf_404_when_missing(client) -> None:
    resp = client.get("/papers/10/pdf")
    assert resp.status_code == 400  # NotFound -> PrecisError handler


def test_paper_pdf_streams_when_present(client, tmp_path) -> None:
    # corpus layout: <corpus>/<letter>/<cite_key>.pdf  (letter = 's')
    pdf = tmp_path / "s" / "smith2024.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    resp = client.get("/papers/10/pdf")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/pdf"
    assert resp.content.startswith(b"%PDF")


def test_webconfig_parses_multiple_corpus_roots(monkeypatch) -> None:
    import os
    from pathlib import Path

    from precis_web.config import WebConfig

    monkeypatch.setenv("PRECIS_CORPUS_DIR", f"/opt/shared/corpus{os.pathsep}/opt/nas/c")
    cfg = WebConfig.from_env()
    # Compare via Path() so Windows' backslash normalisation
    # (``\opt\shared\corpus``) matches the POSIX-form input.
    assert Path(str(cfg.corpus_dir)) == Path("/opt/shared/corpus")
    assert [Path(str(p)) for p in cfg.extra_corpus_dirs] == [Path("/opt/nas/c")]
    assert [Path(str(p)) for p in cfg.corpus_dirs] == [
        Path("/opt/shared/corpus"),
        Path("/opt/nas/c"),
    ]


def test_pdf_resolves_from_second_corpus_root(runtime, tmp_path) -> None:
    # The primary root has no file; the PDF lives under a second root
    # (an NFS mount surfaced at a different path). Resolution must find
    # it rather than 404.
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    bad = tmp_path / "shared" / "corpus"
    good = tmp_path / "nas" / "corpus"
    pdf = good / "s" / "smith2024.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4 fake")
    cfg = WebConfig(corpus_dir=bad, extra_corpus_dirs=(good,))
    c = TestClient(create_app(runtime=runtime, web_config=cfg))
    resp = c.get("/papers/10/pdf")
    assert resp.status_code == 200
    assert resp.content.startswith(b"%PDF")


def test_paper_detail_lists_all_searched_roots(runtime, tmp_path) -> None:
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    bad1 = tmp_path / "shared"
    bad2 = tmp_path / "nas"
    cfg = WebConfig(corpus_dir=bad1, extra_corpus_dirs=(bad2,))
    c = TestClient(create_app(runtime=runtime, web_config=cfg))
    resp = c.get("/papers/10")
    assert resp.status_code == 200
    # Both candidate paths are listed in the diagnostics.
    assert str(bad1 / "s" / "smith2024.pdf") in resp.text
    assert str(bad2 / "s" / "smith2024.pdf") in resp.text


def test_paper_pdf_error_reports_resolved_path(client) -> None:
    # Held paper (pdf_sha256 set) but no file on disk → the error must
    # name the resolved path so a corpus_dir misconfig is diagnosable.
    resp = client.get("/papers/10/pdf")
    assert resp.status_code == 400
    assert "smith2024.pdf" in resp.text
    assert "PRECIS_CORPUS_DIR" in resp.text


def test_paper_detail_shows_lookup_path_when_held_but_missing(client) -> None:
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    # smith2024 has pdf_sha256 but no file under the tmp corpus_dir.
    assert "isn't where the server looked" in resp.text
    assert "smith2024.pdf" in resp.text


# ── refs (per-kind browse) ─────────────────────────────────────────


def test_refs_index_lists(client) -> None:
    resp = client.get("/refs/memory")
    assert resp.status_code == 200
    assert "A decision" in resp.text
    assert "An idea" in resp.text


def test_refs_index_search(client) -> None:
    resp = client.get("/refs/memory", params={"q": "idea"})
    assert resp.status_code == 200
    assert "relevance" in resp.text  # search-mode banner


def test_refs_unknown_kind_rejected(client) -> None:
    resp = client.get("/refs/banana")
    assert resp.status_code == 400  # NotFound -> PrecisError handler


def test_refs_detail_dispatches_get_by_id(client, runtime) -> None:
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "get"
    assert args == {"kind": "memory", "id": 20}


def test_refs_detail_slug_kind_addresses_by_slug(client, runtime) -> None:
    resp = client.get("/refs/oracle/30")
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "get"
    assert args == {"kind": "oracle", "id": "planck-constant"}


def test_refs_detail_wrong_kind_404(client) -> None:
    # id 20 is a memory; requesting it under /refs/oracle must not match.
    resp = client.get("/refs/oracle/20")
    assert resp.status_code == 400


def test_memory_refs_detail_renders_references_panel(client, runtime) -> None:
    """Memory detail body containing kind:ref handles → References
    panel appears with one resolved row per cited handle."""
    # Plant a body with two refs: a paper (slug) + a memory (numeric).
    body_text = (
        "I notice that paper:smith2024 connects with memory:20 — "
        "see also patent:nope404 which won't resolve."
    )
    runtime.dispatch_with_status = lambda verb, args: (body_text, False)
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    assert "References" in resp.text
    # Resolved refs render with their URL.
    assert "/r/paper/smith2024" in resp.text
    # The data-ref-line attribute carries the plain-text cite form for
    # the Copy All button.
    assert "data-ref-line=" in resp.text
    # Copy button present.
    # Copy-as menu present (extends MVP with Markdown / BibTeX too, #189).
    assert "Copy as" in resp.text
    assert "Markdown" in resp.text
    assert "BibTeX" in resp.text


def test_memory_refs_detail_missing_ref_flagged(client, runtime) -> None:
    """Handles that don't resolve render with the (not found) marker
    and a rose-700 class so the eye lands on them."""
    body_text = "Wrong: paper:doesnotexist."
    runtime.dispatch_with_status = lambda verb, args: (body_text, False)
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    assert "(not found)" in resp.text


def test_memory_refs_detail_inline_footnote_markers(client, runtime) -> None:
    """Each handle in the body gets an inline ``[N]`` marker that
    cross-links to the references list (#190)."""
    body_text = "Body says paper:smith2024 and memory:20."
    runtime.dispatch_with_status = lambda verb, args: (body_text, False)
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    # ``[1]`` for paper, ``[2]`` for memory — preserving appearance order.
    assert 'href="#ref-1"' in resp.text
    assert 'href="#ref-2"' in resp.text
    # References list entries carry id="ref-N" so the markers can jump.
    assert 'id="ref-1"' in resp.text
    assert 'id="ref-2"' in resp.text


def test_memory_refs_detail_verification_badges(client, runtime) -> None:
    """Resolved/stub/deleted/missing get distinct status badges (#191)."""
    body_text = "Resolved: memory:20. Missing: paper:doesnotexist."
    runtime.dispatch_with_status = lambda verb, args: (body_text, False)
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    # Resolved badge (memory:20 has body content via FakeStore).
    assert "✓" in resp.text
    # Missing badge (paper:doesnotexist).
    assert "✗" in resp.text
    # Legend at the bottom of the panel.
    assert "stub awaiting fetch" in resp.text


def test_non_memory_refs_detail_omits_references_panel(client, runtime) -> None:
    """References auto-extract only runs on memory views (the MVP scope
    — that's where dreams live). Other kinds render unchanged."""
    body_text = "paper:smith2024 cited inside this oracle"
    runtime.dispatch_with_status = lambda verb, args: (body_text, False)
    resp = client.get("/refs/oracle/30")
    assert resp.status_code == 200
    # Heading shouldn't appear on non-memory views.
    assert "References" not in resp.text


def test_job_detail_shows_actions_not_ask_think(client, runtime) -> None:
    """A failed job's detail page offers unstick affordances (retry /
    transcript / parent) instead of the dream-memory ``Ask & think``
    box — the fix for landing on a closed:failed plan_tick with no way
    to act on it."""
    runtime.dispatch_with_status = lambda verb, args: (
        "# job 80\nstatus: failed\n## event 0\njob-swept: running since …",
        False,
    )
    resp = client.get("/refs/job/80")
    assert resp.status_code == 200
    # The retry form posts to the existing tasks retry route.
    assert 'action="/tasks/80/retry"' in resp.text
    assert "Retry job" in resp.text
    # LLM-planner parent → the model-swap dropdown is offered.
    assert 'name="model"' in resp.text
    assert "retry on sonnet" in resp.text
    # Transcript + parent affordances.
    assert "/tasks/80/transcript" in resp.text
    assert "/tasks?focus=81" in resp.text
    # The generic dream-memory Ask & think box is suppressed for jobs.
    assert "Ask &amp; think" not in resp.text
    assert 'action="/refs/job/80/ask"' not in resp.text


def test_orphan_job_detail_blocks_retry(client, runtime) -> None:
    """A job with no todo parent can't be re-minted; the strip explains
    that instead of offering a button that would just error."""
    runtime.dispatch_with_status = lambda verb, args: ("# job 82\nstatus: ?", False)
    resp = client.get("/refs/job/82")
    assert resp.status_code == 200
    # No retry form (orphan / non-failed).
    assert 'action="/tasks/82/retry"' not in resp.text
    # Still no dream-memory box.
    assert "Ask &amp; think" not in resp.text


def test_refs_detail_kind_outside_legacy_nav_renders_200(client) -> None:
    """``web`` (and friends) are in ``_REFS_BROWSABLE_KINDS`` but were
    NOT in the legacy 6-kind nav set ``_REF_KIND_LABEL`` covers.
    The detail route used to KeyError on the label lookup; verify it
    falls back to a sensible auto-Title-Case label."""
    resp = client.get("/refs/web/70")
    assert resp.status_code == 200
    # The fallback label uses the kind name title-cased.
    assert "Web" in resp.text


def test_conv_detail_renders_transcript_not_agent_card(client, runtime) -> None:
    # Clicking a conversation shows the turns, not the handler's
    # agent-facing overview card (no `get` dispatch, no `Next:` call
    # affordances).
    resp = client.get("/refs/conv/40")
    assert resp.status_code == 200
    assert "hello there" in resp.text
    assert "general kenobi" in resp.text
    assert "alice" in resp.text
    assert "bob" in resp.text
    # The transcript view reads blocks directly; it must not route
    # through the get verb (which would emit the agent card).
    assert not any(verb == "get" for verb, _ in runtime.calls)


def test_conv_detail_shows_turn_count(client) -> None:
    resp = client.get("/refs/conv/40")
    assert resp.status_code == 200
    assert "2 turns" in resp.text


def test_conv_detail_renders_full_meta_per_turn(client, runtime) -> None:
    """Every meta field on a turn surfaces — chunk_kind badge + extra strip.

    The transcript shows author + ts inline (dedicated fields) and
    flattens everything else (stop_reason, token counts, msg_id, …)
    into a key/value strip per turn. The operator should see the full
    record without dropping into ``get(view='last-meta')``.
    """
    from types import SimpleNamespace

    original_blocks = runtime.store.list_blocks_for_ref

    fake_turns = [
        SimpleNamespace(
            pos=0,
            text="user said hi",
            chunk_kind="conv_message",
            meta={
                "author": "elmsfeuer",
                "ts": "2026-06-15T20:00:00Z",
                "chunk_kind": "conv_message",
                "msg_id": "discord:111",
            },
        ),
        SimpleNamespace(
            pos=1,
            text="assistant replied",
            chunk_kind="conv_message",
            meta={
                "author": "asa",
                "ts": "2026-06-15T20:00:05Z",
                "chunk_kind": "conv_message",
                "stop_reason": "end_turn",
                "input_tokens": 420,
                "output_tokens": 137,
                "model": "claude-opus-4-7",
            },
        ),
    ]

    def blocks(ref_id, **kw):
        if ref_id == 40:
            return list(fake_turns)
        return original_blocks(ref_id, **kw)

    runtime.store.list_blocks_for_ref = blocks  # type: ignore[assignment]
    try:
        resp = client.get("/refs/conv/40")
    finally:
        runtime.store.list_blocks_for_ref = original_blocks  # type: ignore[assignment]

    assert resp.status_code == 200
    # chunk_kind badge per turn.
    assert resp.text.count("conv_message") >= 2
    # Author + text still visible.
    assert "elmsfeuer" in resp.text and "asa" in resp.text
    assert "user said hi" in resp.text and "assistant replied" in resp.text
    # Every extra-meta field surfaces (key + value).
    for needle in (
        "msg_id",
        "discord:111",
        "stop_reason",
        "end_turn",
        "input_tokens",
        "420",
        "output_tokens",
        "137",
        "model",
        "claude-opus-4-7",
    ):
        assert needle in resp.text, f"{needle!r} missing from rendered transcript"


def test_refs_nav_tabs_present(client) -> None:
    """After T12.6 the nav collapses memory/conv/gripe/pres into one Refs
    tab. Oracle and Patents keep their own tabs (per-kind UX they need).
    The consolidated Refs tab also links to the per-kind list pages from
    inside the kind sections.
    """
    resp = client.get("/refs/memory")
    for href in (
        "/refs",  # consolidated browser
        "/refs/oracle",  # oracle keeps its own tab (roll the dice)
        "/refs/patent",  # patents keep their own tab (OPS remote search)
    ):
        assert href in resp.text


def test_refs_consolidated_default_renders(client) -> None:
    """``GET /refs`` (no args) lights the 4 default kinds and renders."""
    resp = client.get("/refs")
    assert resp.status_code == 200
    # Default-checked kinds shown as checkbox labels.
    for kind in ("memory", "conv", "gripe", "pres"):
        assert f'value="{kind}"' in resp.text
    # The all=1 escape hatch is in the form.
    assert 'name="all"' in resp.text


def test_refs_consolidated_all_flag_lights_every_kind(client) -> None:
    """``?all=1`` selects every browsable kind, regardless of ``kinds``."""
    resp = client.get("/refs?all=1")
    assert resp.status_code == 200
    # Non-default kinds (perplexity-research, paper, etc.) should show
    # up as checked.
    assert 'value="paper"' in resp.text
    assert 'value="patent"' in resp.text
    assert 'value="perplexity-research"' in resp.text


def test_refs_consolidated_kinds_param_narrows(client) -> None:
    resp = client.get("/refs?kinds=memory")
    assert resp.status_code == 200
    # All checkboxes render either way; what changes is which are checked.
    assert 'value="memory"' in resp.text


def test_refs_detail_renders_tag_strip_empty_state(client) -> None:
    """When a ref has no tags, the detail page shows the empty marker
    and the ``+ tag`` input."""
    resp = client.get("/refs/memory/20")  # FakeStore canned memory id=20
    assert resp.status_code == 200
    assert "no tags yet" in resp.text
    # The add form is present with the expected action.
    assert 'action="/refs/memory/20/tags"' in resp.text
    assert 'name="add"' in resp.text


def test_refs_detail_tags_post_dispatches_tag_add(client, runtime) -> None:
    """POST /refs/{kind}/{ref_id}/tags with ``add=`` flows through the
    handler dispatch, preserving the single-source vocabulary check."""
    resp = client.post(
        "/refs/memory/20/tags",
        data={"add": "topic:foo"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args["kind"] == "memory"
    assert args["id"] == 20
    assert args["add"] == ["topic:foo"]


def test_refs_detail_tags_post_dispatches_tag_remove(client, runtime) -> None:
    resp = client.post(
        "/refs/memory/20/tags",
        data={"remove": "topic:foo"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args["remove"] == ["topic:foo"]


def test_refs_detail_tags_no_op_redirects(client, runtime) -> None:
    """Empty add + empty remove → just bounce back, no dispatch."""
    before = len(runtime.calls)
    resp = client.post(
        "/refs/memory/20/tags",
        data={},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(runtime.calls) == before  # no tag verb invoked


def test_env_index_lists_all_agents(client) -> None:
    """``GET /env`` renders the empty-state agent list — one row per
    AgentSpec — without invoking anything."""
    resp = client.get("/env")
    assert resp.status_code == 200
    # Each agent label must surface so the operator can pick it.
    for label in (
        "Dream agent",
        "Structural reviewer",
        "Deep review",
        "Claude in-process executor",
    ):
        assert label in resp.text


def test_env_select_dream_renders_detail(client, monkeypatch) -> None:
    """``GET /env?agent=dream_agent`` reads the dream daemon's plist
    env and renders the system + directive prompt inline. The web
    process's own env is irrelevant here — we stub the plist read."""
    from precis_web.routes import env as env_mod

    monkeypatch.setattr(
        env_mod,
        "_read_plist_env",
        lambda label: {
            "PRECIS_DREAM_AGENT": "1",
            "PRECIS_DREAM_PROMPT_PATH": "/etc/hostname",
            "PRECIS_DREAM_SOUL_PATH": "/etc/hostname",
        },
    )
    resp = client.get("/env?agent=dream_agent")
    assert resp.status_code == 200
    # Model fallback to the default when the model env is unset.
    assert "claude-opus-4-8" in resp.text
    # Env-var snapshot shows the gating flag as present.
    assert "PRECIS_DREAM_AGENT" in resp.text
    # Plist breadcrumb so the operator knows where the env came from.
    assert "com.precis.dream.plist" in resp.text


def test_env_missing_mcp_config_shows_warning(client, monkeypatch) -> None:
    """When the plist's ``PRECIS_MCP_CONFIG`` points at a non-existent
    file the template surfaces a "MCP config not found" warning rather
    than crashing."""
    from precis_web.routes import env as env_mod

    monkeypatch.setattr(
        env_mod,
        "_read_plist_env",
        lambda label: {"PRECIS_MCP_CONFIG": "/tmp/no-such-mcp-config.json"},
    )
    resp = client.get("/env?agent=dream_agent")
    assert resp.status_code == 200
    assert "MCP config not found" in resp.text


def test_env_missing_plist_surfaces_red_banner(client, monkeypatch) -> None:
    """If the agent's plist is absent (e.g. running outside the cluster)
    the page renders without crashing and the operator sees a clear
    "NOT FOUND" marker rather than silent unsets."""
    import tempfile
    from pathlib import Path as _Path

    from precis_web.routes import env as env_mod

    monkeypatch.setattr(env_mod, "_read_plist_env", lambda label: {})
    monkeypatch.setattr(env_mod, "_PLIST_DIR", _Path(tempfile.mkdtemp()))
    resp = client.get("/env?agent=dream_agent")
    assert resp.status_code == 200
    assert "NOT FOUND" in resp.text


def test_env_in_base_nav(client) -> None:
    """The Env tab is reachable from base.html.j2 nav."""
    resp = client.get("/tasks")
    assert resp.status_code == 200
    assert 'href="/env"' in resp.text


def test_loupe_in_base_nav(client) -> None:
    """The 🔍 loupe form posts to /refs?all=1 so cross-kind search lands
    on the consolidated browser with everything lit."""
    resp = client.get("/tasks")  # any page; loupe is in base
    assert resp.status_code == 200
    assert 'action="/refs"' in resp.text
    # Hidden ``all=1`` field arms the cross-kind scope.
    assert 'name="all" value="1"' in resp.text


# ── task tags ──────────────────────────────────────────────────────


def test_task_add_tag_dispatches_tag_add(client, runtime) -> None:
    resp = client.post(
        "/tasks/2/tags",
        data={"add": "project:precis context:work"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args == {"kind": "todo", "id": 2, "add": ["project:precis", "context:work"]}


def test_task_remove_tag_dispatches_tag_remove(client, runtime) -> None:
    client.post(
        "/tasks/2/tags", data={"remove": "project:precis"}, follow_redirects=False
    )
    verb, args = runtime.calls[-1]
    assert verb == "tag"
    assert args == {"kind": "todo", "id": 2, "remove": ["project:precis"]}


def test_task_empty_tag_call_is_noop(client, runtime) -> None:
    client.post("/tasks/2/tags", data={}, follow_redirects=False)
    assert runtime.calls == []  # nothing dispatched


def test_task_edit_text_dispatches_edit_verb(client, runtime) -> None:
    client.post(
        "/tasks/2/edit", data={"text": "Polished title"}, follow_redirects=False
    )
    verb, args = runtime.calls[-1]
    assert verb == "edit"
    assert args == {
        "kind": "todo",
        "id": 2,
        "mode": "replace",
        "text": "Polished title",
    }


def test_task_edit_blank_text_is_noop(client, runtime) -> None:
    client.post("/tasks/2/edit", data={"text": "   "}, follow_redirects=False)
    assert runtime.calls == []  # nothing dispatched


def test_task_tag_error_renders_inline_instead_of_silent_redirect(
    client, runtime
) -> None:
    # A rejected tag (invalid vocab, guard veto) must surface the
    # handler message, not silently redirect leaving the operator
    # wondering why the tag "didn't show up".
    runtime.error_verbs = {"tag"}
    resp = client.post("/tasks/2/tags", data={"add": "bogus"}, follow_redirects=False)
    assert resp.status_code == 400
    assert "rejected by handler" in resp.text


def test_task_edit_error_renders_inline(client, runtime) -> None:
    runtime.error_verbs = {"edit"}
    resp = client.post(
        "/tasks/2/edit", data={"text": "new text"}, follow_redirects=False
    )
    assert resp.status_code == 400
    assert "rejected by handler" in resp.text


def test_job_notes_splits_events_and_summary() -> None:
    """``_job_notes`` groups job_event (failure reasons) vs job_summary."""
    from contextlib import contextmanager

    from precis_web.routes.tasks import _job_notes

    rows = [
        (6689, "job_event", "runner: timeout after 600s"),
        (6689, "job_summary", "planner minted 3 children, then timed out"),
        (6690, "job_event", "runner: uncaught exception: ValueError()"),
    ]

    class _Cur:
        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cur()

    class _Pool:
        @contextmanager
        def connection(self):
            yield _Conn()

    store = type("S", (), {"pool": _Pool()})()
    out = _job_notes(store, [6689, 6690])
    assert out[6689]["events"] == ["runner: timeout after 600s"]
    assert "timed out" in out[6689]["summary"]
    assert out[6690]["events"] == ["runner: uncaught exception: ValueError()"]
    assert out[6690]["summary"] == ""


def test_job_notes_includes_result() -> None:
    """``_job_notes`` surfaces the structured job_result audit chunk."""
    from contextlib import contextmanager

    from precis_web.routes.tasks import _job_notes

    rows = [
        (6689, "job_result", "verdict (LLM): continue\nsubtasks minted: 5"),
        (6689, "job_summary", "planner minted 5 children"),
    ]

    class _Cur:
        def fetchall(self):
            return rows

    class _Conn:
        def execute(self, *_a, **_k):
            return _Cur()

    class _Pool:
        @contextmanager
        def connection(self):
            yield _Conn()

    store = type("S", (), {"pool": _Pool()})()
    out = _job_notes(store, [6689])
    assert "verdict (LLM): continue" in out[6689]["result"]
    assert "5 children" in out[6689]["summary"]


def test_resolve_workspace_pdf(tmp_path) -> None:
    """``_resolve_workspace_pdf`` returns the path only when it exists."""
    from precis_web.routes.tasks import _resolve_workspace_pdf

    meta = {
        "workspace": {
            "path": "projects/demo",
            "format": "tex",
            "entrypoint": "main.tex",
        }
    }
    # No PRECIS_ROOT → nothing to resolve.
    assert _resolve_workspace_pdf(None, meta) is None
    # No workspace block → None.
    assert _resolve_workspace_pdf(tmp_path, {}) is None
    # Workspace but no compiled PDF on disk yet → None.
    assert _resolve_workspace_pdf(tmp_path, meta) is None
    # PDF present → its path.
    ws = tmp_path / "projects" / "demo"
    ws.mkdir(parents=True)
    (ws / "main.pdf").write_bytes(b"%PDF-1.4 demo")
    got = _resolve_workspace_pdf(tmp_path, meta)
    assert got is not None
    assert got.name == "main.pdf"


def test_task_pdf_route_serves_and_rejects(runtime, tmp_path) -> None:
    """GET /tasks/{id}/pdf streams a compiled workspace PDF, else errors."""
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    # Todo #1 gets a workspace with a compiled PDF; #2 has none.
    runtime.store.todos[0].meta = {
        "workspace": {
            "path": "projects/demo",
            "format": "tex",
            "entrypoint": "main.tex",
        }
    }
    ws = tmp_path / "projects" / "demo"
    ws.mkdir(parents=True)
    (ws / "main.pdf").write_bytes(b"%PDF-1.4 demo")
    app = create_app(runtime=runtime, web_config=WebConfig(precis_root=tmp_path))
    client = TestClient(app)

    served = client.get("/tasks/1/pdf")
    assert served.status_code == 200
    assert served.headers["content-type"] == "application/pdf"

    # No workspace PDF → PrecisError (NotFound) → 400, like the papers route.
    missing = client.get("/tasks/2/pdf")
    assert missing.status_code == 400


def test_history_attempt_detail_renders(client, monkeypatch) -> None:
    """The history fragment exposes per-attempt failure/summary detail."""
    from precis_web.routes import tasks as tasks_mod

    # Inject one failed attempt with a failure reason + summary.
    monkeypatch.setattr(
        tasks_mod,
        "_child_jobs",
        lambda store, ids: (
            [{"id": 6689, "parent_id": 2, "title": "plan_tick", "lease_until": None}]
            if 2 in ids
            else []
        ),
    )
    monkeypatch.setattr(
        tasks_mod,
        "_job_notes",
        lambda store, ids: {
            6689: {"events": ["runner: timeout after 600s"], "summary": "did stuff"}
        },
    )
    resp = client.get("/tasks/2/history")
    assert resp.status_code == 200
    assert "timeout after 600s" in resp.text


def test_task_history_renders_event_log(client) -> None:
    resp = client.get("/tasks/2/history")
    assert resp.status_code == 200
    assert "Event log" in resp.text
    assert "status:done" in resp.text
    assert "Attempts" in resp.text


def test_task_history_empty_log(client) -> None:
    resp = client.get("/tasks/1/history")
    assert resp.status_code == 200
    assert "No recorded events yet" in resp.text


# ── console ────────────────────────────────────────────────────────


def test_console_get(client) -> None:
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "Tool console" in resp.text


def test_console_get_blank_does_not_dispatch(client, runtime) -> None:
    """A bare ``/console`` (no args_text query param) runs nothing."""
    resp = client.get("/console")
    assert resp.status_code == 200
    assert runtime.calls == []


def test_console_get_deeplink_runs_search(client, runtime) -> None:
    """``GET /console?verb=search&args_text=…`` prefills *and* runs the
    read-only verb, so a shared link lands on an already-run query."""
    resp = client.get(
        "/console",
        params={"verb": "search", "args_text": 'kind=paper q="attention"'},
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "search"
    assert args["kind"] == "paper"
    assert args["q"] == "attention"
    # Form is prefilled with the args + the result rendered.
    assert "value='kind=paper q=\"attention\"'" in resp.text or (
        "kind=paper q=&#34;attention&#34;" in resp.text
    )
    assert "[search] ok" in resp.text


def test_console_get_deeplink_runs_get_toc(client, runtime) -> None:
    """The TOC deep-link dispatches ``get`` with ``view=toc``."""
    resp = client.get(
        "/console",
        params={"verb": "get", "args_text": "kind=paper id=pa2928 view=toc"},
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "get"
    assert args == {"kind": "paper", "id": "pa2928", "view": "toc"}


def test_console_get_deeplink_mutation_verb_prefills_only(client, runtime) -> None:
    """A GET must stay safe: a ``delete``/``put`` deep-link prefills the
    form but never fires (so a prefetch / shared link can't mutate)."""
    resp = client.get(
        "/console",
        params={"verb": "delete", "args_text": "kind=memory id=42"},
    )
    assert resp.status_code == 200
    assert runtime.calls == []  # not dispatched


def test_console_get_deeplink_bad_arg_surfaces_error(client, runtime) -> None:
    resp = client.get("/console", params={"verb": "search", "args_text": "novalue"})
    assert resp.status_code == 200
    assert "input error" in resp.text
    assert runtime.calls == []


def test_console_run_dispatches(client, runtime) -> None:
    resp = client.post(
        "/console/run", data={"verb": "search", "args_text": "kind=paper q=foo"}
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "search"
    assert args["kind"] == "paper"
    assert args["q"] == "foo"
    assert "[search] ok" in resp.text


def test_console_run_tolerates_comma_separated_args(client, runtime) -> None:
    """Python-call-style ``kind=draft, id=test01`` parses the same as the
    space-separated form — the trailing comma is stripped, not dispatched
    as part of the kind (which bounced as 'unknown kind: draft,')."""
    resp = client.post(
        "/console/run",
        data={"verb": "get", "args_text": "kind=draft, id=test01"},
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "get"
    assert args["kind"] == "draft"  # no trailing comma
    assert args["id"] == "test01"


def test_console_run_bad_arg_no_dispatch(client, runtime) -> None:
    resp = client.post("/console/run", data={"verb": "search", "args_text": "novalue"})
    assert resp.status_code == 200
    assert "input error" in resp.text
    assert runtime.calls == []  # nothing dispatched


def test_console_quick_online_get(client, runtime) -> None:
    """Online mode → ``get(kind=..., id=<query>)`` for the picked service."""
    resp = client.post(
        "/console/quick",
        data={"service": "math", "mode": "online", "query": "population of Ireland"},
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "get"
    assert args == {"kind": "math", "id": "population of Ireland"}
    # Breadcrumb so the operator can verify what ran. The ``'`` quotes
    # render as ``&#39;`` (Jinja autoescape of text content; displays as
    # ``'`` in the browser).
    assert "get(kind=&#39;math&#39;" in resp.text


def test_console_quick_cache_search(client, runtime) -> None:
    """Cache mode → ``search(kind=..., q=<query>)``."""
    resp = client.post(
        "/console/quick",
        data={
            "service": "perplexity-research",
            "mode": "cache",
            "query": "two-photon absorption",
        },
    )
    assert resp.status_code == 200
    verb, args = runtime.calls[-1]
    assert verb == "search"
    assert args == {"kind": "perplexity-research", "q": "two-photon absorption"}


def test_console_quick_unknown_service_no_dispatch(client, runtime) -> None:
    resp = client.post(
        "/console/quick",
        data={"service": "bogus", "mode": "online", "query": "anything"},
    )
    assert resp.status_code == 200
    assert "unknown service" in resp.text
    assert runtime.calls == []


def test_console_quick_empty_query_no_dispatch(client, runtime) -> None:
    resp = client.post(
        "/console/quick",
        data={"service": "youtube", "mode": "online", "query": "   "},
    )
    assert resp.status_code == 200
    assert "query is required" in resp.text
    assert runtime.calls == []


def test_console_quick_youtube_hint_rendered(client) -> None:
    """The YouTube-id hint must surface on the page so newcomers know
    what to paste."""
    resp = client.get("/console")
    assert resp.status_code == 200
    assert "dQw4w9WgXcQ" in resp.text  # example video id in the hint


def test_console_examples_grouped_box_rendered(client) -> None:
    """The examples box renders every group's title + dropdown options,
    spanning multiple kinds (not just paper)."""
    from precis_web.routes.console import CONSOLE_EXAMPLES

    resp = client.get("/console")
    assert resp.status_code == 200
    # The group dropdown (Alpine x-model) and every group title.
    assert "All groups" in resp.text
    assert 'x-model="grp"' in resp.text
    for g in CONSOLE_EXAMPLES:
        # Group titles carry ``&`` → HTML-escaped to ``&amp;`` on render.
        assert g["group"].replace("&", "&amp;") in resp.text
        assert f'value="{g["key"]}"' in resp.text
    # Representative breadth: examples reach well beyond kind=paper.
    for kind in ("kind=todo", "kind=skill", "kind=oracle", "kind=calc"):
        assert kind in resp.text


def test_console_resolve_record_handle(client, runtime) -> None:
    """A universal record handle (``pa10``) routes through the ``/r/``
    resolver — not the cite_key shape (which would 404 on ``/r/paper/pa10``)."""
    resp = client.post(
        "/console/resolve", data={"handle": "pa10"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/r/paper/10"


def test_console_resolve_chunk_handle_carries_ord(client, runtime) -> None:
    """A paper chunk handle (``pc500``) resolves the owning ref + the
    chunk's ord, landing on the cited-passage surface (``?chunk=4``)."""
    runtime.store.chunk_handles[500] = (10, 4, "paper")
    resp = client.post(
        "/console/resolve", data={"handle": "pc500"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/r/paper/10?chunk=4"


def test_console_resolve_unknown_handle_falls_back_to_search(client, runtime) -> None:
    """A handle-shaped string with no live row (``me999``) falls through
    to the cross-kind search rather than 404ing."""
    resp = client.post(
        "/console/resolve", data={"handle": "me999"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/refs?q=me999")


def test_console_result_echoes_request(client, runtime) -> None:
    """The result panel prints the call that produced the output so the
    request is read alongside the response."""
    resp = client.get(
        "/console",
        params={"verb": "search", "args_text": "kind=paper q=foo"},
    )
    assert resp.status_code == 200
    assert "request" in resp.text
    # verb + args echoed in the panel.
    assert "search kind=paper q=foo" in resp.text


# ── alerts ─────────────────────────────────────────────────────────


def test_alerts_page_renders_all_clear(client) -> None:
    """Under the fake store (empty pool) the open view is all-clear."""
    resp = client.get("/alerts")
    assert resp.status_code == 200
    assert "Alerts" in resp.text
    assert "All clear" in resp.text
    assert "0 open" in resp.text


def test_alerts_resolved_view_renders(client) -> None:
    resp = client.get("/alerts?state=resolved")
    assert resp.status_code == 200
    # The resolved/open toggle is present and the resolved view is selected.
    assert "/alerts?state=resolved" in resp.text
    assert "No resolved alerts" in resp.text


# ── status ─────────────────────────────────────────────────────────


def test_status_page_renders(client) -> None:
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Status" in resp.text
    # New telemetry panels render (empty states under the fake store).
    assert "Machines" in resp.text
    assert "Claude usage" in resp.text
    assert "precis heartbeat" in resp.text  # empty-state hint
    # Background-health panel: empty under the fake store → green all-clear.
    assert "Background health" in resp.text
    assert "No spin loops or failed passes" in resp.text
    # The slow backlog panel is lazy-loaded — the page ships only the
    # htmx placeholder, not the full-table-scan counts.
    assert 'hx-get="/status/backlog"' in resp.text
    assert "loading backlog" in resp.text


def test_status_backlog_shows_last_done(client, monkeypatch) -> None:
    """Each pipeline-backlog row shows when the pass last did work.

    Served by the lazy ``/status/backlog`` fragment (deferred off the
    main page so its full-table ``chunks`` scans don't block render).
    A pass with a recent productive batch renders its relative age
    ('Nm ago'); a pass that never ran renders the em-dash placeholder.
    """
    from datetime import UTC, datetime, timedelta

    from precis_web.routes import status as status_mod

    recent = datetime.now(UTC) - timedelta(minutes=3)
    monkeypatch.setattr(
        status_mod,
        "_backlog_counts",
        lambda store: {
            "embed": {"pending": 10, "done": 90, "last_ts": recent},
            "summarize": {"pending": 5, "done": 95},  # never ran → no last_ts
        },
    )
    resp = client.get("/status/backlog")
    assert resp.status_code == 200
    assert "3m ago" in resp.text  # embed's last productive batch
    assert "last productive batch" in resp.text  # tooltip label


def test_status_telemetry_panels_render_data(client, monkeypatch) -> None:
    """Heartbeat + usage + host panels render injected data."""
    from precis_web.routes import status as status_mod

    monkeypatch.setattr(
        status_mod,
        "_heartbeats",
        lambda store: [
            {
                "host": "caspar",
                "ago": "2m ago",
                "stale": False,
                "temp_c": 91.5,
                "load1": 3.21,
                "load5": 2.10,
                "load15": 1.05,
            }
        ],
    )
    monkeypatch.setattr(
        status_mod,
        "_hosts",
        lambda store: [
            {"host": "caspar", "ago": "1m ago", "stale": False, "problems": 4}
        ],
    )
    monkeypatch.setattr(
        status_mod,
        "_claude_usage",
        lambda store: {
            "day": {"calls": 7, "cost": 1.23},
            "week": {"calls": 40, "cost": 9.99},
            "by_model": [{"label": "claude-sonnet-4-6", "calls": 40, "cost": 9.99}],
        },
    )
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "caspar" in resp.text
    assert "91.5 °C" in resp.text  # hot temp rendered
    assert "$1.23" in resp.text  # 24h cost
    assert "claude-sonnet-4-6" in resp.text
    assert "4 err/warn 24h" in resp.text


def test_status_background_health_panel_renders_anomalies(client, monkeypatch) -> None:
    """Spin-loop + failed-pass rows render in the Background health panel."""
    from precis_web.routes import status as status_mod

    monkeypatch.setattr(
        status_mod,
        "_background_anomalies",
        lambda store: {
            "spin_loops": [
                {
                    "ref_id": 34814,
                    "source": "fetcher:s2",
                    "last_event": "no_oa_version",
                    "count": 2030,
                }
            ],
            "failed_passes": [
                {
                    "host": "melchior",
                    "handler": "deep_review",
                    "failed": 3,
                    "ago": "5m ago",
                }
            ],
        },
    )
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Spin loops" in resp.text
    assert "#34814" in resp.text
    assert "fetcher:s2" in resp.text
    assert "2030" in resp.text
    assert "Failed passes" in resp.text
    assert "deep_review" in resp.text
    # The green all-clear state must be gone when anomalies exist.
    assert "No spin loops or failed passes" not in resp.text


def test_status_ago_formatter() -> None:
    from datetime import UTC, datetime, timedelta

    from precis_web.routes.status import _ago

    now = datetime.now(UTC)
    assert _ago(now - timedelta(seconds=30)).endswith("s ago")
    assert _ago(now - timedelta(minutes=10)).endswith("m ago")
    assert _ago(now - timedelta(hours=5)).endswith("h ago")
    assert _ago(now - timedelta(days=3)).endswith("d ago")
    assert _ago(None) == ""


# ── tasks tag filter ───────────────────────────────────────────────


def test_tasks_url_helper_encodes_each_tag_separately() -> None:
    from precis_web.routes.tasks import _tasks_url

    assert _tasks_url([], []) == "/tasks"
    assert _tasks_url(["a"], []) == "/tasks?require=a"
    assert (
        _tasks_url(["a", "STATUS:doing"], ["level:strategic"])
        == "/tasks?require=a&require=STATUS%3Adoing&exclude=level%3Astrategic"
    )


def _row(id, kind="todo", parent_id=None, status="open", level="", tags=None, depth=0):
    return {
        "id": id,
        "kind": kind,
        "parent_id": parent_id,
        "title": f"row {id}",
        "status": status,
        "level": level,
        "tags": list(tags or []),
        "depth": depth,
        "rollup": {"active": 0, "waiting": 0, "done": 0, "total": 0},
        "is_leaf": True,
        "locked": False,
        "lease_until": None,
        "lease_active": False,
        "attention_icons": [],
        "note": "",
    }


def test_filter_rows_passthrough_when_no_filter() -> None:
    from precis_web.routes.tasks import _filter_rows

    rows = [_row(1), _row(2, parent_id=1)]
    assert _filter_rows(rows, require=[], exclude=[]) == rows


def test_filter_rows_require_and_exclude_and_ancestors() -> None:
    from precis_web.routes.tasks import _filter_rows

    rows = [
        _row(1, tags=["project:precis"]),
        _row(2, parent_id=1, tags=["project:precis", "ask-user:Q"]),
        _row(3, parent_id=2, tags=["ask-user:Q"]),
        _row(4, parent_id=1, tags=["project:precis"]),
    ]
    kept = _filter_rows(rows, require=["ask-user:Q"], exclude=[])
    kept_ids = [r["id"] for r in kept]
    # 2 and 3 match; 1 pulled in as ancestor context; 4 dropped.
    assert kept_ids == [1, 2, 3]


def test_filter_rows_status_and_level_match() -> None:
    """STATUS:* and level:* are matchable like free tags."""
    from precis_web.routes.tasks import _filter_rows

    rows = [
        _row(1, status="doing", level="strategic"),
        _row(2, status="done", level="subtask"),
    ]
    kept = _filter_rows(rows, require=["STATUS:doing"], exclude=[])
    assert [r["id"] for r in kept] == [1]
    kept = _filter_rows(rows, require=[], exclude=["STATUS:done"])
    assert [r["id"] for r in kept] == [1]


def test_filter_rows_jobs_ride_along_with_matching_parent() -> None:
    from precis_web.routes.tasks import _filter_rows

    rows = [
        _row(1, tags=["ask-user:Q"]),
        _row(99, kind="job", parent_id=1, tags=[]),
        _row(2, tags=["project:other"]),
        _row(98, kind="job", parent_id=2, tags=[]),
    ]
    kept = _filter_rows(rows, require=["ask-user:Q"], exclude=[])
    assert [r["id"] for r in kept] == [1, 99]


def test_dashboard_query_filters_rows(client, runtime) -> None:
    """Tags supplied via query string filter the rendered tree."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="Build the thing"),
        make_ref(id=2, kind="todo", title="Draft the spec", parent_id=1),
    ]
    # Inject filtering at the helper level so we don't need the fake
    # tag-join SQL to wire up ask-user tags.
    import precis_web.routes.tasks as tasks_mod

    monkey_calls: list[tuple[list[str], list[str]]] = []
    real_filter = tasks_mod._filter_rows

    def spy(rows, *, require, exclude):
        monkey_calls.append((list(require), list(exclude)))
        return real_filter(rows, require=require, exclude=exclude)

    tasks_mod._filter_rows = spy
    try:
        resp = client.get("/tasks?require=ask-user%3AQ&exclude=STATUS%3Adone")
    finally:
        tasks_mod._filter_rows = real_filter

    assert resp.status_code == 200
    assert monkey_calls and monkey_calls[-1] == (["ask-user:Q"], ["STATUS:done"])
    # The filter form pre-fills the operator's input back into the box.
    assert 'value="ask-user:Q"' in resp.text
    assert 'value="STATUS:done"' in resp.text


def test_status_post_preserves_filter_in_redirect(client) -> None:
    resp = client.post(
        "/tasks/2/status",
        data={
            "status": "done",
            "require": ["ask-user:Q"],
            "exclude": ["STATUS:done"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc.startswith("/tasks?")
    assert "require=ask-user%3AQ" in loc
    assert "exclude=STATUS%3Adone" in loc


def test_delete_post_preserves_filter_in_redirect(client) -> None:
    resp = client.post(
        "/tasks/2/delete",
        data={"require": ["level:strategic"]},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks?require=level%3Astrategic"


# ── tasks: row classification, attention icons, rollup ─────────────


def test_classify_row_buckets() -> None:
    from precis_web.routes.tasks import _classify_row

    assert _classify_row("done", []) == "done"
    assert _classify_row("won't-do", []) == "done"
    assert _classify_row("doing", []) == "active"
    assert _classify_row("open", []) == "active"
    assert _classify_row("blocked", []) == "waiting"
    assert _classify_row("paused", []) == "waiting"
    # Tag-driven waiting overrides plain "open".
    assert _classify_row("open", ["ask-user:Q"]) == "waiting"
    assert _classify_row("open", ["waiting-for:paper:10.x/y"]) == "waiting"
    assert _classify_row("open", ["halt"]) == "waiting"
    assert _classify_row("open", ["child-failed:42"]) == "waiting"


def test_attention_icons_for_ask_and_paper() -> None:
    from precis_web.routes.tasks import _attention_icons

    icons = _attention_icons(["ask-user:what scope?"])
    assert len(icons) == 1 and icons[0]["icon"] == "🔔"
    assert icons[0]["href"] == "/asks"

    icons = _attention_icons(["waiting-for:paper:10.1234/foo"])
    assert len(icons) == 1 and icons[0]["icon"] == "📝"
    assert icons[0]["href"].startswith("/papers?q=")

    # Both signals present → both icons.
    icons = _attention_icons(["ask-user:Q", "waiting-for:paper:abc"])
    assert {i["icon"] for i in icons} == {"🔔", "📝"}

    assert _attention_icons(["project:precis"]) == []


def test_attention_icons_deduplicate_same_class() -> None:
    """Multiple ask-user tags collapse to one 🔔 (not five)."""
    from precis_web.routes.tasks import _attention_icons

    icons = _attention_icons(["ask-user:Q1", "ask-user:Q2", "ask-user:Q3"])
    assert [i["icon"] for i in icons] == ["🔔"]


# ── tasks: focus / drill-down ──────────────────────────────────────


def test_focus_rows_returns_subtree_and_breadcrumb() -> None:
    from precis_web.routes.tasks import _focus_rows

    rows = [
        _row(1, parent_id=None, depth=0),
        _row(2, parent_id=1, depth=1),
        _row(3, parent_id=2, depth=2),
        _row(4, parent_id=1, depth=1),
    ]
    focused, breadcrumb = _focus_rows(rows, focus_id=2)
    assert [r["id"] for r in focused] == [2, 3]
    # Depth rebased so the focused node sits at 0.
    assert focused[0]["depth"] == 0 and focused[1]["depth"] == 1
    assert [b["id"] for b in breadcrumb] == [1]


def test_focus_rows_missing_id_is_noop() -> None:
    """A stale ``focus`` (e.g. after a delete) doesn't crash."""
    from precis_web.routes.tasks import _focus_rows

    rows = [_row(1)]
    focused, breadcrumb = _focus_rows(rows, focus_id=999)
    assert focused == rows
    assert breadcrumb == []


def test_focus_rows_none_is_passthrough() -> None:
    from precis_web.routes.tasks import _focus_rows

    rows = [_row(1)]
    focused, breadcrumb = _focus_rows(rows, focus_id=None)
    assert focused == rows and breadcrumb == []


def test_focus_url_round_trips_in_helper() -> None:
    from precis_web.routes.tasks import _tasks_url

    assert _tasks_url([], [], focus=42) == "/tasks?focus=42"
    assert (
        _tasks_url(["ask-user"], ["STATUS:done"], focus=7)
        == "/tasks?require=ask-user&exclude=STATUS%3Adone&focus=7"
    )


def test_post_preserves_focus_in_redirect(client) -> None:
    resp = client.post(
        "/tasks/2/status",
        data={"status": "doing", "focus": "1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/tasks?focus=1"


# ── tasks: clickable badges + kind:* pseudo-tag + closed-job hide ──


def test_filter_rows_matches_kind_pseudo_tag() -> None:
    """``kind:job`` is a filterable pseudo-tag like ``STATUS:*``."""
    from precis_web.routes.tasks import _filter_rows

    rows = [
        _row(1, tags=[]),
        _row(99, kind="job", parent_id=1, tags=[]),
    ]
    # Requiring kind:todo keeps the todo (job rides along under parent).
    kept = _filter_rows(rows, require=["kind:todo"], exclude=[])
    assert [r["id"] for r in kept] == [1, 99]
    # Excluding kind:job drops jobs even when their parent matched.
    rows2 = [
        _row(1, tags=["project:p"]),
        _row(99, kind="job", parent_id=1, tags=[]),
    ]
    # kind:job on a todo never matches → require=kind:job over a todo
    # set yields no matches, so the kept set is empty.
    kept = _filter_rows(rows2, require=["kind:job"], exclude=[])
    assert kept == []


def test_filter_rows_matches_lowercase_status_too() -> None:
    """``status:doing`` matches the same row as ``STATUS:doing``."""
    from precis_web.routes.tasks import _filter_rows

    rows = [_row(1, status="doing"), _row(2, status="done")]
    assert [
        r["id"] for r in _filter_rows(rows, require=["status:doing"], exclude=[])
    ] == [1]
    assert [
        r["id"] for r in _filter_rows(rows, require=["STATUS:doing"], exclude=[])
    ] == [1]


def test_hide_inactive_jobs_drops_closed_attempts() -> None:
    """Failed / succeeded / done / won't-do jobs are dropped by default."""
    from precis_web.routes.tasks import _hide_inactive_jobs

    rows = [
        _row(1),  # todo, kept
        _row(10, kind="job", parent_id=1, status="failed"),
        _row(11, kind="job", parent_id=1, status="succeeded"),
        _row(12, kind="job", parent_id=1, status="done"),
        _row(13, kind="job", parent_id=1, status="won't-do"),
        _row(14, kind="job", parent_id=1, status="running"),
    ]
    kept = _hide_inactive_jobs(rows, show_all=False)
    assert [r["id"] for r in kept] == [1, 14]
    # show_all opts the hide off.
    assert _hide_inactive_jobs(rows, show_all=True) == rows


def test_dashboard_hides_closed_jobs_by_default(client, runtime) -> None:
    """``show_jobs=active`` (the default) drops failed job rows."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="parent todo"),
    ]
    import precis_web.routes.tasks as tasks_mod

    monkeypatch_jobs = [
        {
            "id": 99,
            "parent_id": 1,
            "title": "plan_tick attempt",
            "lease_until": None,
        }
    ]
    original_child = tasks_mod._child_jobs
    original_tags = tasks_mod._load_tags

    def child_jobs(store, todo_ids):
        return monkeypatch_jobs if 1 in todo_ids else []

    def load_tags(store, ref_ids):
        out = {rid: {"status": "open", "level": ""} for rid in ref_ids}
        if 99 in out:
            out[99] = {"status": "failed", "level": ""}
        return out

    tasks_mod._child_jobs = child_jobs  # type: ignore[assignment]
    tasks_mod._load_tags = load_tags  # type: ignore[assignment]
    try:
        # Default: closed job hidden.
        resp = client.get("/tasks")
        assert resp.status_code == 200
        assert "plan_tick attempt" not in resp.text
        # show_jobs=all: closed job visible.
        resp = client.get("/tasks?show_jobs=all")
        assert resp.status_code == 200
        assert "plan_tick attempt" in resp.text
    finally:
        tasks_mod._child_jobs = original_child  # type: ignore[assignment]
        tasks_mod._load_tags = original_tags  # type: ignore[assignment]


def test_dashboard_status_badge_is_clickable_filter(client) -> None:
    """STATUS / level badges render as ``<a>`` links that filter on click."""
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # Fake store seeds two open todos; the open badge should be a link
    # to ?require=STATUS:open. The kind:todo badge isn't rendered (only
    # job rows get a kind badge).
    assert "require=STATUS%3Aopen" in resp.text


def test_tasks_url_round_trips_show_jobs() -> None:
    from precis_web.routes.tasks import _tasks_url

    assert _tasks_url([], [], None, "all") == "/tasks?show_jobs=all"
    assert _tasks_url([], [], None, None) == "/tasks"
    assert (
        _tasks_url(["kind:job"], [], None, "all")
        == "/tasks?require=kind%3Ajob&show_jobs=all"
    )


# ── tasks: mermaid tree view ───────────────────────────────────────


def test_build_mermaid_tree_root_and_children() -> None:
    """Two-level subtree renders the root and its kids as labelled nodes."""
    from precis_web.routes.tasks import _build_mermaid_tree

    rows = [
        _row(1, status="open", depth=0),
        _row(2, parent_id=1, status="doing", depth=1),
        _row(3, parent_id=1, status="done", depth=1),
    ]
    out = _build_mermaid_tree(rows, root_id=1, max_depth=3)
    assert "graph TD" in out
    assert 'N1["#1 row 1"]:::active' in out
    assert 'N2["#2 row 2"]:::active' in out
    assert 'N3["#3 row 3"]:::done' in out
    assert "N1 --> N2" in out and "N1 --> N3" in out
    # Root highlight class.
    assert "class N1 root" in out
    # Class definitions present so the diagram is self-contained.
    assert "classDef active" in out
    assert "classDef done" in out


def test_build_mermaid_tree_truncates_at_max_depth() -> None:
    """Nodes beyond max_depth are excluded; the parent gets a … suffix."""
    from precis_web.routes.tasks import _build_mermaid_tree

    rows = [
        _row(1, depth=0),
        _row(2, parent_id=1, depth=1),
        _row(3, parent_id=2, depth=2),
        _row(4, parent_id=3, depth=3),
    ]
    out = _build_mermaid_tree(rows, root_id=1, max_depth=2)
    assert "N1" in out and "N2" in out and "N3" in out
    # N4 is past the depth cap.
    assert "N4" not in out
    # N3 gets the truncation marker in its label because it has kids
    # we didn't expand.
    assert 'N3["#3 row 3 …"]' in out


def test_build_mermaid_tree_missing_root_is_empty() -> None:
    from precis_web.routes.tasks import _build_mermaid_tree

    assert _build_mermaid_tree([], root_id=42, max_depth=3) == ""
    assert _build_mermaid_tree([_row(1)], root_id=99, max_depth=3) == ""


def test_build_mermaid_tree_excludes_jobs() -> None:
    """Job rows aren't structure — they don't appear in the diagram."""
    from precis_web.routes.tasks import _build_mermaid_tree

    rows = [
        _row(1, depth=0),
        _row(2, parent_id=1, depth=1),
        _row(99, kind="job", parent_id=1, depth=1),
    ]
    out = _build_mermaid_tree(rows, root_id=1, max_depth=3)
    assert "N99" not in out and "N1 --> N99" not in out


def test_dashboard_emits_mermaid_when_tree_and_focus(client, runtime) -> None:
    """``?focus=N&tree=3`` renders a Mermaid panel."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="parent"),
        make_ref(id=2, kind="todo", title="child", parent_id=1),
    ]
    resp = client.get("/tasks?focus=1&tree=3")
    assert resp.status_code == 200
    # Mermaid source block present.
    assert 'class="mermaid"' in resp.text
    assert "graph TD" in resp.text
    # CDN loader present (lazy-loaded only when the diagram is shown).
    assert "mermaid.esm.min.mjs" in resp.text
    # Depth selector renders for each allowed value.
    for d in (1, 2, 3, 5, 10):
        assert f"{d} deep" in resp.text


def test_dashboard_no_mermaid_without_focus(client) -> None:
    """``?tree=3`` without focus is a no-op (no diagram block)."""
    resp = client.get("/tasks?tree=3")
    assert resp.status_code == 200
    assert "graph TD" not in resp.text
    assert "mermaid.esm.min.mjs" not in resp.text


def test_dashboard_truncates_title_first_line_with_hover_tooltip(
    client, runtime
) -> None:
    """Body of a todo is shown as just the first line in the row.

    Long multi-line bodies (planner-coroutine prompts) used to render
    in full in the row, blowing up vertical density. The truncation
    is at 80 chars + a hover popover with the full body.
    """
    from tests.precis_web.conftest import make_ref

    long_body = (
        "First line summary that should stand alone in the row.\n\n"
        "Second paragraph with the full prompt body that should not appear "
        "in the truncated row but does appear in the hover tooltip below."
    )
    runtime.store.todos = [
        make_ref(id=1, kind="todo", title=long_body),
    ]
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # First line shows.
    assert "First line summary that should stand alone in the row." in resp.text
    # Tooltip popover with the full body is also in the DOM (Alpine x-show
    # hides it until hover) — look for the unique second-paragraph text.
    assert "Second paragraph with the full prompt body" in resp.text


def test_children_popup_returns_immediate_children(client, runtime) -> None:
    """``/tasks/{id}/children-popup`` returns just one level of children."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="root"),
        make_ref(id=2, kind="todo", title="child A", parent_id=1),
        make_ref(id=3, kind="todo", title="child B", parent_id=1),
        make_ref(id=4, kind="todo", title="grandchild", parent_id=2),
    ]
    resp = client.get("/tasks/1/children-popup")
    assert resp.status_code == 200
    assert "child A" in resp.text
    assert "child B" in resp.text
    # Grandchildren are NOT in the depth-0 fragment — they're lazy
    # via htmx once the operator clicks child A's chip.
    assert "grandchild" not in resp.text


def test_children_popup_max_depth_links_to_mermaid(client, runtime) -> None:
    """At depth==max, the fragment offers a Mermaid view link instead."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="root"),
        make_ref(id=2, kind="todo", title="child", parent_id=1),
    ]
    resp = client.get("/tasks/1/children-popup?depth=4")
    assert resp.status_code == 200
    assert "Mermaid tree view" in resp.text
    # child name should NOT render — we're past the depth cap.
    assert "child" not in resp.text or "children" in resp.text  # fuzzy


def test_children_popup_handles_no_children(client, runtime) -> None:
    """A leaf node's popup shows a friendly empty state."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="lone leaf"),
    ]
    resp = client.get("/tasks/1/children-popup")
    assert resp.status_code == 200
    assert "No children" in resp.text


def test_dashboard_focus_shows_inline_add_child_form(client, runtime) -> None:
    """Focused node gets a prominent + child form (not buried in ⋯)."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos = [
        make_ref(id=1, kind="todo", title="parent"),
    ]
    resp = client.get("/tasks?focus=1")
    assert resp.status_code == 200
    # Form action targets the focused id directly.
    assert 'action="/tasks/1/children"' in resp.text
    # Placeholder mentions the focused id so the operator knows where
    # the child will land.
    assert "Add a child under #1" in resp.text


def test_tasks_url_round_trips_tree() -> None:
    from precis_web.routes.tasks import _tasks_url

    assert _tasks_url([], [], 1, None, 3) == "/tasks?focus=1&tree=3"
    assert _tasks_url([], [], 1, None, None) == "/tasks?focus=1"


# ── patent detail chunks ───────────────────────────────────────────


def test_patent_detail_renders_chunks(client, runtime) -> None:
    """The patent detail view shows every body chunk in full.

    The handler's overview is just bibliographic header + abstract;
    body text lives in chunks. The detail view fetches them via
    ``Store.list_blocks_for_ref`` and renders one card per chunk
    showing ``~pos``, the chunk_kind badge, and the full text.
    """
    from types import SimpleNamespace

    from tests.precis_web.conftest import make_ref

    runtime.store.patents = [
        make_ref(id=6172, kind="patent", slug="cn112562800a", title="A patent")
    ]
    # Hook the fake's fetch + blocks for patent 6172.
    original_fetch = runtime.store.fetch_refs_by_ids
    original_blocks = runtime.store.list_blocks_for_ref

    def fetch(ids, *, include_deleted=False):
        out = original_fetch(ids, include_deleted=include_deleted)
        for p in runtime.store.patents:
            if p.id in ids:
                out[p.id] = p
        return out

    fake_blocks = [
        SimpleNamespace(
            pos=0,
            slug="cn112562800a~p0",
            text="First chunk text.",
            meta={},
            chunk_kind="paragraph",
        ),
        SimpleNamespace(
            pos=1,
            slug=None,
            text="Second chunk has more text\nover multiple lines.",
            meta={},
            chunk_kind="claim",
        ),
    ]

    def blocks(ref_id, **kw):
        if ref_id == 6172:
            return list(fake_blocks)
        return original_blocks(ref_id, **kw)

    runtime.store.fetch_refs_by_ids = fetch  # type: ignore[assignment]
    runtime.store.list_blocks_for_ref = blocks  # type: ignore[assignment]
    try:
        resp = client.get("/refs/patent/6172")
    finally:
        runtime.store.fetch_refs_by_ids = original_fetch  # type: ignore[assignment]
        runtime.store.list_blocks_for_ref = original_blocks  # type: ignore[assignment]

    assert resp.status_code == 200
    assert "Chunks" in resp.text
    assert "(2)" in resp.text
    assert "~0" in resp.text and "~1" in resp.text
    assert "paragraph" in resp.text and "claim" in resp.text
    assert "First chunk text." in resp.text
    assert "Second chunk has more text" in resp.text


def test_memory_detail_omits_chunks_section(client) -> None:
    """Non-patent kinds don't render the chunks section — for now."""
    resp = client.get("/refs/memory/20")
    assert resp.status_code == 200
    # Section heading is patent-only at the route level.
    assert "Chunks" not in resp.text


# ── jinja resilience ───────────────────────────────────────────────


def test_template_missing_key_does_not_500(client, monkeypatch) -> None:
    """A context dict missing a key the template references must render.

    The melchior incident: a stale process omitted ``usage`` and the
    page 500'd on ``usage.get(...)``. ``ChainableUndefined`` keeps
    chained access silent.
    """
    from precis_web.routes import status as status_mod

    # Drop ``usage`` (and any other section) from the context by making
    # every section query raise — _safe returns None, the page should
    # render anyway with empty panels.
    def _boom(store):  # type: ignore[no-untyped-def]
        raise RuntimeError("simulated stale schema")

    for name in (
        "_kind_counts",
        "_paper_summary",
        "_todo_status",
        "_recent_events",
        "_claude_usage",
        "_hosts",
        "_heartbeats",
    ):
        monkeypatch.setattr(status_mod, name, _boom)

    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Status" in resp.text


# ── asks ───────────────────────────────────────────────────────────


def test_asks_page_renders_empty(client) -> None:
    """Empty state under the fake store (cursor returns no rows)."""
    resp = client.get("/asks")
    assert resp.status_code == 200
    assert "Asks" in resp.text
    assert (
        "Nothing&#39;s waiting on you" in resp.text
        or "Nothing's waiting on you" in resp.text
    )


def test_asks_page_renders_data(client, monkeypatch) -> None:
    """A populated ask renders the question + an unlock form per row."""
    from precis_web.routes import asks as asks_mod

    monkeypatch.setattr(
        asks_mod,
        "_load_asks",
        lambda store, **kw: [
            {
                "id": 14634,
                "title": "Write the technical section",
                "created_at": None,
                "questions": ["two blockers — fill in the body first"],
                "tags": ["ask-user:two blockers — fill in the body first"],
            }
        ],
    )
    resp = client.get("/asks")
    assert resp.status_code == 200
    assert "#14634" in resp.text
    assert "Write the technical section" in resp.text
    assert "two blockers" in resp.text
    assert 'action="/asks/14634/answer"' in resp.text
    # Hidden remove input carries the raw tag so the unlock dispatch
    # doesn't re-query.
    assert 'name="remove"' in resp.text


# ── needs-you (unified asks + needs-triage queue) ──────────────────


def test_needs_you_renders_triage_section(client) -> None:
    """The merged page lists papers needing triage with a 'view all' link.

    Under the fake store there are no asks (empty cursor); ``list_refs``
    returns the canned papers, so the Needs-triage section renders and
    each row deep-links to the detail page with the triage panel open.
    """
    resp = client.get("/needs-you")
    assert resp.status_code == 200
    assert "Needs you" in resp.text
    assert "Needs triage" in resp.text
    # A paper title from the fake's canned set.
    assert "Ballistic carbon nanotube" in resp.text
    # Rows open the detail page with the triage panel; "view all" goes to
    # the full triage queue. (``/papers-needed`` still appears once — in
    # the Browse ▾ dropdown — but the queue content here is triage.)
    assert "?triage=1" in resp.text
    assert "/papers/triage" in resp.text


def test_needs_you_renders_asks_inline(client, monkeypatch) -> None:
    """A populated ask renders its inline answer form (POSTing to /asks)."""
    from precis_web.routes import needs_you as needs_you_mod

    monkeypatch.setattr(
        needs_you_mod,
        "_load_asks",
        lambda store, **kw: [
            {
                "id": 14634,
                "title": "Write the technical section",
                "created_at": None,
                "questions": ["which venue?"],
                "tags": ["ask-user:which venue?"],
            }
        ],
    )
    resp = client.get("/needs-you")
    assert resp.status_code == 200
    assert "#14634" in resp.text
    assert "which venue?" in resp.text
    # The answer form still targets the canonical /asks write route.
    assert 'action="/asks/14634/answer"' in resp.text


def test_nav_badge_counts_on_every_page(client) -> None:
    """The global nav injects the Needs-you badge on an unrelated page.

    The badge processor sums asks (0 under the empty-cursor fake) and
    the needs-triage paper count, so the rose badge shows on /tasks.
    """
    resp = client.get("/tasks")
    assert resp.status_code == 200
    # New nav structure is present site-wide.
    assert "Needs you" in resp.text
    assert "Browse" in resp.text
    # Rose attention badge carrying the combined count.
    assert "bg-rose-600" in resp.text


def test_ask_value_strips_prefix() -> None:
    from precis_web.routes.asks import _ask_value

    assert _ask_value("ask-user:hello") == "hello"
    assert _ask_value("ask-user") == ""
    assert _ask_value("other") == ""
    # The removed asking-reto alias is no longer stripped.
    assert _ask_value("asking-reto:foo") == ""


def test_answer_dispatches_edit_then_tag_remove(client, runtime) -> None:
    """The unlock flow: edit appends the response, then tag removes the asks."""
    # Inject a todo on the fake store so fetch_refs_by_ids resolves.
    from tests.precis_web.conftest import make_ref

    runtime.store.todos.append(make_ref(id=99, kind="todo", title="Pending question"))
    resp = client.post(
        "/asks/99/answer",
        data={
            "response": "use scope X",
            "remove": ["ask-user:what scope?"],
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    verbs = [v for v, _ in runtime.calls]
    assert verbs[-2:] == ["edit", "tag"]
    edit_args = runtime.calls[-2][1]
    assert edit_args["kind"] == "todo"
    assert edit_args["id"] == 99
    assert edit_args["mode"] == "replace"
    assert "Response: use scope X" in edit_args["text"]
    assert "Pending question" in edit_args["text"]
    tag_args = runtime.calls[-1][1]
    assert tag_args == {
        "kind": "todo",
        "id": 99,
        "remove": ["ask-user:what scope?"],
    }


def test_answer_empty_response_redirects_without_dispatch(client, runtime) -> None:
    """Whitespace-only response is a no-op (no edit, no tag)."""
    before = len(runtime.calls)
    resp = client.post(
        "/asks/1/answer",
        data={"response": "   "},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert len(runtime.calls) == before  # nothing dispatched


def test_answer_edit_failure_skips_tag_remove(client, runtime) -> None:
    """If edit fails the unlock tag-remove must not fire."""
    from tests.precis_web.conftest import make_ref

    runtime.store.todos.append(make_ref(id=77, kind="todo", title="Need input"))
    runtime.error_verbs.add("edit")
    resp = client.post(
        "/asks/77/answer",
        data={"response": "answer text", "remove": ["ask-user:q"]},
        follow_redirects=False,
    )
    assert resp.status_code == 400
    verbs = [v for v, _ in runtime.calls]
    assert "edit" in verbs
    assert "tag" not in verbs


# ── refs title preview ─────────────────────────────────────────────


def test_title_preview_first_two_nonempty_lines() -> None:
    from precis_web.routes.refs import _title_preview

    md = (
        "# Structural review digest — 2026-06-15\n"
        "\n"
        "Strategic root #6649 (Nano-transistors) is mislabelled.\n"
        "\n"
        "## Branches missing an outcome line\n"
    )
    out = str(_title_preview(md))
    assert (
        out == "# Structural review digest — 2026-06-15"
        "<br>"
        "Strategic root #6649 (Nano-transistors) is mislabelled."
    )


def test_title_preview_escapes_per_line_html() -> None:
    """Per-line content is HTML-escaped; only the <br> is raw."""
    from precis_web.routes.refs import _title_preview

    out = str(_title_preview("<script>x</script>\nplain"))
    assert "<script>" not in out
    assert "&lt;script&gt;" in out
    assert "<br>" in out


def test_title_preview_handles_empty() -> None:
    from precis_web.routes.refs import _title_preview

    assert str(_title_preview("")) == "(untitled)"
    assert str(_title_preview("\n\n")) == "(untitled)"


# ---- Papers presence filters (has_pdf / has_chunks) -----------------


def test_papers_filter_params_render_toggles(client) -> None:
    """The filter checkboxes reflect the query params (checked state)."""
    resp = client.get("/papers", params={"has_pdf": "1", "has_chunks": "1"})
    assert resp.status_code == 200
    # Both toggles present and checked.
    assert 'name="has_pdf"' in resp.text
    assert 'name="has_chunks"' in resp.text
    assert resp.text.count("checked") >= 2


def test_papers_filter_has_pdf_pushes_to_list_refs(client, runtime) -> None:
    """has_pdf=1 (no query) forwards has_pdf=True to store.list_refs."""
    seen: dict[str, object] = {}
    original = runtime.store.list_refs

    def _spy(**kw):
        seen.update(kw)
        return original(**kw)

    runtime.store.list_refs = _spy  # type: ignore[method-assign]
    resp = client.get("/papers", params={"has_pdf": "1"})
    assert resp.status_code == 200
    assert seen.get("has_pdf") is True
    # has_chunks toggle off → None, not False (don't-filter).
    assert seen.get("has_chunks") is None


# ---- Ask a follow-up about a thought --------------------------------


def _stub_answer(monkeypatch, text: str = "Here is the answer."):
    """Replace the agentic dispatch with a canned AgentResult."""
    from precis.utils.claude_agent import AgentResult
    from precis_web import ask

    def _fake(prompt, *, store, conv_ref_id):
        _fake.prompt = prompt  # type: ignore[attr-defined]
        return AgentResult(final_text=text, cost_usd=0.01, duration_s=1.0, turns_used=1)

    monkeypatch.setattr(ask, "generate_answer", _fake)
    return _fake


def test_ask_followup_records_question_links_and_answer(
    client, runtime, monkeypatch
) -> None:
    """POST /refs/{kind}/{id}/ask: put(question) → link → answer, redirect."""
    fake = _stub_answer(monkeypatch, "The dream suggests X.")
    resp = client.post(
        "/refs/memory/20/ask",
        data={"question": "What does this imply?"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/refs/conv/")

    verbs = [v for v, _ in runtime.calls]
    assert verbs.count("put") == 2  # question turn + answer turn
    assert "link" in verbs

    # First put captures the human question on a followup/ slug.
    first_put = next(a for v, a in runtime.calls if v == "put")
    assert first_put["kind"] == "conv"
    assert first_put["id"] == "followup/memory/20"
    assert first_put["text"] == "What does this imply?"
    # De-hardcoded: the asker is WebConfig.owner (PRECIS_OWNER), which
    # defaults to "owner" — no longer the literal "reto".
    assert first_put["author"] == "owner"
    assert first_put["ref_meta"]["followup_source"] == "memory:20"

    # The link points the conv back at the source as derived-from.
    link_args = next(a for v, a in runtime.calls if v == "link")
    assert link_args["target"] == "memory:20"
    assert link_args["rel"] == "derived-from"

    # The answer turn carries the model's text.
    answer_put = [a for v, a in runtime.calls if v == "put"][-1]
    assert answer_put["text"] == "The dream suggests X."
    assert answer_put["author"] == "asa"
    # The source thought made it into the prompt.
    assert "A decision" in fake.prompt  # type: ignore[attr-defined]


def test_ask_followup_chunk_scoped_target(client, runtime, monkeypatch) -> None:
    """A chunk=N field scopes the slug + link handle to that chunk."""
    _stub_answer(monkeypatch)
    resp = client.post(
        "/refs/memory/20/ask",
        data={"question": "Explain ~3", "chunk": "3"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    first_put = next(a for v, a in runtime.calls if v == "put")
    assert first_put["id"] == "followup/memory/20/c3"
    link_args = next(a for v, a in runtime.calls if v == "link")
    assert link_args["target"] == "memory:20~3"


def test_ask_followup_blank_question_is_noop(client, runtime, monkeypatch) -> None:
    """An empty question redirects back without dispatching anything."""
    _stub_answer(monkeypatch)
    resp = client.post(
        "/refs/memory/20/ask", data={"question": "   "}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert not runtime.calls


def test_ask_followup_thinking_failure_records_error_turn(
    client, runtime, monkeypatch
) -> None:
    """When the agent raises, the failure is appended as a turn, not lost."""
    from precis.utils.claude_agent import ClaudeAgentError
    from precis_web import ask

    def _boom(prompt, *, store, conv_ref_id):
        raise ClaudeAgentError("nope", stdout="", stderr="", returncode=1)

    monkeypatch.setattr(ask, "generate_answer", _boom)
    resp = client.post(
        "/refs/memory/20/ask",
        data={"question": "anything"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    answer_put = [a for v, a in runtime.calls if v == "put"][-1]
    assert "thinking failed" in answer_put["text"]
    assert answer_put["author"] == "system"


# ── tape-player stop / start (halt a subtree) ──────────────────────


def _patch_subtree(monkeypatch, rows, *, parent=None, halts=None):
    """Stub the subtree/halt DB helpers so the stop/start route logic is
    exercised without Postgres (the fake pool returns empty cursors)."""
    from precis_web.routes import tasks

    monkeypatch.setattr(tasks, "_subtree_rows", lambda store, root_id: rows)
    monkeypatch.setattr(
        tasks,
        "_halt_tags_for",
        lambda store, ids: {i: (halts or {}).get(i, []) for i in ids},
    )
    monkeypatch.setattr(tasks, "_parent_todo_id", lambda store, ref_id: parent)


def test_stop_todo_halts_subtree_and_cancels_live_jobs(
    runtime, client, monkeypatch
) -> None:
    """⏹ on a todo halts every descendant todo and cancels only the
    live (queued/running) jobs — a succeeded job is left untouched."""
    _patch_subtree(
        monkeypatch,
        rows=[
            (100, "todo", "open"),
            (101, "todo", "open"),
            (102, "job", "running"),
            (103, "job", "queued"),
            (104, "job", "succeeded"),
        ],
    )
    resp = client.post("/tasks/100/stop", data={}, follow_redirects=False)
    assert resp.status_code == 303
    halts = [a for v, a in runtime.calls if v == "tag" and a.get("add") == ["halt"]]
    assert {a["id"] for a in halts} == {100, 101}
    cancels = [
        a
        for v, a in runtime.calls
        if v == "tag" and a.get("add") == ["STATUS:cancelled"]
    ]
    assert {a["id"] for a in cancels} == {102, 103}  # not the succeeded 104


def test_stop_skips_already_halted_todo(runtime, client, monkeypatch) -> None:
    """A todo already carrying a halt marker is not re-tagged."""
    _patch_subtree(
        monkeypatch,
        rows=[(100, "todo", "open"), (101, "todo", "open")],
        halts={101: ["halt:cost-cap"]},
    )
    resp = client.post("/tasks/100/stop", data={}, follow_redirects=False)
    assert resp.status_code == 303
    halted_ids = {
        a["id"] for v, a in runtime.calls if v == "tag" and a.get("add") == ["halt"]
    }
    assert halted_ids == {100}


def test_stop_job_cancels_and_halts_parent(runtime, client, monkeypatch) -> None:
    """⏹ on a job cancels it and halts its owner todo."""
    _patch_subtree(monkeypatch, rows=[(200, "job", "running")], parent=42)
    resp = client.post("/tasks/200/stop", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert (
        "tag",
        {"kind": "job", "id": 200, "add": ["STATUS:cancelled"]},
    ) in runtime.calls
    assert ("tag", {"kind": "todo", "id": 42, "add": ["halt"]}) in runtime.calls


def test_stop_missing_ref_is_noop(runtime, client, monkeypatch) -> None:
    """Posting stop for a ref not in the subtree walk redirects, no writes."""
    _patch_subtree(monkeypatch, rows=[])
    resp = client.post("/tasks/999/stop", data={}, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime.calls == []


def test_start_removes_halt_from_subtree(runtime, client, monkeypatch) -> None:
    """▶ clears every halt marker (bare + reason-tagged) across the subtree."""
    _patch_subtree(
        monkeypatch,
        rows=[(100, "todo", "open"), (101, "todo", "open"), (102, "job", "queued")],
        halts={100: ["halt"], 101: ["halt", "halt:tick-cap"]},
    )
    resp = client.post("/tasks/100/start", data={}, follow_redirects=False)
    assert resp.status_code == 303
    removes = {
        tuple(sorted(a["remove"])): a["id"] for v, a in runtime.calls if v == "tag"
    }
    assert removes == {("halt",): 100, ("halt", "halt:tick-cap"): 101}


def test_stop_surfaces_handler_error(runtime, client, monkeypatch) -> None:
    """A rejected tag mutation renders the error page (not a silent redirect)."""
    _patch_subtree(monkeypatch, rows=[(100, "todo", "open")])
    runtime.error_verbs.add("tag")
    resp = client.post("/tasks/100/stop", data={}, follow_redirects=False)
    assert resp.status_code == 400


def test_stop_start_routes_registered(client) -> None:
    paths = {getattr(r, "path", None) for r in client.app.routes}
    assert "/tasks/{ref_id}/stop" in paths
    assert "/tasks/{ref_id}/start" in paths
