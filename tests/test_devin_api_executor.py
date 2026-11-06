"""gr543831: the devin_api job executor (workers/executors/devin_api.py).

``api`` is injected — no network anywhere in this file (the real
transport lives behind ``_HttpxSessionAPI`` / ``with_backoff``). Jobs
are minted against the real test store; the ``job_devin_api`` service
is lit via ``set_service_prio`` exactly as an operator would.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.utils.http import VendorError
from precis.workers.executors import devin_api
from precis.workers.service_config import set_service_prio
from tests.test_llm_accounts import restore_seeded_pools  # noqa: F401  fixture

pytestmark = [pytest.mark.db, pytest.mark.usefixtures("restore_seeded_pools")]


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
    assert "plan lacks the Devin API" in _summary(store, rid)


def test_forbidden_create_quotes_provider_detail(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr570579: a live-shape 403 — the provider's problem+json detail
    rides the VendorError into the job reason, which quotes it and
    names no permission it cannot verify."""
    _enable(store)
    detail = "service user cannot create sessions in this org"
    resp = httpx.Response(
        403,
        json={"detail": detail},
        request=httpx.Request(
            "POST", "https://api.devin.ai/v3/organizations/org-test/sessions"
        ),
    )

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def request(self, *_a: Any, **_kw: Any) -> httpx.Response:
            return resp

    def _fake_client(**_kw: Any) -> _Client:
        return _Client()

    def _no_limiter(*_a: Any, **_kw: Any) -> None:
        return None

    # No api= injection: the real _HttpxSessionAPI + real with_backoff,
    # with only the transport client and the shared limiter faked.
    monkeypatch.setattr("precis.utils.http.http_client", _fake_client)
    monkeypatch.setattr("precis.utils.rate_limit.require", _no_limiter)
    rid = _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store)
    assert result["failed"] == 1
    assert _status(store, rid) == "failed"
    assert _meta(store, rid)["failure_class"] == "config"
    summary = _summary(store, rid)
    assert detail in summary
    assert "UseDevinSessions" not in summary


def test_refusal_detail_masks_echoed_credential(store: Store, api: FakeAPI) -> None:
    """gr570579 review: a provider that echoes a credential inside its
    refusal detail gets it masked out of the job reason and the error
    meta — a kept body is never trusted to be credential-free."""
    _enable(store)
    key = "sk-ant-" + "aB3x" * 10
    exc = VendorError(
        "devin",
        "POST https://api.devin.ai/x -> HTTP 403",
        status=403,
        body=f'{{"detail":"token {key} rejected"}}',
    )
    # The reason text itself — it is logged and composed into messages
    # before the store's write-time mask ever sees it.
    reason = devin_api._plan_failure_reason(exc)
    assert reason is not None
    assert key not in reason
    assert "<redacted:" in reason
    api.create_exc = exc
    rid = _mk_queued_job(store)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["failed"] == 1
    summary = _summary(store, rid)
    assert key not in summary
    assert "<redacted:" in summary
    error = str(_meta(store, rid).get("error") or "")
    assert key not in error
    assert "<redacted:" in error


def test_send_keeps_refusal_body_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr570579: _send keeps the first 300 characters of a refused
    non-200 body on the VendorError so the reason can quote it."""
    detail = "plan has no API entitlement " + "x" * 400
    resp = httpx.Response(
        402,
        json={"detail": detail},
        request=httpx.Request(
            "POST", "https://api.devin.ai/v3/organizations/org-test/sessions"
        ),
    )

    class _Client:
        def __enter__(self) -> _Client:
            return self

        def __exit__(self, *exc: object) -> None:
            return None

        def request(self, *_a: Any, **_kw: Any) -> httpx.Response:
            return resp

    def _fake_client(**_kw: Any) -> _Client:
        return _Client()

    def _no_limiter(*_a: Any, **_kw: Any) -> None:
        return None

    monkeypatch.setattr("precis.utils.http.http_client", _fake_client)
    monkeypatch.setattr("precis.utils.rate_limit.require", _no_limiter)
    api = devin_api._HttpxSessionAPI("tok", "https://api.devin.ai")
    with pytest.raises(VendorError) as excinfo:
        api.create_session("org-test", {"prompt": "x"})
    assert excinfo.value.status == 402
    assert excinfo.value.body == resp.text[:300]


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


# ── ACU pool feed (td572038) ──────────────────────────────────────


def _running_session(acus: float | None, **over: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "session_id": "devin-abc123",
        "url": "x",
        "status": "running",
        "status_detail": "working",
        "pull_requests": [],
    }
    if acus is not None:
        session["acus_consumed"] = acus
    session.update(over)
    return session


def _acu_used(store: Store) -> float | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT used_reported FROM external_rate_limits WHERE provider = %s",
            ("llm:devin:acu",),
        ).fetchone()
    assert row is not None
    return float(row[0]) if row[0] is not None else None


def test_poll_feeds_acu_delta_to_pool(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_running_job(
        store, meta_extra={"usage": {"acus_consumed": 1.0, "acus_fed": 1.0}}
    )
    api.answer("devin-abc123", _running_session(3.5))
    devin_api.run_devin_api_pass(store, api=api)
    # The pool gets the delta over the acus_fed mark, not the
    # cumulative 3.5; the mark advances to the fed figure.
    assert _acu_used(store) == 2.5
    usage = _meta(store, rid)["usage"]
    assert usage["acus_consumed"] == 3.5
    assert usage["acus_fed"] == 3.5


def test_poll_second_poll_feeds_only_the_increment(store: Store, api: FakeAPI) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer("devin-abc123", _running_session(3.5), _running_session(4.0))
    devin_api.run_devin_api_pass(store, api=api)
    assert _acu_used(store) == 3.5
    devin_api.run_devin_api_pass(store, api=api)
    # Second poll's delta reads off the acus_fed mark (3.5), so only
    # the 0.5 increment feeds — never a double count.
    assert _acu_used(store) == 4.0
    assert _meta(store, rid)["usage"]["acus_fed"] == 4.0


def test_poll_absent_report_keeps_mark_and_never_refeeds(
    store: Store, api: FakeAPI
) -> None:
    """Sequence 5, absent, 7 feeds 5 then 2 — the absent poll can neither
    feed nor null the last numeric acus_consumed."""
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        _running_session(5.0),
        _running_session(None),
        _running_session(7.0),
    )
    devin_api.run_devin_api_pass(store, api=api)
    assert _acu_used(store) == 5.0
    devin_api.run_devin_api_pass(store, api=api)
    usage = _meta(store, rid)["usage"]
    assert usage["acus_consumed"] == 5.0  # kept, not overwritten with None
    assert usage["acus_fed"] == 5.0
    assert _acu_used(store) == 5.0
    devin_api.run_devin_api_pass(store, api=api)
    assert _acu_used(store) == 7.0
    assert _meta(store, rid)["usage"]["acus_fed"] == 7.0


def test_poll_falling_report_never_lowers_the_mark(store: Store, api: FakeAPI) -> None:
    """Sequence 5, 3, 6 feeds 5 then 1 — a report below the mark feeds
    nothing and the mark never drops."""
    _enable(store)
    rid = _mk_running_job(store)
    api.answer(
        "devin-abc123",
        _running_session(5.0),
        _running_session(3.0),
        _running_session(6.0),
    )
    devin_api.run_devin_api_pass(store, api=api)
    assert _acu_used(store) == 5.0
    devin_api.run_devin_api_pass(store, api=api)
    usage = _meta(store, rid)["usage"]
    assert usage["acus_consumed"] == 3.0  # provider truth is mirrored
    assert usage["acus_fed"] == 5.0  # the mark never lowers
    assert _acu_used(store) == 5.0
    devin_api.run_devin_api_pass(store, api=api)
    assert _acu_used(store) == 6.0
    assert _meta(store, rid)["usage"]["acus_fed"] == 6.0


def test_poll_pool_failure_does_not_fail_the_job(
    store: Store, api: FakeAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable(store)
    rid = _mk_running_job(store)
    api.answer("devin-abc123", _running_session(3.5))

    def boom(*a: Any, **kw: Any) -> bool:
        raise RuntimeError("pool write exploded")

    monkeypatch.setattr("precis.utils.llm.accounts.add_pool_usage", boom)
    result = devin_api.run_devin_api_pass(store, api=api)
    assert result["failed"] == 0
    assert _status(store, rid) == "running"
    # The usage mirror committed but the mark did not advance — the
    # delta retries on the next poll instead of being lost.
    usage = _meta(store, rid)["usage"]
    assert usage["acus_consumed"] == 3.5
    assert "acus_fed" not in usage
    assert _acu_used(store) is None
