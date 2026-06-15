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

    from precis_web.config import WebConfig

    monkeypatch.setenv("PRECIS_CORPUS_DIR", f"/opt/shared/corpus{os.pathsep}/opt/nas/c")
    cfg = WebConfig.from_env()
    assert str(cfg.corpus_dir) == "/opt/shared/corpus"
    assert [str(p) for p in cfg.extra_corpus_dirs] == ["/opt/nas/c"]
    assert [str(p) for p in cfg.corpus_dirs] == ["/opt/shared/corpus", "/opt/nas/c"]


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
    # New telemetry panels render (empty states under the fake store).
    assert "Machines" in resp.text
    assert "Claude usage" in resp.text
    assert "precis heartbeat" in resp.text  # empty-state hint


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


def test_status_ago_formatter() -> None:
    from datetime import UTC, datetime, timedelta

    from precis_web.routes.status import _ago

    now = datetime.now(UTC)
    assert _ago(now - timedelta(seconds=30)).endswith("s ago")
    assert _ago(now - timedelta(minutes=10)).endswith("m ago")
    assert _ago(now - timedelta(hours=5)).endswith("h ago")
    assert _ago(now - timedelta(days=3)).endswith("d ago")
    assert _ago(None) == ""
