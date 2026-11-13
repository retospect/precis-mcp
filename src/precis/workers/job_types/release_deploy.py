"""``release_deploy`` — deploy an approved, green cut pinned (td543733).

Spec: ``docs/backlog/dark-factory.md`` § "4. Release pass, event-driven";
the ``deploy`` node of the ``release-pass`` template. Mechanical, no LLM;
detached submit/poll under ``ssh_node``, same launcher as ``release_cut``.

**Authority.** Release and deploy are Reto-only (authority.md). The deploy
node is ``blocked-by`` an approval ask (an ``answer`` gate); that gate opens
on any answer, so this job checks the answer itself:
:func:`precis.plans.release.approval` must find ``answer: deploy r<N>`` for
the r<N> the plan's cut recorded — Reto's own comment, or the tick's under
his standing approval ``release.auto_deploy`` (me573231:
:func:`precis.plans.release.auto_answer_approval` posts it once CI is
recorded green on the cut's exact sha and ``release.round`` names an open
round on that head, fail closed, an existing answer never overwritten).
:func:`validate_submit` refuses otherwise
(the tick then notes "not dispatched" each pass and mints nothing) and
:func:`submit` re-checks before launching. There is no param that names a
release: r<N> comes only from the plan root's ``meta.release``.

**Reviews hold the deploy** (release train, Reto 2026-11-12). The
engine-review gate never holds the cut; it holds this node:
:func:`precis.workers.job_types.release_poll.deploy_review_blockers`
judges every engine land inside the release — the cut's recorded range
plus the fixes on ``release/r<N>`` after it — and the FIX/REVERT
correctives their clearances rest on must be on ``release/r<N>``. A
land on main after the cut is the next release's and holds nothing.
:func:`validate_submit` refuses while any line stands (the tick notes
"not dispatched" and mints nothing) and :func:`submit` re-checks; any
gate read error refuses — fail closed.

**Host pin.** ``release_cut`` records the ``host`` and ``checkout`` it ran in
on ``meta.release``; the tick mints this job with ``params.target_node`` =
that host, :func:`validate_submit` refuses any other (or no) node, and
:func:`submit` refuses a worker whose ``PRECIS_NODE`` or checkout differs.

* :func:`submit` runs ``scripts/release --deploy-only r<N>`` in the same
  checkout the cut ran in (the ``.release-owner`` marker there makes it the
  release's owner). The script never cuts; it refuses unless release/r<N> is
  the live recorded release.
* :func:`poll`: exit 0 (deployed) or 3 (rollout done, runtime confirmation
  pending — that stays a session's ``scripts/round deploy
  --confirm-runtime``) succeeds; the ``RELEASE_DEPLOYED: r<N> <head> <prev>``
  line is recorded into ``release.prod_head`` through the store (the
  script's own write needs a prod DSN on the host and may have skipped),
  this release's ``release.round`` row is advanced to ``deployed`` with
  ``deploy.rollout_at`` and ``deploy.sha`` through
  :func:`precis.plans.release.update_round` (same ``n`` only — it never
  creates a row, recorded keys win; best-effort like the prod-head
  write; with no ``RELEASE_DEPLOYED`` line both keys stay off — the
  plan's sha is not the deployed head and the poll time is not the
  rollout time), and
  the ``next:`` line lands in the summary. Any other exit fails with the log
  tail.
"""

from __future__ import annotations

import json
import logging
import os
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.plans import release as _release
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import release_poll as _rp
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


def _refusal(
    store: Any, params: dict[str, Any], *, running: bool = False
) -> tuple[str | None, str | None]:
    """``(refusal, rN)``. Beyond approval, the deploy must sit where the cut
    ran (``meta.release.host`` / ``checkout``): at dispatch the host is the
    job's ``params.target_node`` (the tick pins it); once ``running`` it is
    this worker's own node and checkout."""
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
    host = os.environ.get("PRECIS_NODE") if running else params.get("target_node")
    checkout = None
    if running:
        try:
            checkout = str(_rs.slice_root())
        except RuntimeError as exc:
            return f"{NAME}: {exc}", None
    pin = _release.pin_refusal(
        _release.release_of(store, todo_id), host=host or None, checkout=checkout
    )
    if pin is not None:
        return f"{NAME}: {pin}", None
    held = _rp.deploy_review_blockers(store, todo_id)
    if held:
        return f"{NAME}: {rn} waits on its engine reviews — {'; '.join(held)}", None
    return None, rn


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return _refusal(store, params)[0]


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason, rn = _refusal(ctx.store, params, running=True)
    if reason is not None or rn is None:
        ctx.record_failure(reason or f"{NAME}: no approved release")
        return None
    ctx.set_meta(release=rn)
    # release-train T1: from here the release takes no more fixes — a
    # release-fix land reads this stamp (``release.deploy_started``)
    # before it launches ship onto release/r<N>. Stamped before the
    # deploy script runs; what is left is a land already past its check
    # (a ship launched before this stamp, still in its lint).
    ctx.append_chunk(
        "job_event",
        f"{NAME}: " + _release.mark_deploy_started(ctx.store, int(params["todo_id"])),
    )
    return _rc.launch_script(ctx, NAME, ["--deploy-only", rn])


#: The deploy playbook's own "Report the bounce-scope decision" line
#: (``deploy/redeploy-precis.yml`` step 0b2 — the one classifier, read
#: here from the deploy log rather than re-derived): ``bounce_scope=none``
#: restarts nothing, ``web`` only precis-web, ``full`` every daemon.
_BOUNCE_SCOPE_RE = re.compile(r"\bbounce_scope=(none|web|full)\b")

#: Scopes under which the deploy left every worker running.
UNBOUNCED_WORKER_SCOPES: frozenset[str] = frozenset({"none", "web"})


def parse_bounce_scope(log_text: str) -> str | None:
    """The deploy's bounce scope from its log: the widest verdict any host
    reported (``full`` over ``web`` over ``none``); ``None`` when the log
    carries no classifier line, and the caller then assumes nothing."""
    found = set(_BOUNCE_SCOPE_RE.findall(log_text))
    for scope in ("full", "web", "none"):
        if scope in found:
            return scope
    return None


#: ``app_settings`` key holding the worker shas still running because
#: every deploy since their start bounced no worker (JSON list, newest first).
UNBOUNCED_KEY = "release.unbounced_workers"


def _carry_unbounced(store: Any, scope: str | None, prev: str) -> list[str]:
    """Worker shas that may still heartbeat after this deploy: A→B→C with
    consecutive none/web deploys leaves workers on A, so ``prev`` joins
    the set carried from earlier unbounced deploys. A full (or
    unrecorded) scope restarted the workers: the set resets. Persisted
    under :data:`UNBOUNCED_KEY`."""
    from precis import settings

    shas: list[str] = []
    if scope in UNBOUNCED_WORKER_SCOPES and prev:
        settings.invalidate(UNBOUNCED_KEY)
        try:
            old = json.loads(settings.get_str(UNBOUNCED_KEY, store=store) or "[]")
        except ValueError:
            old = []
        for s in [prev, *(old if isinstance(old, list) else [])]:
            if isinstance(s, str) and len(s) >= 7 and s not in shas:
                shas.append(s)
    settings.set_setting(UNBOUNCED_KEY, json.dumps(shas), store=store)
    return shas


def _record_deploy_scope(
    store: Any, todo_id: Any, scope: str, prev: str, workers: list[str]
) -> str:
    """Stamp the classifier verdict on the plan root's
    ``meta.release.deploy_scope`` (``{scope, prev, workers}``), the record
    ``release_confirm`` already reads (gr645561): it accepts a worker still
    on ``prev`` or on any earlier unbounced sha in ``workers`` when the
    scope left the workers alone. One note."""
    rel = _release.release_of(store, todo_id) if isinstance(todo_id, int) else None
    if rel is None:
        return "bounce scope not recorded: no release on the plan root"
    root = _release.plan_root(store, todo_id)
    store.stamp_ref_meta(
        root,
        {
            "release": {
                **rel,
                "deploy_scope": {"scope": scope, "prev": prev, "workers": workers},
            }
        },
    )
    return f"bounce scope {scope} recorded on td{root} (prev {prev[:9] or '-'})"


def _landed(root: Path, prev: str, head: str) -> list[str]:
    rng = f"{prev}..{head}" if prev else head
    n = _release.LANDED_SHA_LEN
    proc = _rs._git(["rev-list", "--abbrev-commit", f"--abbrev={n}", rng], root)
    if proc.returncode != 0:
        return []
    return [s[:n] for s in proc.stdout.split()]


#: The deploy script's own words for a refusal before any rollout (the
#: backup-window refusal, scripts/deploy): no host moved.
_NO_HOST_TOUCHED = "no host was touched"


def poll(ctx: Any, handle: Any) -> bool:
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    if exit_code not in (0, 3):
        if _NO_HOST_TOUCHED in log_text:
            # release-train T3 review F5: the script refused before any
            # host moved (the backup window, a pin refusal) — the release
            # is not deploying, so it takes fixes again.
            todo_id = ((ctx.meta or {}).get("params") or {}).get("todo_id")
            if isinstance(todo_id, int):
                ctx.append_chunk(
                    "job_event",
                    f"{NAME}: " + _release.clear_deploy_started(ctx.store, todo_id),
                )
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
        scope = parse_bounce_scope(log_text)
        ctx.set_meta(
            release_deploy={"rN": rn, "head": head, "prev": prev, "scope": scope}
        )
        try:
            workers = _carry_unbounced(ctx.store, scope, prev)
        except Exception as exc:  # best-effort like the prod-head write
            log.warning("%s: unbounced worker shas failed", NAME, exc_info=True)
            lines.append(f"unbounced worker shas not carried: {type(exc).__name__}")
            workers = [prev] if prev else []
        if scope is not None and prev:
            try:
                lines.append(
                    _record_deploy_scope(
                        ctx.store,
                        ((ctx.meta or {}).get("params") or {}).get("todo_id"),
                        scope,
                        prev,
                        workers,
                    )
                )
            except Exception as exc:  # best-effort like the prod-head write
                log.warning("%s: deploy scope record failed", NAME, exc_info=True)
                lines.append(f"bounce scope not recorded: {type(exc).__name__}")
    else:
        lines.append("no RELEASE_DEPLOYED line (rollout was already done)")
    now = datetime.now(UTC)
    params = (ctx.meta or {}).get("params") or {}
    todo_id = params.get("todo_id")
    try:
        rel = (
            _release.release_of(ctx.store, todo_id)
            if isinstance(todo_id, int)
            else None
        )
        rn = deployed[0] if deployed else str((rel or {}).get("rN") or "")
        deploy: dict[str, Any] = {}
        if deployed is not None:
            deploy["rollout_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            if deployed[1]:
                deploy["sha"] = deployed[1]
        # With no RELEASE_DEPLOYED line the rollout was already done:
        # ``deploy.sha`` is left off rather than guessed from the plan's
        # cut sha (``pinned_deploy_sha`` falls back to the prod head and
        # the pinned-sha check can still fire) and no ``rollout_at`` is
        # written — now is the poll time, not the rollout time.
        if rn.startswith("r") and rn[1:].isdigit():
            fields: dict[str, Any] = {"n": int(rn[1:]), "state": "deployed"}
            if deploy:
                fields["deploy"] = deploy
            lines += _release.update_round(ctx.store, fields, now=now)
    except Exception as exc:
        # The rollout happened; like record_prod_head the shared
        # record's write is best-effort — a note, not a failure.
        log.warning("%s: release.round advance failed", NAME, exc_info=True)
        lines.append(f"release.round not advanced: {type(exc).__name__}")
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
