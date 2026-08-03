"""The demand materializer (§F cycle a) — hysteresis + dark-ship discipline.

Drives :func:`precis.workers.materialize.run_materialize_pass` directly
against a synthetic ``_BacklogSource`` (a controllable fake backlog count)
so these tests pin the mint/hysteresis LOGIC without needing a real
multi-thousand-chunk corpus. ``embed_batch``'s own drain behaviour is
covered in ``tests/workers/test_embed_batch.py``; the cadence WIRING
(``materialize`` in ``scheduler.CADENCES``) is covered in
``tests/test_scheduler_pass.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers import materialize as m

pytestmark = pytest.mark.db


def _fake_clock(monkeypatch: pytest.MonkeyPatch, *, start: float = 1_000_000.0) -> dict:
    """Pin ``materialize``'s wall clock to a controllable fake — the tick
    bucket (§F cycle a fix) is now DETERMINISTIC (``int(time.time()) //
    _TICK_BUCKET_S``), so a test that wants two calls to land in
    DIFFERENT cadence windows (the realistic "next 300s tick" case) must
    advance this fake between them; a test pinning both calls to the SAME
    value simulates the concurrent/repeated-invocation case the fix
    targets."""
    state = {"t": start}
    monkeypatch.setattr(m.time, "time", lambda: state["t"])
    return state


def _fake_source(
    store_backlog: dict[str, int],
    *,
    job_type: str = "fake_batch",
    batch_limit: int = 2000,
) -> m._BacklogSource:
    return m._BacklogSource(
        name="fake",
        job_type=job_type,
        executor="job_inproc",
        count_fn=lambda _store: store_backlog["n"],
        batch_limit=batch_limit,
        params_fn=lambda limit: {"limit": limit},
    )


def _queued_job_count(store: Store, job_type: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM refs r
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'job_type' = %s
            """,
            (job_type,),
        ).fetchone()
    return int(row[0]) if row else 0


def _job_prios(store: Store, job_type: str) -> list[int | None]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT prio FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND meta->>'job_type' = %s ORDER BY ref_id",
            (job_type,),
        ).fetchall()
    return [r[0] for r in rows]


def _set_all_status(store: Store, job_type: str, status: str) -> None:
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
                "AND meta->>'job_type' = %s",
                (job_type,),
            ).fetchall()
        ]
    for rid in ids:
        store.add_tag(
            rid, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
        )


@pytest.fixture(autouse=True)
def _enable_materialize(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MATERIALIZE_EMBED", "1")
    monkeypatch.delenv("PRECIS_EMBED_BACKLOG_HIGH", raising=False)
    monkeypatch.delenv("PRECIS_EMBED_BATCH_MAX_JOBS", raising=False)


# ── dark-ship discipline ─────────────────────────────────────────────────


def test_dark_flag_off_is_a_pure_noop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_MATERIALIZE_EMBED", raising=False)
    backlog = {"n": 999_999}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    result = m.run_materialize_pass(store)

    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert _queued_job_count(store, "fake_batch") == 0


# ── threshold ─────────────────────────────────────────────────────────────


def test_below_threshold_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH}  # exactly at HIGH, not above
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    result = m.run_materialize_pass(store)

    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)
    assert _queued_job_count(store, "fake_batch") == 0


def test_above_threshold_mints_bounded_batch_at_prio_8(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": 10_000}  # ceil(10000/2000) = 5, capped at max_jobs (4)
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 4
    assert _queued_job_count(store, "fake_batch") == 4
    assert _job_prios(store, "fake_batch") == [8, 8, 8, 8]


def test_mints_fewer_than_max_when_backlog_is_smaller(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH + 1}  # just above HIGH
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 1  # ceil(501/2000) = 1
    assert _queued_job_count(store, "fake_batch") == 1


# ── hysteresis: no re-mint while a batch is live ─────────────────────────


def test_second_tick_with_live_jobs_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": 10_000}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    first = m.run_materialize_pass(store)
    assert first.claimed == 4

    second = m.run_materialize_pass(store)

    assert second.claimed == 0
    assert _queued_job_count(store, "fake_batch") == 4  # unchanged


def test_drained_then_still_above_remints(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": 10_000}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))
    clock = _fake_clock(monkeypatch)

    first = m.run_materialize_pass(store)
    assert first.claimed == 4

    _set_all_status(store, "fake_batch", "succeeded")  # the batch drained
    clock["t"] += m._TICK_BUCKET_S  # the next cadence tick, a new window

    second = m.run_materialize_pass(store)

    assert second.claimed == 4  # still above threshold → re-mints
    assert _queued_job_count(store, "fake_batch") == 8  # 4 old + 4 new


# ── failed-job cooldown ──────────────────────────────────────────────────


def test_failed_job_cooldown_suppresses_remint(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": 10_000}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    first = m.run_materialize_pass(store)
    assert first.claimed == 4
    _set_all_status(store, "fake_batch", "failed")  # the batch just failed

    second = m.run_materialize_pass(store)

    assert second.claimed == 0  # cooldown suppresses the re-mint
    assert _queued_job_count(store, "fake_batch") == 4


def test_failed_job_outside_cooldown_remints(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    backlog = {"n": 10_000}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))
    clock = _fake_clock(monkeypatch)

    first = m.run_materialize_pass(store)
    assert first.claimed == 4
    _set_all_status(store, "fake_batch", "failed")
    stale = datetime.now(UTC) - timedelta(minutes=m._FAILED_COOLDOWN_MINUTES + 5)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = %s "
            "WHERE tag_id = (SELECT tag_id FROM tags WHERE namespace = 'STATUS' "
            "AND value = 'failed')",
            (stale,),
        )
        conn.commit()
    clock["t"] += m._TICK_BUCKET_S  # the next cadence tick, a new window

    second = m.run_materialize_pass(store)

    assert second.claimed == 4  # cooldown window has passed


# ── concurrent/repeated mint in the same tick bucket dedupes ────────────


def test_repeated_mint_in_same_tick_bucket_dedupes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A manual ``--only materialize`` run landing in the same 300s
    cadence window as another mint (or itself re-run) must NOT double the
    batch — the idem_key's ``tick`` is a deterministic wall-clock bucket,
    not a fresh nonce per call, so the existence check dedupes it."""
    backlog = {"n": 10_000}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))
    # Pin both calls to the SAME bucket regardless of real wall-clock drift
    # between them (the test itself takes non-zero time to run).
    _fake_clock(monkeypatch)

    first = m.run_materialize_pass(store)
    second = m.run_materialize_pass(store)

    assert first.claimed == 4
    assert second.claimed == 0  # same bucket → same idem_keys → deduped
    assert _queued_job_count(store, "fake_batch") == 4


# ── env overrides ─────────────────────────────────────────────────────────


def test_backlog_high_and_max_jobs_env_overrides(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PRECIS_EMBED_BACKLOG_HIGH", "50")
    monkeypatch.setenv("PRECIS_EMBED_BATCH_MAX_JOBS", "2")
    backlog = {"n": 60}  # above the overridden HIGH (50)
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog, batch_limit=10),))

    result = m.run_materialize_pass(store)

    # ceil(60/10) = 6, capped at overridden max_jobs (2)
    assert result.claimed == 2
    assert _queued_job_count(store, "fake_batch") == 2
