"""claude_docker executor — launch a container, poll it, reap by name.

Sibling of :mod:`claude_inproc` / :mod:`ssh_node` / :mod:`coordinator`
(ADR 0017): a ``run_claude_docker_pass`` function the CLI registers as a
``RefPass`` — but **only where ``PRECIS_SANDBOX_ENABLED=1``** (mirrors
``classify`` default-OFF), so the pass never runs on a non-sandbox host.

Unlike the blocking executors, this one is **detached + poll** (ADR 0044
compute-lane shape / the ComputeBackend seam): each tick is a cheap
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
   sweeper; reap on exit or past the wall-clock ``deadline``.
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

Slice 1 (``docs/proposals/sandbox-run-substrate.md``) stages
``/work/PROMPT.md`` and drove the launch/poll/reap spine. Slice 2 (this
module's ``_terminate``, design §"Harvest -> DB + NAS") harvests a clean
exit's ``/work/out`` — folder + plaintext projection + content-addressed
tarball + (``mode:build``) ``RUN.json`` recipe — via
:mod:`precis.workers.executors._sandbox_harvest` before the scratch
workdir is deleted; every other terminal path (non-zero exit, timeout,
vanished container) still discards ``out/`` unchanged. Slice 3
(``_launch_run`` / ``build_rerun_argv``, design §"Re-run +
operationalize") re-runs a prior build: stages the harvested tarball
into ``/work``, launches ``sh -c 'cd /work && uv sync && <RUN.json.cmd>'``
with **no** claude / OAuth / API-key env at all, and harvest links the
result folder back to the build folder it re-ran.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from precis.utils.llm.router import Backend, resolve_backend
from precis.workers.executors import _sandbox_harvest, _sandbox_read_mcp
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
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)
from precis.workers.job_types import sandbox_run as _sandbox_run

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "claude_docker"
_CONTAINER_PREFIX = "sandbox-"

#: Heartbeat margin (seconds) added over ``wall_seconds`` when sizing the
#: lease, so a run that's legitimately near its wall-clock ceiling can't
#: expire its own lease and get false-reaped by the stuck-job sweeper.
_LEASE_MARGIN_S = 600

#: Process-lifetime flag: run the orphan reconcile once per worker boot.
_reconciled = False


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


# ── podman plumbing ────────────────────────────────────────────────


def _podman(args: list[str], *, timeout: int = 60) -> subprocess.CompletedProcess[str]:
    """Run a poll/reap-path container command via :func:`_reap_bin` — see
    that function's docstring for why this is runtime-agnostic while the
    launch path (:func:`_launch`, ``build_run_argv``) stays podman-only."""
    return subprocess.run(
        [_reap_bin(), *args],
        capture_output=True,
        text=True,
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


def reconcile_orphans(store: Any) -> int:
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
            reaped += 1
            log.info("claude_docker: reaped orphan container %s", name)
    return reaped


def _reap_orphan_read_mcp(store: Any, job_id: int) -> None:
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


def _job_is_live(store: Any, job_id: int) -> bool:
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
               AND r.deleted_at IS NULL
             LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    if row is None:
        return False
    status = row[0]
    return status in (_RUNNING, "queued")


# ── Pass entry point ───────────────────────────────────────────────


def run_claude_docker_pass(store: Any, *, limit: int = 4) -> dict[str, int]:
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
        rows = _claim(store, node, limit=min(limit, slots))
        for ref_id, _title, meta in rows:
            launched += 1
            if _launch_safe(store, ref_id, meta, node):
                ok += 1
            else:
                failed += 1
    return {"claimed": launched, "ok": ok, "failed": failed}


def _claim(
    store: Any, node: str | None, *, limit: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Claim queued claude_docker jobs and mark them running under a
    ``wall_seconds``-sized lease (in the claim tx, so no double-claim)."""
    if limit <= 0:
        return []
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=limit,
            node=node,
            parent_not_paused=True,
        )
        if not rows:
            conn.commit()
            return []
        for ref_id, _title, meta in rows:
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_lease_seconds(meta), ref_id),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
        conn.commit()
    return rows


def _lease_seconds(meta: dict[str, Any]) -> int:
    wall = int((meta.get("params") or {}).get("wall_seconds", 0) or 0)
    return max(_LEASE_MARGIN_S, wall + _LEASE_MARGIN_S)


# ── Launch ─────────────────────────────────────────────────────────


def _launch_safe(
    store: Any, ref_id: int, meta: dict[str, Any], node: str | None
) -> bool:
    try:
        _launch(store, ref_id, meta, node)
        return True
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("claude_docker: launch of job %d raised: %s", ref_id, exc)
        _fail(store, ref_id, f"runner: launch raised: {exc!r}")
        return False


def _launch(store: Any, ref_id: int, meta: dict[str, Any], node: str | None) -> None:
    """Validate + branch to the mode-specific launcher.

    Defence in depth: a job minted by dispatch from a todo never went
    through the JobHandler put path, so re-run the fail-closed gate here
    at launch time. A rejection fails the job (no container).
    """
    params = dict(meta.get("params") or {})
    reason = _sandbox_run.semantic_rejection(params)
    if reason is not None:
        _fail(store, ref_id, reason)
        return
    mode = str(params.get("mode") or "build")
    if mode == "run":
        _launch_run(store, ref_id, params, node)
    else:
        _launch_build(store, ref_id, params, node)


def _launch_build(
    store: Any, ref_id: int, params: dict[str, Any], node: str | None
) -> None:
    """Stage ``/work`` with ``PROMPT.md``, launch a detached claude
    container, record its handle (``mode:build``)."""
    # GLM/OpenRouter fleet-flip safety gate (docs/proposals/glm-fleet-flip-
    # safety.md Part 3): the container spawns a raw `claude` CLI, which
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
    # podman passes the token by KEY only (value inherited from this process's
    # env, never argv). Populate env from the vault when it's not already there
    # so the key-only inheritance works post-cutover (secrets vault, ADR 0055).
    os.environ.setdefault("CLAUDE_CODE_OAUTH_TOKEN", _oauth)

    wall_seconds = int(params["wall_seconds"])
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

    # precis_access:read (design §"Precis access") — spawn a per-run,
    # token'd, read-only MCP callback BEFORE the container starts, so
    # /work/mcp.json exists the moment the container can read /work.
    # Never for mode:run (this function is build-mode only) and gated
    # fail-closed by semantic_rejection on PRECIS_SANDBOX_READ_MCP
    # already (defence in depth: re-check the value here too, since a
    # dispatch-minted job's params never passed through validate_submit).
    read_mcp_pid: int | None = None
    network = _network_mode()
    if params.get("precis_access") == "read":
        try:
            handle = _sandbox_read_mcp.spawn_read_mcp(store, work_dir=work_dir)
        except Exception as exc:
            _fail(
                store,
                ref_id,
                f"sandbox_run: precis_access:read MCP spawn failed: {exc}",
            )
            return
        read_mcp_pid = handle.pid
        # Only THIS network mode gives the container a route back to the
        # spawned callback's loopback bind (design §"Container networking").
        network = _sandbox_read_mcp.READ_MCP_NETWORK

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
    res = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
    if res.returncode != 0:
        if read_mcp_pid is not None:
            _sandbox_read_mcp.reap_read_mcp(read_mcp_pid)
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
            **({"read_mcp_pid": read_mcp_pid} if read_mcp_pid is not None else {}),
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
    store: Any, ref_id: int, params: dict[str, Any], node: str | None
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

    wall_seconds = int(params["wall_seconds"])
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
    res = subprocess.run(argv, capture_output=True, text=True, timeout=120, check=False)
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


# ── Poll + reap ────────────────────────────────────────────────────


def _running_jobs(store: Any, node: str | None) -> list[tuple[int, dict[str, Any]]]:
    """In-flight claude_docker jobs (``STATUS:running`` + ``meta.container``)
    pinned to this node."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
             WHERE r.kind = 'job'
               AND r.deleted_at IS NULL
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


def _inflight_count(store: Any, node: str | None) -> int:
    return len(_running_jobs(store, node))


def _poll_job(store: Any, ref_id: int, meta: dict[str, Any]) -> str | None:
    """Poll one in-flight job. Returns the terminal STATUS applied
    (``succeeded`` / ``failed``), or ``None`` when it's still running
    (lease heartbeated)."""
    name = str(meta.get("container") or container_name(ref_id))
    deadline = float(meta.get("deadline") or 0.0)
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
    store: Any,
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
    harvests ``/work/out`` (design §"Harvest -> DB + NAS") **before** the
    workdir is deleted — folder + plaintext projection + content-
    addressed tarball, plus (``mode:build`` only) the ``RUN.json``
    recipe and (``mode:run`` only) a ``derived-from`` link back to the
    build folder it re-ran. A harvest failure is caught and logged,
    never crashes the poll tick (the container is already reaped by this
    point; failing the whole terminal transition would strand the job
    ``running`` with no container). Every other terminal path (non-zero
    exit, timeout, vanished container) discards ``out/`` unchanged, as
    slice 1 did.
    """
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

    # Per-unit pinned image provenance (design §"Image distribution" /
    # `code-task:<sha>` tagging convention): the image that actually ran
    # this job is forensic text on every terminal job_summary, not just
    # meta/folder — the whole point is "which image built/ran this" being
    # trivially answerable from the one place a human reads first.
    img = (meta or {}).get("image")
    image_note = f" image={img}." if img else ""

    harvest_note = ""
    if status == _SUCCEEDED and exit_code == 0:
        params = dict((meta or {}).get("params") or {})
        run_mode = str(params.get("mode") or "build")
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
        except Exception:  # pragma: no cover - defensive
            log.warning(
                "claude_docker: harvest of job %d raised", ref_id, exc_info=True
            )
            harvest_note = " harvest failed (see worker log)."

    with store.pool.connection() as conn:
        duration = _duration_seconds(store, ref_id)
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
        if stderr_tail:
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"container log tail ({len(stderr_tail)} chars):\n{stderr_tail}",
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
    shutil.rmtree(work_dir, ignore_errors=True)

    if status == _FAILED:
        from precis.handlers._job_bubble import bubble_job_failure

        bubble_job_failure(store, ref_id)


def _duration_seconds(store: Any, ref_id: int) -> float | None:
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


def _fail(store: Any, ref_id: int, reason: str) -> None:
    """Fail a job before/without a container: event chunk + STATUS:failed
    + failure bubble. Mirrors ``_common.record_failure`` but keeps the
    job_summary shape consistent with the terminal path."""
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, reason, conn=conn)
        _set_status(store, ref_id, _FAILED, conn=conn)
        conn.commit()
    from precis.handlers._job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id)


def _skip(store: Any, ref_id: int, reason: str) -> None:
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
    "container_name",
    "reconcile_orphans",
    "run_claude_docker_pass",
]
