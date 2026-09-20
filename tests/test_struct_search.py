"""``struct_search`` — pure ``SearchSpec``/``top_k_distinct`` + the job_type
write-back seam (stubbed :data:`struct_search.SEARCH_RUNNER`, no AGOX)."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.structure.cell import Cell
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis.structure.search import (
    SearchCandidate,
    SearchResult,
    SearchSpec,
    SearchUnsupported,
    top_k_distinct,
)
from precis.structure.validate import validate as validate_scene
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, struct_search
from precis.workers.job_types.quest_tick import _SIM_JOB_TYPES

_SEED_SPEC = json.dumps(
    {"ops": [{"op": "slab", "element": "Pd", "size": [3, 3, 4], "fix_layers": 2}]}
)

#: A single 4-atom (Pd, Pd, N, O) cluster shape, as Cartesian offsets (Å)
#: from a center point — spaced well past every pairwise overlap floor
#: (Pd-Pd's 1.668 Å is the largest of the four pairs here).
_CLUSTER_OFFSETS: list[tuple[float, float]] = [
    (0.0, 0.0),
    (2.2, 0.0),
    (1.1, 1.9),
    (-1.1, 1.9),
]

#: Height (Å) above the slab's own top layer — clears every pairwise overlap
#: floor on its own (the largest, Pd-Pd, is 1.668 Å < this), so lateral
#: placement only has to keep the 4 new atoms apart from EACH OTHER, not
#: from the (frozen) slab.
_HOVER_A = 2.3


def _seed_scene() -> Scene:
    """Pure, DB-free seed slab (no store needed) — for the pure dedup test."""
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, True)))
    apply_ops(
        scene, [{"op": "slab", "element": "Pd", "size": [3, 3, 4], "fix_layers": 2}]
    )
    return scene


def _cluster_positions(
    seed: Scene, center_fx: float, center_fy: float
) -> list[tuple[float, float, float]]:
    """4 frac positions (Pd, Pd, N, O) for one cluster centered at
    ``(center_fx, center_fy)``, hovering :data:`_HOVER_A` above the seed's
    own top layer."""
    top_frac_z = max(float(a.frac[2]) for a in seed.atoms.values())
    hover_frac_z = top_frac_z + _HOVER_A / float(seed.cell.lattice[2, 2])
    positions = []
    for dx, dy in _CLUSTER_OFFSETS:
        off = seed.cell.cart_to_frac(np.array([dx, dy, 0.0]))
        positions.append(
            (float(center_fx + off[0]), float(center_fy + off[1]), float(hover_frac_z))
        )
    return positions


# 10 distinct cluster centers — each candidate is an independent Scene, so
# only a cluster's OWN 4 atoms need to avoid overlap; centers just need to
# differ so the 10 candidates are genuinely distinct positions.
_CLUSTERS: list[list[tuple[float, float, float]]] = [
    _cluster_positions(_seed_scene(), 0.03 * i, 0.02 * i) for i in range(10)
]


def _with_adatoms(base: Scene, positions: list[tuple[float, float, float]]) -> Scene:
    scene = copy.deepcopy(base)
    for el, pos in zip(("Pd", "Pd", "N", "O"), positions, strict=True):
        apply_ops(scene, [{"op": "add_atom", "element": el, "frac": list(pos)}])
    return scene


def _shift(scene: Scene, dx: float, dy: float) -> Scene:
    """A translation twin: every atom (slab + adatoms) moved by (dx, dy)."""
    out = copy.deepcopy(scene)
    delta = np.array([dx, dy, 0.0])
    for atom in out.atoms.values():
        atom.frac = out.cell.wrap(atom.frac + delta)
    return out


def _canned_population(seed: Scene) -> list[SearchCandidate]:
    """12 candidates: 10 distinct clusters + 2 translation twins of two of
    them (clusters 0 and 5) at a *worse* energy — 10 distinct after dedup."""
    candidates = [
        SearchCandidate(
            scene=_with_adatoms(seed, positions),
            oracle_energy_eV=-2.0 - 0.05 * i,
            surrogate_energy_eV=None,
            iteration=i,
        )
        for i, positions in enumerate(_CLUSTERS)
    ]
    twin_of_0 = SearchCandidate(
        scene=_shift(candidates[0].scene, 0.31, 0.17),
        oracle_energy_eV=-1.0,  # worse than candidates[0] -> loses the dedup
        surrogate_energy_eV=None,
        iteration=10,
    )
    twin_of_5 = SearchCandidate(
        scene=_shift(candidates[5].scene, -0.22, 0.09),
        oracle_energy_eV=-1.0,
        surrogate_energy_eV=None,
        iteration=11,
    )
    return [*candidates, twin_of_0, twin_of_5]


# ── SearchSpec.validate() ───────────────────────────────────────────────


def _spec_kwargs(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "seed": _seed_scene(),
        "box": ((0.0, 1.0), (0.0, 1.0), (0.5, 0.8)),
        "add": {"Pd": 2, "N": 1, "O": 1},
        "model": "mace_mp",
    }
    base.update(overrides)
    return base


def test_valid_spec_constructs() -> None:
    spec = SearchSpec(**_spec_kwargs())
    assert spec.algo == "gofee"
    assert spec.budget == 200


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"add": {}}, "add"),
        ({"add": {"Pd": 0}}, "add"),
        ({"add": {"Pd": -1}}, "add"),
        ({"budget": 0}, "budget"),
        ({"budget": 1001}, "budget"),
        ({"box": ((0.0, 1.0), (0.0, 1.0))}, "box"),
        ({"box": ((0.0, 1.0), (0.0, 1.0), (0.8, 0.5))}, "box"),
        ({"box": ((-0.1, 1.0), (0.0, 1.0), (0.0, 1.0))}, "box"),
        ({"box": ((0.0, 1.1), (0.0, 1.0), (0.0, 1.0))}, "box"),
        ({"algo": "simulated_annealing"}, "algo"),
    ],
)
def test_invalid_spec_rejected(overrides: dict[str, Any], needle: str) -> None:
    with pytest.raises(ValueError, match=needle):
        SearchSpec(**_spec_kwargs(**overrides))


def test_non_periodic_seed_rejected() -> None:
    seed = _seed_scene()
    seed.cell = Cell(seed.cell.lattice, (True, False, True))
    with pytest.raises(ValueError, match="seed"):
        SearchSpec(**_spec_kwargs(seed=seed))


# ── top_k_distinct ───────────────────────────────────────────────────────


def test_top_k_distinct_collapses_translation_twins() -> None:
    seed = _seed_scene()
    population = _canned_population(seed)
    assert len(population) == 12

    ranked = top_k_distinct(population, k=10)

    assert len(ranked) == 10
    # ascending oracle energy
    energies = [c.oracle_energy_eV for c in ranked]
    assert energies == sorted(energies)
    # the twins' worse energy never wins over the original's lower energy
    assert -1.0 not in energies


def test_top_k_distinct_is_pure_no_agox_import(monkeypatch: pytest.MonkeyPatch) -> None:
    """Runs fine with no ``agox`` importable at all (mirrors `scripts/test`'s
    image, which lacks the ``[struct-search]`` extra)."""
    import builtins

    real_import = builtins.__import__

    def _no_agox(name: str, *a: Any, **kw: Any) -> Any:
        if name == "agox" or name.startswith("agox."):
            raise ImportError("no agox here")
        return real_import(name, *a, **kw)

    monkeypatch.setattr(builtins, "__import__", _no_agox)
    seed = _seed_scene()
    ranked = top_k_distinct(_canned_population(seed), k=10)
    assert len(ranked) == 10


# ── job_type write-back ───────────────────────────────────────────────────


@pytest.fixture
def structure(store: Any) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def _fake_ctx(
    store: Any, ref_id: int, params: dict[str, Any]
) -> tuple[DispatchContext, list[Any]]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=ref_id,
        title="search",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _seed_ref(structure: StructureHandler) -> Any:
    structure.put(id="pd_search_seed", text=_SEED_SPEC)
    return structure.store.get_ref(kind="structure", id="pd_search_seed")


def _search_params(
    seed_ref: Any, structure: StructureHandler, **overrides: Any
) -> dict[str, Any]:
    params: dict[str, Any] = {
        "seed_ref_id": seed_ref.id,
        "on_version": structure.store.structure_version(seed_ref.id),
        "box": [[0.0, 1.0], [0.0, 1.0], [0.5, 0.8]],
        "add": {"Pd": 2, "N": 1, "O": 1},
        "algo": "gofee",
        "model": "mace_mp",
        "budget": 200,
        "top_k": 10,
        "timeout_s": 60,
        "quest_id": None,
    }
    params.update(overrides)
    return params


def test_dispatch_writes_back_top_k_distinct_candidates(
    structure: StructureHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ref = _seed_ref(structure)
    seed_scene, _ = structure.store.structure_load(seed_ref.id)
    population = _canned_population(seed_scene)

    def _stub_runner(*, spec: SearchSpec, workdir: Path) -> SearchResult:
        return SearchResult(
            candidates=population,
            budget_used=200,
            wall_s=12.3,
            database_path=str(workdir / "agox.db"),
            best_energy_eV=min(c.oracle_energy_eV for c in population),
        )

    monkeypatch.setattr(struct_search, "SEARCH_RUNNER", _stub_runner)
    monkeypatch.setattr(struct_search, "_default_workdir", lambda ref_id: tmp_path)

    params = _search_params(seed_ref, structure)
    ctx, events = _fake_ctx(structure.store, 4242, params)
    struct_search._dispatch(ctx, struct_search.SPEC)

    assert ("status", "succeeded") in events
    meta_events = [kw for k, kw in events if k == "meta"]
    assert len(meta_events) == 1
    search_meta = meta_events[0]["search"]
    assert search_meta["count"] == 10
    ref_ids = search_meta["candidate_ref_ids"]
    assert len(ref_ids) == 10

    seen_ranks = set()
    for rid in ref_ids:
        scene, _ = structure.store.structure_load(rid)
        assert validate_scene(scene) == []

        ref = structure.store.get_ref(kind="structure", id=rid)
        assert ref is not None
        search_stamp = (ref.meta or {}).get("search")
        assert search_stamp is not None
        assert search_stamp["algo"] == "gofee"
        assert search_stamp["model"] == "mace_mp"
        assert search_stamp["job_ref_id"] == 4242
        assert search_stamp["oracle_energy_eV"] is not None
        seen_ranks.add(search_stamp["rank"])

        links = structure.store.links_for(rid, direction="out", relation="derived-from")
        assert any(lk.dst_ref_id == seed_ref.id for lk in links)

        assert structure.store.has_tag(rid, "OPEN", "search:agox")

        runs = structure.store.structure_runs(rid)
        ml_runs = [r for r in runs if r["fidelity"] == "ml"]
        assert len(ml_runs) == 1
        assert ml_runs[0]["energy"] == pytest.approx(search_stamp["oracle_energy_eV"])

    assert seen_ranks == set(range(1, 11))


def test_dispatch_with_quest_id_wires_candidate_tag_and_serves_link(
    structure: StructureHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ref = _seed_ref(structure)
    seed_scene, _ = structure.store.structure_load(seed_ref.id)
    population = _canned_population(seed_scene)

    def _stub_runner(*, spec: SearchSpec, workdir: Path) -> SearchResult:
        return SearchResult(candidates=population, budget_used=200, wall_s=1.0)

    monkeypatch.setattr(struct_search, "SEARCH_RUNNER", _stub_runner)
    monkeypatch.setattr(struct_search, "_default_workdir", lambda ref_id: tmp_path)

    quest = structure.store.insert_ref(kind="quest", slug=None, title="q")
    params = _search_params(seed_ref, structure, quest_id=quest.id)
    ctx, events = _fake_ctx(structure.store, 4243, params)
    struct_search._dispatch(ctx, struct_search.SPEC)

    meta_events = [kw for k, kw in events if k == "meta"]
    ref_ids = meta_events[0]["search"]["candidate_ref_ids"]
    assert len(ref_ids) == 10
    for rid in ref_ids:
        assert structure.store.has_tag(rid, "OPEN", "candidate")
        links = structure.store.links_for(rid, direction="out", relation="serves")
        assert any(lk.dst_ref_id == quest.id for lk in links)


def test_zero_candidates_after_dedup_still_succeeds(
    structure: StructureHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ref = _seed_ref(structure)

    def _stub_runner(*, spec: SearchSpec, workdir: Path) -> SearchResult:
        return SearchResult(candidates=[], budget_used=200, wall_s=1.0)

    monkeypatch.setattr(struct_search, "SEARCH_RUNNER", _stub_runner)
    monkeypatch.setattr(struct_search, "_default_workdir", lambda ref_id: tmp_path)

    params = _search_params(seed_ref, structure)
    ctx, events = _fake_ctx(structure.store, 4244, params)
    struct_search._dispatch(ctx, struct_search.SPEC)

    assert ("status", "succeeded") in events
    meta_events = [kw for k, kw in events if k == "meta"]
    assert meta_events[0]["search"]["count"] == 0


def test_search_unsupported_is_infra_and_names_the_extra(
    structure: StructureHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_ref = _seed_ref(structure)

    def _raise_unsupported(*, spec: SearchSpec, workdir: Path) -> SearchResult:
        raise SearchUnsupported("agox not importable")

    monkeypatch.setattr(struct_search, "SEARCH_RUNNER", _raise_unsupported)
    monkeypatch.setattr(struct_search, "_default_workdir", lambda ref_id: tmp_path)

    params = _search_params(seed_ref, structure)
    ctx, events = _fake_ctx(structure.store, 4245, params)
    struct_search._dispatch(ctx, struct_search.SPEC)

    fails = [payload for k, payload in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "infra"
    assert "[struct-search]" in fails[0]["reason"]
    assert ("status", "succeeded") not in events


def test_seed_version_drift_fails_fast_as_input(
    structure: StructureHandler, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A seed edited after the job was minted must not be searched silently:
    the job re-reads the geometry at claim time, so the dispatch-time version
    is the contract."""
    seed_ref = _seed_ref(structure)
    calls: list[SearchSpec] = []

    def _never_called(*, spec: SearchSpec, workdir: Path) -> SearchResult:
        calls.append(spec)
        raise AssertionError("runner must not run on a drifted seed")

    monkeypatch.setattr(struct_search, "SEARCH_RUNNER", _never_called)
    monkeypatch.setattr(struct_search, "_default_workdir", lambda ref_id: tmp_path)

    live = structure.store.structure_version(seed_ref.id)
    params = _search_params(seed_ref, structure, on_version=live + 1)
    ctx, events = _fake_ctx(structure.store, 4246, params)
    struct_search._dispatch(ctx, struct_search.SPEC)

    fails = [payload for k, payload in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "input"
    assert f"version {live}" in fails[0]["reason"]
    assert calls == []


# ── registration ───────────────────────────────────────────────────────


def test_struct_search_in_sim_job_types() -> None:
    assert "struct_search" in _SIM_JOB_TYPES


def test_registry_loads_struct_search() -> None:
    spec = get_job_type("struct_search")
    assert spec is not None
    assert spec.name == "struct_search"
    assert spec.compatible_executors == frozenset({"ssh_node"})
    assert spec.requires == frozenset({"has_gpaw"})


# ── AGOX compat shims (ase 3.29 namespace, numpy 2.5 scalar strictness) ──


def test_compat_shim_reexports_ase_constraint_names() -> None:
    """ase 3.29 hid IndexedConstraint/slice2enlist behind ase.constraints.constraint;
    AGOX imports them from ase.constraints. Idempotent."""
    import ase.constraints as ase_constraints

    from precis.structure.search import _agox_compat_shims

    _agox_compat_shims()
    _agox_compat_shims()
    assert hasattr(ase_constraints, "IndexedConstraint")
    assert hasattr(ase_constraints, "slice2enlist")


def test_compat_shim_makes_gpr_lml_gradient_scalar(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapped gradient returns a float even when AGOX's einsum hands back a
    shape-(1,) array (numpy 2.5 makes float() of that a TypeError); wrapping twice
    does not stack."""
    import sys
    import types

    from precis.structure.search import _agox_compat_shims

    class FakeGPR:
        def _log_marginal_likelihood_gradient(self, theta: Any) -> tuple[Any, Any]:
            return np.array([-9.12]), np.zeros(3)

    fake_mod = types.ModuleType("agox.models.GPR.GPR")
    fake_mod.GPR = FakeGPR  # type: ignore[attr-defined]
    for name in ("agox", "agox.models", "agox.models.GPR"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    monkeypatch.setitem(sys.modules, "agox.models.GPR.GPR", fake_mod)

    _agox_compat_shims()
    first = FakeGPR._log_marginal_likelihood_gradient
    _agox_compat_shims()
    assert FakeGPR._log_marginal_likelihood_gradient is first
    p, grad = FakeGPR()._log_marginal_likelihood_gradient(np.zeros(3))
    assert isinstance(p, float) and p == pytest.approx(-9.12)
    assert grad.shape == (3,)


def test_ray_tmp_dir_is_short_enough_for_unix_sockets() -> None:
    """ray's plasma socket lives ~70 bytes below the temp dir; AF_UNIX caps the
    whole path at 107. The cluster scratch workdir blew that (job 366190)."""
    from precis.structure.search import _ray_tmp_dir

    path = str(_ray_tmp_dir())
    assert len(path) + 70 < 107, path
    assert path.endswith(f"precis-ray-{__import__('os').getpid()}")
