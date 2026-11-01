"""gr543831: the devin_api job executor (workers/executors/devin_api.py).

``api`` is injected — no network anywhere in this file (the real
transport lives behind ``_HttpxSessionAPI`` / ``with_backoff``). Jobs
are minted against the real test store; the ``job_devin_api`` service
is lit via ``set_service_prio`` exactly as an operator would.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.utils.http import VendorError
from precis.workers.executors import devin_api
from precis.workers.service_config import set_service_prio

pytestmark = pytest.mark.db


class FakeAPI:
    """Recorded-fixture SessionAPI: canned session dicts per get."""

    def __init__(self) -> None:
        self.created: list[dict[str, Any]] = []
        self.terminated: list[str] = []
        self._answers: dict[str, list[dict[str, Any]]] = {}
        self.create_exc: Exception | None = None

    def answer(self, devin_id: str, *sessions: dict[str, Any]) -> None:
        self._answers[devin_id] = list(sessions)

    def create_session(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.created.append(payload)
        if self.create_exc is not None:
            raise self.create_exc
        return {
            "session_id": "devin-abc123",
            "url": "https://app.devin.ai/sessions/devin-abc123",
            "status": "new",
            "pull_requests": [],
            "acus_consumed": 0,
        }

    def get_session(self, org_id: str, devin_id: str) -> dict[str, Any]:
        answers = self._answers.get(devin_id)
        if answers and len(answers) > 1:
            return answers.pop(0)
        if answers:
            return answers[0]
        return {
            "session_id": devin_id,
            "url": "x",
            "status": "running",
            "status_detail": "working",
            "pull_requests": [],
            "acus_consumed": 1.5,
        }

    def terminate_session(self, org_id: str, devin_id: str) -> None:
        self.terminated.append(devin_id)


@pytest.fixture
def api() -> FakeAPI:
    return FakeAPI()


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEVIN_API_TOKEN", "cog_test_token")
    monkeypatch.setenv("PRECIS_DEVIN_ORG_ID", "org-test")


def _enable(store: Store) -> None:
    set_service_prio(store, "*", devin_api.SERVICE_NAME, 1, actor="test")


def _mk_queued_job(store: Store, params: dict[str, Any] | None = None) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="devin_task test job",
        meta={
            "executor": "devin_api",
            "job_type": "devin_task",
            "params": params or {"prompt": "fix the thing"},
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "queued"), set_by="agent")
    return int(ref.id)


def _mk_running_job(
    store: Store, session_id: str = "devin-abc123", meta_extra: dict | None = None
) -> int:
    meta: dict[str, Any] = {
        "executor": "devin_api",
        "job_type": "devin_task",
        "params": {"prompt": "fix the thing"},
        "devin_session_id": session_id,
    }
    if meta_extra:
        meta.update(meta_extra)
    ref = store.insert_ref(
        kind="job", slug=None, title="running devin_task job", meta=meta
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "running"), set_by="agent")
    return int(ref.id)


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _summary(store: Store, ref_id: int) -> str:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord >= 0 ORDER BY ord",
            (ref_id,),
        ).fetchall()
    return "\n".join(str(r[0]) for r in rows)


# ── dark gate ──────────────────────────────────────────────────────


def test_dark_until_service_config_lights(store: Store, api: FakeAPI) -> None:
    _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result == {"claimed": 0, "ok": 0, "failed": 0, "parked": 0}
    assert api.created == []


def test_missing_token_or_org_noops(
    store: Store, api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(store)
    monkeypatch.delenv("DEVIN_API_TOKEN")
    _mk_queued_job(store)
    assert devin_api.run_devin_api_pass(store, api=api)["claimed"] == 0
    assert api.created == []


# ── mint → poll → terminal ─────────────────────────────────────────


def test_mints_session_and_records_id(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["claimed"] == 1 and result["ok"] == 1
    meta = _meta(store, rid)
    assert meta["devin_session_id"] == "devin-abc123"
    assert _status(store, rid) == "running"
    assert api.created[0]["prompt"] == "fix the thing"
    # No prod-write credential: the payload carries only prompt/tags.
    assert "secret_ids" not in api.created[0]
    assert "session_secrets" not in api.created[0]


def test_poll_to_succeeded_collects_result_and_usage(
    store: Store, api: FakeAPI
) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        {
            "session_id": "devin-abc123",
            "url": "https://app.devin.ai/sessions/devin-abc123",
            "status": "exit",
            "status_detail": "finished",
            "pull_requests": [
                {"pr_url": "https://github.com/o/r/pull/7", "pr_state": "open"}
            ],
            "structured_output": {"commit": "deadbeef"},
            "acus_consumed": 2.25,
            "on_demand_usage": 0,
        },
    )
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["ok"] == 1
    assert _status(store, rid) == "succeeded"
    meta = _meta(store, rid)
    assert meta["usage"]["acus_consumed"] == 2.25
    assert meta["devin"]["pull_requests"][0]["pr_url"].endswith("/pull/7")
    assert "devin-abc123" in _summary(store, rid)


def test_session_error_fails(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        {"session_id": "devin-abc123", "url": "x", "status": "error"},
    )
    devin_api.run_devin_api_pass(store, api=api)
    assert _status(store, rid) == "failed"


# ── paused / quota / plan ──────────────────────────────────────────


def test_quota_suspend_parks_job_and_pauses_lane(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        {
            "session_id": "devin-abc123",
            "url": "x",
            "status": "suspended",
            "status_detail": "out_of_quota",
        },
    )
    queued = _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["parked"] == 1
    assert _status(store, rid) == "waiting_time"
    # Lane paused: the queued job was not minted this tick.
    assert api.created == []
    assert _status(store, queued) == "queued"
    assert store.get_setting(devin_api._PAUSED_KEY)


def test_plan_lacks_api_fails_clean(store: Store, api: FakeAPI) -> None:
    _enable(store)
    api.create_exc = VendorError("devin", "forbidden", status=403)
    rid = _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["failed"] == 1
    assert _status(store, rid) == "failed"
    meta = _meta(store, rid)
    assert meta["failure_class"] == "config"
    assert "plan lacks Devin API" in _summary(store, rid)


def test_vanished_session_fails(store: Store, api: FakeAPI) -> None:
    _enable(store)

    class _Gone(FakeAPI):
        def get_session(self, org_id: str, devin_id: str) -> dict[str, Any]:
            raise VendorError("devin", "not found", status=404)

    rid = _mk_running_job(store)
    devin_api.run_devin_api_pass(store, api=_Gone())
    assert _status(store, rid) == "failed"


def test_blocked_session_parks_without_lane_pause(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        {
            "session_id": "devin-abc123",
            "url": "x",
            "status": "suspended",
            "status_detail": "waiting_for_user",
        },
    )
    _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["parked"] == 1
    assert _status(store, rid) == "waiting_time"
    # Lane NOT paused on a human-input block: the queued job minted.
    assert len(api.created) == 1


def test_rejecting_params_fail_before_mint(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_queued_job(store, params={"prompt": ""})
    devin_api.run_devin_api_pass(store, api=api)
    assert _status(store, rid) == "failed"
    assert api.created == []
