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
  retries. A digest whose ``== job ==`` sections are all blind (the
  ``(no signal lines`` tail — the shared stream carrying only step
  headers, gr587536) is never posted: the job first re-reads the failed
  jobs' own logs through ``gh api actions/jobs/<id>/logs``
  (:func:`_jobs_logs_rescue`) and appends the failure lines it finds —
  reserved the tail room under :data:`_DIGEST_CAP` so a long blind dump
  cannot push them off the brief; still blind, it posts a ``blocked:``
  note on the plan and each fix node and fails instead, so no session
  is claimed on an empty brief — the tick's retry cap files the ask.
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

from precis.plans import release as _release
from precis.utils.secret_scan import mask_secrets
from precis.workers import factory_git as _factory_git
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
#: run left it nothing to digest — every job cancelled or infra-killed.
#: There is no failing test to fix; the fix node closes ``no-op`` and the
#: loop re-cuts instead of dispatching a session. The in-progress variant
#: (:data:`_STILL_RUNNING`) shares this prefix and is checked first.
_NO_LINES = "no failed-job log"

#: A run still in progress with nothing failed yet — a superset of
#: :data:`_NO_LINES`. It is a deferral, never the no-op close: the digest
#: parks on ``not_before`` and re-reads the run until it carries a failed
#: job or completes (the completed marker above is then final).
_STILL_RUNNING = "no failed-job log lines yet"

#: Wait between digests while the run is still in progress
#: (``ci_verdict_poll``'s poll interval), and how many parks one digest
#: may take before the job fails "still in progress" — the tick
#: re-dispatches within the node's retries.
_PARK_INTERVAL = timedelta(minutes=10)
_PARK_CAP = 6

#: ``not_before``'s timestamp shape in a parked handle (land_slice's).
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: What the job records on that outcome (and what the fix node's close
#: comment says).
_NO_FAIL_NOTE = "no failing test in the logs (cancelled or infra)"

#: A GitHub auth refusal in the script's captured output — the factory
#: token cannot read the run's logs. The failure names the probe, which
#: reports the credential's permission line on the same endpoint.
_AUTH_RE = re.compile(
    r"HTTP (401|403)\b|Bad credentials|Resource not accessible",
    re.IGNORECASE,
)

#: The fixer's rule, the last line of every brief this job posts.
_RULE = (
    "Fix forward on main; never skip, xfail or loosen a test. Land the "
    "fix with scripts/ship --quick; the loop then re-cuts."
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
            out.append((name, lines[:_RESCUE_CAP]))
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
        for tid in targets:
            _rs._append_comment(
                ctx.store,
                tid,
                f"no-op: release {rn} is red at {sha9} (run {run_id}) — "
                f"{_NO_FAIL_NOTE}; nothing to fix, the loop re-cuts.",
            )
            _rs.close_todo(ctx.store, tid, None)
        closed = (
            f"; td{', td'.join(str(t) for t in targets)} closed no-op"
            if targets
            else ""
        )
        ctx.append_chunk(
            "job_summary",
            f"{NAME}: {rn} red at {sha9} (run {run_id}); {_NO_FAIL_NOTE}{closed}",
        )
        ctx.set_status("succeeded")
        return True
    sections = _sections(log_text)
    blind = [name for name, is_blind in sections if is_blind]
    rescue = ""
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
        f"td{', td'.join(str(t) for t in targets)}\n{text}",
    )
    ctx.set_status("succeeded")
    return True


def _park_for_run(ctx: Any, handle: dict[str, Any], run_id: str, log_text: str) -> bool:
    """The run is still in progress with nothing failed yet: park the
    handle until ``not_before`` (land_slice's handle deferral — poll
    cannot hand a handle back, so it is mutated in place and persisted to
    ``meta.compute_handle``) and re-digest then. Past :data:`_PARK_CAP`
    the job fails "still in progress". ``False`` keeps the job running."""
    parks = int(handle.get("parks") or 0) + 1
    if parks > _PARK_CAP:
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: run {run_id} still in progress after {_PARK_CAP} "
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
        f"{NAME}: run {run_id} still in progress, nothing failed yet — "
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
