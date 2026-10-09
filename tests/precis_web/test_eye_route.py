"""``/eye/<handle>`` — the focus page (``fisheye-everywhere.md`` in-scope 4,
AC 5): the same ladder as the MCP ``get(extent=)``, rendered in the
browser with every handle linked to its own focus page."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.eye import linkify_handles

pytestmark = pytest.mark.db


@pytest.fixture
def client(runtime_with_store: Any) -> TestClient:
    app = create_app(runtime=runtime_with_store, web_config=WebConfig(corpus_dir=None))
    return TestClient(app)


def _seed(store: Any) -> tuple[Any, Any]:
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    quest = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    store.add_link(src_ref_id=todo.id, dst_ref_id=quest.id, relation="serves")
    return todo, quest


def test_focus_page_renders_the_mcp_eye_with_linked_handles(
    client: TestClient, runtime_with_store: Any, store: Any
) -> None:
    todo, quest = _seed(store)
    page = client.get(f"/eye/td{todo.id}")  # default rung: fisheye+1hop
    assert page.status_code == 200
    mcp_body, err = runtime_with_store.dispatch_with_status(
        "get", {"kind": "todo", "id": todo.id, "extent": "fisheye+1hop"}
    )
    assert not err
    assert "Roadmap:" in page.text and "serves: <a" in page.text
    assert f'href="/eye/qu{quest.id}?extent=fisheye%2B1hop"' in page.text
    # the MCP head line, with its own handle turned into a self-link
    assert mcp_body.splitlines()[0] == f"td{todo.id} [todo] Ship the ring"
    assert f">td{todo.id}</a> [todo] Ship the ring" in page.text
    # the ladder row names every rung, the current one marked
    for rung in ("kwd", "summary", "verbatim", "fisheye", "fisheye+2hop", "+recall"):
        assert f">{rung}</a>" in page.text
    assert f"/todo?focus={todo.id}" in page.text  # the native reader link


def test_focus_page_takes_the_rung_and_the_group_filter(
    client: TestClient, store: Any
) -> None:
    todo, _quest = _seed(store)
    kwd = client.get(f"/eye/td{todo.id}", params={"extent": "kwd"})
    assert kwd.status_code == 200
    assert "— linked (1 hop) —" not in kwd.text and "Ship the ring" in kwd.text
    bad = client.get(f"/eye/td{todo.id}", params={"extent": "fisheye+9hop"})
    assert bad.status_code == 400 and "Unknown rung" in bad.text
    # q= at fisheye+2hop reaches the dispatcher as the group filter
    grp = client.get(
        f"/eye/td{todo.id}", params={"extent": "fisheye+2hop", "q": "paper:cites"}
    )
    assert grp.status_code == 200 and "no second-hop group" in grp.text


def test_focus_page_on_a_skill_and_on_a_dead_handle(
    client: TestClient, store: Any
) -> None:
    skill = client.get("/eye/sk:precis-fisheye-help", params={"extent": "kwd"})
    assert skill.status_code == 200
    assert ">sk:precis-fisheye-help</a> [skill]" in skill.text
    todo, _quest = _seed(store)
    store.retire_ref(todo.id)
    gone = client.get(f"/eye/td{todo.id}")
    assert gone.status_code == 404 and "[error:" in gone.text
    assert client.get("/eye/not-a-handle").status_code == 404


def test_index_redirects_a_handle_and_searches_a_query(
    client: TestClient, store: Any
) -> None:
    todo, _quest = _seed(store)
    r = client.get("/eye/", params={"q": f"td{todo.id}"}, follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/eye/td{todo.id}?extent=fisheye%2B1hop"
    hits = client.get("/eye/", params={"q": "ring"})
    assert hits.status_code == 200 and 'name="q"' in hits.text


def test_linkify_handles_links_only_real_handles() -> None:
    out = linkify_handles(
        "see me4641 and pc13 — not ab12, mao18 or 5mx20; sk:precis-get-help"
    )
    assert (
        '<a class="eye-handle" href="/eye/me4641?extent=fisheye%2B1hop">me4641</a>'
        in out
    )
    assert 'href="/eye/pc13?' in out
    assert "ab12" in out and 'href="/eye/ab12' not in out
    assert 'href="/eye/mao18' not in out and 'href="/eye/mx20' not in out
    assert 'href="/eye/sk:precis-get-help?' in out
    assert linkify_handles("<b>") == "&lt;b&gt;"
