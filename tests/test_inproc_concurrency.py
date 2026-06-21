"""claude_inproc executor concurrency knob (PRECIS_INPROC_CONCURRENCY)."""

from __future__ import annotations

import threading

from precis.store.store import Store
from precis.workers.executors import claude_inproc as ci


def test_concurrency_env_parsed_and_clamped(monkeypatch) -> None:
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "8")
    assert ci._inproc_concurrency() == 8
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "99")
    assert ci._inproc_concurrency() == 16  # clamp high
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "0")
    assert ci._inproc_concurrency() == 1  # clamp low
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "nope")
    assert ci._inproc_concurrency() == 1  # garbage → default
    monkeypatch.delenv("PRECIS_INPROC_CONCURRENCY", raising=False)
    assert ci._inproc_concurrency() == 1  # default


def test_parallel_path_runs_every_claimed_job(monkeypatch, store: Store) -> None:
    """With concurrency>1 the whole claimed batch runs (in a thread pool),
    not just the first — every job's _run_one fires exactly once."""
    rows = [(i, f"job{i}", {"job_type": "plan_tick"}) for i in (101, 102, 103)]
    monkeypatch.setattr(ci, "_claim_jobs", lambda conn, *, limit: list(rows))
    monkeypatch.setattr(ci, "_set_status", lambda *a, **k: None)
    seen: set[int] = set()
    lock = threading.Lock()

    def _fake_run_one(store_, ref_id, title, meta):
        with lock:
            seen.add(ref_id)

    monkeypatch.setattr(ci, "_run_one", _fake_run_one)
    monkeypatch.setenv("PRECIS_INPROC_CONCURRENCY", "3")

    res = ci.run_claude_inproc_pass(store, limit=4)
    assert seen == {101, 102, 103}
    assert res == {"claimed": 3, "ok": 3, "failed": 0}
