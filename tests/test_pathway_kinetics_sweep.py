"""kinetics v2 slice 4 acceptance — the autocatpath_kinetics job's
dispatch through the real store (D7/D8/D9).

These two import ``precis_pathway.runner`` (which imports the engine), so
the module skips without the ``catalyst`` extra; the engine-free glue —
the mint, the kinetics-sweep view, the harvest lift — lives in
``test_pathway_kinetics_glue.py``, which CI runs.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from tests._autocatpath import needs_engine

# precis_pathway imports autocatpath; CI's default env has no catalyst extra.
needs_engine()


def _sweep_payload() -> dict[str, Any]:
    """The REAL ``kinetics.sweep`` payload of the single-site fixture (see
    ``test_pathway_kinetics_glue._sweep_payload`` for the recipe)."""
    import json
    from pathlib import Path

    return json.loads(
        (Path(__file__).parent / "fixtures" / "kinetics_sweep.json").read_text(
            encoding="utf-8"
        )
    )


def test_kinetics_job_dispatch_runs_grid_and_persists(store, monkeypatch):
    """The dispatch runs the sweep subprocess and stamps
    kinetics_sweep + kinetics_v2 onto the pathway ref — through the real
    store, not a mock."""
    from precis.store import Store  # noqa: F401 - documents the fixture type
    from precis_pathway import kinetics_job

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway",
            slug="kin-sweep-dispatch",
            title="kin-sweep-dispatch pathway",
            meta={
                "results": {
                    "nodes": {"A": -1.0, "B": -0.5},
                    "edges": [{"a": "A", "b": "B"}],
                }
            },
            conn=conn,
        )
    payload = _sweep_payload()

    def _fake_sweep(config, res, *, timeout=None):
        res["kinetics_sweep"] = payload
        res["kinetics_v2"] = {
            "tof_mf": 9.1e-3,
            "drc": {"NO@N->NOH": 0.82},
            "orders": {"NO": 0.5, "NH3": -0.1},
        }

    import precis_pathway.runner as runner_mod

    solved: list[dict] = []

    def _fake_solve(config, artifact, *, timeout=None, tpd=None):
        solved.append(config)
        artifact["results_json"]["kinetics_v2"] = {"tof_mf": 4.2e-3}

    monkeypatch.setattr(runner_mod, "run_kinetics_subprocess", _fake_solve)
    monkeypatch.setattr(runner_mod, "run_kinetics_sweep_subprocess", _fake_sweep)
    monkeypatch.setattr(runner_mod, "has_tpd", lambda: False)  # the sweep path only
    cfg = {"name": "kin", "conditions": {"pressures": {"NO": 0.5}}}
    ctx = MagicMock()
    ctx.meta = {"params": {"pathway_ref_id": ref.id, "config": cfg}}
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)
    assert solved == [cfg]  # the re-solve ran under params.config

    got = store.get_ref(kind="pathway", id=ref.id)
    sw = got.meta["results"]["kinetics_sweep"]
    opt = payload["optimum"]
    assert sw["optimum"]["T"] == opt["T"]
    assert sw["optimum"]["value"] == opt["value"]
    assert sw["competing"]["pair"] == ["NO@N", "NOH"]
    assert len(sw["competing"]["points"]) == len(payload["competing"]["points"])
    assert got.meta["results"]["kinetics_v2"]["tof_mf"] == 9.1e-3


def test_kinetics_job_records_failure_on_sweep_error(store, monkeypatch):
    """A sweep error is saved onto the pathway AND fails the job — a
    failed sweep must not end as a success."""
    from precis_pathway import kinetics_job

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="pathway",
            slug="kin-sweep-error",
            title="kin-sweep-error pathway",
            meta={"results": {"nodes": {"A": -1.0}, "edges": [{"a": "A", "b": "A"}]}},
            conn=conn,
        )

    def _fail_sweep(config, res, *, timeout=None):
        res["kinetics_sweep_error"] = "kinetics sweep timed out"

    import precis_pathway.runner as runner_mod

    monkeypatch.setattr(runner_mod, "run_kinetics_sweep_subprocess", _fail_sweep)
    monkeypatch.setattr(runner_mod, "run_kinetics_subprocess", lambda *a, **k: None)
    monkeypatch.setattr(runner_mod, "has_tpd", lambda: False)  # the sweep path only
    ctx = MagicMock()
    ctx.meta = {
        "params": {
            "pathway_ref_id": ref.id,
            "config": {"conditions": {"pressures": {"NO": 0.5}}},
        }
    }
    ctx.store = store
    kinetics_job._dispatch(ctx, kinetics_job.SPEC)

    got = store.get_ref(kind="pathway", id=ref.id)
    assert got.meta["results"]["kinetics_sweep_error"] == "kinetics sweep timed out"
    ctx.record_failure.assert_called_once()


def test_detached_child_round_trips_the_kinetics_job_envelope(monkeypatch, tmp_path):
    """submit writes the request, the child entrypoint writes the
    ``{"ok": True, "result": {"results", "notes"}}`` envelope the seed
    protocol's poll reads."""
    import json

    import precis_pathway.runner as runner_mod
    from precis_pathway import kinetics_job

    spawned: list[list[str]] = []

    class _Proc:
        pid = 4242

    def _popen(cmd: list[str], **_kw: Any) -> _Proc:
        spawned.append(cmd)
        return _Proc()

    monkeypatch.setattr("subprocess.Popen", _popen)
    handle = runner_mod.submit_kinetics_job_detached(
        {"name": "kin"}, {"nodes": {"A": 0.0}, "edges": []}, work_dir=str(tmp_path)
    )
    assert handle["dir"] == str(tmp_path) and handle["pid"] == 4242
    assert spawned and spawned[0][-2:] == [
        str(tmp_path / "request.json"),
        str(tmp_path / "result.json"),
    ]
    req = json.loads((tmp_path / "request.json").read_text(encoding="utf-8"))
    assert req["mode"] == "kinetics_job" and req["config"] == {"name": "kin"}

    monkeypatch.setattr(
        kinetics_job,
        "run_compute",
        lambda cfg, inputs, **_kw: {
            "results": {"kinetics_v2": {"tof_mf": 1.0}},
            "notes": ["n"],
        },
    )
    rc = runner_mod._subprocess_main(
        ["x", str(tmp_path / "request.json"), str(tmp_path / "result.json")]
    )
    assert rc == 0
    status = runner_mod.poll_seed_partial_detached(handle, defer_cleanup=True)
    assert status["state"] == "done"
    assert status["result"] == {
        "results": {"kinetics_v2": {"tof_mf": 1.0}},
        "notes": ["n"],
    }


def test_stored_nodes_move_to_the_recompute_environment():
    """A node priced at 1 bar NO moves by kT ln p when the recompute states
    1e-3 bar, and not at all when the tables agree. A consumed gas gets less
    available, so the node rises."""
    import math
    from types import SimpleNamespace

    from autocatpath import kinetics
    from autocatpath.environment import Environment
    from autocatpath.ledger import Ledger

    from precis_pathway import runner

    T = 298.15
    cfg = SimpleNamespace(
        conditions=SimpleNamespace(temperature=T),
        env=lambda: Environment(T=T, inputs={"NO": 1e-3}, preset="far"),
    )
    ledgers = {"NO@N": Ledger(consumed={"NO": 1})}
    refs = {"gas": {"NO": -5.0}}
    g = {"*": 0.0, "NO@N": 0.2}
    stored = {"thermo": {"mode": "vib", "pressures": {}}}

    moved = runner._nodes_at_environment(kinetics, cfg, ledgers, refs, g, stored)

    assert moved["*"] == 0.0
    kt = kinetics.K_B_EV * T
    assert abs(moved["NO@N"] - (0.2 - kt * math.log(1e-3))) < 1e-9

    same = {"thermo": {"mode": "vib", "pressures": {"NO": 1e-3}}}
    kept = runner._nodes_at_environment(kinetics, cfg, ledgers, refs, g, same)
    assert kept["NO@N"] == 0.2
    none = {"thermo": {"mode": "none"}}
    assert runner._nodes_at_environment(kinetics, cfg, ledgers, refs, g, none) is g


def test_kinetics_child_gets_the_thermo_record(monkeypatch, tmp_path):
    """The solve child needs results.thermo to know what table the nodes were
    priced at: without it the recompute's move to the new table never ran."""
    from precis_pathway import runner

    seen: list[dict] = []

    def _capture(config, artifact, **kw):
        seen.append(dict(artifact["results_json"]))

    monkeypatch.setattr(runner, "run_kinetics", _capture)
    out = tmp_path / "out.json"
    req = {
        "config": {},
        "nodes": {"*": 0.0},
        "edges": [],
        "thermo": {"mode": "vib", "pressures": {}},
    }
    runner._kinetics_child(req, str(out))
    assert seen[0]["thermo"] == {"mode": "vib", "pressures": {}}


@pytest.fixture
def vib_run(monkeypatch):
    """Measured seed modes deliberately differ from the adsorbate table."""
    from autocatpath.config import Config
    from autocatpath.ledger import Ledger

    from precis_pathway import runner

    cfg = Config(name="vib-retemp")
    cfg.thermo.mode = "vib"
    cfg.conditions.temperature = 298.15
    cfg.kinetics.n_samples = 0
    net = SimpleNamespace(links=[], slab=lambda: None)
    ledgers = {"*": Ledger(), "NOH": Ledger()}
    refs: dict[str, dict[str, float]] = {"gas": {}, "mu": {}, "bare": {}}
    monkeypatch.setattr(runner, "_prep", lambda *a: cfg)
    monkeypatch.setattr("autocatpath.pipeline._build_net", lambda *a: net)
    monkeypatch.setattr("autocatpath.ledger.compute_ledgers", lambda *a: (ledgers, []))
    monkeypatch.setattr("autocatpath.pipeline.ledger_refs", lambda *a, **k: refs)
    results = {
        "nodes": {"*": 0.0, "NOH": 0.3},
        "edges": [],
        "thermo": {
            "mode": "vib",
            "temperature": 298.15,
            "freq_floor_meV": 12.0,
            "state_corrections": {"*": 0.0, "NOH": 0.3},
            "state_modes": {
                "*": [{"seed": 1, "hnu": []}],
                "NOH": [
                    {"seed": 1, "hnu": [0.005, 0.1, 0.2]},
                    {"seed": 2, "hnu": [0.006, 0.09, 0.21]},
                ],
            },
        },
    }
    return cfg, results, net, ledgers, refs


def _assert_vib_temperatures(temperatures: dict[float, float], results: dict) -> None:
    from autocatpath.thermo import harmonic_g

    runs = results["thermo"]["state_modes"]["NOH"]
    for temperature, correction in temperatures.items():
        shift = sum(
            harmonic_g(run["hnu"], temperature, 12.0)[0]
            - harmonic_g(run["hnu"], 298.15, 12.0)[0]
            for run in runs
        ) / len(runs)
        assert correction == pytest.approx(0.3 + shift, abs=1e-12)
    assert temperatures[298.15] == pytest.approx(0.3)
    assert temperatures[400.0] > temperatures[600.0] > temperatures[800.0]


def test_solve_retemps_the_measured_modes_not_the_table(vib_run, monkeypatch):
    """gr543723: the Ea stencil must receive the same modes as the sweep."""
    from autocatpath import kinetics, mkm
    from autocatpath.environment import Environment

    from precis_pathway import runner
    from precis_pathway.types import PathwayArtifact

    cfg, results, net, ledgers, refs = vib_run
    temperatures: dict[float, float] = {}

    def solve(cfg, nodes, edges, net, refs, *, thermo_record=None, **kw):
        assert thermo_record is results["thermo"]
        retemp = mkm.build_reT(cfg, net, ledgers, refs, set(nodes), thermo_record, "*")
        assert not retemp.vib_states
        for temperature in (298.15, 400.0, 600.0, 800.0):
            temperatures[temperature] = retemp.corr_at(
                Environment(T=temperature), "NOH"
            )
        return {"tof": 1.0}

    monkeypatch.setattr(kinetics, "solve", solve)
    artifact = cast("PathwayArtifact", {"results_json": results})
    runner.run_kinetics({}, artifact, budget_s=0)

    assert "kinetics_error" not in results
    _assert_vib_temperatures(temperatures, results)


def test_sweep_reprices_measured_modes_at_every_temperature(vib_run, monkeypatch):
    """gr543723: exercise the real sweep provider, stubbing only the solver."""
    from autocatpath import kinetics, kmc, mkm
    from autocatpath.environment import Environment

    from precis_pathway import runner

    _cfg, results, _net, _ledgers, _refs = vib_run
    temperatures: dict[float, float] = {}

    def transitions(g, edges, links, ledgers, refs, temperature, *a, **kw):
        temperatures[temperature] = g["NOH"]
        assert g["*"] == pytest.approx(0.0)
        return [], []

    def grid(*a, build_at, warnings, **kw):
        for temperature in (298.15, 400.0, 600.0, 800.0):
            build_at(Environment(T=temperature))
        return {"warnings": warnings, "points": []}

    monkeypatch.setattr(kinetics, "build_transitions", transitions)
    monkeypatch.setattr(mkm, "run_sweep", grid)
    monkeypatch.setattr(kmc, "sweep_points", lambda *a, **kw: [])
    runner._run_kinetics_sweep({}, results)

    assert "kinetics_sweep_error" not in results
    _assert_vib_temperatures(temperatures, results)


def test_modes_job_measures_stored_geometry_with_the_engine():
    """The modes child reads a stored extxyz and returns the whole measurement;
    a state it cannot read is reported, the others still run."""
    from ase.build import add_adsorbate, fcc111
    from ase.calculators.emt import EMT
    from autocatpath.thermo import harmonic_g

    from precis_pathway import runner

    slab = fcc111("Cu", size=(2, 2, 2), vacuum=8.0)
    n_slab = len(slab)
    add_adsorbate(slab, "O", 1.2, "fcc")
    slab.info["n_slab"] = n_slab
    cfg = MagicMock()
    cfg.conditions.temperature = 298.15
    cfg.thermo.freq_floor = 12.0

    vib, failed = runner.measure_unit_modes(
        {"structures": {"O": runner._atoms_to_extxyz(slab), "bad": "not extxyz"}},
        EMT(),
        cfg,
    )

    assert list(failed) == ["bad"]
    rec = vib["O"]
    assert rec["n_modes"] == 3 and len(rec["hessian"]) == 3
    assert harmonic_g(rec["hnu"], 298.15, 12.0)[0] == rec["g"]


def test_recompute_kinetics_validates_environment_and_objective_with_the_engine():
    """A bad environment/objective is refused at put time, naming the key —
    not as a failed job later."""
    from types import SimpleNamespace

    import pytest

    from precis.errors import BadInput
    from precis_pathway.handler import PathwayHandler

    ref = SimpleNamespace(
        id=11,
        slug="pw-a",
        meta={"config": {"name": "x"}, "results": {"nodes": {"A": 0}, "edges": [1]}},
    )
    h = PathwayHandler.__new__(PathwayHandler)
    setattr(
        h, "hub", SimpleNamespace(live_store=SimpleNamespace(get_ref=lambda **k: ref))
    )
    with pytest.raises(BadInput, match="refused") as e:
        h.put(
            id="pw-a",
            mode="recompute_kinetics",
            args={"objective": {"maximise": "tof", "bogus": 1}},
        )
    assert "objective" in str(e.value.next)
    with pytest.raises(BadInput, match="refused"):
        h.put(id="pw-a", mode="recompute_kinetics", args={"environment": {"T": "hot"}})
