"""``doctor_tick`` scheduler cadence — mints one queued job per 8h window
(``docs/backlog/doctor-tick-report.md`` item 1).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from precis.store import Store
from precis.workers import scheduler


def _doctor_jobs(store: Store) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND meta->>'job_type' = 'doctor_tick' ORDER BY ref_id"
        ).fetchall()
    return [dict(r[0]) for r in rows]


class _FixedDatetime(datetime):
    _fixed: datetime

    @classmethod
    def now(cls, tz=None):
        return cls._fixed if tz is None else cls._fixed.astimezone(tz)


def _freeze(monkeypatch: pytest.MonkeyPatch, when: datetime) -> None:
    frozen = type("_Frozen", (_FixedDatetime,), {"_fixed": when})
    monkeypatch.setattr(scheduler, "datetime", frozen)


def test_window_token_buckets_by_8h_slice() -> None:
    assert scheduler._doctor_tick_window(datetime(2026, 8, 23, 0, 0, tzinfo=UTC)) == (
        "2026-08-23/0"
    )
    assert scheduler._doctor_tick_window(datetime(2026, 8, 23, 7, 59, tzinfo=UTC)) == (
        "2026-08-23/0"
    )
    assert scheduler._doctor_tick_window(datetime(2026, 8, 23, 8, 0, tzinfo=UTC)) == (
        "2026-08-23/1"
    )
    assert scheduler._doctor_tick_window(datetime(2026, 8, 23, 23, 59, tzinfo=UTC)) == (
        "2026-08-23/2"
    )


def test_mint_creates_exactly_one_queued_job(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, datetime(2026, 8, 23, 1, 0, tzinfo=UTC))

    scheduler._run_doctor_tick_mint(store, 32)

    jobs = _doctor_jobs(store)
    assert len(jobs) == 1
    assert jobs[0]["executor"] == "claude_inproc"
    assert jobs[0]["idem_key"] == "doctor:2026-08-23/0"


def test_mint_is_idempotent_within_the_same_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, datetime(2026, 8, 23, 1, 0, tzinfo=UTC))
    scheduler._run_doctor_tick_mint(store, 32)
    _freeze(monkeypatch, datetime(2026, 8, 23, 6, 0, tzinfo=UTC))  # still window 0

    scheduler._run_doctor_tick_mint(store, 32)

    assert len(_doctor_jobs(store)) == 1


def test_mint_fires_again_in_the_next_window(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _freeze(monkeypatch, datetime(2026, 8, 23, 1, 0, tzinfo=UTC))
    scheduler._run_doctor_tick_mint(store, 32)
    _freeze(monkeypatch, datetime(2026, 8, 23, 9, 0, tzinfo=UTC))  # window 1

    scheduler._run_doctor_tick_mint(store, 32)

    jobs = _doctor_jobs(store)
    assert len(jobs) == 2
    assert {j["idem_key"] for j in jobs} == {
        "doctor:2026-08-23/0",
        "doctor:2026-08-23/1",
    }


def test_cadence_row_is_registered_host_agnostic_and_free_to_mint() -> None:
    cad = next(c for c in scheduler.CADENCES if c.name == "doctor_tick")
    assert cad.interval_s == 8 * 3600
    assert cad.host_affinity is None
    assert cad.eligible is None
    assert cad.spends is False


def test_scheduler_pass_fires_the_real_doctor_tick_cadence(store: Store) -> None:
    """End-to-end through ``run_scheduler_pass`` (real cadence, real lease),
    not just the isolated mint function."""
    cad = next(c for c in scheduler.CADENCES if c.name == "doctor_tick")
    result = scheduler.run_scheduler_pass(store, host="h", cadences=(cad,))
    assert (result.claimed, result.ok, result.failed) == (1, 1, 0)
    assert len(_doctor_jobs(store)) == 1
