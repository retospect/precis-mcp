"""The morning briefing as a deliverable (gr347551): a budget pause degrades,
notifies and re-queues hourly to the deadline instead of failing silently."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from precis.budget import outcomes as outcomes_mod
from precis.budget.work_class import WorkClass
from precis.store import Store
from precis.utils.llm.router import DispatchClient, DispatchError, Tier
from precis.workers import briefing
from precis.workers.job_types import briefing as briefing_job
from precis.workers.schedule.worker import (
    DELIVERABLE_RETRY_AT,
    mint_deliverable_retries,
)

MSGS = [{"role": "user", "content": "headlines"}]


def _ladder(monkeypatch: pytest.MonkeyPatch, paused: set[Tier]) -> list[Tier]:
    tried: list[Tier] = []

    def complete(self: DispatchClient, messages: Any, **kw: Any) -> Any:
        tried.append(self.tier)
        if self.tier in paused:
            raise DispatchError(f"budget: {self.tier.value} paused", paused=True)
        return type("R", (), {"text": "brief", "model": f"model-{self.tier.value}"})()

    monkeypatch.setattr(DispatchClient, "complete", complete)
    return tried


def test_compose_serves_on_frontier_when_admitted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tried = _ladder(monkeypatch, paused=set())
    result, degraded = briefing._compose(cast("Store", None), MSGS)
    assert (tried, degraded, result.model) == ([Tier.FRONTIER], False, "model-frontier")


def test_compose_degrades_to_medium_and_tallies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[WorkClass, str]] = []
    monkeypatch.setattr(outcomes_mod, "record", lambda s, c, o: seen.append((c, o)))
    tried = _ladder(monkeypatch, paused={Tier.FRONTIER})
    result, degraded = briefing._compose(cast("Store", None), MSGS)
    assert tried == [Tier.FRONTIER, Tier.MEDIUM]  # a pause is not retried in place
    assert degraded and result.model == "model-medium"
    assert seen == [(WorkClass.DELIVERABLE, "degraded")]


def test_compose_raises_paused_when_every_rung_paused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ladder(monkeypatch, paused=set(briefing.DELIVERABLE_LADDER))
    with pytest.raises(briefing.BriefingPaused, match="medium paused"):
        briefing._compose(cast("Store", None), MSGS)


def test_compose_propagates_real_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def complete(self: DispatchClient, messages: Any, **kw: Any) -> Any:
        raise DispatchError("transport died", paused=False)

    monkeypatch.setattr(DispatchClient, "complete", complete)
    monkeypatch.setattr(briefing, "drain_sleep", lambda s: None)
    with pytest.raises(DispatchError, match="transport died"):
        briefing._compose(cast("Store", None), MSGS)


def test_paused_dispatch_error_is_not_transient() -> None:
    assert briefing._is_transient_llm_error(DispatchError("x", paused=False))
    assert not briefing._is_transient_llm_error(DispatchError("x", paused=True))


# ── handle_pause: notice + next attempt ─────────────────────────────


def _notices(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    posted: list[tuple[str, str]] = []

    def post(store: Any, target: str, text: str, *, date_tag: str, notice: str) -> bool:
        posted.append((notice, text))
        return True

    monkeypatch.setattr(briefing, "post_notice", post)
    return posted


def test_pause_before_deadline_notifies_and_retries_in_an_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    posted = _notices(monkeypatch)
    now = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
    at = briefing.handle_pause(
        cast("Store", None), deliver_to="conv:x", now=now, deadline_utc=None, reason="r"
    )
    assert at == now + timedelta(hours=1)
    assert [n for n, _ in posted] == ["delayed"]
    assert "12:00 UTC" in posted[0][1]


def test_pause_last_attempt_lands_on_the_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _notices(monkeypatch)
    now = datetime(2026, 10, 12, 11, 30, tzinfo=UTC)
    at = briefing.handle_pause(
        cast("Store", None), deliver_to="conv:x", now=now, deadline_utc=None, reason="r"
    )
    assert at == datetime(2026, 10, 12, 12, 0, tzinfo=UTC)


def test_pause_at_deadline_skips_with_notice(monkeypatch: pytest.MonkeyPatch) -> None:
    posted = _notices(monkeypatch)
    now = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    at = briefing.handle_pause(
        cast("Store", None),
        deliver_to="conv:x",
        now=now,
        deadline_utc="09:00",
        reason="r",
    )
    assert at is None
    assert [n for n, _ in posted] == ["skipped"]
    assert "skipped today" in posted[0][1]


def test_deadline_parse_falls_back_on_garbage() -> None:
    day = datetime(2026, 10, 12, 7, tzinfo=UTC)
    assert briefing.deadline_for(day, "bogus").hour == 12
    assert briefing.deadline_for(day, "08:30") == day.replace(hour=8, minute=30)


# ── the job wrapper ─────────────────────────────────────────────────


class _Ctx:
    def __init__(self, params: dict[str, Any]) -> None:
        self.store = object()
        self.ref_id = 7
        self.meta = {"params": params}
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}
        self.failures: list[str] = []

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, msg: str) -> None:
        self.failures.append(msg)


def _paused_run(store: Any, **kw: Any) -> dict[str, Any]:
    raise briefing.BriefingPaused("budget: claude quota low")


def test_job_pause_requeues_instead_of_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(briefing, "run_briefing", _paused_run)
    at = datetime(2026, 10, 12, 8, tzinfo=UTC)
    calls: dict[str, Any] = {}

    def handle(store: Any, **kw: Any) -> datetime:
        calls.update(kw)
        return at

    monkeypatch.setattr(briefing, "handle_pause", handle)
    monkeypatch.setattr(briefing_job, "_request_retry", lambda ctx, when: True)
    ctx = _Ctx({"deliver_to": "conv:x", "deadline_utc": "10:00"})
    briefing_job._dispatch(ctx, None)
    assert ctx.failures == []
    assert calls["deliver_to"] == "conv:x" and calls["deadline_utc"] == "10:00"
    assert ctx.meta_set == {"paused": True, "retry_at": at.isoformat()}


def test_job_pause_past_deadline_is_skipped_not_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(briefing, "run_briefing", _paused_run)
    monkeypatch.setattr(briefing, "handle_pause", lambda store, **kw: None)
    ctx = _Ctx({"deliver_to": "conv:x"})
    briefing_job._dispatch(ctx, None)
    assert ctx.failures == []
    assert ctx.meta_set == {"paused": True, "skipped": True}


def test_deadline_param_in_schema() -> None:
    props = briefing_job.SPEC.params_schema["properties"]
    assert "deadline_utc" in props


# ── DB: notice idempotency + the schedule worker's retry mint ───────


def test_post_notice_is_once_per_day_and_kind(store: Store) -> None:
    args = {"date_tag": "2026-10-12"}
    assert briefing.post_notice(store, "conv:x", "delayed", notice="delayed", **args)
    assert not briefing.post_notice(store, "conv:x", "again", notice="delayed", **args)
    assert briefing.post_notice(store, "conv:x", "skipped", notice="skipped", **args)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta->>'briefing_notice', meta ? 'briefing_date' FROM refs "
            "WHERE kind = 'message' AND meta->>'briefing_notice_date' = '2026-10-12' "
            "ORDER BY ref_id"
        ).fetchall()
    # No briefing_date key: the notice must not read as "brief delivered".
    assert rows == [("delayed", False), ("skipped", False)]


def test_retry_mints_a_fresh_tick_once(store: Store) -> None:
    rec = store.insert_ref(
        kind="todo",
        slug=None,
        title="Morning briefing",
        meta={
            "schedule": {"cron": "0 7 * * *"},
            "executor": "claude_inproc",
            "job_type": "briefing",
            "params": {"deliver_to": "conv:x"},
        },
    )
    now = datetime.now(UTC)
    tick = store.insert_ref(
        kind="todo",
        slug=None,
        title="Morning briefing 2026-10-12",
        parent_id=rec.id,
        meta={
            "spawned_for_tick": "2026-10-12T07:00+00:00",
            DELIVERABLE_RETRY_AT: (now + timedelta(minutes=30)).isoformat(),
        },
    )
    assert mint_deliverable_retries(store, now=now) == 0  # not due yet
    later = now + timedelta(hours=1)
    assert mint_deliverable_retries(store, now=later) == 1
    assert mint_deliverable_retries(store, now=later) == 0  # fires once
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND ref_id <> %s",
            (rec.id, tick.id),
        ).fetchall()
        old = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (tick.id,)
        ).fetchone()
    assert len(rows) == 1
    meta = rows[0][0]
    assert meta["job_type"] == "briefing"
    assert meta["params"] == {"deliver_to": "conv:x"}
    assert meta["spawned_for_tick"] == "2026-10-12T07:00+00:00~retry1"
    assert meta["deliverable_attempt"] == 1
    assert old is not None
    assert DELIVERABLE_RETRY_AT not in old[0] and "deliverable_retried_at" in old[0]
