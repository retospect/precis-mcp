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
  retries.
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

import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import gates as _gates
from precis.plans import release as _release
from precis.utils.secret_scan import mask_secrets
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
    gate's prefix match) that is red and carries a run id, else ``None``."""
    best: tuple[str, int] | None = None
    run_id: str | None = None
    for i, row in enumerate(_gates.read_ci_verdicts(store)):
        if row["verdict"] != "red":
            continue
        rid = str(row.get("run_id") or "").strip()
        if not rid or not _gates._prefix_match(sha, row["sha"].lower()):
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, run_id = key, rid
    return run_id


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
    digest = mask_secrets(log_text or "")[:_DIGEST_CAP]
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
