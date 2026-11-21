"""Snapshots of unfinished work and the park/start-over keys (gr741695).

"If it needs to ask me, put a todo on the thing, park the branch, use the
slot otherwise. Resume after, or start over." (Reto 2026-11-16.) Parked
work never holds a build seat and no timer throws it away. Since the
attempt workspaces (:mod:`._slice_ws`) the work itself is the hub's: the
worker's finalize pushes whatever a run left to the task's attempt ref and
the next attempt starts from it, so the earlier machinery here — the
park-save at a failed end (``meta.saved``), the resume of a saved branch
into a reused tree, ``drop_saved`` — is gone. What stays:

* :func:`_snapshot_commit` — a commit of the whole working tree (tracked,
  modified and untracked, gitignore honoured, secret-named untracked files
  left out) on top of ``HEAD``, built with git plumbing in a private index
  so the folder's own index and branch never move. ``_slice_ws.finalize``
  pushes it as a ``wip: snapshot <why> (td<N>)`` commit.
* :func:`is_wip` and :data:`WIP_ANY` — any commit whose subject starts
  ``wip: `` is unfinished work: it is continued, never landed or reviewed.
* :data:`PARKED_KEY`, :data:`START_OVER_KEY`, :data:`BODY_MARK` — the node
  meta and the ask-body marker ``plan_dispatch`` writes and ``run_slice``
  reads (``start over`` in a closing answer deletes the task's attempts and
  the next launch starts at the base).
* :func:`node_name`, :data:`IDENT_NAME` and :data:`IDENT_EMAIL` — the host
  name a record carries and the factory's commit identity.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from precis.corpus_layout import host_name
from precis.workers import factory_git_mirror as _factory_git_mirror

log = logging.getLogger(__name__)

#: ``meta`` key on the build NODE while it is parked (``_step_park_stamp``).
PARKED_KEY = "parked"
#: ``meta`` key on the build node: the answer said ``start over`` — the next
#: launch deletes the task's attempts from the hub and cuts from
#: ``origin/<base>``. Holds the ask id; ``None`` once consumed.
START_OVER_KEY = "start_over"
#: Subject prefix of the snapshot commit; ``wip: parked <why> (td<N>)``.
WIP_PREFIX = "wip: parked "
#: Any commit whose subject starts so is a snapshot of unfinished work, the
#: parked one above or the attempt workspaces' ``wip: snapshot <why> (td<N>)``.
WIP_ANY = "wip: "
#: The marker the ask body carries once host/worktree/head are written in.
BODY_MARK = "Parked work:"

_HEX40 = re.compile(r"[0-9a-f]{40}")
#: The factory's commit identity: ``-c`` pairs for plumbing commits, the two
#: values for a clone's own config.
IDENT_NAME = "precis-factory"
IDENT_EMAIL = "factory@precis.invalid"
_IDENT = ("-c", f"user.name={IDENT_NAME}", "-c", f"user.email={IDENT_EMAIL}")


def _rs() -> Any:
    from precis.workers.job_types import run_slice

    return run_slice


def node_name() -> str:
    """The build host's name as ``meta.params.target_node`` spells it:
    ``PRECIS_NODE``, else this host's stable name."""
    return os.environ.get("PRECIS_NODE") or host_name()


def wip_subject(todo_id: int, reason: str) -> str:
    why = " ".join(str(reason).split())[:60] or "run ended"
    return f"{WIP_PREFIX}{why} (td{todo_id})"


def _out(proc: subprocess.CompletedProcess[str]) -> str:
    return (proc.stdout or "").strip() if proc.returncode == 0 else ""


#: Basenames of untracked files that never ride a snapshot: ``.env*``,
#: ``*.pem``, ``*.key``, ``id_*``, anything naming a token or credentials.
_SECRET_NAME = re.compile(r"^\.env|\.pem$|\.key$|^id_|token|credentials", re.IGNORECASE)


def _out_raw(proc: subprocess.CompletedProcess[str]) -> str:
    return proc.stdout or "" if proc.returncode == 0 else ""


def _snapshot_commit(
    tree: Path,
    todo_id: int,
    reason: str,
    *,
    subject: str | None = None,
    parent: str | None = None,
    omitted: list[str] | None = None,
) -> str | None:
    """A commit of the whole working tree (tracked, modified and untracked,
    gitignore honoured) on top of ``HEAD``, or ``None`` when the tree holds
    nothing HEAD does not. Built in a private index file so the worktree's
    own index and branch never move. ``subject`` replaces the default
    ``wip: parked <why> (td<N>)`` subject; ``parent`` replaces ``HEAD`` as
    the commit the snapshot is built on (and compared with). The names of
    untracked files left out for their secret-shaped names are appended to
    ``omitted``, whether or not a snapshot results."""
    rs = _rs()
    head = parent or _out(rs._git(["rev-parse", "HEAD"], tree))
    if not _HEX40.fullmatch(head):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="slice-park-"))
    try:
        env = {**rs._git_env(), "GIT_INDEX_FILE": str(tmp / "index")}
        ceiling = rs._attempt_ceiling(tree)
        if ceiling is not None:
            # gr742505: an attempt folder that lost its ``.git`` must be
            # "not a git repository" to this plumbing too — never the host
            # checkout above it.
            env["GIT_CEILING_DIRECTORIES"] = ceiling

        def git(args: list[str], stdin: str | None = None) -> str:
            proc = subprocess.run(
                ["git", *_factory_git_mirror.TRUSTED_ARGS, *_IDENT, *args],
                cwd=str(tree),
                env=env,
                input=stdin,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=False,
                timeout=300,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git {args[0]} failed: {(proc.stderr or '').strip()[:200]}"
                )
            return (proc.stdout or "").strip()

        git(["read-tree", head])
        git(["add", "-A"])
        # Scrub: an untracked file with a secret-shaped name never rides
        # the snapshot (the repo is public and the ref fetchable).
        untracked = _out_raw(
            rs._git(["ls-files", "--others", "--exclude-standard", "-z"], tree)
        )
        named = [
            f for f in untracked.split("\0") if f and _SECRET_NAME.search(Path(f).name)
        ]
        if named:
            git(["rm", "--cached", "-q", "--ignore-unmatch", "--", *named])
            if omitted is not None:
                omitted.extend(n for n in named if n not in omitted)
        new_tree = git(["write-tree"])
        if new_tree == git(["rev-parse", f"{head}^{{tree}}"]):
            return None
        sha = git(
            [
                "commit-tree",
                new_tree,
                "-p",
                head,
                "-m",
                subject or wip_subject(todo_id, reason),
            ]
        )
        return sha if _HEX40.fullmatch(sha) else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def is_wip(root: Path, sha: str) -> bool:
    """``sha`` is a snapshot of unfinished work: its subject starts ``wip: ``."""
    subject = _out(_rs()._git(["log", "-1", "--format=%s", sha], root))
    return subject.startswith(WIP_ANY)
