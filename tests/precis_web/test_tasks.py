"""Tests for the tasks route planner wizard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from precis_web.routes.tasks import _job_timing, _stuck_job, _tree_summary


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


# ── _job_timing ───────────────────────────────────────────────────────


def test_job_timing_queued_or_open_is_blank() -> None:
    now = datetime.now(UTC)
    assert _job_timing("queued", now, None, None) == ""
    assert _job_timing("open", now, None, None) == ""


def test_job_timing_finished_with_started_at_shows_queued_and_ran() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    started = created + timedelta(seconds=12)
    finished = started + timedelta(seconds=258)  # 4m18s
    result = _job_timing("succeeded", created, started, finished)
    assert "queued " in result
    assert "ran " in result
    assert result == "queued 12s · ran 4m18s"


def test_job_timing_finished_without_started_at_uses_fused_span() -> None:
    created = datetime(2026, 1, 1, tzinfo=UTC)
    finished = created + timedelta(seconds=90)
    result = _job_timing("failed", created, None, finished)
    assert result.startswith("queued+ran")
    assert result == "queued+ran 1m30s"


def test_job_timing_running_with_started_at_shows_running() -> None:
    started = datetime.now(UTC) - timedelta(seconds=30)
    created = started - timedelta(seconds=5)
    result = _job_timing("running", created, started, None)
    assert "running " in result


# ── _stuck_job ────────────────────────────────────────────────────────


def test_stuck_job_not_running_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_STUCK_JOB_HOURS", "1")
    old_since = datetime.now(UTC) - timedelta(hours=5)
    assert _stuck_job("queued", old_since, None) is False


def test_stuck_job_running_live_lease_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_STUCK_JOB_HOURS", "1")
    old_since = datetime.now(UTC) - timedelta(hours=5)
    future_lease = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    assert _stuck_job("running", old_since, future_lease) is False


def test_stuck_job_running_expired_lease_recent_since_is_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_STUCK_JOB_HOURS", "1")
    recent_since = datetime.now(UTC) - timedelta(minutes=5)
    assert _stuck_job("running", recent_since, None) is False


def test_stuck_job_running_expired_lease_old_since_is_true(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_STUCK_JOB_HOURS", "1")
    old_since = datetime.now(UTC) - timedelta(hours=2)
    assert _stuck_job("running", old_since, None) is True


# ── _tree_summary ─────────────────────────────────────────────────────


def test_tree_summary_counts_every_bucket_and_picks_oldest_running() -> None:
    now = datetime.now(UTC)
    rows: list[dict[str, Any]] = [
        {
            "kind": "job",
            "status": "running",
            "started_at": now - timedelta(minutes=5),
            "status_since": None,
            "stuck": False,
            "child_failures": [],
            "halted": False,
        },
        {
            # The oldest running job — sits in the middle of the list so
            # a "pick first" or "pick last" bug wouldn't be caught.
            "kind": "job",
            "status": "running",
            "started_at": now - timedelta(hours=2),
            "status_since": None,
            "stuck": True,
            "child_failures": [],
            "halted": False,
        },
        {
            "kind": "job",
            "status": "running",
            "started_at": now - timedelta(seconds=30),
            "status_since": None,
            "stuck": False,
            "child_failures": [],
            "halted": False,
        },
        {
            "kind": "job",
            "status": "queued",
            "started_at": None,
            "status_since": None,
            "stuck": False,
            "child_failures": [],
            "halted": False,
        },
        {
            "kind": "job",
            "status": "queued",
            "started_at": None,
            "status_since": None,
            "stuck": False,
            "child_failures": [],
            "halted": False,
        },
        {
            "kind": "todo",
            "status": "open",
            "started_at": None,
            "status_since": None,
            "stuck": False,
            "child_failures": [{"job_id": 1, "reason": "boom"}],
            "halted": False,
        },
        {
            "kind": "todo",
            "status": "open",
            "started_at": None,
            "status_since": None,
            "stuck": False,
            "child_failures": [],
            "halted": True,
        },
    ]
    oldest_running = rows[1]["started_at"]

    summary = _tree_summary(rows)

    assert summary["running"] == 3
    assert summary["queued"] == 2
    assert summary["stuck"] == 1
    assert summary["parked"] == 1
    assert summary["halted"] == 1
    assert summary["oldest_running"] == oldest_running
    assert summary["oldest_running_for"].startswith("2h")
