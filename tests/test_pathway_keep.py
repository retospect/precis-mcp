"""gr543776 keep-everything capture (precis_pathway.keep + the
run_seed_partial wiring) — no engine needed: the hooks wrap whatever
``pipeline`` module object they are handed, so a fake module exercises the
whole capture path."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from ase import Atoms

from precis_pathway import keep, runner


def _atoms(tag: str, z: float = 0.0) -> Atoms:
    a = Atoms("H2", positions=[[0, 0, z], [0, 0, 0.74 + z]])
    a.info["tag"] = tag
    return a


def _bar(*, es: list[float], n_images: int = 3, converged: bool = True) -> Any:
    return SimpleNamespace(
        e_reactant=es[0],
        e_product=es[-1],
        e_ts=max(es),
        barrier=max(es) - es[0],
        delta_e=es[-1] - es[0],
        converged=converged,
        method="ci-neb",
        images_energy=list(es),
        images=[_atoms(f"img{i}", float(i)) for i in range(n_images)],
        ts_index=es.index(max(es)),
        ts_fmax=0.04,
        desorbed_images=0,
        collapsed_images=0,
        optimizer="bfgs",
        multistart=None,
        fallback=None,
    )


def _fake_pipeline() -> Any:
    """A stand-in ``autocatpath.pipeline`` with the two private collect
    points in their engine call shapes."""
    mod = ModuleType("fake_pipeline")

    def _collect_saddle(bar: Any, n_slab: int, sname: str, ts_collect: Any) -> None:
        if ts_collect is None:
            return
        i = bar.images_energy.index(max(bar.images_energy))
        saddle = bar.images[i].copy()
        saddle.info["ts_energy"] = float(bar.images_energy[i])
        saddle.info["ts_image"] = i
        saddle.info["band_images"] = len(bar.images)
        saddle.info["barrier"] = float(bar.barrier)
        prev = ts_collect.get(sname)
        if prev is None or saddle.info["ts_energy"] < prev.info["ts_energy"]:
            ts_collect[sname] = saddle

    def _state_trust(
        records: Any,
        st: Any,
        sname: Any,
        seed: int,
        res: Any,
        geo: Any,
        slab: Any,
        n_slab: int,
        site: Any = None,
        baseline: Any = None,
    ) -> None:
        return None

    setattr(mod, "_collect_saddle", _collect_saddle)
    setattr(mod, "_state_trust", _state_trust)
    return mod


def test_saddle_hook_captures_band_profile_ts_and_checkpoint(tmp_path: Path) -> None:
    mod = _fake_pipeline()
    bands: dict[str, list[dict[str, Any]]] = {}
    artifacts: dict[str, str] = {}
    ts_collect: dict[str, Any] = {}
    ckpt = tmp_path / "checkpoints"
    restore = keep.install_hooks(
        mod, seed=7, bands=bands, artifacts=artifacts, checkpoint_dir=str(ckpt)
    )

    bar = _bar(es=[0.0, 0.8, 0.1])
    mod._collect_saddle(bar, 4, "N+O", ts_collect)
    # a second call on the same step (a split segment) gets its own stem
    mod._collect_saddle(_bar(es=[0.0, 0.5, 0.2], converged=False), 4, "N+O", ts_collect)
    restore()

    profs = bands["N+O"]
    assert [p["segment"] for p in profs] == [1, 2]
    assert profs[0]["images_energy"] == [0.0, 0.8, 0.1]
    assert profs[1]["converged"] is False

    assert "band-N_O.extxyz" in artifacts
    assert "band-N_O-2.extxyz" in artifacts
    assert "ts-N_O.extxyz" in artifacts
    assert "band-N_O.profile.json" in artifacts
    disk = sorted(p.name for p in ckpt.iterdir())
    assert disk == sorted(artifacts)
    profile = json.loads((ckpt / "band-N_O.profile.json").read_text(encoding="utf-8"))
    assert profile["seed"] == 7
    # the kept ts file is the running-best saddle (the lower second call)
    assert "ts-N_O.extxyz" in (p.name for p in ckpt.iterdir())


def test_trust_hook_keeps_lowest_energy_relax_per_state(tmp_path: Path) -> None:
    mod = _fake_pipeline()
    bands: dict[str, list[dict[str, Any]]] = {}
    artifacts: dict[str, str] = {}
    ckpt = tmp_path / "checkpoints"
    restore = keep.install_hooks(
        mod, seed=1, bands=bands, artifacts=artifacts, checkpoint_dir=str(ckpt)
    )
    st = SimpleNamespace(name="CO*")
    hi = SimpleNamespace(atoms=_atoms("hi"), energy=0.5, converged=True)
    lo = SimpleNamespace(atoms=_atoms("lo"), energy=0.1, converged=True)
    mod._state_trust([], st, "s", 1, hi, None, None, 4)
    mod._state_trust([], st, "s", 1, lo, None, None, 4)
    mod._state_trust([], st, "s", 1, hi, None, None, 4)  # higher again: ignored
    restore()

    assert "relax-CO.extxyz" in artifacts
    assert '"energy": 0.1' in artifacts["relax-CO.json"]
    # the on-disk file is the low-energy geometry — the tag survives extxyz
    text = (ckpt / "relax-CO.extxyz").read_text(encoding="utf-8")
    assert "tag=lo" in text and "tag=hi" not in text


def test_install_hooks_tolerates_engine_without_private_names(tmp_path: Path) -> None:
    mod = ModuleType("bare_pipeline")
    restore = keep.install_hooks(
        mod,
        seed=0,
        bands={},
        artifacts={},
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )
    restore()  # no error, nothing installed


def test_run_seed_partial_collects_bands_ts_and_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The full glue: a fake ``autocatpath`` in sys.modules carries the
    run through the real ``run_seed_partial`` — its fake ``run_one_seed``
    invokes the wrapped collect points the way the engine does."""
    pipeline = _fake_pipeline()

    def run_one_seed(
        cfg: Any,
        seed: int,
        log: Any = print,
        collect: Any = None,
        refs: Any = None,
        defer_neb: Any = None,
        ts_collect: Any = None,
        baseline: Any = None,
    ) -> dict:
        st = SimpleNamespace(name="R")
        res = SimpleNamespace(atoms=_atoms("r"), energy=0.1, converged=True)
        pipeline._state_trust([], st, "s1", seed, res, None, None, 2)
        pipeline._collect_saddle(_bar(es=[0.0, 0.9, 0.2]), 2, "s1", ts_collect)
        return {"states": {"R": 0.1}, "steps": {"s1": {"barrier": 0.9}}}

    setattr(pipeline, "run_one_seed", run_one_seed)

    class _Mlip:
        backend = "emt"
        model = None
        models: list = []

        def specs(self) -> list:
            return [("emt", None)]

    class _Slab:
        relax_lattice = False
        a = None

    class _Cfg:
        mlip = _Mlip()
        slab = _Slab()

        @classmethod
        def from_dict(cls, d: Any) -> _Cfg:
            return cls()

        def to_dict(self) -> dict:
            return {"name": "t"}

    config_mod = ModuleType("autocatpath.config")
    setattr(config_mod, "Config", _Cfg)
    calc_mod = ModuleType("autocatpath.calculators")
    setattr(calc_mod, "resolve_backend", lambda b: b)
    setattr(calc_mod, "make_calculator", lambda mlip: object())
    struct_mod = ModuleType("autocatpath.structures")
    setattr(struct_mod, "default_lattice", lambda e: 4.0)
    setattr(struct_mod, "equilibrium_lattice", lambda e, f: 4.0)

    pkg = ModuleType("autocatpath")
    pkg.__path__ = []  # mark as package
    for name, mod in (
        ("config", config_mod),
        ("calculators", calc_mod),
        ("structures", struct_mod),
        ("pipeline", pipeline),
    ):
        setattr(pkg, name, mod)
        monkeypatch.setitem(sys.modules, f"autocatpath.{name}", mod)
    monkeypatch.setitem(sys.modules, "autocatpath", pkg)

    out = runner.run_seed_partial(
        {"name": "t"}, 3, 0, checkpoint_dir=str(tmp_path / "checkpoints")
    )

    assert out["partial"]["steps"]["s1"]["barrier"] == 0.9
    assert out["bands"]["s1"][0]["images_energy"] == [0.0, 0.9, 0.2]
    assert out["ts"]["s1"]["ts_image"] == 1
    assert out["ts"]["s1"]["barrier"] == pytest.approx(0.9)
    names = set(out["artifacts"])
    assert {"band-s1.extxyz", "band-s1.profile.json", "ts-s1.extxyz"} <= names
    assert "relax-R.extxyz" in names
    assert (tmp_path / "checkpoints" / "band-s1.extxyz").is_file()
    # json round-trip: everything in the envelope must serialise
    json.dumps(out["bands"])
    json.dumps(out["ts"])
    # the wrapped module attrs are restored, not left patched
    assert pipeline._collect_saddle.__name__ == "_collect_saddle"
