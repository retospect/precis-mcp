"""Park = save, resume anywhere (Reto 2026-11-16).

"If it needs to ask me, put a todo on the thing, park the branch, use the
slot otherwise. Resume after, or start over." Parked work never holds a
build seat and no timer throws it away; the branch moves between hosts.

Where each half lives, because parking is decided on the tick (the control host)
but the worktree is on the build host:

* **Save** — :func:`save_work`, called at the end of a failed
  ``run_slice`` / ``land_slice`` job on the host that ran it, before the
  tick sees the failure. It snapshots the working tree as a ``wip:
  parked <why> (td<N>)`` commit built with git plumbing (a private index:
  the worktree, its index and the local branch are untouched, so an
  ordinary retry behaves exactly as before — it still drops the dead
  attempt's uncommitted edits, gr571311) and pushes that commit to
  ``refs/factory/slice/<td>``. A clean tree pushes the branch tip. The
  result rides the job as ``meta.saved = {host, worktree, head, reason,
  at, wip}``; a failed save is logged on the job and the node and never
  blocks the park.
* **Stamp** — the tick's ``_step_park_stamp`` (``plan_dispatch``) copies
  the newest job's ``meta.saved`` to the build node as ``meta.parked`` the
  moment the node parks (an open ask, ``rescue:*``, ``child-failed:*``,
  ``STATUS:paused``) and writes host, worktree and head into every open
  ask's body. One place, whichever path parked it.
* **Resume** — :func:`resume_tip` / :func:`apply_saved_wip`, called from
  ``run_slice._ensure_worktree``: a re-armed parked node (``meta.parked``)
  resumes from the saved tip, WIP commit included, on any host; an
  ordinary retry resumes from the last real commit on the pushed branch.
  ``start over`` in the closing answer (``plan_dispatch._step_rearm``)
  stamps :data:`START_OVER_KEY`; the next launch calls :func:`drop_saved`
  and cuts from ``origin/<base>``.

Disk: nothing here reaps. ``meta.parked`` carries ``host`` and
``worktree`` — a later reaper keys on those (see ``land_slice._cleanup``,
which already removes a landed slice's tree, branch and remote ref).
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.corpus_layout import host_name
from precis.workers import factory_git_mirror as _factory_git_mirror

log = logging.getLogger(__name__)

#: ``meta`` key on the JOB that saved its worktree at its failed end.
SAVED_KEY = "saved"
#: ``meta`` key on the build NODE while it is parked (``_step_park_stamp``).
PARKED_KEY = "parked"
#: ``meta`` key on the build node: the answer said ``start over`` — the next
#: launch drops the saved branch and cuts from ``origin/<base>``. Holds the
#: ask id; ``None`` once consumed.
START_OVER_KEY = "start_over"
#: Subject prefix of the snapshot commit; ``wip: parked <why> (td<N>)``.
WIP_PREFIX = "wip: parked "
#: The marker the ask body carries once host/worktree/head are written in.
BODY_MARK = "Parked work:"

_HEX40 = re.compile(r"[0-9a-f]{40}")
_SLICE_NS = "refs/factory/"
_IDENT = ("-c", "user.name=precis-factory", "-c", "user.email=factory@precis.invalid")


def _rs() -> Any:
    from precis.workers.job_types import run_slice

    return run_slice


def node_name() -> str:
    """The build host's name as ``meta.params.target_node`` spells it:
    ``PRECIS_NODE``, else this host's stable name."""
    return os.environ.get("PRECIS_NODE") or host_name()


def saved_ref(todo_id: int) -> str:
    """The hidden origin ref a parked slice lives under."""
    return f"{_SLICE_NS}{_rs().slice_branch(todo_id)}"


def tracking_ref(todo_id: int) -> str:
    """Where :func:`fetch_saved` lands it in the local clone."""
    return f"refs/remotes/origin/factory/{_rs().slice_branch(todo_id)}"


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


def secret_hits(tree: Path, base: str, sha: str) -> int:
    """Credential-shaped findings across every commit published over ``base`` —
    the repo's own scan (:func:`precis.utils.secret_scan.find_secrets`, the
    single pattern source ``scripts/ship``'s secret scan reads). An
    unreadable history counts as a hit: nothing unscanned is pushed.
    Per-parent patches include merged history and credentials deleted before
    the final snapshot; a net diff would miss those public objects."""
    from precis.utils.secret_scan import find_secrets

    diff = _rs()._git(
        [
            "log",
            "--full-history",
            "--root",
            "-m",
            "-p",
            "--text",
            "--no-ext-diff",
            "--no-textconv",
            "--no-color",
            "-U0",
            "--format=%B",
            f"{base}..{sha}",
        ],
        tree,
    )
    if diff.returncode != 0:
        return 1
    return len(find_secrets(diff.stdout or ""))


def _snapshot_commit(tree: Path, todo_id: int, reason: str) -> str | None:
    """A commit of the whole working tree (tracked, modified and untracked,
    gitignore honoured) on top of ``HEAD``, or ``None`` when the tree holds
    nothing HEAD does not. Built in a private index file so the worktree's
    own index and branch never move."""
    rs = _rs()
    head = _out(rs._git(["rev-parse", "HEAD"], tree))
    if not _HEX40.fullmatch(head):
        return None
    tmp = Path(tempfile.mkdtemp(prefix="slice-park-"))
    try:
        env = {**rs._git_env(), "GIT_INDEX_FILE": str(tmp / "index")}

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
        new_tree = git(["write-tree"])
        if new_tree == git(["rev-parse", f"{head}^{{tree}}"]):
            return None
        sha = git(
            ["commit-tree", new_tree, "-p", head, "-m", wip_subject(todo_id, reason)]
        )
        return sha if _HEX40.fullmatch(sha) else None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _push(tree: Path, todo_id: int, sha: str, expected_tip: str) -> str:
    """Push ``sha`` to ``refs/factory/slice/<td>`` — a seam for tests."""
    from precis.workers.job_types import land_slice as _ls

    return _ls._push_branch(
        tree, _rs().slice_branch(todo_id), sha, expected_tip=expected_tip
    )


def _save_lease(tree: Path, todo_id: int) -> str | None:
    """Read the saved ref, distinguishing verified absence from failure."""
    try:
        tip = fetch_saved(tree, todo_id)
        if tip is not None:
            return tip
        probe = _rs()._git(
            ["ls-remote", "--exit-code", "origin", saved_ref(todo_id)], tree
        )
        return "" if probe.returncode == 2 else None
    except (OSError, subprocess.SubprocessError):
        return None


def save_work(
    tree: Path,
    todo_id: int,
    reason: str,
    *,
    base: str = "origin/main",
    include_dirty: bool = True,
    local_only: bool = False,
) -> dict[str, Any] | None:
    """Save the work in ``tree`` so the node may park without losing it.

    Returns the ``meta.saved`` record ``{host, worktree, head, reason,
    at, wip, push}`` — ``push`` is :func:`land_slice._push_branch`'s note —
    or ``None`` when there is nothing to save (a clean tree whose branch
    is not ahead of ``base``). Never raises past a failed push: the
    record then carries ``error``.

    The push is withheld — kept local, never an error — when origin's
    ref already holds a tip ``sha`` does not descend from (gr709688:
    a stale tree's save must not clobber a round another host pushed;
    the remote tip's WIP snapshot counts at its parent, so a same-host
    re-save still replaces its own earlier snapshot). An unverifiable ref
    or a secret-scan hit also keeps the save local. ``local_only`` keeps
    the snapshot on this host after a pre-land defer refusal.

    ``include_dirty`` False (a failed land: its tree may sit mid-rebase)
    pushes the branch tip only."""
    rs = _rs()
    if not tree.is_dir():
        return None
    head = _out(rs._git(["rev-parse", "HEAD"], tree))
    if not _HEX40.fullmatch(head):
        return None
    wip = _snapshot_commit(tree, todo_id, reason) if include_dirty else None
    ahead = _out(rs._git(["rev-list", "--count", f"{base}..HEAD"], tree))
    if wip is None and (not ahead.isdigit() or int(ahead) == 0):
        return None
    sha = wip or head
    expected_tip = None if local_only else _save_lease(tree, todo_id)
    remote_tip = without_wip(tree, expected_tip) if expected_tip else expected_tip
    if local_only:
        note = "withheld: pre-land defer refused"
    elif expected_tip is None or remote_tip is None:
        note = "withheld: cannot verify origin's saved tip"
    elif (
        remote_tip
        and remote_tip != sha
        and rs._git(["merge-base", "--is-ancestor", remote_tip, sha], tree).returncode
        != 0
    ):
        # gr709688: origin's pushed ref holds a newer tip (a round on
        # another host, or this one's already-pushed defer) — the save
        # must not clobber it with a tip that does not descend from it.
        # The snapshot stays local; a same-host resume still finds it.
        note = f"withheld: behind origin's pushed tip {remote_tip[:12]}"
    else:
        hits = secret_hits(tree, base, sha)
        # A hit keeps the snapshot local: the repo is public and
        # refs/factory/* fetchable. Same-host resume still works from
        # the local object.
        note = (
            f"withheld: secret-scan {hits} hits"
            if hits
            else _push(tree, todo_id, sha, expected_tip)
        )
    record: dict[str, Any] = {
        "host": node_name(),
        "worktree": str(tree),
        "head": sha,
        "reason": " ".join(str(reason).split())[:200],
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "wip": wip is not None,
        "push": note,
    }
    if "FAILED" in note or "skipped" in note:
        record["error"] = note
    return record


def save_at_job_end(
    ctx: Any,
    tree: Path,
    todo_id: int,
    reason: str,
    *,
    base: str = "origin/main",
    include_dirty: bool = True,
    local_only: bool = False,
) -> None:
    """:func:`save_work` for the end of a failed run: stamps ``meta.saved``
    on the job and logs what happened. A failed save is written on the job
    and the node and never blocks the park or masks the failure."""
    error: str | None
    try:
        record = save_work(
            tree,
            todo_id,
            reason,
            base=base,
            include_dirty=include_dirty,
            local_only=local_only,
        )
    except Exception as exc:
        log.warning("park-save: td%d failed", todo_id, exc_info=True)
        record = None
        error = f"{type(exc).__name__}: {exc}"
    else:
        error = None if record is None else record.get("error")
    try:
        if record is not None:
            ctx.set_meta(**{SAVED_KEY: record})
            withheld = str(record["push"]).startswith("withheld")
            ctx.append_chunk(
                "job_event",
                f"park-save{' WITHHELD (kept local only)' if withheld else ''}: "
                f"{record['head'][:12]} "
                f"({'wip snapshot' if record['wip'] else 'branch tip'}) "
                f"on {record['host']}:{record['worktree']}; {record['push']}",
            )
        if error:
            ctx.append_chunk("job_event", f"park-save FAILED: {error}")
            _rs()._append_comment(
                ctx.store,
                todo_id,
                f"park-save FAILED ({error}); the work is only in the worktree "
                f"{tree} on this host",
            )
    except Exception:  # pragma: no cover — the failure still lands
        log.warning("park-save: record for td%d failed", todo_id, exc_info=True)


# ── resume ─────────────────────────────────────────────────────────


def fetch_saved(root: Path, todo_id: int) -> str | None:
    """Fetch ``refs/factory/slice/<td>`` into the clone's tracking ref and
    return its tip, or ``None`` when origin holds none (or cannot be read)."""
    rs = _rs()
    src, dst = saved_ref(todo_id), tracking_ref(todo_id)
    got = rs._git(["fetch", "origin", f"+{src}:{dst}"], root)
    if got.returncode != 0:
        return None
    tip = _out(rs._git(["rev-parse", "--verify", "--quiet", f"{dst}^{{commit}}"], root))
    return tip if _HEX40.fullmatch(tip) else None


def without_wip(root: Path, tip: str) -> str | None:
    """Return the real tip under a park snapshot, or None if unreadable."""
    try:
        commit = _rs()._git(["show", "-s", "--format=%P%n%s", tip], root)
    except (OSError, subprocess.SubprocessError):
        return None
    if commit.returncode:
        return None
    parent, _, subject = commit.stdout.partition("\n")
    if subject.startswith(WIP_PREFIX):
        return parent if _HEX40.fullmatch(parent) else None
    return tip


def resume_tip(root: Path, todo_id: int, *, keep_wip: bool) -> str | None:
    """The commit a launch with no local tree cuts from: the saved tip
    when origin holds one. An ordinary retry (``keep_wip`` False) drops a
    WIP snapshot on top and takes the last real commit; a parked node's
    resume keeps it."""
    tip = fetch_saved(root, todo_id)
    return without_wip(root, tip) if tip is not None and not keep_wip else tip


def apply_saved_wip(
    root: Path, tree: Path, todo_id: int, local_head: str | None = None
) -> str | None:
    """A kept local tree of a parked node: move its branch onto the saved
    tip (the WIP snapshot) when that tip descends from where the branch
    is. ``local_head`` is ``meta.parked.head`` — the snapshot object lives
    in this clone's object store when the same host saved it, so a failed
    push (or a withheld secret-scan hit) loses nothing. Returns the sha moved to, else ``None``."""
    rs = _rs()
    tip = None
    if local_head and _HEX40.fullmatch(local_head):
        have = rs._git(["cat-file", "-e", f"{local_head}^{{commit}}"], tree)
        tip = local_head if have.returncode == 0 else None
    if tip is None:
        tip = fetch_saved(root, todo_id)
    if tip is None:
        return None
    now = _out(rs._git(["rev-parse", "HEAD"], tree))
    if now == tip:
        return None
    if rs._git(["merge-base", "--is-ancestor", "HEAD", tip], tree).returncode != 0:
        return None  # the local branch has moved on its own: keep it
    moved = rs._git(["reset", "--hard", tip], tree)
    return tip if moved.returncode == 0 else None


def drop_saved(root: Path, todo_id: int) -> list[str]:
    """``start over``: remove the slice's worktree and local branch and
    the tracking copy of the saved ref, and delete the ref on origin
    (best effort). Notes; never raises on a missing piece."""
    rs = _rs()
    notes: list[str] = []
    tree = rs.worktrees_dir(root) / rs.slice_name(todo_id)
    branch = rs.slice_branch(todo_id)
    if tree.exists():
        gone = rs._git(["worktree", "remove", "--force", str(tree)], root)
        if gone.returncode != 0:
            shutil.rmtree(tree, ignore_errors=True)
        rs._git(["worktree", "prune"], root)
        notes.append(f"worktree {tree} dropped")
    if rs._branch_exists(root, branch):
        rs._git(["branch", "-D", branch], root)
        notes.append(f"branch {branch} dropped")
    rs._git(["update-ref", "-d", tracking_ref(todo_id)], root)
    from precis.workers.job_types import land_slice as _ls

    notes.extend(_ls._delete_remote_branch(root, branch))
    return notes
