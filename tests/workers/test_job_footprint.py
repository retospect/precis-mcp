"""``meta.footprint`` on terminal STATUS (gr595536).

Every terminal ``STATUS:`` on a ``kind='job'`` ref stamps
``meta.footprint = {ru_maxrss_mb, wall_s, cpu_s}`` — written inside
``_stamp_job_completion`` so executor, sweeper and tag-verb paths all
land it. The measured fields come from
:mod:`precis.utils.job_footprint`'s execution-scoped registry:
``begin`` at claim, ``bind`` on the dispatch thread, ``fold`` at
leave-running, ``take`` at the terminal stamp. Children are measured
per-process via ``MeasuredPopen`` (``os.wait4`` rusage attributed to the
bound job), so concurrent jobs never count each other's CPU and a small
job never inherits an earlier job's RSS peak — the td611679 isolation
fixes ``TestIsolation`` pins.
"""

from __future__ import annotations

import subprocess
import sys
import threading
from datetime import UTC, datetime

import pytest

from precis.store.store import Store
from precis.utils import job_footprint
from precis.workers.executors import _common


@pytest.fixture(autouse=True)
def _clean_registry() -> None:
    job_footprint._reset()


def _seed_job(
    store: Store, *, started_at: str | None = None, meta: str | None = None
) -> int:
    if meta is None:
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


#: A child that burns real CPU (~1s of utime on a typical host).
_BURN_CHILD = "s = 0\nfor i in range(30_000_000):\n    s += i\n"

#: A child whose peak RSS dwarfs a trivial python -c (~400 MiB payload).
_FAT_CHILD = "import time\nb = bytearray(400 << 20)\ntime.sleep(0.3)\n"


def _measured_child(code: str = "pass") -> None:
    """Spawn a child through ``MeasuredPopen`` — the same harness
    ``run_claude`` uses for ``claude -p`` — so its rusage attributes to
    the job bound on this thread."""
    proc = job_footprint.MeasuredPopen(
        [sys.executable, "-c", code],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    proc.wait(timeout=60)
    assert proc.returncode == 0
    assert proc.rusage is not None


class TestFootprintStamp:
    def test_measured_fields_land_on_terminal(self, store: Store) -> None:
        ref_id = _seed_job(store, started_at=datetime.now(UTC).isoformat())
        job_footprint.begin(ref_id)
        job_footprint.bind(ref_id)
        _measured_child()
        _common.set_status(store, ref_id, "succeeded")
        job_footprint.unbind()

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
        job_footprint.bind(ref_id)
        _measured_child()
        _common.set_status(store, ref_id, "yielded")  # segment one closes
        assert ref_id in job_footprint._ACCUM

        job_footprint.begin(ref_id)  # re-claim opens segment two
        job_footprint.bind(ref_id)
        _common.set_status(store, ref_id, "succeeded")
        job_footprint.unbind()

        fp = _footprint(store, ref_id)
        assert fp["cpu_s"] >= 0
        assert fp["ru_maxrss_mb"] > 0

    def test_reterminalise_preserves_measured_fields(self, store: Store) -> None:
        """A second terminal stamp (sweeper re-tag) keeps the measured
        numbers rather than overwriting them with nulls."""
        ref_id = _seed_job(store, started_at=datetime.now(UTC).isoformat())
        job_footprint.begin(ref_id)
        job_footprint.bind(ref_id)
        _measured_child()
        _common.set_status(store, ref_id, "failed")
        job_footprint.unbind()
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

    def test_take_reports_measured_child(self) -> None:
        job_footprint.begin(7)
        job_footprint.bind(7)
        _measured_child()
        out = job_footprint.take(7)
        job_footprint.unbind()
        assert out["ru_maxrss_mb"] is not None and out["ru_maxrss_mb"] > 0
        assert out["cpu_s"] is not None and out["cpu_s"] >= 0

    def test_take_nulls_without_a_segment(self) -> None:
        assert job_footprint.take(99) == {"ru_maxrss_mb": None, "cpu_s": None}


class TestIsolation:
    """The td611679 fixes: measurements are per-job, never worker-wide."""

    def test_unbound_child_stays_unattributed(self) -> None:
        """A child reaped with no job bound must not land on a job —
        under RUSAGE_CHILDREN every reaped child counted."""
        job_footprint.begin(3)
        _measured_child(_BURN_CHILD)
        out = job_footprint.take(3)
        assert out["cpu_s"] is not None
        # The burn child's ~1s of CPU never landed: only this thread's
        # own (near-zero, it blocked in wait) delta did.
        assert out["cpu_s"] < 0.5

    def test_concurrent_jobs_do_not_share_cpu(self) -> None:
        """Two overlapping segments on different threads: B must not
        count the CPU A's bound child burned."""
        results: dict[int, dict[str, float | None]] = {}

        def run_job(ref_id: int, code: str) -> None:
            job_footprint.begin(ref_id)
            job_footprint.bind(ref_id)
            _measured_child(code)
            results[ref_id] = job_footprint.take(ref_id)
            job_footprint.unbind()

        t = threading.Thread(target=run_job, args=(1, _BURN_CHILD))
        t.start()
        run_job(2, "pass")
        t.join(timeout=60)

        assert results[1]["cpu_s"] is not None and results[1]["cpu_s"] > 0.3
        assert results[2]["cpu_s"] is not None
        assert results[2]["cpu_s"] < results[1]["cpu_s"]

    def test_small_job_does_not_inherit_an_earlier_peak(self) -> None:
        """B's RSS is B's own child's peak, not the max ever reaped —
        the lifetime-ru_maxrss read this replaced reported A's peak
        for B."""
        job_footprint.begin(1)
        job_footprint.bind(1)
        _measured_child(_FAT_CHILD)
        a = job_footprint.take(1)
        job_footprint.unbind()

        job_footprint.begin(2)
        job_footprint.bind(2)
        _measured_child("pass")
        b = job_footprint.take(2)
        job_footprint.unbind()

        assert a["ru_maxrss_mb"] is not None and a["ru_maxrss_mb"] > 300
        assert b["ru_maxrss_mb"] is not None
        assert b["ru_maxrss_mb"] < a["ru_maxrss_mb"] - 200

    def test_foreign_thread_fold_contributes_nothing(self) -> None:
        """A segment closed on a thread other than the one it baselined
        on measures nothing — another thread's counter is unreadable."""
        job_footprint.begin(4)
        job_footprint.bind(4)

        def close() -> None:
            job_footprint.fold(4)

        threading.Thread(target=close).start()
        acc = job_footprint._ACCUM.get(4)
        assert acc is None or not acc.measured_cpu


class TestFootprintSummary:
    """``Store.job_footprint_summary`` — the per-(job_type, meta.size)
    p50/p95 rollup the admission table derives from (gr595536)."""

    def _seed_measured(
        self,
        store: Store,
        *,
        job_type: str,
        rss_mb: float,
        cpu_s: float = 1.0,
        size_name: str | None = None,
        size_value: str | None = None,
        finished_at: str | None = None,
    ) -> None:
        size = (
            f', "size": {{"name": "{size_name}", "value": "{size_value}"}}'
            if size_name is not None
            else ""
        )
        meta = (
            f'{{"job_type": "{job_type}"{size}, '
            f'"footprint": {{"ru_maxrss_mb": {rss_mb}, "cpu_s": {cpu_s}, '
            f'"wall_s": 2.0}}'
            + (f', "finished_at": "{finished_at}"' if finished_at is not None else "")
            + "}"
        )
        _seed_job(store, meta=meta)

    def test_p50_p95_per_type_and_size(self, store: Store) -> None:
        for rss in (100.0, 200.0, 300.0, 400.0):
            self._seed_measured(
                store,
                job_type="embed_batch",
                rss_mb=rss,
                size_name="limit",
                size_value="2000",
            )
        self._seed_measured(store, job_type="fix_gripe", rss_mb=50.0)

        rows = store.job_footprint_summary()
        by_type = {r["job_type"]: r for r in rows}
        row = by_type["embed_batch"]
        assert row["n"] == 4
        assert row["size_name"] == "limit" and row["size_value"] == "2000"
        assert row["rss_p50_mb"] == pytest.approx(250.0)
        assert row["rss_p95_mb"] == pytest.approx(385.0)
        assert row["cpu_p50_s"] == pytest.approx(1.0)
        assert by_type["fix_gripe"]["n"] == 1
        # A job minted before gr595539's size stamp groups under a null
        # bucket rather than dropping out.
        assert by_type["fix_gripe"]["size_name"] is None

    def test_unmeasured_rows_drop_out(self, store: Store) -> None:
        """A footprint whose measured fields are null (never claimed,
        detached lane) is no sample for the percentile."""
        _seed_job(
            store,
            meta='{"job_type": "fix_gripe", "footprint": '
            '{"ru_maxrss_mb": null, "cpu_s": null, "wall_s": 0.1}}',
        )
        assert store.job_footprint_summary() == []

    def test_days_window(self, store: Store) -> None:
        self._seed_measured(store, job_type="embed_batch", rss_mb=100.0)
        # Rows without finished_at (or outside the window) drop out of a
        # bounded read; unbounded reads keep them.
        assert store.job_footprint_summary(days=7.0) == []
        assert len(store.job_footprint_summary()) == 1
