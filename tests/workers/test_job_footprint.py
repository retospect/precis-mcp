"""``meta.footprint`` on terminal STATUS (gr595536).

Every terminal ``STATUS:`` on a ``kind='job'`` ref stamps
``meta.footprint = {ru_maxrss_mb, wall_s, cpu_s}`` — written inside
``_stamp_job_completion`` so executor, sweeper and tag-verb paths all
land it. The measured fields come from
:mod:`precis.utils.job_footprint`'s process-local rusage registry:
``begin`` at claim, ``fold`` at leave-running, ``take`` at the terminal
stamp.
"""

from __future__ import annotations

import subprocess
import sys
from datetime import UTC, datetime

import pytest

from precis.store.store import Store
from precis.utils import job_footprint
from precis.workers.executors import _common


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    job_footprint._reset()


def _seed_job(store: Store, *, started_at: str | None = None) -> int:
    meta = f'{{"started_at": "{started_at}"}}' if started_at else "{}"
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, title, meta) "
            "VALUES ('job', 'fp-test', %s::jsonb) RETURNING ref_id",
            (meta,),
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def _footprint(store: Store, ref_id: int) -> dict:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'footprint' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0]) if row[0] is not None else {}


def _run_child() -> None:
    """Reap a real child so RUSAGE_CHILDREN advances inside the segment."""
    subprocess.run([sys.executable, "-c", "pass"], check=True)


class TestFootprintStamp:
    def test_measured_fields_land_on_terminal(self, store: Store) -> None:
        ref_id = _seed_job(store, started_at=datetime.now(UTC).isoformat())
        job_footprint.begin(ref_id)
        _run_child()
        _common.set_status(store, ref_id, "succeeded")

        fp = _footprint(store, ref_id)
        assert set(fp) == {"ru_maxrss_mb", "wall_s", "cpu_s"}
        assert fp["ru_maxrss_mb"] > 0
        assert fp["cpu_s"] >= 0
        assert fp["wall_s"] >= 0
        # The terminal stamp drains both registries for this ref.
        assert ref_id not in job_footprint._RUN
        assert ref_id not in job_footprint._ACCUM

    def test_nulls_when_process_never_claimed(self, store: Store) -> None:
        """A queued row cancelled before it ran stamps the shape with
        the unmeasurable fields null."""
        ref_id = _seed_job(store)
        _common.set_status(store, ref_id, "cancelled")

        fp = _footprint(store, ref_id)
        assert fp == {"ru_maxrss_mb": None, "wall_s": 0.0, "cpu_s": None}

    def test_segments_accumulate_across_yield(self, store: Store) -> None:
        """A yield/park folds the open segment; the terminal stamp sums
        both segments' cpu instead of only the last."""
        ref_id = _seed_job(store, started_at=datetime.now(UTC).isoformat())
        job_footprint.begin(ref_id)
        _run_child()
        _common.set_status(store, ref_id, "yielded")  # segment one closes
        assert ref_id in job_footprint._ACCUM

        job_footprint.begin(ref_id)  # re-claim opens segment two
        _common.set_status(store, ref_id, "succeeded")

        fp = _footprint(store, ref_id)
        assert fp["cpu_s"] >= 0
        assert fp["ru_maxrss_mb"] > 0

    def test_reterminalise_preserves_measured_fields(self, store: Store) -> None:
        """A second terminal stamp (sweeper re-tag) keeps the measured
        numbers rather than overwriting them with nulls."""
        ref_id = _seed_job(store, started_at=datetime.now(UTC).isoformat())
        job_footprint.begin(ref_id)
        _run_child()
        _common.set_status(store, ref_id, "failed")
        first = _footprint(store, ref_id)

        _common.set_status(store, ref_id, "failed")
        second = _footprint(store, ref_id)
        assert second["ru_maxrss_mb"] == first["ru_maxrss_mb"]
        assert second["cpu_s"] == first["cpu_s"]

    def test_non_job_ref_gets_no_footprint(self, store: Store) -> None:
        """The completion hook is kind='job'-gated: a paper taking a
        terminal STATUS never grows a footprint."""
        with store.pool.connection() as conn:
            row = conn.execute(
                "INSERT INTO refs (kind, title) VALUES ('paper', 'x') RETURNING ref_id"
            ).fetchone()
            assert row is not None
            ref_id = int(row[0])
        _common.set_status(store, ref_id, "failed")
        assert _footprint(store, ref_id) == {}


class TestRegistry:
    def test_begin_does_not_rebaseline_an_open_segment(self) -> None:
        """A same-process lease-expiry reclaim must not reset the
        baseline of a job that never stopped running."""
        job_footprint.begin(1)
        first = job_footprint._RUN[1]
        job_footprint.begin(1)
        assert job_footprint._RUN[1] is first

    def test_take_reports_child_scope_rss(self) -> None:
        job_footprint.begin(7)
        _run_child()
        out = job_footprint.take(7)
        assert out["ru_maxrss_mb"] is not None and out["ru_maxrss_mb"] > 0
        assert out["cpu_s"] is not None and out["cpu_s"] >= 0

    def test_take_nulls_without_a_segment(self) -> None:
        assert job_footprint.take(99) == {"ru_maxrss_mb": None, "cpu_s": None}
