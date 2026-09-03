"""``struct_relax`` job_type — a relax at an energy rung, sinking to the run-cube.

The §23.16 cache and the relax backend used to be two ships passing in the
night: the ``structure`` handler's cache-first lookup reads the run-cube
(``struct_runs`` keyed on ``cache_key``), but nothing *populated* it from an
async relax. This job_type is that seam.

Per the structure atomistic IR it is a **thin precis-mcp job_type** that runs the
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

**Container runtime.** The original design anticipated podman + CDI, but the deployed
spark node runs ``docker`` with the NVIDIA Container Toolkit and the
``precis-dft`` image was validated there with ``--gpus all`` — so the default
matches reality. ``PRECIS_DFT_CONTAINER_CMD`` (``docker`` | ``podman``) flips
the GPU flag (``--gpus all`` vs CDI ``--device nvidia.com/gpu=all``) when the
node migrates.

**Container reap.** :func:`kill_container` (active reap) and
:func:`reap_stale_containers` (stale-container watchdog, both invoked from
the sweeper) close gripe 50905 — the container is deterministically named
(``precis-job-<ref_id>``) so it can be found and force-removed by name even
after its owning job's DB row is gone, rather than holding the GPU
indefinitely.

**Self-abort.** :func:`_dispatch` also caps the runner call at
:func:`_relax_timeout_s` (default well under the 6h stale-container
watchdog), so a GPU-driver-wedged relax self-aborts on its own instead of
waiting for the watchdog: the container is force-removed and a
:func:`reset_gpu` (``nvidia-smi --gpu-reset``) is attempted, before the
nightly-reboot last resort (gripe 171381).
"""

from __future__ import annotations

import json
import logging
import os
import shlex
import subprocess
from datetime import UTC, datetime
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
#: Deploy renders PRECIS_DFT_NODE from topology (precis_capabilities.dft);
#: deliberately no node-literal default — a hardcoded node outlives the node
#: it names (2026-08-29 spark retirement). ``None`` ⇒ this host can't resolve
#: a DFT target: the container helpers no-op, dispatch records an infra
#: failure.
_NODE = os.environ.get("PRECIS_DFT_NODE") or None
_IMAGE = os.environ.get("PRECIS_DFT_IMAGE", "precis-dft:cpu")
#: The Linux DFT node mounts caspar's export at /shared (macOS nodes use
#: /opt/shared); the container runs on the node, so the bind paths must be
#: valid there.
_NFS_ROOT = os.environ.get("PRECIS_DFT_NFS_ROOT", "/shared")
_CONTAINER_CMD = os.environ.get("PRECIS_DFT_CONTAINER_CMD", "docker")
_CONTAINER_IN = "/work/in"
_CONTAINER_OUT = "/work/out"
_RESULT_FILE = "result.json"
#: Deterministic container-name prefix (see :func:`build_run_argv`) — the
#: convention both the active reap (:func:`kill_container`, called from the
#: sweeper on a DB-row timeout) and the stale-container watchdog
#: (:func:`reap_stale_containers`) match on, so neither ever touches a
#: container that isn't a ``struct_relax`` compute job (gripe 50905).
_CONTAINER_PREFIX = "precis-job-"

#: Age past which an orphaned ``precis-job-*`` container is force-removed
#: regardless of its owning job's DB row — belt-and-suspenders for a
#: container that outlives its row (row already swept, deleted, or the
#: worker that would have reaped it never came back). Well past any
#: legitimate relax wall-clock. ``PRECIS_DFT_STALE_CONTAINER_HOURS``.
_STALE_CONTAINER_HOURS_DEFAULT = 6.0

#: Wall-clock cap on the runner call itself (gripe 171381) — kept well under
#: the 6h stale-container watchdog above so the runner self-aborts BEFORE the
#: watchdog would reap it, rather than the two racing. Env-overridable for a
#: genuinely long CPU relax. ``PRECIS_DFT_RELAX_TIMEOUT_S``.
_RELAX_TIMEOUT_S_DEFAULT = 4 * 3600


def _stale_container_hours() -> float:
    raw = os.environ.get("PRECIS_DFT_STALE_CONTAINER_HOURS")
    try:
        return max(0.5, float(raw)) if raw else _STALE_CONTAINER_HOURS_DEFAULT
    except ValueError:
        return _STALE_CONTAINER_HOURS_DEFAULT


def _relax_timeout_s() -> float:
    raw = os.environ.get("PRECIS_DFT_RELAX_TIMEOUT_S")
    try:
        return max(60.0, float(raw)) if raw else float(_RELAX_TIMEOUT_S_DEFAULT)
    except ValueError:
        return float(_RELAX_TIMEOUT_S_DEFAULT)


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
    ``--name precis-job-<ref_id>`` so the sweeper can kill it by name (§23 #6;
    see :func:`kill_container` / :func:`reap_stale_containers`, gripe 50905).
    ``gpus=0`` omits the GPU flag (CPU fallback — same image)."""
    argv = [container_cmd, "run", "--rm", "--name", f"{_CONTAINER_PREFIX}{ref_id}"]
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


# ── container reap (gripe 50905) ──────────────────────────────────────────
#
# The relax runs on a remote GPU node (``ssh <node> docker run …``), so
# neither a dead ``ssh_node`` worker nor a swept DB row ever touches the
# actual container — the sweeper excludes ``ssh_node`` jobs from its
# timeout sweep entirely (that executor owns its own lease-steal recovery),
# which is exactly how a stuck ``gpaw-relax`` kept holding the GPU for ~56h
# after its row was already failed out. Two best-effort, never-raising
# hooks close the gap:
#
# * :func:`kill_container` — called immediately wherever a job's DB row
#   *is* transitioned to failed (the sweeper's generic timeout path, for
#   any executor it does sweep) instead of leaving the container for a
#   lazy per-boot reconcile.
# * :func:`reap_stale_containers` — a watchdog independent of any job row:
#   force-removes any ``precis-job-*`` container on the DFT node past a
#   safe age, covering the ``ssh_node``-exclusion gap above and any other
#   way a container could outlive its row.


def _remote_argv(target: str, argv: list[str]) -> list[str]:
    """Build an ``ssh`` argv that survives the remote shell's word-split.

    ``ssh host a b c`` re-joins its trailing args with a plain space and
    hands the result to the remote login shell, which then re-splits on
    IFS (space/tab/newline) — so any argv item containing whitespace (e.g.
    a ``--format`` string with an embedded tab) silently breaks in two once
    it crosses the ssh hop, even though it was one argv element locally.
    Shell-quoting each token before joining makes the remote re-split a
    no-op regardless of what's inside a token."""
    return ["ssh", target, shlex.join(argv)]


def kill_container(
    ref_id: int, *, node: str | None = None, container_cmd: str = _CONTAINER_CMD
) -> bool:
    """Best-effort force-remove of job ``ref_id``'s compute container by its
    deterministic ``precis-job-<ref_id>`` name (see :func:`build_run_argv`).

    Runs on ``node`` (default :data:`_NODE`) — locally when this worker
    *is* that node, over ``ssh`` otherwise, mirroring :func:`_default_runner`.
    Never raises: any ``docker``/``ssh`` failure is logged and swallowed so a
    caller (the sweeper) can invoke this unconditionally. Returns ``True``
    when the command was issued (not a guarantee the container existed —
    ``rm -f`` on a missing name is a harmless no-op)."""
    name = f"{_CONTAINER_PREFIX}{ref_id}"
    target = node or _NODE
    if target is None:
        log.warning(
            "struct_relax: kill_container %s skipped — no node given and "
            "PRECIS_DFT_NODE unset",
            name,
        )
        return False
    local = target == os.environ.get("PRECIS_NODE")
    argv = [container_cmd, "rm", "-f", name]
    cmd = argv if local else _remote_argv(target, argv)
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
        log.info("struct_relax: killed container %s on %s", name, target)
        return True
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "struct_relax: kill_container %s on %s failed", name, target, exc_info=True
        )
        return False


def reset_gpu(*, node: str | None = None, container_cmd: str = _CONTAINER_CMD) -> bool:
    """Best-effort ``nvidia-smi --gpu-reset`` on ``node`` (default
    :data:`_NODE`) — the escalation step between force-removing a wedged
    container (:func:`kill_container`) and the nightly-reboot last resort,
    for a relax that self-aborted on the wall-clock cap (gripe 171381).

    Runs locally when this worker *is* ``node``, over ``ssh`` otherwise,
    mirroring :func:`kill_container`. Never raises: any ``nvidia-smi``/``ssh``
    failure is logged and swallowed. ``nvidia-smi --gpu-reset`` commonly
    fails when the GPU is still held by the wedged process or when not
    root — that's expected, hence best-effort; a persistent wedge still
    needs the nightly reboot. Returns ``True`` iff the reset was issued and
    reported success."""
    target = node or _NODE
    if target is None:
        log.warning(
            "struct_relax: reset_gpu skipped — no node given and PRECIS_DFT_NODE unset"
        )
        return False
    local = target == os.environ.get("PRECIS_NODE")
    argv = ["nvidia-smi", "--gpu-reset"]
    cmd = argv if local else _remote_argv(target, argv)
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=60, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.warning("struct_relax: reset_gpu on %s failed", target, exc_info=True)
        return False
    if res.returncode != 0:
        log.warning(
            "struct_relax: nvidia-smi --gpu-reset on %s rc=%d stderr=%s",
            target,
            res.returncode,
            (res.stderr or "")[:500],
        )
        return False
    log.info("struct_relax: reset GPU on %s", target)
    return True


def _parse_docker_created(raw: str) -> datetime | None:
    """Parse a ``docker ps --format '{{.CreatedAt}}'`` timestamp (e.g.
    ``"2026-07-22 10:15:32 +0000 UTC"``) into an aware ``datetime``. Only the
    date/time/numeric-offset tokens are used; the trailing tz abbreviation is
    ignored. Returns ``None`` on anything unparseable (that container is
    skipped, never force-matched)."""
    parts = raw.strip().split()
    if len(parts) < 3:
        return None
    try:
        return datetime.strptime(
            f"{parts[0]} {parts[1]} {parts[2]}", "%Y-%m-%d %H:%M:%S %z"
        )
    except ValueError:
        return None


def reap_stale_containers(
    *,
    max_age_hours: float | None = None,
    node: str | None = None,
    container_cmd: str = _CONTAINER_CMD,
) -> int:
    """Force-remove every ``precis-job-*`` container on ``node`` (default
    :data:`_NODE`) older than ``max_age_hours`` (default
    :func:`_stale_container_hours`) — independent of its owning job's DB
    row. Belt-and-suspenders for gripe 50905. Never raises: a ``docker``/
    ``ssh`` failure (listing or removing) is logged and swallowed. Returns
    the count force-removed."""
    threshold = max_age_hours if max_age_hours is not None else _stale_container_hours()
    target = node or _NODE
    if target is None:
        # The sweeper calls this bare on every host each pass; a host without
        # a rendered PRECIS_DFT_NODE has no DFT containers to reap — quiet
        # no-op, not a warning per sweep.
        log.debug("struct_relax: reap_stale_containers skipped — no DFT node")
        return 0
    local = target == os.environ.get("PRECIS_NODE")
    list_argv = [
        container_cmd,
        "ps",
        "-a",
        "--filter",
        f"name={_CONTAINER_PREFIX}",
        "--format",
        "{{.Names}}\t{{.CreatedAt}}",
    ]
    cmd = list_argv if local else _remote_argv(target, list_argv)
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "struct_relax: reap_stale_containers: listing on %s failed",
            target,
            exc_info=True,
        )
        return 0
    if res.returncode != 0:
        return 0
    now = datetime.now(UTC)
    reaped = 0
    for line in (res.stdout or "").splitlines():
        parts = line.split("\t", 1)
        if len(parts) != 2:
            continue
        name = parts[0].strip()
        if not name.startswith(_CONTAINER_PREFIX):
            continue  # defensive — the --filter already scopes this
        created = _parse_docker_created(parts[1])
        if created is None:
            continue
        age_hours = (now - created).total_seconds() / 3600.0
        if age_hours < threshold:
            continue
        rm_argv = [container_cmd, "rm", "-f", name]
        rm_cmd = rm_argv if local else _remote_argv(target, rm_argv)
        try:
            subprocess.run(
                rm_cmd, capture_output=True, text=True, timeout=30, check=False
            )
            reaped += 1
            log.warning(
                "struct_relax: reaped stale container %s (age %.1fh > %.1fh threshold)",
                name,
                age_hours,
                threshold,
            )
        except (OSError, subprocess.SubprocessError):
            log.warning(
                "struct_relax: rm -f %s on %s failed", name, target, exc_info=True
            )
    return reaped


def _default_runner(
    argv: list[str],
    *,
    node: str,
    in_dir: str,
    out_dir: str,
    timeout: float | None = None,
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
        ctx.record_failure(
            f"struct_relax: malformed params ({exc})", failure_class="infra"
        )
        return
    model = params.get("model") or "mace_mp"
    steps = int(params.get("steps", 200))
    cell = params.get("cell") or None
    node = params.get("target_node") or _NODE
    if node is None:
        ctx.record_failure(
            "struct_relax: no target node — params carry no target_node and "
            "PRECIS_DFT_NODE is unset on this host (deploy renders it from "
            "topology precis_capabilities.dft)",
            failure_class="infra",
        )
        return

    in_dir, out_dir = STAGER(structure_ref_id)
    Path(in_dir, "POSCAR").write_text(poscar, encoding="utf-8")
    run_params: dict[str, Any] = {"fidelity": fidelity, "model": model, "steps": steps}
    # Variable-cell relax mode passes through to the container contract (absent
    # ⇒ atoms-only, the historical default the container already assumes).
    if cell:
        run_params["cell"] = cell
    Path(in_dir, "params.json").write_text(
        json.dumps(run_params, sort_keys=True), encoding="utf-8"
    )
    argv = build_run_argv(ref_id=structure_ref_id, in_dir=in_dir, out_dir=out_dir)
    ctx.append_chunk("job_event", f"relax[{fidelity}] on {node}: {' '.join(argv)}")

    try:
        rc, output = RUNNER(
            argv, node=node, in_dir=in_dir, out_dir=out_dir, timeout=_relax_timeout_s()
        )
    except subprocess.TimeoutExpired:
        # GPU-driver-wedged relax — self-abort rather than hang forever (or
        # wait for the 6h stale-container watchdog to reap it). Kill the
        # container first (frees the name for a retry), then attempt a GPU
        # reset (best-effort escalation before the nightly-reboot last
        # resort). Gripe 171381.
        log.warning(
            "struct_relax: relax exceeded %.0fs wall-clock cap — self-aborting",
            _relax_timeout_s(),
        )
        kill_container(structure_ref_id, node=node)
        reset_gpu(node=node)
        ctx.append_chunk(
            "job_event",
            f"relax[{fidelity}] on {node}: exceeded {_relax_timeout_s():.0f}s "
            "wall-clock cap — self-aborted (container force-removed, "
            "nvidia-smi --gpu-reset attempted)",
        )
        ctx.record_failure(
            f"struct_relax: relax exceeded {_relax_timeout_s():.0f}s wall-clock cap — "
            "self-aborted (container force-removed, nvidia-smi --gpu-reset attempted; "
            "nightly reboot is the last resort)",
            failure_class="infra",
        )
        return
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("struct_relax: runner raised", exc_info=True)
        ctx.record_failure(f"struct_relax: runner failed: {exc}", failure_class="infra")
        return
    ctx.append_chunk("job_event", f"container rc={rc}\n{output[-2000:]}")

    result_path = Path(out_dir) / _RESULT_FILE
    if rc != 0 or not result_path.exists():
        # The container itself didn't run to completion (crash, OOM-kill,
        # docker/ssh failure, …) — an INFRA failure, not a physical verdict on
        # the candidate. A genuine non-convergence still exits 0 and writes a
        # result.json (``ok: false`` below).
        ctx.record_failure(
            f"struct_relax: container rc={rc}, no {_RESULT_FILE} — see the log event",
            failure_class="infra",
        )
        return
    try:
        result = json.loads(result_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        ctx.record_failure(
            f"struct_relax: malformed {_RESULT_FILE}: {exc}", failure_class="infra"
        )
        return
    if not result.get("ok"):
        # The container ran to completion and the relax code itself reported
        # a genuine failure (e.g. non-convergence) — this IS a physical
        # verdict on the candidate, unlike the infra branches above.
        ctx.record_failure(
            f"struct_relax: relax reported failure: {result.get('error', 'unknown')}",
            failure_class="non-convergence",
        )
        return
    scalars = result.get("scalars") or {}
    if "E_tot" not in scalars:
        ctx.record_failure(
            "struct_relax: result.json missing scalars.E_tot", failure_class="infra"
        )
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


SPEC = JobTypeSpec(
    name="struct_relax",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "SPEC",
    "build_run_argv",
    "kill_container",
    "load",
    "reap_stale_containers",
    "reset_gpu",
]
