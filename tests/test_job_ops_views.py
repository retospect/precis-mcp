"""Tests for ``JobHandler``'s ``id='/limits'`` and ``id='/delta'`` ops
views (gr504488, gr504490).

``/limits`` is the read-only surface onto ``external_rate_limits``
(migration 0121) — the table agents used to detour to prod-psql for.
``/delta`` is the doctor tick's "only what changed" read, keyed on the
last succeeded doctor_tick's start. Rows are tagged with a uuid so
assertions survive the shared ``precis_test`` DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from precis.alerts import raise_alert
from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.job import JobHandler
from precis.store import Store, Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture
def gripe(hub: Hub) -> GripeHandler:
    return GripeHandler(hub=hub)


def _seed_limit(
    store: Store,
    provider: str,
    *,
    capacity: int = 4,
    refill: float = 1.0,
    tokens: float = 4,
    daily_cap: int | None = None,
    day_used: int = 0,
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM external_rate_limits WHERE provider = %s", (provider,)
        )
        conn.execute(
            "INSERT INTO external_rate_limits "
            "(provider, capacity, refill_per_sec, tokens, daily_cap, day_used) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (provider, capacity, refill, tokens, daily_cap, day_used),
        )
        conn.commit()


# ── /limits ─────────────────────────────────────────────────────────


def test_limits_lists_named_providers(jobs: JobHandler, store: Store) -> None:
    prov = f"test-{uuid4().hex[:8]}"
    _seed_limit(store, prov, capacity=4, tokens=4, daily_cap=1000, day_used=42)

    resp = jobs.get(id="/limits")

    assert "external_rate_limits" in resp.body
    assert f"{prov} tokens=4.0/4" in resp.body
    assert "day=42/1000" in resp.body


def test_limits_hides_host_rows_by_default(jobs: JobHandler, store: Store) -> None:
    host_row = f"host:test-{uuid4().hex[:8]}.example"
    _seed_limit(store, host_row)

    resp = jobs.get(id="/limits")
    assert host_row not in resp.body
    assert "host:<netloc> rows hidden" in resp.body

    resp_all = jobs.get(id="/limits?hosts=1")
    assert host_row in resp_all.body


def test_limits_provider_pin(jobs: JobHandler, store: Store) -> None:
    prov = f"test-{uuid4().hex[:8]}"
    other = f"host:test-{uuid4().hex[:8]}.example"
    _seed_limit(store, prov, capacity=2, tokens=0)
    _seed_limit(store, other)

    resp = jobs.get(id=f"/limits?provider={prov}")
    assert f"{prov} tokens=0.0/2" in resp.body
    assert other not in resp.body

    resp_host = jobs.get(id=f"/limits?provider={other}")
    assert other in resp_host.body


def test_limits_unknown_provider_is_empty(jobs: JobHandler) -> None:
    resp = jobs.get(id=f"/limits?provider=no-such-{uuid4().hex[:8]}")
    assert "no external_rate_limits row matches" in resp.body


# ── /delta ──────────────────────────────────────────────────────────


def test_delta_reports_new_alert(jobs: JobHandler, store: Store) -> None:
    fp = f"fp:{uuid4().hex[:8]}"
    aid, is_new = raise_alert(store, source="testsrc", fingerprint=fp, title="cond")
    assert is_new

    resp = jobs.get(id="/delta?since=1")

    assert "NEW ALERTS" in resp.body
    assert f"al{aid} testsrc/{fp}" in resp.body


def test_delta_splits_repeats_and_persistent(jobs: JobHandler, store: Store) -> None:
    tag = uuid4().hex[:8]
    fp_rep = f"fp:rep:{tag}"
    fp_persist = f"fp:persist:{tag}"
    rep_id, _ = raise_alert(store, source="s", fingerprint=fp_rep, title="r")
    pers_id, _ = raise_alert(store, source="s", fingerprint=fp_persist, title="p")

    with store.pool.connection() as conn:
        # rep: created long ago, re-sighted now (updated_at fresh).
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '3 days' WHERE ref_id = %s",
            (rep_id,),
        )
        # persist: open but untouched for days — nothing new this window.
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '3 days', "
            "updated_at = now() - interval '2 days' WHERE ref_id = %s",
            (pers_id,),
        )
        conn.commit()

    resp = jobs.get(id="/delta?since=1")

    new_section = resp.body.split("REPEAT SIGHTINGS")[0]
    assert f"al{rep_id}" not in new_section
    assert f"al{rep_id} s/{fp_rep}" in resp.body.split("REPEAT SIGHTINGS")[1]
    assert f"al{pers_id} s/{fp_persist}" in resp.body.split("PERSISTENT OPEN")[1]


def test_delta_covers_gripes_and_comments(
    jobs: JobHandler, store: Store, gripe: GripeHandler
) -> None:
    tag = uuid4().hex[:8]
    gripe.put(text=f"delta-gripe-{tag}: probe body")
    gr_id = int(gripe.store.list_refs(kind="gripe", limit=1)[0].id)

    gripe.put(id=gr_id, text=f"comment-{tag}")

    resp = jobs.get(id="/delta?since=1")
    assert f"delta-gripe-{tag}" in resp.body
    assert "NEW GRIPES" in resp.body
    assert f"gr{gr_id}" in resp.body.split("GRIPE COMMENTS")[1]


def test_delta_cutoff_from_last_doctor_tick(jobs: JobHandler, store: Store) -> None:
    """With a succeeded doctor_tick job present, the cutoff is its
    started_at — not the 8h fallback."""
    started = datetime.now(UTC) - timedelta(hours=6)
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"doctor_tick test {uuid4().hex[:8]}",
        meta={
            "job_type": "doctor_tick",
            "started_at": started.isoformat(),
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "succeeded"))

    resp = jobs.get(id="/delta")

    assert f"doctor_tick jo{ref.id} start" in resp.body
    assert started.strftime("%Y-%m-%dT%H") in resp.body


def test_delta_since_hours_override(jobs: JobHandler) -> None:
    resp = jobs.get(id="/delta?since=2")
    assert "since=2h" in resp.body


def test_delta_bad_since_raises(jobs: JobHandler) -> None:
    from precis.errors import BadInput

    with pytest.raises(BadInput):
        jobs.get(id="/delta?since=yesterday-ish")
