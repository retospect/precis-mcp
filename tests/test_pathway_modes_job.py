"""``autocatpath_modes``: normal modes for stored vib-priced pathways
(gr543723). Engine-free: the child's measurement is a stand-in here, its engine
half is in ``test_pathway_kinetics_sweep.py``."""

from __future__ import annotations

import sys
from collections.abc import Callable
from types import ModuleType, SimpleNamespace
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


#: torch's two CUDA out-of-memory texts: from the driver while the model is
#: moved onto the device (the failure on gr592970), and from an allocation.
_OOM_AT_LOAD = (
    "CUDA error: out of memory\n"
    "For more detailed error information, run with CUDA_LOG_FILE=stderr"
)
_OOM_AT_MEASURE = "CUDA out of memory. Tried to allocate 2.00 GiB."


def _compute_unit(job_id: int) -> dict[str, Any]:
    """One seed unit as ``_unit`` hands it to the child: a bare slab and the
    slab with one adsorbed atom, on a config that asks for the GPU."""
    from ase import Atoms

    from precis_pathway import runner

    structures = {}
    for name, n_ads in (("*", 0), ("H", 1)):
        atoms = Atoms(
            "Cu" + "H" * n_ads,
            positions=[(0.0, 0.0, 1.5 * i) for i in range(1 + n_ads)],
            cell=[5.0, 5.0, 5.0],
            pbc=True,
        )
        atoms.info["n_slab"] = 1
        structures[name] = runner._atoms_to_extxyz(atoms)
    return {
        "job_id": job_id,
        "seed": job_id,
        "model": "mace:medium",
        "model_index": 0,
        "config": {"mlip": {"device": "cuda"}},
        "force_backend": None,
        "structures": structures,
    }


def _engine(
    monkeypatch: pytest.MonkeyPatch,
    *,
    load: Callable[[str], None] = lambda device: None,
    measure: Callable[[str, str], None] = lambda state, device: None,
) -> tuple[list[str], list[tuple[str, str]]]:
    """Stand-in engine under the real ``run_compute``: a calculator is the
    device it was loaded on. ``load(device)`` and ``measure(state, device)``
    raise to fail that step. Returns the devices loaded and the
    ``(state, device)`` pairs measured, in order."""
    from precis_pathway import runner

    loads: list[str] = []
    measured: list[tuple[str, str]] = []

    def _prep(config: dict[str, Any], force_backend: str | None) -> Any:
        mlip = SimpleNamespace(
            backend="mace", model="medium", models=[], device=config["mlip"]["device"]
        )
        mlip.specs = lambda: [(mlip.backend, mlip.model)]
        return SimpleNamespace(
            mlip=mlip,
            conditions=SimpleNamespace(temperature=298.15),
            thermo=SimpleNamespace(freq_floor=12.0),
        )

    def make_calculator(mlip: Any) -> str:
        loads.append(mlip.device)
        load(mlip.device)
        return str(mlip.device)

    def vib_correction(
        atoms: Any, calc: str, n_slab: int, temperature: float, freq_floor: float
    ) -> dict[str, Any]:
        state = "H" if len(atoms) > n_slab else "*"
        measure(state, calc)
        measured.append((state, calc))
        return _rec(0.16, [0.1] * 3 * (len(atoms) - n_slab))

    calc_mod: Any = ModuleType("autocatpath.calculators")
    calc_mod.make_calculator = make_calculator
    calc_mod.resolve_backend = lambda backend: backend
    thermo_mod: Any = ModuleType("autocatpath.thermo")
    thermo_mod.vib_correction = vib_correction
    if "autocatpath" not in sys.modules:
        monkeypatch.setitem(sys.modules, "autocatpath", ModuleType("autocatpath"))
    monkeypatch.setitem(sys.modules, "autocatpath.calculators", calc_mod)
    monkeypatch.setitem(sys.modules, "autocatpath.thermo", thermo_mod)
    monkeypatch.setattr(runner, "_prep", _prep)
    return loads, measured


def test_run_compute_loads_on_cpu_after_the_gpu_runs_out_of_memory(monkeypatch) -> None:
    """gr592970: the model did not fit on the GPU, so the whole job failed. The
    modes are small: the job loads the model once on cpu and measures there."""

    def load(device: str) -> None:
        if device != "cpu":
            raise RuntimeError(_OOM_AT_LOAD)

    loads, measured = _engine(monkeypatch, load=load)

    out = modes_job.run_compute([_compute_unit(7), _compute_unit(8)])

    assert [(u["job_id"], set(u["vib"]), u["failed"]) for u in out["units"]] == [
        (7, {"*", "H"}, {}),
        (8, {"*", "H"}, {}),
    ]
    assert loads == ["cuda", "cpu"]
    assert {device for _, device in measured} == {"cpu"}
    assert any("out of memory" in n and "on cpu" in n for n in out["notes"])


def test_run_compute_remeasures_on_cpu_a_state_that_ran_out_of_gpu_memory(
    monkeypatch,
) -> None:
    def measure(state: str, device: str) -> None:
        if state == "H" and device != "cpu":
            raise RuntimeError(_OOM_AT_MEASURE)

    _, measured = _engine(monkeypatch, measure=measure)

    out = modes_job.run_compute([_compute_unit(7)])

    assert set(out["units"][0]["vib"]) == {"*", "H"}
    assert out["units"][0]["failed"] == {}
    assert measured == [("*", "cuda"), ("H", "cpu")]
    assert any("out of memory" in n and "H on cpu" in n for n in out["notes"])


def test_run_compute_fails_on_a_second_oom_and_on_any_other_load_error(
    monkeypatch,
) -> None:
    def full(device: str) -> None:
        raise RuntimeError(_OOM_AT_LOAD)

    loads, _ = _engine(monkeypatch, load=full)
    with pytest.raises(RuntimeError, match="out of memory"):
        modes_job.run_compute([_compute_unit(7)])
    assert loads == ["cuda", "cpu"]

    def missing(device: str) -> None:
        raise RuntimeError("backend 'mace' is not installed")

    loads, _ = _engine(monkeypatch, load=missing)
    with pytest.raises(RuntimeError, match="not installed"):
        modes_job.run_compute([_compute_unit(7)])
    assert loads == ["cuda"]


def test_run_compute_keeps_a_state_failed_when_cpu_does_not_measure_it(
    monkeypatch,
) -> None:
    def full(state: str, device: str) -> None:
        if state == "H":
            raise RuntimeError(_OOM_AT_MEASURE)

    def no_cpu(device: str) -> None:
        if device == "cpu":
            raise MemoryError("no room")

    def broken(state: str, device: str) -> None:
        if state == "H":
            raise ValueError("bad geometry")

    # out of memory on cpu too; no cpu calculator; not a memory error at all
    for load, measure, reason, loaded in (
        (lambda device: None, full, "out of memory", ["cuda", "cpu"]),
        (no_cpu, full, "out of memory", ["cuda", "cpu"]),
        (lambda device: None, broken, "ValueError: bad geometry", ["cuda"]),
    ):
        loads, measured = _engine(monkeypatch, load=load, measure=measure)

        unit = modes_job.run_compute([_compute_unit(7)])["units"][0]

        assert set(unit["vib"]) == {"*"} and reason in unit["failed"]["H"]
        assert measured == [("*", "cuda")] and loads == loaded
