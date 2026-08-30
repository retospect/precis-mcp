"""The demand materializer (§F cycle a/b) — hysteresis + the
default-ON/opt-out cutover discipline.

Drives :func:`precis.workers.materialize.run_materialize_pass` directly
against a synthetic ``_BacklogSource`` (a controllable fake backlog count)
so these tests pin the mint/hysteresis LOGIC without needing a real
multi-thousand-chunk corpus. ``embed_batch``'s own drain behaviour is
covered in ``tests/workers/test_embed_batch.py``; the cadence WIRING
(``materialize`` in ``scheduler.CADENCES``) is covered in
``tests/test_scheduler_pass.py``.

§F cycle b flipped the module's own dark-ship gate to default-ON — the
autouse fixture below still pins ``PRECIS_MATERIALIZE_EMBED=1``
explicitly for every hysteresis/mint test in this file (belt-and-braces
against the default ever drifting back), but the cutover itself is
covered by the "default-ON, '0' is the opt-out" section.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

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
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = %s
            """,
            (job_type,),
        ).fetchone()
    return int(row[0]) if row else 0


def _fake_band_source(
    backlog: dict[str, int], *, pass_name: str = "classify"
) -> m._BacklogSource:
    """A SMALL-LLM derived-drain BAND source (``is_band=True``) over a
    controllable fake backlog count, always-enabled so the tests pin the band
    POLICY directly (not the per-source env flags)."""
    return m._BacklogSource(
        name=f"{pass_name}_band_fake",
        job_type="derived_drain",
        executor="job_inproc",
        count_fn=lambda _store: backlog["n"],
        batch_limit=m._DEFAULT_SMALL_DRAIN_LIMIT,
        # Use the REAL params builder so params.limit flows through
        # _small_drain_limit() — the same figure the band's capacity math reads.
        params_fn=m._small_drain_params(pass_name),
        enabled_fn=lambda: True,
        is_band=True,
        params_pass=pass_name,
    )


def _queued_derived(store: Store, pass_name: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT count(*) FROM refs r
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = 'derived_drain'
               AND r.meta->'params'->>'pass' = %s
            """,
            (pass_name,),
        ).fetchone()
    return int(row[0]) if row else 0


def _seed_derived(
    store: Store, pass_name: str, n: int, *, status: str = "queued", prio: int = 8
) -> None:
    """Insert ``n`` derived_drain jobs for ``pass_name`` at ``status`` directly
    (no idem_key, so they don't interfere with a same-tick mint)."""
    with store.pool.connection() as conn:
        for _ in range(n):
            ref = store.insert_ref(
                kind="job",
                slug=None,
                title="seed",
                meta={
                    "job_type": "derived_drain",
                    "executor": "job_inproc",
                    "params": {"pass": pass_name, "limit": 500},
                },
                prio=prio,
                conn=conn,
            )
            store.add_tag(
                ref.id,
                Tag.closed("STATUS", status),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
        conn.commit()


@pytest.fixture
def _small_band_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # Shrink the band so tests mint a handful, not 50.
    monkeypatch.setenv("PRECIS_SMALL_BAND_LOW", "2")
    monkeypatch.setenv("PRECIS_SMALL_BAND_HIGH", "5")


def _job_prios(store: Store, job_type: str) -> list[int | None]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT prio FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'job_type' = %s ORDER BY ref_id",
            (job_type,),
        ).fetchall()
    return [r[0] for r in rows]


def _derived_prios(
    store: Store, pass_name: str, *, status: str | None = None
) -> list[int | None]:
    """``derived_drain`` job prios for one ``params.pass``, optionally narrowed
    to one STATUS — the Finding-5 rebalance assertions need per-band, per-
    status visibility that :func:`_job_prios` (whole ``job_type``) doesn't
    give."""
    status_join = ""
    args: list[Any] = [pass_name]
    if status is not None:
        status_join = (
            "AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' AND t.value = %s)"
        )
        args.append(status)
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT prio FROM refs r
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = 'derived_drain'
               AND r.meta->'params'->>'pass' = %s
               {status_join}
             ORDER BY r.ref_id
            """,
            tuple(args),
        ).fetchall()
    return [r[0] for r in rows]


def _set_all_status(store: Store, job_type: str, status: str) -> None:
    with store.pool.connection() as conn:
        ids = [
            int(r[0])
            for r in conn.execute(
                "SELECT ref_id FROM refs WHERE kind = 'job' AND retired_at IS NULL "
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


# ── §F cycle b cutover: default-ON, "0" is the opt-out ───────────────────


def test_unset_flag_is_active_by_default(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§F cycle b: the materializer is now the standing drain — an unset
    ``PRECIS_MATERIALIZE_EMBED`` (the fleet default post-cutover) mints,
    same as the explicit ``"1"`` the autouse fixture sets for every other
    test in this file."""
    monkeypatch.delenv("PRECIS_MATERIALIZE_EMBED", raising=False)
    backlog = {"n": 999_999}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 4
    assert _queued_job_count(store, "fake_batch") == 4


def test_explicit_off_is_a_pure_noop(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_MATERIALIZE_EMBED=0`` is the documented opt-out/rollback —
    a byte-identical no-op regardless of how far over the backlog is."""
    monkeypatch.setenv("PRECIS_MATERIALIZE_EMBED", "0")
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


# ── backlog WARNING (poor-man's liveness until §D) ───────────────────────


def test_backlog_above_4x_high_logs_warning(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A backlog piled up to 4x HIGH despite mints already in flight is a
    liveness signal (embed_batch not claiming / the embedder unreachable)
    — one WARNING line, not a new alert kind."""
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH * 4 + 1}  # just over 4x HIGH
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        m.run_materialize_pass(store)

    assert any(
        "backlog" in rec.message and "not draining" in rec.message
        for rec in caplog.records
    )


def test_backlog_below_4x_high_is_silent(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Just above the mint threshold (HIGH) but well under the 4x
    liveness line — no WARNING, normal churn."""
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH + 1}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        m.run_materialize_pass(store)

    assert not any("not draining" in rec.message for rec in caplog.records)


def test_backlog_warning_fires_once_per_tick(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """One line per tick, not one per something-else — a single
    ``run_materialize_pass`` call over one source logs the WARNING
    exactly once even though the backlog stays far over 4x HIGH."""
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH * 5}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_source(backlog),))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        m.run_materialize_pass(store)

    warnings = [r for r in caplog.records if "not draining" in r.message]
    assert len(warnings) == 1


# ── SMALL-LLM derived-drain BAND policy (is_band=True) ───────────────────


def test_band_below_low_mints_toward_high(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """Live jobs below LOW with backlog present → top the band up to HIGH."""
    backlog = {"n": 999_999}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 5  # high(5) - live(0), backlog can feed all
    assert _queued_derived(store, "classify") == 5
    assert _job_prios(store, "derived_drain") == [8, 8, 8, 8, 8]


def test_band_at_low_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """Hysteresis: live already at/above LOW → no re-mint."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 2)  # == LOW
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 0
    assert _queued_derived(store, "classify") == 2  # unchanged


def test_band_empty_backlog_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """An empty queue mints nothing even below LOW — no zero-row jobs."""
    backlog = {"n": 0}
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 0
    assert _queued_derived(store, "classify") == 0


def test_band_backlog_caps_mint_below_high(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """Never mint more jobs than the backlog can feed (ceil(count/limit))."""
    backlog = {"n": 600}  # ceil(600/500) = 2, below HIGH-live (5)
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 2
    assert _queued_derived(store, "classify") == 2


def test_band_per_source_independent(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """A full classify band does NOT suppress the summarize band — each source
    counts its own live jobs via params_pass (the anti-starvation property)."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 5)  # classify band full (== HIGH)
    monkeypatch.setattr(
        m, "_BACKLOG_SOURCES", (_fake_band_source(backlog, pass_name="llm_summarize"),)
    )

    result = m.run_materialize_pass(store)

    assert result.claimed == 5  # summarize still mints its own band
    assert _queued_derived(store, "llm_summarize") == 5
    assert _queued_derived(store, "classify") == 5  # untouched


def test_band_failed_cooldown_is_per_pass(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """A freshly-failed classify job puts CLASSIFY's band in cooldown but not
    summarize's — failed-cooldown is discriminated by params_pass."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 1, status="failed")  # recent failure

    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))
    assert m.run_materialize_pass(store).claimed == 0  # classify in cooldown

    monkeypatch.setattr(
        m, "_BACKLOG_SOURCES", (_fake_band_source(backlog, pass_name="llm_summarize"),)
    )
    assert m.run_materialize_pass(store).claimed == 5  # summarize unaffected


def test_band_full_but_nothing_running_warns(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    _small_band_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Band full of QUEUED jobs that nothing is draining (dead melchior /
    target_node misconfig) while backlog remains → the stuck-queue WARNING (the
    band analogue of the high-water liveness signal)."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 5, status="queued")  # band full, 0 running
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        result = m.run_materialize_pass(store)

    assert result.claimed == 0  # band full → no mint
    assert any(
        "nothing is draining" in r.message and "classify_band_fake" in r.message
        for r in caplog.records
    )


def test_band_full_and_running_is_silent(
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    _small_band_env: None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Band full but jobs ARE running (normal steady drain) → no stuck WARNING."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 4, status="queued")
    _seed_derived(store, "classify", 1, status="running")  # something IS draining
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        m.run_materialize_pass(store)

    assert not any("nothing is draining" in r.message for r in caplog.records)


# ── Finding 5 — dynamic prio nudge for a stuck band ──────────────────────


def test_stuck_band_queued_rows_promoted_to_starved_prio(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """Band full of QUEUED jobs, 0 running (the stuck signature) → the
    band's queued rows are promoted to ``_STARVED_PRIO`` so they win the
    ``claim_executor_jobs`` ``ref_id ASC`` tiebreak over a continuously
    re-minted sibling band (Finding 5)."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 5, status="queued")  # band full, 0 running
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 0  # still no mint — this is purely a prio nudge
    assert _derived_prios(store, "classify") == [m._STARVED_PRIO] * 5


def test_stuck_band_prio_reverts_once_running(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """A band previously promoted (simulating a prior stuck tick) reverts its
    QUEUED rows to ``_MINT_PRIO`` the moment something is actually running —
    the self-correcting half of Finding 5's fix."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 4, status="queued", prio=m._STARVED_PRIO)
    _seed_derived(store, "classify", 1, status="running")  # something IS draining
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    m.run_materialize_pass(store)

    assert _derived_prios(store, "classify", status="queued") == [m._MINT_PRIO] * 4


def test_stuck_band_rebalance_scoped_to_one_band(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """The prio nudge for a stuck classify band leaves the sibling summarize
    band (same job_type, different params.pass) and an unrelated job_type
    (embed_batch) untouched."""
    backlog = {"n": 999_999}
    _seed_derived(store, "classify", 5, status="queued")  # stuck
    _seed_derived(store, "llm_summarize", 5, status="queued")  # sibling band
    with store.pool.connection() as conn:
        embed_ref = store.insert_ref(
            kind="job",
            slug=None,
            title="embed seed",
            meta={"job_type": "embed_batch", "executor": "job_inproc", "params": {}},
            prio=m._MINT_PRIO,
            conn=conn,
        )
        store.add_tag(
            embed_ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.commit()
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    m.run_materialize_pass(store)

    assert _derived_prios(store, "classify") == [m._STARVED_PRIO] * 5
    assert _derived_prios(store, "llm_summarize") == [m._MINT_PRIO] * 5
    assert _job_prios(store, "embed_batch") == [m._MINT_PRIO]


def test_drain_limit_env_scales_mint_capacity(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """PRECIS_SMALL_DRAIN_LIMIT feeds BOTH params.limit and the band's
    per-job capacity math — raising it mints FEWER jobs for the same backlog
    (each job drains more), never a desynced over-mint."""
    monkeypatch.setenv("PRECIS_SMALL_DRAIN_LIMIT", "1000")
    backlog = {"n": 1500}  # ceil(1500/1000) = 2 (was ceil(1500/500)=3 at default)
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (_fake_band_source(backlog),))

    result = m.run_materialize_pass(store)

    assert result.claimed == 2
    # and the minted jobs carry the raised per-job limit
    with store.pool.connection() as conn:
        limits = [
            int(r[0])
            for r in conn.execute(
                "SELECT (meta->'params'->>'limit')::int FROM refs "
                "WHERE kind='job' AND meta->>'job_type'='derived_drain' "
                "AND meta->'params'->>'pass'='classify'"
            ).fetchall()
        ]
    assert limits == [1000, 1000]


def test_band_source_disabled_mints_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch, _small_band_env: None
) -> None:
    """enabled_fn=False (the default-OFF dark-ship gate) → the source is skipped
    entirely regardless of backlog."""
    backlog = {"n": 999_999}
    src = m._BacklogSource(
        name="classify_band_off",
        job_type="derived_drain",
        executor="job_inproc",
        count_fn=lambda _s: backlog["n"],
        batch_limit=500,
        params_fn=lambda limit: {"pass": "classify", "limit": limit},
        enabled_fn=lambda: False,
        is_band=True,
        params_pass="classify",
    )
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (src,))

    assert m.run_materialize_pass(store).claimed == 0
    assert _queued_derived(store, "classify") == 0


def test_backlog_warning_reports_live_jobs_count(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The WARNING carries ``live_jobs=N`` so an operator can tell
    "stuck" (0 live — nothing is draining it) from "draining a big
    legacy backlog" (jobs minted and running, just behind) — without
    changing the fire condition (still purely count vs. 4x HIGH)."""
    backlog = {"n": m._DEFAULT_BACKLOG_HIGH * 5}
    src = _fake_source(backlog)
    monkeypatch.setattr(m, "_BACKLOG_SOURCES", (src,))

    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        first = m.run_materialize_pass(store)

    # First tick mints jobs (backlog above HIGH, nothing live yet) —
    # the WARNING fired on the SAME tick reports 0 live (queued jobs
    # aren't inserted until after the WARNING is logged).
    assert first.claimed > 0
    warning = next(r for r in caplog.records if "not draining" in r.message)
    assert "live_jobs=0" in warning.message

    caplog.clear()
    with caplog.at_level("WARNING", logger="precis.workers.materialize"):
        m.run_materialize_pass(store)

    # Second tick: the first tick's jobs are still queued/live — the
    # WARNING now reports a nonzero live_jobs (draining, not stuck).
    warning = next(r for r in caplog.records if "not draining" in r.message)
    assert "live_jobs=0" not in warning.message
