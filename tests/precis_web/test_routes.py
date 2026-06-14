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
