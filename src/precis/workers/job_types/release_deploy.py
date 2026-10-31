"""``release_deploy`` — deploy an approved, green cut pinned (td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``deploy`` node of the ``release-pass`` template. Mechanical, no LLM;
detached submit/poll under ``ssh_node``, same launcher as ``release_cut``.

**Authority.** Release and deploy are Reto-only (authority.md). The deploy
node is ``blocked-by`` an approval ask (an ``answer`` gate); that gate opens
on any answer, so this job checks the answer itself:
:func:`precis.plans.release.approval` must find ``answer: deploy r<N>`` for
the r<N> the plan's cut recorded. :func:`validate_submit` refuses otherwise
(the tick then notes "not dispatched" each pass and mints nothing) and
:func:`submit` re-checks before launching. There is no param that names a
release: r<N> comes only from the plan root's ``meta.release``.

* :func:`submit` runs ``scripts/release --deploy-only r<N>`` in the same
  checkout the cut ran in (the ``.release-owner`` marker there makes it the
  release's owner). The script never cuts; it refuses unless release/r<N> is
  the live recorded release.
* :func:`poll`: exit 0 (deployed) or 3 (rollout done, runtime confirmation
  pending — that stays a session's ``scripts/round deploy
  --confirm-runtime``) succeeds; the ``RELEASE_DEPLOYED: r<N> <head> <prev>``
  line is recorded into ``release.prod_head`` through the store (the
  script's own write needs a prod DSN on the host and may have skipped), and
  the ``next:`` line lands in the summary. Any other exit fails with the log
  tail.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import run_slice as _rs

log = logging.getLogger(__name__)

NAME = "release_deploy"

#: CI wait (the script's --ci-timeout default, 90 min) plus the rollout.
_DEFAULT_WALL_S = 3 * 3600

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's deploy node: the approval and r<N> are read from it.
        "todo_id": {"type": "integer"},
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
    "Dark release pass: deploy the plan's cut release/r<N> pinned with "
    "scripts/release --deploy-only r<N>, only once the approval ask that "
    "blocks the deploy node is answered 'deploy r<N>'; records the prod head. "
    "Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


def _refusal(store: Any, params: dict[str, Any]) -> tuple[str | None, str | None]:
    """``(refusal, rN)``."""
    todo_id = params.get("todo_id")
    reason = _rc.factory_refusal(NAME) or _rc.todo_refusal(
        store, NAME, todo_id, required=True
    )
    if reason is not None:
        return reason, None
    if store is None or not isinstance(todo_id, int):
        return None, None
    rn, why = _release.approval(store, todo_id)
    if rn is None:
        return f"{NAME}: not approved — {why}", None
    return None, rn


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return _refusal(store, params)[0]


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason, rn = _refusal(ctx.store, params)
    if reason is not None or rn is None:
        ctx.record_failure(reason or f"{NAME}: no approved release")
        return None
    ctx.set_meta(release=rn)
    return _rc.launch_script(ctx, NAME, ["--deploy-only", rn])


def _landed(root: Path, prev: str, head: str) -> list[str]:
    rng = f"{prev}..{head}" if prev else head
    n = _release.LANDED_SHA_LEN
    proc = _rs._git(["rev-list", "--abbrev-commit", f"--abbrev={n}", rng], root)
    if proc.returncode != 0:
        return []
    return [s[:n] for s in proc.stdout.split()]


def poll(ctx: Any, handle: Any) -> bool:
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    if exit_code not in (0, 3):
        _rc.fail_with_tail(
            ctx, f"{NAME}: scripts/release --deploy-only exit {exit_code}", log_text
        )
        return True
    lines = [
        f"{NAME}: exit {exit_code} ({'deployed' if exit_code == 0 else 'rollout done, runtime confirmation pending'})"
    ]
    deployed = _release.parse_deployed(log_text)
    if deployed is not None:
        rn, head, prev = deployed
        root = Path(str(handle.get("root") or "."))
        try:
            lines.append(
                _release.record_prod_head(ctx.store, head, _landed(root, prev, head))
            )
        except Exception as exc:  # the rollout happened; bookkeeping is best-effort
            log.warning("%s: record_prod_head failed", NAME, exc_info=True)
            lines.append(f"prod head not recorded: {type(exc).__name__}")
        ctx.set_meta(release_deploy={"rN": rn, "head": head, "prev": prev})
    else:
        lines.append("no RELEASE_DEPLOYED line (rollout was already done)")
    lines += [ln for ln in log_text.splitlines() if ln.startswith("next:")][-1:]
    ctx.append_chunk("job_summary", "\n".join(lines))
    ctx.set_status("succeeded")
    return True


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
