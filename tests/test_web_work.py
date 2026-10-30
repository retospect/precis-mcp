"""Dark-factory step 1, slice 3 (td540843): the ``/work`` web page.

The page and ``search(kind='todo', view='work')`` read the same
``work_model``; these tests drive the page through the real app against a
fixture quest → root → slices tree.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.todo import work_view
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_todo_work_view import _agent, _push_fleet, _tree


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _row_tags(html: str) -> dict[str, str]:
    """``data-handle`` -> the ``<li>`` / ``<section>`` opening tag."""
    return {
        m.group(1): m.group(0)
        for m in re.finditer(r'<(?:li|section)[^>]*data-handle="([^"]+)"[^>]*>', html)
    }


def test_work_page_nests_slices_under_root_under_quest(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _tree(handler, store)
    resp = client.get("/work")

    assert resp.status_code == 200
    html = resp.text
    tags = _row_tags(html)
    quest, root, a, b, lone = (
        f"qu{t['quest']}",
        f"td{t['root']}",
        f"td{t['a']}",
        f"td{t['b']}",
        f"td{t['lone']}",
    )
    for handle in (quest, root, a, b, lone):
        assert handle in tags, f"no data-handle row for {handle}"
        assert f'href="/eye/{handle}"' in html
    # document order: quest, then its root, then the slices
    assert html.index(quest) < html.index(root) < html.index(a) < html.index(b)
    # depth: the root is 1, the slices one deeper
    assert 'data-depth="1"' in tags[root]
    assert 'data-depth="2"' in tags[a]
    assert 'data-depth="2"' in tags[b]
    # the lone strategic is listed under unassigned, not under the quest
    assert "Unassigned roots (1)" in html
    assert html.index(lone) > html.index(b)
    assert "Precis runs its own work" in html


def test_work_page_columns_status_agents_stage_age(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    t = _tree(handler, store)
    _push_fleet(
        store,
        [
            _agent("slice-a", f"build td{t['a']} work view"),
            _agent("WORK_MANAGEMENT", "spec dark factory"),
        ],
    )
    html = client.get("/work").text
    tags = _row_tags(html)
    start = html.index(tags[f"td{t['a']}"])
    a_row = html[start : html.index("</li>", start)]
    b_start = html.index(tags[f"td{t['b']}"])
    b_row = html[b_start : html.index("</li>", b_start)]

    # status column carries the lease icon; stage column the STATUS value
    assert "◀ claimed-by:work_management" in a_row
    assert ">doing</span>" in a_row
    # agents: handle match + lease-holder match = 2; none on the sibling
    assert '<span class="text-success-strong font-semibold">2</span>' in a_row
    assert "text-success-strong font-semibold" not in b_row
    # age: fresh rows say "<n>s ago" (ref updated_at, created_at fallback)
    assert re.search(r"\d+[smhd] ago", a_row)
    assert "melchior" in html


def test_work_page_is_read_only(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    _tree(handler, store)
    html = client.get("/work").text
    # the base chrome carries a search form; the page content has no controls
    content = html[html.index("<h1") :]
    assert "<form" not in content
    assert "<button" not in content
    assert "<input" not in content
    assert client.post("/work").status_code == 405


def test_work_page_matches_the_mcp_view(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    """Same model, two formats: every handle the MCP text lists is a row."""
    _tree(handler, store)
    text = handler.search(view="work").body
    html = client.get("/work").text
    tags = _row_tags(html)
    text_handles = set(re.findall(r"\b(?:td|qu)\d+\b", text.split("\nNext:")[0]))
    assert text_handles
    assert text_handles <= set(tags)
    model = work_view.work_model(store)
    model_handles = {q.handle for q in model.quests} | {
        r.handle for q in model.quests for root in q.roots for r in root.rows
    }
    assert model_handles <= set(tags)


def test_work_page_shows_threaded_and_unplaced_gripes(
    client: TestClient, handler: TodoHandler, hub: Hub, store: Store
) -> None:
    from precis.handlers.gripe import GripeHandler
    from tests._refs import insert_gripe

    t = _tree(handler, store)
    handler.tag(id=t["root"], add=["thread:factory"])
    gripes = GripeHandler(hub=hub)
    placed = int(insert_gripe(store, "Factory: the loop stalls").id)
    gripes.tag(id=placed, add=["thread:factory"])
    orphan = int(insert_gripe(store, "Ghost: nobody owns it").id)
    gripes.tag(id=orphan, add=["thread:local-compute"])
    html = client.get("/work").text
    tags = _row_tags(html)

    # threaded gripe: its own row under the root, addressable and linked
    p_handle, o_handle = f"gr{placed}", f"gr{orphan}"
    assert p_handle in tags
    assert 'data-kind="gripe"' in tags[p_handle]
    assert 'data-status="unbriefed"' in tags[p_handle]
    assert f'data-root="{t["root"]}"' in tags[p_handle]
    assert f'href="/eye/{p_handle}"' in html
    start = html.index(tags[p_handle])
    row = html[start : html.index("</li>", start)]
    assert "Factory: the loop stalls" in row
    assert ">unbriefed</span>" in row
    assert re.search(r"\d+[smhd] ago", row)
    assert html.index(f"td{t['root']}") < html.index(tags[p_handle])
    # the unplaced bucket is its own section with the same row shape
    assert "Unplaced gripes (1)" in html
    assert o_handle in tags
    assert 'data-kind="gripe"' in tags[o_handle]
    assert "data-root=" not in tags[o_handle]
    assert f'href="/eye/{o_handle}"' in html
    assert html.index("Unplaced gripes") < html.index(tags[o_handle])
    assert html.index(tags[p_handle]) < html.index("Unplaced gripes")


def test_work_page_gripe_rows_are_the_models_rows(
    client: TestClient, handler: TodoHandler, hub: Hub, store: Store
) -> None:
    """Every gripe the MCP text lists is a row on the page (one model)."""
    from precis.handlers.gripe import GripeHandler
    from tests._refs import insert_gripe

    t = _tree(handler, store)
    handler.tag(id=t["root"], add=["thread:factory"])
    gripes = GripeHandler(hub=hub)
    for i in range(3):
        gid = int(insert_gripe(store, f"Gripe {i}").id)
        gripes.tag(id=gid, add=["thread:factory" if i == 0 else "thread:local-compute"])
    text = handler.search(view="work").body.split("\nNext:")[0]
    html = client.get("/work").text
    tags = _row_tags(html)
    text_handles = set(re.findall(r"\bgr\d+\b", text))
    assert len(text_handles) == 3
    assert text_handles <= set(tags)
    model = work_view.work_model(store)
    model_handles = {
        g.handle for root in model.quests[0].roots for g in root.gripes.rows
    }
    model_handles |= {g.handle for g in model.unplaced_gripes.rows}
    assert model_handles == text_handles


def test_work_page_empty_store(client: TestClient) -> None:
    resp = client.get("/work")
    assert resp.status_code == 200
    assert "No quest has work in service." in resp.text
    assert "None reported" in resp.text


def test_status_page_links_to_work(client: TestClient) -> None:
    html = client.get("/status?tab=now").text
    assert 'href="/work"' in html
