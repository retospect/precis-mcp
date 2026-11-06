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
  path; ``PRECIS_SHIP_VERBOSE=1`` so the log holds the whole transcript;
  ``PRECIS_GATE_N`` caps the checked gate's xdist workers at
  :func:`test_nproc` — ``PRECIS_LAND_TEST_NPROC``, default 3 — because a
  static-wide selection on melchior OOM-killed a worker twice, gr567471;
  ``PRECIS_TEST_NPROC`` carries the same count to any ``scripts/test``
  run inside the ship).
* :func:`poll` waits for ship's exit. Green: the landed sha (ship's
  ``shipped — landed on main:`` line, else ``git rev-parse origin/main``)
  goes on the job (``meta.landed_sha``); a ``landed: <sha>`` comment and
  ``STATUS:done`` go on the todo only when land_notify did not already
  write them (its comment is the todo's newest); the sha is written into
  the plan's pending ``sha-in-prod`` gate (:func:`pending_gate`). When ship
  took its nothing-new guard (:func:`nothing_new`: the branch's tree equals
  origin/main's, nothing squashed) no sha is the slice's: the todo closes
  with :data:`NO_DIFF_NOTE`, the gate stays pending and no PR is closed
  (gr561142: main's head is another land's commit). The land that fills a
  ``gripe-fix`` plan's gate also moves the plan's gripe to the verify rung
  (``resolved: <sha>`` + ``STATUS:verify``, :func:`_notify_gripe`;
  gr561932), so ``gripe_validate`` closes it once the sha is on prod. Then
  cleanup (:func:`_cleanup`): the tree's ``precis-test-slice-*`` compose
  project is brought down (``-v``; it used to stay warm forever —
  gr570133, and the SessionStart reapers never run on this host), then
  ``git worktree remove``, ``git branch -D slice/<id>`` and, when the
  branch is on origin, ``git push origin --delete slice/<id>``. A cleanup
  step that fails is reported in the summary, not a job failure: the land
  is real. Every launch — the first and each deferral relaunch — first
  sweeps the host's other finished slice stacks
  (:func:`_launch_ship` → :func:`_sweep_stacks` →
  :mod:`precis.workers.slice_stacks`): gone trees down with ``-v``,
  quiet stacks idle past ``PRECIS_SLICE_STACK_IDLE_S`` (default 3600 s;
  the warm gate-cache volume is kept) and the oldest quiet stacks over
  ``PRECIS_SLICE_STACK_CAP`` (default 8) — except any whose newest
  activity is younger than ``PRECIS_SLICE_STACK_CAP_FLOOR_S`` (default
  900 s: ship writes no run marker and gates through separate ``compose
  exec`` calls, so a quiet young stack may be a land mid-ship), and each
  cap eviction re-inspects liveness right before the ``down``. A stack
  with a running non-gate container, an attached exec or a live
  ``precis-test-runs/<pid>`` marker is never touched, and
  ``PRECIS_SLICE_STACK_SWEEP=off`` disables the sweep. Without it
  finished stacks piled into the 8 GiB docker VM until
  gates died on OOM-137 and address-pool exhaustion. Red (lint ✖, a
  forward-merge conflict, anything non-zero): the
  job fails with the ✖ line, a ``handoff:`` comment tells the next build
  session why, and the todo reopens with ``attempt:<n>`` (run_slice's
  :func:`~precis.workers.job_types.run_slice.release_todo`) — except a
  red outside the diff (a gate kill past its deferral cap, marked or a
  bare exit 137), which fails the job
  attempt-free (``meta.attempt_free = "infra-land"`` + ``red_class``, the
  lease back clean, no ``attempt:<n>``) with ``meta.land_again_cap``
  set: ``plan_dispatch`` goes to the re-land ask — never an automatic
  re-land (the same 137 can be the branch's own memory). The branch and
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
  gr550941). Every main-side deferral event ends with the helper's
  ``--diag`` line. Ship's host-side refusals (:func:`is_host_refusal`: the
  docker VM disk preflight, exit 3, no ✖) defer the same way under the same
  counter and cap; the blocked handoff then names the node, not main
  (gr552214). A red log whose gate was killed by the host
  (:func:`gate_kill_line` over the whole log, non-zero exit: ship's ✖
  ``exited 137 (SIGKILL)`` line — the docker VM's OOM killer or the
  container's own mem_limit, gr570133 — or xdist's line-start
  ``worker 'gwN' crashed while running``; the anchored shapes keep a
  failure that QUOTES the literals an ordinary red) — and a bare exit
  137, ship SIGKILLed with no mark in its log — defers the same way
  but under its own ``handle.gate_kills`` counter and the shorter
  ``PRECIS_LAND_GATE_KILL_BACKOFF_S`` (default
  120 s): the VM, not the branch, ran out of memory, so no attempt is
  burned and the relaunch sweeps the idle stacks first. An exit-137 —
  marked or bare — past
  :data:`MAX_GATE_KILL_RETRIES` fails attempt-free with a handoff naming
  the docker VM's memory or the branch's own memory use (a branch memory
  hog can hit the same mem_limit, so the handoff does not clear the
  branch): ``meta.land_again_cap`` parks the run at the
  ``plan_dispatch`` ask — no auto re-land, no builder unless the human
  rewrites the brief. An xdist crash retries once, then falls to
  the ordinary red (a deterministic crash is a branch problem).
  Whatever the red's class, the failing lines — the gate's error lines,
  else the excerpt before the ✖ — ride the job summary and the
  ``handoff:`` comment, because the land log dies with the worktree
  (gr573347 a). And a ``checked selection is RED`` verdict whose failing
  test ids are ALL outside the diff does not charge the branch on sight
  (gr573347 b): a ``verify`` phase on the handle re-runs the ids detached
  — standalone in the tree first (a green is a flake or an order
  dependence: attempt-free, the re-land tick takes it), then, still red,
  on a throwaway ``origin/main`` worktree ``slice-main-td<id>`` (always
  removed afterwards, with its stack). Ids red on main too are not the
  branch's: ship relaunches through ``--quick`` — this run's lint already
  passed — with a ``handoff``-style note on the todo. Only a main-green
  red burns an attempt; any launch or probe failure falls back to the
  ordinary red.
* :func:`kill` SIGTERMs the live phase's process group (a running
  verify's during verify, ship's otherwise) and drops the main probe's
  worktree and stack.

**Pull requests** (td543749). With ``params.pr = <n>`` (a ``pr-intake``
plan's land node) the branch is ``slice/pr-<n>`` and the worktree is
``params.worktree`` when ``dep_check`` pinned one (it must exist: it holds
the relock commit), else ``slice-pr-<n>``, cut here from ``git fetch origin
+pull/<n>/head`` and refused when the head is not the sha the intake
classified (``meta.pr.head_sha`` on the plan root). The subject is the PR
title made lint-clean (:func:`precis.plans.pr_class.land_subject`) plus
``(td<id>)``. Every land ships ``--quick`` (:func:`ship_flag`; Reto
2026-11-05, gr570221 — lint only; the release gate on ``/go`` runs the
suite before deploy) unless ``params.checked`` is ``True`` — what a
pr-intake plan's land node passes, so a PR land runs the checked gate's
contract and import-map tests. A checked land the selector refuses with
FULL (a migration or harness diff: :data:`FULL_SELECTOR_MARK`) relaunches
once with :func:`full_fallback_flag` — ``--quick``, the same lane the
default already uses (me570388) — without consuming an attempt.
After a green land — of a PR node, or of any slice whose todo carries
``meta.pr_close = <n>`` (the "land after fixes" slice) — the PR is closed
with a comment naming the squash sha (:func:`_gh`, the host's ``gh auth``:
the attribution gap of gr543694 applies). A failed close is reported in
the summary, not a job failure.

Guard: ``PRECIS_FACTORY=off`` refuses every submit. Git, launch, docker
and process probes go through module-level seams (:func:`_git`,
:func:`_launch`, :func:`_process_alive`, :func:`_pending_gates`,
:func:`_evict_stack`, :func:`_sweep_stacks`) so tests never run ship,
git, docker or SQL.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans.logbook import log_plan_event
from precis.plans.templates import PENDING_SHA
from precis.store.types import ChunkInsert, Tag
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
from precis.workers import slice_stacks as _slice_stacks
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
#: Ship's nothing-new guard: the branch's tree equals origin/main's, so no
#: squash commit was made (the ``landed on main:`` line that follows names
#: main's existing head, not the slice's work — gr561142).
_NOTHING_NEW_MARK = "nothing new to ship — tree identical to origin/main"
#: The ``landed:`` comment of a slice that landed no commit.
NO_DIFF_NOTE = (
    "landed: nothing — branch had no net diff against origin/main (already on main)"
)


def ship_flag(params: dict[str, Any]) -> str:
    """``--quick`` unless ``params.checked`` is ``True``.

    Reto 2026-11-05 (gr570221): the factory lander defaults to ``--quick``
    — lint only; the release gate on ``/go`` runs the suite before deploy.
    A ``--checked`` default sent every migration/harness slice into the
    FULL selector refusal and burned the attempts around it. ``True``
    still runs the checked gate: r200 went red on a factory land
    (8004d84e0) for an ``args`` key only the dispatch contract test
    catches, and a pr-intake plan's land node asks for that gate
    (``templates`` passes ``checked: True``).
    """
    return "--checked" if params.get("checked") is True else "--quick"


#: The ✖ line ``scripts/ship --checked`` dies with when the impacted-tests
#: selector answers FULL for the diff (a migration or a harness file): the
#: checked lane cannot run a full selection by design, so the refusal names
#: the lane, not the branch (jo570215; a builder handed it as a fix request
#: "repaired" the migration instead, gr570221).
FULL_SELECTOR_MARK = "the selector answers FULL"


def full_fallback_flag() -> str | None:
    """The flag a FULL-selector refusal relaunches with: ``--quick`` (lint
    only; the release gate on ``/go`` runs the full suite before deploy —
    Reto 2026-11-05, me570388) unless ``PRECIS_LAND_FULL_FALLBACK`` is
    ``off``, which keeps the refusal a red."""
    value = os.environ.get("PRECIS_LAND_FULL_FALLBACK", "quick").strip().lower()
    return None if value in ("off", "0", "no", "false") else "--quick"


#: Substrings of ship's own refusal lines (``die`` in ``scripts/ship``'s
#: qland drift guard) that are about MAIN's state, not the branch's: the
#: land is deferred and ship relaunched, not the slice reopened.
MAIN_REFUSAL_MARKS: tuple[str, ...] = (
    "main has no all-green shard matrix",
    "main's last all-green shard matrix is",
)
#: Substrings of ship's host-side refusals (``docker_disk_preflight`` in
#: ``scripts/lib/docker-disk.sh``, exit 3, printed to stderr without a ✖):
#: the lander's own node cannot run a gate right now. Same deferral as a
#: main refusal (gr552214): nothing on the branch caused it, and a builder
#: cannot change it.
HOST_REFUSAL_MARKS: tuple[str, ...] = (
    "docker VM disk has",
    "disk probe failed:",
    # The docker CLI itself is broken for the lander's user (no compose
    # plugin: every `docker compose …` dies parsing its own flags; jo553514).
    # Ship's ✖ then misreads the dead gate as a lockfile problem, so the host
    # line wins over the ✖ in :func:`poll`.
    "unknown shorthand flag: 'f' in -f",
    "is not a docker command",
    "Cannot connect to the Docker daemon",
    # No gate image on the node: compose cannot pull `precis-dev` and the
    # fallback build has no local :premodels seed (jo553766). The node needs
    # `scripts/build-image precis-dev`, not a branch fix.
    "pull access denied for precis-dev",
    "load metadata for precis-mcp:premodels",
)
#: Deferrals tolerated per land; the next main refusal fails the job with a
#: "land BLOCKED" handoff (nothing wrong on the branch).
MAX_MAIN_REFUSALS = 8
#: Seconds a deferred land waits before ship runs again
#: (``PRECIS_LAND_REFUSAL_BACKOFF_S`` overrides): main's state changes on the
#: scale of a /go, not of a poll, so an immediate retry only burns the cap.
MAIN_REFUSAL_BACKOFF_S = 1800
#: The shapes naming a gate process the host killed, not a verdict on the
#: diff — ANCHORED to line start, because a genuinely red run whose
#: pytest failure output quotes the literals (this feature's own
#: KILL_LOG/XDIST_LOG constants printed in a failure report) must read
#: as an ordinary red. Ship's own OOM note is a line STARTING with its
#: ✖ mark (``gate_oom_note``, scripts/lib/gate-slot.sh; exit 137 — the
#: docker VM's OOM killer or the container's own mem_limit, gr570133)
#: and pytest-xdist's dead-worker report starts the line
#: ``worker 'gwN' crashed while running '<test>'``. A red log carrying
#: one defers like a host refusal, under ``handle.gate_kills``.
GATE_KILL_137_RE = re.compile(r"^✖ .*exited 137 \(SIGKILL\)")
GATE_KILL_XDIST_RE = re.compile(r"^worker 'gw\d+' crashed while running")
#: Exit-137 deferrals tolerated per land; the next one fails the job with a
#: host handoff naming the docker VM. An xdist "crashed while running" gets
#: ONE retry — a deterministic crash is a branch problem.
MAX_GATE_KILL_RETRIES = 2
#: Seconds a gate-killed land waits before ship runs again
#: (``PRECIS_LAND_GATE_KILL_BACKOFF_S`` overrides): the sweep on relaunch
#: may have freed the VM, so the retry is quick.
GATE_KILL_BACKOFF_S = 120
#: The cause a gate-kill deferral or blocked handoff names on the job.
GATE_KILL_CROSS = "gate killed (exit 137, VM memory) - infrastructure, not the branch"
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
        # True ships --checked; default and False ship --quick (lint only).
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


def gate_kill_backoff_s() -> int:
    """Deferral wait after a gate kill (``PRECIS_LAND_GATE_KILL_BACKOFF_S``,
    default :data:`GATE_KILL_BACKOFF_S`; never negative)."""
    try:
        n = int(os.environ.get("PRECIS_LAND_GATE_KILL_BACKOFF_S", GATE_KILL_BACKOFF_S))
    except ValueError:
        return GATE_KILL_BACKOFF_S
    return max(n, 0)


def ship_bin(tree: Path) -> str:
    """``PRECIS_SHIP_BIN`` (the test seam), else the worktree's own
    ``scripts/ship``."""
    return os.environ.get("PRECIS_SHIP_BIN") or str(tree / "scripts" / "ship")


#: xdist workers for the lander's ``--checked`` pytest when
#: ``PRECIS_LAND_TEST_NPROC`` is unset: half of scripts/test's default six.
#: A 5k–8k static-wide selection OOM-killed a worker at -n6 on melchior
#: twice (gr567471, Reto 2026-11-05 "ship it").
DEFAULT_TEST_NPROC = 3


def test_nproc() -> str:
    """The xdist worker count the lander hands the ship, as ``PRECIS_GATE_N``
    (what ``scripts/ship --checked`` expands into its ``pytest -n``) and
    ``PRECIS_TEST_NPROC`` (what ``scripts/test`` reads): ``PRECIS_LAND_TEST_NPROC``
    when it is a non-negative integer, else :data:`DEFAULT_TEST_NPROC`. A
    string, because it rides an env dict."""
    raw = os.environ.get("PRECIS_LAND_TEST_NPROC", "")
    try:
        n = int(raw)
    except ValueError:
        return str(DEFAULT_TEST_NPROC)
    return str(n if n >= 0 else DEFAULT_TEST_NPROC)


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


def _evict_stack(project: str, *, volumes: bool) -> bool:
    """``docker compose -p <project> down`` on one slice stack
    (:mod:`precis.workers.slice_stacks`); never raises — docker trouble is
    a cleanup note, not a job failure."""
    try:
        return _slice_stacks.evict_project(project, volumes=volumes)
    except Exception:  # pragma: no cover — defensive; the land is real
        log.warning("land_slice: compose down of %s failed", project, exc_info=True)
        return False


def _sweep_stacks(*, keep: Path | None = None) -> list[str]:
    """The pre-launch sweep of gone/idle ``precis-test-slice-*`` stacks on
    this host (:func:`slice_stacks.sweep`); never raises."""
    try:
        return _slice_stacks.sweep(keep=keep)
    except Exception:  # pragma: no cover — defensive; a launch must go on
        log.warning("land_slice: slice-stack sweep failed", exc_info=True)
        return []


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


_GATE_ERROR_RE = re.compile(
    r"^(?:\S+\.py:\d+(?::\d+)?: (?:error|[A-Z]{1,4}\d{3,4})\b|FAILED |E {3})"
)
#: Ruff's FULL format (the pinned ruff's default): a ``CODE [*] message``
#: header line, then `` --> path:L:C`` on a following line. Condensed to
#: the concise shape ``path:L:C CODE message`` so both ruff dialects read
#: alike on the handoff (gr573347: a full-format ruff red reached the todo
#: as the bare ✖ verdict).
_RUFF_HEAD_RE = re.compile(r"^([A-Z]{1,4}\d{3,4})(?:\s+\[\*\])?\s+(.+)$")
_RUFF_LOC_RE = re.compile(r"^-->\s*(\S+?):(\d+):(\d+)\s*$")
_GATE_ERROR_LINES = 20
_GATE_ERROR_CHARS = 2500


def gate_error_lines(log_text: str) -> str:
    """The lint gate's own error lines in ship's output — mypy
    (``path.py:N: error: …``), ruff concise (``path.py:N:M: F401 …``) and
    full (``F401 [*] msg`` + `` --> path:L:C``, condensed to the concise
    shape) and pytest (``FAILED …`` / ``E   …``) — joined, at most
    :data:`_GATE_ERROR_LINES` and :data:`_GATE_ERROR_CHARS`; ``''`` when
    there are none. The next session's brief starts from the ``handoff:``
    comment, so the red's reason has to ride it (td565331: six mypy errors
    reached the todo as the one ✖ verdict line and nothing else)."""
    lines = _lines(log_text)
    out: list[str] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if _GATE_ERROR_RE.match(s):
            out.append(s)
        else:
            head = _RUFF_HEAD_RE.match(s)
            if head:
                for ahead in lines[i + 1 : i + 4]:
                    loc = _RUFF_LOC_RE.match(ahead.strip())
                    if loc:
                        out.append(
                            f"{loc.group(1)}:{loc.group(2)}:{loc.group(3)}: "
                            f"{head.group(1)} {head.group(2)}"
                        )
                        break
        if len(out) >= _GATE_ERROR_LINES:
            break
    text = "\n".join(out)
    if len(text) > _GATE_ERROR_CHARS:
        text = text[:_GATE_ERROR_CHARS].rstrip() + " …"
    return text


_GATE_EXCERPT_LINES = 40
_GATE_EXCERPT_CHARS = 3000


def gate_red_excerpt(log_text: str) -> str:
    """The last :data:`_GATE_EXCERPT_LINES` non-blank lines before the last
    ``✖`` line — the step banner plus the tool output leading to the
    verdict — capped at :data:`_GATE_EXCERPT_CHARS` (the tail kept: what
    died is nearest the ✖). ``''`` with no ``✖``. Fallback for
    :func:`gate_error_lines`: when no line parses, this is all the next
    session gets — the land log dies with the worktree (gr573347)."""
    lines = _lines(log_text)
    cross = max((i for i, ln in enumerate(lines) if "✖" in ln), default=None)
    if cross is None:
        return ""
    kept = [ln for ln in lines[:cross] if ln.strip()]
    text = "\n".join(kept[-_GATE_EXCERPT_LINES:])
    if len(text) > _GATE_EXCERPT_CHARS:
        text = "…\n" + text[-_GATE_EXCERPT_CHARS:]
    return text


#: Ship's verdict when the checked lane's pytest is red (``die`` in
#: ``scripts/ship``): only this cross triggers the outside-diff verify —
#: a lint or contract red is the branch's outright.
CHECKED_RED_MARK = "checked selection is RED"
#: The ``short test summary info`` banner; FAILED/ERROR lines count only
#: inside its block (which ends at the next ``===`` banner) — an
#: unindented captured-stdout line elsewhere cannot inject a fake id.
_SUMMARY_START_RE = re.compile(r"^=+\s*short test summary info\s*=+$")
_FAILED_ID_RE = re.compile(r"^(?:FAILED|ERROR)\s+(.+)$")


def _summary_id(rest: str) -> str | None:
    """The id half of a summary line: the text before the first `` - ``
    OUTSIDE square brackets — a parametrized id may itself contain
    `` - `` inside them (``t[a - b]``). ``None`` for unbalanced brackets
    or an empty id."""
    depth = 0
    for i, ch in enumerate(rest):
        if depth == 0 and rest.startswith(" - ", i):
            head = rest[:i].strip()
            return head or None
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth < 0:
                return None
    if depth != 0:
        return None
    head = rest.strip()
    return head or None


def failed_test_ids(log_text: str) -> list[str]:
    """Pytest's summary ids — ``FAILED <id> - <reason>`` and collection
    ``ERROR <id> - <reason>`` — de-duplicated in order, and only inside
    the ``=== short test summary info ===`` block. One malformed line
    spoils the set: unbalanced brackets or an id without ``::`` returns
    ``[]``, so the outside-diff check yields no outside set and the red
    stays ordinary."""
    out: list[str] = []
    in_summary = False
    for line in _lines(log_text):
        if _SUMMARY_START_RE.match(line):
            in_summary = True
            continue
        if not in_summary:
            continue
        if line.startswith("="):
            break
        m = _FAILED_ID_RE.match(line)
        if m:
            ident = _summary_id(m.group(1))
            if ident is None or "::" not in ident:
                return []
            if ident not in out:
                out.append(ident)
    return out


def outside_diff_ids(tree: Path, ids: list[str]) -> list[str]:
    """The ids whose test file ``git diff --name-only origin/main...HEAD``
    in ``tree`` does not name. An unanswerable diff returns ``[]`` — an
    unverifiable red stays an ordinary red, never attempt-free."""
    proc = _git(["diff", "--name-only", "origin/main...HEAD"], tree)
    if proc.returncode != 0:
        return []
    changed = {ln.strip() for ln in proc.stdout.splitlines() if ln.strip()}
    return [i for i in ids if i.split("::", 1)[0] not in changed]


def is_main_refusal(cross: str | None) -> bool:
    """True when ``cross`` (a :func:`cross_line` result) is ship refusing on
    main's state (:data:`MAIN_REFUSAL_MARKS`), not on the branch."""
    return bool(cross) and any(mark in str(cross) for mark in MAIN_REFUSAL_MARKS)


def is_host_refusal(cross: str | None) -> bool:
    """True when ``cross`` is ship refusing on the lander's own host
    (:data:`HOST_REFUSAL_MARKS`: the docker VM disk preflight)."""
    return bool(cross) and any(mark in str(cross) for mark in HOST_REFUSAL_MARKS)


def host_refusal_line(log_text: str) -> str | None:
    """The last host-side refusal line in ship's output, or ``None``. These
    carry no ✖ (jo552075: ``scripts/ship: docker VM disk has 4 GB free; …``),
    so :func:`cross_line` misses them."""
    for line in reversed(_lines(log_text)):
        if is_host_refusal(line):
            return line.strip()
    return None


def gate_kill_line(log_text: str, exit_code: int | None) -> str | None:
    """The last gate-kill line in ship's output, or ``None``. The WHOLE
    log is scanned — ship's later generic "gate is RED" ✖ at the tail
    must not hide an earlier kill line — but the marks stay anchored
    (:data:`GATE_KILL_137_RE`, :data:`GATE_KILL_XDIST_RE`): failure output
    quoting the literals is a mid-line match and an ordinary red. A clean
    exit cannot be a kill (``scripts/test`` retries a lone 137 itself, so
    a mark can sit in a successful log); ``None`` — a dead ship that left
    no exit file — stays eligible: it is itself a kill suspect."""
    if exit_code == 0:
        return None
    for line in reversed(_lines(log_text)):
        if GATE_KILL_137_RE.match(line) or GATE_KILL_XDIST_RE.match(line):
            return line.strip()
    return None


def refusal_side(cross: str | None) -> str | None:
    """``"main"`` / ``"host"`` for a refusal that is not about the branch,
    else ``None``."""
    if is_main_refusal(cross):
        return "main"
    if is_host_refusal(cross):
        return "host"
    return None


def nothing_new(log_text: str) -> bool:
    """True when ship took its nothing-new guard (:data:`_NOTHING_NEW_MARK`):
    a green exit that squashed nothing, so no sha is the slice's."""
    return any(_NOTHING_NEW_MARK in line for line in _lines(log_text))


def infra_red(log_text: str, exit_code: int | None) -> str | None:
    """The red's class — ``"gate infrastructure"`` — when ship died on the
    gate's own machinery: a gate-kill line (:func:`gate_kill_line`'s
    anchored marks) or a bare exit 137 (128+SIGKILL with or without the
    mark, gr567471's OOM signature). ``None`` on a clean exit or an
    ordinary branch red. xdist's ``[gwN] node down:`` report is NOT a
    mark: it is the ordinary per-worker death line and rides the
    worker-crash rule — one retry, then a branch red. :func:`poll`
    routes every exit 137 — marked or bare — through the ``gate_kills``
    deferral itself, so it consults this only for the rest."""
    if exit_code == 0:
        return None
    if gate_kill_line(log_text, exit_code) is not None:
        return "gate infrastructure"
    if exit_code == 137:
        return "gate infrastructure"
    return None


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


def _record_land(store: Any, todo_id: int, sha: str | None, subject: str) -> list[str]:
    """The ``landed:`` comment + ``STATUS:done``, each only when land_notify
    did not write it. ``sha=None`` is the nothing-new land: the comment is
    :data:`NO_DIFF_NOTE`, so no reader of ``landed: <sha>`` finds a sha
    (gr561142). Returns what this wrote."""
    wrote: list[str] = []
    newest = (_newest_comment(store, todo_id) or "").lstrip()
    if not newest.lower().startswith("landed:"):
        note = NO_DIFF_NOTE if sha is None else f"landed: {sha[:9]} — {subject}"
        _rs._append_comment(store, todo_id, note)
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


# ── The gripe behind the plan ──────────────────────────────────────

#: The gripe comment chunk kind (``handlers.gripe._COMMENT_KIND``; restated:
#: workers never import handlers).
_GRIPE_COMMENT_KIND = "gripe_comment"

#: Gripe STATUS values a land leaves alone: terminal, or already on the
#: verify rung (``handlers.gripe.AUTO_VERIFY_STATUSES`` + the user rung).
_GRIPE_SETTLED = frozenset({"done", "wontfix", "verify", "auto_verify", "user_verify"})


def _plan_gripe(store: Any, plan_tag: str) -> int | None:
    """The gripe the plan root is ``related-to`` (a ``gripe-fix`` plan), or
    ``None`` for any other plan."""
    root_id = int(plan_tag[5:])
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT l.dst_ref_id
              FROM links l JOIN refs g ON g.ref_id = l.dst_ref_id
             WHERE l.src_ref_id = %s AND l.relation = 'related-to'
               AND g.kind = 'gripe' AND g.retired_at IS NULL
             ORDER BY l.dst_ref_id
             LIMIT 1
            """,
            (root_id,),
        ).fetchone()
    return int(row[0]) if row else None


def _notify_gripe(
    store: Any, gripe_id: int, sha: str, subject: str, todo_id: int
) -> str:
    """What a session does after its own land (gr561932): ``resolved: <sha>``
    as the gripe's newest comment and ``STATUS:verify``; ``gripe_validate``
    closes it once the sha is on prod. Idempotent: a settled gripe is left
    alone, a comment already naming the sha is not repeated. Returns the
    summary line."""
    status = _rs._todo_status(store, gripe_id)
    if status in _GRIPE_SETTLED:
        return f"gripe gr{gripe_id}: left STATUS:{status}"
    comments = [
        c
        for c in store.chunks.list_chunks_for_ref(gripe_id)
        if c.chunk_kind == _GRIPE_COMMENT_KIND
    ]
    newest = str(max(comments, key=lambda c: int(c.ord)).text or "") if comments else ""
    wrote: list[str] = []
    if not (newest.lstrip().lower().startswith("resolved:") and sha[:9] in newest):
        ord_ = (
            max(
                (int(c.ord) for c in store.chunks.list_chunks_for_ref(gripe_id)),
                default=0,
            )
            + 1
        )
        store.chunks.insert_chunks(
            gripe_id,
            [
                ChunkInsert(
                    ord=ord_,
                    text=(
                        f"resolved: {sha[:9]} (main, release pending) — {subject} "
                        f"(landed by the factory, td{todo_id})"
                    ),
                    meta={"chunk_kind": _GRIPE_COMMENT_KIND},
                )
            ],
        )
        wrote.append("resolved comment")
    store.add_tag(
        gripe_id, Tag.closed("STATUS", "verify"), set_by="system", replace_prefix=True
    )
    wrote.append("STATUS:verify")
    return f"gripe gr{gripe_id}: {', '.join(wrote)} written"


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

    # The warm compose stack dies with the tree (gr570133): a
    # precis-test-slice-* project left up keeps a postgres and a sleep-
    # infinity gate on the 8 GiB docker VM, and the SessionStart reapers
    # never run on this host. -v: the tree is gone, nothing is kept. A
    # tree that names no slice project (a dep_check-pinned path) is a
    # designed skip — evict_project would refuse it anyway, and a FAILED
    # note misreads that as docker trouble.
    project = _slice_stacks.project_for_tree(tree)
    if not project.startswith(_slice_stacks.SLICE_PREFIX):
        notes.append(f"compose down: skipped (not a slice project: {project})")
    elif _evict_stack(project, volumes=True):
        notes.append("compose down: ok")
    else:
        notes.append(
            "compose down: FAILED "
            f"(docker compose -p {project} down --remove-orphans -v)"
        )
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


def _fail(
    ctx: Any,
    todo_id: int,
    lane: Any,
    reason: str,
    *,
    handoff: str,
    attempt_free: str | None = None,
) -> None:
    """The red path: a ``handoff:`` for the next build session, the lease
    released (``attempt:<n>``), the job failed with ``reason``.

    ``attempt_free`` (gr570187) is the red's class when it was outside the
    diff (gate infrastructure killed the land): the job is marked
    ``meta.attempt_free = "infra-land"`` + ``meta.red_class`` so
    ``plan_dispatch`` re-mints a land — never a builder — and the lease
    goes back clean (``unclaim_todo``, no ``attempt:<n>``). A caller that
    also stamped ``meta.land_again_cap`` (a capped exit-137) parks the run
    at the ask instead: the tick does not re-land it on its own."""
    attempt = None
    try:
        _rs._append_comment(ctx.store, todo_id, f"handoff: {handoff}")
    except Exception:  # pragma: no cover — the job outcome still lands
        log.warning("land_slice: handoff comment on td%d failed", todo_id)
    if attempt_free is not None:
        ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: "infra-land"}, red_class=attempt_free)
        try:
            _rs.unclaim_todo(ctx.store, todo_id, str(lane) if lane else None)
        except Exception:  # pragma: no cover — defensive; the job still fails
            log.warning(
                "land_slice: lease unclaim on td%d failed", todo_id, exc_info=True
            )
        suffix = f"; todo td{todo_id} back to open (no attempt: {attempt_free})"
    else:
        try:
            attempt = _rs.release_todo(ctx.store, todo_id, str(lane) if lane else None)
        except Exception:  # pragma: no cover — defensive; the job still fails
            log.warning(
                "land_slice: lease release on td%d failed", todo_id, exc_info=True
            )
        suffix = (
            f"; todo td{todo_id} back to open, attempt:{attempt}" if attempt else ""
        )
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
    flag = ship_flag(params)
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
        f"land_slice: scripts/ship {flag} {subject!r} pid {handle['pid']} in {tree} "
        f"(gate_n={test_nproc()})",
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
    evicted = _sweep_stacks(keep=tree)
    if evicted:
        ctx.append_chunk(
            "job_event",
            "land_slice: evicted idle slice stacks: " + ", ".join(evicted),
        )
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
        for key in ("main_refusals", "last_refusal", "gate_kills"):
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
    if pre.get("ran"):
        # What the lander's identity saw before ship ran: a verdict's age or
        # the helper's own "none" — on the job even when the land goes on
        # (jo550940 launched nine times and never recorded which).
        ctx.append_chunk(
            "job_event",
            f"land_slice: drift preflight (lander identity): age="
            f"{pre.get('age') or '-'}; {pre.get('diag')}",
        )
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        exit_path.unlink(missing_ok=True)
        pid = _launch(
            [ship_bin(tree), flag, subject],
            cwd=tree,
            log=log_path,
            exit_file=exit_path,
            # The factory token (vault) as git's credential helper: the slice
            # host's service account has none of its own (td543734). UV_PYTHON
            # pins ship's `uv run` steps to the worker's Python line: with no
            # .venv in a slice tree uv takes the newest interpreter satisfying
            # requires-python — Homebrew 3.15 on melchior, for which rdkit has
            # no wheel, so the frozen sync fails and gen-devin-hooks' check
            # reads as "stale" (jo551780 event 6).
            env={
                **_factory_git.git_auth_env(),
                "PRECIS_SHIP_VERBOSE": "1",
                "UV_PYTHON": ship_python(),
                # Cap the checked run's xdist workers (gr567471): ship's
                # checked gate runs `pytest -n ${PRECIS_GATE_N:-6}` itself,
                # not scripts/test, so both names carry the count.
                "PRECIS_GATE_N": test_nproc(),
                "PRECIS_TEST_NPROC": test_nproc(),
            },
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
    for key in ("main_refusals", "last_refusal", "gate_kills", "fell_back_full"):
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


def ship_python() -> str:
    """The Python line ship's ``uv run`` steps pin to (``UV_PYTHON``): the
    worker's own ``major.minor`` (a version, not the venv's interpreter path,
    so uv picks a matching base interpreter rather than a venv)."""
    return f"{sys.version_info.major}.{sys.version_info.minor}"


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
    helper = _preflight_helper(tree)
    if helper is None:
        return out
    try:
        cp = subprocess.run(
            [
                # The worker's own interpreter, not the helper's `uv run`
                # shebang: on melchior the deploy user's uv cache is
                # root-owned and uv exits 2 before the helper starts, which
                # ship's guard reads as "none green" (gr551238). The helper
                # is stdlib-only.
                sys.executable,
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
    if lines:
        diag = lines[-1].strip()[:400]
    else:
        # The helper never reached its diag: keep what the process said, so
        # an interpreter or shebang failure is not mistaken for a verdict.
        tail = " | ".join((cp.stderr or "").strip().splitlines()[-3:])[-300:]
        diag = f"(no diag line; stderr: {tail or '-'})"
    out.update(
        ran=True,
        rc=cp.returncode,
        age=(cp.stdout or "").strip(),
        diag=f"{diag} (exit {cp.returncode})",
    )
    return out


#: Where the preflight materialises main's copy of the drift helper. Under
#: ``scratch/`` so the helper's ``ROOT`` (two levels up) is still the tree.
#: Since gr550044 the logic lives in ``scripts/lib/last_gated_main_sha.py``
#: (the dotless name is a shim that execs it), so the lib path is tried
#: first — a materialised shim alone could not find its module. The lib
#: copy lands at ``scratch/lib/`` so its ``parents[2]`` is still the tree.
PREFLIGHT_HELPER_REL = Path("scratch") / "last-gated-main-sha"
PREFLIGHT_LIB_REL = Path("scratch") / "lib" / "last_gated_main_sha.py"


def _preflight_helper(tree: Path) -> Path | None:
    """The drift helper the preflight runs: ``origin/main``'s copy, written to
    ``scratch/`` in ``tree`` after a fetch, else the tree's own helper, else
    ``None``.

    A slice branch forks from an older main and ship only merges main in
    *after* the guard; the branch's helper can predate the rule and the flag
    the preflight relies on (jo550940 event 6: "(no diag line) (exit 2)" from
    a copy without ``--diag``). Main's copy is the one ship itself will run:
    the lib module when main has it, else the self-contained dotless script
    an older main still carries."""
    _git(["fetch", "--quiet", "origin", "main"], tree)
    for src, rel in (
        ("scripts/lib/last_gated_main_sha.py", PREFLIGHT_LIB_REL),
        ("scripts/last-gated-main-sha", PREFLIGHT_HELPER_REL),
    ):
        cp = _git(["show", f"origin/main:{src}"], tree)
        if cp.returncode == 0 and cp.stdout.strip():
            target = tree / rel
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(cp.stdout, encoding="utf-8")
                target.chmod(0o755)
                return target
            except OSError:
                log.warning("land_slice: could not write %s", target, exc_info=True)
    for own in (
        tree / "scripts" / "lib" / "last_gated_main_sha.py",
        tree / "scripts" / "last-gated-main-sha",
    ):
        if own.is_file():
            return own
    return None


def _gate_unknown(pre: dict[str, Any]) -> bool:
    """True when the preflight ran and could not say how old main's verdict
    is: exit 0 with empty output (blind identity, GitHub unanswerable), no
    answer at all, or a process that died before the helper's diag line
    (whatever its exit code — a crashed interpreter is not a verdict). Exit 2
    *from the helper* (looked, none green) is ship's refusal to make."""
    if not pre.get("ran"):
        return False
    if str(pre.get("diag") or "").startswith("(no diag line"):
        return True
    return pre.get("rc") != 2 and not pre.get("age")


def _blocked_handoff(
    branch: str, refusals: int, cross: str, side: str = "main"
) -> tuple[str, str]:
    """``(reason, handoff)`` for a land parked past its deferral cap.
    ``side`` is :func:`refusal_side`, or ``"gate"`` for a land that kept
    being gate-killed: the handoff names what has to change."""
    if side == "host":
        return (
            f"land blocked by the lander's host for {refusals} refusals: {cross}",
            f"land BLOCKED on {branch}: the lander's host refused the land "
            f"{refusals} times ({cross}). Nothing is wrong on the branch; the "
            "node needs the ops action the refusal names (an operator, not a "
            "builder). Do not rebuild.",
        )
    if side == "gate":
        # Exit 137 is ALSO the container's own mem_limit (24 GiB,
        # docker/dev/compose.yaml) — a branch's memory hog can hit it, so
        # the handoff does not clear the branch ("Do not rebuild" would
        # be wrong for that half).
        return (
            f"land blocked by gate kills for {refusals} retries: {cross}",
            f"land BLOCKED on {branch}: the gate was killed {refusals} "
            f"times ({cross}). The cause is the docker VM's memory or the "
            "branch's own memory use — a memory hog on the branch is the "
            "builder's to fix; otherwise an operator frees the docker VM.",
        )
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
    side: str = "main",
    backoff_s: int | None = None,
    counter: str = "main_refusals",
) -> bool:
    """A main-side (or host-side, ``side="host"``) refusal is a deferral, not
    a red: count it in ``counter`` (``gate_kills`` for a gate kill — never
    ``main_refusals``, which is only about main's verdict), note it on the
    job and park the handle until ``not_before`` (now +
    :func:`refusal_backoff_s`, or ``backoff_s`` when given — the shorter
    gate-kill wait); :func:`_relaunch_when_due` runs ship again then. The
    dead ship's pid, identity and exit file are dropped so no poll re-reads
    its exit. The handle is mutated in place and persisted to
    ``meta.compute_handle`` (poll cannot hand one back). Always ``False``:
    the job stays running.

    A main-side event carries the lander identity's drift diag (``probe``,
    else a fresh :func:`_drift_preflight`) so a blind token and a red window
    read differently (gr550941); a host-side one has nothing to probe."""
    delay = refusal_backoff_s() if backoff_s is None else backoff_s
    due = _now() + timedelta(seconds=delay)
    for key in ("pid", "pgid", "boot_id", "start_time"):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    handle.update(last_refusal=cross, not_before=due.strftime(_TS_FMT))
    handle[counter] = refusals
    if probe is None and side == "main":
        probe = _drift_preflight(Path(str(handle.get("worktree") or "")))["diag"]
    ctx.append_chunk(
        "job_event",
        f"land_slice: deferred ({side}-side refusal #{refusals}): {cross} — "
        f"scripts/ship again after {due:%H:%M}Z"
        + (f"\ndrift probe (lander identity): {probe}" if probe else ""),
    )
    # Parked time must not eat the ssh_node wall deadline: it covers the
    # wait plus one fresh ship run.
    deadline = _now().timestamp() + delay + wall_seconds()
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
    flag = str(handle.get("flag") or ship_flag(params))
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


def _launch_verify(
    slice_tree: Path, ids: list[str], *, phase: str, run_tree: Path
) -> dict[str, Any] | None:
    """Detached ``scripts/test <ids>`` in ``run_tree``, the log and exit
    file under the SLICE tree's ``scratch/`` so they outlive a probe
    worktree. ``None`` when the launch itself failed — the caller falls
    back to the ordinary red."""
    scratch = slice_tree / "scratch"
    log_path = scratch / f"land_verify_{phase}.log"
    exit_path = scratch / f"land_verify_{phase}.exit"
    try:
        scratch.mkdir(parents=True, exist_ok=True)
        exit_path.unlink(missing_ok=True)
        pid = _launch(
            [str(run_tree / "scripts" / "test"), *ids],
            cwd=run_tree,
            log=log_path,
            exit_file=exit_path,
            env={
                **_factory_git.git_auth_env(),
                "UV_PYTHON": ship_python(),
                "PRECIS_TEST_NPROC": test_nproc(),
            },
        )
    except Exception as exc:
        log.warning(
            "land_slice: verify launch (%s) failed: %s", phase, exc, exc_info=True
        )
        return None
    verify: dict[str, Any] = {
        "phase": phase,
        "ids": list(ids),
        "pid": pid,
        "pgid": pid,
        "log": str(log_path),
        "exit_file": str(exit_path),
        "run_tree": str(run_tree),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        verify["boot_id"], verify["start_time"] = identity
    return verify


def _promote_verify(handle: dict[str, Any], verify: dict[str, Any]) -> None:
    """Make the running probe the handle's live process — ``pid``,
    ``pgid``, ``boot_id`` and ``start_time`` copied to the TOP level, the
    same slot the ship relaunch fills. The executor's cancel, wall-clock
    and orphan-reap gates all validate the top-level identity before
    calling this job's ``kill``; while it still names the dead ship every
    gate fails closed and the kill hook never runs, leaking the probe and
    the ``slice-main-td<id>`` worktree."""
    handle["verify"] = verify
    for key in ("pid", "pgid", "boot_id", "start_time"):
        if key in verify:
            handle[key] = verify[key]
        else:
            handle.pop(key, None)


def _ordinary_ship_red(ctx: Any, handle: dict[str, Any]) -> bool:
    """Back to the ship red's ordinary path once a verify phase is spent —
    or could not probe: ``verify_done`` keeps the outside-diff launch from
    refiring, so the branch is charged as today."""
    handle.pop("verify", None)
    handle["verify_done"] = True
    return poll(ctx, handle)


def _probe_red(v: dict[str, Any], ids: list[str]) -> bool:
    """True only when the probe's own log shows the SAME ids failing —
    a plain test failure is evidence. 'no tests collected', 'not found',
    a usage error, a subset/superset of the probed ids, or any other
    non-zero exit without matching summary lines is a probe failure,
    never evidence: the caller takes the ordinary red."""
    failed = set(failed_test_ids(_rs._read_log(v)))
    return bool(failed) and failed == set(ids)


def _start_main_verify(ctx: Any, handle: dict[str, Any], v: dict[str, Any]) -> bool:
    """Phase ``main`` of the outside-diff verify: cut ``slice-main-td<id>``
    — a detached ``origin/main`` worktree under the worktrees dir
    (``slice_root()/.claude/worktrees``, where
    :func:`run_slice._ensure_worktree` puts the slice trees), so its
    compose project carries the slice prefix and the stack sweep reaps
    it — and run the ids there. The worktree is dropped on every later
    exit path (:func:`_drop_main_verify`). ``False`` → the ordinary
    red."""
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    if todo_id <= 0:
        ctx.append_chunk(
            "job_event",
            f"land_slice: main probe skipped (no positive todo id: {todo_id})",
        )
        return False
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        ctx.append_chunk("job_event", f"land_slice: main probe skipped ({exc})")
        return False
    main_tree = _rs.worktrees_dir(root) / f"slice-main-td{todo_id}"
    project = _slice_stacks.project_for_tree(main_tree)
    # What this job is about to make goes on the job meta BEFORE the add:
    # a kill (or a crash) after this point must be able to remove it.
    ctx.set_meta(main_probe={"tree": str(main_tree), "stack": project})
    if not _clear_stale_main_probe(ctx, root, main_tree, project, todo_id):
        return False
    add = _git(["worktree", "add", "--detach", str(main_tree), "origin/main"], root)
    if add.returncode != 0:
        ctx.append_chunk(
            "job_event",
            "land_slice: main probe worktree add failed: "
            f"{(add.stderr or add.stdout).strip()[:200]}",
        )
        ctx.set_meta(main_probe=None)
        return False
    verify = _launch_verify(
        tree, [str(i) for i in (v.get("ids") or [])], phase="main", run_tree=main_tree
    )
    if verify is None:
        _git(["worktree", "remove", "--force", str(main_tree)], root)
        ctx.set_meta(main_probe=None)
        return False
    verify["main_tree"] = str(main_tree)
    _promote_verify(handle, verify)
    ctx.append_chunk(
        "job_event",
        "land_slice: ids still red standalone — probing "
        f"{', '.join(verify['ids'])} on origin/main ({main_tree.name})",
    )
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=_now().timestamp() + wall_seconds(),
    )
    return True


def _clear_stale_main_probe(
    ctx: Any, root: Path, main_tree: Path, project: str, todo_id: int
) -> bool:
    """What ``_start_main_verify`` clears before its add, when a crashed
    run left ``slice-main-td<id>`` behind: ``worktree prune`` drops
    registrations whose dirs vanished; if the path is still a REGISTERED
    worktree it comes down through git; if it is an unregistered leftover
    directory it is deleted — only that exact validated path, never
    anything else. ``False`` → the ordinary red."""
    _git(["worktree", "prune"], root)
    listed = _git(["worktree", "list", "--porcelain"], root)
    registered = set()
    if listed.returncode == 0:
        registered = {
            ln.split(None, 1)[1].strip()
            for ln in (listed.stdout or "").splitlines()
            if ln.startswith("worktree ")
        }
    if str(main_tree) in registered or os.path.realpath(main_tree) in registered:
        if project.startswith(_slice_stacks.SLICE_PREFIX):
            _evict_stack(project, volumes=True)
        stale = _git(["worktree", "remove", "--force", str(main_tree)], root)
        if stale.returncode != 0:
            ctx.append_chunk(
                "job_event",
                "land_slice: stale main probe worktree remove failed: "
                f"{(stale.stderr or stale.stdout).strip()[:200]}",
            )
            return False
        ctx.append_chunk(
            "job_event",
            f"land_slice: removed stale main probe worktree {main_tree.name}",
        )
        return True
    if not main_tree.exists():
        return True
    # An unregistered leftover directory — debris git no longer knows
    # about. Deleted only when it is exactly the expected probe path.
    expected = _rs.worktrees_dir(root) / f"slice-main-td{todo_id}"
    if main_tree != expected:
        ctx.append_chunk(
            "job_event",
            f"land_slice: refusing to delete {main_tree} (not {expected})",
        )
        return False
    if project.startswith(_slice_stacks.SLICE_PREFIX):
        _evict_stack(project, volumes=True)
    try:
        shutil.rmtree(main_tree)
    except OSError as exc:
        ctx.append_chunk(
            "job_event",
            f"land_slice: stale main probe leftover remove failed: {exc}",
        )
        return False
    ctx.append_chunk(
        "job_event",
        f"land_slice: deleted stale main probe leftover {main_tree.name}",
    )
    return True


def _drop_main_verify(v: dict[str, Any]) -> list[str]:
    """Remove the ``slice-main-td<id>`` worktree and its compose stack —
    on EVERY exit path of phase ``main``. Notes, never raises."""
    notes: list[str] = []
    raw = str(v.get("main_tree") or "")
    if not raw:
        return notes
    main_tree = Path(raw)
    project = _slice_stacks.project_for_tree(main_tree)
    if project.startswith(_slice_stacks.SLICE_PREFIX):
        notes.append(
            f"main probe stack {project}: "
            + ("down" if _evict_stack(project, volumes=True) else "down FAILED")
        )
    try:
        root = _rs.slice_root()
    except RuntimeError as exc:
        notes.append(f"main probe worktree remove skipped ({exc})")
        return notes
    rm = _git(["worktree", "remove", "--force", str(main_tree)], root)
    if rm.returncode == 0:
        notes.append(f"main probe worktree {main_tree.name}: removed")
    else:
        notes.append(
            "main probe worktree remove FAILED "
            f"({(rm.stderr or rm.stdout).strip()[:200]})"
        )
    return notes


def _poll_verify(ctx: Any, handle: dict[str, Any]) -> bool:
    """Detached verify half (gr573347 b): a ``checked selection is RED``
    whose failing ids are ALL outside the diff is re-run before the branch
    is charged — ``alone`` in the slice tree, then ``main`` on a
    throwaway ``origin/main`` worktree:

    * alone green → flaky or order-dependent: attempt-free
      (``red_class = "flaky outside the diff"``), the re-land tick takes
      it from there;
    * alone red + main red → not the branch's: ship relaunches through
      :func:`full_fallback_flag` (``--quick`` — this run's lint already
      passed; the checked tests run after it), a ``handoff``-style note
      names the ids on the todo;
    * alone red + main green → the branch's red after all: the ordinary
      red, attempt burned;
    * any launch or probe failure → the ordinary red."""
    v = handle["verify"]
    exit_code = _rs._read_exit(v)
    if exit_code is None and _process_alive(int(v.get("pid") or 0)):
        return False
    params = (ctx.meta or {}).get("params") or {}
    todo_id = int(params.get("todo_id") or 0)
    ids = [str(i) for i in (v.get("ids") or [])]
    phase = str(v.get("phase") or "")
    if phase == "alone":
        if exit_code == 0:
            number = _pr_param(params)
            default_branch = (
                f"slice/pr-{number}"
                if number is not None
                else _rs.slice_branch(todo_id)
            )
            branch = str(params.get("branch") or default_branch)
            ship_log = _rs._read_log(handle)
            cross = cross_line(ship_log) or CHECKED_RED_MARK
            ctx.append_chunk(
                "job_summary",
                f"land_slice td{todo_id}: RED — {cross}\n"
                f"subject: {handle.get('subject')}\nlog: {handle.get('log')}\n"
                f"verify: {', '.join(ids)} outside the diff, green "
                "standalone (flaky or order-dependent)",
            )
            _fail(
                ctx,
                todo_id,
                params.get("lane"),
                f"ship of td{todo_id} red: {cross} — outside-diff ids green standalone",
                handoff=(
                    f"land FAILED on {branch}: {cross}. The failing ids "
                    f"({', '.join(ids)}) are outside the diff and passed "
                    "standalone — flaky or order-dependent. Nothing to "
                    "rebuild; the lander re-lands."
                ),
                attempt_free="flaky outside the diff",
            )
            return True
        # A non-zero exit is only "red alone" when the probe's own
        # summary shows the named ids failing — anything else is a probe
        # failure and stays the ordinary red.
        if exit_code is not None and _probe_red(v, ids):
            if _start_main_verify(ctx, handle, v):
                return False
        return _ordinary_ship_red(ctx, handle)
    # Phase "main" finished (or the probe died): the throwaway worktree
    # and its stack go away on every exit path — and the job meta forgets
    # the probe, so a later kill does not remove a tree already gone.
    for note in _drop_main_verify(v):
        ctx.append_chunk("job_event", f"land_slice: {note}")
    ctx.set_meta(main_probe=None)
    if phase != "main" or exit_code is None:
        return _ordinary_ship_red(ctx, handle)
    if exit_code == 0:
        handle["verify_note"] = (
            f"Outside-diff verify: {', '.join(ids)} are GREEN on "
            "origin/main — the red is this branch's."
        )
        return _ordinary_ship_red(ctx, handle)
    if not _probe_red(v, ids):
        # "red on main too" needs the same evidence: a non-zero probe that
        # never ran the named tests is a probe failure, never a verdict.
        return _ordinary_ship_red(ctx, handle)
    flag = full_fallback_flag()
    if flag is None:
        return _ordinary_ship_red(ctx, handle)
    tree = Path(str(handle.get("worktree") or params.get("worktree") or ""))
    subject = str(handle.get("subject") or "")
    try:
        _rs._append_comment(
            ctx.store,
            todo_id,
            f"handoff: {', '.join(ids)} are red on origin/main too — the "
            "checked red is not this branch's; the land goes through "
            "--quick (lint only). The reds are main's to fix.",
        )
    except Exception:  # pragma: no cover — the land still goes on
        log.warning("land_slice: main-red note on td%d failed", todo_id)
    ctx.append_chunk(
        "job_event",
        f"land_slice: {', '.join(ids)} red on origin/main too — relaunching "
        f"scripts/ship {flag} (lint only; this run's lint already passed)",
    )
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    fresh = _launch_ship(ctx, tree, flag, subject, carry=handle)
    if fresh is None:
        return True
    handle.clear()
    handle.update(fresh)
    if fresh.get("not_before"):
        return False  # parked by the preflight; its deferral set the meta
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
    if isinstance(handle.get("verify"), dict):
        return _poll_verify(ctx, handle)
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
        # A host-side line outranks the ✖: when docker itself is broken the ✖
        # that follows blames the branch's lockfile (jo553514).
        cross = (
            host_refusal_line(log_text)
            or cross_line(log_text)
            or (
                f"scripts/ship exit {'unknown' if exit_code is None else exit_code}"
                f"{f', SHIP_STATUS {status}' if status else ''}"
            )
        )
        reason = f"ship of td{todo_id} red: {cross}"
        handoff = (
            f"land FAILED on {branch}: {cross}. Fix it on the branch, commit, "
            "leave the tree clean; the lander re-runs scripts/ship."
        )
        # Gate output can echo credentials (env dumps, request URLs): mask
        # FIRST, then cut — a secret spanning the cut boundary can
        # otherwise leak a fragment. What rides the todo comment and the
        # job summary is already masked.
        masked_log = mask_secrets(log_text) or ""
        errors = gate_error_lines(masked_log)
        detail = errors or gate_red_excerpt(masked_log)
        if errors:
            handoff += f"\n\nThe gate's error lines:\n{errors}"
        elif detail:
            # Nothing parsed — the excerpt is all the next session gets;
            # the land log dies with the worktree (gr573347).
            handoff += f"\n\nThe gate's output before the verdict:\n{detail}"
        fallback = full_fallback_flag()
        if (
            fallback
            and FULL_SELECTOR_MARK in cross
            and str(handle.get("flag") or ship_flag(params)) != fallback
        ):
            # The lane is refused, not the branch: relaunch through the quick
            # lane (Reto 2026-11-05, me570388). No attempt is consumed and no
            # builder is asked to fix what the ship said about itself.
            Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
            ctx.append_chunk(
                "job_event",
                f"land_slice: {cross} — relaunching scripts/ship {fallback} "
                "(lint only; the release gate on /go runs the full suite)",
            )
            # Carried onto the fresh handle so the green path's todo
            # comment can say the checked land fell back (td573580).
            handle["fell_back_full"] = True
            fresh = _launch_ship(ctx, tree, fallback, subject, carry=handle)
            if fresh is None:
                return True
            handle.clear()
            handle.update(fresh)
            if fresh.get("not_before"):
                return False  # parked by the preflight; its deferral set the meta
            ctx.set_meta(
                compute_handle=fresh,
                compute_handle_released=False,
                deadline=_now().timestamp() + wall_seconds(),
            )
            return False
        note = handle.pop("verify_note", None)
        if note:
            handoff += f"\n\n{note}"
        # gr573347 b: a checked selection red only on test ids the diff
        # does not touch may not be the branch's — verify standalone,
        # then on origin/main, before charging an attempt. Any launch or
        # probe trouble falls through to the ordinary red.
        ids = failed_test_ids(log_text)
        if (
            handle.get("verify_done") is None
            and CHECKED_RED_MARK in cross
            and todo_id > 0
            and ids
            and len(outside_diff_ids(tree, ids)) == len(ids)
        ):
            verify = _launch_verify(tree, ids, phase="alone", run_tree=tree)
            if verify is not None:
                _promote_verify(handle, verify)
                ctx.append_chunk(
                    "job_event",
                    "land_slice: checked selection red only on ids outside "
                    f"the diff — re-running {', '.join(ids)} standalone "
                    "before charging the branch",
                )
                ctx.set_meta(
                    compute_handle=handle,
                    compute_handle_released=False,
                    deadline=_now().timestamp() + wall_seconds(),
                )
                return False
        side = refusal_side(cross)
        if side is not None:
            # Main's or the host's state, not the branch's: park and retry
            # later, up to the cap.
            refusals = int(handle.get("main_refusals") or 0) + 1
            if refusals <= MAX_MAIN_REFUSALS:
                return _defer_to_main(ctx, handle, cross, refusals, side=side)
            reason, handoff = _blocked_handoff(branch, refusals, cross, side)
        else:
            # A gate the VM killed is host-side too (gr570133): the land
            # defers and retries under `gate_kills` — its own counter, the
            # shorter kill backoff, and the sweep on relaunch — never an
            # attempt, never a handoff. A bare exit 137 (ship SIGKILLed,
            # no mark in its log) is the same kill and rides the same
            # counter. An exit-137 past the cap is still outside the
            # diff — no attempt is burned — but the container's own
            # mem_limit is also 137, so the branch's own memory may be at
            # fault: it does NOT re-land on its own
            # (``meta.land_again_cap`` sends the tick to the ask, keeping
            # the blocked handoff that names the VM's or the branch's
            # memory, gr570187). An xdist crash retries once, then falls
            # through to the ordinary red: a repeated crash is
            # deterministic — a branch problem — and the ``[gwN] node
            # down:`` line xdist emits for it is not an infra mark.
            kill_line = gate_kill_line(log_text, exit_code)
            red_class: str | None = None
            kills = int(handle.get("gate_kills") or 0) + 1
            if kill_line is not None and "exited 137 (SIGKILL)" not in kill_line:
                # A repeated worker crash is deterministic — a branch
                # problem — so ``red_class`` stays ``None`` and the
                # ordinary red below burns an attempt (main's rule).
                if kills <= 1:
                    return _defer_to_main(
                        ctx,
                        handle,
                        f"gate worker crashed mid-run (xdist): {kill_line}",
                        kills,
                        side="host",
                        backoff_s=gate_kill_backoff_s(),
                        counter="gate_kills",
                    )
            elif kill_line is not None or exit_code == 137:
                kill_cross = f"{GATE_KILL_CROSS}: {kill_line or cross}"
                if kills <= MAX_GATE_KILL_RETRIES:
                    return _defer_to_main(
                        ctx,
                        handle,
                        kill_cross,
                        kills,
                        side="host",
                        backoff_s=gate_kill_backoff_s(),
                        counter="gate_kills",
                    )
                reason, handoff = _blocked_handoff(branch, kills, kill_cross, "gate")
                red_class = "gate infrastructure"
            else:
                red_class = infra_red(log_text, exit_code)
            if red_class is not None:
                # Outside the diff (no attempt), but spent — the tick
                # goes to the ask rather than re-landing (gr570187).
                ctx.set_meta(land_again_cap=True)
        ctx.set_meta(exit_code=exit_code, ship_status=status)
        ctx.append_chunk(
            "job_summary",
            f"land_slice td{todo_id}: RED — {cross}\nsubject: {subject}\nlog: "
            f"{handle.get('log')}" + (f"\n{detail}" if detail else ""),
        )
        tail = masked_log[-_TAIL_CAP:].strip()
        _fail(
            ctx,
            todo_id,
            lane,
            reason + (f"\n--- log tail ---\n{tail}" if tail else ""),
            handoff=handoff,
            attempt_free=red_class if side is None else None,
        )
        return True

    # gr561142: ship's nothing-new guard squashes nothing and then names
    # main's existing head on its ``landed on main:`` line. That sha is
    # another land's; it must not reach the todo, the gate or the logbook.
    no_diff = nothing_new(log_text)
    sha = None if no_diff else _landed_sha(log_text, tree)
    if sha is None and not no_diff:
        ctx.record_failure(
            f"land_slice: ship of td{todo_id} exited 0 but no landed sha could "
            f"be read (log {handle.get('log')}); check git log origin/main"
        )
        return True
    landed = f"landed {sha[:9]} — {subject}" if sha else NO_DIFF_NOTE
    summary = [f"land_slice td{todo_id}: {landed}"]
    store = ctx.store
    try:
        wrote = _record_land(store, todo_id, sha, subject)
        summary.append(
            "todo: "
            + (", ".join(wrote) + " written" if wrote else "land_notify did it")
        )
        if handle.get("fell_back_full"):
            # td573580 review: a checked land the FULL selector sent
            # through --quick says so in plain words on the todo.
            _rs._append_comment(
                store,
                todo_id,
                "checked wanted, selector FULL, landed --quick — the "
                "release gate runs the suite",
            )
            summary.append("todo: checked→quick fallback noted")
    except Exception as exc:  # pragma: no cover — the land is real; report it
        log.warning("land_slice: land record on td%d failed", todo_id, exc_info=True)
        summary.append(f"todo: land record FAILED ({exc})")
    gate_id = None
    try:
        gate_id = pending_gate(store, todo_id)
        if gate_id is None:
            summary.append("gate: no pending sha-in-prod gate blocked-by this todo")
        elif sha is None:
            summary.append(
                f"gate td{gate_id}: left pending — no sha landed (the work is "
                "already on main; the plan decides what the gate waits for)"
            )
        else:
            _write_gate(store, gate_id, sha, todo_id)
            summary.append(f"gate td{gate_id}: sha-in-prod sha set to {sha[:9]}")
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: gate write for td%d failed", todo_id, exc_info=True)
        summary.append(f"gate: write FAILED ({exc})")
    plan_tag = _plan_tag(store, todo_id)
    try:
        # gr561932: the land that fills the plan's gate is the fix; it moves
        # the gripe the plan is about to the verify rung, as a session
        # would. A brief node's land (a spec test) fills no gate and leaves
        # the gripe alone.
        if gate_id is not None and sha is not None and plan_tag is not None:
            gripe_id = _plan_gripe(store, plan_tag)
            if gripe_id is not None:
                summary.append(_notify_gripe(store, gripe_id, sha, subject, todo_id))
    except Exception as exc:  # pragma: no cover — reported, the land stands
        log.warning("land_slice: gripe notify for td%d failed", todo_id, exc_info=True)
        summary.append(f"gripe: notify FAILED ({exc})")
    try:
        if plan_tag is not None:
            # gr548800: a landed slice is a quest-logbook milestone.
            root_id = int(plan_tag[5:])
            log_plan_event(
                store,
                todo_id,
                f"slice td{todo_id} {landed}",
                entry_type="milestone" if sha else "observation",
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
        if closed_pr is None:
            pass
        elif sha is None:
            summary.append(f"PR #{closed_pr}: left open — nothing landed")
            closed_pr = None
        else:
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
    """Wall-clock / operator kill: SIGTERM the live phase's process group —
    a running verify's during verify, ship's otherwise (a ship that
    already exited must never be signaled: the pgid may have been
    reused) — then drop the main-probe worktree and stack the job meta
    recorded (:func:`_start_main_verify` writes them before the add), so
    a killed land leaves neither behind."""
    if isinstance(handle, dict):
        verify = handle.get("verify")
        live = verify if isinstance(verify, dict) else handle
        pgid = int(live.get("pgid") or live.get("pid") or 0)
        if pgid > 1:
            try:
                _rs._signal_group(pgid, signal.SIGTERM)
            except ProcessLookupError:
                pass
    probe = (getattr(ctx, "meta", None) or {}).get("main_probe") or {}
    tree = str(probe.get("tree") or "")
    if not tree:
        return
    stack = str(probe.get("stack") or "")
    if stack.startswith(_slice_stacks.SLICE_PREFIX):
        _evict_stack(stack, volumes=True)
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return
    _git(["worktree", "remove", "--force", tree], root)


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
    "CHECKED_RED_MARK",
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "DRIFT_PROBE_TIMEOUT_S",
    "GATE_KILL_137_RE",
    "GATE_KILL_BACKOFF_S",
    "GATE_KILL_CROSS",
    "GATE_KILL_XDIST_RE",
    "HOST_REFUSAL_MARKS",
    "MAIN_REFUSAL_BACKOFF_S",
    "MAIN_REFUSAL_MARKS",
    "MAX_GATE_KILL_RETRIES",
    "MAX_MAIN_REFUSALS",
    "NAME",
    "PARAMS_SCHEMA",
    "PREFLIGHT_HELPER_REL",
    "PREFLIGHT_LIB_REL",
    "REQUIRES",
    "SPEC",
    "SUBJECT_CAP",
    "UNKNOWN_GATE_CROSS",
    "cross_line",
    "failed_test_ids",
    "gate_error_lines",
    "gate_kill_backoff_s",
    "gate_kill_line",
    "gate_red_excerpt",
    "host_refusal_line",
    "is_host_refusal",
    "is_main_refusal",
    "kill",
    "landed_short",
    "outside_diff_ids",
    "pending_gate",
    "poll",
    "refusal_backoff_s",
    "refusal_side",
    "ship_bin",
    "ship_python",
    "ship_status",
    "squash_subject",
    "submit",
    "validate_submit",
    "wall_seconds",
    "with_handle",
]
