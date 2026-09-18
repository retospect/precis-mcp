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

**Two backends, routed by rung (gr346449).** The precis-dft image exposes
exactly one subcommand, ``precis-dft-run gpaw-relax``, so it can only compute a
GPAW rung (:data:`_CONTAINER_FIDELITIES`). The MLIP rung
(:data:`_INPROC_FIDELITIES`) runs **in this process** on the node via
:func:`_default_ml_runner`, reusing
:func:`precis.structure.relax._ml_calculator` — the same backend a local
``relax(fidelity='ml')`` would use, already installed on the DFT node with
torch + CUDA. A rung in neither set fails the job: this dispatcher used to send
*every* fidelity to ``gpaw-relax``, so an ``ml`` request silently ran a
spin-polarized RPBE DFT relax and, if it ever finished inside the wall-clock
cap, recorded DFT energies in the run-cube under a MACE label. Running
different physics than was asked for is worse than not running.

**Self-contained on purpose.** precis-mcp does not depend on precis-dft (the
dependency runs the other way), so this module mirrors precis-dft's *container
contract* — the same argv, the same staged ``POSCAR`` + ``params.json``, the
same ``result.json`` shape — rather than importing its host-side helpers. Both
backends produce that same result shape and land on the one write-back,
:func:`_record_run`. The container execution boundary (``ssh node <container>
run …``) is the module-level :data:`RUNNER` hook and the in-process one is
:data:`ML_RUNNER`; both are swapped for stubs in tests so the orchestration +
write-back is exercised without a cluster.

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
indefinitely. Both VERIFY the removal (``docker ps -a`` on the exact
anchored name) rather than trusting ``rm -f``'s exit code alone — a wedged
dockerd/GPU can report success and lie (gripe 310809).

**Self-abort.** :func:`_dispatch` also caps the runner call at
:func:`_relax_timeout_s` (default well under the 6h stale-container
watchdog), so a GPU-driver-wedged relax self-aborts on its own instead of
waiting for the watchdog: :func:`kill_container` and :func:`reset_gpu`
(``nvidia-smi --gpu-reset``) are attempted, and the recorded event/failure
text is honest about whether the container actually came down — a failed
removal names the surviving container and the operator escalation
(``docker rm -f`` / the nightly-reboot last resort, gripe 171381) instead of
claiming success it can't back up.

**Pre-run guard.** Before staging a run, :func:`_dispatch` checks for a
stale ``precis-job-<ref_id>`` container left over from a prior attempt
(the deterministic name is the per-structure mutex, so a leftover
guarantees an ``rc=125`` name-conflict on every retry) and verified-kills
it; an un-removable stale container fails the job fast with an honest
infra event instead of colliding with ``docker run`` (gripe 310809).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shlex
import socket
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
        # 'ml' (in-process MLIP) | 'gpaw' (container). Anything else has no
        # backend and the dispatcher fails the job — see _CONTAINER_FIDELITIES.
        "fidelity": {"type": "string"},
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

#: Rungs the ``precis-dft`` container actually implements. Its CLI exposes
#: exactly one subcommand (``precis-dft-run gpaw-relax``), and the staged
#: ``params.json`` carries no per-rung DFT settings, so one rung is the whole
#: of what a container run can honestly compute. ``dft-fast``/``dft-tight``
#: are deliberately absent: routing them here would run byte-identical GPAW
#: defaults and file the results under two different rung labels — the same
#: class of lie as sending ``ml`` to ``gpaw-relax``. Adding them means
#: teaching the contract their settings first.
_CONTAINER_FIDELITIES = frozenset({"gpaw"})

#: Rungs this worker computes **in-process** on the node, with no container at
#: all — the MLIP backend is an ordinary precis dependency
#: (:func:`precis.structure.relax._ml_calculator`), already installed on the
#: DFT node alongside torch/CUDA.
_INPROC_FIDELITIES = frozenset({"ml"})

#: Force-convergence target for the in-process MLIP rung, matching the floor
#: :func:`precis.structure.relax._relax_ml` applies to its ``tol`` so a
#: dispatched ``ml`` relax converges on the same criterion as a local one.
#: The run-cube address does not carry it, so it must not drift per host.
_ML_FMAX = 0.05

#: Hosts an :data:`_INPROC_FIDELITIES` rung may pin to. The in-process path
#: needs only the MLIP wheel — no image, no GPU, no NFS staging — so it is not
#: confined to the single DFT node a ``gpaw`` run is. Comma-separated, first
#: non-empty wins: ``PRECIS_MLIP_NODES`` (ops override) →
#: ``PRECIS_AUTOCATPATH_ROUTE_NODE`` (already rendered on the minting daemons
#: from ``precis_capabilities.autocatpath``, which *is* the ``compute`` group:
#: every host running the ``job_ssh_node`` lane, each carrying torch + the MLIP
#: because autocatpath needs the same backend) → ``PRECIS_DFT_NODE`` alone,
#: i.e. exactly the historical behaviour on a host where neither is rendered.
_MLIP_NODES_ENV = "PRECIS_MLIP_NODES"
_AUTOCATPATH_NODES_ENV = "PRECIS_AUTOCATPATH_ROUTE_NODE"


def mlip_nodes() -> list[str]:
    """Hosts an in-process rung may pin to, sorted — the stable ordering is
    what makes :func:`target_node_for` reproducible across minting hosts.
    Empty ⇒ nothing is configured and the caller must refuse the mint."""
    for env in (_MLIP_NODES_ENV, _AUTOCATPATH_NODES_ENV):
        hosts = [h.strip() for h in (os.environ.get(env) or "").split(",") if h.strip()]
        if hosts:
            return sorted(hosts)
    node = os.environ.get("PRECIS_DFT_NODE") or ""
    return [node] if node else []


def target_node_for(fidelity: str, *, key: str) -> str | None:
    """The host to pin a relax of ``fidelity`` to; ``None`` ⇒ nothing is
    configured and the mint must refuse rather than name a ghost node.

    A container rung pins to ``PRECIS_DFT_NODE``: the image, the GPU and the
    NFS scratch the stager writes into all live on that one box. An in-process
    rung has none of those ties (gr346449), so it spreads over
    :func:`mlip_nodes` — deterministically on ``key`` (the run-cube cache key),
    so a re-dispatch of the same geometry lands back on the same host and its
    warm model cache instead of bouncing around the group.
    """
    dft = os.environ.get("PRECIS_DFT_NODE") or None
    if fidelity not in _INPROC_FIDELITIES:
        return dft
    hosts = mlip_nodes()
    if not hosts:
        return dft
    digest = hashlib.sha256(key.encode("utf-8")).digest()
    return hosts[int.from_bytes(digest[:4], "big") % len(hosts)]


#: ``-e OMP_NUM_THREADS=<n>`` for the container run (gr346449). The image bakes
#: ``OMP_NUM_THREADS=1``, and its GPAW is built without OpenMP, so this only
#: threads the BLAS calls — measured ~5x on a 4000^2 dgemm, worth having and
#: not worth a rebuild. Deliberately a small default rather than "all cores":
#: the node runs other work, and ``PRECIS_JOB_CPUSET`` may already have fenced
#: this container into a subset. ``0``/empty passes no flag (the image default).
_OMP_THREADS_DEFAULT = 4


#: ``mpirun -np <n>`` for the container run. GPAW's parallelism is MPI
#: (domain / k-point decomposition), not threads, so this — not
#: ``OMP_NUM_THREADS`` — is what makes a genuine ``gpaw`` rung use more than
#: one core. ``PRECIS_DFT_MPI_RANKS``; default **0 = off**, because an image
#: built before the OpenMPI rebuild has no ``mpirun`` and no MPI-capable
#: ``_gpaw``: turning this on against the old image would fail every run.
#: Flip it once the rebuilt image is on the node (precis-dft
#: docker/Dockerfile asserts MPI at build time, and ``result.json`` reports
#: the rank count it actually ran with).
_MPI_RANKS_DEFAULT = 0


def _mpi_ranks() -> int:
    raw = os.environ.get("PRECIS_DFT_MPI_RANKS")
    if raw is None:
        return _MPI_RANKS_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _MPI_RANKS_DEFAULT


def _omp_threads() -> int:
    raw = os.environ.get("PRECIS_DFT_OMP_THREADS")
    if raw is None:
        return _OMP_THREADS_DEFAULT
    try:
        return max(0, int(raw))
    except ValueError:
        return _OMP_THREADS_DEFAULT


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
    ``gpus=0`` omits the GPU flag (CPU fallback — same image). ``-e
    OMP_NUM_THREADS`` (:func:`_omp_threads`) overrides the image's baked ``1``
    so the run threads its BLAS calls; GPAW itself has no OpenMP, so that is a
    BLAS-only speedup. Actual parallel DFT is MPI: :func:`_mpi_ranks` > 0
    wraps the command in ``mpirun -np <n>``, which requires the MPI-enabled
    image (precis-dft) — hence off by default."""
    argv = [container_cmd, "run", "--rm", "--name", f"{_CONTAINER_PREFIX}{ref_id}"]
    argv += container_limit_flags()
    threads = _omp_threads()
    if threads:
        argv += ["-e", f"OMP_NUM_THREADS={threads}"]
    if gpus:
        argv += _gpu_flags(container_cmd)
    argv += [
        "-v",
        f"{in_dir}:{_CONTAINER_IN}:ro",
        "-v",
        f"{out_dir}:{_CONTAINER_OUT}",
        image,
    ]
    ranks = _mpi_ranks()
    if ranks:
        # --allow-run-as-root: the container's only user IS root, and OpenMPI
        # refuses to launch as root without it.
        argv += ["mpirun", "--allow-run-as-root", "-np", str(ranks)]
    argv += [
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


def _container_still_present(
    name: str, *, target: str, local: bool, container_cmd: str
) -> bool:
    """True iff a container named exactly ``name`` still lists (any state,
    ``docker ps -a``) on ``target``. Anchored (``^/<name>$`` — docker's
    internal name carries the leading ``/``) so ``precis-job-1`` never
    matches ``precis-job-12``. Used both to verify an ``rm -f`` actually
    took effect (:func:`kill_container`, :func:`reap_stale_containers`) and,
    in :func:`_dispatch`, to detect a stale container before a retry
    collides with it on the deterministic ``--name`` mutex (gripe 310809).

    Fails conservative: a listing failure (unreachable node, docker down)
    reports "still present" — the safer read for a mutex-collision guard,
    where a false negative (reporting gone when it isn't) would let a
    doomed retry through."""
    list_argv = [
        container_cmd,
        "ps",
        "-a",
        "--filter",
        f"name=^/{name}$",
        "--format",
        "{{.Names}}",
    ]
    cmd = list_argv if local else _remote_argv(target, list_argv)
    try:
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "struct_relax: presence check for %s on %s failed — assuming present",
            name,
            target,
            exc_info=True,
        )
        return True
    if res.returncode != 0:
        log.warning(
            "struct_relax: presence check for %s on %s rc=%d — assuming present",
            name,
            target,
            res.returncode,
        )
        return True
    return bool((res.stdout or "").strip())


def kill_container(
    ref_id: int, *, node: str | None = None, container_cmd: str = _CONTAINER_CMD
) -> bool:
    """Force-remove job ``ref_id``'s compute container by its deterministic
    ``precis-job-<ref_id>`` name (see :func:`build_run_argv`), and VERIFY it
    is actually gone (gripe 310809 — an ``rm -f`` that exits nonzero, or
    that lies about success on a wedged dockerd/GPU, used to be reported as
    a kill regardless).

    Runs on ``node`` (default :data:`_NODE`) — locally when this worker
    *is* that node, over ``ssh`` otherwise, mirroring :func:`_default_runner`.
    Never raises: any ``docker``/``ssh`` failure is logged and swallowed so a
    caller (the sweeper) can invoke this unconditionally. Returns ``True``
    only when the container is confirmed absent afterward — callers can now
    trust the return value instead of assuming success."""
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
        res = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30, check=False
        )
    except (OSError, subprocess.SubprocessError):
        log.warning(
            "struct_relax: kill_container %s on %s failed", name, target, exc_info=True
        )
        return False
    if res.returncode != 0:
        log.warning(
            "struct_relax: docker rm -f %s on %s rc=%d stderr=%s",
            name,
            target,
            res.returncode,
            (res.stderr or "")[:500],
        )
        return False
    if _container_still_present(
        name, target=target, local=local, container_cmd=container_cmd
    ):
        log.warning(
            "struct_relax: docker rm -f %s on %s exited 0 but the container still "
            "lists — treating as a failed kill",
            name,
            target,
        )
        return False
    log.info("struct_relax: killed container %s on %s", name, target)
    return True


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
            rm_res = subprocess.run(
                rm_cmd, capture_output=True, text=True, timeout=30, check=False
            )
        except (OSError, subprocess.SubprocessError):
            log.warning(
                "struct_relax: rm -f %s on %s failed", name, target, exc_info=True
            )
            continue
        if rm_res.returncode != 0:
            log.warning(
                "struct_relax: rm -f %s on %s rc=%d stderr=%s",
                name,
                target,
                rm_res.returncode,
                (rm_res.stderr or "")[:500],
            )
            continue
        if _container_still_present(
            name, target=target, local=local, container_cmd=container_cmd
        ):
            log.warning(
                "struct_relax: rm -f %s on %s exited 0 but the container still "
                "lists — treating as a failed reap",
                name,
                target,
            )
            continue
        reaped += 1
        log.warning(
            "struct_relax: reaped stale container %s (age %.1fh > %.1fh threshold)",
            name,
            age_hours,
            threshold,
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


class _RelaxDeadline(RuntimeError):
    """The in-process relax blew its wall-clock cap (see :func:`_relax_timeout_s`)."""


def _default_ml_runner(
    *,
    poscar: str,
    model: str,
    steps: int,
    cell: str | None,
    timeout: float,
) -> dict[str, Any]:
    """Relax at the MLIP rung **in this process**, returning the container
    contract's ``result.json`` shape (gr346449).

    There is no ML container: the precis-dft image implements ``gpaw-relax``
    and nothing else, so before this existed a ``fidelity='ml'`` job ran a
    spin-polarized RPBE DFT relax and recorded the answer under a MACE label.
    The MLIP backend is an ordinary precis dependency, already on the DFT node
    with torch + CUDA, so the honest fix is to compute the rung here rather
    than ship a second image.

    The calculator comes from :func:`precis.structure.relax._ml_calculator` so
    the model routing and the "[dft-ml] not installed" message have exactly one
    definition; a missing backend surfaces as ``RelaxUnsupported`` for the
    caller to class as infra. Constraints ride in on the POSCAR's *Selective
    dynamics* block, which is how the fixed-atom mask reached the container
    too, so a slab's frozen bottom layers stay frozen.
    """
    import io
    import time

    from ase.io import write as ase_write
    from ase.io.vasp import read_vasp
    from ase.optimize import BFGS

    from precis.structure.relax import _cell_filter, _ml_calculator

    # read_vasp, not the generic ase.io.read: the generic reader's return type
    # is a frame-or-list union (it honours an ``index`` slice), which no
    # single-structure caller here can use.
    atoms = read_vasp(io.StringIO(poscar))
    before = atoms.get_positions().copy()
    atoms.calc = _ml_calculator(model)

    curve: list[float] = []
    deadline = time.monotonic() + timeout

    def _record() -> None:
        f = atoms.get_forces()
        curve.append(round(float((f**2).sum(axis=1).max() ** 0.5), 4))
        if time.monotonic() > deadline:
            raise _RelaxDeadline(f"exceeded {timeout:.0f}s after {len(curve)} steps")

    target = _cell_filter(atoms, cell) if cell else atoms
    opt = BFGS(target, logfile=None)
    opt.attach(_record, interval=1)
    converged = bool(opt.run(fmax=_ML_FMAX, steps=steps))

    forces = atoms.get_forces()
    max_force = float((forces**2).sum(axis=1).max() ** 0.5)
    disp = atoms.get_positions() - before
    max_disp = float((disp**2).sum(axis=1).max() ** 0.5) if len(atoms) else 0.0
    buf = io.StringIO()
    ase_write(buf, atoms, format="vasp", direct=True)
    return {
        "ok": True,
        "scalars": {
            "E_tot": float(atoms.get_potential_energy()),
            "max_force": max_force,
            "max_disp": max_disp,
            "n_steps": int(opt.get_number_of_steps()),
            "converged": converged,
        },
        "curve": curve,
        "relaxed_poscar": buf.getvalue(),
    }


#: Swapped for a stub in tests, mirroring :data:`RUNNER` for the container path.
ML_RUNNER = _default_ml_runner


def _dispatch_inproc(
    ctx: Any,
    *,
    fidelity: str,
    model: str,
    steps: int,
    cell: str | None,
    poscar: str,
    structure_ref_id: int,
    on_version: int,
    cache_key: str,
    structure_sha: str,
    order: list[str],
    poscar_labels: list[str],
) -> None:
    """Compute an :data:`_INPROC_FIDELITIES` rung here on the node and sink it.

    No container, so none of the container machinery applies: no NFS staging,
    no deterministic-name mutex, no GPU reset. The wall-clock cap is enforced
    from inside the optimiser loop (:class:`_RelaxDeadline`) instead of by
    :data:`RUNNER`'s subprocess timeout.

    A missing MLIP backend is an **infra** failure, not a verdict on the
    candidate: the geometry is fine, this host just isn't provisioned. The
    quest loop reads ``failure_class`` to decide whether a candidate is ruled
    out, and ruling a structure out because a node lacked a wheel would be a
    lie it never revisits.
    """
    from precis.structure.relax import RelaxUnsupported

    timeout = _relax_timeout_s()
    ctx.append_chunk(
        "job_event",
        f"relax[{fidelity}] in-process on "
        f"{os.environ.get('PRECIS_NODE') or socket.gethostname()}: "
        f"model={model} steps={steps} fmax={_ML_FMAX} cap={timeout:.0f}s",
    )
    try:
        result = ML_RUNNER(
            poscar=poscar, model=model, steps=steps, cell=cell, timeout=timeout
        )
    except RelaxUnsupported as exc:
        ctx.record_failure(
            f"struct_relax: rung {fidelity!r} has no backend on this host ({exc}) "
            "-- the job was routed here by the claim gate but the MLIP wheel is "
            "missing; install the [dft-ml] extra on the node",
            failure_class="infra",
        )
        return
    except _RelaxDeadline as exc:
        # Honest and specific (gr346449): the old container-path text blamed
        # the GPU and prescribed a reboot for what was a physics/throughput
        # problem, which is why eleven auto-filed gripes never reached a cause.
        ctx.append_chunk(
            "job_event",
            f"relax[{fidelity}] in-process: {exc} -- self-aborted, nothing to "
            "reap (no container, no GPU reset attempted)",
        )
        ctx.record_failure(
            f"struct_relax: relax[{fidelity}] model={model} exceeded the "
            f"{timeout:.0f}s wall-clock cap ({exc}) -- raise "
            "PRECIS_DFT_RELAX_TIMEOUT_S, lower steps, or shrink the cell",
            failure_class="infra",
        )
        return
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("struct_relax: in-process relax raised", exc_info=True)
        ctx.record_failure(
            f"struct_relax: in-process relax[{fidelity}] failed: {exc}",
            failure_class="infra",
        )
        return

    _record_run(
        ctx,
        result,
        fidelity=fidelity,
        model=model,
        structure_ref_id=structure_ref_id,
        on_version=on_version,
        cache_key=cache_key,
        structure_sha=structure_sha,
        order=order,
        poscar_labels=poscar_labels,
    )


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

    # Fidelity routing (gr346449). The precis-dft image exposes exactly one
    # subcommand -- ``precis-dft-run gpaw-relax`` -- so a container run can only
    # ever compute a GPAW rung. Before this gate every rung took that argv:
    # a ``fidelity='ml'`` request ran a spin-polarized RPBE LCAO DFT relax
    # (~47h on 37 atoms, never inside the wall-clock cap) and, when one did
    # finish, sank DFT numbers into the run-cube under a MACE label. Running
    # different physics than was asked for is worse than not running: an
    # unroutable rung fails loudly here instead.
    if fidelity in _INPROC_FIDELITIES:
        _dispatch_inproc(
            ctx,
            fidelity=fidelity,
            model=model,
            steps=steps,
            cell=cell,
            poscar=poscar,
            structure_ref_id=structure_ref_id,
            on_version=on_version,
            cache_key=cache_key,
            structure_sha=structure_sha,
            order=order,
            poscar_labels=poscar_labels,
        )
        return
    if fidelity not in _CONTAINER_FIDELITIES:
        ctx.record_failure(
            f"struct_relax: no backend for fidelity {fidelity!r} -- the "
            f"precis-dft container implements {sorted(_CONTAINER_FIDELITIES)} "
            f"and this worker computes {sorted(_INPROC_FIDELITIES)} in-process. "
            "Refusing to run a different rung than was requested",
            failure_class="infra",
        )
        return

    node = params.get("target_node") or _NODE
    if node is None:
        ctx.record_failure(
            "struct_relax: no target node — params carry no target_node and "
            "PRECIS_DFT_NODE is unset on this host (deploy renders it from "
            "topology precis_capabilities.dft)",
            failure_class="infra",
        )
        return

    # Pre-clean (gripe 310809, defect C): the container name is a per-structure
    # mutex (deterministic --name precis-job-<ref_id>), so a container left
    # over from a prior attempt guarantees rc=125 name-conflict on every
    # retry forever unless it's cleared first. One cheap `docker ps` check on
    # the healthy path; only pays for a verified kill (defect A) when a stale
    # container is actually found.
    container_name = f"{_CONTAINER_PREFIX}{structure_ref_id}"
    local = node == os.environ.get("PRECIS_NODE")
    if _container_still_present(
        container_name, target=node, local=local, container_cmd=_CONTAINER_CMD
    ):
        log.warning(
            "struct_relax: pre-clean found a stale container %s on %s from a "
            "prior attempt — force-removing before dispatch",
            container_name,
            node,
        )
        if not kill_container(structure_ref_id, node=node):
            ctx.append_chunk(
                "job_event",
                f"relax[{fidelity}] on {node}: stale container {container_name} from "
                "a prior attempt could not be removed — failing fast instead of "
                "colliding on the container name",
            )
            ctx.record_failure(
                f"struct_relax: stale container {container_name} from a prior "
                f"attempt is un-removable on {node} — operator cleanup required "
                "(docker rm -f, or the nightly reboot) before this job can run",
                failure_class="infra",
            )
            return

    in_dir, out_dir = STAGER(structure_ref_id)
    Path(in_dir, "POSCAR").write_text(poscar, encoding="utf-8")
    # ``max_steps`` is the name the container's relax driver actually reads;
    # ``steps`` alone silently left it on its own 200 default, so the step cap
    # never crossed the contract (gr346449). Both are written -- the container
    # is built from another repo and may be an older build that reads neither.
    run_params: dict[str, Any] = {
        "fidelity": fidelity,
        "model": model,
        "steps": steps,
        "max_steps": steps,
    }
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
        kill_ok = kill_container(structure_ref_id, node=node)
        reset_ok = reset_gpu(node=node)
        # Honest, conditional text (gripe 310809, defect B) — the old text
        # unconditionally claimed the container was force-removed even when
        # the removal itself had failed or was never verified; a caller
        # (operator or the next retry) needs to know the truth, not the
        # aspiration.
        if kill_ok:
            container_note = "container force-removed"
        else:
            container_note = (
                f"container {container_name} NOT removed — it survives on {node}; "
                "operator docker rm -f (or the nightly reboot) is required before "
                "a retry can run"
            )
        gpu_note = (
            "nvidia-smi --gpu-reset attempted (succeeded)"
            if reset_ok
            else "nvidia-smi --gpu-reset attempted (failed)"
        )
        ctx.append_chunk(
            "job_event",
            f"relax[{fidelity}] on {node}: exceeded {_relax_timeout_s():.0f}s "
            f"wall-clock cap — self-aborted ({container_note}; {gpu_note})",
        )
        ctx.record_failure(
            f"struct_relax: relax[{fidelity}] model={model} on {node} exceeded the "
            f"{_relax_timeout_s():.0f}s wall-clock cap — self-aborted "
            f"({container_note}; {gpu_note}). A cap overrun is usually the run "
            f"being too big for the rung (atoms/k-points/steps), not wedged "
            f"hardware — check {_CONTAINER_OUT}/gpaw.txt for cores and the "
            f"per-step time, and nvidia-smi for actual GPU use, before treating "
            f"this as a GPU fault",
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
    _record_run(
        ctx,
        result,
        fidelity=fidelity,
        model=model,
        structure_ref_id=structure_ref_id,
        on_version=on_version,
        cache_key=cache_key,
        structure_sha=structure_sha,
        order=order,
        poscar_labels=poscar_labels,
    )


def _record_run(
    ctx: Any,
    result: dict[str, Any],
    *,
    fidelity: str,
    model: str,
    structure_ref_id: int,
    on_version: int,
    cache_key: str,
    structure_sha: str,
    order: list[str],
    poscar_labels: list[str],
) -> None:
    """Sink a finished relax into the run-cube — the one write-back both
    backends (container and in-process MLIP) land on, so a rung cannot acquire
    a second, subtly different recording path."""
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
    "ML_RUNNER",
    "SPEC",
    "build_run_argv",
    "kill_container",
    "load",
    "mlip_nodes",
    "reap_stale_containers",
    "reset_gpu",
    "target_node_for",
]
