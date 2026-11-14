"""The second-repo lander, submit and poll halves (td659522 step 2, catpath).

``land_slice`` lands precis slices through the tree's own ``scripts/ship``.
A repo described by a :class:`~precis.plans.lanes.RepoProfile` has no such
script, so this module lands its slice branch from the profile's constants
alone: fetch the base tip, cut a throwaway *land tree* on it, squash the
slice branch in as ONE commit, refuse what the profile denies, and launch
the profile's setup, lint and test commands against that commit detached.
``land_slice`` keeps the job type and hands :func:`submit`, :func:`poll`
and :func:`kill` a job whose repo is not the default, once
``land_slice._repo_refusal`` admits it.

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
ride the job handle, never a file the builder can write.

Push boundary. A green gate is followed by exactly one credentialed call,
:func:`~precis.workers.factory_git_mirror.push_cas`: a plain fast-forward
push (no ``+`` refspec, force or lease) from a fresh ops gitdir outside the
clone, checked to descend from the handle's ``tip`` before the network is
touched and reconciled against the ``ls-remote`` oracle afterwards. The
handle's ``sha`` is the only thing ever pushed; no ref or file in the clone
is read to decide what to push, and the poll first re-confirms that
``sha^`` is ``tip`` and that the diff still clears the deny paths.

Failures use ``land_slice``'s recorders: a conflict, a deny-path hit, a
version change or a red gate is a charged attempt with a ``handoff:`` for
the next builder; an unreachable or raced origin, a killed gate and a
setup failure that no dependency file explains are attempt-free (not the
builder's fault), which makes the dispatcher mint the land again.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import sys
import tomllib
from pathlib import Path
from typing import Any

from precis.plans import lanes as _lanes
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
from precis.workers import factory_git_mirror as _mirror
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
    # The index, not git's (localised) prose, says whether it was a conflict.
    unmerged = _read(land_tree, ["diff", "--name-only", "-z", "--diff-filter=U"])
    paths = sorted({p for p in (unmerged or "").split("\0") if p})
    if paths:
        lines = _CONFLICT_LINE_RE.findall(proc.stdout or "") or [
            f"CONFLICT (content): Merge conflict in {p}" for p in paths
        ]
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
    if not any(getattr(profile, field) for _phase, field in _PHASES):
        # An empty script exits 0: a profile with no gate would land ungated.
        ctx.record_failure(f"repo_land: the profile for {repo!r} has no gate commands")
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
                f"land FAILED: {branch} {_rs.LANDER_CONFLICT_MARK}{profile.base}; "
                f"run `git merge origin/{profile.base}`, resolve the conflicts "
                f"and commit:\n{text}",
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
        "worktree": slice_tree,
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


# ── Poll ───────────────────────────────────────────────────────────

_GATE_FAIL_RE = re.compile(r"^✖ gate (\w+) failed: (.*)$", re.MULTILINE)

#: Files whose change in the diff makes a failed ``setup`` the branch's.
_DEP_FILES: frozenset[str] = frozenset({"pyproject.toml", "uv.lock"})

#: ``red_class`` of the attempt-free failures (``meta.attempt_free ==
#: "infra-land"``, so ``plan_dispatch._reland_params`` mints the land again).
RACED_CLASS = "origin-raced"
PUSH_ERROR_CLASS = "origin-push"
HANDLE_CLASS = "land-handle"
CLOSED_CLASS = "repo-closed"
GATE_INFRA_CLASS = "gate infrastructure"
SETUP_INFRA_CLASS = "gate setup"
TIMED_OUT_CLASS = "timed-out"


def _drop_slice(root: Path, slice_tree: Path, branch: str) -> list[str]:
    """Remove the slice worktree (only one under the repo's worktrees dir)
    and delete the local slice branch; one note per step."""
    notes: list[str] = []
    if slice_tree.parent == _rs.worktrees_dir(root) and slice_tree.exists():
        proc = _ls._git(["worktree", "remove", "--force", str(slice_tree)], root)
        notes.append("worktree remove: " + ("ok" if proc.returncode == 0 else "FAILED"))
        shutil.rmtree(slice_tree, ignore_errors=True)
        _ls._git(["worktree", "prune"], root)
    if branch:
        proc = _ls._git(["branch", "-D", branch], root)
        notes.append("branch -D: " + ("ok" if proc.returncode == 0 else "FAILED"))
    return notes


def _cleanup(root: Path, slice_tree: Path, land_tree: Path, branch: str) -> list[str]:
    """The green path's tidy-up: the land tree, the slice worktree and the
    local slice branch (never pushed, so no origin delete). Best effort:
    never raises."""
    notes: list[str] = []
    try:
        _drop_land_tree(root, land_tree)
        notes.append("land tree dropped")
        notes += _drop_slice(root, slice_tree, branch)
    except Exception as exc:  # pragma: no cover - cleanup is best effort
        log.warning("repo_land: cleanup failed", exc_info=True)
        notes.append(f"cleanup FAILED ({exc})")
    return notes


def _hex(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else ""


def _pid(value: Any) -> int:
    """A handle's pid field as an int; 0 for anything that is not one."""
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _signal_gate(handle: dict[str, Any]) -> None:
    """SIGTERM the gate's process group, never the worker's own (td652194)."""
    pgid = _pid(handle.get("pgid")) or _pid(handle.get("pid"))
    own = {os.getpid(), os.getpgrp()}
    if pgid in own or _pid(handle.get("pid")) in own:
        log.warning("repo_land: kill refused - handle names the worker (%d)", pgid)
    elif pgid > 1:
        try:
            _rs._signal_group(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass


def _handle_root(handle: dict[str, Any]) -> Path | None:
    try:
        return _rs.slice_root_for(str(handle.get("repo") or ""))
    except RuntimeError:
        return None


def _red_handoff(
    phase: str, command: str, exit_code: int | None, masked_log: str
) -> tuple[str, str]:
    """``(what failed, handoff)`` for a red gate: the phase, the failing
    command, the failing test ids and the scrubbed error lines or excerpt."""
    where = f"gate {phase} failed: {command}" if phase else "gate failed"
    code = "unknown" if exit_code is None else str(exit_code)
    handoff = (
        f"land FAILED: {where} (exit {code}). Fix it on the branch, commit, "
        "leave the tree clean; the lander re-squashes and re-runs the gate."
    )
    ids = _ls.failed_test_ids(masked_log)
    if ids:
        handoff += "\n\nFailing tests: " + ", ".join(ids[:20])
    errors = _ls.gate_error_lines(masked_log)
    detail = errors or _ls.gate_red_excerpt(masked_log)
    if errors:
        handoff += f"\n\nThe gate's error lines:\n{errors}"
    elif detail:
        handoff += f"\n\nThe gate's output before the verdict:\n{detail}"
    return where, handoff


def _poll_red(
    ctx: Any,
    handle: dict[str, Any],
    root: Path,
    exit_code: int | None,
    todo_id: int,
    lane: Any,
) -> bool:
    """The non-zero-exit path. Attempt-free when the host, not the branch,
    is at fault: a killed gate (exit 137 or a kill line) or a ``setup``
    failure with no dependency file in the diff; charged otherwise."""
    masked = mask_secrets(_rs._read_log(handle)) or ""
    found = _GATE_FAIL_RE.findall(masked)
    phase, command = found[-1] if found else ("", "")
    where, handoff = _red_handoff(phase, command, exit_code, masked)
    red_class: str | None = None
    if (
        # No exit file: the launcher never got to write one, so the gate was
        # killed from outside (a worker restart, a reboot), not failed.
        exit_code is None
        or exit_code == 137
        or _ls.gate_kill_line(masked, exit_code) is not None
    ):
        red_class = GATE_INFRA_CLASS
    elif phase == "setup":
        paths = _changed_paths(root, str(handle["tip"]), str(handle["sha"]))
        if paths is not None and not _DEP_FILES.intersection(paths):
            red_class = SETUP_INFRA_CLASS
    ctx.set_meta(exit_code=exit_code)
    tail = masked[-_ls._TAIL_CAP :].strip()
    ctx.append_chunk(
        "job_summary",
        f"repo_land td{todo_id}: RED - {where}\nsubject: {handle.get('subject')}",
    )
    _drop_land_tree(root, _land_tree_path(root, todo_id))
    _ls._fail(
        ctx,
        todo_id,
        lane,
        f"gate of td{todo_id} red: {where}"
        + (f"\n--- log tail ---\n{tail}" if tail else ""),
        handoff=handoff,
        attempt_free=red_class,
    )
    return True


def _poll_green(
    ctx: Any,
    handle: dict[str, Any],
    root: Path,
    profile: _lanes.RepoProfile,
    todo_id: int,
    lane: Any,
) -> bool:
    """Exit 0: re-check the handle's sha, push it, record the land."""
    sha, tip = _hex(handle.get("sha")), _hex(handle.get("tip"))
    land_tree = _land_tree_path(root, todo_id)

    def free(why: str, handoff: str, red_class: str) -> bool:
        _drop_land_tree(root, land_tree)
        _ls._fail(ctx, todo_id, lane, why, handoff=handoff, attempt_free=red_class)
        return True

    parent = _read(root, ["rev-parse", f"{sha}^"]) if sha and tip else None
    if parent is None or parent.strip() != tip:
        return free(
            f"squash {sha[:9] or '?'} is not a child of {tip[:9] or '?'}; not pushing",
            "land FAILED: the squash commit no longer sits on the base tip it "
            "was gated against (infrastructure); the land is retried.",
            HANDLE_CLASS,
        )
    paths = _changed_paths(root, tip, sha)
    if paths is None:
        return free(
            "changed-path enumeration failed before the push; failing closed",
            "land FAILED: git could not enumerate the changed paths "
            "(infrastructure); the land is retried.",
            HANDLE_CLASS,
        )
    deny = (
        profile.deny_paths
        if profile.deny_paths is not None
        else _lanes.deny_paths_for(ctx.store)
    )
    denied = _rs.deny_violations(paths, deny)
    if denied:
        ctx.set_meta(**{_rs.HUMAN_ONLY_KEY: denied[:50]})
        _drop_land_tree(root, land_tree)
        _ls._fail(
            ctx,
            todo_id,
            lane,
            "branch touches deny path(s): " + ", ".join(denied[:50]),
            handoff="land FAILED: the branch touches deny path(s): "
            + ", ".join(f"`{p}`" for p in denied[:50])
            + " - no lane may write them.",
        )
        return True
    # Admission is re-read at the last moment before the one outward write:
    # the factory switch, and everything ``land_slice._repo_refusal`` asks at
    # submit (the profile's lander, ``factory.open_repos``, the todo's repo
    # tag against the job's param). A repo closed or a todo retagged while
    # the gate ran is not pushed to.
    params = dict((ctx.meta or {}).get("params") or {})
    closed = (
        "the factory is off"
        if _rs.factory_off()
        else f"the handle's repo is not the job's ({_rs.job_repo(params)!r})"
        if _rs.job_repo(params) != profile.name
        else _ls._repo_refusal(ctx.store, params)
    )
    if closed is not None:
        return free(
            f"repo {profile.name!r} is no longer admitted ({closed}); not pushing",
            "land FAILED: the repo was closed to the factory while the gate ran "
            "(not the branch); the land is retried once it is open.",
            CLOSED_CLASS,
        )
    # Stamped BEFORE the push: once the sha can be on the other repo's
    # main, every reader of this todo's ``landed:`` line must be able to
    # tell it is not a precis sha, whatever fails afterwards.
    try:
        ctx.store.stamp_ref_meta(todo_id, {_lanes.LANDED_REPO_KEY: profile.name})
    except Exception as exc:
        log.warning("repo_land: landed_repo stamp on td%d failed", todo_id)
        return free(
            f"could not stamp {_lanes.LANDED_REPO_KEY} on td{todo_id} ({exc}); "
            "not pushing",
            "land FAILED: the todo could not be marked before the push "
            "(infrastructure); the land is retried.",
            PUSH_ERROR_CLASS,
        )
    common = _ls._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    if common.returncode != 0 or not common.stdout.strip():
        return free(
            "could not locate the clone's object store",
            "land FAILED: the object store was not found (infrastructure); "
            "the land is retried.",
            PUSH_ERROR_CLASS,
        )
    env = _factory_git.git_auth_env(base=_ls.land_env())
    env["GIT_TERMINAL_PROMPT"] = "0"
    outcome, detail = _mirror.push_cas(
        profile.remote,
        profile.base,
        sha,
        tip,
        ops_parent=_mirror.mirror_root(_rs.slice_root()),
        env=env,
        object_dirs=(Path(common.stdout.strip()) / "objects",),
    )
    if outcome == "raced":
        return free(
            f"origin/{profile.base} moved during the gate: {detail}",
            f"land FAILED: origin/{profile.base} moved while the gate ran "
            "(not the branch); the land re-squashes onto the new tip.",
            RACED_CLASS,
        )
    if outcome != "ok":
        return free(
            f"push of {sha[:9]} to origin/{profile.base} failed: {detail}",
            "land FAILED: the push could not be verified (infrastructure, not "
            "the branch); the land is retried.",
            PUSH_ERROR_CLASS,
        )
    subject = str(handle.get("subject") or "")
    summary = [f"repo_land td{todo_id}: landed {sha[:9]} - {subject}"]
    try:
        wrote = _ls._record_land(ctx.store, todo_id, sha, subject)
        summary.append("todo: " + ", ".join(wrote) + " written")
    except Exception as exc:  # pragma: no cover - the land is real; report it
        log.warning("repo_land: land record on td%d failed", todo_id, exc_info=True)
        summary.append(f"todo: land record FAILED ({exc})")
    cleanup = _cleanup(
        root,
        Path(str(handle.get("slice_tree") or "")),
        land_tree,
        str(handle.get("branch") or ""),
    )
    summary += cleanup
    ctx.set_meta(exit_code=0, land_subject=subject, landed_sha=sha, cleanup=cleanup)
    ctx.append_chunk("job_summary", "\n".join(summary))
    ctx.set_status("succeeded")
    return True


def _poll_no_land(ctx: Any, handle: dict[str, Any], todo_id: int) -> bool:
    """A branch with nothing new: the nothing-new record, done, no push."""
    root = _handle_root(handle)
    summary = [f"repo_land td{todo_id}: {_ls.NO_DIFF_NOTE}"]
    try:
        wrote = _ls._record_land(ctx.store, todo_id, None, "")
        summary.append("todo: " + ", ".join(wrote) + " written")
    except Exception as exc:  # pragma: no cover - reported, the job still ends
        log.warning("repo_land: no-diff record on td%d failed", todo_id, exc_info=True)
        summary.append(f"todo: no-diff record FAILED ({exc})")
    cleanup: list[str] = []
    if root is not None:
        cleanup = _cleanup(
            root,
            Path(str(handle.get("worktree") or "")),
            _land_tree_path(root, todo_id),
            str(((ctx.meta or {}).get("params") or {}).get("branch") or ""),
        )
    ctx.set_meta(exit_code=0, landed_sha=None, cleanup=cleanup)
    ctx.append_chunk("job_summary", "\n".join(summary + cleanup))
    ctx.set_status("succeeded")
    return True


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half with ``land_slice.poll``'s contract: ``False``
    while the gate runs; ``True`` once the job is terminal (succeeded after
    the push and the land record, or failed through ``land_slice._fail``)."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"repo_land: malformed handle {handle!r}")
        return True
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    lane = params.get("lane")
    if handle.get("no_land"):
        ctx.set_meta(compute_handle_released=True)
        return _poll_no_land(ctx, handle, todo_id)
    exit_code = _rs._read_exit(handle)
    root = _handle_root(handle)
    if exit_code is None and _ls._process_alive(_pid(handle.get("pid"))):
        deadline = handle.get("deadline")
        past = (
            isinstance(deadline, (int, float))
            and not isinstance(deadline, bool)
            and _ls._now().timestamp() > float(deadline)
        )
        if not past:
            ctx.set_meta(compute_handle=handle, compute_handle_released=False)
            return False
        _signal_gate(handle)
        if root is not None:
            _drop_land_tree(root, _land_tree_path(root, todo_id))
        # land_slice's wall verdict (DEADLINE_KILL_META): attempt-free, and
        # parked at the ask rather than re-landed on its own.
        ctx.set_meta(compute_handle_released=True, land_again_cap=True)
        _ls._fail(
            ctx,
            todo_id,
            lane,
            f"gate of td{todo_id} passed its wall deadline and was killed",
            handoff="land FAILED: the gate passed its wall-clock deadline "
            "(infrastructure, not the branch).",
            attempt_free=TIMED_OUT_CLASS,
        )
        return True
    ctx.set_meta(compute_handle_released=True)
    profile = _lanes.repo_profile(str(handle.get("repo") or ""))
    if root is None or profile is None or not profile.remote:
        ctx.record_failure(f"repo_land: no root or profile for {handle.get('repo')!r}")
        return True
    if exit_code == 0:
        return _poll_green(ctx, handle, root, profile, todo_id, lane)
    return _poll_red(ctx, handle, root, exit_code, todo_id, lane)


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM the gate's process group (never
    the worker's own) and drop the land tree. The slice tree and branch
    stay: nothing of the slice was pushed."""
    if not isinstance(handle, dict):
        return
    _signal_gate(handle)
    params = (getattr(ctx, "meta", None) or {}).get("params") or {}
    root = _handle_root(handle)
    if root is not None:
        _drop_land_tree(root, _land_tree_path(root, int(params.get("todo_id") or 0)))
