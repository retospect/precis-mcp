"""gr664410 — the todo dashboard list as a Drive preset.

``queue=doable`` renders the doable leaf rotation (same
``precis.todo.views._doable_rows`` the workbench's doable panel reads);
``focus=``/``serves=`` scope either list to a subtree / a quest's
serving set; TodoPresenter puts the STATUS/prio badges and the
``/todo/{id}/*`` job controls on every todo row. Bare ``/todo``
redirects here; ``/todo/{id}`` stays the workbench.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store.types import Tag
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.ref_urls import ref_url


@pytest.fixture
def drive_client(runtime_with_store, tmp_path) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _todo(store, title: str, **kw):
    return store.insert_ref(kind="todo", slug=None, title=title, **kw)


def test_bare_todo_redirects_to_the_drive_doable_preset(drive_client) -> None:
    resp = drive_client.get("/todo", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/drive?k=todo&queue=doable"
    # Any narrowing param keeps the workbench live.
    assert drive_client.get("/todo?show_jobs=active").status_code == 200
    assert drive_client.get("/todo?focus=1").status_code == 200


def test_todo_id_url_lands_on_the_focused_workbench(drive_client) -> None:
    resp = drive_client.get("/todo/42", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/todo?focus=42"
    assert ref_url("todo", 42) == "/todo/42"


def test_doable_queue_lists_doable_leaves_in_rotation_order(
    drive_client, store
) -> None:
    hot = _todo(store, "hot leaf", prio=1)
    cold = _todo(store, "cold leaf", prio=9)
    store.add_tag(hot.id, Tag.closed("STATUS", "doing"))
    halted = _todo(store, "halted leaf")
    store.add_tag(halted.id, Tag.open("halt"))
    resp = drive_client.get("/drive?k=todo&queue=doable&state=all")
    assert resp.status_code == 200
    assert resp.text.index("hot leaf") < resp.text.index("cold leaf")
    assert "halted leaf" not in resp.text
    # The row carries the workbench's own controls (gr664410): the
    # STATUS select and the ⏹ stop POST, opening into the workbench.
    assert f'action="/todo/{hot.id}/status"' in resp.text
    assert f'action="/todo/{hot.id}/stop"' in resp.text
    assert f'href="/todo/{hot.id}"' in resp.text


def test_focus_scopes_the_browse_to_one_subtree(drive_client, store) -> None:
    root = _todo(store, "root A")
    child = _todo(store, "child A", parent_id=root.id)
    other = _todo(store, "root B")
    _todo(store, "leaf B", parent_id=other.id)
    resp = drive_client.get(f"/drive?k=todo&focus={root.id}")
    assert "root A" in resp.text
    assert "child A" in resp.text
    assert "root B" not in resp.text
    assert "leaf B" not in resp.text
    # …and the doable queue scopes the same way (under that subtree).
    resp = drive_client.get(f"/drive?k=todo&queue=doable&focus={root.id}")
    assert "child A" in resp.text
    assert "leaf B" not in resp.text


def test_serves_scopes_to_the_quests_serving_todos(drive_client, store) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="the quest")
    serving = _todo(store, "serving todo")
    _todo(store, "unrelated todo")
    store.add_link(
        src_ref_id=serving.id,
        dst_ref_id=quest.id,
        relation="serves",
        set_by="system",
    )
    resp = drive_client.get(f"/drive?k=todo&serves=qu{quest.id}")
    assert "serving todo" in resp.text
    assert "unrelated todo" not in resp.text


def test_todo_row_badges_and_actions_render(drive_client, store) -> None:
    todo = _todo(store, "badge me", prio=2)
    store.add_tag(todo.id, Tag.closed("STATUS", "doing"))
    resp = drive_client.get("/drive?k=todo&state=all")
    assert resp.status_code == 200
    assert "P2" in resp.text
    assert ">doing</" in resp.text or "doing" in resp.text
    # A halted row offers ▶ start instead of ⏹ stop.
    store.add_tag(todo.id, Tag.open("halt"))
    resp = drive_client.get("/drive?k=todo&state=all")
    assert f'action="/todo/{todo.id}/start"' in resp.text
    assert f'action="/todo/{todo.id}/stop"' not in resp.text
    assert "halted" in resp.text


def test_doable_queue_honours_the_tag_chips(drive_client, store) -> None:
    """Review (td664890): ``tag=`` narrows the doable rotation through
    the same leaf filter ``search(kind='todo', view='doable')`` runs —
    applied inside the queue's SQL, before pagination, so a filtered
    page still fills."""
    doing = _todo(store, "doing leaf")
    store.add_tag(doing.id, Tag.closed("STATUS", "doing"))
    _todo(store, "open leaf")
    resp = drive_client.get("/drive?k=todo&queue=doable&tag=STATUS:doing")
    assert resp.status_code == 200
    assert "doing leaf" in resp.text
    assert "open leaf" not in resp.text
    # A negated chip excludes the same way.
    resp = drive_client.get("/drive?k=todo&queue=doable&tag=!STATUS:doing")
    assert resp.status_code == 200
    assert "open leaf" in resp.text
    assert "doing leaf" not in resp.text


def test_todo_preset_params_survive_the_filter_form(drive_client, store) -> None:
    """Review (td664890): queue/focus/serves have no picker control, so
    they ride the filter form as hidden inputs — a facet resubmit keeps
    the queue and the subtree scope instead of silently dropping them."""
    root = _todo(store, "the root")
    _todo(store, "its leaf", parent_id=root.id)
    resp = drive_client.get(f"/drive?k=todo&queue=doable&focus={root.id}")
    assert resp.status_code == 200
    assert 'name="queue" value="doable"' in resp.text
    assert f'name="focus" value="{root.id}"' in resp.text
    # …and serves= serializes the same way.
    resp = drive_client.get("/drive?k=todo&queue=doable&serves=qu7")
    assert 'name="serves" value="qu7"' in resp.text


def test_job_row_badges_and_retry_render(drive_client, store) -> None:
    parent = _todo(store, "the parent")
    job = store.insert_ref(
        kind="job", slug=None, title="attempt 1", parent_id=parent.id
    )
    store.add_tag(job.id, Tag.closed("STATUS", "failed"))
    # folder=* ("Anywhere") — a job always has a parent, so the default
    # unfiled top-level would hide it.
    resp = drive_client.get("/drive?k=job&state=all&folder=*")
    assert resp.status_code == 200
    assert "failed" in resp.text
    assert f'action="/todo/{job.id}/retry"' in resp.text
    # open_url is the generic /refs/job/{id} reader, like every other
    # surface's job link.
    assert f'href="/refs/job/{job.id}"' in resp.text
