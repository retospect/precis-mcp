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


def test_focus_page_renders_ask_question_and_answer_box(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    """gr570259: a focused todo waiting on a human shows the question near
    the top with an answer box underneath — no detour through /asks."""
    q = "Nightly macOS test-other needs ~120 min against its 90 min timeout?"
    ask = id_of(
        handler.put(
            text="raise the cap, shard it, or hold that half?",
            tags=[f"ask-user:{q}"],
            meta={
                "ask": {
                    "kind": "ruling",
                    "options": ["raise the cap", "shard it", "hold that half"],
                    "blocks": "nothing",
                    "context": "ran long twice this week",
                }
            },
        ).body
    )

    resp = client.get(f"/todo?focus={ask}")
    assert resp.status_code == 200
    html = resp.text
    # The question text, above an answer form posting to the shared
    # /asks answer route and returning to this focus page.
    assert q in html
    assert f'action="/asks/{ask}/answer"' in html
    assert "Submit answer" in html
    assert f'name="next" value="/todo?focus={ask}"' in html
    # The ask's stored tags (long questions redirect to see-chunk-N)
    # ride as hidden remove inputs so submit unlocks the todo.
    ask_tags = [str(t) for t in store.tags_for(ask) if str(t).startswith("ask-user")]
    assert ask_tags
    for t in ask_tags:
        assert f'name="remove" value="{t}"' in html
    # The structured meta.ask (kind / options / context) renders too.
    assert "ruling" in html
    assert "raise the cap" in html
    assert "ran long twice this week" in html


def test_focus_page_without_ask_shows_no_answer_box(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    root = id_of(handler.put(text="A plain root.", meta={"rotation_root": True}).body)

    html = client.get(f"/todo?focus={root}").text
    assert 'action="/asks/' not in html
    assert "Submit answer" not in html
