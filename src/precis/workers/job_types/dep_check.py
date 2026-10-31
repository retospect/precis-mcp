"""``dep_check`` — resolve a Dependabot PR's lock, never run it (td543749).

Spec: ``docs/backlog/dark-factory.md`` § "9. Inbound pull requests"; the
``check`` node of a ``dependabot`` ``pr-intake`` plan. Mechanical, no LLM.
A detached submit/poll pair under ``ssh_node`` (the ``release_cut`` shape):
:func:`submit` launches ``python -m precis.workers.job_types.dep_check`` in
the slice checkout (:func:`precis.workers.job_types.run_slice.slice_root`)
and :func:`poll` reads its ``DEP_CHECK: {json}`` result line.

The check (:func:`run_check`), in worktree ``slice-pr-<n>`` on branch
``slice/pr-<n>`` under the checkout:

1. Fetch the PR head into ``slice/pr-<n>`` (a stale worktree of an earlier
   attempt is removed first) — by the sha the intake classified
   (``meta.pr.head_sha``: ``git fetch origin <sha>``), falling back to
   ``+pull/<n>/head:refs/heads/slice/pr-<n>`` (:func:`fetch_pr_head`) — and
   ``git worktree add``; ``git rev-parse HEAD`` must be that sha — a head
   that moved since is refused.
2. ``uv lock --check --no-build``. A stale lock (Dependabot's pip updater
   bumps the ``pyproject.toml`` floor and leaves ``uv.lock`` alone) is
   re-resolved with ``uv lock --no-build`` and committed on the branch as
   ``build(deps): relock uv.lock for PR #<n>``, so the land carries a lock
   that matches the floor.
3. ``uv sync --dry-run --locked --no-build --python <v>`` for each of
   :data:`PYTHONS` (3.12, 3.13), into a throwaway environment path.

Resolve only: ``--no-build`` means no source distribution is built (a
build runs the package's own code), and nothing is installed, imported or
run. The uv env is an allowlist (:data:`ENV_ALLOWLIST` plus ``UV_*`` /
``XDG_*`` / proxy variables): no DB DSN or vault variable reaches it.

Green: the result goes on the plan root (``meta.pr.dep_check``) and the
plan's ``land_slice`` node gets ``params.worktree`` + ``params.branch`` and,
when ``PRECIS_NODE`` is set, ``params.target_node`` — the land runs on this
host, in this worktree, with the relock commit. Red (a fetch, lock or
resolve failure): the job fails with the step and the uv tail, the worktree
and branch are removed; the plan tick retries, then files the retry-cap ask.

Refused while ``PRECIS_FACTORY=off``. Git and uv go through the
:func:`_run` seam, the launch through :func:`_launch`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import pr_class as _pr_class
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "dep_check"

#: The pinned Pythons the lock must resolve for.
PYTHONS: tuple[str, ...] = ("3.12", "3.13")
RESULT_PREFIX = "DEP_CHECK: "
_DEFAULT_WALL_S = 30 * 60
_TAIL_CAP = 1500
_STEP_TIMEOUT_S = 900
_RESULT_RE = re.compile(r"^DEP_CHECK: (\{.*\})\s*$", re.MULTILINE)

#: Parent-env names uv inherits (besides ``UV_*``, ``XDG_*``, proxies).
ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "TZ",
    "SSL_CERT_FILE",
)
_ENV_PREFIXES: tuple[str, ...] = ("UV_", "XDG_")
_PROXY_NAMES = frozenset(
    {"HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy"}
)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's check node (the PR number comes from its root's meta.pr).
        "todo_id": {"type": "integer"},
        # Explicit PR number (overrides meta.pr.number).
        "pr": {"type": "integer"},
        # ssh_node's host pin (a host with the checkout, git and uv).
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Dependabot PR check: fetch the PR head into slice/pr-<n>, relock when "
    "uv.lock is stale (committed on the branch), resolve the lock for python "
    "3.12 and 3.13 with uv --no-build (never builds, imports or runs the "
    "dependency); green pins the plan's land to this worktree. Refused while "
    "PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


def pr_branch(number: int) -> str:
    """The land branch of a PR: ``slice/pr-<n>``."""
    return f"slice/pr-{int(number)}"


def pr_tree(root: Path, number: int) -> Path:
    """The PR's worktree: ``<root>/.claude/worktrees/slice-pr-<n>``."""
    return root / ".claude" / "worktrees" / f"slice-pr-{int(number)}"


def uv_bin() -> str:
    """``PRECIS_UV_BIN`` (the test seam), else ``uv`` on PATH."""
    return os.environ.get("PRECIS_UV_BIN") or "uv"


def uv_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment uv runs with (module docstring)."""
    src = os.environ if base is None else base
    env = {k: v for k, v in src.items() if k in ENV_ALLOWLIST}
    env.update(
        {
            k: v
            for k, v in src.items()
            if k.startswith(_ENV_PREFIXES) or k in _PROXY_NAMES
        }
    )
    env["UV_NO_BUILD"] = "1"
    return env


# ── Seams ──────────────────────────────────────────────────────────


def _run(
    argv: list[str], cwd: Path, env: dict[str, str] | None = None
) -> subprocess.CompletedProcess[str]:
    """Run ``argv`` in ``cwd``; never raises on a non-zero exit."""
    try:
        return subprocess.run(
            argv,
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=_STEP_TIMEOUT_S,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return subprocess.CompletedProcess(argv, 127, stdout="", stderr=str(exc))


def _launch(argv: list[str], *, cwd: Path, log_path: Path, exit_file: Path) -> int:
    """Start ``argv`` detached (run_slice's launcher, stdin from /dev/null)."""
    return _rs._launch(
        argv,
        cwd=cwd,
        stdin=Path(os.devnull),
        log=log_path,
        exit_file=exit_file,
        env=dict(os.environ),
    )


def _run_dir() -> Path:
    path = Path(tempfile.gettempdir()) / "precis-pr-ops"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ── The check (runs in the detached child) ─────────────────────────


def _tail(proc: subprocess.CompletedProcess[str]) -> str:
    text = ((proc.stderr or "") + "\n" + (proc.stdout or "")).strip()
    return text[-_TAIL_CAP:]


def fetch_pr_head(
    git: Callable[[list[str]], subprocess.CompletedProcess[str]],
    number: int,
    head: str | None,
    branch: str,
) -> subprocess.CompletedProcess[str]:
    """Fetch PR ``number`` into local ``branch`` (``git`` runs git args in
    the checkout). With a full ``head`` sha (the one the intake classified)
    it is fetched by sha (``git fetch origin <sha>``) and the branch set to
    it; when the server refuses a by-sha fetch it falls back to the pull ref
    ``pull/<n>/head``. The caller verifies the result with ``git rev-parse``
    — the pull ref may have moved, a sha cannot."""
    sha = (head or "").strip().lower()
    if _pr_class.SHA40_RE.fullmatch(sha):
        got = git(["fetch", "origin", sha])
        if got.returncode == 0:
            made = git(["branch", "-f", branch, sha])
            if made.returncode == 0:
                return made
    return git(["fetch", "origin", f"+pull/{int(number)}/head:refs/heads/{branch}"])


def run_check(
    root: Path, number: int, *, head: str | None = None, run: Any = None
) -> dict[str, Any]:
    """The check (module docstring). Returns ``{pr, worktree, branch, head,
    lock: current|relocked|failed, pythons: {v: ok|failed}, ok, failed_step,
    tail}``; ``ok`` False leaves no worktree or branch behind."""
    run = run or _run
    tree, branch = pr_tree(root, number), pr_branch(number)
    env = uv_env()
    uv = uv_bin()
    out: dict[str, Any] = {
        "pr": int(number),
        "worktree": str(tree),
        "branch": branch,
        "head": "",
        "lock": "",
        "pythons": {},
        "ok": False,
        "failed_step": "",
        "tail": "",
    }

    def fail(
        step: str, proc: subprocess.CompletedProcess[str] | None
    ) -> dict[str, Any]:
        out["failed_step"] = step
        out["tail"] = _tail(proc) if proc is not None else ""
        run(["git", "worktree", "remove", "--force", str(tree)], root)
        run(["git", "branch", "-D", branch], root)
        return out

    if tree.exists():
        run(["git", "worktree", "remove", "--force", str(tree)], root)
    fetch = fetch_pr_head(lambda args: run(["git", *args], root), number, head, branch)
    if fetch.returncode != 0:
        return fail("git fetch", fetch)
    add = run(["git", "worktree", "add", str(tree), branch], root)
    if add.returncode != 0:
        return fail("git worktree add", add)
    rev = run(["git", "rev-parse", "HEAD"], tree)
    out["head"] = rev.stdout.strip()
    if head and not out["head"].startswith(head.strip().lower()[:40]):
        proc = subprocess.CompletedProcess(
            [], 1, stdout="", stderr=f"fetched {out['head']}, intake saw {head}"
        )
        return fail("head moved since intake", proc)
    check = run([uv, "lock", "--check", "--no-build"], tree, env)
    if check.returncode == 0:
        out["lock"] = "current"
    else:
        relock = run([uv, "lock", "--no-build"], tree, env)
        if relock.returncode != 0:
            out["lock"] = "failed"
            return fail("uv lock", relock)
        run(["git", "add", "uv.lock"], tree)
        commit = run(
            [
                "git",
                "-c",
                "user.name=precis dep_check",
                "-c",
                "user.email=dep-check@users.noreply.github.com",
                "commit",
                "-m",
                f"build(deps): relock uv.lock for PR #{int(number)}",
            ],
            tree,
        )
        if commit.returncode != 0:
            return fail("git commit uv.lock", commit)
        out["lock"] = "relocked"
    for py in PYTHONS:
        venv = tree / "scratch" / f"dep-check-venv-{py}"
        proc = run(
            [uv, "sync", "--dry-run", "--locked", "--no-build", "--python", py],
            tree,
            {**env, "UV_PROJECT_ENVIRONMENT": str(venv)},
        )
        out["pythons"][py] = "ok" if proc.returncode == 0 else "failed"
        if proc.returncode != 0:
            return fail(f"uv sync --dry-run --python {py}", proc)
    out["ok"] = True
    return out


def parse_result(log_text: str) -> dict[str, Any] | None:
    """The last ``DEP_CHECK: {json}`` line of the child's output."""
    found = _RESULT_RE.findall(log_text)
    if not found:
        return None
    try:
        data = json.loads(found[-1])
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def main(argv: list[str] | None = None) -> int:
    """The detached child: run the check, print the result line."""
    ap = argparse.ArgumentParser(prog="dep_check")
    ap.add_argument("--root", required=True)
    ap.add_argument("--pr", type=int, required=True)
    ap.add_argument("--head", default="")
    args = ap.parse_args(argv)
    res = run_check(Path(args.root), args.pr, head=args.head or None)
    print(RESULT_PREFIX + json.dumps(res, sort_keys=True), flush=True)
    return 0 if res.get("ok") else 1


# ── Job protocol ───────────────────────────────────────────────────


def _pr_number(store: Any, params: dict[str, Any]) -> tuple[int | None, dict[str, Any]]:
    from precis.plans import pr_class

    pr = pr_class.pr_of(store, int(params["todo_id"])) if store is not None else None
    explicit = params.get("pr")
    if isinstance(explicit, int) and explicit > 0:
        return explicit, pr or {}
    if pr is None:
        return None, {}
    return int(pr["number"]), pr


def _refusal(store: Any, params: dict[str, Any]) -> str | None:
    from precis.workers.job_types import release_cut as _rc

    reason = _rc.factory_refusal(NAME) or _rc.todo_refusal(
        store, NAME, params.get("todo_id"), required=True
    )
    if reason is not None:
        return reason
    number, _pr = _pr_number(store, params)
    if number is None:
        return (
            f"{NAME}: td{params.get('todo_id')} is in no pr-intake plan (no "
            "meta.pr on its root); next: pass params.pr"
        )
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return _refusal(store, params)


def submit(ctx: Any, spec: Any) -> Any:
    """Launch the check detached; the handle, or ``None`` after failing."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason = _refusal(ctx.store, params)
    if reason is not None:
        ctx.record_failure(reason)
        return None
    number, pr = _pr_number(ctx.store, params)
    assert number is not None
    try:
        root = _rs.slice_root()
        base = _run_dir() / f"{NAME}-{ctx.ref_id}"
        log_path = base.with_suffix(".log")
        exit_file = base.with_suffix(".exit")
        exit_file.unlink(missing_ok=True)
        argv = [
            sys.executable,
            "-m",
            "precis.workers.job_types.dep_check",
            "--root",
            str(root),
            "--pr",
            str(number),
        ]
        if pr.get("head_sha"):
            argv += ["--head", str(pr["head_sha"])]
        pid = _launch(argv, cwd=root, log_path=log_path, exit_file=exit_file)
    except Exception as exc:
        log.warning("%s: launch failed", NAME, exc_info=True)
        ctx.record_failure(f"{NAME}: launch failed: {exc}")
        return None
    ctx.append_chunk("job_event", f"{NAME}: PR #{number} pid {pid} in {root}")
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "root": str(root),
        "pr": number,
        "log": str(log_path),
        "exit_file": str(exit_file),
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def _pin_land(store: Any, todo_id: int, res: dict[str, Any]) -> list[str]:
    """Stamp the result on the plan root and pin the plan's open
    ``land_slice`` node(s) for this PR to the worktree (and host)."""
    from precis.plans import release as _release

    root = _release.plan_root(store, todo_id)
    notes: list[str] = []
    ref = store.get_ref(kind="todo", id=root)
    pr = dict((ref.meta or {}).get("pr") or {}) if ref is not None else {}
    if pr:
        keep = {k: res[k] for k in ("head", "lock", "pythons", "branch", "worktree")}
        store.stamp_ref_meta(root, {"pr": {**pr, "dep_check": keep}})
        notes.append(f"td{root}: meta.pr.dep_check = lock {res['lock']}")
    node = os.environ.get("PRECIS_NODE", "").strip()
    for node_id, meta, status in _release._plan_nodes(store, root):
        op = meta.get("op")
        if status in ("done", "won't-do", "cancelled") or not isinstance(op, dict):
            continue
        raw = op.get("params")
        params: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        if op.get("type") != "land_slice" or params.get("pr") != res["pr"]:
            continue
        pinned = {**params, "worktree": res["worktree"], "branch": res["branch"]}
        if node:
            pinned["target_node"] = node
        store.stamp_ref_meta(node_id, {"op": {**op, "params": pinned}})
        notes.append(
            f"td{node_id}: land pinned to {res['worktree']}"
            + (f" on {node}" if node else "")
        )
    return notes


def poll(ctx: Any, handle: Any) -> bool:
    from precis.workers.job_types import release_cut as _rc

    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    res = parse_result(log_text)
    number = handle.get("pr")
    if res is None or not res.get("ok") or exit_code != 0:
        if res is None:
            why = f"exit {exit_code}, no {RESULT_PREFIX.strip()} line"
        else:
            why = (
                f"{res.get('failed_step') or 'failed'} (lock {res.get('lock') or '?'})"
            )
            tail = str(res.get("tail") or "").strip()
            if tail:
                log_text = tail
        if isinstance(res, dict):
            ctx.set_meta(dep_check=res)
        _rc.fail_with_tail(ctx, f"{NAME}: PR #{number} red: {why}", log_text)
        return True
    todo_id = int(((ctx.meta or {}).get("params") or {}).get("todo_id") or 0)
    try:
        notes = _pin_land(ctx.store, todo_id, res)
    except Exception as exc:
        log.warning(
            "%s: pinning the land for td%d failed", NAME, todo_id, exc_info=True
        )
        _rc.fail_with_tail(
            ctx, f"{NAME}: PR #{number} green but recording it failed: {exc}", ""
        )
        return True
    ctx.set_meta(dep_check=res)
    pys = ", ".join(f"{k} {v}" for k, v in sorted(res["pythons"].items()))
    ctx.append_chunk(
        "job_summary",
        "\n".join(
            [
                f"{NAME}: PR #{number} green at {res['head'][:9]}: lock {res['lock']}; {pys}"
            ]
            + [f"- {n}" for n in notes]
        ),
    )
    ctx.set_status("succeeded")
    return True


def kill(ctx: Any, handle: Any) -> None:
    from precis.workers.job_types import release_cut as _rc

    _rc.kill(ctx, handle)


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "ENV_ALLOWLIST",
    "NAME",
    "PARAMS_SCHEMA",
    "PYTHONS",
    "REQUIRES",
    "kill",
    "main",
    "parse_result",
    "poll",
    "pr_branch",
    "pr_tree",
    "run_check",
    "submit",
    "uv_env",
    "validate_submit",
    "wall_seconds",
]


if __name__ == "__main__":  # pragma: no cover — the detached child
    raise SystemExit(main())
