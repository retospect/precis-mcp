"""gr548798 / gr548799: the focused todo page (the plan node's web page)
renders the subtree's ``blocked-by`` links as a depends-on /
depended-on-by table, and the node card carries the full title."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _child(handler: TodoHandler, text: str, parent: int) -> int:
    return id_of(handler.put(text=text, parent_id=parent, meta={"llm_tier": None}).body)


def _dep_row(html: str, todo_id: int) -> str:
    start = html.index(f'data-dep="{todo_id}"')
    return html[start : html.index("</tr>", start)]


def test_focus_page_renders_depends_on_table(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    root = id_of(handler.put(text="The plan root.", meta={"rotation_root": True}).body)
    a = _child(handler, "spec the fix", root)
    b = _child(handler, "build the fix", root)
    c = _child(handler, "validate the fix", root)
    store.add_link(src_ref_id=b, dst_ref_id=a, relation="blocked-by")
    store.add_link(src_ref_id=c, dst_ref_id=b, relation="blocked-by")

    resp = client.get(f"/todo?focus={root}")
    assert resp.status_code == 200
    html = resp.text
    # the node card carries the focused todo's title
    assert "The plan root." in html
    # the table renders both directions out of the links
    assert "Dependencies" in html
    assert "Depends on" in html and "Depended on by" in html
    row = _dep_row(html, b)
    assert f"/todo?focus={a}" in row and "spec the fix" in row
    assert f"/todo?focus={c}" in row and "validate the fix" in row
    row = _dep_row(html, a)
    assert f"/todo?focus={b}" in row and "build the fix" in row


def test_focus_page_without_links_shows_no_table(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    root = id_of(handler.put(text="A lone root.", meta={"rotation_root": True}).body)
    _child(handler, "independent child", root)

    html = client.get(f"/todo?focus={root}").text
    assert "Dependencies" not in html
    assert 'data-dep="' not in html
