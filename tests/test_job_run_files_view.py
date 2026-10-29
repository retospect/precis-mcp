"""gr524274: ``get(kind='job', id=N, view='run-files')`` — a finished
``placer='topo'`` pcb job's run artifacts (``meta.topo_run``) through
:meth:`Store.pcb_job_run_files`, not a hand-rolled ``refs.meta`` dig.

Covers the store accessor (topo_run present/absent/partial) and the job
view (listing, one-file content, the unknown-file refusal, the clear
empty answer, the SVG cap note).
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.store import Store

pytestmark = pytest.mark.db

_FILES: dict[str, Any] = {
    "manifest.json": {
        "schema": "precis.topo-run/1",
        "result": {"crossings": 2, "evals": 500, "length_mm": 42.5},
    },
    "elites.json": {"elites": [{"pose": [0.0, 0.0, 0.0, 0.0]}]},
    "pairs.json": {"stats": {"pairs": 1}, "pairs": []},
    "pins.json": {"locks": {}},
    "summary.txt": "topo-place — demo\n500 evals, 2 crossings\n",
    "placement.svg": '<svg xmlns="http://www.w3.org/2000/svg"></svg>',
}


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _insert_job(store: Store, meta: dict[str, Any]) -> int:
    ref = store.insert_ref(
        kind="job", slug=None, title="pcb_place demo", meta=dict(meta)
    )
    return int(ref.id)


def _insert_topo_job(store: Store) -> int:
    return _insert_job(
        store,
        {
            "executor": "job_inproc",
            "job_type": "pcb_place",
            "topo_run": {
                "files": dict(_FILES),
                "route": {"unrouted": 0, "median_len_mm": 12.5},
            },
        },
    )


def test_accessor_returns_run_files_summary_and_route(store: Store) -> None:
    job_id = _insert_topo_job(store)

    run = store.pcb_job_run_files(job_id)

    assert run is not None
    assert run["files"] == _FILES
    assert run["summary"] == _FILES["summary.txt"]
    assert run["route"] == {"unrouted": 0, "median_len_mm": 12.5}


def test_accessor_none_without_topo_run(store: Store) -> None:
    plain = _insert_job(store, {"executor": "job_inproc", "job_type": "pcb_place"})
    assert store.pcb_job_run_files(plain) is None
    # A missing ref and a non-job ref both read back None.
    assert store.pcb_job_run_files(999_999_999) is None
    other = store.insert_ref(kind="pcb", slug="rf-acc", title="board", meta={})
    assert store.pcb_job_run_files(int(other.id)) is None


def test_accessor_never_raises_on_missing_keys(store: Store) -> None:
    """A partial topo_run (files/route absent) reads back empty parts."""
    job_id = _insert_job(store, {"topo_run": {}})

    run = store.pcb_job_run_files(job_id)

    assert run is not None
    assert run == {"files": {}, "summary": "", "route": {}}


def test_run_files_view_lists_names_sizes_summary_route(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_topo_job(store)

    resp = jobs.get(id=job_id, view="run-files")

    assert resp.result_count == len(_FILES)
    body = resp.body
    for name in _FILES:
        assert name in body
    assert "chars (text)" in body and "chars (json)" in body
    assert _FILES["summary.txt"].strip() in body
    assert "## route meta" in body
    assert '"unrouted": 0' in body


def test_run_files_view_one_file_content(jobs: JobHandler, store: Store) -> None:
    job_id = _insert_topo_job(store)

    text = jobs.get(id=job_id, view="run-files", file="summary.txt")
    assert text.body == _FILES["summary.txt"]

    manifest = jobs.get(id=job_id, view="run-files", file="manifest.json")
    assert json.loads(manifest.body) == _FILES["manifest.json"]

    svg = jobs.get(id=job_id, view="run-files", file="placement.svg")
    assert svg.body == _FILES["placement.svg"]


def test_run_files_view_unknown_file_refuses_with_options(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_topo_job(store)

    with pytest.raises(BadInput) as excinfo:
        jobs.get(id=job_id, view="run-files", file="routes.gbr")

    assert excinfo.value.options == sorted(_FILES)
    assert excinfo.value.next == f"get(kind='job', id={job_id}, view='run-files')"


def test_run_files_view_clear_empty_answer_without_topo_run(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_job(store, {"executor": "job_inproc", "job_type": "pcb_place"})

    resp = jobs.get(id=job_id, view="run-files")

    assert resp.result_count == 0
    assert "no topo_run" in resp.body


def test_run_files_view_svg_capped_with_note(jobs: JobHandler, store: Store) -> None:
    big_svg = "<svg>" + "x" * 25_000 + "</svg>"
    job_id = _insert_job(
        store,
        {"topo_run": {"files": {"summary.txt": "s\n", "placement.svg": big_svg}}},
    )

    resp = jobs.get(id=job_id, view="run-files", file="placement.svg")

    assert resp.body.startswith("<svg>")
    assert len(resp.body) < len(big_svg)
    assert "[truncated" in resp.body
    listing = jobs.get(id=job_id, view="run-files")
    assert f"{len(big_svg):,} chars (text)" in listing.body
