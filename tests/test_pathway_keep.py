"""gr543776 keep-everything capture (precis_pathway.keep + the
run_seed_partial wiring) — no engine needed: the hooks wrap whatever
``pipeline`` module object they are handed, so a fake module exercises the
whole capture path."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest
from ase import Atoms
from ase.io import read as ase_read

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
    points in their engine call shapes (items 1–2)."""
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


def _fake_engine(monkeypatch: pytest.MonkeyPatch) -> tuple[Any, Any]:
    """A fake ``autocatpath.pipeline`` + its ``neb`` module carrying the
    private call shapes items 4–6 wrap: ``neb_barrier`` fans out to
    ``neb._neb_attempt`` per start and per denser retry (both looked up
    as module attrs, so the keep hooks see them), ``_run_neb`` runs a
    primary band plus an auto-retry, ``_split_and_stamp`` relaxes the dip
    image and runs two segment bands, ``_relax_state_bound`` runs a
    desorbed plain relax, a failed reseat and a successful one priced by
    ``_adsorption_barrier``, and ``_pool_run_nebs`` exercises
    ``neb_barrier_pool`` plus its retry call."""
    pipe = _fake_pipeline()
    neb = ModuleType("fake_neb")

    def _neb_attempt(
        reactant: Any,
        product: Any,
        make_calc: Any,
        n_images: int,
        fmax: float,
        max_steps: int,
        climb: bool,
        tether: Any = None,
        n_slab: Any = None,
        optimizer: str = "bfgs",
        batched_eval_factory: Any = None,
        init_seed: int = 0,
        jitter: float = 0.0,
    ) -> Any:
        e_r = float(reactant.info.get("e", 0.0))
        e_p = float(product.info.get("e", 0.0))
        span = max(1, n_images - 1)
        es = [
            e_r + (e_p - e_r) * i / span + (0.5 if 0 < i < n_images - 1 else 0.0)
            for i in range(n_images)
        ]
        images = [_atoms(f"s{init_seed}-img{i}", float(i)) for i in range(n_images)]
        return es, True, images

    def _bar_from(es: list[float], images: list[Any]) -> Any:
        b = _bar(es=es, n_images=len(images))
        b.images = images
        return b

    def neb_barrier(
        reactant: Any,
        product: Any,
        make_calc: Any = None,
        n_images: int = 5,
        fmax: float = 0.1,
        max_steps: int = 100,
        climb: bool = True,
        retries: int = 0,
        n_slab: Any = None,
        tether: Any = None,
        optimizer: str = "bfgs",
        batched_eval_factory: Any = None,
        path_seeds: int = 1,
        path_jitter: float = 0.05,
        fallback_optimizer: Any = None,
    ) -> Any:
        last: Any = None
        for s in range(max(1, path_seeds)):
            last = neb._neb_attempt(
                reactant,
                product,
                make_calc,
                n_images,
                fmax,
                max_steps,
                climb,
                optimizer=optimizer,
                init_seed=s,
            )
            for _ in range(retries):
                last = neb._neb_attempt(
                    reactant,
                    product,
                    make_calc,
                    n_images + 2,
                    fmax,
                    max_steps * 2,
                    climb,
                    optimizer=optimizer,
                    init_seed=s,
                )
        assert last is not None
        es, _conv, images = last
        return _bar_from(list(es), list(images))

    neb_barrier.__module__ = "fake_neb"
    setattr(neb, "_neb_attempt", _neb_attempt)
    setattr(neb, "neb_barrier", neb_barrier)
    setattr(pipe, "neb_barrier", neb_barrier)

    def neb_barrier_pool(jobs: Any, make_calc: Any = None, **kw: Any) -> Any:
        return [neb.neb_barrier(r, p) for r, p in jobs]

    setattr(neb, "neb_barrier_pool", neb_barrier_pool)
    setattr(pipe, "neb_barrier_pool", neb_barrier_pool)

    def relax(atoms: Any, calc: Any, **kw: Any) -> Any:
        return SimpleNamespace(
            atoms=_atoms("intermediate", 0.3), energy=-0.4, converged=True
        )

    setattr(pipe, "relax", relax)

    def _run_neb(
        cfg: Any,
        sname: str,
        r_atoms: Any,
        p_atoms: Any,
        n_slab: int,
        seed: int,
        entry: dict,
        warnings: list,
        log: Any = print,
        ts_collect: Any = None,
        trust: Any = None,
        shed: Any = None,
    ) -> None:
        pipe.neb_barrier(r_atoms, p_atoms)  # the primary band, discarded
        retry = pipe.neb_barrier(r_atoms, p_atoms)  # the auto-retry
        pipe._collect_saddle(retry, n_slab, sname, ts_collect)
        entry["barrier"] = retry.barrier

    setattr(pipe, "_run_neb", _run_neb)

    def _split_and_stamp(
        cfg: Any,
        sname: str,
        seed: int,
        bar: Any,
        dip: int,
        r_atoms: Any,
        p_atoms: Any,
        n_slab: int,
        entry: dict,
        warnings: list,
        log: Any,
        ts_collect: Any,
        trust: Any,
        shed: Any,
        retried: bool,
    ) -> bool:
        mres = pipe.relax(bar.images[dip], None)
        mres.atoms.positions += 100.0  # in-place reuse, like the engine
        for a, b in ((r_atoms, mres.atoms), (mres.atoms, p_atoms)):
            seg = pipe.neb_barrier(a, b)
            pipe._collect_saddle(seg, n_slab, sname, ts_collect)
        return True

    setattr(pipe, "_split_and_stamp", _split_and_stamp)

    def _relax_state(state: Any, slab: Any, n_slab: int, cfg: Any, seed: int) -> Any:
        res = SimpleNamespace(
            atoms=_atoms(f"plain-{state.name}", 0.0), energy=0.3, converged=True
        )
        geo = SimpleNamespace(
            ok=False,
            anchor_height=3.1,
            adsorbate_height=3.6,
            reasons=["detached"],
        )
        return res, geo

    _reseat_calls: list[int] = []

    def _dissolve_reseat(
        start: Any, n_slab: int, cfg: Any, k: Any, rt: Any, ramp: Any
    ) -> Any:
        n = len(_reseat_calls)
        _reseat_calls.append(n)
        res = SimpleNamespace(
            atoms=_atoms(f"reseat-{n}", 0.1 + n * 0.1),
            energy=0.1,
            converged=True,
        )
        geo = SimpleNamespace(
            ok=n > 0,
            anchor_height=1.2 if n > 0 else 2.9,
            adsorbate_height=1.5 if n > 0 else 3.3,
            reasons=[],
        )
        return res, geo

    def _adsorption_barrier(
        desorbed: Any, bound: Any, cfg: Any, n_images: int = 5
    ) -> float:
        bar = pipe.neb_barrier(desorbed, bound, n_images=n_images)
        return max(0.0, bar.barrier)

    def _relax_state_bound(
        state: Any, slab: Any, n_slab: int, cfg: Any, seed: int
    ) -> Any:
        res, geo = pipe._relax_state(state, slab, n_slab, cfg, seed)
        diag = {
            "state": state.name,
            "rt": 1.8,
            "k": 2.0,
            "plain_anchor": 3.1,
            "plain_worst": 3.6,
            "attempts": [],
            "reseated": False,
            "ads_barrier": None,
        }
        desorbed = res.atoms
        res.atoms.positions += 100.0  # in-place reuse, like the engine
        best = (res, geo)
        for _attempt in range(2):
            res2, geo2 = pipe._dissolve_reseat(desorbed, n_slab, cfg, 2.0, 1.8, [1.0])
            res2.atoms.positions += 100.0  # in-place reuse
            ads = (
                pipe._adsorption_barrier(desorbed, res2.atoms, cfg) if geo2.ok else 0.0
            )
            diag["attempts"].append(
                {
                    "anchor": geo2.anchor_height,
                    "worst": geo2.adsorbate_height,
                    "ok": bool(geo2.ok),
                    "ads_barrier": round(ads, 3),
                }
            )
            if geo2.ok:
                diag["reseated"] = True
                diag["ads_barrier"] = round(ads, 3)
                return res2, geo2, True, diag
            best = (res2, geo2)
        return best[0], best[1], False, diag

    setattr(pipe, "_relax_state", _relax_state)
    setattr(pipe, "_dissolve_reseat", _dissolve_reseat)
    setattr(pipe, "_adsorption_barrier", _adsorption_barrier)
    setattr(pipe, "_relax_state_bound", _relax_state_bound)

    def _pool_run_nebs(jobs: list[dict], log: Any = print) -> None:
        bars = pipe.neb_barrier_pool([(j["r"], j["p"]) for j in jobs])
        # the pooled auto-retry replaces the first attempt of job 0
        retry = pipe.neb_barrier_pool([(jobs[0]["r"], jobs[0]["p"])])
        bars[0] = retry[0]
        for j, bar in zip(jobs, bars):
            pipe._collect_saddle(bar, j["n_slab"], j["sname"], j.get("ts_collect"))
            j["entry"]["barrier"] = bar.barrier

    setattr(pipe, "_pool_run_nebs", _pool_run_nebs)
    monkeypatch.setitem(sys.modules, "fake_neb", neb)
    return pipe, neb


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
    _stub_autocatpath(monkeypatch, pipeline)

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


# ── items 3–6 ──────────────────────────────────────────────────────────


def _stub_autocatpath(monkeypatch: pytest.MonkeyPatch, pipeline: Any) -> None:
    """Register a fake ``autocatpath`` package whose ``pipeline`` is the
    given module — enough for ``run_seed_partial`` to drive it."""
    config_mod = ModuleType("autocatpath.config")

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
        def from_dict(cls, d: Any) -> Any:
            return cls()

        def to_dict(self) -> dict:
            return {"name": "t"}

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


def test_run_neb_attempts_kept_by_kind_and_index(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 5: every discarded NEB attempt's band and energies — the
    primary call's rungs and the auto-retry call's — labelled by attempt
    kind and index."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    ckpt = tmp_path / "checkpoints"
    restore = keep.install_hooks(
        pipe, seed=3, bands={}, artifacts=artifacts, checkpoint_dir=str(ckpt)
    )

    r = _atoms("r")
    r.info["e"] = 0.0
    p = _atoms("p")
    p.info["e"] = 0.2
    entry: dict[str, Any] = {}
    pipe._run_neb(None, "A+B", r, p, 4, 3, entry, [])
    restore()

    assert entry["barrier"] == pytest.approx(0.65)
    for tag in ("a1", "retry2"):
        stem = f"att-A_B-{tag}-s0-bfgs-r1"
        assert f"{stem}.extxyz" in artifacts
        meta = json.loads(artifacts[f"{stem}.attempt.json"])
        assert meta["scope"] == "step"
        assert meta["name"] == "A+B"
        assert meta["call_tag"] == tag
        assert meta["init_seed"] == 0 and meta["rung"] == 1
        assert meta["images_energy"] == [0.0, 0.55, 0.6, 0.65, 0.2]
        assert (ckpt / f"{stem}.extxyz").is_file()
    assert json.loads(artifacts["att-A_B-a1-s0-bfgs-r1.attempt.json"])["call"] == 1
    assert json.loads(artifacts["att-A_B-retry2-s0-bfgs-r1.attempt.json"])["call"] == 2
    # the reported band still lands through the saddle collect
    assert "band-A_B.extxyz" in artifacts


def test_split_keeps_minimum_and_segment_bands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 4: the hidden-intermediate repair keeps ``mres.atoms`` (the
    newly found minimum), both segment bands (rung files ``seg1``/``seg2``)
    and the split index."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    restore = keep.install_hooks(
        pipe,
        seed=2,
        bands={},
        artifacts=artifacts,
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    bar = _bar(es=[0.0, 0.9, 0.3, 0.8, 0.1])
    out = pipe._split_and_stamp(
        None,
        "A+B",
        2,
        bar,
        2,
        _atoms("r"),
        _atoms("p"),
        4,
        {},
        [],
        print,
        {},
        None,
        None,
        False,
    )
    restore()

    assert out is True
    assert "split-A_B-min.extxyz" in artifacts
    idx = json.loads(artifacts["split-A_B.json"])
    assert idx["kind"] == "hidden_intermediate_split"
    assert idx["dip_image"] == 3
    assert idx["intermediate_energy"] == pytest.approx(-0.4)
    assert idx["stamped"] is True
    assert "att-A_B-seg1-s0-bfgs-r1.extxyz" in artifacts
    assert "att-A_B-seg2-s0-bfgs-r1.extxyz" in artifacts
    assert "intermediate" in artifacts["split-A_B-min.extxyz"]


def test_reseat_keeps_geometries_heights_and_adsorption_band(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 6: desorbed plain-relax geometry, the failed reseat attempt's
    geometry, the adsorption band, and the heights as data — not only
    warning text."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    restore = keep.install_hooks(
        pipe,
        seed=1,
        bands={},
        artifacts=artifacts,
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    state = SimpleNamespace(name="CO")
    res, geo, bound, diag = pipe._relax_state_bound(state, None, 4, None, 1)
    restore()

    assert bound is True
    assert "reseat-CO-plain.extxyz" in artifacts
    assert "reseat-CO-fail-1.extxyz" in artifacts
    # the successful reseat is not duplicated — the relax capture has it
    assert "reseat-CO-fail-2.extxyz" not in artifacts
    # the adsorption band rides the adsorption scope
    assert "att-CO-ads1-s0-bfgs-r1.extxyz" in artifacts
    data = json.loads(artifacts["reseat-CO.json"])
    assert data["bound"] is True
    assert data["diag"]["plain_anchor"] == 3.1
    assert data["diag"]["attempts"][0]["anchor"] == 2.9
    assert data["diag"]["attempts"][1]["ads_barrier"] == pytest.approx(0.5)
    assert "plain-CO" in artifacts["reseat-CO-plain.extxyz"]


def test_pool_attempts_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Item 5 (pooled): every ``neb_barrier_pool`` call's returned bands
    are kept per job, the retry call labelled poolretry."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    restore = keep.install_hooks(
        pipe,
        seed=4,
        bands={},
        artifacts=artifacts,
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    jobs = [
        {
            "sname": "A+B",
            "seed": 4,
            "r": _atoms("r"),
            "p": _atoms("p"),
            "n_slab": 4,
            "entry": {},
            "ts_collect": {},
        },
        {
            "sname": "C+D",
            "seed": 4,
            "r": _atoms("r2"),
            "p": _atoms("p2"),
            "n_slab": 4,
            "entry": {},
            "ts_collect": {},
        },
    ]
    pipe._pool_run_nebs(jobs)
    restore()

    assert "att-A_B-pool-0.extxyz" in artifacts
    assert "att-C_D-pool-1.extxyz" in artifacts
    assert "att-A_B-poolretry2-0.extxyz" in artifacts
    meta = json.loads(artifacts["att-A_B-poolretry2-0.json"])
    assert meta["kind"] == "pool-attempt"
    assert meta["pool_call"] == 2


def test_step_retry_keeps_full_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 3: a step-retry seed computes the whole network and narrows
    to one step — the discarded full run rides the artifact store as
    ``full-run.json``."""
    pipeline, _neb = _fake_engine(monkeypatch)

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
        return {
            "states": {"*": 0.0, "R": 0.1, "P": 0.3},
            "steps": {
                "s1": {"barrier": 0.9, "reactant": "R", "product": "P"},
                "s2": {"barrier": 1.1, "reactant": "*", "product": "R"},
            },
            "warnings": [],
            "trust": [],
        }

    setattr(pipeline, "run_one_seed", run_one_seed)
    _stub_autocatpath(monkeypatch, pipeline)
    monkeypatch.setattr(runner, "root_state", lambda _c: "*")

    out = runner.run_seed_partial(
        {"name": "t"},
        3,
        0,
        only_steps=["s1"],
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    assert set(out["partial"]["steps"]) == {"s1"}
    full = json.loads(out["artifacts"]["full-run.json"])
    assert set(full["steps"]) == {"s1", "s2"}
    assert set(full["states"]) == {"*", "R", "P"}
    assert (tmp_path / "checkpoints" / "full-run.json").is_file()


def test_results_byte_identical_with_and_without_capture(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The hooks are retention-only: a fixture run's reported results
    are identical with the capture active and with it a no-op."""
    pipeline, _neb = _fake_engine(monkeypatch)

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
        r = _atoms("r")
        r.info["e"] = 0.0
        p = _atoms("p")
        p.info["e"] = 0.2
        entry: dict[str, Any] = {}
        pipeline._run_neb(None, "s1", r, p, 2, seed, entry, [], ts_collect=ts_collect)
        if collect is not None:
            collect["R"] = (0.1, res.atoms.copy())
        return {
            "states": {"R": 0.1},
            "steps": {"s1": {"barrier": entry["barrier"]}},
        }

    setattr(pipeline, "run_one_seed", run_one_seed)
    _stub_autocatpath(monkeypatch, pipeline)

    captured = runner.run_seed_partial(
        {"name": "t"}, 5, 0, checkpoint_dir=str(tmp_path / "checkpoints")
    )
    monkeypatch.setattr(runner, "_install_keep_hooks", lambda *a, **k: lambda: None)
    plain = runner.run_seed_partial({"name": "t"}, 5, 0)

    for key in (
        "partial",
        "structures",
        "lattice",
        "weights_sha256",
        "model",
        "model_index",
        "seed",
    ):
        assert json.dumps(captured[key], sort_keys=True, default=str) == json.dumps(
            plain[key], sort_keys=True, default=str
        ), key


def test_failed_artifact_write_never_fails_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed artifact write warns and continues — retention never
    fails the job."""
    pipeline, _neb = _fake_engine(monkeypatch)

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
        r = _atoms("r")
        r.info["e"] = 0.0
        p = _atoms("p")
        p.info["e"] = 0.2
        entry: dict[str, Any] = {}
        pipeline._run_neb(None, "s1", r, p, 2, seed, entry, [], ts_collect=ts_collect)
        return {"states": {"R": 0.1}, "steps": {"s1": entry}}

    setattr(pipeline, "run_one_seed", run_one_seed)
    _stub_autocatpath(monkeypatch, pipeline)

    def _boom(*a: Any, **k: Any) -> str:
        raise OSError("disk full")

    monkeypatch.setattr(keep, "_frames_extxyz", _boom)
    monkeypatch.setattr(keep, "_atoms_to_extxyz", _boom)

    out = runner.run_seed_partial(
        {"name": "t"}, 5, 0, checkpoint_dir=str(tmp_path / "checkpoints")
    )

    assert out["partial"]["steps"]["s1"]["barrier"] == pytest.approx(0.65)
    # rung metadata still recorded (only the geometry writes failed)
    assert "att-s1-a1-s0-bfgs-r1.attempt.json" in out["artifacts"]


def test_second_pool_flush_keeps_both_attempts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second ``_pool_run_nebs`` restarts the pool-call count — the
    dup guard keeps both flushes' attempts under distinct keys."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    restore = keep.install_hooks(
        pipe,
        seed=4,
        bands={},
        artifacts=artifacts,
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    jobs = [
        {
            "sname": "A+B",
            "seed": 4,
            "r": _atoms("r"),
            "p": _atoms("p"),
            "n_slab": 4,
            "entry": {},
            "ts_collect": {},
        }
    ]
    pipe._pool_run_nebs(jobs)
    pipe._pool_run_nebs(jobs)
    restore()

    assert "att-A_B-pool-0.extxyz" in artifacts
    assert "att-A_B-pool-0-dup1.extxyz" in artifacts
    assert "att-A_B-poolretry2-0.extxyz" in artifacts
    assert "att-A_B-poolretry2-0-dup1.extxyz" in artifacts
    assert json.loads(artifacts["att-A_B-pool-0-dup1.json"])["pool_call"] == 1


def test_kept_geometry_immune_to_later_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaf captures serialise at leaf-return time: the fake engine
    mutates the returned Atoms in place (+100 A) before the enclosing
    call ends — the kept geometry must hold the leaf-time positions."""
    pipe, _neb = _fake_engine(monkeypatch)
    artifacts: dict[str, str] = {}
    restore = keep.install_hooks(
        pipe,
        seed=1,
        bands={},
        artifacts=artifacts,
        checkpoint_dir=str(tmp_path / "checkpoints"),
    )

    state = SimpleNamespace(name="CO")
    pipe._relax_state_bound(state, None, 4, None, 1)
    bar = _bar(es=[0.0, 0.9, 0.3, 0.8, 0.1])
    pipe._split_and_stamp(
        None,
        "A+B",
        2,
        bar,
        2,
        _atoms("r"),
        _atoms("p"),
        4,
        {},
        [],
        print,
        {},
        None,
        None,
        False,
    )
    restore()

    def _z0(name: str) -> float:
        a = ase_read(io.StringIO(artifacts[name]), format="extxyz")
        assert isinstance(a, Atoms)
        return float(a.positions[0, 2])

    assert _z0("reseat-CO-plain.extxyz") == pytest.approx(0.0)
    assert _z0("reseat-CO-fail-1.extxyz") == pytest.approx(0.1)
    assert _z0("split-A_B-min.extxyz") == pytest.approx(0.3)
