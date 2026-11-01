"""gr540872: a coverage scan's per-point geometry as ``scan_geometry``
chunks (extxyz in ``chunk_blobs``) — stored append-only, kept out of the
embed / summarize / keyword queues, never inlined on the job card, and
read through ``get(kind='job', view='geometry')``.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store, scan_geometry
from precis.workers import chunk_keywords, llm_summarize
from precis.workers.embed import EmbedHandler, unembedded_chunk_count
from precis.workers.summarize import RakeLemmaHandler

pytestmark = pytest.mark.db

_XYZ_OK = '1\nProperties=species:S:1:pos:R:3 energy=-1.5 pbc="F F F"\nO 0.1 0.2 0.3\n'
_XYZ_BAD = '1\nProperties=species:S:1:pos:R:3 energy=-1.2 pbc="F F F"\nO 0.9 0.8 0.7\n'
_FRAMES = _XYZ_BAD * 3


def _records() -> list[dict[str, Any]]:
    return [
        {"id": "(111):O:1", "facet": "(111)", "adsorbate": "O", "n": 1,
         "failed": False, "final": _XYZ_OK, "frames": None},
        {"id": "(111):O:4", "facet": "(111)", "adsorbate": "O", "n": 4,
         "failed": True, "final": _XYZ_BAD, "frames": _FRAMES,
         "n_frames_total": 137},
    ]  # fmt: skip


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _job(store: Store) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="surface_coverage_scan demo",
        meta={"executor": "ssh_node", "job_type": "surface_coverage_scan"},
    )
    return int(ref.id)


def test_store_writes_a_final_per_point_and_frames_per_failed_point(
    store: Store,
) -> None:
    job = _job(store)
    assert scan_geometry.store_geometries(store, job, _records()) == 3
    points = scan_geometry.list_points(store, job)
    assert [p["point"] for p in points] == ["(111):O:1", "(111):O:4"]
    ok, bad = points
    assert not ok["failed"] and set(ok["parts"]) == {"final"}
    assert bad["failed"] and set(bad["parts"]) == {"final", "frames"}
    assert bad["n_frames_total"] == 137
    assert scan_geometry.fetch_point(store, job, "(111):O:1") == _XYZ_OK
    assert scan_geometry.fetch_point(store, job, "(111):O:4", "frames") == _FRAMES
    assert scan_geometry.fetch_point(store, job, "(111):O:1", "frames") is None


def test_chunk_text_is_a_float_free_summary_and_no_index(store: Store) -> None:
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text, meta FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (job, scan_geometry.KIND),
        ).fetchall()
    assert len(rows) == 3
    for text, meta in rows:
        assert "0." not in text and "-1." not in text, text
        assert meta["no_index"] == "true"


def test_append_only_rerun_adds_chunks_and_the_newest_wins(store: Store) -> None:
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    rerun = [dict(_records()[0], final=_XYZ_BAD)]
    scan_geometry.store_geometries(store, job, rerun)
    with store.pool.connection() as conn:
        (n,) = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND chunk_kind = %s",
            (job, scan_geometry.KIND),
        ).fetchone()
    assert n == 4  # nothing updated in place
    assert scan_geometry.fetch_point(store, job, "(111):O:1") == _XYZ_BAD


def test_scan_geometry_is_skipped_by_every_derivation_queue(store: Store) -> None:
    """Condition 1: no embedding, keyword or summary pass ever claims them."""
    assert scan_geometry.KIND in EmbedHandler.skip_chunk_kinds
    assert scan_geometry.KIND in RakeLemmaHandler.skip_chunk_kinds
    assert scan_geometry.KIND in chunk_keywords.SKIP_KINDS
    assert scan_geometry.KIND in llm_summarize.SKIP_KINDS
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    with store.pool.connection() as conn:
        assert unembedded_chunk_count(conn, ref_id=job) == 0


def test_plain_job_get_never_inlines_the_geometry(
    store: Store, jobs: JobHandler
) -> None:
    """Condition 2: the default card shows no extxyz and no summary line."""
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    body = jobs.get(id=job).body
    assert "Properties=species" not in body
    assert "scan geometry (111)" not in body


def test_geometry_view_lists_points_and_fetches_one(
    store: Store, jobs: JobHandler
) -> None:
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    listing = jobs.get(id=job, view="geometry")
    assert listing.result_count == 2
    assert "2 point(s), 1 failed" in listing.body
    assert "(111):O:4 — O n=4 FAILED" in listing.body
    assert "Properties=species" not in listing.body
    one = jobs.get(id=job, view="geometry", point="(111):O:1")
    assert one.body == _XYZ_OK
    frames = jobs.get(id=job, view="geometry", point="(111):O:4", frames=True)
    assert frames.body == _FRAMES


def test_geometry_view_refusals(store: Store, jobs: JobHandler) -> None:
    job = _job(store)
    scan_geometry.store_geometries(store, job, _records())
    with pytest.raises(BadInput, match="no scan point"):
        jobs.get(id=job, view="geometry", point="(100):O:1")
    with pytest.raises(BadInput, match="no frames geometry"):
        jobs.get(id=job, view="geometry", point="(111):O:1", frames=True)
    with pytest.raises(BadInput, match="only apply to view='geometry'"):
        jobs.get(id=job, point="(111):O:1")
    empty = _job(store)
    assert jobs.get(id=empty, view="geometry").result_count == 0
