"""``struct_relax`` job_type — a relax at an energy rung, sinking to the run-cube.

The §23.16 cache and the relax backend used to be two ships passing in the
night: the ``structure`` handler's cache-first lookup reads the run-cube
(``struct_runs`` keyed on ``cache_key``), but nothing *populated* it from an
async relax. This job_type is that seam.

Per ADR 0043 §23.12 it is a **thin precis-mcp job_type** that runs the
``ml``/``gpaw`` relax as a ``code``-executor job over ``ssh_node`` → the GPU
node → the ``precis-dft`` compute container, and writes the **run-cube** (a
``struct_runs`` row + the convergence curve + the relaxed geometry on the row)
— *not* a ``dft_calculation`` (that kind stays precis-dft's; the kind-merge is
Slice 2). So a converged relax becomes a zero-compute cache hit for the next
identical ``(structure_sha, fidelity, model, params, code_version)`` request, on
this design or any other sharing the input geometry.

**Self-contained on purpose.** precis-mcp does not depend on precis-dft (the
dependency runs the other way), so this module mirrors precis-dft's *container
contract* — the same ``precis-dft-run gpaw-relax`` argv, the same staged
``POSCAR`` + ``params.json``, the same ``result.json`` shape — rather than
importing its host-side helpers. The one execution boundary (``ssh node
<container> run …``) is the module-level :data:`RUNNER` hook, swapped for a stub
in tests so the orchestration + write-back is exercised without a cluster.

**Container runtime.** ADR §23.12 anticipated podman + CDI, but the deployed
spark node runs ``docker`` with the NVIDIA Container Toolkit and the
``precis-dft`` image was validated there with ``--gpus all`` — so the default
matches reality. ``PRECIS_DFT_CONTAINER_CMD`` (``docker`` | ``podman``) flips
the GPU flag (``--gpus all`` vs CDI ``--device nvidia.com/gpu=all``) when the
node migrates.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any

from precis.utils.container_limits import container_limit_flags
from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Which structure ref the run is recorded under (per-design audit /
        # view='runs'); the cache lookup itself is global by cache_key.
        "structure_ref_id": {"type": "integer"},
        "on_version": {"type": "integer"},
        "fidelity": {"type": "string"},  # 'ml' | 'gpaw' | 'dft-fast' | …
        "model": {"type": ["string", "null"]},
        "steps": {"type": "integer"},
        # The §23.16 content address + the relaxed-geometry write-back ordering.
        "cache_key": {"type": "string"},
        "structure_sha": {"type": "string"},
        # canonical_order(scene) — final_geometry.frac is indexed by this rank.
        "order": {"type": "array", "items": {"type": "string"}},
        # Labels in POSCAR row order (element-grouped, as to_poscar emits), so
        # the relaxed POSCAR's rows map back to labels → canonical rank.
        "poscar_labels": {"type": "array", "items": {"type": "string"}},
        # The staged input geometry (VASP POSCAR, Direct coords).
        "poscar": {"type": "string"},
        # The GPU node this relax pins itself to — the claim gate (§23 #3)
        # ensures only that node's worker claims it, so the worker that
        # stages to NFS is the box the container runs on.
        "target_node": {"type": ["string", "null"]},
    },
    "required": [
        "structure_ref_id",
        "on_version",
        "fidelity",
        "cache_key",
        "structure_sha",
        "order",
        "poscar_labels",
        "poscar",
    ],
    "additionalProperties": True,
}

COMPATIBLE_EXECUTORS = frozenset({"ssh_node"})
#: Satisfied by EXECUTOR_PROVIDES['ssh_node'] == {'has_gpaw'}.
REQUIRES = frozenset({"has_gpaw"})
DESCRIPTION = (
    "Relax a structure at an energy rung on the GPU node; sink to the run-cube."
)

# ── container contract (mirrors precis-dft.jobs.gpaw_relax) ──────────────
_NODE = os.environ.get("PRECIS_DFT_NODE", "spark")
_IMAGE = os.environ.get("PRECIS_DFT_IMAGE", "precis-dft:cpu")
#: Spark mounts caspar's export at /shared (macOS nodes use /opt/shared); the
#: container runs on the node, so the bind paths must be valid there.
_NFS_ROOT = os.environ.get("PRECIS_DFT_NFS_ROOT", "/shared")
_CONTAINER_CMD = os.environ.get("PRECIS_DFT_CONTAINER_CMD", "docker")
_CONTAINER_IN = "/work/in"
_CONTAINER_OUT = "/work/out"
_RESULT_FILE = "result.json"


def _gpu_flags(container_cmd: str) -> list[str]:
    """GPU passthrough flags for the runtime. docker uses the nvidia runtime
    hook (``--gpus all``); podman uses CDI (``--device nvidia.com/gpu=all``)."""
    if container_cmd == "podman":
        return ["--device", "nvidia.com/gpu=all"]
    return ["--gpus", "all"]


def build_run_argv(
    *,
    ref_id: int,
    in_dir: str,
    out_dir: str,
    image: str = _IMAGE,
    container_cmd: str = _CONTAINER_CMD,
    gpus: int = 1,
) -> list[str]:
    """The container ``run`` argv ssh'd to the node (pure). Deterministic
    ``--name precis-job-<ref_id>`` so the sweeper can kill it by name (§23 #6).
    ``gpus=0`` omits the GPU flag (CPU fallback — same image)."""
    argv = [container_cmd, "run", "--rm", "--name", f"precis-job-{ref_id}"]
    argv += container_limit_flags()
    if gpus:
        argv += _gpu_flags(container_cmd)
    argv += [
        "-v",
        f"{in_dir}:{_CONTAINER_IN}:ro",
        "-v",
        f"{out_dir}:{_CONTAINER_OUT}",
        image,
        "precis-dft-run",
        "gpaw-relax",
        "--in",
        _CONTAINER_IN,
        "--out",
        _CONTAINER_OUT,
    ]
    return argv


def _default_runner(
    argv: list[str], *, node: str, in_dir: str, out_dir: str, timeout: int | None = None
) -> tuple[int, str]:
    """Run the container ``argv`` on ``node``; return ``(returncode,
    combined_output)``. When this worker *is* the target node (the node gate
    co-locates them — §23 #3), run the container directly; otherwise ssh to the
    node. The single execution boundary — tests swap :data:`RUNNER` for a stub
    that writes a fake ``result.json`` into ``out_dir`` so the orchestration +
    write-back runs without a cluster."""
    local = node == os.environ.get("PRECIS_NODE")
    cmd = argv if local else ["ssh", node, *argv]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, proc.stdout + proc.stderr


def _default_stager(ref_id: int, *, nfs_root: str = _NFS_ROOT) -> tuple[str, str]:
    """``(in_dir, out_dir)`` under the shared scratch tree, created. On NFS so
    the same paths resolve on the claiming worker and on the node."""
    base = Path(nfs_root) / "scratch" / f"precis-job-{ref_id}"
    in_dir, out_dir = base / "in", base / "out"
    in_dir.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)
    return str(in_dir), str(out_dir)


#: Overridable hooks (tests monkeypatch these). The runner is the cluster
#: boundary; the stager is the NFS boundary.
RUNNER = _default_runner
STAGER = _default_stager


def _parse_poscar_frac(poscar: str) -> list[list[float]]:
    """Fractional coords from a VASP POSCAR (Direct), robust to VASP4/5.

    Layout: comment, scale, 3 lattice rows, [symbols], counts, [Selective
    dynamics], coord-mode, then one row per atom. We read the first three
    floats of each atom row (Direct = fractional). Cartesian is not expected
    (the container relaxes in Direct), so we trust the mode line is Direct."""
    lines = [ln for ln in poscar.splitlines()]
    idx = 5  # after comment(0), scale(1), lattice(2,3,4)
    toks = lines[idx].split()
    if toks and not toks[0].lstrip("-").isdigit():  # VASP5 element-symbols line
        idx += 1
    counts = [int(x) for x in lines[idx].split()]
    n = sum(counts)
    idx += 1
    if lines[idx].strip()[:1].lower() == "s":  # Selective dynamics
        idx += 1
    idx += 1  # the Direct / Cartesian line
    coords: list[list[float]] = []
    for k in range(n):
        parts = lines[idx + k].split()
        coords.append([float(parts[0]), float(parts[1]), float(parts[2])])
    return coords


def _final_geometry(
    relaxed_poscar: str, poscar_labels: list[str], order: list[str]
) -> dict[str, Any] | None:
    """Map the relaxed POSCAR's rows (element-grouped ``poscar_labels`` order)
    onto the canonical ``order`` the run-cube stores frac by. Returns None on a
    count mismatch (geometry not applied; the scalar envelope still caches) —
    mirroring ``cache.apply_geometry``'s count-guard."""
    coords = _parse_poscar_frac(relaxed_poscar)
    if len(coords) != len(poscar_labels) or len(poscar_labels) != len(order):
        return None
    by_label = {lbl: coords[i] for i, lbl in enumerate(poscar_labels)}
    return {"frac": [by_label[lbl] for lbl in order], "lattice": None}


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``ssh_node`` for a claimed job. Stages the
    geometry, runs the relax in the container on the GPU node, parses the
    result, and records the run-cube. ``ctx`` is a
    :class:`~precis.workers.executors._context.DispatchContext`."""
    params = (ctx.meta or {}).get("params") or {}
    try:
        structure_ref_id = int(params["structure_ref_id"])
        on_version = int(params["on_version"])
        fidelity = str(params["fidelity"])
        cache_key = str(params["cache_key"])
        structure_sha = str(params["structure_sha"])
        order = list(params["order"])
        poscar_labels = list(params["poscar_labels"])
        poscar = str(params["poscar"])
    except (KeyError, TypeError, ValueError) as exc:
        ctx.record_failure(f"struct_relax: malformed params ({exc})")
        return
    model = params.get("model") or "mace_mp"
    steps = int(params.get("steps", 200))
    cell = params.get("cell") or None
    node = params.get("target_node") or _NODE

    in_dir, out_dir = STAGER(structure_ref_id)
    Path(in_dir, "POSCAR").write_text(poscar)
    run_params: dict[str, Any] = {"fidelity": fidelity, "model": model, "steps": steps}
    # Variable-cell relax mode passes through to the container contract (absent
    # ⇒ atoms-only, the historical default the container already assumes).
    if cell:
        run_params["cell"] = cell
    Path(in_dir, "params.json").write_text(json.dumps(run_params, sort_keys=True))
    argv = build_run_argv(ref_id=structure_ref_id, in_dir=in_dir, out_dir=out_dir)
    ctx.append_chunk("job_event", f"relax[{fidelity}] on {node}: {' '.join(argv)}")

    try:
        rc, output = RUNNER(argv, node=node, in_dir=in_dir, out_dir=out_dir)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("struct_relax: runner raised", exc_info=True)
        ctx.record_failure(f"struct_relax: runner failed: {exc}")
        return
    ctx.append_chunk("job_event", f"container rc={rc}\n{output[-2000:]}")

    result_path = Path(out_dir) / _RESULT_FILE
    if rc != 0 or not result_path.exists():
        ctx.record_failure(
            f"struct_relax: container rc={rc}, no {_RESULT_FILE} — see the log event"
        )
        return
    try:
        result = json.loads(result_path.read_text())
    except json.JSONDecodeError as exc:
        ctx.record_failure(f"struct_relax: malformed {_RESULT_FILE}: {exc}")
        return
    if not result.get("ok"):
        ctx.record_failure(
            f"struct_relax: relax reported failure: {result.get('error', 'unknown')}"
        )
        return
    scalars = result.get("scalars") or {}
    if "E_tot" not in scalars:
        ctx.record_failure("struct_relax: result.json missing scalars.E_tot")
        return

    curve = list(result.get("curve") or scalars.get("force_curve") or [])
    n_steps = int(scalars.get("n_steps", len(curve)))
    final_geometry = None
    relaxed_poscar = result.get("relaxed_poscar")
    if relaxed_poscar:
        final_geometry = _final_geometry(relaxed_poscar, poscar_labels, order)
        if final_geometry is None:
            ctx.append_chunk(
                "job_event",
                "warn: relaxed geometry row/label count mismatch — caching the "
                "energy envelope without geometry write-back",
            )

    run_id = ctx.store.structure_record_run(
        structure_ref_id,
        fidelity=fidelity,
        on_version=on_version,
        converged=bool(scalars.get("converged", True)),
        n_steps=n_steps,
        max_disp=float(scalars.get("max_disp", 0.0) or 0.0),
        energy=float(scalars["E_tot"]),
        max_force=scalars.get("max_force"),
        model=model,
        curve=curve,
        cache_key=cache_key,
        structure_sha=structure_sha,
        final_geometry=final_geometry,
    )
    ctx.set_meta(
        struct_run_id=run_id, cache_key=cache_key, energy=float(scalars["E_tot"])
    )
    ctx.append_chunk(
        "job_summary",
        f"relax[{fidelity}] converged: E_tot={scalars['E_tot']:.4f} eV in "
        f"{n_steps} steps → run-cube #{run_id} (cache_key {cache_key[:12]}…). "
        f"The next identical relax is a zero-compute cache hit.",
    )
    ctx.set_status("succeeded")


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("struct_relax runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="struct_relax",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "build_run_argv", "load"]
