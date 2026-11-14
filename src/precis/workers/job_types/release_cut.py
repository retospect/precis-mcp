"""``release_cut`` — cut ``release/r<N>`` and return (dark release pass, td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``cut`` node of the ``release-pass`` template. Mechanical, no LLM. A
detached submit/poll pair under ``ssh_node``:

* :func:`submit` runs ``scripts/release --cut-only`` in the repo checkout
  (:func:`precis.workers.job_types.run_slice.slice_root`: ``PRECIS_SLICE_ROOT``,
  else the main checkout around the worker's code) detached, stdout+stderr to
  a log and the exit code to a file beside it (under the temp dir), and
  returns ``{pid, pgid, root, log, exit_file, started_at}``. The sha it
  cuts comes from :func:`_cut_target`: the pass's recorded
  ``meta.release_target.sha`` (origin/main's pinned tip when the poll
  minted it) for a first cut, else origin/main's tip — ``--sha`` is
  passed only in the first case. No review ever holds a cut (release
  train, Reto 2026-11-12): the engine-review gate holds the deploy
  (``release_poll.deploy_review_blockers``, read by ``release_deploy``),
  scoped to the lands inside the release.
* :func:`poll` waits for the exit file; on exit 0 with a
  ``RELEASE_CUT: r<N> <sha>`` line it records the cut on the plan
  (:func:`precis.plans.release.record_cut`: ``meta.release`` on the plan
  root with the ``host`` and ``checkout`` it ran in, the real range on the
  plan's ``tests-green`` gate and loop, ``deploy r<N>`` on the approval
  ask), writes the shared ``release.round`` row through
  :func:`precis.plans.release.update_round` (``create=True`` — the
  script's own per-clone publish may have skipped on this host; the row
  carries ``pass: td<root>`` so ``release_poll`` can abandon it when
  the pass is dropped), and
  succeeds with r<N> and the sha in the
  summary and ``meta.release_cut``. The round write is best-effort:
  skipped when the cut's host is unknown, a failure a log warning plus a
  summary note — never a job failure, the cut happened. No
  ``RELEASE_CUT`` line (S already live,
  a release in flight elsewhere) or a non-zero exit fails the job with the
  log tail, and the refusal line is classified (:func:`classify_cut_refusal`,
  ``meta.cut_refusal = {class, line}``, the line in the summary): ``tip-only``
  (``scripts/round``'s "the next release cuts at origin/main", the asked sha
  below the tip), ``gated-behind`` ("origin/gated cannot fast-forward":
  a later --remote land moved gated past the recorded target; the re-target
  must land at or after gated, and a first cut drops a target that is a strict
  ancestor of gated up front), ``in-flight`` and ``holding`` are waits — the job fails
  attempt-free (no attempt burned, no retry-cap ask), the hold is recorded
  (``release_poll.note_cut_hold``) and the plan root marked
  ``meta.cut_retarget``, so the next tick's :func:`_cut_target` re-targets
  to origin/main's tip; the hour-long recut-hold ask is the backstop (keyed
  on the marker, quoting the refusal), and an ``in-flight`` first
  cut mints at most one job per ``IN_FLIGHT_PACE_MIN`` minutes. Any
  other refusal is ``unclassified``: the plan tick's retry cap files the ask,
  now quoting the line.

Before any launch :func:`submit` runs :func:`refresh_tooling`: fetch
origin/main and mirror ``scripts/release``, ``scripts/round`` and
``scripts/lib`` out of it into the checkout (those paths only; staged
against HEAD, and a file main deleted goes too), so a tooling fix that
landed on main reaches the cut even when the checkout's tree is older
(r263 refused four times on a stale ``scripts/round``). The staged state
is owed back (:func:`restore_tooling`, gr620480): it lasts exactly the
launch — a failed launch, a finished or a killed run puts the paths back
at HEAD — or a later ``merge --ff-only`` in the deploy checkout is
refused on the leftovers once main moves them again. The job records
``tooling: <sha9>`` (``meta.tooling``, the summary); a failed fetch, or
unstaged local edits to those paths, runs the working tree and the label
says so.

Before ``--cut-only`` launches, :func:`submit` clears the checkout's
stale local round when three preflight conditions hold: the clone's own
``round.json`` has exactly one ``deploying``/``deployed`` record (an int
``n``, a pinned sha, no pending cut, no runtime confirm), the shared
``release.round`` row proves that release rolled out elsewhere
(:func:`precis.plans.release.round_confirmed`: ``confirmed`` at ``n`` or
a later number — a hand-cut r<n+1> supersedes it too, a smaller number
never does), and no ``release_deploy`` job is live anywhere in the store
(:func:`precis.plans.release.live_deploy_job`). The supersede runs as a
detached ``supersede`` phase — ``scripts/round supersede r<n>`` with
``PRECIS_GRIPE_NOTE=0`` so the shared row is never rewritten; the verb's
own ancestor-of-prod gate is the proof, it touches no remote ref, and a
refusal fails the job with the log tail (no cut launched). On exit 0 the
handle swaps onto the usual ``--cut-only`` launch, the supersede note
carried into the cut's summary. An open ``cut``/``gating``/``fixing``/
``green`` local record the shared row does NOT dispute is not covered —
``supersede`` refuses open records, and the false-``RELEASE_CUT``
hazard they carry needs ``scripts/release``'s resume path, a later
slice (td575911).

A second preflight covers the dropped pass (gr612091): a local record
in an open state whose shared ``release.round`` row proves it dead —
a terminal state (``release_poll``'s ``abandoned`` stamp on a
won't-do'd pass), a higher ``n``, or a different branch, the same
dispute ``scripts/release``'s shared-row filter applies — reads as in
flight to ``scripts/release`` on a host that cannot read the row
itself, so the next pass's first cut rides its retry cap into a
showstopper ask. The job runs a detached ``abandon`` phase —
``scripts/round cut --abandon`` with ``PRECIS_GRIPE_NOTE=0`` — which
marks the record abandoned and deletes the origin branch while its
head is on main; the verb's own refusals (a deployment journal,
unforwarded commits) fail the job with the log tail, no cut launched.
On exit 0 the handle swaps onto the ``--cut-only`` launch exactly as
the supersede's does.

A third covers a branch the shared row never learned about (r262: the
pass dropped and its origin branch deleted, the clone's record left open):
an open local record whose ``release/r<n>`` is absent from origin (an
``ls-remote`` that succeeded — a failed read is not absence), with no
``deployment``/``deploy`` key and no live ``release_deploy`` job, launches
the same ``cut --abandon`` as an ``abandon_gone`` phase (its absent-branch
path only marks the record). The job records ``meta.preflight = "abandoned
r<n>"`` and the summary says so.

The cut never waits for CI: ``ci_verdict_poll`` records the verdict and the
``tests-green`` gate reads it. Refused while ``PRECIS_FACTORY=off``. The
helpers below (:func:`launch_script`, :func:`finish_run`) are shared with
``release_deploy``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import pr_class
from precis.plans import release as _release
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import release_poll as _rp
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "release_cut"

_DEFAULT_WALL_S = 30 * 60
_TAIL_CAP = 2000

#: The refusal prefix of a cut held as a wait (gr611217; today the
#: paced ``in-flight`` first cut) — not a failure: ``validate_submit``
#: refuses it before any job mints, and a ``submit`` that races into it
#: fails attempt-free.
HOLD_PREFIX = "holding: "

#: A first cut held as ``in-flight`` mints at most one cut job per this many
#: minutes (the plan tick would otherwise mint one per tick).
IN_FLIGHT_PACE_MIN = 10

#: Refusal classes (``meta.cut_refusal.class``). ``tip-only``,
#: ``in-flight`` and ``holding`` are waits the plan tick must not burn an
#: attempt on (and ask about): the job fails attempt-free and the hold is
#: recorded; ``unclassified`` keeps the retry cap, its ask quotes the line.
REFUSAL_TIP_ONLY = "tip-only"
REFUSAL_IN_FLIGHT = "in-flight"
REFUSAL_HOLDING = "holding"
REFUSAL_GATED_BEHIND = "gated-behind"
REFUSAL_UNCLASSIFIED = "unclassified"

#: ``scripts/round``'s pipelined-cut rule (``_cut_refuse``): the line names
#: origin/main's tip and the sha the cut asked for.
_TIP_ONLY_RE = re.compile(
    r"the next release cuts at origin/main(?: \((?P<tip>[0-9a-f]+)\))?"
    r"(?:, not (?P<asked>[0-9a-f]+))?"
)
#: ``scripts/release``'s "release in flight: <branch>" say-line (exit 0, no
#: ``RELEASE_CUT``), ``scripts/round``'s "release branch <b> exists on
#: origin" and the record-level "already records an open release".
_IN_FLIGHT_RE = re.compile(
    r"release in flight|exists on origin|already records an open release"
)
#: ``scripts/round``'s fast-forward guard on the cut sha: a later --remote
#: land moved origin/gated past the pass's recorded target.
_GATED_BEHIND_RE = re.compile(r"origin/gated cannot fast-forward")
_HOLDING_RE = re.compile(r"(?:^|\s)holding: ")
_REFUSED_RE = re.compile(r"cut refused|^scripts/(?:release|round): ")
_REFUSAL_LINE_CAP = 300

#: The class a refusal-shaped line takes, most specific first — applied
#: to ONE line at a time by :func:`classify_cut_refusal`.
_REFUSAL_CLASSES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (REFUSAL_TIP_ONLY, _TIP_ONLY_RE),
    (REFUSAL_GATED_BEHIND, _GATED_BEHIND_RE),
    (REFUSAL_IN_FLIGHT, _IN_FLIGHT_RE),
    (REFUSAL_HOLDING, _HOLDING_RE),
)


def classify_cut_refusal(log_text: str) -> dict[str, str]:
    """``{"class", "line"}`` for a failed ``--cut-only`` log — the refusal
    line the cut died on (``scripts/round: cut refused — ...``,
    ``scripts/release: ...``, ``release in flight: ...``) and its class
    (:data:`REFUSAL_TIP_ONLY`, :data:`REFUSAL_GATED_BEHIND`,
    :data:`REFUSAL_IN_FLIGHT`,
    :data:`REFUSAL_HOLDING`, else :data:`REFUSAL_UNCLASSIFIED` — whose line
    is the last refusal-shaped line, else the last non-empty line). The
    LAST refusal-shaped line wins (gr620480): a log can carry earlier
    refusal-shaped echoes of the cut's own retries — ``scripts/release``'s
    lost-race "exists on origin" before the retry dies on a different
    refusal — and classing the whole log by class priority would call the
    later refusal ``in-flight`` (attempt-free re-mints, and the hour-long
    ask quoting the wrong line)."""
    lines = [ln.strip() for ln in log_text.splitlines() if ln.strip()]
    for ln in reversed(lines):
        cls = next((c for c, pat in _REFUSAL_CLASSES if pat.search(ln)), None)
        if cls is not None:
            return {"class": cls, "line": ln[:_REFUSAL_LINE_CAP]}
        if _REFUSED_RE.search(ln):
            return {"class": REFUSAL_UNCLASSIFIED, "line": ln[:_REFUSAL_LINE_CAP]}
    return {
        "class": REFUSAL_UNCLASSIFIED,
        "line": lines[-1][:_REFUSAL_LINE_CAP] if lines else "",
    }


def _below_tip(line: str) -> bool:
    """Whether a tip-only refusal line shows the asked sha below the tip
    (the two shas differ, or the line does not say); the line naming the
    same sha twice is not a below-the-tip refusal and takes the retry cap."""
    m = _TIP_ONLY_RE.search(line)
    if m is None or not m.group("tip") or not m.group("asked"):
        return True
    return not (
        m.group("tip").startswith(m.group("asked"))
        or m.group("asked").startswith(m.group("tip"))
    )


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


def _launch(
    argv: list[str],
    *,
    cwd: Path,
    log_path: Path,
    exit_file: Path,
    extra_env: Mapping[str, str] | None = None,
) -> int:
    """Seam: start ``argv`` detached (run_slice's launcher, stdin from
    /dev/null, the worker's own env plus the factory git token as git's
    credential helper — the cut and deploy push ``release/r<N>``, ``gated``
    and ``prod`` from a service account's checkout; ``scripts/deploy``
    keeps the worker's own env for the rest). ``extra_env`` merges over
    that env (the stale-round supersede's ``PRECIS_GRIPE_NOTE=0``)."""
    env = _factory_git.git_auth_env()
    if extra_env:
        env.update(extra_env)
    return _rs._launch(
        argv,
        cwd=cwd,
        stdin=Path(os.devnull),
        log=log_path,
        exit_file=exit_file,
        env=env,
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


def launch_script(
    ctx: Any,
    name: str,
    args: list[str],
    *,
    script: str = "release",
    env: dict[str, str] | None = None,
) -> dict[str, Any] | None:
    """Run ``<root>/scripts/<script> <args>`` detached; the handle, or
    ``None`` after failing the job. ``env`` merges over the launcher's
    factory-git env."""
    try:
        root = _rs.slice_root()
        path = root / "scripts" / script
        if not path.exists():
            raise RuntimeError(f"no scripts/{script} in {root}")
        base = _run_dir() / f"{name}-{ctx.ref_id}"
        log_path = base.with_suffix(".log")
        exit_file = base.with_suffix(".exit")
        exit_file.unlink(missing_ok=True)
        pid = _launch(
            [str(path), *args],
            cwd=root,
            log_path=log_path,
            exit_file=exit_file,
            extra_env=env or None,
        )
    except Exception as exc:
        log.warning("%s: launch failed", name, exc_info=True)
        ctx.record_failure(f"{name}: launch failed: {exc}")
        return None
    ctx.append_chunk(
        "job_event",
        f"{name}: launched scripts/{script} {' '.join(args)} pid {pid} in {root}",
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
    if pgid > 1:
        try:
            _rs._signal_group(pgid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    _undo_handle_tooling(handle)


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


# ── the stale local round record (read straight from the file, no net) ──


def _round_json_path(root: Path) -> Path | None:
    """Seam (tests patch it): the checkout's per-clone round record
    ``<git-common-dir>/precis-round/round.json`` (``scripts/round``'s state
    dir), else ``None`` when git cannot say."""
    try:
        proc = _rs._git(
            ["rev-parse", "--path-format=absolute", "--git-common-dir"], root
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return Path(proc.stdout.strip()) / "precis-round" / "round.json"


def _stale_local_round(root: Path) -> int | None:
    """``n`` of the one local ``round.json`` record ``scripts/round
    supersede r<n>`` would take — ``deploying``/``deployed``, an int
    ``n``, no pending cut, a ``deployment`` dict pinning a non-empty
    ``sha``, no ``runtime_confirmed_at`` — else ``None``. A missing or
    unparsable file, a closed round, an open (``cut``/``gating``/
    ``fixing``/``green`` — ``supersede`` refuses them) or terminal
    record, and several candidates all answer ``None``."""
    path = _round_json_path(root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("open"):
        return None
    candidates = []
    for rel in data.get("releases") or []:
        if not isinstance(rel, dict):
            continue
        n = rel.get("n")
        if rel.get("state") not in ("deploying", "deployed") or type(n) is not int:
            continue
        if rel.get("cut_pending"):
            continue
        dep = rel.get("deployment")
        if (
            not isinstance(dep, dict)
            or not isinstance(dep.get("sha"), str)
            or not dep["sha"]
            or dep.get("runtime_confirmed_at")
        ):
            continue
        candidates.append(n)
    return candidates[0] if len(candidates) == 1 else None


def _stale_supersede_n(ctx: Any) -> int | None:
    """``n`` of the checkout's stale local record the cut supersedes
    first — the three preflight conditions of the module docstring — or
    ``None``. A read failure is a warning and "no": the cut launches as
    today and the script's own ``stale_why`` gate decides."""
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return None  # launch_script below reports the same failure
    n = _stale_local_round(root)
    if n is None:
        return None
    try:
        confirmed = _release.round_confirmed(_release.read_round(ctx.store), n)
        quiet = not _release.live_deploy_job(ctx.store)
    except Exception:
        log.warning("%s: stale-round preflight read failed", NAME, exc_info=True)
        return None
    return n if confirmed and quiet else None


def _local_open_round(root: Path) -> dict[str, Any] | None:
    """The checkout's one local ``round.json`` record still in an open
    state — ``cut``/``gating``/``fixing``/``green``, an int ``n``, no
    pending cut (a ``cut_pending`` record is live work this clone owns;
    ``scripts/release``'s adoption path, not a corpse) — else ``None``.
    A missing or unparsable file, a closed round, terminal or
    deploying records, and several open records (an invariant
    violation — never judged) all answer ``None``."""
    path = _round_json_path(root)
    if path is None:
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("open"):
        return None
    candidates = [
        rel
        for rel in data.get("releases") or []
        if isinstance(rel, dict)
        and rel.get("state") in ("cut", "gating", "fixing", "green")
        and type(rel.get("n")) is int
        and not rel.get("cut_pending")
    ]
    return candidates[0] if len(candidates) == 1 else None


def _dead_open_round_n(ctx: Any) -> int | None:
    """``n`` of the checkout's open record the shared ``release.round``
    row proves dead (gr612091), else ``None``: ``release_poll``'s
    ``abandoned`` stamp — or any terminal state, a higher ``n``, or a
    different branch — disputes the local record exactly as
    ``scripts/release``'s shared-row filter does, but on a host that
    cannot read the row the record and its origin branch still read in
    flight. ``round cut --abandon`` retires both; its own refusals
    (deploy journal, unforwarded commits) fail closed. A read failure
    is a warning and "no": the cut launches as today."""
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return None  # launch_script below reports the same failure
    rel = _local_open_round(root)
    if rel is None:
        return None
    try:
        shared = _release.read_round(ctx.store)
    except Exception:
        log.warning("%s: dead-round preflight read failed", NAME, exc_info=True)
        return None
    if not isinstance(shared, dict):
        return None
    n, db_n = rel.get("n"), shared.get("n")
    if type(n) is not int or type(db_n) is not int:
        return None
    if db_n > n or (db_n == n and _release.is_terminal(shared)):
        return int(n)
    db_branch = str(shared.get("branch") or "")
    if db_n == n and db_branch and db_branch != rel.get("branch"):
        return int(n)
    return None


def _origin_branch_gone(root: Path, branch: str) -> bool:
    """Whether the remote listing read origin and ``refs/heads/<branch>``
    is absent. A failed or timed-out read is not absence."""
    try:
        proc = _tooling_git(
            root, ["ls-remote", "--heads", "origin", f"refs/heads/{branch}"]
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and not proc.stdout.strip()


def _gone_open_round_n(ctx: Any) -> int | None:
    """``n`` of the checkout's open record whose ``release/r<n>`` branch
    is gone from origin and which never deployed, else ``None``: the pass
    was dropped and its branch deleted elsewhere, yet the clone's
    ``round.json`` still reads it in flight, so every cut refuses until
    ``round cut --abandon`` marks it (r262 needed an ssh session). All of:
    one open (non-pending) local record, no ``deployment``/``deploy`` key
    (``_cut_abandon`` refuses a journal anyway), no live ``release_deploy``
    job in the store, and a listing that succeeded and found no such
    branch. Any read failure is a warning and "no"."""
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return None  # launch_script below reports the same failure
    rel = _local_open_round(root)
    if rel is None or rel.get("deployment") or rel.get("deploy"):
        return None
    branch = str(rel.get("branch") or "")
    if not branch:
        return None
    try:
        if _release.live_deploy_job(ctx.store):
            return None
    except Exception:
        log.warning("%s: gone-round preflight read failed", NAME, exc_info=True)
        return None
    return int(rel["n"]) if _origin_branch_gone(root, branch) else None


# ── the release tooling a launch runs ──────────────────────────────

#: The paths a ``scripts/release``/``scripts/round`` launch executes and
#: sources (``scripts/lib`` for the runpy-loaded helpers).
_TOOLING_PATHS = ("scripts/release", "scripts/round", "scripts/lib")

_TOOLING_GIT_S = 120


def _tooling_git(root: Path, args: list[str]) -> subprocess.CompletedProcess[str]:
    """``git -C root <args>`` under the factory git credentials; never
    raises on a non-zero exit."""
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=_TOOLING_GIT_S,
        env=_factory_git.git_auth_env(),
    )


def _tooling_paths_diff(root: Path, filt: str, sha: str) -> list[str]:
    """Tooling paths whose state differs between HEAD and ``sha`` under
    ``--diff-filter=<filt>``: ``A`` is what sha has and HEAD does not —
    what a refresh stages that :func:`restore_tooling` must delete;
    ``D`` is what HEAD has and sha dropped — what the refresh mirrors
    away. ``--no-renames`` keeps a rename legible as delete+add."""
    out = _tooling_git(
        root,
        [
            "diff",
            "--name-only",
            "--no-renames",
            "-z",
            f"--diff-filter={filt}",
            "HEAD",
            sha,
            "--",
            *_TOOLING_PATHS,
        ],
    )
    return out.stdout.split("\0")[:-1] if out.returncode == 0 else []


def restore_tooling(root: Path, added: list[str]) -> None:
    """Undo a :func:`refresh_tooling` apply (gr620480): index and worktree
    back to HEAD for the tooling paths — ``checkout HEAD --`` restores
    what HEAD tracks, ``added`` drops what it does not — so the deploy
    checkout's next ``merge --ff-only`` is not refused on the staged
    leftovers. The saved ``added`` list is filtered against the HEAD at
    restore time, not the HEAD the refresh saw: a HEAD that advanced in
    between may now track one of those paths (the staging committed, or a
    fast-forward carried it), and a blind ``git rm`` stages the deletion
    of a legitimate file. Paths carrying unstaged worktree edits are left
    alone: the refresh's own never-overwrite rule."""
    if _tooling_git(root, ["diff", "--quiet", "--", *_TOOLING_PATHS]).returncode:
        return
    _tooling_git(root, ["checkout", "HEAD", "--", *_TOOLING_PATHS])
    files = [f for f in added if f]
    if files:
        out = _tooling_git(
            root, ["ls-tree", "-r", "-z", "--name-only", "HEAD", "--", *files]
        )
        if out.returncode == 0:
            tracked = set(out.stdout.split("\0"))
            files = [f for f in files if f not in tracked]
    if files:
        _tooling_git(root, ["rm", "-q", "-f", "--ignore-unmatch", "--", *files])


def _undo_tooling(added: Any, *, root: Any = None) -> None:
    """Restore the tooling paths after a launch — a finished, failed or
    killed run owes the checkout its HEAD state back (gr620480). Best
    effort, never the job's verdict."""
    if not isinstance(added, list):
        return
    try:
        restore_tooling(
            Path(str(root)) if root else _rs.slice_root(), [str(p) for p in added]
        )
    except Exception:
        log.warning("%s: tooling restore failed", NAME, exc_info=True)


def _undo_handle_tooling(handle: Any) -> None:
    """``_undo_tooling`` off a launch handle's ``tooling_added``/``root``."""
    if isinstance(handle, dict):
        _undo_tooling(handle.get("tooling_added"), root=handle.get("root"))


def refresh_tooling(root: Path) -> tuple[str, list[str] | None]:
    """Make the checkout's ``scripts/release``, ``scripts/round`` and
    ``scripts/lib`` match origin/main, and answer ``(label, added)`` —
    the tooling label the job records (the 9-char origin/main sha, else
    ``working tree (<why>)``) and the paths the refresh staged that HEAD
    does not track, which :func:`restore_tooling` deletes on top of its
    ``checkout HEAD --`` after the launch. ``added`` is ``None`` when the
    tree was left alone: nothing is owed.

    The cut runs the checkout's own working tree
    (:func:`run_slice.slice_root`), which can be older than origin/main —
    a fix to ``scripts/round`` then never reaches the cut (r263 refused
    four times on a tip-only rule the tree predated). The refresh is
    ``git fetch origin main`` plus ``git checkout origin/main -- <paths>``
    for those paths only, then ``git rm`` of what HEAD tracks and sha
    dropped — a mirror, not an overlay (gr620480: a deleted lib file must
    go too). HEAD, the branch and every other file stay as they are, and
    the paths read as staged against HEAD, which also protects the running
    cut from a concurrent fast-forward. It runs in place, not from an
    export of origin/main: both scripts resolve their ``ROOT`` from their
    own location, and the ``PRECIS_ROUND_ROOT`` override that would
    redirect an export also switches off their shared-row reads and writes
    ("a foreign checkout"), so only the in-place copy keeps the cut's
    round state, ``.release-owner`` marker and shared-row sync as they are
    today. The staged state must not outlive the launch — once main moves
    those paths again a later ``merge --ff-only`` is refused on the
    leftovers — so the job restores them when the run ends. Every failure
    — fetch, an unreadable ref, unstaged local edits to those paths (never
    overwritten), the checkout — falls back to the working tree and names
    why. A failure after the checkout ran still owes the undo: the
    handler restores the partial apply itself, and answers the ``added``
    list when even that fails — ``None`` would strand the staging."""
    staged: list[str] | None = None
    try:
        fetched = _tooling_git(
            root,
            ["fetch", "-q", "origin", "+refs/heads/main:refs/remotes/origin/main"],
        )
        if fetched.returncode != 0:
            why = (fetched.stderr.strip().splitlines() or ["git fetch failed"])[-1]
            return f"working tree (fetch of origin/main failed: {why[:120]})", None
        head = _tooling_git(
            root, ["rev-parse", "--verify", "-q", "refs/remotes/origin/main^{commit}"]
        )
        sha = head.stdout.strip()
        if head.returncode != 0 or not sha:
            return "working tree (origin/main unreadable after the fetch)", None
        if (
            _tooling_git(
                root, ["diff", "--quiet", sha, "--", *_TOOLING_PATHS]
            ).returncode
            == 0
        ):
            # Already matches: touch nothing — but name what restore must
            # still drop (a cut that never got to restore left its
            # staging behind, and this run's restore cleans it too).
            return sha[:9], _tooling_paths_diff(root, "A", sha)
        if _tooling_git(root, ["diff", "--quiet", "--", *_TOOLING_PATHS]).returncode:
            return (
                "working tree (tooling paths carry local edits; not refreshed)",
                None,
            )
        added = _tooling_paths_diff(root, "A", sha)
        staged = added  # from here the checkout may leave a partial apply
        out = _tooling_git(root, ["checkout", sha, "--", *_TOOLING_PATHS])
        if out.returncode != 0:
            why = (out.stderr.strip().splitlines() or ["git checkout failed"])[-1]
            restore_tooling(root, added)  # drop any partial writes
            return f"working tree (refresh failed: {why[:120]})", None
        dropped = _tooling_paths_diff(root, "D", sha)
        if dropped:
            _tooling_git(root, ["rm", "-q", "-f", "--ignore-unmatch", "--", *dropped])
        return sha[:9], added
    except (OSError, subprocess.SubprocessError) as exc:
        label = f"working tree ({type(exc).__name__}: {str(exc)[:120]})"
        if staged is None:
            return label, None
        try:
            restore_tooling(root, staged)
        except Exception:
            log.warning("%s: refresh undo failed", NAME, exc_info=True)
            return label, staged
        return label, None


def _tooling_label(ctx: Any) -> tuple[str, list[str] | None]:
    """Refresh the tooling for this job's launches and record it on the
    job (``meta.tooling``, a ``job_event``): ``(label, added)`` — the
    ``added`` :func:`restore_tooling` drops after the run, ``None`` for
    ``working tree (...)`` (the checkout cannot be resolved or refreshed;
    nothing applied, nothing owed)."""
    try:
        label, added = refresh_tooling(_rs.slice_root())
    except RuntimeError as exc:
        label, added = f"working tree ({str(exc)[:120]})", None
    except Exception as exc:  # never the cut's verdict: run the tree as is
        log.warning("%s: tooling refresh raised", NAME, exc_info=True)
        label, added = f"working tree ({type(exc).__name__})", None
    ctx.set_meta(tooling=label)
    ctx.append_chunk("job_event", f"{NAME}: tooling: {label}")
    return label, added


# ── release_cut ────────────────────────────────────────────────────


def _retarget_marker(store: Any, todo_id: Any) -> dict[str, Any] | None:
    """The plan root's ``meta.cut_retarget`` (see :func:`_note_refusal`):
    ``{class, line, pace_until}`` of the refusal that held the first cut,
    else ``None``. A marker of its own, not ``meta.cut_hold``: the hold's
    ``since`` must survive the gate clearing so the hour-long ask measures
    the whole hold, and the submit after the validate must still see the
    re-target."""
    if not isinstance(todo_id, int):
        return None
    ref = store.get_ref(kind="todo", id=_release.plan_root(store, todo_id))
    mark = (ref.meta or {}).get("cut_retarget") if ref is not None else None
    return mark if isinstance(mark, dict) and mark.get("class") else None


def _now() -> datetime:
    """Seam (tests patch it): the pacing clock, UTC."""
    return datetime.now(UTC)


def _paced(mark: dict[str, Any]) -> str | None:
    """The :data:`HOLD_PREFIX` refusal while an ``in-flight`` first cut is
    inside its pacing window (:data:`IN_FLIGHT_PACE_MIN` after the refusal
    that marked it), else ``None``: validate refuses, so no job mints."""
    until = mark.get("pace_until")
    if not until:
        return None
    try:
        t = datetime.strptime(str(until), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except ValueError:
        return None
    if _now() >= t:
        return None
    return (
        f"{HOLD_PREFIX}a release is in flight ({mark.get('line')}); "
        f"the next cut try is after {until}"
    )


def _clear_retarget(store: Any, todo_id: Any) -> None:
    """A cut succeeded: drop the first cut's hold and re-target marker,
    each a write only when one is there. A failure is a log line."""
    try:
        if _retarget_marker(store, todo_id) is not None:
            store.stamp_ref_meta(
                _release.plan_root(store, todo_id), {"cut_retarget": None}
            )
        _rp.note_cut_hold(store, todo_id, None)
    except Exception:
        log.warning("%s: retarget clear failed", NAME, exc_info=True)


def _vs_gated(repo: str, sha: str) -> str:
    """How ``sha`` sits against origin/gated, from GitHub's
    ``compare/gated...<sha>`` status: ``identical``/``ahead`` (gated is
    ``sha`` or its ancestor: the cut can fast-forward it), ``behind`` (``sha``
    is a strict ancestor of gated), ``diverged``, else ``unknown`` (a reply
    without a status — the script's own guard decides). Raises
    :class:`precis.plans.pr_class.GitHubUnreadable`."""
    url = f"{_rp._API.format(repo=repo)}/compare/gated...{sha}"
    data = pr_class._get_json(url, {})
    status = str(data.get("status") or "") if isinstance(data, dict) else ""
    if status in ("identical", "ahead", "behind", "diverged"):
        return status
    return "unknown"


def _stale_vs_gated(store: Any, todo_id: Any) -> str:
    """The recorded first-cut target's compare/gated verdict —
    ``behind`` drops the target for origin/main's tip — else
    ``""``: no recorded target, no repo or an unreadable GitHub (fail
    open: the script's own fast-forward guard still refuses it). The
    verdict is cached on the target (``meta.release_target.vs_gated``):
    the sha is fixed at mint and origin/gated only moves forward, so a
    ``behind`` never un-happens and a stale ``ahead`` degenerates to one
    doomed launch whose ``gated-behind`` refusal re-targets anyway —
    re-checking every first-cut validate tick would only spend the
    shared unauthenticated api.github.com budget (gr620480)."""
    if not isinstance(todo_id, int):
        return ""
    target = _release.release_target_of(store, todo_id)
    if target is None:
        return ""
    ref = store.get_ref(kind="todo", id=_release.plan_root(store, todo_id))
    raw = (ref.meta or {}).get("release_target") if ref is not None else None
    if isinstance(raw, dict) and raw.get("vs_gated"):
        return str(raw["vs_gated"])
    repo = pr_class.resolve_repo(store, None)
    if not repo:
        return ""
    try:
        verdict = _vs_gated(repo, target["sha"])
    except pr_class.GitHubUnreadable:
        return ""
    try:
        store.stamp_ref_meta(
            _release.plan_root(store, todo_id),
            {
                "release_target": {
                    **(raw if isinstance(raw, dict) else target),
                    "vs_gated": verdict,
                }
            },
        )
    except Exception:
        log.warning("%s: vs_gated stamp failed", NAME, exc_info=True)
    return verdict


def _cut_target(store: Any, todo_id: Any) -> tuple[str | None, str | None]:
    """``(sha, refusal)`` — the sha ``scripts/release --cut-only --sha``
    cuts, ``None`` for a tip cut, or ``(None, refusal)`` while an
    ``in-flight`` first cut is paced (:func:`_paced`).

    No review holds a cut (release train, Reto 2026-11-12): the gate
    holds the release's deploy instead (``release_poll.
    deploy_review_blockers``). A first cut uses the pass's recorded
    ``meta.release_target.sha`` — the origin/main tip the poll pinned.
    Every other case cuts origin/main's tip: a plan already cut (the
    ``meta.release`` exists; ``scripts/round`` cuts a pipelined release
    only at origin/main, gr611217), a first cut a refusal held
    (``meta.cut_retarget``, written by :func:`_note_refusal`: the
    recorded target sat below origin/main or origin/gated, or a release
    was in flight), a recorded target that is a strict ancestor of
    origin/gated (a later --remote land moved gated past it — dropped up
    front so ``scripts/round``'s fast-forward guard never has to refuse
    it), and a pass without a recorded target (a hand mint). The
    compare/gated read runs once per target and caches on it
    (:func:`_stale_vs_gated`) — a per-tick call would spend the shared
    unauthenticated api.github.com budget (gr620480)."""
    if not isinstance(todo_id, int) or _release.release_of(store, todo_id) is not None:
        return None, None
    mark = _retarget_marker(store, todo_id)
    if mark is not None:
        return None, _paced(mark)
    if _stale_vs_gated(store, todo_id) == "behind":
        return None, None
    target = _release.release_target_of(store, todo_id)
    return (str(target["sha"]) if target is not None else None), None


def _handle_tooling(
    handle: dict[str, Any] | None, *, tooling: str, added: list[str] | None
) -> dict[str, Any] | None:
    """Carry the tooling state onto a launched handle — ``tooling`` and
    ``tooling_added`` for the restore when the run ends. A failed launch
    never gets polled: it owes the checkout the undo now."""
    if not isinstance(handle, dict):
        _undo_tooling(added)
        return None
    handle["tooling"] = tooling
    if added is not None:
        handle["tooling_added"] = added
    return handle


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    todo_id = params.get("todo_id")
    reason = factory_refusal(NAME) or todo_refusal(store, NAME, todo_id, required=False)
    if reason is not None or store is None or not isinstance(todo_id, int):
        return reason
    # A re-cut runs where the plan's first cut ran: the .release-owner marker
    # is in that checkout, so --cut-only elsewhere cuts nothing (td543855).
    # The first cut has no meta.release yet and is not pinned.
    pin = _release.pin_refusal(
        _release.release_of(store, todo_id),
        host=str(params.get("target_node") or "").strip() or None,
    )
    if pin is not None:
        return f"{NAME}: {pin}"
    _sha, refusal = _cut_target(store, todo_id)
    return f"{NAME}: {refusal}" if refusal is not None else None


#: The env marker ``scripts/release`` and ``scripts/round cut`` require
#: before they cut (gr675506, Reto 2026-11-14: exactly one cutter, the
#: factory release pass). Its value names the pass.
RELEASE_PASS_ENV = "PRECIS_RELEASE_PASS"


def pass_env(ctx: Any) -> dict[str, str]:
    """``{PRECIS_RELEASE_PASS: td<pass root>}`` for the cut launch — the
    same ``td<root>`` handle the round row's ``pass`` carries — else the
    job's own ``jo<id>`` when the job names no todo. Never empty: an empty
    marker would read as a hand session."""
    todo_id = ((ctx.meta or {}).get("params") or {}).get("todo_id")
    value = ""
    if isinstance(todo_id, int) and not isinstance(todo_id, bool) and ctx.store:
        try:
            value = f"td{_release.plan_root(ctx.store, todo_id)}"
        except Exception:
            log.warning("%s: plan root lookup failed", NAME, exc_info=True)
    return {RELEASE_PASS_ENV: value or f"jo{ctx.ref_id}"}


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason = factory_refusal(NAME) or todo_refusal(
        ctx.store, NAME, params.get("todo_id"), required=False
    )
    if reason is not None:
        ctx.record_failure(reason)
        return None
    sha, refusal = _cut_target(ctx.store, params.get("todo_id"))
    if refusal is not None:
        if refusal.startswith(HOLD_PREFIX):
            # gr611217: the wait began between validate and submit — it
            # burns no plan attempt.
            ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: "cut-hold"})
        ctx.record_failure(f"{NAME}: {refusal}")
        return None
    argv = ["--cut-only"] + (["--sha", sha] if sha else [])
    tooling, added = _tooling_label(ctx)
    if ctx.store is not None:
        n = _stale_supersede_n(ctx)
        if n is not None:
            # A launched phase, not a synchronous call: the verb needs
            # the factory git env for its ls-remote/fetch, and a
            # restarted worker still reads the verdict from the log and
            # exit file. PRECIS_GRIPE_NOTE=0 skips its release.round
            # publish — the other clone's confirmed row stays as it was.
            handle = launch_script(
                ctx,
                f"{NAME}_supersede",
                ["supersede", f"r{n}"],
                script="round",
                env={"PRECIS_GRIPE_NOTE": "0"},
            )
            if isinstance(handle, dict):
                handle.update(phase="supersede", n=n, cut_argv=argv)
            return _handle_tooling(handle, tooling=tooling, added=added)
        dead = _dead_open_round_n(ctx)
        if dead is not None:
            # gr612091: the shared row already retired this release —
            # `cut --abandon` marks the record and deletes the origin
            # branch (its own guards refuse a deploy journal and
            # unforwarded commits). Same detached-phase shape as the
            # supersede: PRECIS_GRIPE_NOTE=0 keeps the shared row as it
            # is, the exit file decides.
            handle = launch_script(
                ctx,
                f"{NAME}_abandon",
                ["cut", "--abandon"],
                script="round",
                env={"PRECIS_GRIPE_NOTE": "0"},
            )
            if isinstance(handle, dict):
                handle.update(phase="abandon", n=dead, cut_argv=argv)
            return _handle_tooling(handle, tooling=tooling, added=added)
        gone = _gone_open_round_n(ctx)
        if gone is not None:
            # The record's origin branch is gone and it never deployed
            # (a dropped pass deleted it): the same detached `cut
            # --abandon`, from origin/main's refreshed tooling; its
            # absent-branch path only marks the record.
            handle = launch_script(
                ctx,
                f"{NAME}_abandon",
                ["cut", "--abandon"],
                script="round",
                env={"PRECIS_GRIPE_NOTE": "0"},
            )
            if isinstance(handle, dict):
                handle.update(phase="abandon_gone", n=gone, cut_argv=argv)
            return _handle_tooling(handle, tooling=tooling, added=added)
    handle = launch_script(ctx, NAME, argv, env=pass_env(ctx))
    return _handle_tooling(handle, tooling=tooling, added=added)


#: The retire phases' printed verb, note and meta key — ``supersede``
#: for the stale deploying/deployed record (td575911), ``abandon`` for
#: the dead open one (gr612091).
_PHASES: dict[str, tuple[str, str, str]] = {
    "supersede": (
        "supersede r{n}",
        "superseded stale local r{n}, prod moved past it",
        "stale_round_superseded",
    ),
    "abandon": (
        "cut --abandon",
        "abandoned dead local r{n}: the shared round row retired it",
        "dead_round_abandoned",
    ),
    "abandon_gone": (
        "cut --abandon",
        "abandoned r{n} (preflight): its branch is gone from origin and it "
        "never deployed",
        "dead_round_abandoned",
    ),
}


def _phase_done(ctx: Any, handle: dict[str, Any]) -> bool:
    """A retire phase's verdict: a refusal fails the job with the log
    tail and cuts nothing; exit 0 swaps the handle in place onto the
    ``--cut-only`` launch (``release_confirm._confirmed``'s pattern: a
    fresh handle persisted as ``compute_handle`` with a fresh deadline,
    the note carried into the cut's summary)."""
    verb_t, note_t, meta_key = _PHASES[str(handle.get("phase"))]
    done = finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    n = handle.get("n")
    if exit_code != 0:
        _undo_handle_tooling(handle)
        fail_with_tail(
            ctx,
            f"{NAME}: scripts/round {verb_t.format(n=n)} exit "
            f"{exit_code}; no cut launched",
            log_text,
        )
        return True
    note = note_t.format(n=n)
    tail = log_text.strip()[-300:]
    fresh = launch_script(
        ctx,
        NAME,
        list(handle.get("cut_argv") or ["--cut-only"]),
        env=pass_env(ctx),
    )
    ctx.append_chunk("job_event", f"{NAME}: {note}" + (f" — {tail}" if tail else ""))
    ctx.set_meta(**{meta_key: {"n": n}})
    if handle.get("phase") == "abandon_gone":
        ctx.set_meta(preflight=f"abandoned r{n}")
    if fresh is None:
        _undo_handle_tooling(handle)
        return True  # the launch failure is recorded; no cut
    tooling = handle.get("tooling")
    added = handle.get("tooling_added")
    handle.clear()
    handle.update(fresh)
    handle["notes"] = [note]
    if tooling:
        handle["tooling"] = tooling
    if added is not None:
        handle["tooling_added"] = added
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + wall_seconds(),
    )
    return False


def _note_refusal(ctx: Any, refusal: dict[str, str]) -> None:
    """Record a failed cut's refusal on the job: ``meta.cut_refusal`` and
    a summary line always. A wait — ``tip-only`` (the asked sha sits below
    origin/main), ``in-flight``, ``holding`` — also fails attempt-free
    (``plan_dispatch._no_attempt``: no attempt burned, no retry-cap ask)
    and records the hold on the plan root (:func:`release_poll.note_cut_hold`):
    the next tick's :func:`_cut_target` then re-targets to origin/main's
    tip (an ``in-flight`` one after its pacing window). ``release_poll``'s
    hour-long recut-hold ask is the backstop (it keys on this marker).
    ``unclassified`` changes nothing about the retry."""
    cls, line = refusal["class"], refusal["line"]
    ctx.set_meta(cut_refusal=dict(refusal))
    if line:
        ctx.append_chunk("job_summary", f"release_cut: cut refused ({cls}): {line}")
    wait = cls in (REFUSAL_IN_FLIGHT, REFUSAL_HOLDING, REFUSAL_GATED_BEHIND) or (
        cls == REFUSAL_TIP_ONLY and _below_tip(line)
    )
    if not wait:
        return
    ctx.set_meta(**{_rs.ATTEMPT_FREE_KEY: f"cut-{cls}"})
    todo_id = ((ctx.meta or {}).get("params") or {}).get("todo_id")
    if isinstance(todo_id, int) and ctx.store is not None:
        _rp.note_cut_hold(ctx.store, todo_id, [line])
        pace = (
            (_now() + timedelta(minutes=IN_FLIGHT_PACE_MIN)).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
            if cls == REFUSAL_IN_FLIGHT
            else None
        )
        try:
            ctx.store.stamp_ref_meta(
                _release.plan_root(ctx.store, todo_id),
                {"cut_retarget": {"class": cls, "line": line, "pace_until": pace}},
            )
        except Exception:
            log.warning("%s: cut_retarget stamp failed", NAME, exc_info=True)


def poll(ctx: Any, handle: Any) -> bool:
    if isinstance(handle, dict) and handle.get("phase") in _PHASES:
        return _phase_done(ctx, handle)
    done = finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    _undo_handle_tooling(handle)
    exit_code, log_text = done
    cut = _release.parse_cut(log_text)
    if exit_code != 0 or cut is None:
        why = (
            f"exit {exit_code}"
            if exit_code != 0
            else "exit 0 but no RELEASE_CUT line (S already live, or a release "
            "is in flight elsewhere — nothing cut for this plan)"
        )
        reason = f"{NAME}: scripts/release --cut-only {why}"
        refusal = classify_cut_refusal(log_text)
        if refusal["line"]:
            reason += f"\nrefusal ({refusal['class']}): {refusal['line']}"
        _note_refusal(ctx, refusal)
        fail_with_tail(ctx, reason, log_text)
        return True
    rn, sha, rng = cut
    params = (ctx.meta or {}).get("params") or {}
    todo_id = params.get("todo_id")
    checkout = str(handle.get("root") or ".")
    repo = origin_repo(Path(checkout))
    host = run_host(params)
    notes: list[str] = list(handle.get("notes") or [])
    if handle.get("tooling"):
        notes.append(f"tooling: {handle['tooling']}")
    if isinstance(todo_id, int):
        now = datetime.now(UTC)
        try:
            notes += _release.record_cut(
                ctx.store,
                todo_id,
                rn,
                sha,
                rng,
                repo=repo,
                host=host,
                checkout=checkout,
                now=now,
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
        if host is None:
            notes.append("release.round not written: the cut's host is unknown")
        else:
            try:
                notes += _release.update_round(
                    ctx.store,
                    {
                        "n": int(rn[1:]),
                        "branch": f"release/{rn}",
                        "base": sha,
                        "state": "cut",
                        "owner": {"host": host, "checkout": checkout},
                        "pass": f"td{_release.plan_root(ctx.store, todo_id)}",
                    },
                    create=True,
                    now=now,
                )
            except Exception as exc:
                # The cut happened; like record_prod_head this write is
                # best-effort — a warning plus a summary note, not a failure.
                log.warning("%s: release.round write failed", NAME, exc_info=True)
                notes.append(f"release.round not written: {type(exc).__name__}")
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
    if isinstance(todo_id, int):
        _clear_retarget(ctx.store, todo_id)
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
    "pass_env",
    "poll",
    "run_host",
    "submit",
    "validate_submit",
    "wall_seconds",
]
