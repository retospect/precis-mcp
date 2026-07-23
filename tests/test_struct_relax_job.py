"""``struct_relax`` job_type — the cache↔relax seam (ADR 0043 §23.12 + §23.16).

Proves the seam end-to-end *without a cluster*: a stubbed container run (the
:data:`RUNNER` hook writes a fake ``result.json``) drives the dispatch, which
records the **run-cube** — and a subsequent ``StructureHandler`` relax of the
same geometry is then a zero-compute cache hit that writes back the relaxed
positions. Compute happens once, ever; everything after is a lookup.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.structure import cache as relax_cache
from precis.structure.export import _grouped, to_poscar
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import struct_relax

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, True]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def _poscar_labels(scene) -> list[str]:
    """Labels in the row order ``to_poscar`` emits (element-grouped)."""
    order, groups = _grouped(scene)
    return [a.label for el in order for a in groups[el]]


def _fake_ctx(store, params: dict[str, Any]) -> tuple[DispatchContext, list]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=999,
        title="relax",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _build_params(structure, ident: str = "pd_pair") -> dict[str, Any]:
    """The job params the handler (Part B) will mint — built here directly."""
    ref = structure.store.get_ref(kind="structure", id=ident)
    scene, _ = structure.store.structure_load(ref.id)
    return {
        "structure_ref_id": ref.id,
        "on_version": structure.store.structure_version(ref.id),
        "fidelity": "ml",
        "model": "mace_mp",
        "steps": 200,
        "cache_key": relax_cache.run_cache_key(
            scene, fidelity="ml", model="mace_mp", params={"steps": 200}
        ),
        "structure_sha": relax_cache.structure_sha(scene),
        "order": relax_cache.canonical_order(scene),
        "poscar_labels": _poscar_labels(scene),
        "poscar": to_poscar(scene),
    }


def _relaxed_poscar(structure, ident: str, moved_to: float) -> str:
    """A POSCAR like the container would emit: aPd2 relaxed along x."""
    scene, _ = structure.store.structure_load(
        structure.store.get_ref(kind="structure", id=ident).id
    )
    scene.atoms["aPd2"].frac = np.array([moved_to, 0.0, 0.0])
    return to_poscar(scene)


def _stub_runner(relaxed_poscar: str, *, ok: bool = True, e_tot: float = -3.21):
    """A RUNNER that writes a fake result.json into out_dir (no cluster)."""

    def runner(argv, *, node, in_dir, out_dir, timeout=None):
        result = {
            "ok": ok,
            "scalars": {
                "E_tot": e_tot,
                "max_force": 0.04,
                "n_steps": 7,
                "converged": True,
            },
            "relaxed_poscar": relaxed_poscar,
            "curve": [0.5, 0.1, 0.04],
        }
        Path(out_dir, "result.json").write_text(json.dumps(result))
        return 0, "SCF converged\n"

    return runner


def test_build_run_argv_docker_vs_podman():
    docker = struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o")
    assert docker[:5] == ["docker", "run", "--rm", "--name", "precis-job-7"]
    assert "--gpus" in docker and "all" in docker
    podman = struct_relax.build_run_argv(
        ref_id=7, in_dir="/i", out_dir="/o", container_cmd="podman"
    )
    assert "--device" in podman and "nvidia.com/gpu=all" in podman
    assert "--gpus" not in podman
    # CPU fallback omits the GPU flag entirely.
    cpu = struct_relax.build_run_argv(ref_id=7, in_dir="/i", out_dir="/o", gpus=0)
    assert "--gpus" not in cpu and "--device" not in cpu


def test_dispatch_populates_the_run_cube(structure, tmp_path, monkeypatch):
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)

    monkeypatch.setattr(struct_relax, "STAGER", lambda rid: _stage(tmp_path, rid))
    monkeypatch.setattr(
        struct_relax,
        "RUNNER",
        _stub_runner(_relaxed_poscar(structure, "pd_pair", 0.24)),
    )
    ctx, events = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    assert ("status", "succeeded") in events
    # the run-cube now carries this cache_key + the relaxed geometry.
    hit = structure.store.structure_find_cached_run(params["cache_key"])
    assert hit is not None
    assert hit["converged"] is True
    assert hit["energy"] == pytest.approx(-3.21)
    assert hit["curve"] == [0.5, 0.1, 0.04]
    # final_geometry is in canonical order; aPd2 moved 0.26 → 0.24.
    fracs = {round(row[0], 4) for row in hit["final_geometry"]["frac"]}
    assert fracs == {0.0, 0.24}


def test_seam_a_later_handler_relax_is_a_zero_compute_hit(
    structure, tmp_path, monkeypatch
):
    """The whole point: the dispatch writes the cube, then an *otherwise-gated*
    ml relax on the same design returns from cache — no backend, no Unsupported,
    and the relaxed geometry lands on the design."""
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)
    monkeypatch.setattr(struct_relax, "STAGER", lambda rid: _stage(tmp_path, rid))
    monkeypatch.setattr(
        struct_relax,
        "RUNNER",
        _stub_runner(_relaxed_poscar(structure, "pd_pair", 0.24)),
    )
    ctx, _ = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    # ml would raise Unsupported (no MACE here); the seam makes it a cache hit.
    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "relax[ml]" in resp.body and "converged" in resp.body
    reloaded, _ = structure.store.structure_load(
        structure.store.get_ref(kind="structure", id="pd_pair").id
    )
    assert round(float(reloaded.atoms["aPd2"].frac[0]), 4) == 0.24


def test_dispatch_failure_records_no_cache_row(structure, tmp_path, monkeypatch):
    """``ok: false`` in ``result.json`` is the relax code itself reporting a
    genuine (non-convergence) physical failure — a real verdict on the
    candidate, so ``failure_class="non-convergence"``."""
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)
    monkeypatch.setattr(struct_relax, "STAGER", lambda rid: _stage(tmp_path, rid))
    monkeypatch.setattr(
        struct_relax,
        "RUNNER",
        _stub_runner(_relaxed_poscar(structure, "pd_pair", 0.24), ok=False),
    )
    ctx, events = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    fails = [payload for k, payload in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "non-convergence"
    assert ("status", "succeeded") not in events
    assert structure.store.structure_find_cached_run(params["cache_key"]) is None


def test_dispatch_infra_failure_is_classed_infra(structure, tmp_path, monkeypatch):
    """The real bug this pins: a runner that dies (container/docker/executor
    failure — no ``result.json`` at all) must be classed ``"infra"``, NOT
    laundered into the same bucket as a genuine physical non-convergence —
    quest ``harvest_measures`` reads this to decide ruled-out vs retry."""
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)
    monkeypatch.setattr(struct_relax, "STAGER", lambda rid: _stage(tmp_path, rid))

    def _crashing_runner(argv, *, node, in_dir, out_dir, timeout=None):
        return 137, "OOM-killed"  # no result.json written — container died

    monkeypatch.setattr(struct_relax, "RUNNER", _crashing_runner)
    ctx, events = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    fails = [payload for k, payload in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "infra"
    assert ("status", "succeeded") not in events
    assert structure.store.structure_find_cached_run(params["cache_key"]) is None


def _stage(tmp_path, ref_id: int) -> tuple[str, str]:
    base = Path(tmp_path) / f"job-{ref_id}"
    (base / "in").mkdir(parents=True, exist_ok=True)
    (base / "out").mkdir(parents=True, exist_ok=True)
    return str(base / "in"), str(base / "out")
