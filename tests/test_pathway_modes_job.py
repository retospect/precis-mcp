"""``autocatpath_modes``: normal modes for stored vib-priced pathways
(gr543723). Engine-free: the child's measurement is a stand-in here, its engine
half is in ``test_pathway_kinetics_sweep.py``."""

from __future__ import annotations

import sys
from typing import Any
from unittest.mock import MagicMock

import pytest

import precis_pathway
from precis.errors import BadInput
from precis.store import Tag
from precis_pathway import modes_job


def _rec(g: float, hnu: list[float]) -> dict[str, Any]:
    return {
        "g": g,
        "n_modes": len(hnu),
        "n_floored": 0,
        "n_imag": 0,
        "hnu": hnu,
        "hnu_imag": [0.0] * len(hnu),
        "hessian": [[1.0]],
    }


def _pathway(store: Any, slug: str = "pw-modes") -> Any:
    return store.insert_ref(
        kind="pathway",
        slug=slug,
        title=f"{slug} pathway",
        meta={
            "results": {
                "nodes": {"*": 0.0, "H": -0.5},
                "thermo": {
                    "mode": "vib",
                    "temperature": 298.15,
                    "state_corrections": {"*": 0.0, "H": 0.16},
                },
            }
        },
    )


def _seed_job(
    store: Any,
    pathway_id: int,
    *,
    status: str = "succeeded",
    structures: dict[str, Any] | None = None,
) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="seed",
        meta={
            "executor": "ssh_node",
            "job_type": "autocatpath_seed",
            "seed": 2,
            "model": "mace:medium",
            "model_index": 0,
            "params": {"pathway_ref_id": pathway_id, "config": {"name": "x"}},
            "structures": {
                "*": {"energy": -1.0, "extxyz": "bare"},
                "H": {"energy": -2.0, "extxyz": "with-h"},
            }
            if structures is None
            else structures,
            "partial": {
                "seed": 2,
                "vib": {"H": {"g": 0.16, "n_modes": 3, "n_floored": 0, "n_imag": 0}},
            },
        },
    )
    store.add_tag(ref.id, Tag.parse_strict(f"STATUS:{status}"), set_by="agent")
    return int(ref.id)


def test_seed_jobs_takes_succeeded_jobs_with_geometry(store) -> None:
    pathway = _pathway(store)
    keep = _seed_job(store, pathway.id)
    _seed_job(store, pathway.id, status="failed")
    _seed_job(store, pathway.id, structures={})
    other = _pathway(store, "pw-other")
    _seed_job(store, other.id)

    assert [job_id for job_id, _ in modes_job.seed_jobs(store, pathway.id)] == [keep]


def test_merge_keeps_the_stored_g_and_adds_the_whole_measurement() -> None:
    thermo = {"state_corrections": {"*": 0.0, "H": 0.16}}
    metas = {7: {"seed": 2, "model": "m", "partial": {"vib": {"H": {"g": 0.16}}}}}
    measured = [
        {
            "job_id": 7,
            "vib": {"*": _rec(0.0, []), "H": _rec(0.1605, [0.05, 0.1, 0.2])},
            "failed": {},
        }
    ]

    new_thermo, partials, check = modes_job.merge_measurements(thermo, metas, measured)

    h = partials[7]["vib"]["H"]
    assert h["g"] == 0.16 and h["g_remeasured"] == 0.1605
    assert h["hessian"] == [[1.0]] and h["hnu"] == [0.05, 0.1, 0.2]
    assert new_thermo["state_modes"]["H"] == [
        {"seed": 2, "model": "m", "hnu": [0.05, 0.1, 0.2], "hnu_imag": [0.0] * 3}
    ]
    assert new_thermo["state_modes"]["*"][0]["hnu"] == []
    assert check["n_over_tolerance"] == 0
    assert check["max_abs_dg_eV"] == pytest.approx(0.0005)
    assert check["states_without_modes"] == []


def test_merge_names_a_state_that_got_no_modes_and_a_g_that_disagrees() -> None:
    thermo = {"state_corrections": {"*": 0.0, "H": 0.16}}
    metas = {7: {"partial": {"vib": {"*": {"g": 0.5}}}}}
    measured = [
        {"job_id": 7, "vib": {"*": _rec(0.0, [])}, "failed": {"H": "RuntimeError: x"}}
    ]

    _, _, check = modes_job.merge_measurements(thermo, metas, measured)

    assert check["states_without_modes"] == ["H"]
    assert check["failed"] == {"H": ["job 7: RuntimeError: x"]}
    assert check["n_over_tolerance"] == 1 and check["worst_state"] == "*"


def _ctx(store: Any, pathway_id: int) -> MagicMock:
    ctx = MagicMock()
    ctx.store = store
    ctx.meta = {"params": {"pathway_ref_id": pathway_id}}
    return ctx


def _finished(monkeypatch: pytest.MonkeyPatch, units: list[dict[str, Any]]) -> None:
    from precis_pathway import seed_job

    envelope = {"units": units, "notes": ["n"]}
    fake = MagicMock()
    fake.poll_seed_partial_detached.return_value = {"state": "done", "result": envelope}
    monkeypatch.setitem(sys.modules, "precis_pathway.runner", fake)
    monkeypatch.setattr(precis_pathway, "runner", fake, raising=False)
    monkeypatch.setattr(seed_job, "_reap_scratch", lambda ctx, h, *, failed: None)


def test_poll_writes_the_seed_job_and_the_pathway(store, monkeypatch) -> None:
    pathway = _pathway(store)
    job_id = _seed_job(store, pathway.id)
    _finished(
        monkeypatch,
        [
            {
                "job_id": job_id,
                "vib": {"*": _rec(0.0, []), "H": _rec(0.16, [0.1, float("nan")])},
                "failed": {},
            }
        ],
    )
    ctx = _ctx(store, pathway.id)

    assert modes_job._poll(ctx, {"dir": "/tmp/x"}) is True

    ctx.set_status.assert_called_once_with("succeeded")
    stored = store.get_ref(kind="pathway", id=pathway.id).meta["results"]
    assert set(stored["thermo"]["state_modes"]) == {"*", "H"}
    assert stored["nodes"] == {"*": 0.0, "H": -0.5}  # the rest of the record stands
    vib = store.get_ref(kind="job", id=job_id).meta["partial"]["vib"]
    assert vib["H"]["hnu"] == [0.1, None]  # jsonb refuses NaN
    assert vib["H"]["g"] == 0.16


def test_poll_fails_when_a_state_is_left_without_modes(store, monkeypatch) -> None:
    pathway = _pathway(store)
    job_id = _seed_job(store, pathway.id)
    _finished(
        monkeypatch,
        [{"job_id": job_id, "vib": {"*": _rec(0.0, [])}, "failed": {"H": "boom"}}],
    )
    ctx = _ctx(store, pathway.id)

    assert modes_job._poll(ctx, {"dir": "/tmp/x"}) is True

    ctx.set_status.assert_not_called()
    assert "no modes for H" in ctx.record_failure.call_args.args[0]


def _handler(store: Any) -> Any:
    from types import SimpleNamespace

    from precis_pathway.handler import PathwayHandler

    h = PathwayHandler.__new__(PathwayHandler)
    setattr(h, "hub", SimpleNamespace(live_store=store))
    return h


def test_measure_modes_mode_mints_a_gpu_job(store, monkeypatch) -> None:
    import precis.handlers.job as job_mod

    minted: list[dict[str, Any]] = []

    class _Jobs:
        def __init__(self, hub: Any = None) -> None:
            pass

        def put(self, **kw: Any) -> Any:
            minted.append(kw)
            return MagicMock(body="created job id=9")

    monkeypatch.setattr(job_mod, "JobHandler", _Jobs)
    pathway = _pathway(store)
    _seed_job(store, pathway.id)

    resp = _handler(store).put(id=pathway.id, mode="measure_modes")

    assert "2 states lack modes" in resp.body
    assert minted[0]["job_type"] == "autocatpath_modes"
    assert minted[0]["parent_id"] == pathway.id
    assert minted[0]["requires"] == {"gpu": 1}


def test_measure_modes_refusals_name_the_next_step(store) -> None:
    pathway = _pathway(store)
    with pytest.raises(BadInput, match="no succeeded seed job") as e:
        _handler(store).put(id=pathway.id, mode="measure_modes")
    assert e.value.next

    table = store.insert_ref(
        kind="pathway",
        slug="pw-table",
        title="pw-table pathway",
        meta={"results": {"thermo": {"mode": "table"}}},
    )
    with pytest.raises(BadInput, match="not vib-priced"):
        _handler(store).put(id=table.id, mode="measure_modes")
    with pytest.raises(BadInput, match="needs the pathway handle"):
        _handler(store).put(mode="measure_modes")


def test_measure_modes_says_so_when_nothing_is_missing(store) -> None:
    done = store.insert_ref(
        kind="pathway",
        slug="pw-done",
        title="pw-done pathway",
        meta={
            "results": {
                "thermo": {
                    "mode": "vib",
                    "state_corrections": {"H": 0.1},
                    "state_modes": {"H": [{"hnu": [0.1]}]},
                }
            }
        },
    )
    resp = _handler(store).put(id=done.id, mode="measure_modes")
    assert "already has modes" in resp.body
