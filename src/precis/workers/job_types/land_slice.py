"""``land_slice`` — the lander: land one finished slice branch (dark factory).

Spec: ``docs/backlog/dark-factory.md`` § "Branches: who merges what, how"
(the builder commits on its branch only; the lander lands it with
``scripts/ship`` and deletes the branch and its worktree). Mechanical: no
LLM. :mod:`precis.workers.job_types.run_slice` mints one of these (idem key
``land_slice:td<id>``, a child of the slice todo, pinned to the host that
holds the worktree) once a build session exited clean and its branch passed
the deny-path check.

Detached submit/poll pair under ``ssh_node``, like ``run_slice``:

* :func:`submit` reads the squash subject from the branch
  (:func:`squash_subject`: the slice's commit subject, its trailing handle
  list made to carry ``(td<id>)`` so ``scripts/lib/land_notify.py`` moves
  the todo, capped at 72 bytes) and launches ``scripts/ship --quick
  "<subject>"`` detached in the worktree (``PRECIS_SHIP_BIN`` overrides the
  path; ``PRECIS_SHIP_VERBOSE=1`` so the log holds the whole transcript).
* :func:`poll` waits for ship's exit. Green: the landed sha (ship's
  ``shipped — landed on main:`` line, else ``git rev-parse origin/main``)
  goes on the job (``meta.landed_sha``); a ``landed: <sha>`` comment and
  ``STATUS:done`` go on the todo only when land_notify did not already
  write them (its comment is the todo's newest); the sha is written into
  the plan's pending ``sha-in-prod`` gate (:func:`pending_gate`); then
  ``git worktree remove``, ``git branch -D slice/<id>`` and, when the
  branch is on origin, ``git push origin --delete slice/<id>``. A cleanup
  step that fails is reported in the summary, not a job failure: the land
  is real. Red (lint ✖, a forward-merge conflict, anything non-zero): the
  job fails with the ✖ line, a ``handoff:`` comment tells the next build
  session why, and the todo reopens with ``attempt:<n>`` (run_slice's
  :func:`~precis.workers.job_types.run_slice.release_todo`). The branch and
  worktree stay for that session. A ✖ about *main* (:func:`is_main_refusal`:
  the drift guard's two refusals) is a deferral, not a red: poll counts it
  (``handle.main_refusals``), parks the handle for
  :func:`refusal_backoff_s` (``handle.not_before``, default 30 min), then
  relaunches ship; the todo is untouched. Each step renews ``meta.deadline``
  (park: backoff + wall; relaunch: wall), so a parked land is not killed at
  the 1 h wall while a running ship still is; past
  :data:`MAX_MAIN_REFUSALS` the job fails with a "land BLOCKED" handoff.
  Before every launch the lander runs the drift helper itself under its own
  identity (:func:`_drift_preflight`); when that cannot say how old main's
  verdict is (a blind token, GitHub unanswerable) the land parks the same
  way instead of riding ship's "unknown never refuses" (fail-closed,
  gr550941). Every deferral event ends with the helper's ``--diag`` line.
* :func:`kill` SIGTERMs ship's process group.

**Pull requests** (td543749). With ``params.pr = <n>`` (a ``pr-intake``
plan's land node) the branch is ``slice/pr-<n>`` and the worktree is
``params.worktree`` when ``dep_check`` pinned one (it must exist: it holds
the relock commit), else ``slice-pr-<n>``, cut here from ``git fetch origin
+pull/<n>/head`` and refused when the head is not the sha the intake
classified (``meta.pr.head_sha`` on the plan root). The subject is the PR
title made lint-clean (:func:`precis.plans.pr_class.land_subject`) plus
``(td<id>)``, and ``params.checked`` (the template sets it) ships with
``--checked``: a pyproject diff selects the contract and import-map tests.
After a green land — of a PR node, or of any slice whose todo carries
``meta.pr_close = <n>`` (the "land after fixes" slice) — the PR is closed
with a comment naming the squash sha (:func:`_gh`, the host's ``gh auth``:
the attribution gap of gr543694 applies). A failed close is reported in
the summary, not a job failure.

Guard: ``PRECIS_FACTORY=off`` refuses every submit. Git, launch and
process probes go through module-level seams (:func:`_git`,
:func:`_launch`, :func:`_process_alive`, :func:`_pending_gates`) so tests
never run ship, git or SQL.
"""

from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans.logbook import log_plan_event
from precis.plans.templates import PENDING_SHA
from precis.store.types import Tag
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import JobTypeSpec
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "land_slice"

#: ``scripts/ship``'s subject cap, in bytes.
SUBJECT_CAP = 72
_DEFAULT_WALL_S = 3600
_TAIL_CAP = 2000

#: ``scripts/lib/land_notify.py``'s trailing handle list.
_HANDLE = r"(?:gr|td)\d+"
_HANDLES_RE = re.compile(rf"\(({_HANDLE}(?:[,\s]+{_HANDLE})*)\)\s*$")
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_STATUS_RE = re.compile(r"SHIP_STATUS:\s*(\S+)")
_LANDED_MARK = "shipped — landed on main:"

#: Substrings of ship's own refusal lines (``die`` in ``scripts/ship``'s
#: qland drift guard) that are about MAIN's state, not the branch's: the
#: land is deferred and ship relaunched, not the slice reopened.
MAIN_REFUSAL_MARKS: tuple[str, ...] = (
    "main has no all-green shard matrix",
    "main's last all-green shard matrix is",
)
#: Deferrals tolerated per land; the next main refusal fails the job with a
#: "land BLOCKED" handoff (nothing wrong on the branch).
MAX_MAIN_REFUSALS = 8
#: Seconds a deferred land waits before ship runs again
#: (``PRECIS_LAND_REFUSAL_BACKOFF_S`` overrides): main's state changes on the
#: scale of a /go, not of a poll, so an immediate retry only burns the cap.
MAIN_REFUSAL_BACKOFF_S = 1800
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The slice todo.
        "todo_id": {"type": "integer"},
        # The slice's worktree (run_slice's handle.worktree).
        "worktree": {"type": "string"},
        # The slice's branch, slice/<id>.
        "branch": {"type": "string"},
        # The lane holding the lease (released on a red land).
        "lane": {"type": "string"},
        # ssh_node's host pin: the host that holds the worktree.
        "target_node": {"type": "string"},
        # A pull request's land (worktree/branch then optional, see above).
        "pr": {"type": "integer"},
        # Ship with --checked instead of --quick.
        "checked": {"type": "boolean"},
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Land one finished slice: scripts/ship --quick with the slice's commit "
    "subject in its worktree, the landed sha on the todo and into the plan's "
    "pending sha-in-prod gate, then delete the worktree and the slice/<id> "
    "branch (local and origin). A red ship reopens the todo with attempt:<n>; "
    "a refusal about main's state (drift guard) retries ship every 30 min, up to 8 times. "
    "Minted by run_slice; with params.pr it lands a pull request's head "
    "(slice/pr-<n>, --checked) and closes the PR. Refused while "
    "PRECIS_FACTORY=off."
)


# ── Config ─────────────────────────────────────────────────────────


def wall_seconds() -> float:
    """Type cap for ssh_node's lease and kill deadline
    (``PRECIS_LAND_SLICE_WALL_S``, default 1 h)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_SLICE_WALL_S", ""))
    except ValueError:
        return float(_DEFAULT_WALL_S)
    return float(n if n > 0 else _DEFAULT_WALL_S)


def refusal_backoff_s() -> int:
    """Deferral wait (``PRECIS_LAND_REFUSAL_BACKOFF_S``, default
    :data:`MAIN_REFUSAL_BACKOFF_S`; never negative)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_REFUSAL_BACKOFF_S", MAIN_REFUSAL_BACKOFF_S))
    except ValueError:
        return MAIN_REFUSAL_BACKOFF_S
    return max(n, 0)


def ship_bin(tree: Path) -> str:
    """``PRECIS_SHIP_BIN`` (the test seam), else the worktree's own
    ``scripts/ship``."""
    return os.environ.get("PRECIS_SHIP_BIN") or str(tree / "scripts" / "ship")


# ── Seams (tests monkeypatch these) ────────────────────────────────


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git <args>`` in ``cwd``; never raises on a non-zero exit."""
    return _rs._git(args, cwd)


def _launch(
    argv: list[str], *, cwd: Path, log: Path, exit_file: Path, env: dict[str, str]
) -> int:
    """Start ``argv`` detached (run_slice's launcher, stdin from
    ``/dev/null``). Returns the pid (= the process group)."""
    return _rs._launch(
        argv, cwd=cwd, stdin=Path(os.devnull), log=log, exit_file=exit_file, env=env
    )


def _process_alive(pid: int) -> bool:
    return _rs._process_alive(pid)


def _now() -> datetime:
    """The clock (UTC); a seam so tests move time."""
    return datetime.now(UTC)


def _gh(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``gh <args>`` (the PR close); never raises on a non-zero exit."""
    from precis.workers.job_types import pr_action

    return pr_action._gh(args)


def _pending_gates(store: Any, plan_tag: str, todo_id: int) -> list[tuple[int, bool]]:
    """``(gate id, blocked-by todo_id)`` of every live ``sha-in-prod`` gate
    tagged ``plan_tag`` whose ``args.sha`` is still :data:`PENDING_SHA`."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id,
                   EXISTS (SELECT 1 FROM links l
                            WHERE l.src_ref_id = r.ref_id AND l.dst_ref_id = %s
                              AND l.relation = 'blocked-by')
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t USING (tag_id)
             WHERE r.kind = 'todo' AND r.retired_at IS NULL
               AND t.namespace = 'OPEN' AND t.value = %s
               AND r.meta->'gate'->>'kind' = 'sha-in-prod'
               AND r.meta->'gate'->'args'->>'sha' = %s
             ORDER BY r.ref_id
            """,
            (todo_id, plan_tag, PENDING_SHA),
        ).fetchall()
    return [(int(r[0]), bool(r[1])) for r in rows]


# ── Subject ────────────────────────────────────────────────────────


def _cap_bytes(text: str, cap: int) -> str:
    return text.encode("utf-8")[: max(cap, 0)].decode("utf-8", errors="ignore")


def with_handle(subject: str, todo_id: int) -> str:
    """``subject`` with ``td<todo_id>`` in its trailing handle list (added
    to an existing ``(grN)`` list, else appended as `` (td<id>)``), the
    text before the list trimmed so the whole fits :data:`SUBJECT_CAP`
    bytes."""
    subject = " ".join(subject.split())
    handle = f"td{todo_id}"
    m = _HANDLES_RE.search(subject)
    if m is None:
        head, handles = subject, handle
    else:
        head, handles = subject[: m.start()], m.group(1)
        if handle not in re.findall(_HANDLE, handles):
            handles = f"{handles}, {handle}"
    tail = f" ({handles})"
    head = head.rstrip()
    if len((head + tail).encode("utf-8")) > SUBJECT_CAP:
        head = _cap_bytes(head, SUBJECT_CAP - len(tail.encode("utf-8"))).rstrip()
    return head + tail


def squash_subject(subjects: list[str], todo_id: int) -> str | None:
    """The squash subject from the branch's commit subjects (oldest
    first): the oldest naming ``td<todo_id>`` in its handle list, else the
    oldest, through :func:`with_handle`. ``None`` for no commits."""
    subjects = [s.strip() for s in subjects if s.strip()]
    if not subjects:
        return None
    handle = f"td{todo_id}"
    for s in subjects:
        m = _HANDLES_RE.search(s)
        if m and handle in re.findall(_HANDLE, m.group(1)):
            return with_handle(s, todo_id)
    return with_handle(subjects[0], todo_id)


def _branch_subjects(tree: Path) -> list[str] | None:
    proc = _git(["log", "--reverse", "--format=%s", "origin/main..HEAD"], tree)
    if proc.returncode != 0:
        return None
    return proc.stdout.splitlines()


# ── Ship output ────────────────────────────────────────────────────


def _lines(log_text: str) -> list[str]:
    return [_ANSI_RE.sub("", ln).rstrip() for ln in log_text.splitlines()]


def ship_status(log_text: str) -> str | None:
    """The last ``SHIP_STATUS: <token>`` token in ship's output."""
    for line in reversed(_lines(log_text)):
        m = _STATUS_RE.search(line)
        if m:
            return m.group(1)
    return None


def cross_line(log_text: str) -> str | None:
    """The last ``✖`` line in ship's output (its failure verdict)."""
    for line in reversed(_lines(log_text)):
        if "✖" in line:
            return line.strip()
    return None


def is_main_refusal(cross: str | None) -> bool:
    """True when ``cross`` (a :func:`cross_line` result) is ship refusing on
    main's state (:data:`MAIN_REFUSAL_MARKS`), not on the branch."""
    return bool(cross) and any(mark in str(cross) for mark in MAIN_REFUSAL_MARKS)


def landed_short(log_text: str) -> str | None:
    """The sha on the line after ship's ``shipped — landed on main:``."""
    lines = _lines(log_text)
    for i in range(len(lines) - 1, -1, -1):
        if _LANDED_MARK in lines[i] and i + 1 < len(lines):
            token = (lines[i + 1].split() or [""])[0]
            if re.fullmatch(r"[0-9a-f]{7,40}", token):
                return token
    return None


def _landed_sha(log_text: str, tree: Path) -> str | None:
    """The full landed sha: ship's report line resolved in ``tree``, else
    ``origin/main``."""
    short = landed_short(log_text)
    for rev in ([f"{short}^{{commit}}"] if short else []) + ["origin/main"]:
        proc = _git(["rev-parse", "--verify", "--quiet", rev], tree)
        sha = proc.stdout.strip()
        if proc.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    return short


# ── Store writes ───────────────────────────────────────────────────


def _newest_comment(store: Any, todo_id: int) -> str | None:
    comments = [
        c for c in _rs._todo_chunks(store, todo_id) if c.chunk_kind == _rs._COMMENT_KIND
    ]
    if not comments:
        return None
    return str(max(comments, key=lambda c: int(c.ord)).text or "")


def _close_todo(store: Any, todo_id: int) -> None:
    """``STATUS:done`` and every ``claimed-by:`` lease dropped (what the todo
    handler does on a terminal STATUS)."""
    for value in _rs._open_tag_values(store, todo_id):
        if value.startswith("claimed-by:"):
            store.remove_tag(todo_id, Tag.open(value))
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def _record_land(store: Any, todo_id: int, sha: str, subject: str) -> list[str]:
    """The ``landed:`` comment + ``STATUS:done``, each only when land_notify
    did not write it. Returns what this wrote."""
    wrote: list[str] = []
    newest = (_newest_comment(store, todo_id) or "").lstrip()
    if not newest.lower().startswith("landed:"):
        _rs._append_comment(store, todo_id, f"landed: {sha[:9]} — {subject}")
        wrote.append("landed comment")
    if _rs._todo_status(store, todo_id) != "done":
        _close_todo(store, todo_id)
        wrote.append("STATUS:done")
    return wrote


def _plan_tag(store: Any, todo_id: int) -> str | None:
    for value in _rs._open_tag_values(store, todo_id):
        if value.startswith("plan:") and value[5:].isdigit():
            return value
    return None


def pending_gate(store: Any, todo_id: int) -> int | None:
    """The plan's ``sha-in-prod`` gate this land fills: among the todo's
    plan's gates still at :data:`PENDING_SHA`, the one ``blocked-by`` the
    todo. ``None`` when the todo is in no plan or no pending gate is
    ``blocked-by`` it (so a revert task, which no gate waits on, never
    fills a gate that was reset after a REVERT verdict)."""
    plan = _plan_tag(store, todo_id)
    if plan is None:
        return None
    gates = _pending_gates(store, plan, todo_id)
    mine = [g for g, blocked_by_me in gates if blocked_by_me]
    return mine[0] if mine else None


def _write_gate(store: Any, gate_id: int, sha: str, todo_id: int) -> None:
    ref = store.get_ref(kind="todo", id=gate_id)
    gate = dict((ref.meta or {}).get("gate") or {}) if ref is not None else {}
    args = dict(gate.get("args") or {})
    args["sha"] = sha
    store.stamp_ref_meta(gate_id, {"gate": {**gate, "args": args}})
    _rs._append_comment(store, gate_id, f"sha: {sha} — landed by td{todo_id}")


# ── Cleanup ────────────────────────────────────────────────────────


def _cleanup(tree: Path, branch: str) -> list[str]:
    """Remove the worktree, the local branch and the origin branch (when
    pushed). One note per step; failures are reported, not raised."""
    notes: list[str] = []
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        return [f"cleanup skipped: {exc}"]

    def step(label: str, args: list[str]) -> bool:
        proc = _git(args, root)
        if proc.returncode == 0:
            notes.append(f"{label}: ok")
            return True
        notes.append(f"{label}: FAILED ({(proc.stderr or proc.stdout).strip()[:200]})")
        return False

    step("worktree remove", ["worktree", "remove", str(tree)])
    step("branch -D", ["branch", "-D", branch])
    remote = _git(["ls-remote", "--exit-code", "--heads", "origin", branch], root)
    if remote.returncode == 0 and remote.stdout.strip():
        step("push --delete", ["push", "origin", "--delete", branch])
    else:
        notes.append("origin branch: not pushed")
    return notes


# ── Submit gate ────────────────────────────────────────────────────


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    """Refuses while ``PRECIS_FACTORY=off`` and on a missing/closed todo."""
    del gripe_id
    if _rs.factory_off():
        return (
            "land_slice: the factory is off (PRECIS_FACTORY=off); next: unset it "
            "on the worker host to land slices"
        )
    reason = _rs._todo_rejection(store, params.get("todo_id"))
    return reason.replace("run_slice:", "land_slice:", 1) if reason else None


# ── Pull requests ──────────────────────────────────────────────────


def _pr_param(params: dict[str, Any]) -> int | None:
    pr = params.get("pr")
    if isinstance(pr, bool) or not isinstance(pr, int) or pr <= 0:
        return None
    return pr


def _plan_pr(store: Any, todo_id: int) -> dict[str, Any] | None:
    """The plan root's ``meta.pr`` (``None`` outside a pr-intake plan)."""
    from precis.plans import pr_class

    try:
        return pr_class.pr_of(store, todo_id)
    except Exception:  # pragma: no cover — a land never fails on this read
        log.warning("land_slice: meta.pr read for td%d failed", todo_id, exc_info=True)
        return None


def _pr_worktree(
    params: dict[str, Any], number: int, head: str
) -> tuple[Path | None, str]:
    """``(worktree, '')`` for a PR land, or ``(None, why)``: the pinned
    ``params.worktree`` (must exist), else ``slice-pr-<n>`` cut here from
    the PR head, which must be ``head`` (the classified sha) when known."""
    from precis.workers.job_types import dep_check as _dc

    given = str(params.get("worktree") or "")
    if given:
        tree = Path(given)
        if tree.is_dir():
            return tree, ""
        return None, (
            f"pinned worktree {tree} is missing on this host (it holds "
            "dep_check's relock); reopen the plan's check node to re-run dep_check"
        )
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        return None, str(exc)
    tree, branch = _dc.pr_tree(root, number), _dc.pr_branch(number)
    if not tree.is_dir():
        fetch = _dc.fetch_pr_head(lambda args: _git(args, root), number, head, branch)
        if fetch.returncode != 0:
            return None, (
                f"git fetch of PR #{number}'s head into {branch} failed: "
                f"{fetch.stderr.strip()[:200]}"
            )
        add = _git(["worktree", "add", str(tree), branch], root)
        if add.returncode != 0:
            return None, f"git worktree add {tree} failed: {add.stderr.strip()[:200]}"
    if head:
        rev = _git(["rev-parse", "HEAD"], tree).stdout.strip().lower()
        if not rev or not rev.startswith(head.strip().lower()[:40]):
            return None, (
                f"PR #{number}'s head is {rev[:9] or '?'}, not {head[:9]} (the sha "
                "the intake classified and the decision saw); a moved head needs "
                "a new intake"
            )
    return tree, ""


def _pr_to_close(store: Any, params: dict[str, Any], todo_id: int) -> int | None:
    """The PR a green land closes: ``params.pr``, else the todo's
    ``meta.pr_close`` (the 'land after fixes' slice)."""
    number = _pr_param(params)
    if number is not None:
        return number
    ref = store.get_ref(kind="todo", id=todo_id)
    raw = (ref.meta or {}).get("pr_close") if ref is not None else None
    return (
        raw if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0 else None
    )


def _close_pr(store: Any, number: int, todo_id: int, sha: str) -> str:
    """Close PR ``number`` with a comment naming the squash sha; one note."""
    from precis.plans import pr_class

    pr = _plan_pr(store, todo_id) or {}
    repo = pr_class.resolve_repo(store, pr.get("repo"))
    if not repo:
        return f"PR #{number}: not closed (no owner/repo; set release.repo)"
    text = (
        f"Landed on main as {sha[:12]} (squash, td{todo_id}) by the precis "
        "lander. Closing: the change is on main."
    )
    proc = _gh(["pr", "close", str(number), "--repo", repo, "--comment", text])
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()[:200]
        return f"PR #{number}: close FAILED ({err})"
    return f"PR #{number}: closed with the landed sha"


# ── Detached protocol ──────────────────────────────────────────────


def _fail(ctx: Any, todo_id: int, lane: Any, reason: str, *, handoff: str) -> None:
    """The red path: a ``handoff:`` for the next build session, the lease
    released (``attempt:<n>``), the job failed with ``reason``."""
    attempt = None
    try:
        _rs._append_comment(ctx.store, todo_id, f"handoff: {handoff}")
    except Exception:  # pragma: no cover — the job outcome still lands
        log.warning("land_slice: handoff comment on td%d failed", todo_id)
    try:
        attempt = _rs.release_todo(ctx.store, todo_id, str(lane) if lane else None)
    except Exception:  # pragma: no cover — defensive; the job still fails
        log.warning("land_slice: lease release on td%d failed", todo_id, exc_info=True)
    suffix = f"; todo td{todo_id} back to open, attempt:{attempt}" if attempt else ""
    ctx.record_failure(f"land_slice: {reason}{suffix}")


def submit(ctx: Any, spec: Any) -> Any:
    """Detached submit half: launch ship. Returns the handle, or ``None``
    after driving the job terminal."""
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    todo_id = params.get("todo_id")
    if _rs.factory_off():
        ctx.record_failure("land_slice: the factory is off (PRECIS_FACTORY=off)")
        return None
    reason = _rs._todo_rejection(ctx.store, todo_id)
    if reason is not None:
        ctx.record_failure(reason.replace("run_slice:", "land_slice:", 1))
        return None
    assert isinstance(todo_id, int)
    lane = params.get("lane")
    number = _pr_param(params)
    pr = _plan_pr(ctx.store, todo_id) if number is not None else None
    if number is not None:
        found, why = _pr_worktree(params, number, str((pr or {}).get("head_sha") or ""))
        if found is None:
            _fail(ctx, todo_id, lane, why, handoff=f"land FAILED: {why}.")
            return None
        tree = found
    else:
        tree = Path(str(params.get("worktree") or ""))
        if not str(params.get("worktree") or "") or not tree.is_dir():
            _fail(
                ctx,
                todo_id,
                lane,
                f"worktree {tree} is missing on this host",
                handoff=f"land FAILED: worktree {tree} is missing; rebuild the slice.",
            )
            return None
    subjects = _branch_subjects(tree)
    subject = squash_subject(subjects or [], todo_id)
    if subject is not None and pr is not None and pr.get("title"):
        from precis.plans import pr_class

        subject = with_handle(
            pr_class.land_subject(str(pr["title"]), str(pr.get("class") or "")),
            todo_id,
        )
    flag = "--checked" if params.get("checked") else "--quick"
    if subject is None:
        what = "git log failed" if subjects is None else "no commit ahead"
        _fail(
            ctx,
            todo_id,
            lane,
            f"nothing to land on {params.get('branch')} ({what} of origin/main)",
            handoff=(
                f"land FAILED: {params.get('branch')} has no commit ahead of "
                "origin/main; commit the work on the branch."
            ),
        )
        return None
    handle = _launch_ship(ctx, tree, flag, subject)
    if handle is None:
        return None
    ctx.set_meta(land_subject=subject)
    if handle.get("not_before"):
        return handle  # parked before launch; the deferral event says why
    ctx.append_chunk(
        "job_event",
        f"land_slice: scripts/ship {flag} {subject!r} pid {handle['pid']} in {tree}",
    )
    return handle


def _launch_ship(
    ctx: Any,
    tree: Path,
    flag: str,
    subject: str,
    *,
    carry: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Launch ``scripts/ship <flag> <subject>`` detached in ``tree`` and
    return the fresh handle (``flag`` kept in it so :func:`poll` can relaunch;
    ``main_refusals`` / ``last_refusal`` copied from ``carry``). ``None`` after
    driving the job terminal when the launch itself failed."""
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    scratch = tree / "scratch"
    log_path = scratch / "land_slice.log"
    exit_path = scratch / "land_slice.exit"
    pre = _drift_preflight(tree)
    if _gate_unknown(pre):
        # Fail closed: the lander's identity cannot see main's gate, so do not
        # let ship's "unknown never refuses" carry an unattended land. Park
        # under the same backoff and cap as a main-side refusal.
        parked: dict[str, Any] = {
            "worktree": str(tree),
            "log": str(log_path),
            "exit_file": str(exit_path),
            "subject": subject,
            "flag": flag,
        }
        for key in ("main_refusals", "last_refusal"):
            if carry is not None and key in carry:
                parked[key] = carry[key]
        refusals = int(parked.get("main_refusals") or 0) + 1
        if refusals > MAX_MAIN_REFUSALS:
            branch = str(params.get("branch") or _rs.slice_branch(todo_id))
            reason, handoff = _blocked_handoff(branch, refusals, UNKNOWN_GATE_CROSS)
            ctx.append_chunk(
                "job_summary",
                f"land_slice td{todo_id}: BLOCKED — {UNKNOWN_GATE_CROSS}\n"
                f"drift probe (lander identity): {pre['diag']}",
            )
            _fail(ctx, todo_id, params.get("lane"), reason, handoff=handoff)
            return None
        _defer_to_main(ctx, parked, UNKNOWN_GATE_CROSS, refusals, probe=pre["diag"])
        return parked
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        exit_path.unlink(missing_ok=True)
        pid = _launch(
            [ship_bin(tree), flag, subject],
            cwd=tree,
            log=log_path,
            exit_file=exit_path,
            # The factory token (vault) as git's credential helper: the slice
            # host's service account has none of its own (td543734).
            env={**_factory_git.git_auth_env(), "PRECIS_SHIP_VERBOSE": "1"},
        )
    except Exception as exc:
        log.warning("land_slice: launch for td%d failed", todo_id, exc_info=True)
        _fail(
            ctx,
            todo_id,
            params.get("lane"),
            f"ship launch failed: {exc}",
            handoff=f"land FAILED: scripts/ship did not start ({exc}).",
        )
        return None
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "worktree": str(tree),
        "log": str(log_path),
        "exit_file": str(exit_path),
        "subject": subject,
        "flag": flag,
        "started_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    for key in ("main_refusals", "last_refusal"):
        if carry is not None and key in carry:
            handle[key] = carry[key]
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


#: Seconds the drift preflight may take before it counts as unanswered.
DRIFT_PROBE_TIMEOUT_S = 45

#: The cross line the lander writes when its own identity cannot read main's
#: gate; :data:`MAIN_REFUSAL_MARKS` matches it, so it defers like ship's own.
UNKNOWN_GATE_CROSS = (
    "✖ main has no all-green shard matrix the lander's identity can read "
    "(drift guard unknown: blind token or GitHub unanswerable; fail-closed, "
    "gr550941)"
)


def _drift_preflight(tree: Path) -> dict[str, Any]:
    """What the lander's own GitHub identity sees of main's gate, before ship
    runs: ``scripts/last-gated-main-sha --age-hours --diag`` in ``tree`` under
    the factory token. ``{"ran": bool, "rc": int | None, "age": str, "diag":
    str}`` — ``age`` is the helper's stdout (hours, or empty for unknowable),
    ``diag`` its one stderr line with the exit code appended.

    gr549328/gr550941: ship refused the land on melchior while the same main
    read green elsewhere. Ship treats an unknowable lookup as "do not refuse"
    (a GitHub outage must not stop the fleet); an *unattended* land must not
    ride that — :func:`_gate_unknown` parks it instead. ``ran`` is False when
    the tree has no helper (then ship's own guard is all there is)."""
    out: dict[str, Any] = {"ran": False, "rc": None, "age": "", "diag": ""}
    helper = tree / "scripts" / "last-gated-main-sha"
    if not helper.is_file():
        return out
    try:
        cp = subprocess.run(
            [
                str(helper),
                "--age-hours",
                "--start",
                "origin/main",
                "--hours",
                "48",
                "--diag",
            ],
            cwd=tree,
            env=_factory_git.git_auth_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=DRIFT_PROBE_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        out.update(ran=True, diag=f"helper did not answer ({type(exc).__name__})")
        return out
    lines = [
        ln for ln in (cp.stderr or "").splitlines() if "last-gated-main-sha:" in ln
    ]
    diag = lines[-1].strip()[:400] if lines else "(no diag line)"
    out.update(
        ran=True,
        rc=cp.returncode,
        age=(cp.stdout or "").strip(),
        diag=f"{diag} (exit {cp.returncode})",
    )
    return out


def _gate_unknown(pre: dict[str, Any]) -> bool:
    """True when the preflight ran and could not say how old main's verdict
    is: exit 0 with empty output (blind identity, GitHub unanswerable) or no
    answer at all. Exit 2 (looked, none green) is ship's refusal to make."""
    if not pre.get("ran"):
        return False
    return pre.get("rc") != 2 and not pre.get("age")


def _blocked_handoff(branch: str, refusals: int, cross: str) -> tuple[str, str]:
    """``(reason, handoff)`` for a land parked past :data:`MAX_MAIN_REFUSALS`."""
    return (
        f"land blocked by main for {refusals} refusals: {cross}",
        f"land BLOCKED on {branch}: main refused the land {refusals} times "
        f"({cross}). Nothing is wrong on the branch; it needs a /go (full "
        "gate) over main or a fixed red on main. Do not rebuild.",
    )


def _defer_to_main(
    ctx: Any,
    handle: dict[str, Any],
    cross: str,
    refusals: int,
    *,
    probe: str | None = None,
) -> bool:
    """A main-side refusal is a deferral, not a red: count it, note it on the
    job and park the handle until ``not_before`` (now + :func:`refusal_backoff_s`);
    :func:`_relaunch_when_due` runs ship again then. The dead ship's pid,
    identity and exit file are dropped so no poll re-reads its exit. The
    handle is mutated in place and persisted to ``meta.compute_handle`` (poll
    cannot hand one back). Always ``False``: the job stays running.

    The event carries the lander identity's drift diag (``probe``, else a
    fresh :func:`_drift_preflight`) so a blind token and a red window read
    differently (gr550941)."""
    due = _now() + timedelta(seconds=refusal_backoff_s())
    for key in ("pid", "pgid", "boot_id", "start_time"):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    handle.update(
        main_refusals=refusals,
        last_refusal=cross,
        not_before=due.strftime(_TS_FMT),
    )
    if probe is None:
        probe = _drift_preflight(Path(str(handle.get("worktree") or "")))["diag"]
    ctx.append_chunk(
        "job_event",
        f"land_slice: deferred (main-side refusal #{refusals}): {cross} — "
        f"scripts/ship again after {due:%H:%M}Z"
        + (f"\ndrift probe (lander identity): {probe}" if probe else ""),
    )
    # Parked time must not eat the ssh_node wall deadline: it covers the
    # wait plus one fresh ship run.
    deadline = _now().timestamp() + refusal_backoff_s() + wall_seconds()
    ctx.set_meta(
        compute_handle=handle, compute_handle_released=False, deadline=deadline
    )
    return False


def _relaunch_when_due(ctx: Any, handle: dict[str, Any]) -> bool:
    """Poll half of a parked deferral: ``False`` before ``not_before``; at or
    after it, drop the marker and launch ship again (same worktree, flag and
    subject, counters carried), persisting the new handle in place. ``True``
    only when the launch failed (the job is then already failed)."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _TS_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        due = _now()  # unreadable marker: do not park forever
    if _now() < due:
        return False
    params = (ctx.meta or {}).get("params") or {}
    handle.pop("not_before", None)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    flag = str(
        handle.get("flag") or ("--checked" if params.get("checked") else "--quick")
    )
    fresh = _launch_ship(
        ctx, tree, flag, str(handle.get("subject") or ""), carry=handle
    )
    if fresh is None:
        return True
    handle.clear()
    handle.update(fresh)
    if fresh.get("not_before"):
        return False  # parked again by the preflight; its deferral set the meta
    ctx.set_meta(
        compute_handle=fresh,
        compute_handle_released=False,
        deadline=_now().timestamp() + wall_seconds(),
    )
    return False


def poll(ctx: Any, handle: Any) -> bool:
    """Detached poll half: ``False`` while ship runs; then the land record
    and cleanup (green) or the red path."""
    if not isinstance(handle, dict):
        ctx.record_failure(f"land_slice: malformed handle {handle!r}")
        return True
    if handle.get("not_before"):
        return _relaunch_when_due(ctx, handle)
    exit_code = _rs._read_exit(handle)
    if exit_code is None and _process_alive(int(handle.get("pid") or 0)):
        return False
    ctx.set_meta(compute_handle_released=True)

    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    number = _pr_param(params)
    default_branch = (
        f"slice/pr-{number}" if number is not None else _rs.slice_branch(todo_id)
    )
    branch = str(params.get("branch") or default_branch)
    lane = params.get("lane")
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    subject = str(handle.get("subject") or "")
    log_text = _rs._read_log(handle)
    status = ship_status(log_text)
    green = exit_code == 0 and (status is None or status == "shipped")

    if not green:
        cross = cross_line(log_text) or (
            f"scripts/ship exit {'unknown' if exit_code is None else exit_code}"
            f"{f', SHIP_STATUS {status}' if status else ''}"
        )
        reason = f"ship of td{todo_id} red: {cross}"
        handoff = (
            f"land FAILED on {branch}: {cross}. Fix it on the branch, commit, "
            "leave the tree clean; the lander re-runs scripts/ship."
        )
        if is_main_refusal(cross):
            # Main's state, not the branch's: park and retry later, up to the cap.
            refusals = int(handle.get("main_refusals") or 0) + 1
            if refusals <= MAX_MAIN_REFUSALS:
                return _defer_to_main(ctx, handle, cross, refusals)
            reason, handoff = _blocked_handoff(branch, refusals, cross)
        ctx.set_meta(exit_code=exit_code, ship_status=status)
        ctx.append_chunk(
            "job_summary",
            f"land_slice td{todo_id}: RED — {cross}\nsubject: {subject}\nlog: "
            f"{handle.get('log')}",
        )
        tail = log_text[-_TAIL_CAP:].strip()
        _fail(
            ctx,
            todo_id,
            lane,
            reason + (f"\n--- log tail ---\n{tail}" if tail else ""),
            handoff=handoff,
        )
        return True

    sha = _landed_sha(log_text, tree)
    if sha is None:
        ctx.record_failure(
            f"land_slice: ship of td{todo_id} exited 0 but no landed sha could "
            f"be read (log {handle.get('log')}); check git log origin/main"
        )
        return True
    summary = [f"land_slice td{todo_id}: landed {sha[:9]} — {subject}"]
    store = ctx.store
    try:
        wrote = _record_land(store, todo_id, sha, subject)
        summary.append(
            "todo: "
            + (", ".join(wrote) + " written" if wrote else "land_notify did it")
        )
    except Exception as exc:  # pragma: no cover — the land is real; report it
        log.warning("land_slice: land record on td%d failed", todo_id, exc_info=True)
        summary.append(f"todo: land record FAILED ({exc})")
    gate_id = None
    try:
        gate_id = pending_gate(store, todo_id)
        if gate_id is not None:
            _write_gate(store, gate_id, sha, todo_id)
            summary.append(f"gate td{gate_id}: sha-in-prod sha set to {sha[:9]}")
        else:
            summary.append("gate: no pending sha-in-prod gate blocked-by this todo")
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: gate write for td%d failed", todo_id, exc_info=True)
        summary.append(f"gate: write FAILED ({exc})")
    try:
        plan_tag = _plan_tag(store, todo_id)
        if plan_tag is not None:
            # gr548800: a landed slice is a quest-logbook milestone.
            root_id = int(plan_tag[5:])
            log_plan_event(
                store,
                todo_id,
                f"slice td{todo_id} landed {sha[:9]} — {subject}",
                entry_type="milestone",
                root=root_id,
            )
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: logbook write for td%d failed", todo_id, exc_info=True)
        summary.append(f"logbook: write FAILED ({exc})")
    cleanup = _cleanup(tree, branch)
    summary += cleanup
    closed_pr = None
    try:
        closed_pr = _pr_to_close(store, dict(params), todo_id)
        if closed_pr is not None:
            summary.append(_close_pr(store, closed_pr, todo_id, sha))
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: PR close for td%d failed", todo_id, exc_info=True)
        summary.append(f"PR close FAILED ({exc})")
    ctx.set_meta(
        exit_code=exit_code,
        ship_status=status or "shipped",
        landed_sha=sha,
        gate_id=gate_id,
        cleanup=cleanup,
        closed_pr=closed_pr,
    )
    ctx.append_chunk("job_summary", "\n".join(summary))
    ctx.set_status("succeeded")
    return True


def kill(ctx: Any, handle: Any) -> None:
    """Wall-clock / operator kill: SIGTERM ship's process group."""
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


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    validate_submit=validate_submit,
    submit=submit,
    poll=poll,
    kill=kill,
    wall_seconds=wall_seconds,
)


__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "DRIFT_PROBE_TIMEOUT_S",
    "MAIN_REFUSAL_BACKOFF_S",
    "MAIN_REFUSAL_MARKS",
    "MAX_MAIN_REFUSALS",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "SPEC",
    "SUBJECT_CAP",
    "UNKNOWN_GATE_CROSS",
    "cross_line",
    "is_main_refusal",
    "kill",
    "landed_short",
    "pending_gate",
    "poll",
    "refusal_backoff_s",
    "ship_bin",
    "ship_status",
    "squash_subject",
    "submit",
    "validate_submit",
    "wall_seconds",
    "with_handle",
]
