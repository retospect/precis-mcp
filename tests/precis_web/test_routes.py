"""Route-level tests for precis_web (no Postgres; fakes in conftest)."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


# ── app skeleton ───────────────────────────────────────────────────


def test_root_redirects_to_tasks(client) -> None:
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code in (302, 307)
    assert resp.headers["location"] == "/tasks"


def test_healthz(client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


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


def test_paper_detail_renders(client) -> None:
    resp = client.get("/papers/10")
    assert resp.status_code == 200
    assert "smith2024" in resp.text


def test_papers_index_hover_card_has_authors_and_abstract(client) -> None:
    resp = client.get("/papers")
    assert resp.status_code == 200
    # Author name (given + family) rendered in the hover card.
    assert "Jane Smith" in resp.text
    # Abstract with JATS/HTML tags stripped to plain text.
    assert "We study X in depth." in resp.text
    assert "<jats:p>" not in resp.text


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


def test_refs_nav_tabs_present(client) -> None:
    resp = client.get("/refs/memory")
    for href in (
        "/refs/conv",
        "/refs/oracle",
        "/refs/gripe",
        "/refs/patent",
        "/refs/pres",
    ):
        assert href in resp.text


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


def test_console_run_bad_arg_no_dispatch(client, runtime) -> None:
    resp = client.post("/console/run", data={"verb": "search", "args_text": "novalue"})
    assert resp.status_code == 200
    assert "input error" in resp.text
    assert runtime.calls == []  # nothing dispatched


# ── status ─────────────────────────────────────────────────────────


def test_status_page_renders(client) -> None:
    resp = client.get("/status")
    assert resp.status_code == 200
    assert "Status" in resp.text
