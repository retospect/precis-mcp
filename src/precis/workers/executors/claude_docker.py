"""claude_docker executor — launch a container, poll it, reap by name.

Sibling of :mod:`claude_inproc` / :mod:`ssh_node` / :mod:`coordinator`: a ``run_claude_docker_pass`` function the CLI registers as a
``RefPass`` — but **only where ``PRECIS_SANDBOX_ENABLED=1``** (mirrors
``classify`` default-OFF), so the pass never runs on a non-sandbox host.

Unlike the blocking executors, this one is **detached + poll** (compute-lane shape / the ComputeBackend seam): each tick is a cheap
``inspect`` + heartbeat, the heavy work is out-of-process in the
container. That makes it a good round-robin citizen inside the existing
per-node worker — no new daemon.

Each pass does three things:

1. **Boot reconcile** (once per process) — ``rm -f`` orphaned
   ``sandbox-*`` containers with no live owning job (recovers a worker
   restart mid-run).
2. **Poll** in-flight (``STATUS:running`` + ``meta.container``) jobs
   pinned to this node: ``inspect`` status/exit, **renew the lease**
   (heartbeat) so a legit multi-hour run never trips the stuck-job
   sweeper; reap on exit, past the wall-clock ``deadline``, or on a §B-2
   operator ``meta.kill_requested`` (``precis jobs kill``) — the same
   ``_terminate`` path, at the next poll tick.
3. **Claim + launch** queued jobs up to ``PRECIS_SANDBOX_CONCURRENCY``
   (default 2), gated by ``target_node == PRECIS_NODE`` and an optional
   ``PRECIS_LOAD_CEILING`` load gate. Launch is detached
   (``podman run -d --name sandbox-<job_id>``); the container gets the
   OAuth token via ``--env`` (no ``--bare``, no ``ANTHROPIC_API_KEY``),
   cgroup caps, and **never** a ``--device`` (never a GPU).

Reaping is **by container name**, never a host pid — the name survives a
worker restart (conmon keeps the container alive independent of the
worker). **Launch is deliberately podman-only** (rootless podman is a
security choice for untrusted compute): ``_podman_bin()`` (default
``podman``, override ``PRECIS_PODMAN_BIN``) so tests inject a stub. **Poll
and reap are runtime-agnostic** (``_reap_bin()``, shared
``capability_probe.container_runtime()`` detector): on a docker-only host
(no podman on PATH) inspecting/removing an already-launched container falls
back to docker instead of throwing ``FileNotFoundError`` on every boot
reconcile.

Slice 1 (the stub-gated container-execution substrate) stages
``/work/PROMPT.md`` and drove the launch/poll/reap spine. Slices 2-4:

* **Harvest** (slice 2, this module's ``_terminate`` → ``_sandbox_harvest``)
  — a clean exit's ``/work/out`` mints a ``kind='folder'`` ref
  (``derived-from`` the job, ``supersedes``-chained to the same todo's
  prior build) with each file projected as a disk-backed ``plaintext``
  ref + the whole tree as a content-addressed tarball; ``RUN.json``
  becomes the ``mode:run`` recipe on folder + job meta. Every other
  terminal path (non-zero exit, timeout, vanished container) still
  discards ``out/`` unchanged.
* **Re-run** (slice 3, ``_launch_run`` / ``build_rerun_argv``) —
  ``mode:run`` stages a prior build's tarball into ``/work``
  (sha256-verified; reconstructable from the plaintext refs'
  ``meta.harvest_orig_path`` on a store miss) and launches
  ``sh -c 'cd /work && uv sync && <RUN.json.cmd>'`` with **no** claude /
  OAuth / API-key env at all.
* **Script** (``_launch_script`` / ``build_script_argv``) —
  ``mode:script`` takes the job's PyPI wheels from a separate stage job
  (``_sandbox_stage``; the lock + download run in a thread under their
  own lease, requeueing the run job until done), stages the script (inline
  ``code`` or a resolved ``code_ref``), ``/work/lock.txt`` and the
  ``inputs`` (``_sandbox_inputs``, read-only at ``/work/in``), and
  launches ``--network none`` with an explicit ``sh -c`` (offline
  install, then the script). ``out/`` is harvested on every exit, not
  only a clean one.
* **Read dial** (``precis_access:read``, ``_sandbox_read_mcp``) — a
  ``mode:script`` job may get a per-run ``precis serve --read-only``
  child on a unix socket (``agent_ro`` DSN, get/search/more only, fresh
  per-run token; only the PID persists in job meta — never the token),
  its socket dir bind-mounted read-only at ``/work/mcp`` while the
  container keeps ``--network none``. Fail-closed on
  ``PRECIS_SANDBOX_READ_MCP``; teardown is wired into every terminal
  path (``_terminate`` + boot ``reconcile_orphans``).
* **Image provenance** — the image that ran a job is recorded on
  ``job.meta.image``, echoed on every terminal ``job_summary`` line, and
  stamped onto the harvested folder's meta.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.utils.llm.router import Backend, resolve_backend
from precis.workers.executors import (
    _sandbox_harvest,
    _sandbox_inputs,
    _sandbox_read_mcp,
    _sandbox_script,
    _sandbox_stage,
)
from precis.workers.executors._common import (
    CANCELLED as _CANCELLED,
)
from precis.workers.executors._common import (
    FAILED as _FAILED,
)
from precis.workers.executors._common import (
    JOB_EVENT_KIND as _JOB_EVENT_KIND,
)
from precis.workers.executors._common import (
    JOB_SUMMARY_KIND as _JOB_SUMMARY_KIND,
)
from precis.workers.executors._common import (
    QUEUED as _QUEUED,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    SUCCEEDED as _SUCCEEDED,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    poison_guard as _poison_guard,
)
from precis.workers.executors._common import (
    release_job_reservation as _release_job_reservation,
)
from precis.workers.executors._common import (
    renew_lease_if_mine as _renew_lease_if_mine,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.job_types import sandbox_run as _sandbox_run

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "claude_docker"
_CONTAINER_PREFIX = "sandbox-"

#: Heartbeat margin (seconds) added over ``wall_seconds`` when sizing the
#: lease, so a run that's legitimately near its wall-clock ceiling can't
#: expire its own lease and get false-reaped by the stuck-job sweeper.
_LEASE_MARGIN_S = 600

#: Process-lifetime flag: run the orphan reconcile once per worker boot.
_reconciled = False

#: Stage jobs this process is running: job id -> (thread, claim meta, lost).
#: ``lost`` is set when a lease renewal finds another worker reclaimed the
#: row, so the thread skips its terminal write.
_STAGE_THREADS: dict[int, tuple[threading.Thread, dict[str, Any], threading.Event]] = {}

#: gr329258/gr333438 residual: an exit-0 container with an EMPTY ``out/``
#: that also ran for under this many seconds is more likely a silently
#: failed agent (e.g. auth rejected before it wrote anything) than a
#: genuinely fast, successful task — flagged in the job summary + logged,
#: never used to reclassify success/failure (that's a status-semantics
#: call this fix doesn't make).
_EMPTY_HARVEST_SHORT_RUN_S = 60


# ── Config ─────────────────────────────────────────────────────────


def _podman_bin() -> str:
    """The launch-path container binary — deliberately podman-only.

    Rootless podman is a security choice for the sandbox's untrusted
    compute, not just a default, so ``_launch`` (the only caller that
    invokes this) never falls back to docker. Contrast :func:`_reap_bin`,
    which the poll/reap path uses instead.
    """
    return os.environ.get("PRECIS_PODMAN_BIN") or "podman"


def _reap_bin() -> str:
    """The poll/reap-path container binary — runtime-agnostic.

    Inspecting/removing an *already-launched* container isn't a security
    decision the way launching one is, so this mirrors
    :func:`agent_container._container_bin`: prefer podman, fall back to
    docker (via the shared :func:`capability_probe.container_runtime`
    detector) so a docker-only host (no podman on PATH — e.g. spark) can
    still reap orphaned ``sandbox-*`` containers at boot instead of the
    reconcile pass throwing ``FileNotFoundError: 'podman'`` every restart.
    Falls back to ``"podman"`` (today's behavior) only when neither runtime
    is detected.
    """
    from precis.workers.capability_probe import container_runtime

    return container_runtime() or "podman"


def _concurrency() -> int:
    """Max in-flight container runs per host. Default 2; clamped [1, 16]."""
    try:
        n = int(os.environ.get("PRECIS_SANDBOX_CONCURRENCY", "2"))
    except ValueError:
        return 2
    return max(1, min(16, n))


def _work_root() -> Path:
    """Scratch root the executor stages ``/work`` dirs under."""
    return Path(os.environ.get("PRECIS_SANDBOX_WORK_DIR") or "/tmp/precis-sandbox")


def _network_mode() -> str:
    """Container network mode. Open egress, bounded internal reachability
    (bridge preferred over ``--network=host``); the ops play may pin it."""
    return os.environ.get("PRECIS_SANDBOX_NETWORK") or "bridge"


def _cgroup_caps() -> tuple[str, str, int]:
    """``(--memory, --cpus, --pids-limit)`` caps. Env-overridable."""
    memory = os.environ.get("PRECIS_SANDBOX_MEMORY") or "8g"
    cpus = os.environ.get("PRECIS_SANDBOX_CPUS") or "2"
    try:
        pids = int(os.environ.get("PRECIS_SANDBOX_PIDS_LIMIT", "512"))
    except ValueError:
        pids = 512
    return memory, cpus, pids


def _load_ok() -> bool:
    """Optional load gate. When ``PRECIS_LOAD_CEILING`` is set (or falls
    back to ``cpu_count * 1.5``), skip *claiming* new work while the 1-min
    load average is over the ceiling. Polling still runs. Best-effort:
    platforms without ``getloadavg`` never gate."""
    raw = os.environ.get("PRECIS_LOAD_CEILING")
    try:
        ceiling = float(raw) if raw else (os.cpu_count() or 1) * 1.5
    except ValueError:
        ceiling = (os.cpu_count() or 1) * 1.5
    try:
        load1 = os.getloadavg()[0]
    except (OSError, AttributeError):  # pragma: no cover - platform dep
        return True
    return load1 <= ceiling


def _remove_run_dirs(work_dir: Path) -> None:
    """Delete a run's ``/work`` dir and its ``mode:script`` siblings: the
    staged inputs (``<run>-in``) and the read MCP's socket dir
    (``<run>-mcp``). Idempotent."""
    shutil.rmtree(work_dir, ignore_errors=True)
    shutil.rmtree(work_dir.parent / f"{work_dir.name}-in", ignore_errors=True)
    shutil.rmtree(_sandbox_read_mcp.socket_dir(work_dir), ignore_errors=True)


def container_name(job_id: int) -> str:
    return f"{_CONTAINER_PREFIX}{job_id}"


def _job_id_from_container(name: str) -> int | None:
    if not name.startswith(_CONTAINER_PREFIX):
        return None
    try:
        return int(name[len(_CONTAINER_PREFIX) :])
    except ValueError:
        return None


# ── Launch argv (pure — asserted by tests) ─────────────────────────


def build_run_argv(
    *,
    podman_bin: str,
    job_id: int,
    image: str,
    work_dir: str,
    model: str,
    memory: str,
    cpus: str,
    pids_limit: int,
    network: str,
) -> list[str]:
    """Build the detached ``podman run`` argv for one job.

    Invariants (asserted by the tests): ``-d --name sandbox-<job_id>``;
    the OAuth token passed by **key only** (``--env
    CLAUDE_CODE_OAUTH_TOKEN`` — podman reads the value from the executor
    env, so it never lands in argv / ``ref_events``); **no** ``--bare``,
    **no** ``ANTHROPIC_API_KEY``; cgroup caps present; **no**
    ``--device`` (never a GPU); and the ``image`` pinned as the IMAGE
    positional behind a ``--`` end-of-options sentinel (gr179503). The
    resolved model is passed as a non-secret env value for the image
    entrypoint.
    """
    return [
        podman_bin,
        "run",
        "-d",
        "--name",
        container_name(job_id),
        # OAuth token by KEY only — value inherited from the daemon env.
        "--env",
        "CLAUDE_CODE_OAUTH_TOKEN",
        # Model for the image entrypoint (non-secret → value is fine).
        "--env",
        f"PRECIS_SANDBOX_MODEL={model}",
        # cgroup caps — bound the blast radius on a load-dominant host.
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--pids-limit",
        str(pids_limit),
        # Bounded reachability; never --network=host by default.
        "--network",
        network,
        # The IN/OUT bus — the only thing touching both DB and /work is
        # this (trusted) executor; the container sees only files.
        "-v",
        f"{work_dir}:/work",
        # ``--`` end-of-options sentinel: even though ``image`` is format-
        # validated by ``sandbox_run.semantic_rejection`` (rejecting a
        # leading ``-``), pin it as the IMAGE positional here too so a
        # ``-``-leading value can never be parsed as a podman flag
        # (gr179503 — defense in depth at the argv sink).
        "--",
        image,
    ]


def build_rerun_argv(
    *,
    podman_bin: str,
    job_id: int,
    image: str,
    work_dir: str,
    cmd: str,
    memory: str,
    cpus: str,
    pids_limit: int,
    network: str,
) -> list[str]:
    """Build the detached ``podman run`` argv for a ``mode:run`` re-run.

    Same detached-name / cgroup-cap / bounded-network / ``/work`` bus
    invariants as :func:`build_run_argv`, but two structural differences
    the design requires: **no** ``CLAUDE_CODE_OAUTH_TOKEN`` /
    ``ANTHROPIC_API_KEY`` / any auth env at all (no claude is spawned —
    tested by asserting neither name appears anywhere in argv), and an
    explicit in-container CMD override (``sh -c 'cd /work && uv sync &&
    <cmd>'``) since the code-task image's default entrypoint (unbuilt —
    see the design doc's ops-half note) assumes the build lane's claude
    invocation, not a bare re-run. ``cmd`` is the harvested
    ``RUN.json.cmd`` recipe string — it runs inside the same cgroup-capped,
    network-bounded container the build itself ran in, so it carries no
    more trust than the build container already had.
    """
    shell_cmd = f"cd /work && uv sync && {cmd}"
    return [
        podman_bin,
        "run",
        "-d",
        "--name",
        container_name(job_id),
        # cgroup caps — bound the blast radius on a load-dominant host.
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--pids-limit",
        str(pids_limit),
        # Bounded reachability; never --network=host by default.
        "--network",
        network,
        # The IN/OUT bus — the staged build tree lives at /work root
        # (not /work/out — that's this run's OWN out lane).
        "-v",
        f"{work_dir}:/work",
        "--",
        image,
        "sh",
        "-c",
        shell_cmd,
    ]


#: The ``mode:script`` in-container command: a scratch venv, an offline
#: install of the executor-staged wheels pinned by ``/work/lock.txt``,
#: then the script. Never the image CMD (``code-task-run`` runs claude).
_SCRIPT_INSTALL = (
    "uv venv /tmp/v && uv pip install --python /tmp/v --offline --no-index "
    "--find-links /work/wheels -r /work/lock.txt"
)
SCRIPT_SHELL_CMD = f"{_SCRIPT_INSTALL} && /tmp/v/bin/python /work/main.py"


def script_shell_cmd(entry: str = "main.py", *, warm: bool = False) -> str:
    """:data:`SCRIPT_SHELL_CMD` for ``entry`` (relative to ``/work``).

    ``warm=True`` mounts the stage job's pre-built venv (``venvs/<lock
    sha>``) at :data:`_sandbox_script.CONTAINER_VENV` instead of running
    ``uv venv`` + ``uv pip install`` per run — the warm lane.

    A folder entry (``code/…``) gets ``PYTHONPATH=/work`` so
    ``import precis_client`` still finds ``/work/precis_client.py``.
    ``entry`` is shape-checked at submit (``_sandbox_inputs.entry_rejection``)
    and quoted here as well.
    """
    python = (
        f"{_sandbox_script.CONTAINER_VENV}/bin/python" if warm else "/tmp/v/bin/python"
    )
    prefix = "" if warm else f"{_SCRIPT_INSTALL} && "
    if entry == "main.py":
        return f"{prefix}{python} /work/main.py"
    return f"{prefix}PYTHONPATH=/work {python} {shlex.quote('/work/' + entry)}"


def build_script_argv(
    *,
    podman_bin: str,
    job_id: int,
    image: str,
    work_dir: str,
    wheels_dir: str,
    memory: str,
    cpus: str,
    pids_limit: int,
    entry: str = "main.py",
    inputs_dir: str | None = None,
    mcp_dir: str | None = None,
    venv_dir: str | None = None,
) -> list[str]:
    """Build the detached ``podman run`` argv for a ``mode:script`` run.

    Same detached-name / cgroup-cap / ``/work`` bus invariants as
    :func:`build_rerun_argv`, with no auth env at all, but the network is
    always ``none`` — not :func:`_network_mode` — so neither
    ``PRECIS_SANDBOX_NETWORK`` nor a param can open egress: the deps
    arrive as wheels the executor downloaded (``wheels_dir``, shared
    across jobs by lock sha, mounted read-only at ``/work/wheels`` so a
    script cannot poison the cache), and the in-container command is the
    explicit :func:`script_shell_cmd`, never the image CMD. ``inputs_dir``
    (the staged ``inputs``) mounts read-only at ``/work/in``; ``mcp_dir``
    (the read MCP's socket dir, ``precis_access:read`` only) mounts
    read-only at ``/work/mcp`` — the container reaches the socket through
    the bind, not a network.
    """
    extra_mounts: list[str] = []
    if inputs_dir is not None:
        extra_mounts += ["-v", f"{inputs_dir}:/work/in:ro"]
    if mcp_dir is not None:
        extra_mounts += ["-v", f"{mcp_dir}:{_sandbox_read_mcp.CONTAINER_SOCKET_DIR}:ro"]
    dep_mounts: list[str]
    if venv_dir is not None:
        # Warm lane: the stage job's pre-built venv, mounted at the path
        # it was built at so its baked-in interpreter paths resolve.
        dep_mounts = [
            "-v",
            f"{venv_dir}:{_sandbox_script.CONTAINER_VENV}:ro",
        ]
    else:
        dep_mounts = ["-v", f"{wheels_dir}:/work/wheels:ro"]
    return [
        podman_bin,
        "run",
        "-d",
        "--name",
        container_name(job_id),
        # cgroup caps — bound the blast radius on a load-dominant host.
        "--memory",
        memory,
        "--cpus",
        cpus,
        "--pids-limit",
        str(pids_limit),
        # No network at all: PyPI is reached executor-side only.
        "--network",
        "none",
        "-v",
        f"{work_dir}:/work",
        *dep_mounts,
        *extra_mounts,
        "--",
        image,
        "sh",
        "-c",
        script_shell_cmd(entry, warm=venv_dir is not None),
    ]


# ── podman plumbing ────────────────────────────────────────────────


def _podman(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a poll/reap-path container command via :func:`_reap_bin` — see
    that function's docstring for why this is runtime-agnostic while the
    launch path (:func:`_launch`, ``build_run_argv``) stays podman-only."""
    return subprocess.run(
        [_reap_bin(), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout,
        check=False,
    )


def _inspect(name: str) -> tuple[str, int] | None:
    """Return ``(status, exit_code)`` for a container, or ``None`` if it
    doesn't exist. Status is podman's ``.State.Status`` (``running`` /
    ``exited`` / ``created`` …)."""
    res = _podman(
        [
            "inspect",
            "--format",
            "{{.State.Status}} {{.State.ExitCode}}",
            name,
        ]
    )
    if res.returncode != 0:
        return None
    parts = (res.stdout or "").split()
    if len(parts) < 2:
        return None
    try:
        return parts[0], int(parts[1])
    except ValueError:
        return parts[0], -1


def _logs_tail(name: str, *, max_chars: int = 4000) -> str:
    """Best-effort stderr/stdout tail from the container for forensics."""
    try:
        res = _podman(["logs", "--tail", "50", name])
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return ""
    text = (res.stderr or "") + (res.stdout or "")
    return text[-max_chars:]


def _logs_streams(name: str, *, max_chars: int = 4000) -> tuple[str, str]:
    """Best-effort ``(stdout, stderr)`` tails, kept apart (``mode:script``:
    stdout is the script's result channel, stderr its traceback)."""
    try:
        res = _podman(["logs", "--tail", "50", name])
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        return "", ""
    return (res.stdout or "")[-max_chars:], (res.stderr or "")[-max_chars:]


def _reap(name: str) -> None:
    """Force-remove a container (idempotent; best-effort)."""
    try:
        _podman(["rm", "-f", name])
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        log.warning("claude_docker: rm -f %s failed", name, exc_info=True)


def _list_sandbox_containers() -> list[str]:
    """All ``sandbox-*`` container names (running or exited)."""
    res = _podman(
        ["ps", "-a", "--filter", f"name={_CONTAINER_PREFIX}", "--format", "{{.Names}}"]
    )
    if res.returncode != 0:
        return []
    return [
        ln.strip()
        for ln in (res.stdout or "").splitlines()
        if ln.strip().startswith(_CONTAINER_PREFIX)
    ]


# ── Boot reconcile ─────────────────────────────────────────────────


def reconcile_orphans(store: Store) -> int:
    """``rm -f`` every ``sandbox-*`` container with no live owning job.

    A container is an orphan when its job ref is gone / soft-deleted /
    already terminal (``STATUS`` ∈ succeeded|failed|cancelled). Returns
    the count reaped. Idempotent; safe to call repeatedly.

    Also reaps that job's ``precis_access:read`` MCP callback child, if
    any (``meta.read_mcp_pid``) — a worker restart between the container
    exiting and ``_terminate`` running (which would have reaped it
    normally) is exactly the crash window this boot-reconcile exists for.
    """
    reaped = 0
    for name in _list_sandbox_containers():
        job_id = _job_id_from_container(name)
        if job_id is None:
            continue
        if not _job_is_live(store, job_id):
            _reap_orphan_read_mcp(store, job_id)
            _reap(name)
            _remove_run_dirs(_work_root() / name)
            reaped += 1
            log.info("claude_docker: reaped orphan container %s", name)
    return reaped


def _reap_orphan_read_mcp(store: Store, job_id: int) -> None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'read_mcp_pid' FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    if not row or not row[0]:
        return
    try:
        _sandbox_read_mcp.reap_read_mcp(int(row[0]))
    except Exception:  # pragma: no cover - defensive
        log.warning(
            "claude_docker: orphan reap of read-mcp pid %s (job %d) raised",
            row[0],
            job_id,
            exc_info=True,
        )


def _job_is_live(store: Store, job_id: int) -> bool:
    """A job is live when its ref exists, isn't deleted, and its STATUS is
    non-terminal (queued / running)."""
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT t.value
              FROM refs r
              LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
              LEFT JOIN tags t
                     ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS'
             WHERE r.ref_id = %s
               AND r.kind = 'job'
               AND r.retired_at IS NULL
             LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return False
    status = row[0]
    return status in (_RUNNING, "queued")


# ── Pass entry point ───────────────────────────────────────────────


def run_claude_docker_pass(store: Store, *, limit: int = 4) -> dict[str, int]:
    """Poll in-flight runs, then claim + launch queued jobs.

    Returns ``{claimed, ok, failed}`` for runner aggregation. ``claimed``
    counts launches attempted this tick; ``ok`` counts jobs that this
    tick either launched cleanly or drove to a clean terminal state;
    ``failed`` counts jobs failed this tick.
    """
    global _reconciled
    node = os.environ.get("PRECIS_NODE")

    if not _reconciled:
        try:
            reconcile_orphans(store)
        except Exception:  # pragma: no cover - defensive
            log.warning("claude_docker: boot reconcile failed", exc_info=True)
        _reconciled = True

    ok = 0
    failed = 0

    # 0) Keep the leases of this process's running stage jobs alive.
    _heartbeat_stages(store)

    # 1) Poll in-flight jobs pinned to this node.
    for ref_id, meta in _running_jobs(store, node):
        try:
            terminal = _poll_job(store, ref_id, meta)
            if terminal == _SUCCEEDED:
                ok += 1
            elif terminal == _FAILED:
                failed += 1
        except Exception:  # pragma: no cover - defensive
            log.warning("claude_docker: poll of job %d raised", ref_id, exc_info=True)

    # 2) Claim + launch queued jobs, capped by concurrency + load.
    inflight = _inflight_count(store, node)
    slots = _concurrency() - inflight
    launched = 0
    if slots > 0 and _load_ok():
        rows, poisoned = _claim(store, node, limit=min(limit, slots))
        launched += poisoned
        failed += poisoned
        for ref_id, _title, meta in rows:
            launched += 1
            if meta.get("container"):
                # Re-adopt (§H, REQUIRED — see _claim's docstring): this
                # claim reclaimed a row that already has a launched
                # container stamped (the claiming process died AFTER
                # ``_launch`` wrote ``meta.container`` but before it reached
                # a terminal STATUS — e.g. between two poll ticks). The
                # container survives a worker restart independent of the
                # worker (conmon), so launching a SECOND one under the same
                # name would either collide or silently orphan the first.
                # Never launch here: STATUS:running + meta.container is
                # exactly what step 1's ``_running_jobs`` selects, so the
                # NEXT pass's poll (this one already ran step 1 before this
                # claim) resumes ownership by name-match — inspect(name)
                # still running -> keep polling; gone -> _poll_job's normal
                # vanished-container path fails it. Counts as ok (no new
                # failure surfaced by the reclaim itself).
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"runner: re-adopted job {ref_id} (container "
                    f"{meta.get('container')} already launched by a prior "
                    "worker generation) — resuming poll, not relaunching",
                )
                ok += 1
                continue
            if _launch_safe(store, ref_id, meta, node):
                ok += 1
            else:
                failed += 1
    return {"claimed": launched, "ok": ok, "failed": failed}


def _claim(
    store: Store, node: str | None, *, limit: int
) -> tuple[list[tuple[int, str, dict[str, Any]]], int]:
    """Claim queued claude_docker jobs and mark them running under a
    ``wall_seconds``-sized lease (in the claim tx, so no double-claim).

    Returns ``(rows, poisoned)`` — ``poisoned`` counts rows that hit the
    crash-loop guard (§H piece 3, :func:`_common.poison_guard`) and were
    failed + bubbled right here instead of being handed back to run;
    ``rows`` excludes them.

    ``reclaim_stale_running=True`` (the lease-epoch reclaim arm): the
    detached container outlives the launching worker process, so
    ``_running_jobs``' poll loop already self-heals across a restart FOR a
    row that made it far enough to get ``meta.container`` stamped. The
    epoch/expiry arms here close the one narrow gap that leaves — a claim
    that set ``STATUS:running`` but crashed before ``_launch`` wrote
    ``meta.container`` — which would otherwise sit until the lease expires.
    A reclaimed row that DOES already carry ``meta.container`` is handled
    by the re-adopt branch in :func:`run_claude_docker_pass` (never
    re-launched; the poll loop resumes ownership by name-match)."""
    if limit <= 0:
        return [], 0
    poisoned = 0
    claimed: list[tuple[int, str, dict[str, Any]]] = []
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=limit,
            node=node,
            parent_not_paused=True,
            reclaim_stale_running=True,
            respect_reserve=True,
        )
        if not rows:
            conn.commit()
            return [], 0
        for ref_id, title, meta in rows:
            # Crash-loop guard (§H piece 3): a container-relaunch job that
            # keeps dying its worker every generation eventually stops
            # stealing itself forever, including epoch-only reclaims.
            if _poison_guard(store, conn, ref_id, meta):
                poisoned += 1
                continue
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(meta), ref_id),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
            claimed.append((ref_id, title, meta))
        conn.commit()
    return claimed, poisoned


def _lease_seconds(meta: dict[str, Any]) -> int:
    params = meta.get("params") or {}
    if params.get("mode") == _sandbox_stage.MODE:
        # Short and heartbeated: a dead worker's stage is retried quickly.
        return _sandbox_stage.STAGE_LEASE_S
    wall = int(_sandbox_run.resolve_wall_seconds(params) or 0)
    return max(_LEASE_MARGIN_S, wall + _LEASE_MARGIN_S)


# ── Launch ─────────────────────────────────────────────────────────


def _launch_safe(
    store: Store, ref_id: int, meta: dict[str, Any], node: str | None
) -> bool:
    try:
        _launch(store, ref_id, meta, node)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("claude_docker: launch of job %d raised: %s", ref_id, exc)
        _fail(store, ref_id, f"runner: launch raised: {exc!r}")
        return False


def _launch(store: Store, ref_id: int, meta: dict[str, Any], node: str | None) -> None:
    """Validate + branch to the mode-specific launcher.

    Defence in depth: a job minted by dispatch from a todo never went
    through the JobHandler put path, so re-run the fail-closed gate here
    at launch time. A rejection fails the job (no container).
    """
    params = dict(meta.get("params") or {})
    if params.get("mode") == _sandbox_stage.MODE:
        # Executor-minted and internal: the put gate refuses this mode.
        _launch_stage(store, ref_id, params, meta)
        return
    reason = _sandbox_run.semantic_rejection(params)
    if reason is not None:
        _fail(store, ref_id, reason)
        return
    mode = str(params.get("mode") or "build")
    if mode == "run":
        _launch_run(store, ref_id, params, node)
    elif mode == "script":
        _launch_script(store, ref_id, params, node, meta=meta)
    else:
        _launch_build(store, ref_id, params, node)


def _launch_build(
    store: Store, ref_id: int, params: dict[str, Any], node: str | None
) -> None:
    """Stage ``/work`` with ``PROMPT.md``, launch a detached claude
    container, record its handle (``mode:build``)."""
    # GLM/OpenRouter fleet-flip safety gate: the container spawns a raw `claude` CLI, which
    # assumes Claude model semantics — under backend=openai,
    # resolve_sandbox_model() (-> resolve_model(Tier.FRONTIER)) returns an OSS
    # slug that `claude` can't run (HTTP 400). Skip cleanly rather than
    # launch a doomed container: STATUS:cancelled, no failure bubble (this
    # is a config mismatch, not a job failure), so a re-claim after the
    # backend reverts to anthropic just launches normally. Build-mode only
    # — mode:run spawns no claude CLI, so the backend flip doesn't apply.
    if resolve_backend() is Backend.OPENAI:
        log.info(
            "claude_docker: llm.backend=openai — skipping sandbox_run job "
            "%d (the container spawns a raw `claude` CLI that assumes "
            "Claude model semantics, unsupported under the OSS/OpenRouter "
            "backend)",
            ref_id,
        )
        _skip(
            store,
            ref_id,
            "sandbox_run: llm.backend=openai is not supported — the "
            "container spawns a raw `claude` CLI (Claude model semantics "
            "only); skipped cleanly, re-attempt once the backend reverts "
            "to anthropic",
        )
        return

    from precis import secrets as _secrets

    _oauth = _secrets.get_secret("CLAUDE_CODE_OAUTH_TOKEN")
    if not _oauth:
        _fail(
            store,
            ref_id,
            "sandbox_run: CLAUDE_CODE_OAUTH_TOKEN is not set in the daemon "
            "env — the container can't authenticate Claude",
        )
        return
    # podman passes the token by KEY only (value inherited from the
    # SUBPROCESS's env — see the ``subprocess.run(..., env=...)`` call
    # below). Deliberately NEVER pinned into this (long-lived daemon)
    # process's ``os.environ``: ``get_secret`` resolves the env leg BEFORE
    # the vault leg (gr333244), so a ``setdefault`` here would make the
    # first-resolved value shadow every later vault rotation until the
    # daemon restarts. Re-resolved fresh on every launch instead.

    wall_seconds = int(_sandbox_run.resolve_wall_seconds(params) or 0)
    image = params.get("image") or _sandbox_run.default_image()
    model = params.get("model") or _sandbox_run.resolve_sandbox_model()
    name = container_name(ref_id)

    # Stage the /work run dir with PROMPT.md (the harvest contract). A
    # stale dir from a prior attempt on the same ref is cleared first.
    work_dir = _work_root() / name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    (work_dir / "out").mkdir(parents=True, exist_ok=True)
    (work_dir / "in").mkdir(parents=True, exist_ok=True)
    (work_dir / "_run").mkdir(parents=True, exist_ok=True)
    (work_dir / "PROMPT.md").write_text(
        _sandbox_run.compose_prompt(str(params.get("prompt") or "")),
        encoding="utf-8",
    )
    # World-writable staging: the image runs as its non-root `sandbox`
    # user (uid 1000), which rootless podman maps to a subordinate host
    # uid — NOT the daemon user that owns these dirs. Without the chmod
    # the task can't write /work/out and every harvest comes back empty.
    for p in (work_dir, work_dir / "out", work_dir / "in", work_dir / "_run"):
        p.chmod(0o777)

    # precis_access:read is refused for mode:build by semantic_rejection
    # (the read MCP is a unix socket Claude Code cannot speak), so no
    # callback is spawned here.
    network = _network_mode()

    # A leftover container of the same name (crashed prior attempt) would
    # make ``run --name`` fail; clear it first.
    _reap(name)

    memory, cpus, pids_limit = _cgroup_caps()
    argv = build_run_argv(
        podman_bin=_podman_bin(),
        job_id=ref_id,
        image=image,
        work_dir=str(work_dir),
        model=model,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        network=network,
    )
    # The token is passed ONLY here — the subprocess's env, never this
    # process's ``os.environ`` (see the comment above): the key-only
    # ``--env CLAUDE_CODE_OAUTH_TOKEN`` in ``argv`` inherits from whatever
    # env dict this call passes, so a freshly-resolved value reaches the
    # container even if the vault rotated since the last launch.
    res = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
        env={**os.environ, "CLAUDE_CODE_OAUTH_TOKEN": _oauth},
    )
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        _fail(store, ref_id, f"sandbox_run: podman run failed: {tail}")
        return

    container_id = (res.stdout or "").strip()
    deadline = time.time() + wall_seconds
    with store.pool.connection() as conn:
        _set_meta(
            conn,
            ref_id,
            container=name,
            container_id=container_id,
            run_host=node or "",
            deadline=deadline,
            image=image,
            model=model,
        )
        _append_chunk(
            store,
            ref_id,
            _JOB_EVENT_KIND,
            f"launched container {name} (image={image}, model={model}, "
            f"wall={wall_seconds}s) on {node or '<unpinned>'}",
            conn=conn,
        )
        conn.commit()
    log.info("claude_docker: launched job %d as %s", ref_id, name)


def _launch_run(
    store: Store, ref_id: int, params: dict[str, Any], node: str | None
) -> None:
    """Stage a prior build's harvested tarball into ``/work``, launch a
    detached re-run container (``mode:run`` — no claude, no OAuth).

    ``params.artifact`` is validated by ``semantic_rejection`` (positive
    int) before this runs. ``precis_access`` is accepted by the schema
    but ignored here regardless of value — design: "mode:run containers
    get no mcp.json ever".
    """
    folder_id = int(params["artifact"])
    name = container_name(ref_id)
    work_dir = _work_root() / name
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    # /work/out is this run's OWN output lane (RESULT.md, artifacts/) —
    # the staged build tree lands at the /work root, not here.
    (work_dir / "out").mkdir(parents=True, exist_ok=True)
    (work_dir / "in").mkdir(parents=True, exist_ok=True)
    (work_dir / "_run").mkdir(parents=True, exist_ok=True)

    try:
        folder_meta = _sandbox_harvest.stage_run_artifact(
            store, folder_id=folder_id, dest_dir=work_dir
        )
    except ValueError as exc:
        _fail(store, ref_id, f"sandbox_run: mode:run staging failed: {exc}")
        return

    run_recipe = folder_meta.get("run_recipe") or {}
    cmd = run_recipe.get("cmd") if isinstance(run_recipe, dict) else None
    if not isinstance(cmd, str) or not cmd.strip():
        _fail(
            store,
            ref_id,
            f"sandbox_run: folder:{folder_id} has no RUN.json.cmd to re-run",
        )
        return

    wall_seconds = int(_sandbox_run.resolve_wall_seconds(params) or 0)
    image = (
        params.get("image") or folder_meta.get("image") or _sandbox_run.default_image()
    )

    # A leftover container of the same name (crashed prior attempt) would
    # make ``run --name`` fail; clear it first.
    _reap(name)

    memory, cpus, pids_limit = _cgroup_caps()
    argv = build_rerun_argv(
        podman_bin=_podman_bin(),
        job_id=ref_id,
        image=image,
        work_dir=str(work_dir),
        cmd=cmd,
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        network=_network_mode(),
    )
    res = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if res.returncode != 0:
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        _fail(store, ref_id, f"sandbox_run: podman run failed: {tail}")
        return

    container_id = (res.stdout or "").strip()
    deadline = time.time() + wall_seconds
    with store.pool.connection() as conn:
        _set_meta(
            conn,
            ref_id,
            container=name,
            container_id=container_id,
            run_host=node or "",
            deadline=deadline,
            image=image,
            run_of_folder_id=folder_id,
        )
        _append_chunk(
            store,
            ref_id,
            _JOB_EVENT_KIND,
            f"launched re-run container {name} (image={image}, "
            f"build folder:{folder_id}, wall={wall_seconds}s) on "
            f"{node or '<unpinned>'}",
            conn=conn,
        )
        conn.commit()
    log.info("claude_docker: launched re-run job %d as %s", ref_id, name)


def _launch_script(
    store: Store,
    ref_id: int,
    params: dict[str, Any],
    node: str | None,
    *,
    meta: dict[str, Any] | None = None,
) -> None:
    """Take the script's PyPI wheels from its stage job (requeueing until
    that job succeeded), stage the
    script (``/work/main.py``, or a folder under ``/work/code``),
    ``/work/lock.txt`` and the ``inputs``, spawn the read MCP for
    ``precis_access:read``, then launch a detached ``--network none``
    container (``mode:script`` — no claude, no OAuth).

    ``semantic_rejection`` has checked the param shapes; ``code_ref`` and
    ``inputs`` are resolved again here (``_sandbox_inputs``) because a
    dispatch-minted job skipped ``validate_submit`` and the refs may have
    changed. The lock + download run in a separate stage job
    (:mod:`_sandbox_stage`), never in this tick, so the deadline set at
    launch excludes staging; a failed stage fails this job before a
    container exists.
    """
    name = container_name(ref_id)
    work_dir = _work_root() / name
    inputs = list(params.get("inputs") or [])
    try:
        if isinstance(params.get("code_ref"), str):
            code = _sandbox_inputs.resolve_code_ref(
                store,
                str(params["code_ref"]),
                block=params.get("block"),
                entry=params.get("entry"),
            )
        else:
            code = _sandbox_inputs.ResolvedCode(
                entry="main.py", source=str(params["code"])
            )
        staged = _staged_deps(store, ref_id, params, node, meta or {})
        if staged is None:
            return  # requeued behind its stage job, or failed
        if work_dir.exists():
            shutil.rmtree(work_dir, ignore_errors=True)
        # ``wheels``/``mcp`` are only mountpoints for read-only binds; ``in``
        # is one too when inputs are staged.
        for sub in ("out", "in", "_run", "wheels", "mcp"):
            (work_dir / sub).mkdir(parents=True, exist_ok=True)
        _sandbox_inputs.stage_code(store, code, work_dir)
        inputs_dir: Path | None = None
        if inputs:
            inputs_dir = work_dir.parent / f"{name}-in"
            _sandbox_inputs.stage_inputs(store, inputs, inputs_dir)
    except (_sandbox_inputs.ScriptRefError, _sandbox_script.ScriptStageError) as exc:
        _remove_run_dirs(work_dir)
        _fail(store, ref_id, str(exc))
        return
    (work_dir / "lock.txt").write_text(staged.lock_text, encoding="utf-8")
    # World-writable staging, as for mode:build: the image's non-root
    # user maps to a subordinate host uid, not the daemon user.
    for p in (work_dir, work_dir / "out", work_dir / "in", work_dir / "_run"):
        p.chmod(0o777)
    # Staged trees are read by that subordinate uid too, whatever the
    # daemon's umask was when tar or write_bytes created them.
    for tree in (inputs_dir, work_dir / "code"):
        if tree is not None and tree.exists():
            for p in (tree, *tree.rglob("*")):
                if not p.is_symlink():
                    p.chmod(0o755 if p.is_dir() else 0o644)

    wall_seconds = int(_sandbox_run.resolve_wall_seconds(params) or 0)
    image = params.get("image") or _sandbox_run.default_image()

    # A leftover container of the same name (crashed prior attempt) would
    # make ``run --name`` fail; clear it first.
    _reap(name)

    # precis_access:read — the per-run read-only MCP on a unix socket,
    # spawned BEFORE the container so /work/mcp.json exists when it
    # starts (the client retries the connect while serve boots). The
    # value is re-checked by semantic_rejection at launch, flag included.
    read_mcp_pid: int | None = None
    mcp_dir: Path | None = None
    if params.get("precis_access") == "read":
        try:
            handle = _sandbox_read_mcp.spawn_read_mcp(store, work_dir=work_dir)
        except Exception as exc:
            _remove_run_dirs(work_dir)
            _fail(
                store,
                ref_id,
                f"sandbox_run: precis_access:read MCP spawn failed: {exc}",
            )
            return
        read_mcp_pid = handle.pid
        mcp_dir = handle.socket_dir

    memory, cpus, pids_limit = _cgroup_caps()
    # Warm lane only when the staged venv was built under this run's
    # image — interpreter paths are baked into the venv.
    venv_dir = (
        str(staged.venv_dir)
        if staged.venv_dir is not None and staged.venv_image == image
        else None
    )
    argv = build_script_argv(
        podman_bin=_podman_bin(),
        job_id=ref_id,
        image=image,
        work_dir=str(work_dir),
        wheels_dir=str(staged.wheels_dir),
        memory=memory,
        cpus=cpus,
        pids_limit=pids_limit,
        entry=code.entry,
        inputs_dir=str(inputs_dir) if inputs_dir is not None else None,
        mcp_dir=str(mcp_dir) if mcp_dir is not None else None,
        venv_dir=venv_dir,
    )
    res = subprocess.run(
        argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    if res.returncode != 0:
        if read_mcp_pid is not None:
            _sandbox_read_mcp.reap_read_mcp(read_mcp_pid)
        _remove_run_dirs(work_dir)
        tail = (res.stderr or res.stdout or "").strip()[-2000:]
        _fail(store, ref_id, f"sandbox_run: podman run failed: {tail}")
        return

    container_id = (res.stdout or "").strip()
    deadline = time.time() + wall_seconds
    n_deps = len(params.get("deps") or [])
    with store.pool.connection() as conn:
        _set_meta(
            conn,
            ref_id,
            container=name,
            container_id=container_id,
            run_host=node or "",
            deadline=deadline,
            image=image,
            deps_lock_sha=staged.lock_sha,
            **({"read_mcp_pid": read_mcp_pid} if read_mcp_pid is not None else {}),
        )
        _append_chunk(
            store,
            ref_id,
            _JOB_EVENT_KIND,
            f"launched script container {name} (image={image}, network=none, "
            f"entry={code.entry}, {n_deps} deps, lock {staged.lock_sha[:12]}"
            f"{' reused' if staged.reused else ''}, {len(inputs)} inputs, "
            f"read_mcp={'uds' if read_mcp_pid is not None else 'none'}, "
            f"wall={wall_seconds}s) on {node or '<unpinned>'}",
            conn=conn,
        )
        conn.commit()
    log.info("claude_docker: launched script job %d as %s", ref_id, name)


# ── Dependency stage jobs (gr496426) ───────────────────────────────


#: In-container command the stage job runs to pre-build the warm venv at
#: :data:`_sandbox_script.CONTAINER_VENV` (structure-workspaces §Packaging
#: "a pre-built venv per lock sha").
_VENV_BUILD = (
    f"uv venv {_sandbox_script.CONTAINER_VENV} && "
    f"uv pip install --python {_sandbox_script.CONTAINER_VENV} "
    "--offline --no-index --find-links /wheels -r /lock/lock.txt"
)


def _build_venv(
    staged: _sandbox_script.StagedDeps,
) -> tuple[Path | None, str | None, str | None]:
    """Pre-build the run venv for ``staged.lock_sha`` in a one-shot
    container on the default image — the venv dir lands under
    ``venvs/<lock sha>`` complete-marked.

    Built inside the image (not on the host) so the venv's interpreter
    paths resolve in run containers; mounted at ``CONTAINER_VENV`` so the
    run bind path equals the build path. Returns ``(venv_dir, image,
    error)`` — a build failure is not fatal: the run falls back to the
    cold ``uv venv`` install (logged, still correct).
    """
    image = _sandbox_run.default_image()
    root = _sandbox_script.venv_cache_root(_work_root())
    dest = root / staged.lock_sha
    marker = _sandbox_script.venv_complete_marker()
    if (dest / marker).exists():
        return dest, image, None
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{staged.lock_sha[:12]}-", dir=root))
    try:
        (tmp / "venv").mkdir()
        lock_dir = tmp / "lock"
        lock_dir.mkdir()
        (lock_dir / "lock.txt").write_text(staged.lock_text, encoding="utf-8")
        res = subprocess.run(
            [
                _podman_bin(),
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{tmp / 'venv'}:{_sandbox_script.CONTAINER_VENV}",
                "-v",
                f"{staged.wheels_dir}:/wheels:ro",
                "-v",
                f"{lock_dir}:/lock:ro",
                "--",
                image,
                "sh",
                "-c",
                _VENV_BUILD,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_sandbox_stage.STAGE_WALL_S,
            check=False,
        )
        if res.returncode != 0:
            tail = (res.stderr or res.stdout or "").strip()[-2000:]
            return None, None, f"venv build failed (exit {res.returncode}): {tail}"
        (tmp / "venv" / marker).write_text(staged.lock_sha + "\n", encoding="utf-8")
        (tmp / "venv").chmod(0o755)
        try:
            (tmp / "venv").rename(dest)
        except OSError:
            if not (dest / marker).exists():
                return None, None, "venv rename lost the race and no winner landed"
        return dest, image, None
    except (OSError, subprocess.SubprocessError) as exc:
        return None, None, f"venv build raised: {exc!r}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _staged_deps(
    store: Store,
    ref_id: int,
    params: dict[str, Any],
    node: str | None,
    meta: dict[str, Any],
) -> _sandbox_script.StagedDeps | None:
    """The run job's staged wheels, or ``None`` when it was requeued behind
    a pending stage job or failed with that stage's error."""
    deps = list(params.get("deps") or [])
    cache_root = _sandbox_script.wheel_cache_root(_work_root())
    if not _sandbox_stage.normalise_deps(deps):
        return _sandbox_script.stage_deps([], cache_root=cache_root)  # no network
    prev = meta.get("stage_job")
    state = _sandbox_stage.get_stage(store, int(prev)) if prev else None
    if state is None or (state.status == "succeeded" and not state.usable):
        state = _sandbox_stage.find_or_mint(
            store, str(params.get("target_node") or node or "") or None, deps
        )
    if state.usable:
        info = state.meta["stage"]
        return _sandbox_script.StagedDeps(
            lock_text=str(info["lock_text"]),
            lock_sha=str(info["lock_sha"]),
            wheels_dir=Path(str(info["wheels_dir"])),
            reused=True,
            venv_dir=(Path(str(info["venv_dir"])) if info.get("venv_dir") else None),
            venv_image=info.get("venv_image"),
        )
    if state.pending:
        _requeue_for_stage(store, ref_id, state.ref_id, first=prev != state.ref_id)
        return None
    _fail(
        store,
        ref_id,
        "sandbox_run: dependency staging failed: "
        f"{state.meta.get('stage_error') or 'stage job ' + str(state.ref_id)}",
    )
    return None


def _requeue_for_stage(
    store: Store, ref_id: int, stage_id: int, *, first: bool
) -> None:
    """Put a claimed run job back to ``queued`` with a short re-check lease
    (a queued row is not claimable before its ``lease_until``). Its
    reservation is refunded; the launching claim re-reserves and sizes the
    run lease and ``wall_seconds`` deadline afresh."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'stage_job', %s::int,"
            "  'lease_until', (now() + make_interval(secs => %s))::text"
            ") WHERE ref_id = %s",
            (stage_id, _sandbox_stage.RECHECK_S, ref_id),
        )
        _release_job_reservation(conn, ref_id)
        if first:
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"waiting for dependency stage job {stage_id} "
                "(staging time does not count against wall_seconds)",
                conn=conn,
            )
        _set_status(store, ref_id, _QUEUED, conn=conn)
        conn.commit()


def _launch_stage(
    store: Store, ref_id: int, params: dict[str, Any], meta: dict[str, Any]
) -> None:
    """Run a stage job's lock + download in a thread; the pass keeps its
    lease alive and the thread writes the terminal state."""
    live = _STAGE_THREADS.get(ref_id)
    if live is not None and live[0].is_alive():
        return  # reclaimed while this process still runs it
    deps = list(params.get("deps") or [])
    reason = _sandbox_run.deps_rejection(deps)
    if reason is not None:
        _fail(store, ref_id, reason)
        return
    lost = threading.Event()
    thread = threading.Thread(
        target=_run_stage,
        args=(store, ref_id, deps, lost),
        name=f"sandbox-stage-{ref_id}",
        daemon=True,
    )
    _STAGE_THREADS[ref_id] = (thread, meta, lost)
    _append_chunk(
        store, ref_id, _JOB_EVENT_KIND, f"staging {len(deps)} deps (lock + wheels)"
    )
    thread.start()


def _run_stage(
    store: Store, ref_id: int, deps: list[str], lost: threading.Event
) -> None:
    try:
        try:
            staged = _sandbox_script.stage_deps(
                deps, cache_root=_sandbox_script.wheel_cache_root(_work_root())
            )
        except _sandbox_script.ScriptStageError as exc:
            error = str(exc)
        except Exception as exc:
            log.warning("claude_docker: stage job %d raised", ref_id, exc_info=True)
            error = f"sandbox_run: dependency staging raised: {exc!r}"
        else:
            if lost.is_set():
                return  # another worker owns the row now
            venv_dir, venv_image, venv_err = _build_venv(staged)
            if venv_err is not None:
                log.warning(
                    "claude_docker: stage job %d venv build: %s", ref_id, venv_err
                )
            with store.pool.connection() as conn:
                _set_meta(
                    conn,
                    ref_id,
                    stage={
                        "lock_text": staged.lock_text,
                        "lock_sha": staged.lock_sha,
                        "wheels_dir": str(staged.wheels_dir),
                        "reused": staged.reused,
                        **(
                            {"venv_dir": str(venv_dir), "venv_image": venv_image}
                            if venv_dir is not None
                            else {}
                        ),
                    },
                )
                _append_chunk(
                    store,
                    ref_id,
                    _JOB_EVENT_KIND,
                    f"staged lock {staged.lock_sha[:12]}"
                    f"{' (reused)' if staged.reused else ''}",
                    conn=conn,
                )
                _set_status(store, ref_id, _SUCCEEDED, conn=conn)
                conn.commit()
            return
        if lost.is_set():
            return
        with store.pool.connection() as conn:
            _set_meta(conn, ref_id, stage_error=error)
            conn.commit()
        _fail(store, ref_id, error)
    except Exception:  # pragma: no cover - defensive
        log.warning(
            "claude_docker: stage job %d bookkeeping failed", ref_id, exc_info=True
        )
    finally:
        _STAGE_THREADS.pop(ref_id, None)


def _heartbeat_stages(store: Store) -> None:
    for ref_id, (thread, meta, lost) in list(_STAGE_THREADS.items()):
        if not thread.is_alive():
            continue
        try:
            with store.pool.connection() as conn:
                mine = _renew_lease_if_mine(
                    conn, ref_id, meta, _sandbox_stage.STAGE_LEASE_S
                )
                conn.commit()
            if not mine:
                lost.set()
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "claude_docker: stage heartbeat of %d failed", ref_id, exc_info=True
            )


def join_stage_threads(timeout: float = 30.0) -> None:
    """Wait for this process's stage threads (tests, orderly shutdown)."""
    for thread, _meta, _lost in list(_STAGE_THREADS.values()):
        thread.join(timeout)


# ── Poll + reap ────────────────────────────────────────────────────


def _running_jobs(store: Store, node: str | None) -> list[tuple[int, dict[str, Any]]]:
    """In-flight claude_docker jobs (``STATUS:running`` + ``meta.container``)
    pinned to this node."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'executor' = %s
               AND r.meta ? 'container'
               AND (r.meta->'params'->>'target_node') IS NOT DISTINCT FROM %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = %s
                   )
             ORDER BY r.ref_id
            """,
            (_EXECUTOR_NAME, node, _RUNNING),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def _inflight_count(store: Store, node: str | None) -> int:
    return len(_running_jobs(store, node))


def _poll_job(store: Store, ref_id: int, meta: dict[str, Any]) -> str | None:
    """Poll one in-flight job. Returns the terminal STATUS applied
    (``succeeded`` / ``failed``), or ``None`` when it's still running
    (lease heartbeated)."""
    name = str(meta.get("container") or container_name(ref_id))
    deadline = float(meta.get("deadline") or 0.0)
    kill_requested = meta.get("kill_requested")
    state = _inspect(name)

    if state is None:
        # Container vanished (rm'd out from under us / never started) —
        # an empty run. Terminal failure.
        _terminate(
            store,
            ref_id,
            name,
            status=_FAILED,
            summary=f"sandbox_run job:{ref_id}: container {name} not found "
            "(empty run / vanished).",
            exit_code=None,
            meta=meta,
        )
        return _FAILED

    status, exit_code = state
    if status == "exited":
        ok = exit_code == 0
        _terminate(
            store,
            ref_id,
            name,
            status=_SUCCEEDED if ok else _FAILED,
            summary=f"sandbox_run job:{ref_id}: container {name} exited {exit_code}.",
            exit_code=exit_code,
            meta=meta,
        )
        return _SUCCEEDED if ok else _FAILED

    # Still running (or created). §B-2 operator kill backstop (`precis jobs
    # kill`) takes precedence over the wall-clock deadline check below —
    # same terminal path (_terminate), different swept tag/summary.
    if kill_requested:
        note = kill_requested.get("note") if isinstance(kill_requested, dict) else None
        summary = (
            f"sandbox_run job:{ref_id}: killed by operator ({note})."
            if note
            else f"sandbox_run job:{ref_id}: killed by operator."
        )
        _terminate(
            store,
            ref_id,
            name,
            status=_FAILED,
            summary=summary,
            exit_code=None,
            swept="killed-by-operator",
            meta=meta,
        )
        return _FAILED

    # Still running (or created). Wall-clock kill?
    if deadline and time.time() > deadline:
        _terminate(
            store,
            ref_id,
            name,
            status=_FAILED,
            summary=f"sandbox_run job:{ref_id}: killed at wall-clock deadline "
            f"(container {name}).",
            exit_code=None,
            swept="wall-timeout",
            meta=meta,
        )
        return _FAILED

    # Alive and within budget — heartbeat the lease.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => %s))::text"
            ") WHERE ref_id = %s",
            (_lease_seconds(meta), ref_id),
        )
        conn.commit()
    return None


def _terminate(
    store: Store,
    ref_id: int,
    name: str,
    *,
    status: str,
    summary: str,
    exit_code: int | None,
    swept: str | None = None,
    meta: dict[str, Any] | None = None,
) -> None:
    """Write minimal forensics, reap the container + workdir, set the
    terminal STATUS, and bubble a failure to the parent todo.

    On a clean exit (``status == succeeded`` and ``exit_code == 0``),
    harvests ``/work/out`` (see ``_sandbox_harvest``) **before** the
    workdir is deleted — folder + plaintext projection + content-
    addressed tarball, plus (``mode:build`` only) the ``RUN.json``
    recipe and (``mode:run`` only) a ``derived-from`` link back to the
    build folder it re-ran. A harvest failure is caught and logged,
    never crashes the poll tick (the container is already reaped by this
    point; failing the whole terminal transition would strand the job
    ``running`` with no container). For ``mode:build`` / ``mode:run``,
    every other terminal path (non-zero exit, timeout, vanished
    container) discards ``out/`` unchanged, as slice 1 did;
    ``mode:script`` harvests ``out/`` on every terminal path and appends
    the stdout and stderr tails as separate ``job_event`` chunks.

    gr329258/gr333438: an empty ``out/`` on a short exit-0 run gets an
    explicit ``⚠`` warning appended to the summary (and WARN-logged) —
    a flag, not a status change (:data:`_EMPTY_HARVEST_SHORT_RUN_S`).
    """
    params = dict((meta or {}).get("params") or {})
    run_mode = str(params.get("mode") or "build")
    stdout_tail = ""
    if run_mode == "script":
        stdout_tail, stderr_tail = _logs_streams(name)
    else:
        stderr_tail = _logs_tail(name)
    # Kill (best-effort) then force-remove — covers a still-running
    # deadline reap and a clean exited container alike.
    try:
        _podman(["kill", name])
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pass
    _reap(name)

    # precis_access:read teardown — reap the per-run MCP callback child on
    # EVERY terminal path (success, failure, timeout, vanished container):
    # the container can no longer reach it regardless of why this run
    # ended. This is one of the two paths the design requires (the other
    # is reconcile_orphans, for a container that's already gone at boot);
    # both call the same idempotent reap_read_mcp.
    read_mcp_pid = (meta or {}).get("read_mcp_pid")
    if isinstance(read_mcp_pid, int):
        try:
            _sandbox_read_mcp.reap_read_mcp(read_mcp_pid)
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "claude_docker: reap of read-mcp pid %d (job %d) raised",
                read_mcp_pid,
                ref_id,
                exc_info=True,
            )

    work_dir = _work_root() / name

    # Per-unit pinned image provenance (
    # `code-task:<sha>` tagging convention): the image that actually ran
    # this job is forensic text on every terminal job_summary, not just
    # meta/folder — the whole point is "which image built/ran this" being
    # trivially answerable from the one place a human reads first.
    img = (meta or {}).get("image")
    image_note = f" image={img}." if img else ""

    # Computed once, up front, so both the empty-harvest short-run flag
    # below and the trailing "(Ns)" forensic suffix use the same number
    # instead of two separate DB round trips racing each other.
    duration = _duration_seconds(store, ref_id)

    harvest_note = ""
    # mode:script harvests on EVERY exit: a failing script's partial out/
    # is the debugging evidence. build/run harvest only a clean exit.
    if (status == _SUCCEEDED and exit_code == 0) or run_mode == "script":
        build_folder_id = params.get("artifact") if run_mode == "run" else None
        try:
            result = _sandbox_harvest.harvest_out(
                store,
                job_ref_id=ref_id,
                container_name=name,
                work_dir=work_dir,
                image=str((meta or {}).get("image") or ""),
                model=str((meta or {}).get("model") or ""),
                extra_derived_from=(
                    int(build_folder_id) if isinstance(build_folder_id, int) else None
                ),
            )
            harvest_note = " " + _sandbox_harvest.summarize(result)
            if (
                result.empty
                and run_mode != "script"
                and duration is not None
                and duration < _EMPTY_HARVEST_SHORT_RUN_S
            ):
                # gr329258 residual: flag only — never reclassify
                # success/failure (a status-semantics call not being
                # made here).
                harvest_note += (
                    " ⚠ empty out/ on a short exit-0 run — likely "
                    "silent agent failure (auth?)."
                )
                log.warning(
                    "claude_docker: job %d exited 0 with empty out/ after "
                    "%.0fs — likely silent agent failure",
                    ref_id,
                    duration,
                )
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "claude_docker: harvest of job %d raised", ref_id, exc_info=True
            )
            harvest_note = " harvest failed (see worker log)."

    with store.pool.connection() as conn:
        _append_chunk(
            store,
            ref_id,
            _JOB_SUMMARY_KIND,
            summary
            + (f" exit={exit_code}." if exit_code is not None else "")
            + image_note
            + harvest_note
            + (f" ({duration:.0f}s)" if duration is not None else ""),
            conn=conn,
        )
        if stdout_tail:
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"script stdout tail ({len(stdout_tail)} chars):\n{stdout_tail}",
                conn=conn,
            )
        if stderr_tail:
            label = (
                "script stderr tail" if run_mode == "script" else "container log tail"
            )
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"{label} ({len(stderr_tail)} chars):\n{stderr_tail}",
                conn=conn,
            )
        _set_meta(conn, ref_id, exit_code=exit_code)
        if swept is not None:
            from precis.store import Tag

            store.add_tag(
                ref_id,
                Tag.parse_strict(f"swept:{swept}"),
                set_by="system",
                conn=conn,
            )
        _set_status(store, ref_id, status, conn=conn)
        conn.commit()

    # Harvest (above) copies anything worth keeping out of /work/out
    # before this — the scratch workdir is deleted either way.
    _remove_run_dirs(work_dir)

    if status == _FAILED:
        from precis.utils.job_bubble import bubble_job_failure

        bubble_job_failure(store, ref_id)


def _duration_seconds(store: Store, ref_id: int) -> float | None:
    """Best-effort run duration from the ref's created/updated timestamps.
    Cheap and approximate — the exact container runtime is a slice-2
    forensic; slice 1 only needs an order-of-magnitude figure."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT extract(epoch FROM (now() - created_at)) "
            "FROM refs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    if row is None or row[0] is None:
        return None
    try:
        return float(row[0])
    except (TypeError, ValueError):
        return None


# ── Failure helper ─────────────────────────────────────────────────


def _fail(store: Store, ref_id: int, reason: str) -> None:
    """Fail a job before/without a container: event chunk + STATUS:failed
    + failure bubble. Mirrors ``_common.record_failure`` but keeps the
    job_summary shape consistent with the terminal path."""
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, reason, conn=conn)
        _set_status(store, ref_id, _FAILED, conn=conn)
        conn.commit()
    from precis.utils.job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id)


def _skip(store: Store, ref_id: int, reason: str) -> None:
    """Cleanly terminate a job before/without a container — a config-
    mismatch no-op, not a failure (e.g. the backend-flip safety gate).
    Event chunk + STATUS:cancelled, **no** failure bubble — mirrors the
    cooperative-cancel treatment ``claude_inproc`` gives a pre-run cancel
    request, so a downstream parent todo isn't tagged ``child-failed`` for
    a job that never actually attempted (and failed) its task."""
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, reason, conn=conn)
        _set_status(store, ref_id, _CANCELLED, conn=conn)
        conn.commit()


__all__ = [
    "build_rerun_argv",
    "build_run_argv",
    "build_script_argv",
    "container_name",
    "reconcile_orphans",
    "run_claude_docker_pass",
]
