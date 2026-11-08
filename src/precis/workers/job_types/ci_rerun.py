"""``ci_rerun`` — re-fire a red check.yml run's failed legs, once per run id.

The ``validated:`` non-repro path (gr588028): a release-fix ``run_slice``
session that ends ``validated: <why>`` with no commit did not reproduce
the red legs — the flake read says re-fire them rather than burn a fix
attempt or land in rescue (the r248 incident, run 42437576554 rerun by
hand). ``run_slice.poll`` then parks the fix node on
``waiting-for:ci-rerun`` instead of closing it, and the plan tick's
``_step_ci_rerun`` mints this job. The once-per-run-id rule is the mint's
``ci_rerun:<run_id>`` idem key plus
:func:`precis.plans.release.ci_rerun_fired`: a second ``validated:`` on
the same run id falls back to the ordinary ``no_commit`` path — one
rerun says flake, two says real.

Mechanics mirror ``ci_red_digest``: a detached submit/poll pair under
``ssh_node`` running ``scripts/ci-rerun <run_id>`` in the checkout the
cut ran in, pinned to that host the same way (``params.target_node``;
``params.todo_id`` resolves the plan root's ``meta.release`` for the
cut sha and host). The run id is ALWAYS the newest ``red``
``release.ci_verdicts`` row on the cut sha
(:func:`precis.plans.release.newest_red_run`) — never a green run,
never another sha's run, and never a caller-supplied id: a supplied
``params.run_id`` is refused outright (only ``scripts/ci-rerun
--probe`` takes a run id — a read-only 404 POST that fires nothing).

``gh run rerun --failed`` needs the Actions write permission where the
digest needed read only — a credential that cannot write gets a 403
("Resource not accessible"), which fails the job with a message naming
``scripts/ci-rerun --probe`` (a run-0 POST that fires nothing and prints
the credential's permission line). The tick's stall path files the ask;
nothing loops.

On exit 0 the job parks its handle once for :data:`_SETTLE` — GitHub
takes a beat to flip the rerun to in_progress, and a verdict read before
it does still sees the old red conclusion — then succeeds with
``meta.ci_rerun.run_id``. The tick then mints the plan's own
``ci_verdict_poll`` under the fix node to record the rerun's verdict;
there is no second verdict reader.

Refused while ``PRECIS_FACTORY=off``.
"""

from __future__ import annotations

import logging
import os
import re
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.utils.secret_scan import mask_secrets
from precis.workers.job_types import release_cut as _rc

log = logging.getLogger(__name__)

NAME = "ci_rerun"

#: Requesting a rerun is one gh call; the wall only covers a stuck one.
_DEFAULT_WALL_S = 10 * 60

#: A GitHub auth refusal in the script's own ``ci-rerun:`` stderr lines —
#: the factory token cannot write the run's rerun endpoint.
_AUTH_RE = re.compile(
    r"HTTP (401|403)\b|Bad credentials|Resource not accessible",
    re.IGNORECASE,
)

#: ``scripts/ci-rerun``'s pre-rerun baseline line on stdout: the run's
#: ``run_attempt`` before ``gh run rerun`` fired (gr588028 review, F4 —
#: the verdict poll's freshness guard).
_ATTEMPT_RE = re.compile(r"^ci-rerun: attempt=(\d+)\s*$", re.MULTILINE)

#: ``not_before``'s timestamp shape in a parked handle (land_slice's).
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: How long an accepted rerun request settles before the job succeeds:
#: the window GitHub takes to flip the run back to in_progress, so the
#: verdict poll the tick mints next does not read the stale red
#: conclusion.
_SETTLE = timedelta(minutes=5)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The parked fix node: the plan root's meta.release names the cut.
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (the host holding the checkout + gh).
        "target_node": {"type": "string"},
        "resources": {
            "type": "object",
            "properties": {"wall_seconds": {"type": "integer"}},
        },
    },
    "required": ["todo_id"],
    "additionalProperties": False,
}

COMPATIBLE_EXECUTORS: frozenset[str] = frozenset({"ssh_node"})
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION: str = (
    "Dark release pass, the validated-flake rerun: run scripts/ci-rerun "
    "on the red check.yml run to re-fire its failed jobs (once per run "
    "id), then settle so the plan's verdict poll records the rerun's "
    "verdict. Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


def _refusal(store: Any, params: dict[str, Any]) -> str | None:
    reason = _rc.factory_refusal(NAME) or _rc.todo_refusal(
        store, NAME, params.get("todo_id"), required=True
    )
    if reason is not None:
        return reason
    if params.get("run_id") is not None:
        return (
            f"{NAME}: params.run_id is not accepted — the rerun always "
            "targets the newest red verdict run for the plan's cut sha"
        )
    todo_id = params.get("todo_id")
    if (
        store is not None
        and isinstance(todo_id, int)
        and _release.release_of(store, todo_id) is None
    ):
        return (
            f"{NAME}: td{todo_id} is not under a release plan with a "
            "recorded cut (meta.release)"
        )
    return None


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    reason = _refusal(store, params)
    if reason is not None or store is None:
        return reason
    # The rerun fires with the same env the cut pushed with; it runs
    # where the cut ran (the tick pins params.target_node).
    pin = _release.pin_refusal(
        _release.release_of(store, params["todo_id"]),
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
    todo_id = params["todo_id"]
    rel = _release.release_of(ctx.store, todo_id)
    if rel is None:
        ctx.record_failure(
            f"{NAME}: td{todo_id}'s plan has no recorded cut (meta.release)"
        )
        return None
    run_id = _release.newest_red_run(ctx.store, str(rel.get("sha") or "")) or ""
    if not run_id:
        ctx.record_failure(f"{NAME}: no red verdict with a run id for {rel['rN']}")
        return None
    handle = _rc.launch_script(ctx, NAME, [run_id], script="ci-rerun")
    if isinstance(handle, dict):
        handle["run_id"] = run_id
    return handle


def _park_for_settle(ctx: Any, handle: dict[str, Any], run_id: str) -> bool:
    """The rerun request was accepted: park the handle until ``not_before``
    (``ci_red_digest._park_for_run``'s in-place deferral) so GitHub has
    flipped the run to in_progress before the verdict poll the tick mints
    next reads it. ``False`` keeps the job running."""
    due = datetime.now(UTC) + _SETTLE
    for key in ("pid", "pgid", "boot_id", "start_time"):
        handle.pop(key, None)
    Path(str(handle.get("exit_file") or os.devnull)).unlink(missing_ok=True)
    handle["not_before"] = due.strftime(_TS_FMT)
    ctx.append_chunk(
        "job_event",
        f"{NAME}: run {run_id} rerun requested — settling "
        f"{int(_SETTLE.total_seconds() // 60)} min for GitHub to flip the "
        "run back to in_progress",
    )
    # Parked time must not eat the ssh_node wall deadline: it covers the
    # settle plus margin.
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + _SETTLE.total_seconds() + wall_seconds(),
    )
    return False


def _settle_done(ctx: Any, handle: dict[str, Any]) -> bool:
    """Poll half of the settle park: ``False`` before ``not_before``; after
    it, succeed with the run id stamped for the tick's poll mint."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _TS_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        due = datetime.now(UTC)  # unreadable marker: do not park forever
    if datetime.now(UTC) < due:
        return False
    run_id = str(handle.get("run_id") or "")
    ctx.set_meta(
        ci_rerun={
            "run_id": run_id,
            "rerun_attempt_before": handle.get("rerun_attempt_before"),
        }
    )
    ctx.append_chunk(
        "job_summary",
        f"{NAME}: run {run_id} rerun requested; the plan's verdict poll "
        "records the rerun's verdict",
    )
    ctx.set_status("succeeded")
    return True


def poll(ctx: Any, handle: Any) -> bool:
    if isinstance(handle, dict) and handle.get("not_before"):
        return _settle_done(ctx, handle)
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    if exit_code != 0:
        masked = mask_secrets(log_text)
        # The refusal pattern reads the script's own ``ci-rerun:`` stderr
        # lines only — never echoed log content.
        err_lines = "\n".join(
            line for line in masked.splitlines() if line.startswith("ci-rerun:")
        )
        auth = _AUTH_RE.search(err_lines)
        if auth is not None:
            _rc.fail_with_tail(
                ctx,
                f"{NAME}: scripts/ci-rerun got a GitHub auth refusal "
                f"({auth.group(0)}) — the credential may lack Actions "
                "write; next: hand-check `scripts/ci-rerun --probe` for "
                "its permission line on the rerun endpoint",
                masked,
            )
            return True
        _rc.fail_with_tail(
            ctx,
            f"{NAME}: scripts/ci-rerun exit {exit_code}",
            masked,
        )
        return True
    run_id = str(handle.get("run_id") or "")
    # The pre-rerun attempt baseline the script printed — the verdict
    # poll the tick mints next reads a verdict as fresh only past it.
    m = _ATTEMPT_RE.search(log_text)
    if m is not None:
        handle["rerun_attempt_before"] = int(m.group(1))
    return _park_for_settle(ctx, handle, run_id)


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
