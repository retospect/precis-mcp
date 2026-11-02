"""Real-PG regression tests for ``_pathway_run_jobs`` (gr549584).

The FakeStore suite can't parse SQL — the provenance strip's extended
run-jobs query (seed/host/wall/STATUS/summary per job, the page-level
"ran on" made per-job) is exercised here against the live ``store``
fixture, same posture as ``test_drive_sql.py``.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from precis.store import Tag
from precis.store.types import ChunkInsert
from precis_web.routes.refs import _pathway_run_jobs


def _job(store, ref_id, **meta):
    ref = store.insert_ref(kind="job", slug=None, title=f"job {ref_id}", meta=meta)
    return ref


def test_pathway_run_jobs_per_job_seed_host_status_summary(store) -> None:
    """Each chip carries its OWN seed number and claim host — the pathway
    pools several seeds, and they may have run on different machines
    (gr549584 comment 1: 549464 on pollux, 549466 on spark)."""
    pw = store.insert_ref(kind="pathway", slug="q1cand-x-rx-y", title="pw", meta={})
    seed0 = _job(
        store,
        0,
        pathway_ref=int(pw.id),
        job_type="autocatpath_seed",
        params={"seed": 0},
        lease_host="pollux",
        wall_s=107.4,
    )
    seed1 = _job(
        store,
        0,
        pathway_ref=int(pw.id),
        job_type="autocatpath_seed",
        params={"seed": 1},
        lease_host="spark",
        wall_s=322.1,
    )
    agg = _job(
        store,
        0,
        pathway_ref=int(pw.id),
        job_type="autocatpath_aggregate",
        lease_host="pollux",
        wall_s=40.0,
    )
    store.add_tag(int(seed1.id), Tag.parse_strict("STATUS:succeeded", kind="job"))
    store.chunks.insert_chunks(
        int(seed1.id),
        [
            ChunkInsert(
                ord=0,
                text="autocatpath_seed: seed=1 model=mace:medium: "
                "29 state(s), 8 warning(s).",
                meta={"chunk_kind": "job_summary"},
            )
        ],
    )
    # an unrelated job row (no pathway_ref) must not appear
    _job(store, 0, job_type="autocatpath_seed", params={"seed": 3})

    jobs = _pathway_run_jobs(store, int(pw.id))
    by_id = {j["id"]: j for j in jobs}
    assert set(by_id) == {int(seed0.id), int(seed1.id), int(agg.id)}
    assert by_id[int(seed0.id)]["label"] == "seed 0 on pollux"
    assert by_id[int(seed1.id)]["label"] == "seed 1 on spark"
    assert by_id[int(agg.id)]["label"] == "aggregate on pollux"
    assert "/refs/job/" in by_id[int(seed1.id)]["url"]
    t1 = by_id[int(seed1.id)]["title"]
    assert "succeeded" in t1
    assert "29 state(s), 8 warning(s)" in t1
    # insert_ref stamps a fresh job's own wall_s/finished_at (elapsed ~0) —
    # the exact figure is pinned by the fake-pool test instead; here we pin
    # that the per-job host text lands.
    assert "on spark" in t1
    assert "one seed" in t1
    assert "not a new simulation" in by_id[int(agg.id)]["title"]


def test_pathway_run_jobs_missing_fields_degrade(store) -> None:
    """A job row with no seed/host/status/summary still links — the chip
    just says less (never a 500 on a legacy or odd job row)."""
    pw = store.insert_ref(kind="pathway", slug="q1cand-a-rx-b", title="pw", meta={})
    job = _job(store, 0, pathway_ref=int(pw.id), job_type="autocatpath_explore")
    jobs = _pathway_run_jobs(store, int(pw.id))
    assert [j["id"] for j in jobs] == [int(job.id)]
    assert jobs[0]["label"] == "autocatpath_explore"
    assert "job #" in jobs[0]["title"]
