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

Slice 1 (``docs/proposals/sandbox-run-substrate.md``): the ``/work/out``
→ folder + tarball artifact projection is **slice 2**. This slice stages
``/work/PROMPT.md``, discards ``out/``, and keeps only forensics.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

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
    **no** ``ANTHROPIC_API_KEY``; cgroup caps present; and **no**
    ``--device`` (never a GPU). The resolved model is passed as a
    non-secret env value for the image entrypoint.
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
        image,
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
    """
    reaped = 0
    for name in _list_sandbox_containers():
        job_id = _job_id_from_container(name)
        if job_id is None:
            continue
        if not _job_is_live(store, job_id):
            _reap(name)
            reaped += 1
            log.info("claude_docker: reaped orphan container %s", name)
    return reaped


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
    """Stage ``/work``, launch a detached container, record its handle."""
    params = dict(meta.get("params") or {})

    # Defence in depth: a job minted by dispatch from a todo never went
    # through the JobHandler put path, so re-run the fail-closed gate at
    # launch. A rejection fails the job (no container).
    reason = _sandbox_run.semantic_rejection(params)
    if reason is not None:
        _fail(store, ref_id, reason)
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
) -> None:
    """Write minimal forensics, reap the container + workdir, set the
    terminal STATUS, and bubble a failure to the parent todo."""
    stderr_tail = _logs_tail(name)
    # Kill (best-effort) then force-remove — covers a still-running
    # deadline reap and a clean exited container alike.
    try:
        _podman(["kill", name])
    except (OSError, subprocess.SubprocessError):  # pragma: no cover
        pass
    _reap(name)

    work_dir = _work_root() / name
    with store.pool.connection() as conn:
        duration = _duration_seconds(store, ref_id)
        _append_chunk(
            store,
            ref_id,
            _JOB_SUMMARY_KIND,
            summary
            + (f" exit={exit_code}." if exit_code is not None else "")
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

    # Slice-1 discards /work/out — keep only forensics (harvest is slice 2).
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


__all__ = [
    "build_run_argv",
    "container_name",
    "reconcile_orphans",
    "run_claude_docker_pass",
]
