"""``release_cut`` — cut ``release/r<N>`` and return (dark release pass, td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``cut`` node of the ``release-pass`` template. Mechanical, no LLM. A
detached submit/poll pair under ``ssh_node``:

* :func:`submit` runs ``scripts/release --cut-only`` in the repo checkout
  (:func:`precis.workers.job_types.run_slice.slice_root`: ``PRECIS_SLICE_ROOT``,
  else the main checkout around the worker's code) detached, stdout+stderr to
  a log and the exit code to a file beside it (under the temp dir), and
  returns ``{pid, pgid, root, log, exit_file, started_at}``.
* :func:`poll` waits for the exit file; on exit 0 with a
  ``RELEASE_CUT: r<N> <sha>`` line it records the cut on the plan
  (:func:`precis.plans.release.record_cut`: ``meta.release`` on the plan
  root with the ``host`` and ``checkout`` it ran in, the real range on the
  plan's ``tests-green`` gate and loop, ``deploy r<N>`` on the approval
  ask) and succeeds with r<N> and the sha in the
  summary and ``meta.release_cut``. No ``RELEASE_CUT`` line (S already live,
  a release in flight elsewhere) or a non-zero exit fails the job with the
  log tail; the plan tick's retry cap then files the ask.

The cut never waits for CI: ``ci_verdict_poll`` records the verdict and the
``tests-green`` gate reads it. Refused while ``PRECIS_FACTORY=off``. The
helpers below (:func:`launch_script`, :func:`finish_run`) are shared with
``release_deploy``.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "release_cut"

_DEFAULT_WALL_S = 30 * 60
_TAIL_CAP = 2000

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's cut node; its plan root gets meta.release. Unset: the
        # cut still runs, nothing is stamped.
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (the host holding the checkout + push creds).
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Dark release pass: run scripts/release --cut-only in the repo checkout "
    "(cut release/r<N> from main, no CI wait), then stamp r<N>/sha on the "
    "plan root's meta.release and the real range on the plan's tests-green "
    "gate. Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


# ── Shared detached-script helpers (release_deploy uses them too) ──


def _launch(argv: list[str], *, cwd: Path, log_path: Path, exit_file: Path) -> int:
    """Seam: start ``argv`` detached (run_slice's launcher, stdin from
    /dev/null, the worker's own env — the deploy needs its credentials)."""
    return _rs._launch(
        argv,
        cwd=cwd,
        stdin=Path(os.devnull),
        log=log_path,
        exit_file=exit_file,
        env=dict(os.environ),
    )


def _run_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "precis-release-ops"
    path.mkdir(parents=True, exist_ok=True)
    return path


def factory_refusal(name: str) -> str | None:
    if _rs.factory_off():
        return (
            f"{name}: the factory is off (PRECIS_FACTORY=off); next: unset it on "
            "the worker host to run release ops"
        )
    return None


def todo_refusal(store: Any, name: str, todo_id: Any, *, required: bool) -> str | None:
    if todo_id is None and not required:
        return None
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or todo_id <= 0:
        return f"{name}: params.todo_id must be a positive integer (the plan node)"
    if store is not None and store.get_ref(kind="todo", id=todo_id) is None:
        return f"{name}: todo {todo_id} not found; next: pass a live plan node id"
    return None


def launch_script(ctx: Any, name: str, args: list[str]) -> dict[str, Any] | None:
    """Run ``<root>/scripts/release <args>`` detached; the handle, or
    ``None`` after failing the job."""
    try:
        root = _rs.slice_root()
        script = root / "scripts" / "release"
        if not script.exists():
            raise RuntimeError(f"no scripts/release in {root}")
        base = _run_dir() / f"{name}-{ctx.ref_id}"
        log_path = base.with_suffix(".log")
        exit_file = base.with_suffix(".exit")
        exit_file.unlink(missing_ok=True)
        pid = _launch(
            [str(script), *args], cwd=root, log_path=log_path, exit_file=exit_file
        )
    except Exception as exc:
        log.warning("%s: launch failed", name, exc_info=True)
        ctx.record_failure(f"{name}: launch failed: {exc}")
        return None
    ctx.append_chunk(
        "job_event",
        f"{name}: launched scripts/release {' '.join(args)} pid {pid} in {root}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "root": str(root),
        "log": str(log_path),
        "exit_file": str(exit_file),
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def finish_run(ctx: Any, handle: Any, name: str) -> tuple[int | None, str] | None:
    """``None`` while the script runs; else ``(exit code, log text)``
    (``exit code`` ``None`` when the process vanished without one)."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"{name}: malformed handle {handle!r}")
        return None
    exit_code = _rs._read_exit(handle)
    if exit_code is None and _rs._process_alive(int(handle.get("pid") or 0)):
        return None
    ctx.set_meta(compute_handle_released=True, exit_code=exit_code)
    return exit_code, _rs._read_log(handle)


def fail_with_tail(ctx: Any, reason: str, log_text: str) -> None:
    tail = log_text[-_TAIL_CAP:].strip()
    ctx.record_failure(f"{reason}\n--- log tail ---\n{tail}" if tail else reason)


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the script's process group."""
    del ctx
    if not isinstance(handle, dict):
        return
    pgid = int(handle.get("pgid") or handle.get("pid") or 0)
    if pgid <= 1:
        return
    try:
        _rs._signal_group(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass


def run_host(params: dict[str, Any]) -> str | None:
    """The node this job runs on: its ``params.target_node`` pin (ssh_node
    pins a job to the node that launched it), else this worker's
    ``PRECIS_NODE``; ``None`` for a node-less worker."""
    node = params.get("target_node") or os.environ.get("PRECIS_NODE")
    return str(node).strip() or None if node else None


def origin_repo(root: Path) -> str | None:
    """``owner/repo`` of the checkout's ``origin`` remote, else ``None``."""
    try:
        proc = _rs._git(["remote", "get-url", "origin"], root)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    return _release.repo_from_remote(proc.stdout)


# ── release_cut ────────────────────────────────────────────────────


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return factory_refusal(NAME) or todo_refusal(
        store, NAME, params.get("todo_id"), required=False
    )


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason = factory_refusal(NAME) or todo_refusal(
        ctx.store, NAME, params.get("todo_id"), required=False
    )
    if reason is not None:
        ctx.record_failure(reason)
        return None
    return launch_script(ctx, NAME, ["--cut-only"])


def poll(ctx: Any, handle: Any) -> bool:
    done = finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    cut = _release.parse_cut(log_text)
    if exit_code != 0 or cut is None:
        why = (
            f"exit {exit_code}"
            if exit_code != 0
            else "exit 0 but no RELEASE_CUT line (S already live, or a release "
            "is in flight elsewhere — nothing cut for this plan)"
        )
        fail_with_tail(ctx, f"{NAME}: scripts/release --cut-only {why}", log_text)
        return True
    rn, sha, rng = cut
    params = (ctx.meta or {}).get("params") or {}
    todo_id = params.get("todo_id")
    checkout = str(handle.get("root") or ".")
    repo = origin_repo(Path(checkout))
    host = run_host(params)
    notes: list[str] = []
    if isinstance(todo_id, int):
        try:
            notes = _release.record_cut(
                ctx.store,
                todo_id,
                rn,
                sha,
                rng,
                repo=repo,
                host=host,
                checkout=checkout,
            )
        except Exception as exc:
            log.warning("%s: record_cut on td%s failed", NAME, todo_id, exc_info=True)
            fail_with_tail(
                ctx,
                f"{NAME}: cut {rn} at {sha[:9]} but recording it on td{todo_id} "
                f"failed: {exc}",
                log_text,
            )
            return True
    ctx.set_meta(
        release_cut={
            "rN": rn,
            "sha": sha,
            "range": rng,
            "repo": repo,
            "host": host,
            "checkout": checkout,
        }
    )
    ctx.append_chunk(
        "job_summary",
        "\n".join(
            [
                f"release_cut: {rn} at {sha} (range {rng}, repo {repo or 'unknown'}, "
                f"host {host or 'unknown'}, checkout {checkout})"
            ]
            + [f"- {n}" for n in notes]
        ),
    )
    ctx.set_status("succeeded")
    return True


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "finish_run",
    "kill",
    "launch_script",
    "origin_repo",
    "poll",
    "run_host",
    "submit",
    "validate_submit",
    "wall_seconds",
]
