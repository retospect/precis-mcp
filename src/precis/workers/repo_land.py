"""Submit half of the second-repo lander (td659522 step 2, catpath).

``land_slice`` lands precis slices through the tree's own ``scripts/ship``.
A repo described by a :class:`~precis.plans.lanes.RepoProfile` has no such
script, so this module lands its slice branch from the profile's constants
alone: fetch the base tip, cut a throwaway *land tree* on it, squash the
slice branch in as ONE commit, refuse what the profile denies, and launch
the profile's setup, lint and test commands against that commit detached.
Not wired yet: nothing in production calls :func:`submit` until
``land_slice`` dispatches to it.

Trust boundary. The gate runs builder-written code on the host, so it
gets no credential: :func:`gate_env` is the scrubbed land env minus the
ssh agent, the gh config home and the docker endpoint. The credential is
used for exactly one call here, the base fetch, and that goes through
:func:`~precis.workers.job_types.run_slice.fetch_base_tip` (a fresh ops
gitdir outside the clone, the url the profile constant and never the
clone's config).
Every other git call is :func:`~precis.workers.job_types.land_slice._git`
(scrubbed env, hooks off). The gate commands come from the registry
constant, never from the tree. The squash sha and the tip it was cut on
ride the job handle, never a file the builder can write: the poll half
(next round) pushes only a sha that is a child of that tip.

Failures use ``land_slice``'s recorders: a conflict, a deny-path hit or a
version change is a charged attempt with a ``handoff:`` for the next
builder; an unreachable origin is attempt-free (not the builder's fault).
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import sys
import tomllib
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.workers.job_types import land_slice as _ls
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

#: Land-env names the gate must not see: the ssh agent socket, gh's config
#: home and the docker endpoint are host reach, not build inputs.
_GATE_DROP: tuple[str, ...] = ("SSH_AUTH_SOCK", "GH_CONFIG_DIR", "DOCKER_HOST")

#: Gate script, log and exit-file names under the run dir.
GATE_SCRIPT = "repo_gate.sh"
GATE_LOG = "repo_gate.log"
GATE_EXIT = "repo_gate.exit"

#: Gate phases in run order, with the profile field each reads.
_PHASES: tuple[tuple[str, str], ...] = (
    ("setup", "setup_cmds"),
    ("lint", "lint_cmds"),
    ("test", "test_cmds"),
)

_CONFLICT_LINE_RE = re.compile(r"^CONFLICT\b.*$", re.MULTILINE)


# ── Gate ───────────────────────────────────────────────────────────


def gate_env(profile: _lanes.RepoProfile) -> dict[str, str]:
    """The gate's environment: :func:`land_slice.land_env` minus the host
    reach names, plus ``UV_PYTHON`` when the profile pins one and the
    worker's ``UV_CACHE_DIR`` (as sessions get it). The ``PRECIS_*`` knobs
    are this repo's, not the other one's, and tell its tests nothing but
    the host layout, so they go too. Never a credential: a name matching
    the land env's secret pattern is dropped last, whatever put it there."""
    env = {k: v for k, v in _ls.land_env().items() if not k.startswith("PRECIS_")}
    for name in _GATE_DROP:
        env.pop(name, None)
    if profile.uv_python:
        env["UV_PYTHON"] = profile.uv_python
    env["UV_CACHE_DIR"] = str(_rs.uv_cache_dir())
    return {k: v for k, v in env.items() if not _ls._LAND_ENV_SECRET_RE.search(k)}


def gate_script(profile: _lanes.RepoProfile) -> str:
    """A POSIX shell script running the profile's setup, lint and test
    commands in order. The first failure prints ``✖ gate <phase> failed:
    <command>`` and exits with that command's status. Commands are the
    registry's constants, each run in a subshell."""
    lines = ["#!/bin/sh"]
    for phase, field in _PHASES:
        for cmd in getattr(profile, field):
            msg = shlex.quote(f"✖ gate {phase} failed: {cmd}")
            lines.append(f"( {cmd}\n) || {{ rc=$?; printf '%s\\n' {msg}; exit $rc; }}")
    lines.append("exit 0")
    return "\n".join(lines) + "\n"


def version_changed(tip_text: str | None, sha_text: str | None) -> bool:
    """Whether ``[project].version`` differs between two ``pyproject.toml``
    texts. Fails closed: ``True`` when either text is missing or does not
    parse, or carries no string version."""
    versions: list[str] = []
    for text in (tip_text, sha_text):
        if text is None:
            return True
        try:
            version = tomllib.loads(text).get("project", {}).get("version")
        except (tomllib.TOMLDecodeError, AttributeError):
            return True
        if not isinstance(version, str):
            return True
        versions.append(version)
    return versions[0] != versions[1]


# ── Land tree ──────────────────────────────────────────────────────


def _land_tree_path(root: Path, todo_id: int) -> Path:
    return _rs.worktrees_dir(root) / f"land-td{todo_id}"


def _drop_land_tree(root: Path, land_tree: Path) -> None:
    """Remove the land tree and its worktree record; never raises."""
    try:
        if land_tree.exists():
            _ls._git(["worktree", "remove", "--force", str(land_tree)], root)
        shutil.rmtree(land_tree, ignore_errors=True)
        _ls._git(["worktree", "prune"], root)
    except Exception:  # pragma: no cover - cleanup is best effort
        log.warning("repo_land: dropping %s failed", land_tree, exc_info=True)


def _land_tree(root: Path, todo_id: int, tip: str) -> Path | None:
    """A fresh detached worktree of ``tip`` at ``<root>/.claude/worktrees/
    land-td<id>``, any stale one removed first. ``None`` when git fails."""
    land_tree = _land_tree_path(root, todo_id)
    _drop_land_tree(root, land_tree)
    land_tree.parent.mkdir(parents=True, exist_ok=True)
    add = _ls._git(["worktree", "add", "--detach", str(land_tree), tip], root)
    return land_tree if add.returncode == 0 else None


def _squash(land_tree: Path, branch: str, ident: list[str]) -> tuple[str, str]:
    """``merge --squash`` of ``refs/heads/<branch>`` into the land tree.
    ``("ok", "")``; ``("conflict", <CONFLICT lines>)``; or
    ``("error", <git's message>)``."""
    proc = _ls._git([*ident, "merge", "--squash", f"refs/heads/{branch}"], land_tree)
    if proc.returncode == 0:
        return "ok", ""
    lines = _CONFLICT_LINE_RE.findall(proc.stdout or "")
    if lines:
        return "conflict", "\n".join(lines)
    return "error", ((proc.stderr or "") + (proc.stdout or "")).strip()[-500:]


def _read(cwd: Path, args: list[str]) -> str | None:
    """``git <args>`` stdout in ``cwd``, ``None`` on a non-zero exit."""
    proc = _ls._git(args, cwd)
    return proc.stdout if proc.returncode == 0 else None


def _staged_empty(land_tree: Path) -> bool | None:
    """Whether the index equals HEAD after the squash; ``None`` on a git
    failure."""
    proc = _ls._git(["diff", "--cached", "--quiet"], land_tree)
    if proc.returncode == 0:
        return True
    return False if proc.returncode == 1 else None


def _changed_paths(land_tree: Path, tip: str, sha: str) -> list[str] | None:
    """Paths the squash commit changes against ``tip`` (renames split);
    ``None`` when git cannot say. ``-z`` because a name carrying a tab or
    a non-ASCII byte is otherwise C-quoted whole (``core.quotePath``),
    and the quoted form never matches a deny glob (review td677929)."""
    out = _read(land_tree, ["diff", "--name-only", "--no-renames", "-z", tip, sha])
    if out is None:
        return None
    return sorted(p for p in out.split("\0") if p)


def _branch_author(root: Path, branch: str) -> tuple[str, str] | None:
    """The slice branch tip's author name and email."""
    out = _read(root, ["log", "-1", "--format=%an%x00%ae", f"refs/heads/{branch}"])
    if out is None or "\0" not in out:
        return None
    name, _, email = out.strip().partition("\0")
    return (name, email) if name and email else None


def _branch_subjects(root: Path, tip: str, branch: str) -> list[str]:
    out = _read(
        root,
        [
            "log",
            "--reverse",
            "--no-merges",
            "--format=%s",
            f"{tip}..refs/heads/{branch}",
        ],
    )
    return out.splitlines() if out else []


def _ident_args(root: Path, branch: str) -> list[str]:
    """``-c user.*`` args naming the slice branch tip's author. The
    ``GIT_AUTHOR_*``/``GIT_COMMITTER_*`` env, when set, still wins (git
    prefers it to config); git's global config is blinded, so without
    this a bare host has no identity (``merge --squash`` wants one too)."""
    author = _branch_author(root, branch)
    if author is None:
        return []
    return ["-c", f"user.name={author[0]}", "-c", f"user.email={author[1]}"]


def _commit(land_tree: Path, ident: list[str], subject: str, tip: str) -> str | None:
    """Commit the staged squash and return its sha, or ``None`` when the
    commit fails or its parent is not ``tip``."""
    proc = _ls._git(
        [*ident, "commit", "--quiet", "--no-verify", "--no-gpg-sign", "-m", subject],
        land_tree,
    )
    if proc.returncode != 0:
        return None
    sha = (_read(land_tree, ["rev-parse", "HEAD"]) or "").strip()
    parent = (_read(land_tree, ["rev-parse", "HEAD^"]) or "").strip()
    return sha if sha and parent == tip else None


# ── Submit ─────────────────────────────────────────────────────────


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: land the slice branch of ``params.repo`` and
    launch its gate. Returns the handle, ``{"no_land": True, ...}`` for a
    branch with nothing new, or ``None`` after driving the job terminal."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    todo_id = params.get("todo_id")
    if _rs.factory_off():
        ctx.record_failure("repo_land: the factory is off (PRECIS_FACTORY=off)")
        return None
    reason = _ls._todo_rejection(ctx.store, params)
    if reason is not None:
        ctx.record_failure(reason.replace("run_slice:", "repo_land:", 1))
        return None
    assert isinstance(todo_id, int)
    lane = params.get("lane")
    repo = _rs.job_repo(params)
    profile = _lanes.repo_profile(repo)
    if profile is None or not profile.remote:
        ctx.record_failure(f"repo_land: no build profile with a remote for {repo!r}")
        return None
    try:
        root = _rs.slice_root_for(profile.name)
    except RuntimeError as exc:
        ctx.record_failure(f"repo_land: {exc}")
        return None
    branch = str(params.get("branch") or _rs.slice_branch(todo_id))
    slice_tree = str(params.get("worktree") or "") or str(
        _rs.worktrees_dir(root) / _rs.slice_name(todo_id)
    )

    def fail(why: str, handoff: str, *, attempt_free: str | None = None) -> None:
        _ls._fail(ctx, todo_id, lane, why, handoff=handoff, attempt_free=attempt_free)

    # 2. The base tip, fetched under the credential through the ops gitdir.
    tip = _rs.fetch_base_tip(profile, root)
    if tip is None:
        fail(
            f"could not fetch origin/{profile.base} of {profile.name}",
            f"land FAILED: origin/{profile.base} of {profile.name} could not be "
            "fetched (infrastructure, not the branch); the land is retried.",
            attempt_free="origin-fetch",
        )
        return None
    # 3. A throwaway tree on that tip.
    land_tree = _land_tree(root, todo_id, tip)
    if land_tree is None:
        fail(
            f"git worktree add failed for the land tree of td{todo_id}",
            "land FAILED: the land tree could not be created; the land is retried.",
            attempt_free="land-tree",
        )
        return None

    def refuse(why: str, handoff: str) -> None:
        _drop_land_tree(root, land_tree)
        fail(why, handoff)

    # 4. Squash.
    ident = _ident_args(root, branch)
    state, text = _squash(land_tree, branch, ident)
    if state != "ok":
        if state == "conflict":
            refuse(
                f"merge conflict squashing {branch} onto origin/{profile.base}\n{text}",
                f"land FAILED: {branch} conflicts with origin/{profile.base}; "
                f"rebase on origin/{profile.base} and resolve:\n{text}",
            )
        else:
            refuse(
                f"squash of {branch} failed: {text}",
                f"land FAILED: git could not squash {branch} ({text}).",
            )
        return None
    # 5. Nothing new to land.
    empty = _staged_empty(land_tree)
    if empty is None:
        refuse(
            f"git diff failed in the land tree of td{todo_id}",
            "land FAILED: git could not read the squash; rebuild the slice.",
        )
        return None
    if empty:
        _drop_land_tree(root, land_tree)
        return {"no_land": True, "worktree": slice_tree, "repo": profile.name}
    # 6. Commit.
    subject = _ls.squash_subject(
        _branch_subjects(root, tip, branch),
        todo_id,
        node=_ls._node_subject(ctx.store, todo_id),
    )
    if subject is None:
        refuse(
            f"nothing to land on {branch} (no commit ahead of origin/{profile.base})",
            f"land FAILED: {branch} has no commit ahead of origin/{profile.base}; "
            "commit the work on the branch.",
        )
        return None
    sha = _commit(land_tree, ident, subject, tip)
    if sha is None:
        refuse(
            f"squash commit failed or is not a child of {tip[:9]}",
            "land FAILED: git could not commit the squash on origin/"
            f"{profile.base}; rebuild the slice.",
        )
        return None
    # 7. Deny paths and the version line.
    paths = _changed_paths(land_tree, tip, sha)
    if paths is None:
        refuse(
            f"changed-path enumeration failed in {land_tree} (git failed); "
            "failing closed",
            "land FAILED: git could not enumerate the changed paths, so the "
            "deny gate cannot run.",
        )
        return None
    deny = (
        profile.deny_paths
        if profile.deny_paths is not None
        else _lanes.deny_paths_for(ctx.store)
    )
    denied = _rs.deny_violations(paths, deny)
    if denied:
        ctx.set_meta(**{_rs.HUMAN_ONLY_KEY: denied[:50]})
        refuse(
            "branch touches deny path(s): " + ", ".join(denied[:50]),
            "land FAILED: the branch touches deny path(s): "
            + ", ".join(f"`{p}`" for p in denied[:50])
            + " - no lane may write them; a coordinator ruling lands the "
            "branch by hand or rewrites the work.",
        )
        return None
    if "pyproject.toml" in paths and version_changed(
        _read(land_tree, ["show", f"{tip}:pyproject.toml"]),
        _read(land_tree, ["show", f"{sha}:pyproject.toml"]),
    ):
        ctx.set_meta(**{_rs.HUMAN_ONLY_KEY: ["pyproject.toml"]})
        refuse(
            "the branch changes [project].version in pyproject.toml",
            "land FAILED: releases are a separate node; the version line of "
            "pyproject.toml may not change in a slice. Revert that line.",
        )
        return None
    # 8. Launch the gate detached in the land tree.
    try:
        run = _rs.run_dir(todo_id)
        script = run / GATE_SCRIPT
        log_path = run / GATE_LOG
        exit_path = run / GATE_EXIT
        script.write_text(gate_script(profile), encoding="utf-8")
        exit_path.unlink(missing_ok=True)
        pid = _rs._launch(
            ["/bin/sh", str(script)],
            cwd=land_tree,
            stdin=Path(os.devnull),
            log=log_path,
            exit_file=exit_path,
            env=gate_env(profile),
        )
    except Exception as exc:
        log.warning("repo_land: gate launch for td%d failed", todo_id, exc_info=True)
        refuse(
            f"gate launch failed: {exc}",
            f"land FAILED: the gate did not start ({exc}).",
        )
        return None
    # 9. The handle.
    launched = _ls._now()
    handle: dict[str, Any] = {
        "lander": "repo_land",
        "repo": profile.name,
        "pid": pid,
        "pgid": pid,
        "worktree": str(land_tree),
        "land_tree": str(land_tree),
        "slice_tree": slice_tree,
        "branch": branch,
        "tip": tip,
        "sha": sha,
        "subject": subject,
        "log": str(log_path),
        "exit_file": str(exit_path),
        "started_at": launched.strftime(_ls._TS_FMT),
        "deadline": launched.timestamp() + _ls.wall_seconds(),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    ps_identity = (
        _rs._darwin_process_identity(pid) if sys.platform == "darwin" else None
    )
    if ps_identity is not None:
        handle["ps_identity"] = ps_identity
    ctx.set_meta(land_subject=subject)
    ctx.append_chunk(
        "job_event",
        f"repo_land: {profile.name} gate pid {pid} on {sha[:9]} (tip {tip[:9]}) "
        f"in {land_tree}",
    )
    return handle
