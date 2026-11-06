"""Pause/Resume web controls on ``/work`` (td573297) — the browser twin of
``edit(kind='quest', mode='pause'|'resume')`` and
``tag(kind='todo', ..., add=['STATUS:paused'])``.

Builds on the ``_tree``/``client`` fixtures ``tests/test_web_work.py``
already drives against a fixture quest → root → slices tree.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

import precis_web.routes.work as work_mod
from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from tests.test_todo_work_view import _tree
from tests.test_web_work import _row_tags


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


@pytest.fixture
def client(runtime_with_store: Any, tmp_path: Path) -> TestClient:
    app = create_app(
        runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
    )
    return TestClient(app)


def _fake_signed_in_user(login: str = "reto"):
    from precis.users import WebUser

    def _fake_user(request: Any) -> WebUser:
        return WebUser(
            id=1,
            login=login,
            abbrev="rs",
            full_name=None,
            email=None,
            disabled_at=None,
            last_login_at=None,
            created_at=None,
            updated_at=None,
        )

    return _fake_user


def _sign_in(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_mod, "current_user", _fake_signed_in_user())


def _sign_out(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(work_mod, "current_user", lambda request: None)


class TestControlsVisibility:
    def test_buttons_present_with_can_write(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        _sign_in(monkeypatch)
        html = client.get("/work").text
        assert f'action="/work/slice/{t["a"]}/pause"' in html
        assert f'action="/work/quest/{t["quest"]}/pause"' in html

    def test_buttons_absent_without_can_write(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        _sign_out(monkeypatch)
        html = client.get("/work").text
        content = html[html.index("<h1") :]
        assert "<form" not in content
        assert f"/work/slice/{t['a']}" not in html
        assert f"/work/quest/{t['quest']}" not in html

    def test_done_row_has_no_pause_resume_button(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        handler.tag(id=t["b"], add=["STATUS:done"])
        _sign_in(monkeypatch)
        html = client.get("/work").text
        tags = _row_tags(html)
        b_start = html.index(tags[f"td{t['b']}"])
        b_row = html[b_start : html.index("</li>", b_start)]
        assert "<form" not in b_row

    def test_paused_row_shows_resume_not_pause(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        handler.tag(id=t["b"], add=["STATUS:paused"])
        _sign_in(monkeypatch)
        html = client.get("/work").text
        tags = _row_tags(html)
        b_start = html.index(tags[f"td{t['b']}"])
        b_row = html[b_start : html.index("</li>", b_start)]
        assert f'action="/work/slice/{t["b"]}/resume"' in b_row
        assert f'action="/work/slice/{t["b"]}/pause"' not in b_row


class TestControlsDispatch:
    def test_slice_post_sets_status_paused(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        _sign_in(monkeypatch)
        resp = client.post(f"/work/slice/{t['b']}/pause")
        assert resp.status_code == 200  # followed the 303 to /work
        tags = [str(tag) for tag in store.tags_for(t["b"])]
        assert "STATUS:paused" in tags

    def test_quest_post_pauses_quest_and_its_root(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        _sign_in(monkeypatch)
        resp = client.post(f"/work/quest/{t['quest']}/pause")
        assert resp.status_code == 200
        assert "STATUS:dormant" in [str(tag) for tag in store.tags_for(t["quest"])]
        assert "STATUS:paused" in [str(tag) for tag in store.tags_for(t["root"])]

    def test_post_without_user_is_403(
        self, client: TestClient, handler: TodoHandler, store: Any, monkeypatch: Any
    ) -> None:
        t = _tree(handler, store)
        _sign_out(monkeypatch)
        assert client.post(f"/work/slice/{t['b']}/pause").status_code == 403
        assert client.post(f"/work/quest/{t['quest']}/pause").status_code == 403
        # neither write happened
        assert "STATUS:paused" not in [str(tag) for tag in store.tags_for(t["b"])]
