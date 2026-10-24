"""Kinetics v2 slice 10 glue (docs/backlog/kinetics-engine-v2.md D10): the
validation manifest stamp, ``autocatpath_revalidate`` and
``view='manifest'``.

The acceptance replays use the trimmed pw455722 fixture
(``tests/fixtures/pw455722_results.json``, NO -> NH3 on Pd(111)): the
"stored" result is one chain solve of it, the re-run another — both on the
stub network ``tests/test_pathway_environment.py`` uses, so no calculator
and no slab are needed.  The macOS arm64 cross-platform check is a manual
post-deploy step (spec slice 10), not a test here.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

pytest.importorskip("autocatpath")
pytest.importorskip("autocatpath.manifest")  # engine >= 0.38

from autocatpath import kinetics
from autocatpath import manifest as engine_manifest
from autocatpath.config import Config

from precis_pathway import manifest as vm
from precis_pathway import revalidate_job
from precis_pathway.revalidate_job import Refused, revalidate

FIXTURE = Path(__file__).parent / "fixtures" / "pw455722_results.json"


@pytest.fixture(scope="module")
def fx() -> dict[str, Any]:
    with FIXTURE.open(encoding="utf-8") as fh:
        return json.load(fh)


class _Net:
    """The slice of ``Network`` ``kinetics.solve`` reads (as in
    test_pathway_environment)."""

    def __init__(self, fx: dict[str, Any]) -> None:
        self.links = [(a, b) for a, b, kind in fx["links"] if kind != "reaction"]
        self.steps = [
            SimpleNamespace(
                name=f"{a}->{b}",
                reactant=SimpleNamespace(name=a),
                product=SimpleNamespace(name=b),
            )
            for a, b, kind in fx["links"]
            if kind == "reaction"
        ]
        self._states = sorted(fx["nodes"])

    def order(self) -> list[str]:
        return ["*", *(s for s in self._states if s != "*")]

    def states(self) -> list[str]:
        return list(self._states)

    def slab(self) -> None:
        return None


@pytest.fixture
def lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, str]:
    p = tmp_path / "uv.lock"
    p.write_text("version = 1\n", encoding="utf-8")
    monkeypatch.setenv(vm.LOCK_ENV, str(p))
    return engine_manifest.sha256_file(p), "uv.lock"


@pytest.fixture
def solve(fx: dict[str, Any], monkeypatch: pytest.MonkeyPatch):
    """``solve(config) -> results``: one pw455722 chain solve, results-shaped."""
    monkeypatch.setattr(kinetics, "site_area_A2", lambda _slab: fx["site_area_A2"])
    net = _Net(fx)

    def run(config: dict[str, Any]) -> dict[str, Any]:
        _v1, v2 = kinetics.solve(
            Config.from_dict(config),
            fx["nodes"],
            fx["edges"],
            net,
            fx["refs"],
            span=fx["score"]["activity"]["span_eV"],
            log=lambda *a, **k: None,
        )
        return {
            "nodes": copy.deepcopy(fx["nodes"]),
            "edges": copy.deepcopy(fx["edges"]),
            "score": fx["score"],
            "kinetics_v2": v2,
        }

    return run


ENGINE_SHA = "b" * 40


@pytest.fixture
def stored(
    fx: dict[str, Any], solve, lock, monkeypatch: pytest.MonkeyPatch
) -> dict[str, Any]:
    """The pathway meta a pw455722-class aggregate on a stamped engine 0.38
    leaves (the installed engine's version and commit, pinned for the test)."""
    import autocatpath

    monkeypatch.setattr(autocatpath, "__version__", "0.38.0")
    monkeypatch.setattr(autocatpath, "__git_sha__", ENGINE_SHA, raising=False)
    config = copy.deepcopy(fx["config"])
    config["kinetics"]["n_samples"] = 0
    results = solve(config)
    rec = vm.build(
        config=config,
        config_snapshot_yaml=json.dumps(config, sort_keys=True),
        results=results,
        autocatpath_version="0.38.0",
    )
    assert rec is not None
    mid = vm.stamp_results(results, rec)
    return {
        "config": config,
        "results": results,
        "validation_manifest": rec,
        "manifest_id": mid,
    }


def _rerun(solve, scale: float = 1.0):
    def rerun(config, then, legs):
        now = solve(config)
        if scale != 1.0:
            now["kinetics_v2"]["tof_chain"] *= scale
        return now, ()

    return rerun


def _other_platform() -> Any:
    p = engine_manifest.capture_platform()
    arch = "x86_64" if p.arch == "arm64" else "arm64"
    return engine_manifest.Platform(
        arch, p.os, p.accelerator, p.torch, p.numpy, p.numba, p.blas
    )


# --- the stamp --------------------------------------------------------------------


def test_stamp_lands_on_the_record_and_is_stable(stored, fx, solve) -> None:
    mid = stored["manifest_id"]
    assert len(mid) == 64
    assert stored["results"]["kinetics_v2"]["manifest_id"] == mid
    assert stored["validation_manifest"]["manifest_id"] == mid
    # the kinetics job rebuilds from the same stored inputs: same id
    again = vm.build(
        config=stored["config"],
        config_snapshot_yaml=json.dumps(stored["config"], sort_keys=True),
        results=solve(stored["config"]),
        autocatpath_version="0.38.0",
    )
    assert again is not None and again["manifest_id"] == mid
    rec = stored["validation_manifest"]
    assert rec["lock"]["lock_source"] == "uv.lock"
    assert rec["seeds"]["kinetics_seed"] == fx["config"]["kinetics"]["mc_seed"]
    assert rec["inputs"]["network"]["template"] == "ammonia"


def test_constraints_lock_hashes_pins_not_the_export_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def sha(name: str, body: str) -> tuple[str | None, str | None]:
        p = tmp_path / name / "constraints.txt"
        p.parent.mkdir()
        p.write_text(body, encoding="utf-8")
        monkeypatch.setenv(vm.LOCK_ENV, str(p))
        return vm.lock_sha256()

    a = sha("a", "# uv export ... -o /tmp/tmpAAAA\nnumpy==2.5.0\nscipy==1.16.0\n")
    b = sha("b", "# uv export ... -o /tmp/tmpBBBB\nnumpy==2.5.0\nscipy==1.16.0\n")
    c = sha("c", "# uv export ... -o /tmp/tmpAAAA\nnumpy==2.5.1\nscipy==1.16.0\n")
    assert a == b and a[1] == "constraints.txt"
    assert c[0] != a[0]


# --- acceptance (spec §Slices item 10) -------------------------------------------


def test_same_platform_revalidation_reproduces_every_observable(
    stored, solve, lock
) -> None:
    """Every observable the CPU legs produce reproduces — and because the
    energies (GPU) and TPD legs were not re-run, the record says so: the
    verdict is partial and ``complete`` is false (NOT validated)."""
    entry = revalidate(stored, stored["manifest_id"], rerun=_rerun(solve), lock=lock)
    assert entry["verdict"] == "reproduced_partial(energies,tpd)"
    assert entry["complete"] is False
    assert entry["column"] == engine_manifest.SAME_PLATFORM
    compared = {o["observable"] for o in entry["observables"]}
    then = set(engine_manifest.observables(stored["results"])) - {"energies"}
    assert compared == then
    assert {"tof_chain", "tof_span_limit", "consistency"} <= compared
    assert all(o["ok"] for o in entry["observables"])
    assert entry["platform_then"] == entry["platform_now"]
    assert entry["not_run"] == ["energies", "tpd"]
    assert set(entry["not_run_why"]) == {"energies", "tpd"}
    assert entry["caveats"] == []


def test_only_a_run_of_every_leg_is_complete(stored, solve, lock) -> None:
    every = ("kinetics", "kmc", "sweep", "energies", "tpd")
    entry = revalidate(
        stored, stored["manifest_id"], rerun=_rerun(solve), lock=lock, legs=every
    )
    assert entry["verdict"] == "reproduced" and entry["complete"] is True
    assert "energies" in {o["observable"] for o in entry["observables"]}


def test_an_unstamped_engine_is_never_complete(stored, solve, lock) -> None:
    every = ("kinetics", "kmc", "sweep", "energies", "tpd")
    entry = revalidate(
        stored,
        stored["manifest_id"],
        rerun=_rerun(solve),
        lock=lock,
        legs=every,
        engine=("0.38.0", None),
    )
    assert entry["verdict"] == "reproduced_partial(engine-unstamped-now)"
    assert entry["complete"] is False


def test_an_empty_comparison_is_never_reproduced(stored, solve, lock) -> None:
    bare = copy.deepcopy(stored)
    bare["results"] = {"nodes": stored["results"]["nodes"]}  # only energies
    entry = revalidate(
        bare,
        stored["manifest_id"],
        rerun=lambda config, then, legs: (dict(then), ()),
        lock=lock,
    )
    assert entry["verdict"] == "failed(nothing-compared)"
    assert entry["complete"] is False


def test_engine_version_or_commit_mismatch_refuses(stored, solve, lock) -> None:
    with pytest.raises(
        Refused, match="autocatpath_version: manifest 0.38.0 != host 0.38.1"
    ):
        revalidate(
            stored,
            stored["manifest_id"],
            rerun=_rerun(solve),
            lock=lock,
            engine=("0.38.1", ENGINE_SHA),
        )
    with pytest.raises(
        Refused, match="autocatpath_sha: manifest bbbbbbbbbbbb != host 999999999999"
    ):
        revalidate(
            stored,
            stored["manifest_id"],
            rerun=_rerun(solve),
            lock=lock,
            engine=("0.38.0", "9" * 40),
        )


def test_a_drifted_rerun_is_not_reported_reproduced(stored, solve, lock) -> None:
    entry = revalidate(
        stored, stored["manifest_id"], rerun=_rerun(solve, 1.1), lock=lock
    )
    assert entry["verdict"] == "drifted(tof_chain)"
    row = next(o for o in entry["observables"] if o["observable"] == "tof_chain")
    assert row["delta"] == pytest.approx(0.0414, abs=1e-3) and row["tolerance"] == 0.01


def test_cross_platform_rerun_uses_the_manifest_cross_column(
    stored, solve, lock
) -> None:
    other = _other_platform()
    # +0.03 decades: past the same-platform 0.01, inside the cross-platform 0.05
    entry = revalidate(
        stored,
        stored["manifest_id"],
        rerun=_rerun(solve, 10**0.03),
        lock=lock,
        platform_now=other,
    )
    assert entry["column"] == engine_manifest.CROSS_PLATFORM
    assert entry["verdict"] == "reproduced_partial(energies,tpd)"
    assert entry["platform_now"]["arch"] != entry["platform_then"]["arch"]
    same = revalidate(
        stored, stored["manifest_id"], rerun=_rerun(solve, 10**0.03), lock=lock
    )
    assert same["verdict"] == "drifted(tof_chain)"


def test_lock_hash_mismatch_refuses_naming_the_mismatch(stored, solve) -> None:
    ran: list[bool] = []

    def rerun(config, then, legs):
        ran.append(True)
        return solve(config), ()

    with pytest.raises(
        Refused,
        match=r"uv_lock_sha256: manifest [0-9a-f]{12} \(uv.lock\) != host 000000000000",
    ):
        revalidate(
            stored, stored["manifest_id"], rerun=rerun, lock=("0" * 64, "uv.lock")
        )
    assert ran == []  # refused before any compute


def test_wrong_or_edited_manifest_refuses(stored, solve, lock) -> None:
    with pytest.raises(Refused, match="manifest_id mismatch"):
        revalidate(stored, "f" * 64, rerun=_rerun(solve), lock=lock)
    edited = copy.deepcopy(stored)
    edited["validation_manifest"]["seeds"]["kinetics_seed"] = 99
    with pytest.raises(Refused, match="record was edited"):
        revalidate(edited, stored["manifest_id"], rerun=_rerun(solve), lock=lock)


# --- view='manifest' over a real entry (engine-free view tests:
# test_pathway_manifest_glue.py) ----------------------------------------------------


def test_manifest_view_renders_a_real_entry(stored, solve, lock) -> None:
    text = vm.manifest_text(stored)
    assert f"manifest_id: {stored['manifest_id']}" in text
    assert "validated: NO" in text
    partial = revalidate(stored, stored["manifest_id"], rerun=_rerun(solve), lock=lock)
    drifted = revalidate(
        stored, stored["manifest_id"], rerun=_rerun(solve, 1.1), lock=lock
    )
    for i, e in enumerate((partial, drifted), 1):
        e.update(at="2026-10-24T00:00:00Z", job_id=i)
    meta = {**stored, "revalidation": {stored["manifest_id"]: [partial, drifted]}}
    text = vm.manifest_text(meta)
    assert "validated: NO" in text
    assert "job 1: reproduced_partial(energies,tpd) (complete: false)" in text
    assert "job 2: drifted(tof_chain) (complete: false) [same_platform]" in text
    assert "drift tof_chain:" in text
    assert "not run: energies — the min-energy-path leg" in text


# --- through the real store ----------------------------------------------------------


def _ref(store: Any, slug: str, meta: dict[str, Any]) -> Any:
    from precis_pathway.persist import _json_finite

    # NaN -> null, as pathway_meta stores it (the replay's DRC carries NaN)
    with store.tx() as conn:
        return store.insert_ref(
            kind="pathway",
            slug=slug,
            title=f"{slug} pathway",
            meta=_json_finite(meta),
            conn=conn,
        )


def test_dispatch_appends_the_verdict_to_the_pathway(
    store: Any, stored, solve, lock, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref = _ref(store, "reval-dispatch", stored)
    monkeypatch.setattr(revalidate_job, "rerun_cpu_legs", _rerun(solve))
    ctx = MagicMock()
    ctx.store, ctx.ref_id = store, 4242
    ctx.meta = {
        "params": {"pathway_ref_id": ref.id, "manifest_id": stored["manifest_id"]}
    }
    revalidate_job._dispatch(ctx, revalidate_job.SPEC)
    ctx.set_status.assert_called_with("succeeded")
    runs = store.get_ref(kind="pathway", id=ref.id).meta["revalidation"][
        stored["manifest_id"]
    ]
    assert [r["verdict"] for r in runs] == ["reproduced_partial(energies,tpd)"]
    assert runs[0]["complete"] is False
    assert ctx.set_meta.call_args.kwargs["complete"] is False
    assert runs[0]["job_id"] == 4242 and runs[0]["at"].endswith("Z")


def test_dispatch_refusal_names_the_lock_mismatch(
    store: Any, stored, lock, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ref = _ref(store, "reval-refuse", stored)
    other = tmp_path / "other.lock"
    other.write_text("version = 2\n", encoding="utf-8")
    monkeypatch.setenv(vm.LOCK_ENV, str(other))
    ctx = MagicMock()
    ctx.store = store
    ctx.meta = {
        "params": {"pathway_ref_id": ref.id, "manifest_id": stored["manifest_id"]}
    }
    revalidate_job._dispatch(ctx, revalidate_job.SPEC)
    reason = ctx.record_failure.call_args.args[0]
    assert reason.startswith(
        "autocatpath_revalidate: refused — uv_lock_sha256: manifest"
    )
    assert "revalidation" not in store.get_ref(kind="pathway", id=ref.id).meta


def test_put_mode_revalidate_mints_the_job_and_view_routes(store: Any, stored) -> None:
    from precis.dispatch import Hub
    from precis.errors import BadInput
    from precis_pathway.handler import PathwayHandler

    ref = _ref(
        store, "reval-put", {**stored, "graph": {"nodes": []}, "status": "ready"}
    )
    hub = Hub(store=store)
    h = PathwayHandler(hub=hub)
    h._register_with(hub)
    with pytest.raises(BadInput, match="is not this pathway's"):
        h.put(id="reval-put", mode="revalidate", args={"manifest": "0" * 64})
    resp = h.put(
        id="reval-put", mode="revalidate", args={"manifest": stored["manifest_id"]}
    )
    assert "re-validation of manifest" in resp.body
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'params'->>'manifest_id', meta->>'executor' FROM refs "
            "WHERE kind = 'job' AND parent_id = %s AND meta->>'job_type' = %s",
            (ref.id, "autocatpath_revalidate"),
        ).fetchone()
    assert tuple(row) == (stored["manifest_id"], "ssh_node")
    body = h.get(id="reval-put", view="manifest").body
    assert f"manifest_id: {stored['manifest_id']}" in body
    _ref(store, "reval-bare", {"results": {}, "status": "ready"})
    with pytest.raises(BadInput, match="carries no validation manifest"):
        h.put(id="reval-bare", mode="revalidate")
