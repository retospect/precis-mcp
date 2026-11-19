"""``release_deploy`` — deploy an approved, green cut pinned (td543733).

Spec: `me692430` § "4. Release pass, event-driven";
the ``deploy`` node of the ``release-pass`` template. Mechanical, no LLM;
detached submit/poll under ``ssh_node``, same launcher as ``release_cut``.

**Authority.** Manual passes block on an approval ask; since its answer
gate opens on any answer, :func:`precis.plans.release.approval` checks
``answer: deploy r<N>`` itself. Standing ``release.auto_deploy`` approval
skips the ask at mint; existing asks still keep their answer contract.
:func:`validate_submit` refuses otherwise (the tick notes "not dispatched"
and mints nothing), and :func:`submit` rechecks before launching. There is
no param that names a release: r<N> comes from the plan root's
``meta.release``.

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
The first cut has no host yet: plan_dispatch pins it to ``release.cut_host``
(held when unset) and ``release_cut`` refuses a checkout with no deploy
overlay, so the host recorded here is one that can deploy (gr713351).

**Shared release policy** (gr702332). :func:`_refusal` calls
:func:`precis.plans.release.may_release` at dispatch and launch:
other open passes/rounds, prod/main ancestry, release reviews and a green CI
verdict on the exact head all gate the deploy. Standing auto-deploy skips
minting an approval ask; a manual pass still requires its answer.
GitHub ancestry and in-flight reads fail closed: an outage holds deployment
until a later tick can verify them. The clock is checked only at dispatch;
once running, the script guards the window and its refusal is attempt-free.
Malformed window settings are left to the script's own guard. A backup-window refusal that races dispatch still stamps
``meta.attempt_free = "backup-window"`` in :func:`poll`, so the plan tick
counts no attempt for it.

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

**The session MCP pin** (gr685985). The rollout does not move the prod
watched checkout the session MCP serves from, and nothing else ran the
manual ``scripts/host-ops/pin-prod`` step, so every release left the
agents' tool surface one release behind. After a rollout poll runs
``scripts/host-ops/pin-prod-sha`` at the deployed sha
(:func:`_pin_prod`) — the smallest mechanism: one fixed helper, invoked
directly when the factory user owns the checkout and otherwise as
``sudo -n -u <owner> <helper> <sha>`` under a one-line sudoers grant
(``pin-prod-sha.sudoers``; no env, no other arguments, so the factory
gets no write access to the tree). Both owner detection and the helper
use the fixed ``<main-checkout>-prod`` sibling, ignoring worker environment
overrides and home fallbacks that sudo could change; the CheckoutWatchdog restarts
the server onto the new HEAD. Best-effort: a failed pin is a summary
note, the rollout stands, and ``release_confirm`` fails on "MCP still
serves <sha>" instead of attesting a lagging server.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
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
    "scripts/release --deploy-only r<N>, under standing approval or the "
    "blocking ask's 'deploy r<N>' answer and shared release gate; records prod. "
    "Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


#: ``meta.attempt_free`` value of a job the backup-window guard refused:
#: ``plan_dispatch`` counts no attempt for it (its ``ATTEMPT_FREE_KEY``).
WINDOW_FREE_REASON = "backup-window"

#: ``run_slice.ATTEMPT_FREE_KEY``, restated (the one-way dependency rule).
WINDOW_FREE_REASON_KEY = "attempt_free"

#: The deploy script's marker for the window refusal, as against its other
#: pre-rollout refusals (``_NO_HOST_TOUCHED`` alone also covers those).
_WINDOW_REFUSAL = "backup-window guard refused"


def _utcnow() -> datetime:
    return datetime.now(UTC)


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
    ok, reason = _release.may_release(
        store,
        "deploy",
        todo_id=todo_id,
        now=_utcnow(),
        checks=("flight", "head", "branches", "deploy")
        if running
        else ("clock", "flight", "head", "branches", "deploy"),
    )
    if not ok:
        if reason.startswith("inside the no-release window "):
            hit = reason.removeprefix("inside the no-release window ")
            return (
                f"{NAME}: held — inside the backup window {hit} "
                f"({_rp.WINDOWS_SETTING}); dispatches once it ends"
            ), None
        return f"{NAME}: {reason}", None
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
    return _rc.launch_script(ctx, NAME, ["--deploy-only", rn], env=_rc.pass_env(ctx))


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


#: The pin helper's wall: one ``git fetch`` and a detached checkout.
_PIN_TIMEOUT_S = 120

#: The narrow helper that moves the prod watched checkout (gr685985) —
#: invoked directly when the factory user owns the checkout, else through
#: the one-line sudoers grant shipped as ``pin-prod-sha.sudoers``.
PIN_PROD_HELPER = Path("scripts") / "host-ops" / "pin-prod-sha"

_SHA_RE = re.compile(r"[0-9a-f]{7,40}")

#: sudo's own words when the sudoers grant is absent (``sudo -n`` refuses
#: before any command runs).
_SUDO_GRANT_MISS = re.compile(
    r"password is required|no askpass|not in the sudoers|a terminal is required|not allowed to execute",
    re.I,
)


def _prod_checkout_dir(root: Path) -> Path:
    """Fixed sibling of the main checkout, also used by pin-prod-sha.

    Ignore worker environment and home-directory fallbacks: sudo can change
    both, so owner detection and the privileged pin must not depend on them.
    """
    proc = _rs._git(["rev-parse", "--path-format=absolute", "--git-common-dir"], root)
    main = Path(proc.stdout.strip()).parent if proc.returncode == 0 else root
    return Path(f"{main}-prod")


def _path_owner(path: Path) -> str:
    """``path``'s owner name (``""`` when unstatable)."""
    import pwd

    try:
        return pwd.getpwuid(path.stat().st_uid).pw_name
    except (OSError, KeyError):
        return ""


def _me() -> str:
    """The factory worker's user name."""
    import pwd

    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


def _pin_argv(helper: Path, owner: str, user: str, sha: str) -> list[str]:
    """The pin command: the helper alone when the factory user owns the
    checkout, else ``sudo -n -u <owner> <helper> <sha>`` — exactly, no
    ``env`` hop and no other arguments (the helper sets its own
    variables; a sudoers grant that allowed env would allow any command
    as the owner)."""
    if owner and owner != user:
        return ["sudo", "-n", "-u", owner, str(helper), sha]
    return [str(helper), sha]


def _run_pin(argv: list[str], root: Path) -> subprocess.CompletedProcess[str]:
    """Seam: run the pin command (tests stub this)."""
    return subprocess.run(
        argv,
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=_PIN_TIMEOUT_S,
        check=False,
    )


def _pin_prod(root: Path, sha: str = "") -> str:
    """Seam: pin the prod watched checkout at the deployed ``sha`` through
    :data:`PIN_PROD_HELPER` and return one summary note (never raises) —
    the CheckoutWatchdog then restarts the session MCP onto the release.
    Cross-owner the helper runs as the checkout's owner through the
    narrow sudoers grant, so the factory gets no write access to the
    tree. A missing grant, a failed pin, or a reported HEAD that is not
    the deployed sha all say "not pinned" — best-effort: the rollout
    stands, and ``release_confirm`` fails on "MCP still serves <sha>"
    instead of attesting a lagging server."""
    helper = (root / PIN_PROD_HELPER).resolve()
    if not helper.exists():
        return f"prod checkout not pinned: no {PIN_PROD_HELPER} in {root}"
    if not _SHA_RE.fullmatch(sha or ""):
        return f"prod checkout not pinned: bogus deployed sha {sha[:12]!r}"
    try:
        owner = _path_owner(_prod_checkout_dir(root))
        user = _me()
        argv = _pin_argv(helper, owner, user, sha)
        proc = _run_pin(argv, root)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"prod checkout not pinned: {type(exc).__name__}"
    if proc.returncode != 0:
        if argv[0] == "sudo" and _SUDO_GRANT_MISS.search(proc.stderr or ""):
            return (
                "prod checkout not pinned: missing sudoers grant — add on this "
                f"host: {user} ALL=({owner}) NOPASSWD: {helper}"
            )[:300]
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:]
        return f"prod checkout not pinned: exit {proc.returncode} {''.join(tail)}"[:300]
    first = (proc.stdout or "").strip().splitlines()[:1]
    m = re.search(r" at ([0-9a-f]{7,40})\b", "".join(first))
    if m is None or not sha.startswith(m.group(1)):
        got = m.group(1) if m else "?"
        return f"prod checkout not pinned: at {got}, deployed {sha[:12]}"
    return f"prod checkout pinned: {''.join(first)}"[:300]


#: The deploy script's own words for a refusal before any rollout (the
#: backup-window refusal, scripts/deploy): no host moved.
_NO_HOST_TOUCHED = "no host was touched"

#: ``scripts/deploy``'s exit code for the same refusal — every guard ahead
#: of its reachability ping, whatever the wording (gr730609).
#: ``scripts/round`` and ``scripts/release`` pass it through; a test pins
#: the copies. The words above still cover a checkout whose scripts
#: predate the code.
UNTOUCHED_EXIT = 75


def poll(ctx: Any, handle: Any) -> bool:
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    if exit_code not in (0, 3):
        if exit_code == UNTOUCHED_EXIT or _NO_HOST_TOUCHED in log_text:
            # release-train T3 review F5: the script refused before any
            # host moved (the backup window, a pin refusal, a checkout
            # with no overlay) — the release is not deploying, so it
            # takes fixes again.
            if _WINDOW_REFUSAL in log_text:
                # gr697664: a window refusal is no failed attempt.
                ctx.set_meta(**{WINDOW_FREE_REASON_KEY: WINDOW_FREE_REASON})
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
        lines.append(_pin_prod(root, head))
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
