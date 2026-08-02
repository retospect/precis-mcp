"""Tests for the tasks route planner wizard."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def store(client: TestClient) -> Any:
    """Expose the fake store attached to the test app runtime."""
    return client.app.state.runtime.store  # type: ignore[attr-defined]


def test_create_root_parked_no_llm_tag(client: TestClient, store: Any) -> None:
    """A new root without ``start`` is parked: rotation_root but no llm_tier/executor."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    response = client.post(
        "/tasks/roots",
        data={
            "text": "Write the report",
            "description": "A project report",
            "doc_type": "draft",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    calls = [c for c in runtime.calls if c[0] == "put"]
    assert len(calls) == 1
    args = calls[0][1]
    assert args["kind"] == "todo"
    assert args["text"] == "Write the report"
    assert args["body"] == "A project report"
    assert args["meta"]["doc_type"] == "draft"
    assert args["meta"]["rotation_root"] is True
    assert "workspace" not in args["meta"]
    assert "llm_tier" not in args["meta"]


def test_create_root_start_now_stamps_llm_opus(client: TestClient, store: Any) -> None:
    """A new root with ``start=on`` immediately stamps llm_tier='opus' and a workspace."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    response = client.post(
        "/tasks/roots",
        data={
            "text": "Write the paper",
            "description": "About widgets",
            "doc_type": "paper",
            "start": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    calls = [c for c in runtime.calls if c[0] == "put"]
    assert len(calls) == 1
    args = calls[0][1]
    assert args["kind"] == "todo"
    assert args["text"] == "Write the paper"
    assert args["body"] == "About widgets"
    assert args["meta"]["doc_type"] == "paper"
    assert "workspace" in args["meta"]
    assert args["meta"]["workspace"]["format"] == "tex"
    assert args["meta"]["workspace"]["entrypoint"] == "main.tex"
    assert args["meta"]["rotation_root"] is True
    assert args["meta"]["llm_tier"] == "opus"


def test_create_root_start_now_draft_uses_md_workspace(
    client: TestClient, store: Any
) -> None:
    """Non-paper doc types seed an md workspace."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    response = client.post(
        "/tasks/roots",
        data={
            "text": "Pitch deck",
            "description": "",
            "doc_type": "pres",
            "start": "on",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    args = [c for c in runtime.calls if c[0] == "put"][0][1]
    assert args["meta"]["workspace"]["format"] == "md"
    assert args["meta"]["workspace"]["entrypoint"] == "main.md"


def test_start_task_seeds_workspace_and_llm_for_parked_root(
    client: TestClient, store: Any
) -> None:
    """The ▶ start button on a parked root adds workspace + llm_tier='opus'."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    # Ref id=1 is a canned root with empty meta and no llm_tier.
    response = client.post("/tasks/1/start", follow_redirects=False)
    assert response.status_code == 303

    # workspace meta merged
    ws_writes = [m for m in store.meta_writes if m[0] == 1]
    assert len(ws_writes) == 1
    assert "workspace" in ws_writes[0][1]
    assert ws_writes[0][1]["workspace"]["format"] == "md"

    # llm_tier='opus' set via tag(meta=...)
    tag_calls = [c for c in runtime.calls if c[0] == "tag" and c[1].get("id") == 1]
    assert len(tag_calls) == 1
    assert tag_calls[0][1]["meta"]["llm_tier"] == "opus"


def test_start_task_skips_llm_when_already_present(
    client: TestClient, store: Any
) -> None:
    """Starting a todo that already has meta.llm_tier does not add it again."""
    runtime = client.app.state.runtime  # type: ignore[attr-defined]
    # Ref id=81 is the canned planner parent with meta.llm_tier='opus'.
    response = client.post("/tasks/81/start", follow_redirects=False)
    assert response.status_code == 303
    # No tag(meta=...) call setting llm_tier for id=81 (it's already set).
    tag_calls = [c for c in runtime.calls if c[0] == "tag" and c[1].get("id") == 81]
    assert not any("llm_tier" in c[1].get("meta", {}) for c in tag_calls)
