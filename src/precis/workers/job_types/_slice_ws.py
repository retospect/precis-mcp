"""Attempt workspaces: a fresh clone per job, nothing kept only in a folder.

The design call is gr741695 (Reto, 2026-11-20). The rules this module
holds for ``run_slice`` and ``land_slice``:

* **One folder per job.** :func:`cut` makes ``slice-td<todo>-j<job>``
  (a build) or ``slice-td<todo>-land-j<job>`` (a land) and refuses a path
  that exists: no job enters a folder another job made, so nothing is
  ever reset, salvaged or unlocked for a successor.
* **A clone, not a worktree.** The folder is its own repository, cloned
  from the host's hub. Its objects are hard links to the hub's object
  store (``git clone --local``), so it costs no copy, and it shares
  nothing writable: a git process killed in one attempt leaves its lock
  in that attempt's own ``.git`` and blocks no other build. Housekeeping
  on the hub cannot remove an object a clone needs, because a hard link
  keeps the file alive.
* **The hub holds the work.** The hub is a bare repository per host
  checkout (:func:`hub_path`) that nobody works in. Every attempt has one
  ref in it, ``refs/factory/build/<todo>/<job>`` (:func:`build_ref`).
  The builder pushes that ref before it exits; :func:`finalize` checks
  it at the job's end and pushes for a builder that did not (a crash, a
  kill, a lane that cannot push). A folder is deleted (:func:`drop`)
  only after :func:`finalize` says the hub holds its head or it holds
  nothing; otherwise it stays and the caller alerts. Nothing unlanded is
  pushed to GitHub: the lander alone writes there.
* **The hub's hook is the fence.** Its ``pre-receive``
  (:mod:`precis.utils.hub_hook`) takes a push only from a session (its
  own attempt ref) or from the worker (``PRECIS_HUB_WORKER=1``, attempt
  refs only), refuses deletions and anything but commits, and runs the
  repo's secret scan over every push's new commits. A refusal reaches the
  builder that is still running, which fixes it and pushes again. The
  worker writes a branch (the base) or a legacy import into the hub by a
  hub-side fetch, never by push.
* **Nothing is lost with the folder.** :func:`finalize` pushes the
  attempt's line and, as parents of one extra commit, every other tip that
  only the folder holds (branches, stashes, a detached ``HEAD``, an
  in-flight rebase's state, the tip the hub ref held).
* **The next attempt continues from the hub.** :func:`resolve_start`
  names the commit a new attempt starts from: the task's newest pushed
  attempt, else the base tip. The folder of the earlier attempt is never
  consulted.
* **No lock.** Nothing here takes one. A folder has one writer at a
  time because every save and every delete of one runs in the executor's
  watch pass, one after another (``run_slice.tidy``). The hub is shared
  on purpose, and git's own ref and object handling carries pushes,
  fetches and clones that run side by side. Its tidy-up
  (:func:`maintain_hub`) deletes only files an older file already
  replaces, so it needs no quiet moment either.

The builder and the worker run as one account, so the hook guards
against mistakes, not against a session that writes the hub's files
directly. The worker records the commit id it verified
(``meta.build_ref`` on the task) and :func:`resolve_start` refuses a ref
that no longer holds it; the hub keeps a reflog, so a replaced tip can
be found again.

A hub is local to its host. A task's next attempt, its review and its
land run on the host whose hub holds its work (``meta.build_ref.host``).

Everything here runs in the worker, outside any session. Git that needs
the factory credential never runs inside the hub or an attempt's gitdir
(``factory_git_mirror`` holds that machinery); the hub itself is read and
written without a credential.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import precis
from precis.workers import factory_git_mirror as _mirror
from precis.workers import slice_stacks
from precis.workers.job_types import _slice_park

log = logging.getLogger(__name__)

#: ``precis.plans.templates.SLICE_REF_NS``, kept here because that module
#: imports the whole plan engine (a test holds the two equal).
REF_NS = "refs/factory/"
#: The attempt refs' home under :data:`REF_NS`.
BUILD_NS = "build/"

ROLE_BUILD = "build"
ROLE_LAND = "land"

#: :attr:`Pushed.state` values.
PUSHED = "pushed"  # the hub ref holds ``sha``
EMPTY = "empty"  # the folder holds nothing its start commit does not
WITHHELD = "withheld"  # the hub's secret scan refused the push; the folder stays
FAILED = "failed"  # the push or a git step failed; the folder stays

#: The line the hub's hook prints first on any refusal; ``finalize`` and
#: the builder's brief both key on it.
HOOK_REFUSED = "precis-hub: refused"
#: The refusal reason the secret scan uses, after :data:`HOOK_REFUSED`.
HOOK_SECRET = "secret-scan"

#: Session env the hook reads to hold a push to the session's own ref.
ENV_TODO = "PRECIS_SLICE_ID"
ENV_JOB = "PRECIS_SLICE_JOB"
#: Set to ``1`` on the worker's own pushes into the hub; without it and
#: without a session identity the hook refuses the push.
ENV_WORKER = "PRECIS_HUB_WORKER"


class WorkspaceError(RuntimeError):
    """An expected refusal with a one-sentence reason ending in ``next:``."""


class HubUnreadable(WorkspaceError):
    """The hub could not be read. Callers defer: an attempt must never
    start from the base because the task's pushed work could not be seen."""


class RefMoved(WorkspaceError):
    """A recorded attempt ref no longer holds the recorded commit."""


# ── seams and small helpers ────────────────────────────────────────

HUB_NAME = "precis-hub.git"
_MAINTAINED = "precis-maintained"
#: Refs under which :func:`finalize` keeps the commits it makes of state it
#: is about to undo (an in-flight rebase, a detached ``HEAD``'s tree), so a
#: second run, or a look at the folder, still finds them.
KEEP_NS = "refs/factory-keep/"
#: :func:`maintain_hub` runs at most this often. The interval is also what
#: makes it safe without a lock: a run deletes only files the run before it
#: replaced, so anything it deletes has had a full copy for this long.
MAINTAIN_EVERY_S = 24 * 3600
#: The tasks of ``git maintenance run`` that :func:`maintain_hub` asks for.
MAINTAIN_TASKS = ("loose-objects", "incremental-repack")
#: A ``*.cut-*`` temporary, or an ops repository under the hub, this old
#: belongs to a call that died.
CUT_TMP_MAX_AGE_S = 3600
_PUSH_TIMEOUT_S = 120.0
#: The tidy-up runs inside the executor's watch pass; this keeps it well
#: under the time that pass may hold its lock.
_MAINTAIN_TIMEOUT_S = 300.0

_SHA = re.compile(r"[0-9a-f]{40}")
_BUILD_REF = re.compile(r"refs/factory/build/(\d+)/(\d+)")
_ATTEMPT_DIR = re.compile(r"slice-td(\d+)-(land-)?j(\d+)")
_CUT_TMP = re.compile(r"slice-.+\.cut-[0-9a-f-]+")
_DROP_TMP = re.compile(r"slice-.+\.drop-[0-9a-f-]+")
_BASE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def _rs() -> Any:
    """``run_slice``, imported when first needed (it imports this module's
    callers)."""
    return _slice_park._rs()


def _failed(args: list[str], exc: BaseException) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args, 125, "", f"{type(exc).__name__}: {exc}")


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """``run_slice._git`` that also turns a missing ``cwd`` or a timeout into
    a failed result: nothing here may raise on a git step."""
    try:
        return _rs()._git(args, cwd)
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed(args, exc)


def _hub_git(hub: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    return _git(["--git-dir", str(hub), *args], hub.parent)


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


def _err(proc: subprocess.CompletedProcess[str], limit: int = 200) -> str:
    return " ".join((proc.stderr or proc.stdout or "").split())[:limit]


def _must(
    proc: subprocess.CompletedProcess[str],
    what: str,
    next_: str = "check the hub and retry",
) -> str:
    """Stdout of a git step, or :class:`WorkspaceError` naming ``what``."""
    if proc.returncode != 0:
        raise WorkspaceError(f"could not {what} ({_err(proc)}); next: {next_}")
    return (proc.stdout or "").strip()


def _uniq() -> str:
    """A suffix no other call has used, in this process or any other: two
    threads of one worker must not share a temporary name."""
    return f"{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def build_ref(todo_id: int, job_id: int) -> str:
    """``refs/factory/build/<todo>/<job>``: the attempt's ref in the hub."""
    return f"{REF_NS}{BUILD_NS}{todo_id}/{job_id}"


def parse_build_ref(ref: str) -> tuple[int, int] | None:
    """``(todo_id, job_id)`` of a :func:`build_ref` name, else ``None``."""
    m = _BUILD_REF.fullmatch(ref)
    return (int(m.group(1)), int(m.group(2))) if m else None


def legacy_ref(todo_id: int) -> str:
    """``refs/factory/slice/<todo>``: where the engine before gr741695
    pushed a slice on GitHub. Read-only here (:func:`import_legacy`)."""
    return f"{REF_NS}slice/{todo_id}"


def attempt_dir(root: Path, todo_id: int, job_id: int, role: str = ROLE_BUILD) -> Path:
    """The job's folder under ``run_slice.worktrees_dir(root)``:
    ``slice-td<todo>-j<job>`` for a build, ``slice-td<todo>-land-j<job>``
    for a land. The basename names the Compose test project
    (``precis-test-<basename>``), which ``slice_stacks`` finds by its
    ``precis-test-slice-`` prefix."""
    if role == ROLE_BUILD:
        name = f"slice-td{todo_id}-j{job_id}"
    elif role == ROLE_LAND:
        name = f"slice-td{todo_id}-land-j{job_id}"
    else:
        raise ValueError(f"unknown attempt role {role!r}")
    return _rs().worktrees_dir(root) / name


def parse_attempt_dir(path: Path) -> tuple[int, int, str] | None:
    """``(todo_id, job_id, role)`` of a folder :func:`attempt_dir` named,
    else ``None``."""
    m = _ATTEMPT_DIR.fullmatch(path.name)
    if m is None:
        return None
    return int(m.group(1)), int(m.group(3)), ROLE_LAND if m.group(2) else ROLE_BUILD


# ── the hub ────────────────────────────────────────────────────────


def hub_path(root: Path) -> Path:
    """The hub of the host checkout at ``root``: ``precis-hub.git`` in
    ``root``'s git common dir. It sits beside the checkout's object store
    (one filesystem, so clones hard-link), survives ``git clean`` and the
    worktree reapers, and lasts as long as the checkout does."""
    dotgit = root / ".git"
    common = dotgit
    if dotgit.is_file():
        # A linked worktree: ``.git`` names its gitdir, which names the
        # common dir the objects live in.
        try:
            line = dotgit.read_text(encoding="utf-8").strip()
            gitdir = root / line.removeprefix("gitdir:").strip()
            marker = gitdir / "commondir"
            common = (
                (gitdir / marker.read_text(encoding="utf-8").strip()).resolve()
                if marker.is_file()
                else gitdir
            )
        except OSError:
            pass
    elif not dotgit.exists() and (root / "objects").is_dir():
        common = root  # ``root`` is itself a bare repository
    return common / HUB_NAME


def ensure_hub(root: Path, *, notes: list[str] | None = None) -> Path:
    """Create the hub when absent and return its path. Idempotent.

    A new hub is a bare repository seeded from ``root``'s objects by hard
    link, with no refs but the ones :func:`sync_hub` writes. Each call
    also makes its configuration and hook current: git's own automatic
    housekeeping off (``gc.auto=0``, ``receive.autoGc=false``,
    ``maintenance.auto=false``; :func:`maintain_hub` is the only one), reflogs on
    (``core.logAllRefUpdates=always``), ``core.hooksPath`` pinned to the
    hub's own ``hooks`` (a global setting cannot turn the hook off), and
    ``hooks/pre-receive`` a stub that runs :mod:`precis.utils.hub_hook`
    under the worker's own interpreter, isolated from the pusher's
    environment, so the rules and the scan patterns come from the
    deployed code and never from a branch. Raises :class:`WorkspaceError`
    when ``root`` is not a checkout or the hub cannot be made."""
    notes = notes if notes is not None else []
    hub = hub_path(root)
    common = hub.parent
    if not (common / "objects").is_dir():
        raise WorkspaceError(
            f"{root} is not a git checkout (no object store in {common}); "
            "next: point the factory at the repository's main checkout "
            "(PRECIS_SLICE_ROOT)"
        )
    if not (hub / "HEAD").is_file():
        _seed_hub(hub, notes)
    _configure_hub(hub)
    if _install_hook(hub):
        notes.append(f"hub {hub.name}: pre-receive hook installed")
    return hub


def sync_hub(
    root: Path,
    *,
    url: str,
    base: str,
    private: bool = False,
    env: Mapping[str, str] | None = None,
) -> str | None:
    """Move the hub's ``refs/heads/<base>`` to the tip ``url`` holds and
    return that tip, or ``None`` when the remote could not be read or the
    sync could not run (the hub then keeps the tip it had: a stale base
    costs the builder a rebase the lander repeats anyway, never a failed
    launch). Never raises.

    Two syncs may run at once: each fetches for itself and git's own ref
    update orders the two writes, so no lock is taken. The one that loses
    the ref update reads the tip the other wrote. The ref is written by a
    hub-side fetch, never by a push, so the hub's hook never sees the
    worker write a branch. A ``private`` repository is fetched in a fresh
    ops repository under the factory credential (``factory_git_mirror``),
    and the hub then fetches the commit from that ops repository without
    one; there each sync writes the tip it read, so beside another sync
    the base can step back to a tip seconds older until the next sync,
    which costs what any stale base costs."""
    try:
        if not _BASE.fullmatch(base) or ".." in base:
            return None
        hub = ensure_hub(root)
        return _sync_once(hub, url=url, base=base, private=private, env=env)
    except Exception as exc:
        log.warning("sync_hub failed: %s: %s", type(exc).__name__, exc)
        return None


def hub_tip(hub: Path, ref: str) -> str | None:
    """The commit ``ref`` holds in the hub, or ``None``."""
    proc = _hub_git(hub, ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"])
    sha = _out(proc)
    return sha if _SHA.fullmatch(sha) else None


def hub_builds(hub: Path, todo_id: int) -> dict[int, str]:
    """``{job_id: sha}`` for every ``refs/factory/build/<todo>/*`` in the
    hub. Raises :class:`HubUnreadable`."""
    proc = _hub_git(
        hub,
        [
            "for-each-ref",
            "--format=%(objectname) %(refname)",
            f"{REF_NS}{BUILD_NS}{todo_id}/",
        ],
    )
    if proc.returncode != 0:
        raise HubUnreadable(
            f"the hub {hub} could not be read ({_err(proc, 120)}); "
            "next: check that it is a repository, then retry the launch"
        )
    out: dict[int, str] = {}
    for line in (proc.stdout or "").splitlines():
        sha, _, ref = line.partition(" ")
        parsed = parse_build_ref(ref)
        if parsed is not None and parsed[0] == todo_id and _SHA.fullmatch(sha):
            out[parsed[1]] = sha
    return out


def import_legacy(
    hub: Path, url: str, todo_id: int, *, env: Mapping[str, str] | None = None
) -> str | None:
    """Bring a task's pre-gr741695 work across the switch: fetch
    :func:`legacy_ref` from ``url`` (GitHub) into the hub as
    ``refs/factory/build/<todo>/0`` and return its tip. ``None`` means
    GitHub, read successfully, holds no such ref: the task has no legacy
    work and starts from the base. When GitHub holds one and the import
    fails, or GitHub cannot be read, :class:`HubUnreadable` is raised: the
    caller defers, it never starts from the base while work may exist. A
    hub that already has the ref returns it without a fetch. The fetch runs
    in a fresh ops repository under the credential, and the hub fetches
    the commit from there without one, so neither the hub's hook nor a
    credential is involved in writing the hub."""
    ref = build_ref(todo_id, 0)
    have = hub_tip(hub, ref)
    if have:
        return have
    base_env = dict(env) if env is not None else _rs()._git_env()
    return _fetch_via_ops(
        hub, url, src_ref=legacy_ref(todo_id), dst_ref=ref, env=base_env
    )


def maintain_hub(hub: Path) -> str | None:
    """The hub's tidy-up, at most once per :data:`MAINTAIN_EVERY_S`: git's
    incremental maintenance (:data:`MAINTAIN_TASKS`). A run packs what is
    loose and combines small packs into a new one; it deletes only the
    files the run before it replaced that way. So a clone, a fetch or a
    push beside it loses nothing it needs, no lock is taken and nothing
    waits for it. (A clone that copies files in the very second a run
    deletes some can fail on a vanished file; the launch fails without
    costing an attempt and the next one succeeds.)

    Objects nothing refers to are never removed here: ``git gc`` is not
    run on a hub while jobs can start. With the factory stopped the
    operator may run ``git gc --prune=now`` on it. Leftover ops
    repositories of a fetch that died are removed. A note, or ``None``
    when it was not due. Never raises."""
    try:
        marker = hub / _MAINTAINED
        if not (hub / "HEAD").is_file():
            return None
        if marker.exists() and time.time() - marker.stat().st_mtime < MAINTAIN_EVERY_S:
            return None
        # Marked first: a run that fails is retried at the next interval,
        # not on every sweep.
        marker.touch()
        _drop_dead_ops(hub)
        # git's own lock for this command. A run that was killed leaves it
        # behind, and every later run would then skip without a word.
        own = hub / "objects" / "maintenance.lock"
        try:
            held_for: float | None = time.time() - own.stat().st_mtime
        except OSError:
            held_for = None
        if held_for is not None:
            if held_for <= 2 * _MAINTAIN_TIMEOUT_S:
                return "hub maintenance skipped: another run of it is under way"
            own.unlink(missing_ok=True)
        began = time.monotonic()
        # One call per task, in this order: git 2.39 given both tasks in one
        # call fails on a hub whose own store has no pack yet ("no pack files
        # to index"), and a hub with nothing to combine needs no second call.
        for task in MAINTAIN_TASKS:
            if task == "incremental-repack" and not any(
                (hub / "objects" / "pack").glob("*.pack")
            ):
                continue
            proc = _mirror._run(
                [
                    "git",
                    *_mirror.TRUSTED_ARGS,
                    "--git-dir",
                    str(hub),
                    "maintenance",
                    "run",
                    "--quiet",
                    f"--task={task}",
                ],
                cwd=hub.parent,
                env=_rs()._git_env(),
                timeout=_MAINTAIN_TIMEOUT_S,
            )
            if proc.returncode != 0:
                return f"hub maintenance FAILED: {_err(proc)}"
        took = time.monotonic() - began
        return (
            f"hub maintained: packed loose objects, combined small packs ({took:.0f}s)"
        )
    except Exception as exc:
        return f"hub maintenance FAILED: {type(exc).__name__}: {str(exc)[:160]}"


def _drop_dead_ops(hub: Path) -> None:
    """Remove the ops repositories under the hub (a sync's, a push's) whose
    call died before it could: one older than :data:`CUT_TMP_MAX_AGE_S` has
    no owner."""
    try:
        entries = [e for e in (hub / "ops").iterdir() if e.name.startswith("ops-")]
    except OSError:
        return
    for entry in entries:
        try:
            if time.time() - entry.stat().st_mtime > CUT_TMP_MAX_AGE_S:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue


def delete_builds(hub: Path, todo_id: int) -> str:
    """After a land or a start-over: delete every
    ``refs/factory/build/<todo>/*`` in the hub (``update-ref -d``; the
    hook refuses deletion by push). One note; never raises."""
    try:
        builds = hub_builds(hub, todo_id)
        gone = 0
        failed: list[str] = []
        for job_id in sorted(builds):
            proc = _hub_git(hub, ["update-ref", "-d", build_ref(todo_id, job_id)])
            if proc.returncode == 0:
                gone += 1
            else:
                failed.append(f"j{job_id}: {_err(proc, 80)}")
        if not builds:
            return f"no attempt refs of td{todo_id} in the hub"
        note = f"deleted {gone} attempt ref(s) of td{todo_id} from the hub"
        return note + (f"; FAILED for {'; '.join(failed)}" if failed else "")
    except Exception as exc:
        return (
            f"attempt refs of td{todo_id} not deleted: "
            f"{type(exc).__name__}: {str(exc)[:160]}"
        )


# ── an attempt ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class Start:
    """Where an attempt starts: a commit the hub holds."""

    sha: str
    #: The hub ref that holds it; ``None`` for the base tip.
    ref: str | None
    #: The tip is a ``wip:`` snapshot of an attempt that did not finish.
    wip: bool = False


def resolve_start(
    hub: Path,
    todo_id: int,
    *,
    recorded: Mapping[str, Any] | None = None,
    floor: int | None = None,
) -> Start | None:
    """The commit a new attempt of ``todo_id`` continues from, or ``None``
    when the task has no pushed work (the caller starts at the base tip).

    ``floor`` is the task's ``meta.build_floor``: an attempt ref of a job
    with a smaller id is stale (its work landed, or a start-over dropped
    it) and is never a start, even when it was not deleted or sits in
    another host's hub.

    ``recorded`` is the task's ``meta.build_ref`` (``{ref, sha, wip,
    host}``, written by the worker after :func:`finalize`). When it names
    the newest attempt the hub holds, the hub ref must still hold ``sha``:
    :class:`RefMoved` otherwise. An attempt ref of a later job wins over
    the record: that job pushed, and the worker did not get to record it
    (a crash, a push the worker could not add to). Without a record, the
    newest attempt ref by job id. ``wip`` is true when ``recorded`` says so
    or the commit's subject starts ``wip: `` (a builder's own snapshot of
    work it could not finish). Raises :class:`HubUnreadable`."""
    builds = hub_builds(hub, todo_id)
    if floor is not None:
        builds = {job: sha for job, sha in builds.items() if job >= floor}
    rec_ref = recorded.get("ref") if recorded else None
    rec_sha = recorded.get("sha") if recorded else None
    if recorded and isinstance(rec_ref, str) and isinstance(rec_sha, str):
        parsed = parse_build_ref(rec_ref)
        if (
            parsed is not None
            and parsed[0] == todo_id
            and (floor is None or parsed[1] >= floor)
            and not any(job > parsed[1] for job in builds)
        ):
            tip = hub_tip(hub, rec_ref)
            if tip != rec_sha:
                held = tip[:12] if tip else "nothing"
                raise RefMoved(
                    f"{rec_ref} holds {held} but the task recorded {rec_sha[:12]}; "
                    f"next: look at `git --git-dir {hub} reflog {rec_ref}`, then "
                    "clear the task's build_ref or put the ref back"
                )
            # The record's flag says the worker made the snapshot; a builder
            # that could not finish pushes its own ``wip: snapshot`` commit,
            # which only the subject shows. Either makes the start unfinished.
            return Start(
                rec_sha,
                rec_ref,
                bool(recorded.get("wip")) or _is_wip(hub, rec_sha),
            )
    if not builds:
        return None
    job_id = max(builds)
    sha = builds[job_id]
    return Start(sha, build_ref(todo_id, job_id), _is_wip(hub, sha))


def cut(
    hub: Path,
    tree: Path,
    *,
    base: str,
    branch: str,
    start: Start | None = None,
    seed_from: Path | None = None,
    notes: list[str] | None = None,
) -> str:
    """Make ``tree`` a fresh clone of ``hub`` on local branch ``branch``
    and return the commit it checked out.

    The clone's ``origin`` is the hub: ``refs/heads/<base>`` arrives as
    ``origin/<base>``, and ``start.ref`` is fetched when the attempt
    continues an earlier one. The checkout is ``start.sha`` when given,
    else ``origin/<base>``. A fetched ref that does not hold
    ``start.sha`` raises :class:`RefMoved`. The clone's own config gets
    the factory's commit identity and nothing else; hooks stay git's
    defaults.

    Refuses an existing ``tree`` (:class:`WorkspaceError`): a job never
    enters a folder that is already there. Built under a temporary
    sibling name (``<name>.cut-<pid>-<unique>``) and renamed, so a failed
    cut leaves nothing a sweep would take for an attempt. No lock is
    taken: the hub's tidy-up never deletes a file a clone needs
    (:func:`maintain_hub`). ``seed_from`` is the checkout whose ``.venv``
    seeds the clone's (:func:`seed_env`). ``notes`` collects what happened
    for the job's events."""
    notes = notes if notes is not None else []
    if os.path.lexists(tree):
        raise WorkspaceError(
            f"{tree} already exists, and a job never enters a folder that is "
            "already there; next: cut a new attempt (a new job) or drop the old "
            "folder once its work is in the hub"
        )
    if start is not None and start.ref is not None:
        tip = hub_tip(hub, start.ref)
        if tip != start.sha:
            raise RefMoved(
                f"{start.ref} holds {tip[:12] if tip else 'nothing'}, not "
                f"{start.sha[:12]}; next: resolve the start again from the hub"
            )
    tree.parent.mkdir(parents=True, exist_ok=True)
    tmp = tree.with_name(f"{tree.name}.cut-{_uniq()}")
    try:
        sha = _cut_clone(hub, tmp, base=base, branch=branch, start=start)
        try:
            os.rename(tmp, tree)
        except OSError as exc:
            raise WorkspaceError(
                f"could not put {tree.name} in place ({exc}); next: launch again"
            ) from exc
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    where = f"continuing {start.ref}" if start and start.ref else f"origin/{base}"
    notes.append(f"cut {tree.name} from the hub at {sha[:12]} ({where})")
    if seed_from is not None:
        seed_env(seed_from, tree, notes=notes)
    return sha


def seed_env(src_root: Path, tree: Path, *, notes: list[str] | None = None) -> None:
    """Best effort: copy ``src_root``'s ``.venv`` into ``tree`` with
    ``src_root``'s own ``scripts/seed-worktree-venv`` (never the clone's
    copy, which a session may have written). A missing source or a
    failed seed is a note, not an error: the first ``uv run`` then
    builds the environment."""
    notes = notes if notes is not None else []
    script = src_root / "scripts" / "seed-worktree-venv"
    again = "the first uv run builds one"
    if not (src_root / ".venv").is_dir() or not script.is_file():
        notes.append(f"no .venv to seed {tree.name} from in {src_root}; {again}")
        return
    env = _rs()._git_env()
    env["PATH"] = _rs().path_with_user_uv(env.get("PATH"))
    try:
        proc = subprocess.run(
            [str(script), str(tree), str(src_root)],
            cwd=str(tree),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
            timeout=600,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        notes.append(
            f"seeding the .venv of {tree.name} failed "
            f"({type(exc).__name__}: {exc}); {again}"
        )
        return
    if proc.returncode != 0:
        notes.append(f"seeding the .venv of {tree.name} failed ({_err(proc)}); {again}")
    else:
        notes.append(f"seeded the .venv of {tree.name} from {src_root}")


@dataclass(frozen=True)
class Pushed:
    """What :func:`finalize` found and did."""

    state: str
    ref: str
    #: The commit the hub ref holds now (``PUSHED``), else ``None``.
    sha: str | None = None
    #: The tip of the attempt's line (the branch the clone was cut on),
    #: after any in-flight rebase or merge was aborted. ``sha`` is ``head``,
    #: a snapshot commit on top of it, or a commit on top of those that
    #: holds other tips as further parents; ``head`` is an ancestor of it
    #: through first parents.
    head: str | None = None
    #: ``sha`` is a ``wip:`` snapshot the worker made of uncommitted work
    #: or of other tips the folder held.
    wip: bool = False
    #: ``"builder"`` when the hub already held ``sha``, else ``"worker"``.
    by: str = ""
    note: str = ""

    @property
    def safe(self) -> bool:
        """The folder may be deleted: the hub holds its head, or it holds
        nothing."""
        return self.state in (PUSHED, EMPTY)

    def as_meta(self) -> dict[str, Any]:
        """The record for ``meta.build_ref`` on the task and ``meta.push``
        on the job: ``{state, ref, sha, head, wip, by, note, host, at}``."""
        return {
            "state": self.state,
            "ref": self.ref,
            "sha": self.sha,
            "head": self.head,
            "wip": self.wip,
            "by": self.by,
            "note": self.note,
            "host": _slice_park.node_name(),
            "at": _now(),
        }


def finalize(
    tree: Path,
    todo_id: int,
    job_id: int,
    *,
    hub: Path,
    base: str,
    start_sha: str | None,
    reason: str,
    branch: str | None = None,
) -> Pushed:
    """Make sure the hub holds everything ``tree`` holds. Never raises.

    The caller guarantees no process of the session is alive. Nothing
    reachable only inside the folder is lost, and nothing the folder holds
    that cannot be established is guessed: a git step that fails gives
    ``FAILED`` and the folder stays.

    The attempt's *line* is the local branch ``branch`` (the one the clone
    was cut on; ``None``: the branch ``HEAD`` is on, else ``HEAD``). A
    named branch the session moved off and deleted is read as ``None`` when
    no commit is named by a reflog alone; any other missing branch is
    ``FAILED``. In order:

    1. Remove stale locks (the folder has one owner and it is dead). When
       a rebase, merge, cherry-pick, revert, am or bisect is in progress,
       capture its state first (its ``HEAD`` plus the working tree, as a
       snapshot commit in a private index, referenced under
       :data:`KEEP_NS`), then abort it: ``rebase --abort`` discards the
       commits made at a stop and resolved conflicts.
    2. ``head`` is the tip of the line. Uncommitted work is snapshotted
       as a ``wip: snapshot <reason> (td<todo>)`` commit on top of ``HEAD``
       (``_slice_park``'s plumbing snapshot: a private index, the folder's
       own index and branch untouched). It rides on the line when ``HEAD``
       is the line's tip; otherwise it is one more tip to keep. Untracked
       files left out for secret-shaped names are named in the note. A
       repeat finalize of the same state takes the snapshot the earlier one
       made (:func:`_earlier_snapshot`), so it gives the same sha again and
       pushes nothing.
    3. The tips to keep are every local tip not reachable from ``head``,
       the snapshot or any ``refs/remotes/*``: other branches, tags, a
       detached ``HEAD``, every stash entry, the captured in-flight state,
       and the hub ref's current tip when it is not an ancestor of what is
       about to be pushed (an attempt ref never moves to something that
       drops commits it held).
    4. ``EMPTY`` when there is no snapshot, the line's tip is
       ``start_sha`` (or not ahead of ``origin/<base>`` when ``start_sha``
       is ``None``), and nothing is kept.
    5. ``PUSHED`` by the builder when the hub ref already holds the
       commit (the head, or the snapshot).
    6. Otherwise the worker pushes to :func:`build_ref`: the head or the
       snapshot, and, when there are tips to keep, one more commit on
       top with the same tree whose first parent is that commit and
       whose further parents are the kept tips (subject ``wip: snapshot
       <reason> (td<todo>), keeps <k> other tip(s)``; the note names them).
       The push is made from a fresh ops repository that borrows the
       folder's and the hub's objects, never from the folder's own
       gitdir (its config and hooks are session-written), carries
       ``PRECIS_HUB_WORKER=1`` and replaces only the tip it read (a
       lease). The hub's hook scans it like any push: a
       :data:`HOOK_SECRET` refusal is ``WITHHELD``, any other failure
       ``FAILED``. A push is confirmed by reading the ref back.

    ``reason`` is one short phrase for the snapshot subject and the note
    (``"session exited"``, ``"wall timeout"``, ``"killed"``)."""
    ref = build_ref(todo_id, job_id)
    omitted: list[str] = []
    try:
        got = _finalize(
            tree,
            todo_id,
            hub=hub,
            base=base,
            start_sha=start_sha,
            reason=reason,
            ref=ref,
            branch=branch,
            omitted=omitted,
        )
    except Exception as exc:
        log.warning("finalize of %s failed", tree, exc_info=True)
        got = Pushed(
            FAILED,
            ref,
            note=f"finalize failed: {type(exc).__name__}: {str(exc)[:200]}",
        )
    if omitted:
        shown = ", ".join(omitted[:10])
        more = f" (+{len(omitted) - 10} more)" if len(omitted) > 10 else ""
        left = (
            f"left out for their secret-like names, only in the folder: {shown}{more}"
        )
        got = replace(got, note=f"{got.note}; {left}" if got.note else left)
    return got


def push_commit(
    tree: Path, todo_id: int, job_id: int, sha: str, *, hub: Path
) -> Pushed:
    """Put one commit a worker job made itself into the hub as that job's
    attempt ref, for a mechanical job that has no session and no attempt
    folder (``pin_bump`` commits in a tree of its own and hands the commit
    to the lander). ``tree`` is any checkout whose object store holds
    ``sha``. Nothing is snapshotted and nothing aborted: the caller names
    the commit. The push goes through the hub's hook like any other
    (:data:`WITHHELD` on a secret-scan refusal) and is confirmed by reading
    the ref back. Never raises."""
    ref = build_ref(todo_id, job_id)
    try:
        if not _SHA.fullmatch(sha):
            return Pushed(FAILED, ref, note=f"{sha[:40]!r} is not a commit id")
        if not (hub / "HEAD").is_file():
            return Pushed(
                FAILED,
                ref,
                note=f"the hub {hub} is missing; next: ensure_hub, then push again",
            )
        if _git(["cat-file", "-e", f"{sha}^{{commit}}"], tree).returncode != 0:
            return Pushed(FAILED, ref, note=f"{tree} does not hold {sha[:12]}")
        tip = hub_tip(hub, ref)
        if tip == sha:
            return Pushed(
                PUSHED,
                ref,
                sha=sha,
                head=sha,
                by="worker",
                note="the hub already held it",
            )
        return _push_attempt(tree, hub, ref, sha, tip, head=sha, wip=False)
    except Exception as exc:
        log.warning("push of %s from %s failed", sha[:12], tree, exc_info=True)
        return Pushed(
            FAILED, ref, note=f"push failed: {type(exc).__name__}: {str(exc)[:200]}"
        )


def drop(tree: Path, *, notes: list[str] | None = None) -> bool:
    """Delete an attempt folder and bring its Compose test project down
    with its volumes (``slice_stacks``). ``True`` when no attempt folder is
    left under that name.

    The folder is renamed to ``<name>.drop-<unique>`` first and deleted
    under that name. From the rename on it is no attempt folder: a delete
    that stops half-way (a file another account owns, a crash) leaves
    something no sweep takes for an attempt, so it can never be saved again
    as a snapshot in which the deleted files count as removed by the
    builder. The sweep removes what is left.

    Callers pass only a folder whose :class:`Pushed` was
    :attr:`~Pushed.safe`. A folder has one writer at a time: the
    executor's watch pass (the job's own poll, the host sweep), or its own
    job dropping a clone no session ever ran in. Nothing here arbitrates."""
    notes = notes if notes is not None else []
    try:
        project = slice_stacks.project_for_tree(tree)
    except Exception:
        project = None
    try:
        if tree.is_symlink():
            tree.unlink()
            doomed = None
        elif tree.exists():
            doomed = tree.with_name(f"{tree.name}.drop-{_uniq()}")
            os.rename(tree, doomed)
        else:
            doomed = None
    except OSError as exc:
        notes.append(f"could not remove {tree}: {exc}")
        return False
    try:
        if project and not slice_stacks.evict_project(project, volumes=True):
            notes.append(
                f"compose project {project} was not brought down "
                "(none to bring down, or docker unreachable)"
            )
    except Exception as exc:
        notes.append(
            f"compose teardown of {tree.name} failed: "
            f"{type(exc).__name__}: {str(exc)[:120]}"
        )
    if doomed is not None:
        try:
            shutil.rmtree(doomed, onexc=_make_writable_and_retry)
        except OSError as exc:
            notes.append(f"{doomed.name} is left for the sweep: {exc}")
    return True


def sweep(
    root: Path,
    settle: Callable[[Path, int, int, str], Pushed | None],
    *,
    keep: Path | None = None,
    budget_s: float | None = None,
) -> list[str]:
    """The host sweep (``run_slice.tidy``, in the executor's watch pass):
    visit every attempt folder under ``run_slice.worktrees_dir(root)``
    except ``keep``.

    ``settle(tree, todo_id, job_id, role)`` is the caller's judgement and
    action for one folder: ``None`` while its job still runs (or cannot
    be judged), else the :class:`Pushed` of a :func:`finalize` it ran, or
    a stored one. A :attr:`~Pushed.safe` folder is dropped; any other
    stays and yields a note that begins ``"kept"`` and names the folder,
    for the caller's alert. A land folder holds no work of its own:
    ``settle`` returns an ``EMPTY`` record once its job ended. Leftovers
    older than an hour are removed: the ``*.cut-*`` temporary of a failed
    :func:`cut` and the ``*.drop-*`` remains of a :func:`drop` that could
    not finish. Then :func:`maintain_hub` runs.

    ``budget_s`` bounds the time under the watch pass's lock: once it is
    spent no further folder is started (one already started is finished)
    and the hub's tidy-up is left for a sweep that got through. A folder
    that was judged is cheap the next time, so a later sweep reaches the
    rest. Never raises; returns one note per action."""
    notes: list[str] = []
    try:
        folders = _rs().worktrees_dir(root)
        began = time.monotonic()
        entries = sorted(folders.iterdir()) if folders.is_dir() else []
        for seen, entry in enumerate(entries):
            if budget_s is not None and time.monotonic() - began >= budget_s:
                notes.append(
                    f"sweep paused after {time.monotonic() - began:.0f}s with "
                    f"{len(entries) - seen} of {len(entries)} entries not "
                    "looked at; the next sweep goes on"
                )
                return notes
            notes.extend(_sweep_one(entry, settle, keep))
        hub = hub_path(root)
        if (hub / "HEAD").is_file():
            note = maintain_hub(hub)
            if note:
                notes.append(note)
    except Exception as exc:
        log.warning("sweep of %s failed", root, exc_info=True)
        notes.append(f"sweep stopped: {type(exc).__name__}: {str(exc)[:160]}")
    return notes


# ── internals ──────────────────────────────────────────────────────

_HOOK_MODULE = "precis.utils.hub_hook"

_HOOK_TEXT = """#!/bin/sh
# precis attempt hub: the pre-receive fence (gr741695). Written by
# ensure_hub; edits are overwritten. The rules live in {module}, run
# isolated (-I) from the worker's own package directory.
py={py}
code={code}
if [ ! -x "$py" ]; then
  echo "{refused} (hook interpreter missing)" >&2
  echo "  $py is gone; the operator must run ensure_hub again" >&2
  exit 1
fi
out=$("$py" -I -c "$code" pre-receive 2>&1)
rc=$?
if [ "$rc" -eq 0 ]; then
  [ -n "$out" ] && printf '%s\\n' "$out" >&2
  exit 0
fi
if [ "$rc" -ne 3 ]; then
  echo "{refused} (hook failed to run, status $rc)" >&2
fi
[ -n "$out" ] && printf '%s\\n' "$out" >&2
exit 1
"""


def _package_dir() -> str:
    """The directory that holds the worker's own ``precis`` package: ``src``
    for an editable install, ``site-packages`` or the ``PYTHONPATH`` entry
    otherwise. Resolved from the imported package, never from the
    environment of a push."""
    return str(Path(precis.__file__).resolve().parent.parent)


def _hook_code() -> str:
    """The Python the stub runs: ``-I`` ignores ``PYTHON*`` variables, the
    user site and the working directory, and this puts the worker's
    package directory first on ``sys.path``."""
    return (
        f"import sys; sys.path.insert(0, {_package_dir()!r}); "
        f"from {_HOOK_MODULE.rpartition('.')[0]} import "
        f"{_HOOK_MODULE.rpartition('.')[2]} as hook; "
        "sys.exit(hook.main(sys.argv[1:]))"
    )


def _hook_text() -> str:
    return _HOOK_TEXT.format(
        module=_HOOK_MODULE,
        py=shlex.quote(sys.executable),
        code=shlex.quote(_hook_code()),
        refused=HOOK_REFUSED,
    )


def _install_hook(hub: Path) -> bool:
    """Write ``hooks/pre-receive`` when it is missing, stale or not
    executable. ``True`` when it wrote. Raises :class:`WorkspaceError`."""
    hook = hub / "hooks" / "pre-receive"
    text = _hook_text()
    try:
        if (
            hook.is_file()
            and hook.read_text(encoding="utf-8") == text
            and os.access(hook, os.X_OK)
        ):
            return False
        hook.parent.mkdir(parents=True, exist_ok=True)
        tmp = hook.with_name(f".pre-receive.{_uniq()}")
        tmp.write_text(text, encoding="utf-8")
        tmp.chmod(0o755)
        os.replace(tmp, hook)
    except OSError as exc:
        raise WorkspaceError(
            f"could not write the hub's pre-receive hook ({exc}); next: check "
            f"that {hub} is writable"
        ) from exc
    return True


def _link_objects(src: Path, dst: Path) -> None:
    """Hard-link every object file of ``src`` into ``dst`` (a copy where
    the two are on different filesystems). A file that vanishes mid-walk
    (a ``gc`` in ``src``) is skipped: the hub's first sync fetches what it
    lacks."""
    for dirpath, dirnames, files in os.walk(src):
        here = Path(dirpath)
        rel = here.relative_to(src)
        if rel == Path("."):
            dirnames[:] = [d for d in dirnames if d != "info"]
        (dst / rel).mkdir(parents=True, exist_ok=True)
        for name in files:
            if name.startswith("tmp_"):
                continue
            target = dst / rel / name
            if target.exists():
                continue
            try:
                os.link(here / name, target)
            except FileNotFoundError:
                continue
            except OSError:
                try:
                    shutil.copy2(here / name, target)
                except FileNotFoundError:
                    continue
    alternates = src / "info" / "alternates"
    if alternates.is_file():
        (dst / "info").mkdir(parents=True, exist_ok=True)
        shutil.copy2(alternates, dst / "info" / "alternates")


def _seed_hub(hub: Path, notes: list[str]) -> None:
    """Make the hub: an empty bare repository with ``hub.parent``'s objects
    hard-linked in, built beside its final name and renamed into place so a
    second caller never sees half of it."""
    tmp = hub.with_name(f"{hub.name}.cut-{_uniq()}")
    try:
        _must(
            _git(["init", "--bare", "--quiet", "-b", "main", str(tmp)], hub.parent),
            "create the hub",
        )
        _link_objects(hub.parent / "objects", tmp / "objects")
        try:
            os.rename(tmp, hub)
        except OSError:
            if not (hub / "HEAD").is_file():
                raise
    except OSError as exc:
        raise WorkspaceError(
            f"could not create the hub {hub} ({exc}); next: check disk space and "
            "permissions on the checkout's .git"
        ) from exc
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    notes.append(f"hub {hub.name} created beside the checkout's object store")


def _configure_hub(hub: Path) -> None:
    """git's automatic housekeeping off, reflogs on, hooks pinned to the
    hub's own directory. Each key is read first (from the hub's own config
    file) so a call that changes nothing writes nothing, and two jobs never
    fight for git's config lock."""
    wanted = {
        "gc.auto": "0",
        "receive.autoGc": "false",
        "maintenance.auto": "false",
        "core.logAllRefUpdates": "always",
        "core.hooksPath": str(hub / "hooks"),
    }
    config = ["config", "--file", str(hub / "config")]
    for key, value in wanted.items():
        if _out(_hub_git(hub, [*config, "--get", key])) == value:
            continue
        proc = _hub_git(hub, [*config, key, value])
        if proc.returncode != 0:
            time.sleep(0.2)
            _must(_hub_git(hub, [*config, key, value]), f"set {key} on the hub")


def _sync_once(
    hub: Path,
    *,
    url: str,
    base: str,
    private: bool,
    env: Mapping[str, str] | None,
) -> str | None:
    dest = f"refs/heads/{base}"
    before = hub_tip(hub, dest)
    if private:
        try:
            return _fetch_via_ops(
                hub,
                url,
                src_ref=dest,
                dst_ref=dest,
                env=env if env is not None else _rs()._git_env(),
            )
        except HubUnreadable as exc:
            log.warning("sync_hub: %s", exc)
    else:
        proc = _hub_git(
            hub, ["fetch", "--quiet", "--no-tags", "--", url, f"+{dest}:{dest}"]
        )
        if proc.returncode == 0:
            return hub_tip(hub, dest)
    # The fetch failed. When the base moved meanwhile, another sync wrote it
    # (git lets one of two ref updates through): that tip is the answer.
    now = hub_tip(hub, dest)
    return now if now is not None and now != before else None


def _fetch_via_ops(
    hub: Path, url: str, *, src_ref: str, dst_ref: str, env: Mapping[str, str]
) -> str | None:
    """Bring ``src_ref`` of ``url`` into the hub as ``dst_ref`` without
    running a credential inside the hub and without a push (the hub's hook
    guards pushes; the worker writes a branch or an import only by a
    fetch). The credentialed fetch lands in a fresh ops repository that
    borrows the hub's objects; its tip is compared with ``ls-remote``; the
    hub then fetches it from the ops repository, with the credential
    removed from the environment. The commit id, or ``None`` when the
    remote, read successfully, holds no ``src_ref``. Raises
    :class:`HubUnreadable` when the remote cannot be read or a step
    fails."""
    unreadable = f"could not read {src_ref} from {url}"
    remote = _mirror.ls_remote(url, (src_ref,), env=env, namespace="")
    if remote is None:
        raise HubUnreadable(f"{unreadable}; next: retry when GitHub answers")
    sha = remote.get(src_ref)
    if sha is None:
        return None
    local = _mirror.local_env(env)
    ops = _mirror._ops_repo(hub / "ops", hub / "objects", env=local)
    if ops is None:
        raise HubUnreadable(f"{unreadable} (no ops repository); next: retry")
    try:
        got = _mirror._run(
            [
                "git",
                *_mirror.TRUSTED_ARGS,
                "-C",
                str(ops),
                "fetch",
                "--quiet",
                "--no-tags",
                "--",
                url,
                f"+{src_ref}:refs/heads/fetched",
            ],
            cwd=ops,
            env=_mirror.trusted_env(env),
            timeout=_mirror.POPULATE_TIMEOUT_S,
        )
        if got.returncode != 0 or _mirror._tip(ops, "fetched", env=local) != sha:
            raise HubUnreadable(f"{unreadable} ({_err(got)}); next: retry")
        into = _mirror._run(
            [
                "git",
                *_mirror.TRUSTED_ARGS,
                "--git-dir",
                str(hub),
                "fetch",
                "--quiet",
                "--no-tags",
                "--",
                str(ops),
                f"+refs/heads/fetched:{dst_ref}",
            ],
            cwd=hub.parent,
            env=local,
            timeout=_mirror.POPULATE_TIMEOUT_S,
        )
        if into.returncode != 0 or hub_tip(hub, dst_ref) != sha:
            raise HubUnreadable(
                f"could not bring {src_ref} into the hub ({_err(into)}); next: retry"
            )
        return sha
    except (OSError, subprocess.SubprocessError) as exc:
        raise HubUnreadable(
            f"{unreadable} ({type(exc).__name__}); next: retry"
        ) from exc
    finally:
        _mirror._drop_ops(ops)


def _is_wip(hub: Path, sha: str) -> bool:
    """The commit's subject starts ``wip: `` (a snapshot of unfinished work)."""
    subject = _out(_hub_git(hub, ["log", "-1", "--format=%s", sha]))
    return subject.startswith(_slice_park.WIP_ANY)


def _cut_clone(
    hub: Path, tmp: Path, *, base: str, branch: str, start: Start | None
) -> str:
    """Clone ``hub`` into ``tmp``, set the factory's identity, and check out
    ``branch`` at the attempt's start. Returns the commit checked out."""
    _must(
        _git(
            [
                "clone",
                "--local",
                "--no-checkout",
                "--no-tags",
                "--quiet",
                "--",
                str(hub),
                str(tmp),
            ],
            tmp.parent,
        ),
        f"clone the hub into {tmp.name}",
    )
    # The clone copied the hub's files as they stood. The hub's multi-pack
    # index names packs by file, and one written in the same moment as a
    # new pack can name a pack the copy missed, which would hide that pack's
    # objects although the older packs here still hold them. Without the
    # index git reads each pack the clone has by itself.
    (tmp / ".git" / "objects" / "pack" / "multi-pack-index").unlink(missing_ok=True)
    _must(
        _git(["config", "user.name", _slice_park.IDENT_NAME], tmp),
        "set the clone's identity",
    )
    _must(
        _git(["config", "user.email", _slice_park.IDENT_EMAIL], tmp),
        "set the clone's identity",
    )
    # The clone sets up tracking for the hub's HEAD branch; the attempt's
    # config is the identity alone.
    listed = _out(
        _git(["config", "--local", "--name-only", "--get-regexp", r"^branch\."], tmp)
    )
    for section in sorted({k.rsplit(".", 1)[0] for k in listed.splitlines()}):
        _git(["config", "--local", "--remove-section", section], tmp)
    if start is not None and start.ref is not None:
        track = "refs/remotes/origin/" + start.ref.removeprefix("refs/")
        _must(
            _git(
                ["fetch", "--quiet", "--no-tags", "origin", f"+{start.ref}:{track}"],
                tmp,
            ),
            f"fetch {start.ref}",
        )
        held = _out(_git(["rev-parse", "--verify", f"{track}^{{commit}}"], tmp))
        if held != start.sha:
            raise RefMoved(
                f"{start.ref} no longer holds {start.sha[:12]}; "
                "next: resolve the start again from the hub"
            )
    if start is not None:
        rev = start.sha
        if _git(["cat-file", "-e", f"{rev}^{{commit}}"], tmp).returncode != 0:
            raise WorkspaceError(
                f"the hub does not hold {rev[:12]}; next: resolve the start again"
            )
    else:
        rev = f"refs/remotes/origin/{base}"
        probe = _git(["rev-parse", "--verify", "--quiet", f"{rev}^{{commit}}"], tmp)
        if probe.returncode != 0:
            raise WorkspaceError(
                f"the hub holds no refs/heads/{base}; next: sync the hub from "
                "GitHub, then launch again"
            )
    _must(
        _git(["checkout", "--quiet", "--no-track", "-b", branch, rev], tmp),
        f"check out {branch}",
    )
    return _must(_git(["rev-parse", "HEAD"], tmp), "read HEAD")


_LOCKS = ("index.lock", "HEAD.lock", "packed-refs.lock", "config.lock", "shallow.lock")


def _clear_locks(gitdir: Path) -> None:
    """Remove the lock files a killed git left in ``gitdir``. The folder has
    one owner and it is dead."""
    for name in _LOCKS:
        (gitdir / name).unlink(missing_ok=True)
    refs = gitdir / "refs"
    if refs.is_dir():
        for lock in refs.rglob("*.lock"):
            lock.unlink(missing_ok=True)


@dataclass(frozen=True)
class _Tip:
    """A commit only the folder (or the hub ref) holds, and what names it."""

    labels: tuple[str, ...]
    sha: str


def _keep_ref(tree: Path, sha: str, tag: str) -> None:
    """Reference ``sha`` in the folder under :data:`KEEP_NS`, so the commit
    :func:`finalize` made of state it is about to undo survives a ``gc`` and
    a second run finds it. Raises :class:`WorkspaceError`."""
    _must(
        _git(["update-ref", f"{KEEP_NS}{tag}-{_uniq()}", sha], tree),
        f"keep {sha[:12]} in the folder",
        "look at the folder by hand",
    )


def _undo_inflight(
    tree: Path, todo_id: int, reason: str, kept: list[_Tip], omitted: list[str]
) -> str | None:
    """Clear stale locks, then undo whatever a killed session left in
    progress (rebase, am, merge, cherry-pick, revert, bisect) after capturing
    its state into ``kept``. ``None`` when the folder is at rest; else why
    it could not be."""
    gitdir_text = _out(_git(["rev-parse", "--absolute-git-dir"], tree))
    if not gitdir_text:
        return f"{tree} is not a git folder; next: look at it by hand"
    gitdir = Path(gitdir_text)
    # A folder that lost its own .git resolves to the host checkout above
    # it (gr742505): its locks and its in-flight rebase are not ours.
    if os.path.realpath(gitdir) != os.path.realpath(tree / ".git"):
        return (
            f"{tree} has no repository of its own (git resolved {gitdir}); "
            "next: look at it by hand"
        )
    _clear_locks(gitdir)
    steps: list[tuple[str, list[str]]] = []
    if (gitdir / "rebase-merge").exists():
        steps.append(("rebase", ["rebase", "--abort"]))
    if (gitdir / "rebase-apply").exists():
        if (gitdir / "rebase-apply" / "applying").exists():
            steps.append(("am", ["am", "--abort"]))
        else:
            steps.append(("rebase", ["rebase", "--abort"]))
    if (gitdir / "MERGE_HEAD").exists():
        steps.append(("merge", ["merge", "--abort"]))
    if (gitdir / "CHERRY_PICK_HEAD").exists() or (gitdir / "sequencer").exists():
        steps.append(("cherry-pick", ["cherry-pick", "--abort"]))
    if (gitdir / "REVERT_HEAD").exists():
        steps.append(("revert", ["revert", "--abort"]))
    bisecting = (gitdir / "BISECT_START").exists() or (gitdir / "BISECT_LOG").exists()
    if not steps and not bisecting:
        return None
    what = steps[0][0] if steps else "bisect"
    head = _out(_git(["rev-parse", "--verify", "--quiet", "HEAD^{commit}"], tree))
    if _SHA.fullmatch(head):
        # An abort resets to the pre-operation state and drops what the stop
        # held: the commits made there and the resolved conflicts.
        captured = (
            _slice_park._snapshot_commit(
                tree,
                todo_id,
                reason,
                subject=f"wip: snapshot in-flight {what} {_why(reason)} (td{todo_id})",
                parent=head,
                omitted=omitted,
            )
            or head
        )
        _keep_ref(tree, captured, f"inflight-{what}")
        kept.append(_Tip((f"in-flight {what}",), captured))
    for name, args in steps:
        proc = _git(args, tree)
        if proc.returncode != 0:
            return (
                f"could not abort the {name} the session left in {tree.name} "
                f"({_err(proc)}); next: abort it by hand, then finalize again"
            )
    if bisecting:
        proc = _git(["bisect", "reset"], tree)
        if proc.returncode != 0:
            return (
                f"could not end the bisect the session left in {tree.name} "
                f"({_err(proc)}); next: run git bisect reset by hand, then "
                "finalize again"
            )
    return None


def _why(reason: str) -> str:
    return " ".join(str(reason).split())[:60] or "run ended"


def _snapshot_subject(todo_id: int, reason: str) -> str:
    return f"wip: snapshot {_why(reason)} (td{todo_id})"


#: Parents of the hub ref's tip that :func:`_earlier_snapshot` looks at: the
#: worker's own commit on top of a snapshot has the snapshot and the kept
#: tips as parents, never more than a handful.
_EARLIER_PARENTS = 32


def _earlier_snapshot(
    tree: Path, hub: Path, ref: str, snap: str, parent: str
) -> tuple[str, bool] | None:
    """The snapshot an earlier finalize of this folder made of the same
    state: a commit with ``snap``'s tree whose only parent is ``parent``,
    found under the folder's keep refs, at the hub ref's tip or among that
    tip's parents. ``(sha, a keep ref of the folder holds it)``, or ``None``
    when there is none or git cannot tell.

    A finalize does not touch the folder's index, so a second one sees the
    same uncommitted work and makes a commit that differs from the first
    only in its date and maybe its subject. Taking the earlier one instead,
    an unchanged folder gives the sha it gave before and nothing is pushed;
    without that every repeat stacked another commit on the hub ref."""
    want = _out(_git(["rev-parse", "--verify", "--quiet", f"{snap}^{{tree}}"], tree))
    if not want:
        return None

    def match(rows: str) -> str | None:
        for row in rows.splitlines():
            parts = row.split()
            if parts[1:] == [want, parent] and _SHA.fullmatch(parts[0]):
                return parts[0]
        return None

    kept = _git(
        ["for-each-ref", "--format=%(objectname) %(tree) %(parent)", KEEP_NS], tree
    )
    found = match(kept.stdout) if kept.returncode == 0 else None
    if found is not None:
        return found, True
    tip = hub_tip(hub, ref)
    if tip is None:
        return None
    fmt = "--format=%H %T %P"
    at_tip = _hub_git(hub, ["log", "--no-walk", fmt, tip])
    if at_tip.returncode != 0:
        return None
    found = match(at_tip.stdout)
    if found is None:
        parents = at_tip.stdout.split()[2:][:_EARLIER_PARENTS]
        if not parents:
            return None
        behind = _hub_git(hub, ["log", "--no-walk=unsorted", fmt, *parents])
        found = match(behind.stdout) if behind.returncode == 0 else None
    if found is None:
        return None
    # The folder's own git works with it from here on: it must hold it.
    if _git(["cat-file", "-e", f"{found}^{{commit}}"], tree).returncode != 0:
        return None
    return found, False


def _local_tips(
    tree: Path,
    line_ref: str | None,
    head_commit: str,
    on_branch: bool,
    known: set[str],
) -> list[_Tip]:
    """Every local tip that could hold work: branches, tags, earlier keep
    refs, every stash entry, a detached ``HEAD``. Not yet filtered by
    reachability. A keep ref of a commit already in ``known`` adds no
    second name for it."""
    tips: list[_Tip] = []
    listed = _must(
        _git(
            ["for-each-ref", "--format=%(refname)", "refs/heads", "refs/tags", KEEP_NS],
            tree,
        ),
        "list the folder's refs",
        "look at the folder by hand",
    )
    for name in listed.splitlines():
        if not name or name == line_ref:
            continue
        sha = _out(
            _git(["rev-parse", "--verify", "--quiet", f"{name}^{{commit}}"], tree)
        )
        if not _SHA.fullmatch(sha):
            continue  # a tag of a tree or a blob holds no commit
        if name.startswith("refs/heads/"):
            label = f"branch {name.removeprefix('refs/heads/')}"
        elif name.startswith("refs/tags/"):
            label = f"tag {name.removeprefix('refs/tags/')}"
        else:
            if sha in known:
                continue
            label = f"kept ref {name.removeprefix(KEEP_NS)}"
        tips.append(_Tip((label,), sha))
    stashes = _must(
        _git(["stash", "list", "--format=%H"], tree),
        "list the folder's stash entries",
        "look at the folder by hand",
    )
    for sha in stashes.split():
        if _SHA.fullmatch(sha):
            tips.append(_Tip(("stash",), sha))
    if not on_branch:
        tips.append(_Tip(("detached HEAD",), head_commit))
    return tips


def _left_branch(tree: Path, branch: str) -> bool:
    """Whether ``HEAD``'s reflog shows a checkout away from ``branch``: the
    branch was this folder's and the session moved off it before it went."""
    proc = _git(["reflog", "show", "--format=%gs", "HEAD"], tree)
    if proc.returncode != 0:
        return False
    left = f"checkout: moving from {branch} to "
    return any(line.startswith(left) for line in proc.stdout.splitlines())


def _reflog_only(tree: Path) -> bool:
    """Whether the folder holds a commit that no ref and not ``HEAD`` reaches,
    only a reflog entry (a commit left behind by a deleted branch, an amend
    or a dropped stash). ``True`` too when git could not tell."""
    proc = _git(["rev-list", "-n", "1", "--reflog", "--not", "--all"], tree)
    return proc.returncode != 0 or bool(proc.stdout.strip())


def _only_in_folder(
    tree: Path, tips: Sequence[_Tip], top: str, remotes: Sequence[str]
) -> list[_Tip]:
    """The ``tips`` that would be lost with the folder: not reachable from
    ``top`` or any remote-tracking ref. Tips with one commit are merged,
    their names joined."""
    out: dict[str, _Tip] = {}
    for tip in tips:
        if tip.sha == top:
            continue
        if tip.sha in out:
            have = out[tip.sha]
            out[tip.sha] = _Tip((*have.labels, *tip.labels), tip.sha)
            continue
        proc = _git(["rev-list", "-n", "1", tip.sha, "--not", top, *remotes], tree)
        if proc.returncode != 0:
            raise WorkspaceError(
                f"could not tell whether {tip.sha[:12]} is held elsewhere "
                f"({_err(proc)}); next: look at the folder by hand"
            )
        if proc.stdout.strip():
            out[tip.sha] = tip
    return list(out.values())


def _push_env() -> dict[str, str]:
    """The worker's environment for a push into the hub: no session identity,
    and the worker's own marker the hook looks for."""
    env = _rs()._git_env()
    env.pop(ENV_TODO, None)
    env.pop(ENV_JOB, None)
    env[ENV_WORKER] = "1"
    return env


def _borrow_ops(tree: Path, hub: Path) -> Path | None:
    """A fresh ops repository that borrows the folder's and the hub's
    objects (the push and the checks around it never run in the folder's
    own gitdir). ``None`` when it cannot be made; the caller drops it with
    ``_mirror._drop_ops``."""
    found = _out(
        _git(["rev-parse", "--path-format=absolute", "--git-path", "objects"], tree)
    )
    if not found:
        return None
    return _mirror._ops_repo(hub / "ops", Path(found), hub / "objects", env=_push_env())


def _ops_git(
    ops: Path, args: Sequence[str], timeout: float = _PUSH_TIMEOUT_S
) -> subprocess.CompletedProcess[str]:
    """``git <args>`` in the ops repository under the worker's push
    environment; a failure to run is a failed result, never an exception."""
    argv = ["git", "--no-replace-objects", "-C", str(ops), *args]
    try:
        return _mirror._run(
            argv, cwd=ops, env=_mirror.trusted_env(_push_env()), timeout=timeout
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return _failed(argv, exc)


def _is_ancestor(ops: Path, ancestor: str, of: str) -> bool | None:
    """Whether ``ancestor`` is reachable from ``of``; ``None`` when git could
    not tell."""
    rc = _ops_git(ops, ["merge-base", "--is-ancestor", ancestor, of]).returncode
    return {0: True, 1: False}.get(rc)


def _kept_note(tips: Sequence[_Tip]) -> str:
    names = Counter(label for t in tips for label in t.labels)
    shown = ", ".join(n if c == 1 else f"{n} x{c}" for n, c in names.items())
    return f"kept {len(tips)} other tip(s): {shown}"


def _finalize(
    tree: Path,
    todo_id: int,
    *,
    hub: Path,
    base: str,
    start_sha: str | None,
    reason: str,
    ref: str,
    branch: str | None,
    omitted: list[str],
) -> Pushed:
    if not tree.is_dir():
        return Pushed(
            FAILED,
            ref,
            note=f"the folder {tree} is missing; the hub may not hold its work",
        )
    if not (hub / "HEAD").is_file():
        return Pushed(
            FAILED,
            ref,
            note=f"the hub {hub} is missing; next: ensure_hub, then finalize again",
        )
    kept: list[_Tip] = []
    problem = _undo_inflight(tree, todo_id, reason, kept, omitted)
    if problem:
        return Pushed(FAILED, ref, note=problem)
    head_commit = _out(_git(["rev-parse", "--verify", "HEAD^{commit}"], tree))
    if not _SHA.fullmatch(head_commit):
        return Pushed(FAILED, ref, note=f"HEAD of {tree.name} names no commit")
    symbolic = _out(_git(["symbolic-ref", "-q", "HEAD"], tree))
    on_branch = symbolic.startswith("refs/heads/")
    if branch:
        line_ref: str | None = f"refs/heads/{branch}"
        line = _out(
            _git(["rev-parse", "--verify", "--quiet", f"{line_ref}^{{commit}}"], tree)
        )
        if not _SHA.fullmatch(line):
            # The session removed the branch it was cut on (a validate
            # session with nothing to commit that tidied up after itself).
            # When ``HEAD`` is known to have left that branch and no commit
            # is named by a reflog alone, nothing went with the branch, and
            # the line is what ``HEAD`` is on, as for ``branch=None``.
            # Otherwise the folder stays for a human.
            if not _left_branch(tree, branch):
                return Pushed(
                    FAILED,
                    ref,
                    note=f"the folder has no local branch {branch}; next: look "
                    "at the folder by hand",
                )
            if _reflog_only(tree):
                return Pushed(
                    FAILED,
                    ref,
                    note=f"the folder has no local branch {branch} and holds "
                    "commits that only a reflog names; next: look at the "
                    "folder by hand",
                )
            line_ref = symbolic if on_branch else None
            line = head_commit
    else:
        line_ref = symbolic if on_branch else None
        line = head_commit
    snap = _slice_park._snapshot_commit(
        tree,
        todo_id,
        reason,
        subject=_snapshot_subject(todo_id, reason),
        parent=head_commit,
        omitted=omitted,
    )
    snap_has_ref = False
    if snap is not None:
        reused = _earlier_snapshot(tree, hub, ref, snap, head_commit)
        if reused is not None:
            snap, snap_has_ref = reused
    line_snap: str | None = None
    if snap is not None:
        if head_commit == line:
            line_snap = snap
        else:
            where = (
                symbolic.removeprefix("refs/heads/") if on_branch else "detached HEAD"
            )
            if not snap_has_ref:
                _keep_ref(tree, snap, "dirty")
            kept.append(_Tip((f"uncommitted work on {where}",), snap))
    top = line_snap or line
    kept.extend(
        _local_tips(tree, line_ref, head_commit, on_branch, {t.sha for t in kept})
    )
    remotes = _must(
        _git(["for-each-ref", "--format=%(objectname)", "refs/remotes"], tree),
        "list the folder's remote-tracking refs",
        "look at the folder by hand",
    ).split()
    tips = _only_in_folder(tree, kept, top, remotes)
    ops = _borrow_ops(tree, hub)
    if ops is None:
        return Pushed(FAILED, ref, head=line, note="could not make a push repository")
    try:
        hub_ref_tip = hub_tip(hub, ref)
        hub_lost = False
        if hub_ref_tip is not None and hub_ref_tip != top:
            ancestor = _is_ancestor(ops, hub_ref_tip, top)
            if ancestor is None:
                return Pushed(
                    FAILED,
                    ref,
                    head=line,
                    note="could not compare the hub ref's tip with the folder's; "
                    "next: retry",
                )
            hub_lost = not ancestor
        if line_snap is None and not tips and not hub_lost:
            # Nothing of its own: the line is where the attempt started, or
            # it holds nothing the base does not (a session that reset its
            # clone to the base). Pushing the base tip as this attempt would
            # record it as the task's newest work and bury the earlier
            # attempt the session started from.
            counted = _git(
                ["rev-list", "--count", f"refs/remotes/origin/{base}..{line}"], tree
            )
            if _out(counted) == "0":
                return Pushed(
                    EMPTY, ref, head=line, note=f"nothing beyond origin/{base}"
                )
            if start_sha is not None and line == start_sha:
                return Pushed(
                    EMPTY, ref, head=line, note="nothing beyond the start commit"
                )
        keeps = list(tips)
        if hub_lost and hub_ref_tip is not None:
            earlier = "the hub ref's earlier tip"
            if any(t.sha == hub_ref_tip for t in keeps):
                keeps = [
                    _Tip((*t.labels, earlier), t.sha) if t.sha == hub_ref_tip else t
                    for t in keeps
                ]
            else:
                keeps.append(_Tip((earlier,), hub_ref_tip))
        if not keeps and hub_ref_tip == top:
            return Pushed(
                PUSHED,
                ref,
                sha=top,
                head=line,
                wip=line_snap is not None,
                by="builder",
                note="the hub already held it",
            )
        if (
            hub_lost
            and hub_ref_tip is not None
            and _hub_holds_all(ops, hub_ref_tip, top, [t.sha for t in tips])
        ):
            return Pushed(
                PUSHED,
                ref,
                sha=hub_ref_tip,
                head=line,
                wip=True,
                by="builder",
                note="the hub already held it (an earlier finalize)",
            )
        parents = [t.sha for t in keeps]
        subject = (
            f"{_snapshot_subject(todo_id, reason)}, keeps {len(parents)} other tip(s)"
        )
        out = _push_attempt(
            tree,
            hub,
            ref,
            top,
            hub_ref_tip,
            head=line,
            wip=line_snap is not None or bool(parents),
            ops=ops,
            extra_parents=parents,
            subject=subject,
        )
        if keeps:
            out = replace(out, note=f"{out.note}; {_kept_note(keeps)}".lstrip("; "))
        return out
    finally:
        _mirror._drop_ops(ops)


def _hub_holds_all(ops: Path, tip: str, top: str, others: Sequence[str]) -> bool:
    """The hub ref's ``tip`` already holds ``top`` and every commit in
    ``others`` and has ``top``'s tree: what an earlier finalize left, so a
    repeat does not stack another commit on it."""
    for sha in (top, *others):
        if _is_ancestor(ops, sha, tip) is not True:
            return False
    trees = _ops_git(ops, ["rev-parse", f"{tip}^{{tree}}", f"{top}^{{tree}}"])
    names = trees.stdout.split()
    return trees.returncode == 0 and len(names) == 2 and names[0] == names[1]


def _push_attempt(
    tree: Path,
    hub: Path,
    ref: str,
    sha: str,
    tip: str | None,
    *,
    head: str,
    wip: bool,
    ops: Path | None = None,
    extra_parents: Sequence[str] = (),
    subject: str = "",
) -> Pushed:
    """The worker's push of ``sha`` to ``ref`` in the hub, from a fresh ops
    repository that borrows ``tree``'s objects (never from ``tree``'s own
    gitdir: its config and hooks are session-written). With
    ``extra_parents``, ``sha``'s tree is committed again with ``sha`` as
    first parent and these as further parents, and that commit is pushed.
    Leased on the tip read just before. The push carries the worker's
    marker (:data:`ENV_WORKER`) and the hub's hook runs on it like any
    other. ``ops`` is an ops repository the caller owns; without one this
    makes and drops its own."""
    own = ops is None
    if ops is None:
        ops = _borrow_ops(tree, hub)
    if ops is None:
        return Pushed(
            FAILED, ref, head=head, wip=wip, note="could not make a push repository"
        )
    try:
        if extra_parents:
            made = _ops_git(
                ops,
                [
                    "-c",
                    f"user.name={_slice_park.IDENT_NAME}",
                    "-c",
                    f"user.email={_slice_park.IDENT_EMAIL}",
                    "commit-tree",
                    f"{sha}^{{tree}}",
                    "-p",
                    sha,
                    *[a for parent in extra_parents for a in ("-p", parent)],
                    "-m",
                    subject,
                ],
            )
            commit = _out(made)
            if not _SHA.fullmatch(commit):
                return Pushed(
                    FAILED,
                    ref,
                    head=head,
                    wip=wip,
                    note=f"could not commit the kept tips ({_err(made)})",
                )
            sha = commit
        proc = _ops_git(
            ops,
            [
                "push",
                str(hub),
                f"{sha}:{ref}",
                f"--force-with-lease={ref}:{tip or ''}",
            ],
        )
    finally:
        if own:
            _mirror._drop_ops(ops)
    text = (proc.stderr or "") + (proc.stdout or "")
    if proc.returncode == 0:
        if hub_tip(hub, ref) == sha:
            replaced = " (replaced an earlier tip)" if tip else ""
            return Pushed(
                PUSHED,
                ref,
                sha=sha,
                head=head,
                wip=wip,
                by="worker",
                note=f"worker pushed it{replaced}",
            )
        return Pushed(
            FAILED,
            ref,
            head=head,
            wip=wip,
            note="the push reported success but the hub does not hold the commit",
        )
    lines = _hook_lines(text)
    if HOOK_SECRET in " ".join(lines[:1]):
        return Pushed(
            WITHHELD,
            ref,
            head=head,
            wip=wip,
            note="the hub's secret scan refused it, nothing was pushed: "
            + "; ".join(lines)[:600],
        )
    detail = "; ".join(lines or [" ".join(text.split())])[:300]
    return Pushed(
        FAILED, ref, head=head, wip=wip, note=f"push to the hub failed: {detail}"
    )


def _hook_lines(text: str) -> list[str]:
    """The hook's own lines (the marker and what follows it) out of git's
    push output, with git's ``remote: `` prefix removed."""
    lines = [ln.split("remote: ", 1)[-1].rstrip() for ln in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith(HOOK_REFUSED):
            return [
                ln.strip()
                for ln in lines[i:]
                if ln.strip() and not ln.startswith(("To ", " ! ", "error:", "hint:"))
            ]
    return []


def _make_writable_and_retry(
    func: Callable[..., Any], path: str, exc: BaseException
) -> None:
    """``shutil.rmtree`` ``onexc``: a directory that is not writable (a
    read-only object directory) is made so and the failed call retried."""
    parent = os.path.dirname(path)
    os.chmod(parent, 0o755)
    func(path)


def _sweep_one(
    entry: Path,
    settle: Callable[[Path, int, int, str], Pushed | None],
    keep: Path | None,
) -> list[str]:
    """One folder of :func:`sweep`: its notes; never raises."""
    try:
        if keep is not None and entry.resolve() == keep.resolve():
            return []
        cut_tmp = _CUT_TMP.fullmatch(entry.name) is not None
        if cut_tmp or _DROP_TMP.fullmatch(entry.name):
            # Never judged: neither is an attempt folder. A young one may
            # still be in its owner's hands.
            if time.time() - entry.stat().st_mtime < CUT_TMP_MAX_AGE_S:
                return []
            what = "a failed cut" if cut_tmp else "a dropped folder"
            try:
                shutil.rmtree(entry, onexc=_make_writable_and_retry)
            except OSError:
                log.warning("sweep: could not remove %s", entry, exc_info=True)
            if os.path.lexists(entry):
                # The words are the same on every sweep, so the caller's
                # alert is raised once.
                return [
                    f"kept {entry.name}: the leftover of {what} could not be "
                    "removed; it holds no work the hub lacks, remove it by hand"
                ]
            return [f"removed the leftover of {what}: {entry.name}"]
        parsed = parse_attempt_dir(entry)
        if parsed is None or not entry.is_dir():
            return []
        todo_id, job_id, role = parsed
        pushed = settle(entry, todo_id, job_id, role)
        if pushed is None:
            return []
        if not pushed.safe:
            return [f"kept {entry.name}: {pushed.state}: {pushed.note}"[:400]]
        sub: list[str] = []
        if drop(entry, notes=sub):
            return [f"dropped {entry.name} ({pushed.state})"]
        return [f"kept {entry.name}: could not be removed ({'; '.join(sub)[:200]})"]
    except Exception as exc:
        log.warning("sweep of %s failed", entry, exc_info=True)
        return [
            f"kept {entry.name}: sweep failed ({type(exc).__name__}: {str(exc)[:160]})"
        ]
