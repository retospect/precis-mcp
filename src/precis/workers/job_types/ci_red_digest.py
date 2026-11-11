"""``ci_red_digest`` — the red run's digest as a ``brief:`` for the fixer.

The ``digest`` node of the ``release-fix`` loop body
(:func:`precis.plans.templates.mint_release_fix`, td573431). Mechanical,
no LLM; a detached submit/poll pair under ``ssh_node``, the same launcher
as ``release_cut`` — the script runs in the checkout the cut ran in,
pinned to that host the same way.

* Normal mode (``params.todo_id``, the digest node): the newest
  ``release.ci_verdicts`` row whose sha prefix-matches the plan's cut and
  whose verdict is ``red`` with a run id names the check.yml run
  (:func:`precis.plans.gates.read_ci_verdicts`); none fails the job and
  the plan tick's retry cap files the ask. ``scripts/ci-failed <run_id>``
  digests the failed jobs; on exit 0 the job posts one ``brief:``
  comment — the header line, the digest through
  :func:`precis.utils.secret_scan.mask_secrets` capped at
  :data:`_DIGEST_CAP`, and the fix-forward rule — on every live
  ``run_slice`` todo ``blocked-by`` the digest node (none found fails the
  job). The fixer's slice session reads it through
  ``run_slice._brief_parts``. A run still in progress prints the ``no
  failed-job log lines yet`` marker instead: the job parks its handle on
  ``not_before`` (:func:`_park_for_run`, land_slice's deferral —
  ``ssh_node`` wires no ``ctx.defer``) and re-digests, so a brief always
  reflects a finished run; past :data:`_PARK_CAP` waits the job fails
  "still in progress" and the tick re-dispatches within the node's
  retries. A *completed* run it can digest nothing of — the marker
  without ``yet``: every job cancelled, skipped or infra-killed, or the
  run never started one (a startup failure, ``jobs=[]``) — is an
  INFRASTRUCTURE red, not a test red (gr611682, the coordinator ruling):
  the fix node never closes ``no-op`` on it — closing it let the loop
  re-cut and republish the same sha, so the same jobless run re-read as
  red every iteration until the cap. :func:`_infra_done` re-reads the
  run live instead: green now → record the green and close the fix
  nodes moot; still red → fire ``gh run rerun`` once per run
  (``--failed`` only when failed jobs exist — it cannot re-fire jobs
  that never concluded) and park on the rerun, so the loop waits
  without burning an iteration; a run that stays infra past
  :data:`_PARK_CAP` fails the job so the tick retries, then files the
  coordinator ask (:data:`COORDINATOR_ASK_KEY`). A digest whose
  ``== job ==`` sections are all blind (the
  ``(no signal lines`` tail — the shared stream carrying only step
  headers, gr587536) is never posted: the job first re-reads the failed
  jobs' own logs through ``gh api actions/jobs/<id>/logs``
  (:func:`_jobs_logs_rescue`) and appends the failure lines it finds —
  reserved the tail room under :data:`_DIGEST_CAP` so a long blind dump
  cannot push them off the brief; still blind, it posts a ``blocked:``
  note on the plan and each fix node and fails instead, so no session
  is claimed on an empty brief — the tick's retry cap files the ask.
  **The red-test waiver** (:mod:`precis.plans.red_waiver`, setting
  ``release.red_waiver``, Reto 2026-11-10) is tried on a real TEST red
  before the brief is posted — never on an infra red, ruled above: a
  complete run (completed — an in-progress one parks the digest via
  :func:`_park_for_run` and re-reads; every job success/failure/skipped;
  no failed-first or collection error, ids matching the pytest counts), 1-2 parsed failing ids, none
  and no cut-range path on the hard-stop lists, an attributable culprit
  commit, no id on a still-unfixed waiver gripe. Granted, the job files the
  ``broken-build`` gripe, records the cut's verdict as waived
  (green for the gate), closes the iteration's fix/re-cut/poll nodes
  ``waived: gr<N>`` and succeeds — no brief, no fix session, no re-cut;
  every step of the decision lands in the job summary. Refused (the
  summary names the reason), the loop runs as before.
* Probe mode (``params.probe`` + ``params.run_id``; ``todo_id``
  optional): ``scripts/ci-failed <run_id> --probe`` prints one
  ``probe: status=<code> accepted-permissions=<value|absent> run=<id>``
  line — what the factory's GitHub credential (the ``gh`` env the shared
  launcher installs, ``release_cut._launch``) may read on the run's logs
  endpoint. The line lands in the job summary and ``meta.ci_probe``.
  Submitted by hand (the coordinator) against any recent run id, it
  names the permission line the token needs (td573240).

A non-zero exit in either mode fails the job with the log tail. Refused
while ``PRECIS_FACTORY=off``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import red_waiver as _waiver
from precis.plans import release as _release
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
from precis.workers.job_types import ci_verdict_poll as _cvp
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "ci_red_digest"

#: Digesting a finished run's failed-job logs is a short read; the wall
#: only covers a stuck ``gh``.
_DEFAULT_WALL_S = 10 * 60
#: The brief's digest cap — a shard dump still lands as one comment.
_DIGEST_CAP = 12000

_RUN_ID_RE = re.compile(r"[0-9]+")
_PROBE_RE = re.compile(
    r"^probe: status=(\S+) accepted-permissions=(.*) run=([0-9]+)\s*$",
    re.MULTILINE,
)

#: ``scripts/ci-failed`` prints this (without ``yet``) when a *completed*
#: run left it nothing to digest — every job cancelled, skipped or
#: infra-killed, or the run never started one (a startup failure,
#: ``jobs=[]``). That is an infrastructure red, not a test red (gr611682,
#: the coordinator ruling): :func:`_infra_done` owns it — the fix node
#: never closes ``no-op`` and the loop never re-cuts the same sha on it.
#: The in-progress variant (:data:`_STILL_RUNNING`) shares this prefix
#: and is checked first.
_NO_LINES = "no failed-job log"

#: A run still in progress with nothing failed yet — a superset of
#: :data:`_NO_LINES`. It is a deferral, never a ruling: the digest
#: parks on ``not_before`` and re-reads the run until it carries a failed
#: job or completes (the completed marker then takes the infra ruling).
_STILL_RUNNING = "no failed-job log lines yet"

#: Wait between digests while the run is still in progress
#: (``ci_verdict_poll``'s poll interval), and how many parks one digest
#: may take before the job fails "still in progress" — the tick
#: re-dispatches within the node's retries.
_PARK_INTERVAL = timedelta(minutes=10)
_PARK_CAP = 6

#: ``not_before``'s timestamp shape in a parked handle (land_slice's).
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: ``gh run view --json`` fields the infra ruling reads live at decision
#: time — a rerun landing between the ``ci-failed`` read and this one
#: changes the answer: status/conclusion (green now?), the jobs list
#: (a jobless or skipped-only run is gr611682's startup failure) and the
#: attempt counter (``attempt`` ≥ 2 means a rerun was already fired — by
#: this node or by hand — so never fire another).
_RUN_STATE_JSON = "status,conclusion,jobs,attempt"

#: The digest node's once-per-run marker, ``meta.<key> = {run_id, at}``:
#: a node that already fired ``gh run rerun`` for a run never fires it
#: again — the marker on the node's meta survives a job re-dispatch
#: where the compute handle's fields do not.
_INFRA_RERUN_KEY = "ci_infra_rerun"

#: The job-meta stamp :func:`_infra_blocked` leaves on the job it fails
#: (``plan_dispatch`` reads the same key, restated): a run still infra
#: past the park cap, or a ``gh run rerun`` the credential refused, is a
#: persistent infrastructure failure — the retry-cap ask files on the
#: ``ask-coordinator`` channel, never to a lane retry.
COORDINATOR_ASK_KEY = "coordinator_ask"

#: A completed run whose conclusion is one of these is green — the same
#: set ``ci_verdict_poll`` records its green verdict on.
_OK_CONCLUSIONS = _cvp.OK_CONCLUSIONS

#: A GitHub auth refusal in the script's captured output — the factory
#: token cannot read the run's logs. The failure names the probe, which
#: reports the credential's permission line on the same endpoint.
_AUTH_RE = re.compile(
    r"HTTP (401|403)\b|Bad credentials|Resource not accessible",
    re.IGNORECASE,
)

#: The fixer's rule, the last line of every brief this job posts. The
#: ``moot:`` tail (gr597599) names the escape ``run_slice._fix_moot``
#: honors: a fix already on main needs no session diff.
_RULE = (
    "Fix forward on main; never skip, xfail or loosen a test. Land the "
    "fix with scripts/ship --quick; the loop then re-cuts. If the ids "
    "are already green on origin/main, finish with `moot: <sha>` and "
    "no commit."
)

#: ci-failed's per-job section head, and the marker it prints when the
#: (shared or per-job) log carried no signal lines — the step-header
#: tail of gr587536. A digest whose every section is blind is an empty
#: brief: it must never dispatch a fix session (td587533).
_SECTION = re.compile(r"^== (.+?) ==$", re.MULTILINE)
_BLIND = "(no signal lines"

#: The jobs-logs rescue's failure-line matcher — the coordinator's list
#: on gr587536: pytest's summary ids, the ``= N failed`` count and the
#: E-lines of the failure detail, after ANSI/timestamp stripping.
_JOBLOG_SIGNAL = re.compile(r"^(?:FAILED|ERROR|E\s)|=+ *\d+ failed\b")
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_TS = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z\s+")
_UNKNOWN_FLAG = re.compile(r"unknown (flag|shorthand)", re.IGNORECASE)

#: Per-job cap on the rescued failure lines; the brief's _DIGEST_CAP
#: bounds the whole comment either way.
_RESCUE_CAP = 40

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The release-fix loop's digest node; the brief lands on each
        # run_slice todo blocked-by it. Optional in probe mode.
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (the host holding the checkout + gh).
        "target_node": {"type": "string"},
        # Probe mode: report what the credential may read on the run's
        # logs endpoint instead of digesting it.
        "probe": {"type": "boolean"},
        # A check.yml run id, digits only. Required in probe mode; an
        # explicit override for the newest red verdict otherwise.
        "run_id": {"type": "string", "pattern": "^[0-9]+$"},
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
    "Dark release pass, the release-fix loop's digest node: run "
    "scripts/ci-failed on the red check.yml run and post the digest as a "
    "brief: comment on the fix nodes blocked-by it. probe+run_id reports "
    "the credential's permission line instead. Refused while "
    "PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


def _mode_refusal(params: dict[str, Any]) -> str | None:
    """Probe mode needs ``run_id``; digest mode needs ``todo_id`` (and a
    numeric ``run_id`` when it is given as an override)."""
    if params.get("probe"):
        if not _RUN_ID_RE.fullmatch(str(params.get("run_id") or "")):
            return (
                f"{NAME}: probe mode needs params.run_id (a check.yml run id, digits)"
            )
        return None
    todo_id = params.get("todo_id")
    if not isinstance(todo_id, int) or isinstance(todo_id, bool) or todo_id <= 0:
        return (
            f"{NAME}: needs params.todo_id (the digest node) or "
            "params.probe + params.run_id (the credential probe)"
        )
    run_id = params.get("run_id")
    if run_id is not None and not _RUN_ID_RE.fullmatch(str(run_id)):
        return f"{NAME}: params.run_id must be digits (a check.yml run id)"
    return None


def _red_run_id(store: Any, sha: str) -> str | None:
    """The newest ``release.ci_verdicts`` row on ``sha`` (the tests-green
    gate's prefix match) that is red and carries a run id, else ``None``
    — :func:`precis.plans.release.newest_red_run`, shared with
    ``ci_rerun``'s run-id pick."""
    return _release.newest_red_run(store, sha)


def _fix_targets(store: Any, digest_id: int) -> list[int]:
    """Live todos ``blocked-by`` the digest node whose ``meta.op`` is
    ``run_slice`` — the fix nodes the brief lands on."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM links l JOIN refs r ON r.ref_id = l.src_ref_id
             WHERE l.relation = 'blocked-by' AND l.dst_ref_id = %s
               AND r.kind = 'todo' AND r.retired_at IS NULL
               AND (r.meta ->> 'op' = 'run_slice'
                    OR r.meta -> 'op' ->> 'type' = 'run_slice')
             ORDER BY r.ref_id
            """,
            (digest_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _sections(text: str) -> list[tuple[str, bool]]:
    """``[(job name, blind)]`` per ``== job ==`` section of a ci-failed
    digest — blind when the section carries the no-signal tail marker."""
    marks = list(_SECTION.finditer(text))
    out: list[tuple[str, bool]] = []
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(text)
        out.append((m.group(1), _BLIND in text[m.end() : end]))
    return out


def _gh(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """One ``gh`` call in the checkout with the factory credential — the
    same env :func:`release_cut.launch_script` hands ``ci-failed`` —
    ``cwd`` the checkout so ``{owner}/{repo}`` resolves. ``None`` when
    ``gh`` or the checkout is unavailable; callers read it as no read."""
    try:
        root = _rs.slice_root()
    except RuntimeError:
        return None
    try:
        return subprocess.run(
            ["gh", *args],
            cwd=root,
            env=_factory_git.git_auth_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None


def _gh_api(args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """``gh api --allow-escape-sequences <args>``, retried once without
    the flag on a gh that predates it (scripts/ci-failed's ``_gh_api``)."""
    cp = _gh(["api", "--allow-escape-sequences", *args])
    if (
        cp is not None
        and cp.returncode != 0
        and _UNKNOWN_FLAG.search(cp.stderr or cp.stdout)
    ):
        cp = _gh(["api", *args])
    return cp


def _jobs_logs_rescue(
    run_id: str, blind: list[str]
) -> tuple[list[tuple[str, list[str]]], str]:
    """Re-read the blind jobs through ``actions/jobs/<id>/logs`` and pull
    their pytest failure lines (gr587536, coordinator addition): the
    shared ``--log-failed`` stream can carry only the step header where
    the job's own log has the FAILED ids. ``[(name, deduped lines)]``
    for the jobs that yielded any, and the reason when none did."""
    cp = _gh_api([f"repos/{{owner}}/{{repo}}/actions/runs/{run_id}/jobs?per_page=100"])
    if cp is None or cp.returncode != 0:
        return [], "the run's jobs list is unreadable"
    try:
        jobs = json.loads(cp.stdout).get("jobs") or []
    except ValueError:
        return [], "the run's jobs list is unreadable"
    ids = {
        str(j.get("name") or ""): j.get("id")
        for j in jobs
        if isinstance(j, dict) and j.get("conclusion") == "failure"
    }
    out: list[tuple[str, list[str]]] = []
    for name in blind:
        jid = ids.get(name)
        if jid is None:
            continue
        cp = _gh_api([f"repos/{{owner}}/{{repo}}/actions/jobs/{jid}/logs"])
        if cp is None or cp.returncode != 0:
            continue
        lines: list[str] = []
        for raw in cp.stdout.splitlines():
            line = _TS.sub("", _ANSI.sub("", raw), count=1)
            if _JOBLOG_SIGNAL.search(line) and line not in lines:
                lines.append(line)
        if lines:
            kept = lines[:_RESCUE_CAP]
            if len(lines) > _RESCUE_CAP:
                # Mark the cut: the red-test waiver must not trust a
                # partial id list (``red_waiver.parse_failures``).
                kept.append(_waiver.RESCUE_TRUNCATED)
            out.append((name, kept))
    if not out:
        return [], "the jobs-logs read found no FAILED lines either"
    return out, ""


def _blocked_posted(store: Any, todo_id: int, run_id: str) -> bool:
    """``True`` when the todo already carries this run's ``blocked:``
    note — a retried digest must not spam one per attempt."""
    for c in _rs._todo_chunks(store, todo_id):
        if c.chunk_kind != "todo_comment":
            continue
        text = str(c.text or "").lstrip()
        if text.startswith("blocked:") and f"run {run_id}" in text:
            return True
    return False


# ── the infra ruling (gr611682) ────────────────────────────────────


def _run_state(run_id: str) -> dict[str, Any] | None:
    """The run's live ``{status, conclusion, jobs, attempt}`` —
    ``gh run view --json`` read at ruling time, not from the digest's
    snapshot: a rerun landing between the ``ci-failed`` read and this
    one changes the answer. ``None`` when gh or the read fails — callers
    treat an unreadable state as still-infra, never as green."""
    cp = _gh(["run", "view", run_id, "--json", _RUN_STATE_JSON])
    if cp is None or cp.returncode != 0:
        return None
    try:
        data = json.loads(cp.stdout)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _state_attempt(state: dict[str, Any]) -> int:
    """The run's ``attempt`` counter from the live read; 0 when absent
    or unreadable — an unproven rerun, so a first fire stays allowed."""
    try:
        return int(state.get("attempt") or 0)
    except (TypeError, ValueError):
        return 0


def _infra_why(state: dict[str, Any] | None) -> str:
    """The one-line infra classification for comments and the job
    summary — jobless (gr611682's startup failure), skipped/cancelled
    only, or failed jobs whose logs are unreadable: each is 'no failing
    test in the logs', never a test red."""
    if state is None:
        return "the run is unreadable"
    jobs = [j for j in state.get("jobs") or [] if isinstance(j, dict)]
    if not jobs:
        return "the run never started a job (jobs=[])"
    if any(j.get("conclusion") == "failure" for j in jobs):
        return "the failed jobs' logs are unreadable"
    counts: dict[str, int] = {}
    for j in jobs:
        key = str(j.get("conclusion") or j.get("status") or "?")
        counts[key] = counts.get(key, 0) + 1
    seen = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))
    return f"no job concluded failure ({seen})"


def _rerun_argv(run_id: str, state: dict[str, Any]) -> list[str]:
    """``gh run rerun`` args for an infra red: ``--failed`` re-fires the
    failed legs only when the run HAS failed jobs — on a jobless or
    skipped-only run it errors 'no failed jobs', and it can never
    re-fire jobs that did not conclude
    (``scripts/lib/ci_run.rerun_hint``)."""
    argv = ["run", "rerun", run_id]
    jobs = state.get("jobs") or []
    if any(isinstance(j, dict) and j.get("conclusion") == "failure" for j in jobs):
        argv.append("--failed")
    return argv


def _infra_rerun_marked(store: Any, todo_id: int, run_id: str) -> bool:
    """``True`` once this digest node fired ``gh run rerun`` for
    ``run_id`` — the :data:`_INFRA_RERUN_KEY` marker on the node's meta
    survives a job re-dispatch where the compute handle does not."""
    try:
        ref = store.get_ref(kind="todo", id=todo_id)
    except Exception:
        return False
    marker = (ref.meta or {}).get(_INFRA_RERUN_KEY) if ref is not None else None
    return isinstance(marker, dict) and str(marker.get("run_id") or "") == run_id


def _infra_blocked(
    ctx: Any,
    todo_id: int,
    run_id: str,
    targets: list[int],
    note: str,
    fail: str,
    log_text: str,
) -> bool:
    """The infra ruling's terminal exit: post ``note`` — deduped per
    run by :func:`_blocked_posted` — on the plan root and each fix node,
    then fail the job with ``fail`` and the masked log tail, stamped
    :data:`COORDINATOR_ASK_KEY` so the plan tick — after re-dispatching
    within the node's retries — files the cap ask on the
    ``ask-coordinator`` channel."""
    root = _release.plan_root(ctx.store, todo_id)
    for tid in dict.fromkeys([root, *targets]):
        if not _blocked_posted(ctx.store, tid, run_id):
            _rs._append_comment(ctx.store, tid, note)
    ctx.append_chunk("job_summary", f"{NAME}: {note}")
    ctx.set_meta(**{COORDINATOR_ASK_KEY: fail})
    _rc.fail_with_tail(ctx, fail, mask_secrets(log_text))
    return True


def _infra_done(
    ctx: Any,
    handle: dict[str, Any],
    todo_id: int,
    run_id: str,
    targets: list[int],
    rel: dict[str, Any],
    log_text: str,
) -> bool:
    """The ``_NO_LINES`` ruling (gr611682, the coordinator's): a
    completed red run with no failing test in the logs is an
    INFRASTRUCTURE red — jobless (a startup failure), skipped-only, or
    cancelled — never a test red. The fix node never closes ``no-op``
    on it: closing it let the loop re-cut and republish the same sha,
    so the same jobless run re-read as red every iteration until the
    cap. The digest asks for the rerun once — ``gh run rerun``;
    ``--failed`` only when failed jobs exist but their logs are
    unreadable — and parks on it, so the loop waits without burning an
    iteration. A run green now is recorded and the fix nodes close
    moot; a run that stays infra past :data:`_PARK_CAP` fails the job —
    the tick retries within the node's retries, then files the
    coordinator ask."""
    rn = str(rel.get("rN") or "r?")
    sha = str(rel.get("sha") or "")
    sha9 = sha[:9] or "unknown"
    rng = str(rel.get("range") or sha)
    state = _run_state(run_id)
    why = _infra_why(state)
    if (
        state is not None
        and str(state.get("status") or "") == "completed"
        and str(state.get("conclusion") or "") in _OK_CONCLUSIONS
    ):
        # The rerun landed green: the recorded red is stale. Record the
        # green so the tests-green gate and the loop's ``until`` open,
        # then close the fix nodes — nothing ever needed fixing.
        line = _release.record_ci_verdict(ctx.store, sha, rng, "green", run_id)
        for tid in targets:
            _rs._append_comment(
                ctx.store,
                tid,
                f"no-op: release {rn} red at {sha9} (run {run_id}) was an "
                f"infra red ({why}); the run is green now — nothing to fix.",
            )
            _rs.close_todo(ctx.store, tid, None)
        ctx.append_chunk(
            "job_summary",
            f"{NAME}: {rn} at {sha9} (run {run_id}) was an infra red and is "
            f"green now ({line}); fix node(s) closed no-op",
        )
        ctx.set_status("succeeded")
        return True
    if int(handle.get("parks") or 0) >= _PARK_CAP:
        # Still nothing to digest after the whole wait: the infra
        # ruling is terminal.
        note = (
            f"blocked: release {rn} is red at {sha9} (run {run_id}) — "
            f"{why}; an infra red, not a test red, and still red after "
            f"{_PARK_CAP} re-reads waiting on a rerun's verdict. A hand "
            "rerun or a push unblocks the loop; the digest job failed "
            "so the plan tick retries, then files the coordinator ask."
        )
        return _infra_blocked(
            ctx,
            todo_id,
            run_id,
            targets,
            note,
            f"{NAME}: run {run_id} is still an infra red ({why}) after "
            f"{_PARK_CAP} parks",
            log_text,
        )
    if state is None or str(state.get("status") or "") != "completed":
        # A rerun already in flight — or GitHub unreadable: the next
        # digest re-reads the run, like the still-running marker.
        status = str(state.get("status")) if state is not None else "unreadable"
        return _park_for_run(
            ctx,
            handle,
            run_id,
            log_text,
            waiting=f"{status} — the rerun's verdict has not landed",
            stuck=f"{status} past the rerun",
        )
    # A completed non-green run with nothing to digest — the infra red
    # proper. Ask for the rerun once, then wait on it.
    if _state_attempt(state) <= 1 and not _infra_rerun_marked(
        ctx.store, todo_id, run_id
    ):
        argv = _rerun_argv(run_id, state)
        cp = _gh(argv)
        if cp is not None and cp.returncode == 0:
            ctx.store.stamp_ref_meta(
                todo_id,
                {
                    _INFRA_RERUN_KEY: {
                        "run_id": run_id,
                        "at": datetime.now(UTC).strftime(_TS_FMT),
                    }
                },
            )
            shown = f"gh {' '.join(argv)}"
            root = _release.plan_root(ctx.store, todo_id)
            note = (
                f"note: release {rn} is red at {sha9} (run {run_id}) — "
                f"{why}; an infra red, not a test red. The digest fired "
                f"`{shown}` and waits on the rerun's verdict instead of "
                "re-cutting the same sha; the fix node stays open."
            )
            for tid in dict.fromkeys([root, *targets]):
                _rs._append_comment(ctx.store, tid, note)
            ctx.append_chunk(
                "job_event",
                f"{NAME}: {rn} red at {sha9} (run {run_id}) is infra "
                f"({why}) — `{shown}` fired; the loop waits",
            )
            return _park_for_run(
                ctx,
                handle,
                run_id,
                log_text,
                waiting=f"the rerun of run {run_id} landing",
                stuck="still an infra red after the fired rerun",
            )
        if cp is None:
            # A local fault (no checkout or gh), not a refusal — wait.
            return _park_for_run(
                ctx,
                handle,
                run_id,
                log_text,
                waiting=f"gh unreadable for the rerun of run {run_id}",
                stuck="still an infra red with gh unreadable",
            )
        lines = (cp.stderr or cp.stdout or "").strip().splitlines()
        detail = lines[-1][:160] if lines else f"exit {cp.returncode}"
        note = (
            f"blocked: release {rn} is red at {sha9} (run {run_id}) — "
            f"{why}; an infra red, not a test red, but "
            f"`gh {' '.join(argv)}` was refused ({detail}); the "
            "credential may lack Actions write. A hand rerun or a push "
            "unblocks the loop; the digest job failed so the plan tick "
            "retries, then files the coordinator ask."
        )
        return _infra_blocked(
            ctx,
            todo_id,
            run_id,
            targets,
            note,
            f"{NAME}: run {run_id} is an infra red ({why}) and its rerun "
            f"was refused ({detail})",
            log_text,
        )
    # A rerun was already asked for — by this node (the marker) or by
    # hand (attempt >= 2) — and the run is still an infra red: wait on
    # it, never fire another.
    return _park_for_run(
        ctx,
        handle,
        run_id,
        log_text,
        waiting=f"the asked-for rerun of run {run_id} producing a verdict",
        stuck=f"still an infra red ({why}) after the rerun",
    )


def _refusal(store: Any, params: dict[str, Any]) -> str | None:
    return (
        _rc.factory_refusal(NAME)
        or _rc.todo_refusal(store, NAME, params.get("todo_id"), required=False)
        or _mode_refusal(params)
    )


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    reason = _refusal(store, params)
    if reason is not None or store is None:
        return reason
    todo_id = params.get("todo_id")
    if not isinstance(todo_id, int):
        return None
    # The digest reads the run with the same env the cut pushed with; it
    # runs where the cut ran (the tick pins params.target_node).
    pin = _release.pin_refusal(
        _release.release_of(store, todo_id),
        host=str(params.get("target_node") or "").strip() or None,
    )
    return f"{NAME}: {pin}" if pin is not None else None


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason = _refusal(ctx.store, params)
    if reason is not None:
        ctx.record_failure(reason)
        return None
    if params.get("probe"):
        return _rc.launch_script(
            ctx, NAME, [str(params["run_id"]), "--probe"], script="ci-failed"
        )
    todo_id = params["todo_id"]
    run_id = str(params.get("run_id") or "")
    if not run_id:
        rel = _release.release_of(ctx.store, todo_id)
        if rel is None:
            ctx.record_failure(
                f"{NAME}: td{todo_id}'s plan has no recorded cut (meta.release)"
            )
            return None
        run_id = _red_run_id(ctx.store, str(rel.get("sha") or "")) or ""
        if not run_id:
            ctx.record_failure(f"{NAME}: no red verdict with a run id for {rel['rN']}")
            return None
    handle = _rc.launch_script(ctx, NAME, [run_id], script="ci-failed")
    if isinstance(handle, dict):
        handle["run_id"] = run_id
    return handle


def _probe_done(ctx: Any, log_text: str) -> bool:
    m = _PROBE_RE.search(log_text)
    if m is None:
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: scripts/ci-failed --probe exit 0 but no probe: line",
            mask_secrets(log_text),
        )
        return True
    ctx.append_chunk("job_summary", m.group(0).strip())
    ctx.set_meta(
        ci_probe={
            "status": m.group(1),
            "accepted_permissions": m.group(2).strip(),
            "run": m.group(3),
        }
    )
    ctx.set_status("succeeded")
    return True


def _compose_digest(log_text: str, rescue: str) -> str:
    """The masked digest through :data:`_DIGEST_CAP`, the rescued lines
    reserved the tail room: they are the brief's only real signal, so a
    blind dump past the cap loses the room, never them."""
    tail = mask_secrets(rescue)
    head = mask_secrets(log_text)[: max(0, _DIGEST_CAP - len(tail))]
    return (head + tail)[:_DIGEST_CAP]


class _WaiverPark(Exception):
    """The run is still in progress: :func:`_digest_done` parks the digest
    (``_park_for_run``) and re-reads later instead of ruling on a partial
    picture."""


def _try_waiver(
    ctx: Any,
    todo_id: int,
    rel: dict[str, Any],
    run_id: str,
    log_text: str,
    rescued: dict[str, list[str]],
) -> tuple[bool, list[str]]:
    """The red-test waiver (:mod:`precis.plans.red_waiver`) on a real test
    red. ``(True, steps)`` when it was granted AND carried out — the
    verdict recorded waived, the iteration's nodes closed, the job
    succeeded (or failed after the gripe was filed, so the tick retries
    and the retry converges on the same gripe); ``(False, steps)`` when it
    was off or refused, ``steps`` naming every check and the reason. A
    fault before the gripe is filed (git unreadable, filing raised)
    refuses the waiver: the fix loop runs as before."""
    steps: list[str] = []
    store = ctx.store
    if not _waiver.waiver_enabled(store):
        steps.append(f"waiver: {_waiver.SETTING_KEY} is off; the fix loop runs")
        return False, steps
    steps.append(f"waiver: {_waiver.SETTING_KEY} is on")
    rn = str(rel.get("rN") or "r?")
    sha = str(rel.get("sha") or "")
    rng = str(rel.get("range") or "")
    failures, problems = _waiver.parse_failures(log_text, rescued)
    shown = ", ".join(failures) or "none"
    steps.append(f"waiver: {len(failures)} failing test id(s) parsed: {shown}")
    if problems:
        steps.append("waiver: digest problems: " + "; ".join(problems))
        steps.append("waiver: REFUSED — the digest does not show every failing test")
        return False, steps
    # The run must be a complete picture: every job reported success or
    # failure (a cancelled / skipped / timed-out shard may hold more reds).
    state = _run_state(run_id)
    if _waiver.run_in_progress(state):
        # Not a verdict yet: the waiver must see the whole run, so the
        # digest parks and re-reads instead of refusing for good.
        raise _WaiverPark(str((state or {}).get("status") or "in progress"))
    partial = _waiver.partial_run_problem(state)
    if partial is not None:
        steps.append(f"waiver: REFUSED — the run is not complete: {partial}")
        return False, steps
    attempt = _state_attempt(state or {})
    if attempt < 1:
        steps.append("waiver: REFUSED — the run's attempt is unreadable (unbindable)")
        return False, steps
    steps.append(
        f"waiver: run {run_id}#{attempt} completed, every job success/failure/skipped"
    )
    try:
        root = _rs.slice_root()
    except RuntimeError:
        steps.append("waiver: REFUSED — no repo checkout to read the cut range in")
        return False, steps
    paths = _waiver.range_paths(root, rng, env=_factory_git.git_auth_env())
    if paths is None:
        steps.append(f"waiver: cut range {rng!r} unreadable in the checkout")
        steps.append("waiver: REFUSED — the cut range could not be read")
        return False, steps
    steps.append(f"waiver: cut range {rng} touches {len(paths)} path(s)")
    try:
        prior = _waiver.open_waived_tests(store, rn)
    except Exception as exc:
        steps.append(f"waiver: REFUSED — earlier waivers unreadable ({exc})")
        return False, steps
    steps.append(
        f"waiver: ids on still-unfixed waiver gripes: {sorted(prior) or 'none'}"
    )
    ok, why = _waiver.waiver_decision(list(failures), paths, prior)
    steps.append(f"waiver: {'rules passed' if ok else 'REFUSED'} — {why}")
    if not ok:
        return False, steps
    try:
        culprits = _waiver.culprit_commits(root, rng, failures)
        if not culprits:
            steps.append("waiver: REFUSED — no attributable culprit")
            return False, steps
        gid = _waiver.file_waiver_gripe(
            store,
            rn=rn,
            sha=sha,
            rng=rng,
            run_id=run_id,
            failures=failures,
            culprits=culprits,
            plan_root=_release.plan_root(store, todo_id),
        )
    except Exception as exc:
        log.warning("%s: waiver gripe for %s failed", NAME, rn, exc_info=True)
        steps.append(f"waiver: gripe not filed ({exc}); the fix loop runs")
        return False, steps
    steps.append(
        f"waiver: GRANTED — filed gr{gid} (prio 1, broken-build, release-waiver:{rn}); "
        f"{len(culprits)} culprit commit(s)"
    )
    ids = list(failures)
    status = _waiver.record_waived_verdict(
        store,
        sha=sha,
        rng=rng,
        run_id=run_id,
        attempt=attempt,
        gripe_id=gid,
        tests=ids,
    )
    steps.append(f"waiver: {status}")
    if not status.startswith("ci verdict recorded"):
        ctx.append_chunk("job_summary", mask_secrets("\n".join(steps)))
        ctx.record_failure(
            f"{NAME}: waiver gr{gid} filed but the verdict was not recorded "
            f"({status}); the tick retries and reuses gr{gid}"
        )
        return True, steps
    sha9 = sha[:9] or "unknown"
    closed: list[int] = []
    for tid in _waiver.iteration_nodes(store, todo_id):
        _rs._append_comment(
            store,
            tid,
            f"waived: gr{gid} — release {rn} at {sha9} (run {run_id}) "
            f"deploys past known-red test(s) {', '.join(ids)}; no fix "
            "session and no re-cut for this iteration.",
        )
        _rs.close_todo(store, tid, None)
        closed.append(tid)
    steps.append(
        "waiver: closed iteration node(s) "
        + (", ".join(f"td{t}" for t in closed) or "none")
        + f" (waived: gr{gid})"
    )
    ctx.append_chunk(
        "job_summary",
        mask_secrets(
            f"{NAME}: {rn} red at {sha9} (run {run_id}) WAIVED by gr{gid}\n"
            + "\n".join(steps)
        ),
    )
    ctx.set_meta(
        red_waiver={"rN": rn, "gripe": gid, "tests": ids, "run_id": run_id},
    )
    ctx.set_status("succeeded")
    return True, steps


def _digest_done(
    ctx: Any, handle: dict[str, Any], params: dict[str, Any], log_text: str
) -> bool:
    todo_id = params["todo_id"]
    run_id = str(handle.get("run_id") or params.get("run_id") or "")
    if _STILL_RUNNING in log_text:
        return _park_for_run(ctx, handle, run_id, log_text)
    rel = _release.release_of(ctx.store, todo_id) or {}
    rn = str(rel.get("rN") or "r?")
    sha9 = str(rel.get("sha") or "")[:9] or "unknown"
    rng = str(rel.get("range") or "")
    targets = _fix_targets(ctx.store, todo_id)
    if _NO_LINES in log_text:
        return _infra_done(ctx, handle, todo_id, run_id, targets, rel, log_text)
    sections = _sections(log_text)
    blind = [name for name, is_blind in sections if is_blind]
    rescue = ""
    rescued: list[tuple[str, list[str]]] = []
    if blind:
        rescued, why = _jobs_logs_rescue(run_id, blind)
        if rescued:
            parts = [
                "jobs-logs read (gh api actions/jobs/<id>/logs — the "
                "shared log carried only the step headers):"
            ]
            for name, lines in rescued:
                parts.append(f"== {name} ==")
                parts.extend(f"  {line}" for line in lines)
            rescue = "\n" + "\n".join(parts)
        elif len(blind) == len(sections):
            # A fully blind digest is an empty brief: no fix session is
            # dispatched on it (gr587536 — td587533 was claimed on one).
            # The plan and the fix nodes get the blocked: note; the job
            # fails so the plan tick retries, then files the ask.
            note = (
                f"blocked: release {rn} is red at {sha9} (run {run_id}) "
                "but its logs carry no failure lines the digest can read "
                f"— {why}. No fix session is dispatched on an empty "
                "brief; the digest job failed so the plan tick retries, "
                "then files the ask."
            )
            root = _release.plan_root(ctx.store, todo_id)
            for tid in dict.fromkeys([root, *targets]):
                if not _blocked_posted(ctx.store, tid, run_id):
                    _rs._append_comment(ctx.store, tid, note)
            ctx.append_chunk("job_summary", f"{NAME}: {note}")
            ctx.record_failure(
                f"{NAME}: run {run_id} digest is blind ({why}); no brief "
                "posted — blocked: notes on the plan and the fix nodes"
            )
            return True
    try:
        granted, waiver_lines = _try_waiver(
            ctx, todo_id, rel, run_id, log_text or "", dict(rescued)
        )
    except _WaiverPark as park:
        return _park_for_run(
            ctx,
            handle,
            run_id,
            log_text,
            waiting=f"{park} — the red-test waiver needs the whole run",
            stuck=f"{park} past the waiver's wait",
        )
    if granted:
        return True
    digest = _compose_digest(log_text or "", rescue)
    text = "\n".join(
        [
            f"brief: release {rn} is red at {sha9} (range {rng}), "
            f"check.yml run {run_id}",
            digest,
            _RULE,
        ]
    )
    if not targets:
        ctx.record_failure(
            f"{NAME}: no run_slice todo is blocked-by td{todo_id}; the "
            "brief has nowhere to land"
        )
        return True
    for tid in targets:
        _rs._append_comment(ctx.store, tid, text)
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: {rn} red at {sha9} (run {run_id}); brief posted on "
        f"td{', td'.join(str(t) for t in targets)}\n"
        + "\n".join(mask_secrets(line) for line in waiver_lines)
        + f"\n{text}",
    )
    ctx.set_status("succeeded")
    return True


def _park_for_run(
    ctx: Any,
    handle: dict[str, Any],
    run_id: str,
    log_text: str,
    *,
    waiting: str = "still in progress, nothing failed yet",
    stuck: str = "still in progress",
) -> bool:
    """Park the handle until ``not_before`` (land_slice's handle
    deferral — poll cannot hand a handle back, so it is mutated in place
    and persisted to ``meta.compute_handle``) and re-digest then.
    ``waiting``/``stuck`` name what the park is for in the event line and
    the cap failure — a run still in progress, or an infra red waiting
    on its rerun (:func:`_infra_done`). Past :data:`_PARK_CAP` the job
    fails. ``False`` keeps the job running."""
    parks = int(handle.get("parks") or 0) + 1
    if parks > _PARK_CAP:
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: run {run_id} {stuck} after {_PARK_CAP} "
            "parks; the plan tick re-dispatches within the node's retries",
            mask_secrets(log_text),
        )
        return True
    due = datetime.now(UTC) + _PARK_INTERVAL
    for key in ("pid", "pgid", "boot_id", "start_time"):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    handle.update(not_before=due.strftime(_TS_FMT), parks=parks)
    ctx.append_chunk(
        "job_event",
        f"{NAME}: run {run_id} {waiting} — "
        f"re-digest after {due:%H:%M}Z (park {parks}/{_PARK_CAP})",
    )
    # Parked time must not eat the ssh_node wall deadline: it covers the
    # wait plus one fresh ci-failed run.
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + _PARK_INTERVAL.total_seconds() + wall_seconds(),
    )
    return False


def _relaunch_when_due(ctx: Any, handle: dict[str, Any]) -> bool:
    """Poll half of a park: ``False`` before ``not_before``; at or after
    it, drop the marker and run scripts/ci-failed on the same run again
    (the parked fields — ``run_id``, ``parks`` — carry onto the fresh
    handle). ``True`` only when the relaunch failed the job."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _TS_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        due = datetime.now(UTC)  # unreadable marker: do not park forever
    if datetime.now(UTC) < due:
        return False
    handle.pop("not_before", None)
    run_id = str(handle.get("run_id") or "")
    parks = int(handle.get("parks") or 0)
    if not run_id:
        ctx.record_failure(f"{NAME}: parked handle has no run_id to re-digest")
        return True
    fresh = _rc.launch_script(ctx, NAME, [run_id], script="ci-failed")
    if fresh is None:
        return True
    handle.clear()
    handle.update(fresh)
    handle["run_id"], handle["parks"] = run_id, parks
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + wall_seconds(),
    )
    return False


def poll(ctx: Any, handle: Any) -> bool:
    if isinstance(handle, dict) and handle.get("not_before"):
        return _relaunch_when_due(ctx, handle)
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    params = dict((ctx.meta or {}).get("params") or {})
    if exit_code != 0:
        masked = mask_secrets(log_text)
        # The refusal pattern reads the script's own ``ci-failed:`` stderr
        # lines only — a partial digest (exit 1 past the metadata read)
        # can carry 'HTTP 403' as log content.
        err_lines = "\n".join(
            line for line in masked.splitlines() if line.startswith("ci-failed:")
        )
        auth = _AUTH_RE.search(err_lines)
        if auth is not None:
            run_id = str(handle.get("run_id") or params.get("run_id") or "")
            _rc.fail_with_tail(
                ctx,
                f"{NAME}: scripts/ci-failed got a GitHub auth refusal "
                f"({auth.group(0)}); next: run the token probe — submit "
                f"{NAME} with params.probe=true, params.run_id="
                f"{run_id or '<run-id>'} for the credential's permission line",
                masked,
            )
            return True
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: scripts/ci-failed exit {exit_code}",
            masked,
        )
        return True
    if params.get("probe"):
        return _probe_done(ctx, log_text)
    return _digest_done(ctx, handle, params, log_text)


kill = _rc.kill

__all__ = [
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "kill",
    "poll",
    "submit",
    "validate_submit",
    "wall_seconds",
]
