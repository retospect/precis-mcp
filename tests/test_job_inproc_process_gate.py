"""Per-PROCESS capability gate for ``job_inproc`` (gr623024).

``resource_slots`` is keyed per HOST: one worker process with
``PRECIS_SLICE_ROOT`` makes the host advertise ``repo_checkout``, so a
checkout-less ``job_inproc`` process beside it passed the host-level claim
gate, claimed ``release_poll`` and failed closed in ``slice_root()``. The
claiming process now probes its own checkout and excludes the job_types that
need it in the claim SQL (the row stays queued), and ``release_poll`` itself
parks instead of finishing "blocked" when it still lands on such a process.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers import capability_probe
from precis.workers.executors import _common, job_inproc
from precis.workers.executors._common import claim_executor_jobs
from precis.workers.job_types import run_slice

pytestmark = pytest.mark.db

_HOST = "shared_host"
#: The cached function itself, held before any test stubs the module name.
_PROCESS_CACHE = job_inproc.process_unclaimable_job_types


@pytest.fixture(autouse=True)
def _fresh_process_cache() -> Any:
    """The per-process cache must not leak between probe stubs."""
    _PROCESS_CACHE.cache_clear()
    yield
    _PROCESS_CACHE.cache_clear()


def _advertise_checkout(store: Store, host: str) -> None:
    """The host row a sibling worker with a checkout produces."""
    store.sync_host_resource_slots(host, {"repo_checkout": 1})
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host,),
        )
        conn.commit()


def _queue(store: Store, job_type: str, params: dict[str, object]) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=job_type,
        meta={"job_type": job_type, "executor": "job_inproc", "params": params},
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _claim_like_the_pass(store: Store) -> list[int]:
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="job_inproc",
            limit=10,
            reserve_host_id=_HOST,
            check_job_type_requires=True,
            exclude_job_types=job_inproc.process_unclaimable_job_types(),
        )
        conn.commit()
    return [int(r[0]) for r in rows]


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def test_checkoutless_process_leaves_release_poll_queued(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capability_probe, "_probe_repo_checkout", lambda: 0)
    _advertise_checkout(store, _HOST)
    poll = _queue(store, "release_poll", {})
    other = _queue(store, "paper_pull", {"ref_id": 1, "url": "https://x/y.pdf"})

    assert "release_poll" in job_inproc.process_unclaimable_job_types()
    assert "paper_pull" not in job_inproc.process_unclaimable_job_types()

    claimed = _claim_like_the_pass(store)

    assert claimed == [other]
    assert _status(store, poll) == "queued"
    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (poll,)
        ).fetchone()
    assert meta is not None
    assert not (meta[0] or {}).get("lease_until")


def test_checkout_process_claims_release_poll(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(capability_probe, "_probe_repo_checkout", lambda: 1)
    _advertise_checkout(store, _HOST)
    poll = _queue(store, "release_poll", {})

    assert job_inproc.process_unclaimable_job_types() == frozenset()
    assert _claim_like_the_pass(store) == [poll]


def test_unknown_probe_excludes_nothing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``None`` (git timed out) is unknown, not absent: leave the row."""
    monkeypatch.setattr(capability_probe, "_probe_repo_checkout", lambda: None)
    assert job_inproc.process_unclaimable_job_types() == frozenset()


def test_pass_passes_the_process_exclusion_to_the_claim(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through ``run_job_inproc_pass``: nothing is claimed."""
    monkeypatch.setattr(capability_probe, "_probe_repo_checkout", lambda: 0)
    monkeypatch.setattr(_common, "reserve_host", lambda: _HOST)
    _advertise_checkout(store, _HOST)
    poll = _queue(store, "release_poll", {})

    result = job_inproc.run_job_inproc_pass(store, limit=1)

    assert result == {"claimed": 0, "ok": 0, "failed": 0}
    assert _status(store, poll) == "queued"


def test_release_poll_landing_on_a_checkoutless_process_defers(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The backstop: a claim that still lands (stale cache, reclaim) ends
    parked for a capable process, never succeeded with a blocked summary."""
    monkeypatch.setattr(
        job_inproc, "process_unclaimable_job_types", lambda: frozenset()
    )
    monkeypatch.setattr(_common, "reserve_host", lambda: _HOST)
    _advertise_checkout(store, _HOST)

    def _no_root() -> Any:
        raise RuntimeError("no git checkout around here")

    monkeypatch.setattr(run_slice, "slice_root", _no_root)
    poll = _queue(store, "release_poll", {})

    result = job_inproc.run_job_inproc_pass(store, limit=1)

    assert result["claimed"] == 1
    status = _status(store, poll)
    assert status == "waiting_time"
    assert status != "succeeded"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (poll,)
        ).fetchone()
        summaries = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s "
            "AND chunk_kind = 'job_summary'",
            (poll,),
        ).fetchone()
    assert row is not None and summaries is not None
    meta = row[0]
    assert meta["placement_parks"] == 1
    assert meta["wake_when"]["kind"] == "at_time"
    assert summaries[0] == 0  # no "blocked" summary written
