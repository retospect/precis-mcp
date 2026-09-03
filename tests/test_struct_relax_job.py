"""``struct_relax`` job_type — the cache↔relax seam.

Proves the seam end-to-end *without a cluster*: a stubbed container run (the
:data:`RUNNER` hook writes a fake ``result.json``) drives the dispatch, which
records the **run-cube** — and a subsequent ``StructureHandler`` relax of the
same geometry is then a zero-compute cache hit that writes back the relaxed
positions. Compute happens once, ever; everything after is a lookup.
"""

from __future__ import annotations

import json
import shlex
import subprocess
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
        Path(out_dir, "result.json").write_text(json.dumps(result), encoding="utf-8")
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


def test_dispatch_self_aborts_on_wall_clock_timeout(structure, tmp_path, monkeypatch):
    """A GPU-driver-wedged relax must self-abort on the wall-clock cap rather
    than hang forever — kill the container, attempt a GPU reset, and record
    an ``infra`` failure (no run-cube row). Gripe 171381."""
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)
    monkeypatch.setattr(struct_relax, "STAGER", lambda rid: _stage(tmp_path, rid))

    def _hanging_runner(
        argv: list[str],
        *,
        node: str,
        in_dir: str,
        out_dir: str,
        timeout: float | None = None,
    ) -> tuple[int, str]:
        raise subprocess.TimeoutExpired(cmd=["docker", "run"], timeout=1)

    monkeypatch.setattr(struct_relax, "RUNNER", _hanging_runner)

    call_order: list[str] = []
    kill_calls: list[tuple[int, str | None]] = []
    reset_calls: list[str | None] = []

    def _fake_kill_container(
        ref_id: int, *, node: str | None = None, **kw: Any
    ) -> bool:
        call_order.append("kill")
        kill_calls.append((ref_id, node))
        return True

    def _fake_reset_gpu(*, node: str | None = None, **kw: Any) -> bool:
        call_order.append("reset")
        reset_calls.append(node)
        return True

    monkeypatch.setattr(struct_relax, "kill_container", _fake_kill_container)
    monkeypatch.setattr(struct_relax, "reset_gpu", _fake_reset_gpu)

    ctx, events = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    assert kill_calls == [(params["structure_ref_id"], struct_relax._NODE)]
    assert reset_calls == [struct_relax._NODE]
    assert call_order == ["kill", "reset"]  # container force-removed before GPU reset

    fails = [payload for k, payload in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "infra"
    assert ("status", "succeeded") not in events
    assert structure.store.structure_find_cached_run(params["cache_key"]) is None


def test_relax_timeout_s_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_DFT_RELAX_TIMEOUT_S", raising=False)
    assert struct_relax._relax_timeout_s() == float(
        struct_relax._RELAX_TIMEOUT_S_DEFAULT
    )

    monkeypatch.setenv("PRECIS_DFT_RELAX_TIMEOUT_S", "7200")
    assert struct_relax._relax_timeout_s() == 7200.0

    monkeypatch.setenv("PRECIS_DFT_RELAX_TIMEOUT_S", "1")  # below the floor
    assert struct_relax._relax_timeout_s() == 60.0

    monkeypatch.setenv("PRECIS_DFT_RELAX_TIMEOUT_S", "not-a-number")
    assert struct_relax._relax_timeout_s() == float(
        struct_relax._RELAX_TIMEOUT_S_DEFAULT
    )


def test_reset_gpu_local_vs_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)

    monkeypatch.setenv("PRECIS_NODE", "spark")
    assert struct_relax.reset_gpu(node="spark") is True
    assert calls[-1] == ["nvidia-smi", "--gpu-reset"]

    monkeypatch.setenv("PRECIS_NODE", "caspar")
    assert struct_relax.reset_gpu(node="spark") is True
    assert calls[-1] == ["ssh", "spark", "nvidia-smi --gpu-reset"]

    def raising_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        raise OSError("nvidia-smi not found")

    monkeypatch.setattr(struct_relax.subprocess, "run", raising_run)
    assert struct_relax.reset_gpu(node="spark") is False


def test_no_dft_node_helpers_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """No node arg + PRECIS_DFT_NODE unset (``_NODE is None``): the container
    helpers no-op instead of ssh-ing a node literal that no longer exists —
    no subprocess is ever spawned."""
    monkeypatch.setattr(struct_relax, "_NODE", None)
    calls: list[list[str]] = []

    def fake_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)
    assert struct_relax.kill_container(123) is False
    assert struct_relax.reset_gpu() is False
    assert struct_relax.reap_stale_containers() == 0
    assert calls == []


def test_dispatch_without_any_target_node_fails_infra(
    structure, tmp_path, monkeypatch
) -> None:
    """Params without ``target_node`` and no PRECIS_DFT_NODE fallback: the
    dispatch records a self-describing infra failure instead of staging a
    relax with nowhere to run."""
    structure.put(id="pd_pair", text=_PD)
    params = _build_params(structure)
    assert "target_node" not in params

    monkeypatch.setattr(struct_relax, "_NODE", None)
    ctx, events = _fake_ctx(structure.store, params)
    struct_relax._dispatch(ctx, struct_relax.SPEC)

    fails = [e for e in events if e[0] == "fail"]
    assert len(fails) == 1
    assert fails[0][1]["failure_class"] == "infra"
    assert "PRECIS_DFT_NODE" in fails[0][1]["reason"]


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


# ── container reap (gripe 50905) ──────────────────────────────────────────


def test_kill_container_local_runs_docker_rm(monkeypatch: pytest.MonkeyPatch) -> None:
    """When this worker *is* the DFT node, ``kill_container`` shells out to
    ``docker rm -f precis-job-<ref_id>`` directly (no ssh hop)."""
    monkeypatch.setenv("PRECIS_NODE", "spark")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)
    monkeypatch.setattr(struct_relax, "_NODE", "spark")

    ok = struct_relax.kill_container(42, node="spark")

    assert ok is True
    assert calls == [["docker", "rm", "-f", "precis-job-42"]]


def test_kill_container_remote_hops_via_ssh(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the job's node isn't this worker, the kill is ssh'd to the node —
    the container runs on the remote GPU box, not the sweeper's host. The
    remote command is a single shell-quoted argv element (not exploded into
    separate ssh argv items) so the remote shell's IFS re-split can't break
    a token apart."""
    monkeypatch.setenv("PRECIS_NODE", "caspar")
    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)

    ok = struct_relax.kill_container(42, node="spark")

    assert ok is True
    assert calls == [["ssh", "spark", "docker rm -f precis-job-42"]]


def test_kill_container_never_raises_on_subprocess_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A docker/ssh failure (binary missing, node unreachable, …) is
    swallowed — the sweeper must never crash on a best-effort kill."""

    def raising_run(argv, **kw):
        raise OSError("no such host")

    monkeypatch.setattr(struct_relax.subprocess, "run", raising_run)

    assert struct_relax.kill_container(42, node="spark") is False


def test_parse_docker_created_handles_docker_format() -> None:
    dt = struct_relax._parse_docker_created("2026-07-22 10:15:32 +0000 UTC")
    assert dt is not None
    assert dt.year == 2026 and dt.month == 7 and dt.day == 22
    assert struct_relax._parse_docker_created("garbage") is None


def test_reap_stale_containers_kills_old_not_fresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The watchdog force-removes a ``precis-job-*`` container past the age
    threshold and leaves a fresh one alone — never touches anything else."""
    monkeypatch.setenv("PRECIS_NODE", "spark")
    monkeypatch.setattr(struct_relax, "_NODE", "spark")

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=60)).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    fresh_ts = (now - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    ps_output = f"precis-job-1\t{old_ts}\nprecis-job-2\t{fresh_ts}\n"

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, ps_output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)

    reaped = struct_relax.reap_stale_containers(max_age_hours=6.0)

    assert reaped == 1
    rm_calls = [c for c in calls if c[1] == "rm"]
    assert rm_calls == [["docker", "rm", "-f", "precis-job-1"]]


def test_remote_argv_quotes_a_token_containing_a_tab() -> None:
    """``ssh host a b c`` re-joins args with a plain space and the remote
    shell re-splits on IFS (which includes tab) — so a raw tab inside a
    ``--format`` token silently breaks it in two once it crosses the ssh
    hop. ``_remote_argv`` must shell-quote so the round trip through the
    remote shell reconstructs the exact original tokens."""
    argv = ["docker", "ps", "--format", "{{.Names}}\t{{.CreatedAt}}"]
    remote = struct_relax._remote_argv("spark", argv)

    assert remote[:2] == ["ssh", "spark"]
    assert len(remote) == 3  # the whole remote command is ONE argv element
    # Simulate the remote shell's word-split — it must reconstruct exactly
    # the original tokens, tab and all.
    assert shlex.split(remote[2]) == argv


def test_reap_stale_containers_over_ssh_kills_old_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The stale-container watchdog also works over the remote (ssh) branch
    — listing and killing both survive the remote shell's word-split now
    that the command is shell-quoted (gripe 50905 follow-up)."""
    monkeypatch.setenv("PRECIS_NODE", "caspar")  # this worker is NOT spark

    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC)
    old_ts = (now - timedelta(hours=60)).strftime("%Y-%m-%d %H:%M:%S +0000 UTC")
    ps_output = f"precis-job-9\t{old_ts}\n"

    calls: list[list[str]] = []

    def fake_run(argv, **kw):
        calls.append(argv)
        assert argv[0] == "ssh" and argv[1] == "spark"
        assert len(argv) == 3  # one shell-quoted remote command string
        remote_argv = shlex.split(argv[2])
        if remote_argv[1] == "ps":
            return subprocess.CompletedProcess(argv, 0, ps_output, "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(struct_relax.subprocess, "run", fake_run)

    reaped = struct_relax.reap_stale_containers(max_age_hours=6.0, node="spark")

    assert reaped == 1
    assert len(calls) == 2  # one ps, one rm
    rm_argv = shlex.split(calls[1][2])
    assert rm_argv == ["docker", "rm", "-f", "precis-job-9"]


def test_reap_stale_containers_never_raises_on_listing_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raising_run(argv, **kw):
        raise OSError("docker not found")

    monkeypatch.setattr(struct_relax.subprocess, "run", raising_run)

    assert struct_relax.reap_stale_containers(max_age_hours=6.0, node="spark") == 0


def _stage(tmp_path, ref_id: int) -> tuple[str, str]:
    base = Path(tmp_path) / f"job-{ref_id}"
    (base / "in").mkdir(parents=True, exist_ok=True)
    (base / "out").mkdir(parents=True, exist_ok=True)
    return str(base / "in"), str(base / "out")
