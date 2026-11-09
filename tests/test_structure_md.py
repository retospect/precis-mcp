"""``structure/md.py`` helpers + the ``struct_md`` job_type (gr606465):
params normalization, the content address, the mobile-atom set, the
store ops (migration 0290), and the dispatch → write-back path —
end-to-end on the real ``emt`` runner (no ``[dft-ml]`` extra needed), so
the acceptance — a 1 ps NVT run on a held slab returning a trajectory
handle and an MSD — exercises the same code prod runs.

Scenes reuse the NEB pair fixture (``_endpoint_is``: a Cu(111)[3,3,4]
slab with the bottom two layers fixed, an adsorbed O and a CH4 — a
"held slab" by construction).
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.structure import StructureHandler
from precis.structure.md import (
    MD_CODE_VERSION,
    MD_DEFAULTS,
    md_cache_key,
    mobile_indices,
    normalize_md_params,
    rdf_summary,
    run_nvt,
)
from precis.structure.neb import neb_atoms, slab_first_order
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, struct_md
from tests.test_structure_endpoints import _endpoint_is
from tests.test_structure_handler import _child_jobs2
from tests.test_structure_neb_store import _job as _status_job
from tests.test_structure_pair_check import _IS_PAYLOAD, _put_is_fs


def _slab(element: str = "Cu"):
    pytest.importorskip("ase")
    scene, _mg_site, _spare = _endpoint_is(element)
    return scene


def _md_atoms(scene: Any):
    order, n_slab = slab_first_order(scene)
    return neb_atoms(scene, order), order, n_slab


# -- params normalization ---------------------------------------------------


def test_normalize_defaults_fill_the_spec_knob_set() -> None:
    assert normalize_md_params(None) == MD_DEFAULTS
    assert normalize_md_params({}) == MD_DEFAULTS


def test_normalize_coerces_and_keeps_overrides() -> None:
    out = normalize_md_params(
        {
            "temperature_k": "500",
            "timestep_fs": 0.5,
            "steps": "2000",
            "friction_per_fs": 0.05,
            "traj_every": "20",
            "rdf_rmax_A": 8.0,
            "rdf_nbins": "80",
            "seed": 7,
        }
    )
    assert out["temperature_k"] == 500.0 and out["timestep_fs"] == 0.5
    assert out["steps"] == 2000 and out["traj_every"] == 20
    assert out["friction_per_fs"] == 0.05 and out["seed"] == 7
    assert out["rdf_rmax_A"] == 8.0 and out["rdf_nbins"] == 80


def test_normalize_rejects_unknown_and_out_of_bounds_keys() -> None:
    with pytest.raises(ValueError, match="stepz"):
        normalize_md_params({"stepz": 10})
    for bad in (
        {"temperature_k": 0},
        {"temperature_k": 3000},
        {"timestep_fs": 0},
        {"timestep_fs": 10},
        {"steps": 0},
        {"steps": 100000},
        {"friction_per_fs": 0},
        {"traj_every": 0},
        {"rdf_rmax_A": 0},
        {"rdf_nbins": 0},
        {"seed": -1},
    ):
        with pytest.raises(ValueError, match="md params"):
            normalize_md_params(bad)


# -- the content address ------------------------------------------------------


def test_cache_key_is_deterministic_and_param_sensitive() -> None:
    scene = _slab()
    base = normalize_md_params({})
    k1 = md_cache_key(scene, fidelity="ml", model="mace_mp", params=base)
    k2 = md_cache_key(scene, fidelity="ml", model="mace_mp", params=base)
    assert k1 == k2 and len(k1) == 64
    assert md_cache_key(scene, fidelity="emt", model="emt", params=base) != k1
    assert md_cache_key(scene, fidelity="ml", model="chgnet", params=base) != k1
    assert (
        md_cache_key(
            scene,
            fidelity="ml",
            model="mace_mp",
            params=normalize_md_params({"temperature_k": 600}),
        )
        != k1
    )
    assert (
        md_cache_key(
            scene, fidelity="ml", model="mace_mp", params=base, code_version="999"
        )
        != k1
    )


def test_cache_key_ignores_request_stamps() -> None:
    scene = _slab()
    base = normalize_md_params({})
    k = md_cache_key(scene, fidelity="ml", model="mace_mp", params=base)
    stamped = {**base, "requested_by": "42"}
    assert md_cache_key(scene, fidelity="ml", model="mace_mp", params=stamped) == k


def test_code_version_constant_is_stamped() -> None:
    assert MD_CODE_VERSION == "2"


# -- the mobile-atom set ------------------------------------------------------


def test_mobile_indices_excludes_the_held_layers() -> None:
    scene = _slab()
    _atoms, order, n_slab = _md_atoms(scene)
    mobile = mobile_indices(scene, order)
    fixed = {i for i, la in enumerate(order) if scene.atoms[la].fixed}
    assert fixed and max(fixed) < n_slab  # fix_layers: 2 pinned the bottom
    assert set(mobile) == set(range(len(order))) - fixed
    # The adsorbate atoms (O, C, H) are all mobile.
    mobile_elements = {scene.atoms[order[i]].element for i in mobile}
    assert {"O", "C", "H"} <= mobile_elements


# -- the NVT runner (real EMT backend) ----------------------------------------


def test_run_nvt_emt_1ps_on_held_slab() -> None:
    """Acceptance (gr606465): a 1 ps NVT run on a held slab returns a
    trajectory and an MSD — run on the always-on EMT backend so the test
    needs no [dft-ml] extra."""
    import io

    from ase.calculators.emt import EMT
    from ase.io import read as ase_read

    scene = _slab()
    atoms, order, _n_slab = _md_atoms(scene)
    atoms.calc = EMT()
    params = normalize_md_params({"steps": 1000, "timestep_fs": 1.0, "traj_every": 100})
    outcome = run_nvt(atoms, params, timeout_s=300.0)

    assert outcome["n_steps"] == 1000 and outcome["n_frames"] == 11
    assert outcome["t_ps"][-1] == pytest.approx(1.0)
    # The trajectory handle: extXYZ that parses back to the same frames.
    frames = ase_read(io.StringIO(outcome["extxyz"]), index=":", format="extxyz")
    assert isinstance(frames, list)
    assert len(frames) == outcome["n_frames"]
    assert len(frames[0]) == len(order)
    # The MSD summary: a value per sample, mobile = the free atoms.
    msd = outcome["msd"]
    assert msd["n_mobile"] == len(mobile_indices(scene, order))
    assert len(msd["msd_A2"]) == outcome["n_frames"]
    assert msd["msd_A2"][0] == pytest.approx(0.0)
    assert msd["final_A2"] == pytest.approx(msd["msd_A2"][-1])
    assert msd["final_A2"] >= 0.0
    # Thermodynamic timeseries is populated and finite.
    assert outcome["temp_mean_k"] > 0.0
    assert np.isfinite(outcome["e_pot_mean_ev"])
    # RDF: only pairs with at least one mobile endpoint. Top-layer Cu is
    # mobile (only the bottom two layers are held), so Cu-Cu pairs are
    # present alongside the adsorbate–surface pairs; nothing fully held
    # leaks in (every stored pair touches a mobile atom by construction).
    pairs = outcome["rdf"]["pairs"]
    assert "Cu-O" in pairs and "C-Cu" in pairs and "Cu-Cu" in pairs


def test_run_nvt_held_atoms_stay_pinned_and_frames_keep_sampled_positions() -> None:
    """Regression (td607072): the COM pin must be scoped to the mobile
    set and the extXYZ must serialize the sampled positions verbatim —
    a global ``FixCom`` ratchets held atoms by the per-step COM
    correction, and constrained frame copies collapse held coordinates
    onto the post-run positions."""
    import io

    from ase.calculators.emt import EMT
    from ase.io import read as ase_read

    scene = _slab()
    atoms, order, _n_slab = _md_atoms(scene)
    atoms.calc = EMT()
    initial = atoms.get_positions().copy()
    held = sorted(i for i, la in enumerate(order) if scene.atoms[la].fixed)
    assert held  # the fixture IS the held slab

    params = normalize_md_params({"steps": 300, "timestep_fs": 1.0, "traj_every": 50})
    outcome = run_nvt(atoms, params, timeout_s=300.0)

    # Held coordinates are bit-exact across the whole run — the global
    # FixCom's uniform shift used to drift them every step.
    np.testing.assert_array_equal(atoms.get_positions()[held], initial[held])

    frames = ase_read(io.StringIO(outcome["extxyz"]), index=":", format="extxyz")
    assert isinstance(frames, list)
    # Frame 0 is the initial geometry verbatim — a constrained copy
    # rewrote held atoms to the post-run positions and shifted every
    # frame's mobile set onto the final COM.
    np.testing.assert_allclose(frames[0].get_positions(), initial, atol=1e-9)
    for f in frames[1:]:
        np.testing.assert_allclose(f.get_positions()[held], initial[held], atol=1e-9)


def test_msd_and_rdf_on_a_static_frame_set() -> None:
    """The summaries on a held geometry: mobile atoms at rest give a
    zero MSD and a finite RDF — the degenerate honest read."""
    cell = np.eye(3) * 10.0
    pbc = (True, True, True)
    pos = np.array([[0.0, 0.0, 0.0], [2.0, 0.0, 0.0], [0.0, 2.0, 0.0]])
    positions = [pos, pos.copy()]
    out = rdf_summary(
        positions, ["Cu", "H", "H"], cell, pbc, [1, 2], rmax=6.0, nbins=60
    )
    assert set(out["pairs"]) == {"Cu-H", "H-H"}
    assert out["pairs"]["Cu-H"]["n_pairs"] == 2
    g = np.asarray(out["pairs"]["H-H"]["g"])
    # One pair at 2.0 Å → exactly one nonzero bin.
    assert g.max() > 0 and (g > 0).sum() == 1

    from precis.structure.md import msd_series

    ms = msd_series(positions, cell, pbc, [1, 2])
    assert ms["msd_A2"] == [0.0, 0.0]
    assert ms["final_A2"] == 0.0


def test_normalize_ceiling_refuses_unbounded_gpu_spend() -> None:
    with pytest.raises(ValueError, match="steps"):
        normalize_md_params({"steps": 50001})


# -- the struct_md store ops (real PG; migration 0290) -------------------------


def _ref(store: Any, slug: str) -> int:
    hit = store.get_ref(kind="structure", id=slug)
    if hit is not None:
        return int(hit.id)
    return int(store.insert_ref(kind="structure", slug=slug, title=slug).id)


def _record(store: Any, *, cache_key: str = "k1", status: str = "queued") -> int:
    return store.struct_md_record_run(
        structure_ref_id=_ref(store, "md-a"),
        on_version=1,
        fidelity="ml",
        model="mace_mp",
        params={"steps": 1000},
        cache_key=cache_key,
        job_ref_id=None,
        status=status,
    )


def _get(store: Any, run_id: int) -> dict:
    row = store.struct_md_get(run_id)
    assert row is not None, run_id
    return row


def test_migration_0290_table_and_succeeded_index_exist(store: Any) -> None:
    with store.pool.connection() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name = 'struct_md_runs'"
            ).fetchall()
        }
        assert tables == {"struct_md_runs"}
        idx = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'struct_md_runs_key_succeeded'"
        ).fetchone()
    assert idx is not None
    assert "succeeded" in idx[0]


def test_record_and_update_roundtrip(store: Any) -> None:
    run_id = _record(store)
    row = _get(store, run_id)
    assert row["status"] == "queued"
    assert row["fidelity"] == "ml" and row["on_version"] == 1
    for col in ("n_steps", "n_frames", "msd", "rdf", "trajectory"):
        assert row[col] is None, col

    store.struct_md_update_run(
        run_id,
        status="succeeded",
        n_steps=1000,
        n_frames=11,
        temperature_k=300.0,
        temp_mean_k=298.7,
        e_pot_mean_ev=-12.3,
        e_pot_std_ev=0.4,
        msd={"t_ps": [0.0, 1.0], "msd_A2": [0.0, 0.5], "n_mobile": 24, "final_A2": 0.5},
        rdf={"rmax": 6.0, "nbins": 60, "pairs": {}},
        trajectory="1\nx\nH 0 0 0\n",
        wall_s=3.0,
    )
    row = _get(store, run_id)
    assert row["status"] == "succeeded" and row["n_frames"] == 11
    assert row["msd"]["final_A2"] == 0.5
    assert row["trajectory"].startswith("1\n")
    # A status-only flip must not null out the stored result.
    store.struct_md_update_run(run_id, status="failed")
    row = _get(store, run_id)
    assert row["status"] == "failed" and row["n_steps"] == 1000


def test_unique_index_allows_one_succeeded_row_per_cache_key(store: Any) -> None:
    import psycopg

    first = _record(store, cache_key="dup")
    store.struct_md_update_run(first, status="succeeded")
    second = _record(store, cache_key="dup")
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.struct_md_update_run(second, status="succeeded")


def test_cached_and_inflight_and_reap_dead(store: Any) -> None:
    assert store.struct_md_cached("k2") is None
    assert store.struct_md_inflight("k2") is None
    run_id = _record(store, cache_key="k2")
    assert store.struct_md_inflight("k2")["id"] == run_id
    store.struct_md_update_run(run_id, status="succeeded")
    assert store.struct_md_inflight("k2") is None
    assert store.struct_md_cached("k2")["id"] == run_id

    # A live-status row under a dead job is a zombie the reaper fails.
    zombie = _record(store, cache_key="k3")
    store.struct_md_update_run(zombie, status="running")
    store.struct_md_update_run(zombie, job_ref_id=_status_job(store, "failed"))
    assert store.struct_md_inflight("k3") is None
    assert store.struct_md_reap_dead("k3") == [zombie]
    assert _get(store, zombie)["status"] == "failed"
    assert store.struct_md_reap_dead("k3") == []


def test_mints_today_counts_every_mint_but_dedupe(store: Any) -> None:
    assert store.struct_md_mints_today() == 0
    _record(store, cache_key="d1")
    spent = _record(store, cache_key="d2")
    store.struct_md_update_run(spent, status="failed")  # still counts
    collapsed = _record(store, cache_key="d3")
    store.struct_md_update_run(
        collapsed, status="failed", dedupe="collapsed onto in-flight job jo9"
    )
    assert store.struct_md_mints_today() == 2
    assert _get(store, collapsed)["params"]["dedupe"].startswith("collapsed")


def test_runs_for_returns_newest_first(store: Any) -> None:
    a = _record(store, cache_key="r1")
    b = _record(store, cache_key="r2")
    assert [r["id"] for r in store.struct_md_runs_for(_ref(store, "md-a"))][:2] == [
        b,
        a,
    ]
    assert store.struct_md_runs_for(_ref(store, "unrelated")) == []


# -- the struct_md job dispatch -------------------------------------------------


def _fake_ctx(store: Any, params: dict[str, Any]) -> tuple[DispatchContext, list[Any]]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=7777,
        title="md",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _md_ref(store: Any) -> Any:
    scene = _slab()
    ref, _ = store.structure_save(
        slug="md_is", title="MD slab", scene=scene, version=1, card_text="MD"
    )
    return ref


def _job_params(store: Any, ref: Any, run_id: int, **over: Any) -> dict:
    params: dict[str, Any] = {
        "md_run_id": run_id,
        "structure_ref_id": ref.id,
        "on_version": store.structure_version(ref.id),
        "is_slug": "md_is",
        "fidelity": "emt",
        "model": "emt",
        "params": normalize_md_params({}),
        "cache_key": "k-md-job",
        "target_node": None,
    }
    params.update(over)
    return params


def _outcome(**over: Any) -> dict[str, Any]:
    outcome: dict[str, Any] = {
        "extxyz": "2\nframe\nH 0.0 0.0 0.0\nH 0.0 0.0 0.74\n",
        "t_ps": [0.0, 1.0],
        "e_pot_ev": [-1.0, -0.9],
        "temp_k": [300.0, 310.0],
        "temperature_k": 300.0,
        "temp_mean_k": 305.0,
        "e_pot_mean_ev": -0.95,
        "e_pot_std_ev": 0.05,
        "msd": {
            "t_ps": [0.0, 1.0],
            "msd_A2": [0.0, 0.5],
            "n_mobile": 24,
            "final_A2": 0.5,
        },
        "rdf": {"rmax": 6.0, "nbins": 60, "pairs": {}},
        "n_steps": 1000,
        "n_frames": 2,
        "n_mobile": 24,
        "wall_s": 3.0,
    }
    outcome.update(over)
    return outcome


def test_job_type_is_registered() -> None:
    spec = get_job_type("struct_md")
    assert spec is not None and spec.dispatch is struct_md._dispatch
    assert "ssh_node" in spec.compatible_executors


def test_job_writes_trajectory_and_summaries(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _md_ref(store)
    run_id = store.struct_md_record_run(
        structure_ref_id=ref.id,
        on_version=store.structure_version(ref.id),
        fidelity="emt",
        model="emt",
        params=normalize_md_params({}),
        cache_key="k-md-job",
        job_ref_id=None,
    )
    monkeypatch.setattr(struct_md, "MD_RUNNER", lambda **kw: _outcome())
    ctx, events = _fake_ctx(store, _job_params(store, ref, run_id))
    struct_md._dispatch(ctx, struct_md.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded"
    assert row["n_steps"] == 1000 and row["n_frames"] == 2
    assert row["msd"]["final_A2"] == 0.5
    assert row["trajectory"].startswith("2\n")
    assert row["temp_mean_k"] == 305.0 and row["e_pot_mean_ev"] == -0.95
    # Nothing lands in the relax run-cube (same rule as struct_neb).
    assert store.structure_runs(ref.id) == []


def test_job_end_to_end_on_the_real_emt_runner(store: Any) -> None:
    """The full acceptance path: a minted run row + the real
    ``_default_md_runner`` at the always-on ``emt`` rung — a 1 ps NVT on
    a held slab returns a trajectory handle and an MSD."""
    pytest.importorskip("ase")
    ref = _md_ref(store)
    run_id = store.struct_md_record_run(
        structure_ref_id=ref.id,
        on_version=store.structure_version(ref.id),
        fidelity="emt",
        model="emt",
        params=normalize_md_params({}),
        cache_key="k-md-e2e",
        job_ref_id=None,
    )
    params = _job_params(
        store,
        ref,
        run_id,
        params=normalize_md_params(
            {"steps": 1000, "timestep_fs": 1.0, "traj_every": 100}
        ),
    )
    ctx, events = _fake_ctx(store, params)
    struct_md._dispatch(ctx, struct_md.SPEC)

    assert ("status", "succeeded") in events
    row = _get(store, run_id)
    assert row["status"] == "succeeded"
    assert row["n_steps"] == 1000 and row["n_frames"] == 11
    assert row["msd"]["n_mobile"] == len(mobile_indices(*(_md_args(store, ref))))
    assert row["msd"]["final_A2"] >= 0.0
    assert row["temp_mean_k"] > 0.0
    # The trajectory handle: parseable extXYZ frames.
    import io

    from ase.io import read as ase_read

    frames = ase_read(io.StringIO(row["trajectory"]), index=":", format="extxyz")
    assert isinstance(frames, list)
    assert len(frames) == row["n_frames"]


def _md_args(store: Any, ref: Any) -> tuple[Any, list[str]]:
    scene, _ = store.structure_load(ref.id)
    order, _ = slab_first_order(scene)
    return scene, order


def test_job_drift_refuses_before_compute(store: Any) -> None:
    ref = _md_ref(store)
    run_id = store.struct_md_record_run(
        structure_ref_id=ref.id,
        on_version=99,  # minted against a version that isn't live
        fidelity="emt",
        model="emt",
        params=normalize_md_params({}),
        cache_key="k-drift",
        job_ref_id=None,
    )
    params = _job_params(store, ref, run_id, on_version=99)
    ctx, events = _fake_ctx(store, params)
    struct_md._dispatch(ctx, struct_md.SPEC)
    assert _get(store, run_id)["status"] == "failed"
    assert any(k == "fail" for k, _ in events)


def test_job_fully_fixed_design_refuses(store: Any) -> None:
    ref = _md_ref(store)
    with store.pool.connection() as conn:
        conn.execute("UPDATE struct_atoms SET fixed = 7 WHERE ref_id = %s", (ref.id,))
    run_id = store.struct_md_record_run(
        structure_ref_id=ref.id,
        on_version=store.structure_version(ref.id),
        fidelity="emt",
        model="emt",
        params=normalize_md_params({}),
        cache_key="k-fixed",
        job_ref_id=None,
    )
    ctx, events = _fake_ctx(store, _job_params(store, ref, run_id))
    struct_md._dispatch(ctx, struct_md.SPEC)
    assert _get(store, run_id)["status"] == "failed"
    fail = next(v for k, v in events if k == "fail")
    assert "held fixed" in fail["reason"]


# -- the mode='md' mint + view='md' render (handler level) ---------------------


@pytest.fixture
def structure(store: Any) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _pin_nodes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PRECIS_MLIP_NODES", "alpha,beta")
    for env in ("PRECIS_AUTOCATPATH_ROUTE_NODE", "PRECIS_DFT_NODE"):
        monkeypatch.delenv(env, raising=False)


def _md(structure: Any, **over: Any) -> Any:
    args: dict[str, Any] = {"fidelity": "emt"}
    args.update(over)
    return structure.put(id="ch_is", mode="md", args=args)


def _only_md_run(store: Any, ref_id: int) -> dict[str, Any]:
    runs = store.struct_md_runs_for(ref_id)
    assert len(runs) == 1
    return runs[0]


def _slug_ref(store: Any, slug: str) -> Any:
    ref = store.get_ref(kind="structure", id=slug)
    assert ref is not None, slug
    return ref


def test_put_md_arg_gates(structure: Any, store: Any) -> None:
    _put_is_fs(structure)
    with pytest.raises(BadInput, match="requires id"):
        structure.put(mode="md", args={"fidelity": "emt"})
    with pytest.raises(BadInput, match="unknown key"):
        _md(structure, bogus_knob=1)
    with pytest.raises(BadInput, match="md params"):
        _md(structure, steps=0)
    with pytest.raises(BadInput, match="fidelity"):
        _md(structure, fidelity="gpaw")
    assert store.struct_md_runs_for(_slug_ref(store, "ch_is").id) == []


def test_put_md_mints_the_run_row_and_job(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    resp = _md(structure, steps=500)
    assert "queued struct_md" in resp.body
    assert "emt/emt" in resp.body

    ref = _slug_ref(store, "ch_is")
    run = _only_md_run(store, ref.id)
    assert run["status"] == "queued" and run["fidelity"] == "emt"
    assert run["params"]["steps"] == 500
    assert run["job_ref_id"] is not None

    jobs = _child_jobs2(store, ref.id)
    assert len(jobs) == 1
    job = jobs[0]
    assert job["job_type"] == "struct_md" and job["executor"] == "ssh_node"
    params = job["params"]
    assert params["md_run_id"] == run["id"]
    assert params["fidelity"] == "emt" and params["model"] == "emt"
    assert params["target_node"] in {"alpha", "beta"}
    assert params["resources"]["wall_seconds"] >= 3600
    assert job["idem_key"] == f"struct_md:{run['cache_key']}"


def test_put_md_identical_request_collapses_inflight(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    _md(structure)
    resp = _md(structure)
    assert "already in flight" in resp.body
    assert len(_child_jobs2(store, _slug_ref(store, "ch_is").id)) == 1


def test_put_md_identical_request_is_a_cache_hit(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    _md(structure)
    run = _only_md_run(store, _slug_ref(store, "ch_is").id)
    store.struct_md_update_run(
        run["id"],
        status="succeeded",
        n_steps=1000,
        n_frames=11,
        temperature_k=300.0,
        temp_mean_k=300.0,
        e_pot_mean_ev=-1.0,
        e_pot_std_ev=0.1,
        msd={"final_A2": 0.5},
        trajectory="x",
    )
    resp = _md(structure)
    assert "cached struct_md run" in resp.body
    assert len(_child_jobs2(store, _slug_ref(store, "ch_is").id)) == 1


def test_put_md_cache_hit_from_an_identical_design_names_the_owner(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (td607072): a same-geometry design's identical request
    collapses onto the original run — the advertised handle must be the
    run's OWNER, not the requester (``view='md'`` only renders runs the
    asked ref owns, so a requester handle dead-ends in
    "doesn't belong")."""
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    structure.put(id="ch_twin", args={"derive_from": "ch_is", "ops": []})
    _pin_nodes(monkeypatch)
    _md(structure)
    run = _only_md_run(store, _slug_ref(store, "ch_is").id)
    store.struct_md_update_run(
        run["id"],
        status="succeeded",
        n_steps=1000,
        n_frames=11,
        temperature_k=300.0,
        temp_mean_k=300.0,
        e_pot_mean_ev=-1.0,
        e_pot_std_ev=0.1,
        msd={"final_A2": 0.5},
        trajectory="x",
    )
    resp = structure.put(id="ch_twin", mode="md", args={"fidelity": "emt"})
    assert "cached struct_md run" in resp.body
    assert f"st{run['structure_ref_id']} (ch_is)" in resp.body
    assert "id='ch_is'" in resp.body
    assert "id='ch_twin'" not in resp.body
    # The advertised pointer actually renders the run.
    got = structure.get(id="ch_is", view="md", args={"run": run["id"]})
    assert f"md run {run['id']}" in got.body


def test_put_md_inflight_from_an_identical_design_polls_the_owner(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The in-flight collapse's poll hint names the owner too — a
    requester slug's ``view='md'`` list is empty while the run is the
    original design's."""
    structure.put(id="ch_is", text=_IS_PAYLOAD)
    structure.put(id="ch_twin", args={"derive_from": "ch_is", "ops": []})
    _pin_nodes(monkeypatch)
    _md(structure)
    resp = structure.put(id="ch_twin", mode="md", args={"fidelity": "emt"})
    assert "already in flight" in resp.body
    assert "id='ch_is'" in resp.body
    assert "id='ch_twin'" not in resp.body
    assert store.struct_md_runs_for(_slug_ref(store, "ch_twin").id) == []


def test_put_md_fully_fixed_refuses(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    ref = _slug_ref(store, "ch_is")
    with store.pool.connection() as conn:
        conn.execute("UPDATE struct_atoms SET fixed = 7 WHERE ref_id = %s", (ref.id,))
    with pytest.raises(BadInput, match="held fixed"):
        _md(structure)
    assert store.struct_md_runs_for(ref.id) == []


def test_put_md_daily_cap_meters_mints(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    monkeypatch.setenv("PRECIS_STRUCT_MD_DAILY", "1")
    _md(structure)
    ref = _slug_ref(store, "ch_is")
    store.struct_md_update_run(_only_md_run(store, ref.id)["id"], status="failed")
    with pytest.raises(BadInput, match="cap"):
        _md(structure, temperature_k=500)
    assert len(store.struct_md_runs_for(ref.id)) == 1


def test_view_md_lists_and_opens_a_run(
    structure: Any, store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _put_is_fs(structure)
    _pin_nodes(monkeypatch)
    ref = _slug_ref(store, "ch_is")
    resp = structure.get(id="ch_is", view="md")
    assert "no MD runs" in resp.body

    _md(structure)
    run = _only_md_run(store, ref.id)
    store.struct_md_update_run(
        run["id"],
        status="succeeded",
        n_steps=1000,
        n_frames=11,
        temperature_k=300.0,
        temp_mean_k=298.0,
        e_pot_mean_ev=-1.0,
        e_pot_std_ev=0.1,
        msd={
            "t_ps": [0.0, 1.0],
            "msd_A2": [0.0, 0.5],
            "n_mobile": 3,
            "final_A2": 0.5,
        },
        rdf={
            "rmax": 6.0,
            "nbins": 60,
            "pairs": {"C-O": {"r": [0.05, 0.15], "g": [0.0, 2.1], "n_pairs": 1}},
        },
        trajectory="2\nframe\nH 0 0 0\nH 0 0 0.74\n",
        wall_s=3.0,
    )
    resp = structure.get(id="ch_is", view="md")
    assert "1 MD run(s)" in resp.body
    resp = structure.get(id="ch_is", view="md", args={"run": run["id"]})
    assert "MSD over 3 mobile" in resp.body
    assert "C-O" in resp.body and "2.10" in resp.body
    resp = structure.get(
        id="ch_is", view="md", args={"run": run["id"], "trajectory": True}
    )
    assert resp.body.startswith("2\n")
    with pytest.raises(BadInput, match="doesn't belong"):
        structure.get(id="ch_fs", view="md", args={"run": run["id"]})
