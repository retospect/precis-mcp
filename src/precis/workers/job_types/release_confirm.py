"""``release_confirm`` — the factory attests the deployed runtime (td573432).

Spec: `me692430` § "4. Release pass, event-driven";
the ``confirm`` node of the ``release-pass`` template
(:func:`precis.plans.templates.mint_release_pass`). Mechanical, no LLM; a
detached submit/poll pair under ``ssh_node``, the same launcher as
``release_cut``, pinned to the cut's host and checkout the same way.

After ``release_deploy`` the plan must confirm the pinned sha is what
runs, without a session. :func:`gather_evidence` reads the mechanical
evidence — every live worker host's ``host_heartbeat.meta->>'code_sha'``
(the release daemon cohort of :mod:`precis.daemon_fleet`, store only,
no network; a heartbeat inside the last 60 minutes on a non-ephemeral
host that is not a 12-hex container id — with a published
``deploy.fleet`` ``covered`` list the configured members alone attest,
td649547; without one a host running workers or carrying a pre-snapshot
beat does, and a worker-less one-off checkout is a note, never counted,
gr623769) — plus the live web's
``X-Precis-Boot-Sha`` header when the ``release.health_url`` operator
setting names its ``/healthz`` (one plain bounded ``httpx`` GET — the
URL is operator-set, not agent-supplied, so the SSRF guard is the wrong
gate and does not apply, gr607271). The workers decide; the web is
optional. Any gap or mismatch fails the job *before* any script runs;
a failure whose only problem is the web probe's — every live worker
already at the release sha — files the ruling ask itself with the
evidence rather than dying into the tick's retry-cap showstopper. The
factory attests, a failed probe asks, never rolls back; there is no
``good/r<N>`` tag.

**The session MCP** (gr685985). The shared session MCP serves from a
watched checkout that only a manual step (``scripts/host-ops/pin-prod``)
moves, and worker heartbeats say nothing about it: r337 and r338 were
confirmed while it still served the release before. The server stamps what
it serves in the ``release.mcp_served`` setting (:mod:`precis.mcp_served_stamp`)
and :func:`_mcp_evidence` reads the stamp, store only — no HTTP, no token.
No stamp, or one older than the worker liveness window, attests nothing
and is not a problem (an install without a shared MCP still confirms). A
live stamp attests when its ``sha`` is the release sha, or the previous
prod sha under an unbounced scope; or when its ``pending_sha`` — the
checkout's HEAD, which the checkout watchdog stamps on first seeing it
move — is the release sha (the restart is deferred while sessions are
live, up to the watchdog's cap, so the confirm does not wait for it).
Any other live stamp parks the probe like a worker still rolling, and past
the deadline is the one problem; when it is the *only* problem the job
files its own ruling ask (:func:`_mint_mcp_ask`), as for the web.

**The restart race.** Right after the rollout a worker's heartbeat can
still carry the old sha. The probe re-reads the store until a deadline —
:data:`_CONFIRM_WINDOW` after the deploy finished (the round row's
``deploy.rollout_at``, else the prod head's ``deployed_at``): a worker
whose newest heartbeat predates the deploy finish is *not yet restarted*,
and one still heartbeating another sha after it is *still rolling*
(gr645561: the restart lags the rollout by a minute or two); both keep
the probe parked (phase ``probe``, ``not_before``-paced like
``ci_red_digest``). Past the deadline anything still not at the release
sha fails, naming each host, its last heartbeat time and its sha.

**An unbounced range.** The deploy's bounce-scope classifier
(``deploy/redeploy-precis.yml`` step 0b2) restarts no worker for a
docs/scripts-only range (scope ``none``) nor for a web-only one (``web``);
those workers heartbeat the previous prod sha for good. ``release_deploy``
reads the classifier's verdict from the deploy log and stamps it with the
previous prod sha on the plan root (``meta.release.deploy_scope``); a
worker reporting exactly that previous sha then counts as confirmed under
such a scope, and the evidence line says so (gr645561). A ``full`` scope,
an unrecorded scope or any other sha is held to the release sha. Hosts with no heartbeat inside the liveness window stay ignored,
as do live hosts outside the cohort — worker-less under unconfigured
membership, or uncovered by a published ``deploy.fleet`` (gr623769,
td649547).

**A moved head** (gr684358). The release branch can move past the plan's
recorded sha after its verdict — a review fix-forward's ``land_slice
--onto`` or a hand push mints no restamp/verdict node — and
``release_deploy`` then ships the live head (its ``wait_ci`` gated that
head green before the rollout). The pin check accepts it when the pinned
sha IS ``release/r<N>``'s live head AND a green gate run is on record for
it — ``release.ci_verdicts``'s newest verdict on the sha (a waived red's
``green`` row counts) or check.yml's newest run on the branch. Anything
unverifiable — an unreadable head, a head that moved again, no green run
— keeps the mismatch a problem: fail closed, never a guessed sha.

**The merge-back goes first.** ``scripts/round deploy --confirm-runtime``
retires the release: it tags ``deployed/r<N>``, merges the release head
into main itself and deletes the branch — and exits 1 on "final forward
merge conflicts" when a release fix and main changed the same lines.
The pass's ``merge_back`` node, dispatched beside this one, is what can
resolve that (a resolver slice, then an ours-merge), but it finished
after the confirm had already failed into a human ask — r335 and r336,
2026-11-19, each on a red that was fixed once on the release branch and
once on main. So an attested release parks (the probe's park) until
``meta.release.merge_back`` reads ``merged`` or ``noop``; the script's
own forward merge then has nothing left to do. The wait is bounded by
:data:`_MERGE_BACK_WAIT`; past it the script runs as before. The
evidence is gathered first either way, so a deploy that did not take
still fails at once.

On ok the job runs ``scripts/round deploy --confirm-runtime <sha>
--runtime-evidence <text>`` (phase ``confirm``), stamps the evidence on
the plan root and ``release.round``
(:func:`precis.plans.release.record_confirm` — the script's own publish
wins where it wrote first), then ``scripts/round close`` (phase
``close``). A failed ``close`` — non-zero exit *or a failed launch* —
still succeeds the job: the release is already confirmed and tagged
``deployed/r<N>`` and ``scripts/release`` retries the close on its next
run.

No URL, host of the health URL or token reaches a log line, job result
or failure reason: a web failure is reported by exception class name or
HTTP status only, never ``str(exc)`` (httpx messages carry the URL).
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis import daemon_fleet as _df
from precis import mcp_served_stamp as _mss
from precis.plans import red_waiver as _red_waiver
from precis.plans import release as _release
from precis.plans.gates import read_ci_verdicts
from precis.utils.secret_scan import mask_secrets
from precis.workers.job_types import release_cut as _rc
from precis.workers.job_types import release_deploy as _rd
from precis.workers.job_types import run_slice as _rs

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

NAME = "release_confirm"

#: The probe and the two scripts are minutes of work; the wall only
#: covers a stuck ``git``/``ssh`` inside ``scripts/round``.
_DEFAULT_WALL_S = 15 * 60

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # The plan's confirm node: the release is read from its root.
        "todo_id": {"type": "integer"},
        # ssh_node's host pin (the host holding the release checkout).
        "target_node": {"type": "string"},
        # The release-pass merge_back node (release-train T3): merge
        # release/r<N> into main instead of attesting the runtime
        # (precis.workers.job_types.release_merge_back).
        "merge_back": {"type": "boolean"},
        # merge_back after a conflict: "ours" records the ancestry only,
        # once the resolver todo named by ``resolver`` landed the content.
        "strategy": {"type": "string", "enum": ["ours"]},
        "resolver": {"type": "integer"},
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
    "Dark release pass, the confirm node: attest the pinned sha is what "
    "runs — every live worker's heartbeat code_sha, and the web's boot "
    "sha when release.health_url is set — then scripts/round deploy "
    "--confirm-runtime and scripts/round close. A failed probe asks, "
    "never rolls back. Refused while PRECIS_FACTORY=off."
)


def wall_seconds() -> float:
    return float(_DEFAULT_WALL_S)


#: ``release.health_url`` — the operator setting naming the live web's
#: ``/healthz``; empty means the web is not attested.
HEALTH_URL_KEY = "release.health_url"

#: The response header ``/healthz`` carries the boot sha in — restated
#: as a literal (``precis_web.bootsha.BOOT_SHA_HEADER``; a test pins
#: them equal, the same repeated-literal arrangement as
#: :data:`precis.plans.release.ROUND_KEY`).
BOOT_SHA_HEADER = "X-Precis-Boot-Sha"

#: The restart-race window: the probe re-reads heartbeats until this long
#: after the deploy finished, then anything still not at the release sha
#: fails the job. ``_REPROBE_EVERY`` paces the parked probe (the
#: ``not_before`` marker, ``ci_red_digest``'s park pattern — no sleep
#: loop in a worker pass).
_CONFIRM_WINDOW = timedelta(minutes=10)
_REPROBE_EVERY = timedelta(seconds=60)

#: How long an attested release holds its retirement script back for the
#: pass's merge-back (module docstring, "The merge-back goes first").
#: Past it the script runs anyway and, on a conflict, fails as it did
#: before the wait existed — the tick's ask goes to a human.
_MERGE_BACK_WAIT = timedelta(hours=2)

#: Timestamp shape ``release.round`` / ``release.prod_head`` and the
#: parked handle carry (``precis.plans.release._stamp``'s, restated).
_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

#: The evidence line's and the failure reason's caps. The reason must
#: fit the retry-cap ask's 600-character context
#: (``plan_dispatch._mint_ask`` copies the newest job_event tail into it,
#: after its own head line).
_TEXT_CAP = 900
_FAILURE_CAP = 560


@dataclass(frozen=True, slots=True)
class Evidence:
    """What :func:`gather_evidence` found: ``ok`` (no problems and no
    hosts still waiting on a restart), the pinned ``sha`` the runtime
    must serve (``None`` when neither the round row nor the prod head
    names one), ``text`` the masked, capped evidence line for
    ``--runtime-evidence``, and ``problems`` — the decisive evidence
    first (a pinned-sha mismatch, mismatching workers and the web line
    before any "no live host" or "unset" line). ``waiting`` names the
    live hosts whose heartbeat predates the deploy finish and whose
    ``deadline`` (the restart-race window's end) has not passed — the
    probe parks on them instead of failing. ``web_only`` is true when
    the web probe's is the ONLY problem — every live worker already
    attests and none is still waiting on a restart — and the job files
    the ruling ask itself (gr607271) instead of dying into the tick's
    retry-cap showstopper. ``noncohort`` names the live hosts that
    attest nothing — no worker evidence under unconfigured membership
    (gr623769), or uncovered by a published ``deploy.fleet`` (td649547):
    reported as a note in ``text``, never a problem and never
    ``waiting``. ``unbounced`` names the workers accepted at the previous
    prod sha because the deploy's bounce scope restarted no worker
    (gr645561). ``mcp_only`` is true when the session MCP's stamp
    (:func:`_mcp_evidence`) is the ONLY problem — the workers all attest —
    and ``mcp_sha`` is the sha it still serves then (gr685985)."""

    ok: bool
    sha: str | None
    text: str
    problems: list[str]
    waiting: tuple[str, ...] = ()
    deadline: datetime | None = None
    web_only: bool = False
    noncohort: tuple[str, ...] = ()
    unbounced: tuple[str, ...] = ()
    mcp_only: bool = False
    mcp_sha: str | None = None


def _same_sha(a: str | None, b: str) -> bool:
    """Whether ``a`` and ``b`` name one commit: equal, or one a prefix of
    the other (a heartbeat may carry the short form); ``a`` of at least
    7 hex characters."""
    if not a or len(a) < 7 or len(b) < 7:
        return False
    return a.startswith(b) or b.startswith(a)


def unbounced_shas(rel: dict[str, Any]) -> list[str]:
    """The prod shas a worker may still report because every deploy since
    it started recorded a bounce scope that restarted no worker (the
    previous prod sha plus the earlier unbounced ones), else ``[]``
    (:func:`precis.workers.job_types.release_deploy._record_deploy_scope`
    writes ``meta.release.deploy_scope``)."""
    ds = rel.get("deploy_scope")
    if not isinstance(ds, dict) or ds.get("scope") not in _rd.UNBOUNCED_WORKER_SCOPES:
        return []
    out: list[str] = []
    for s in [ds.get("prev"), *(ds.get("workers") or [])]:
        s = str(s or "")
        if len(s) >= 7 and s not in out:
            out.append(s)
    return out


def _http_get(url: str) -> Any:
    """Seam: one GET of the health URL — a plain bounded ``httpx`` GET
    (10 s, no redirects) under the politeness core (``release-health``
    policy: a short retry over a transport blip or 429/5xx raise, no
    shared limiter row, no breaker — the confirm tick is the cadence).
    ``release.health_url`` is an operator setting, not an
    agent-supplied URL, so ``safe_fetch``'s SSRF guard is the wrong gate
    and refused the fleet's own address (gr607271); ``http_client`` pins
    every connection through that guard's transport, so this builds a
    bare client. The response (a non-200 comes back as a response, not a
    raise); a raise is a failed probe — the caller reports the exception
    class only (httpx messages carry the URL)."""
    from precis.utils.http import (
        DEFAULT_USER_AGENT,
        call_with_backoff,
        require_httpx,
    )

    httpx = require_httpx()

    def _send() -> Any:
        with httpx.Client(
            timeout=10,
            follow_redirects=False,
            headers={"User-Agent": DEFAULT_USER_AGENT},
        ) as client:
            return client.get(url)

    return call_with_backoff(_send, service="release-health")


def _parse_ts(value: Any) -> datetime | None:
    """``value`` as an aware datetime (the ``_TS_FMT`` / ISO stamps the
    store rows and the parked handle carry, or a timestamptz), else
    ``None``."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _rn_number(rn: object) -> int | None:
    """``7`` for ``"r7"``, else ``None`` (``release._rn_number``'s twin —
    the job keeps its own so it does not reach into the module's
    privates)."""
    m = re.fullmatch(r"r(\d+)", str(rn or ""))
    return int(m.group(1)) if m else None


def _deployed_at(store: Store, rn: str) -> datetime | None:
    """When ``rn``'s deploy finished: the round row's ``deploy.rollout_at``
    when the row is this release, else the prod head's ``deployed_at``
    (``pinned_deploy_sha``'s own fallback order), else ``None``."""
    row = _release.read_round(store)
    n = _rn_number(rn)
    if row is not None and n is not None and row.get("n") == n:
        dep = row.get("deploy")
        if isinstance(dep, dict):
            ts = _parse_ts(dep.get("rollout_at"))
            if ts is not None:
                return ts
    rec = _release.read_prod_head(store)
    return _parse_ts(rec.get("deployed_at")) if rec is not None else None


def _live_fleet(
    store: Store,
) -> tuple[list[tuple[str, datetime | None, str | None]], list[tuple[str, str]]]:
    """``(workers, noncohort)`` of the live heartbeat rows, store only.

    The cohort itself lives in :mod:`precis.daemon_fleet` (gr627361 —
    shared with the machine kind's ``view='daemons'`` read so the MCP
    view can't drift from the evidence this job counts). ``workers`` is
    ``(host, heartbeat ts, code_sha|None)`` per host that attests;
    ``ts`` is the heartbeat's write time, the restart check's clock.
    ``noncohort`` is ``(host, reason)`` per live host that attests
    nothing (gr623769, td649547): reported as a note, never counted."""
    beats, noncohort = _df.live_fleet(store)
    workers = [(b.host, b.ts, b.code_sha) for b in beats]
    return workers, noncohort


def _web_evidence(url: str | None, sha: str) -> tuple[str, str | None]:
    """``(clause, problem)`` for the web half of the evidence.

    ``url`` unset is a clause, never a problem — the workers decide. A
    raise, a non-200 or a missing/mismatching boot-sha header is a
    problem reported without the URL or host."""
    if not url or not url.strip():
        return "not attested (release.health_url unset)", None
    try:
        resp = _http_get(url.strip())
    except Exception as exc:
        return "", f"web unreachable ({type(exc).__name__})"
    if resp.status_code != 200:
        return "", f"web HTTP {resp.status_code}"
    boot = str(resp.headers.get(BOOT_SHA_HEADER) or "").strip()
    if boot == sha:
        return f"at {sha[:12]}", None
    reported = boot[:12] if boot else "no sha"
    return "", f"web reports {reported}, release is {sha[:12]}"


def _mcp_evidence(
    store: Store,
    sha: str,
    prev_shas: list[str],
    *,
    now: datetime,
    deadline: datetime | None,
) -> tuple[str, str | None, bool, str | None]:
    """``(clause, problem, waiting, served)`` for the session MCP half of
    the evidence (gr685985), from the server's own store stamp.

    No stamp, or one older than the worker liveness window, is a clause,
    never a problem — an install without a shared MCP must still confirm.
    A live stamp attests when it serves ``sha`` (or an unbounced previous
    sha), or when the watched checkout is already at ``sha`` and only the
    deferred restart is pending. Any other live stamp is ``waiting``
    before ``deadline`` (like a worker still rolling) and the problem
    after it. ``served`` is the sha the stamp says is served."""
    stamp = _mss.read_stamp(store)
    if stamp is None or now - stamp["at"] > timedelta(minutes=_df.FLEET_FRESH_MIN):
        return "not attested (no live served-sha stamp)", None, False, None
    served = str(stamp["sha"])
    pending = str(stamp.get("pending_sha") or "")
    if _same_sha(served, sha):
        return f"at {sha[:12]}", None, False, served
    if any(_same_sha(served, p) for p in prev_shas):
        return (
            f"at {served[:12]} (previous prod sha, deploy bounced no worker)",
            None,
            False,
            served,
        )
    if pending and _same_sha(pending, sha):
        return (
            f"checkout at {sha[:12]}, restart pending (serving {served[:12]})",
            None,
            False,
            served,
        )
    if deadline is not None and now < deadline:
        return f"serving {served[:12]}, still rolling", None, True, served
    return (
        f"serving {served[:12]}",
        f"session MCP still serves {served[:12]}, release is {sha[:12]} "
        "— pin the prod checkout (scripts/host-ops/pin-prod)",
        False,
        served,
    )


def _fetch_head(repo: str, branch: str) -> str | None:
    """Seam: ``branch``'s live head on GitHub
    (``ci_verdict_poll.fetch_head``); ``None`` when unreadable."""
    from precis.workers.job_types import ci_verdict_poll as _cvp

    return _cvp.fetch_head(repo, branch)


def _gate_run_verdict(repo: str, sha: str, branch: str) -> str | None:
    """Seam: ``green``/``red``/``pending`` of the newest check.yml run on
    ``sha`` on ``branch`` (``ci_verdict_poll.gate_verdict`` — the gate
    legs decide, as they did for the deploy); ``None`` when GitHub is
    unreadable."""
    from precis.workers.job_types import ci_verdict_poll as _cvp

    try:
        runs = _cvp.fetch_runs(repo, sha)
    except Exception:
        return None
    if runs is None:
        return None
    verdict, _run_id, _why = _cvp.gate_verdict(repo, runs, sha, branch)
    return verdict


def _repo_of(store: Store, rel: dict[str, Any]) -> str | None:
    """The GitHub ``owner/repo`` the moved-head check reads — the cut's
    recorded repo, the ``release.repo`` setting, else this checkout's
    origin remote (``resolve_target``'s order), or ``None``."""
    from precis import settings

    repo = _release.valid_repo(rel.get("repo")) or _release.valid_repo(
        settings.get_str("release.repo", store=store)
    )
    if repo:
        return repo
    try:
        return _rc.origin_repo(_rs.slice_root())
    except RuntimeError:
        return None


def _recorded_verdict(store: Store, sha: str) -> str | None:
    """The newest ``release.ci_verdicts`` verdict on ``sha`` (the
    tests-green gate's prefix match — a waived red's ``green`` row is the
    standing decision it is), else ``None``."""
    best: tuple[str, int] | None = None
    verdict: str | None = None
    for i, row in enumerate(read_ci_verdicts(store)):
        if not _same_sha(sha, str(row.get("sha") or "")):
            continue
        key = (str(row.get("at") or ""), i)
        if best is None or key >= best:
            best, verdict = key, str(row.get("verdict") or "")
    return verdict


def _moved_head(store: Store, rel: dict[str, Any], sha: str, rn: str) -> str | None:
    """The acceptance phrase for a pinned ``sha`` that is not the plan's
    release sha, else ``None`` (gr684358).

    The release branch can move past ``meta.release.sha`` after its
    verdict — a review fix-forward's ``land_slice --onto`` or a hand push
    mints no restamp/verdict node — and ``release_deploy`` ships the live
    head, its ``wait_ci`` having gated that head green before the
    rollout. So a pinned sha that IS ``release/<rn>``'s live head and
    carries a green gate run is the release, not a deploy of the wrong
    commit: accepted, with the phrase ("verdict recorded green" /
    "check.yml green") the evidence line reports. ``None`` keeps the
    mismatch a problem — an unverifiable repo or head, a head that moved
    again, or no green run all fail closed."""
    repo = _repo_of(store, rel)
    if repo is None or not rn:
        return None
    branch = _release.release_branch(rn)
    try:
        head = _fetch_head(repo, branch)
    except Exception:
        return None
    if head is None or not _same_sha(sha, head):
        return None
    if _recorded_verdict(store, sha) == "green":
        return "verdict recorded green"
    if _gate_run_verdict(repo, sha, branch) == "green":
        return "check.yml green"
    return None


def gather_evidence(store: Store, rel: dict[str, Any], *, now: datetime) -> Evidence:
    """The runtime attestation for the plan's release ``rel``
    (``meta.release``); ``now`` pins the call's clock seam for tests.

    Required evidence is the workers — the pinned sha
    (:func:`precis.plans.release.pinned_deploy_sha`) must equal every
    live host's heartbeat ``code_sha`` exactly, and it must be the plan's
    own release sha — or the release branch's live head with a green
    gate run when the branch moved past the plan after its verdict
    (:func:`_moved_head`, gr684358). Only a cohort member attests — with a published
    ``deploy.fleet`` ``covered`` list, the configured members alone
    (td649547); without one, a host running workers or carrying a
    pre-snapshot beat (gr623769): a live non-attestor is reported in
    ``text`` as a note, never a problem and never ``waiting``. The
    restart race: a host whose newest heartbeat
    predates the deploy finish (:func:`_deployed_at`) has not restarted
    yet — it joins ``waiting`` until ``deadline`` (the confirm window's
    end) and is a problem only past it; a heartbeat written *after* the
    deploy finish at another sha is still rolling: it waits too
    (gr645561). A worker still on the previous prod sha attests when the
    deploy's bounce scope restarted no worker (:func:`unbounced_shas`). The web is
    optional: ``release.health_url`` unset attests nothing and is not a
    problem. So is the session MCP (:func:`_mcp_evidence`, gr685985): its
    stamp attests, parks the probe while it lags, or is the problem past
    the deadline; no live stamp is a clause only. Any problem or any waiting host makes ``ok`` false."""
    rn = str(rel.get("rN") or "")
    sha = _release.pinned_deploy_sha(store, rn)
    rel_sha = str(rel.get("sha") or "")
    live, noncohort = _live_fleet(store)
    hosts = ", ".join(host for host, _, _ in live)
    deployed_at = _deployed_at(store, rn) if sha is not None else None
    deadline = deployed_at + _CONFIRM_WINDOW if deployed_at is not None else None

    prev_shas = unbounced_shas(rel)
    problems: list[str] = []
    waiting: list[str] = []
    unbounced: list[str] = []
    states: list[str] = []
    moved = ""
    if sha is not None:
        if rel_sha and not _same_sha(sha, rel_sha):
            how = _moved_head(store, rel, sha, rn)
            if how is None:
                problems.append(
                    f"pinned deploy sha {sha[:12]} is not the plan's release "
                    f"sha {rel_sha[:12]}"
                )
            else:
                moved = (
                    f"; {_release.release_branch(rn)} head moved past the "
                    f"plan's {rel_sha[:12]} ({how})"
                )
        for host, ts, code_sha in live:
            reported = code_sha[:12] if code_sha else "no code_sha"
            if any(_same_sha(code_sha, p) for p in prev_shas):
                # The deploy restarted no worker (bounce scope none/web):
                # the previous sha is where this worker stays.
                unbounced.append(host)
                states.append(f"{host} at {reported} (not bounced)")
            elif deployed_at is not None and (ts is None or ts <= deployed_at):
                # The heartbeat predates the deploy finish: the worker
                # has not restarted, its report says nothing yet.
                if deadline is not None and now < deadline:
                    waiting.append(host)
                    states.append(f"{host} not yet restarted")
                else:
                    beat = ts.strftime(_TS_FMT) if ts is not None else "unknown"
                    problems.append(
                        f"worker {host} last heartbeat {beat} reports "
                        f"{reported}, release is {sha[:12]}"
                    )
                    states.append(f"{host} at {reported}")
            elif code_sha != sha:
                if deadline is not None and now < deadline:
                    # Heartbeating after the deploy finish at another sha:
                    # the restart is still rolling (gr645561).
                    waiting.append(host)
                    states.append(f"{host} at {reported}, still rolling")
                else:
                    problems.append(
                        f"worker {host} reports {reported}, release is {sha[:12]}"
                    )
                    states.append(f"{host} at {reported}")
            else:
                states.append(f"{host} at {sha[:12]}")
    from precis import settings

    url = settings.get_str(HEALTH_URL_KEY, store=store) if sha is not None else None
    web_problem: str | None = None
    if sha is not None:
        web_clause, web_problem = _web_evidence(url, sha)
        if web_problem is not None:
            problems.append(web_problem)
    else:
        web_clause = "not attested (no pinned sha)"
    mcp_problem: str | None = None
    mcp_sha: str | None = None
    if sha is not None:
        mcp_clause, mcp_problem, mcp_waits, mcp_sha = _mcp_evidence(
            store, sha, prev_shas, now=now, deadline=deadline
        )
        if mcp_problem is not None:
            problems.append(mcp_problem)
        if mcp_waits:
            waiting.append("session MCP")
    else:
        mcp_clause = "not attested (no pinned sha)"
    if not live:
        problems.append("no live worker host (no heartbeat in the last 60 min)")
    if sha is None:
        problems.append("no pinned deploy sha (release.round and prod head unset)")

    if (
        sha is not None
        and live
        and not waiting
        and not unbounced
        and all(code_sha == sha for _, _, code_sha in live)
    ):
        workers = f"workers: {len(live)} live ({hosts}), all at {sha[:12]}"
    elif sha is not None and live:
        workers = f"workers: {len(live)} live ({', '.join(states)})"
    else:
        workers = f"workers: {len(live)} live ({hosts})"
    short = sha[:12] if sha else "unset"
    note = ""
    if unbounced and prev_shas:
        note = (
            f"; unbounced: {', '.join(unbounced)} still at the previous prod "
            f"sha {', '.join(p[:12] for p in prev_shas)} (deploy bounced no worker)"
        )
    if noncohort:
        note += "; note: " + ", ".join(f"{h} ({why})" for h, why in noncohort)
    text = mask_secrets(
        f"{rn or 'r?'} pinned {short}{moved}; {workers}{note}; web: {web_clause}; "
        f"mcp: {mcp_clause}"
    )[:_TEXT_CAP]
    return Evidence(
        ok=not problems and not waiting,
        sha=sha,
        text=text,
        problems=problems,
        waiting=tuple(waiting),
        deadline=deadline,
        web_only=(web_problem is not None and len(problems) == 1 and not waiting),
        noncohort=tuple(h for h, _why in noncohort),
        unbounced=tuple(unbounced),
        mcp_only=(mcp_problem is not None and len(problems) == 1 and not waiting),
        mcp_sha=mcp_sha if mcp_problem is not None else None,
    )


def failure_reason(ev: Evidence) -> str:
    """The job's failure text after ``release_confirm: probe failed — ``:
    the problems in order, masked, capped so the retry-cap ask's
    600-character context still carries the decisive line."""
    return mask_secrets("; ".join(ev.problems))[:_FAILURE_CAP]


#: ``meta.ask.context`` cap (``todo.guards._ASK_CONTEXT_CAP``, restated —
#: the same 600 the retry-cap ask's context allows).
_ASK_CONTEXT_CAP = 600

#: Options of the ask a web-only probe failure files on the confirm node
#: (gr607271 ruling): the workers already attest — the answer re-arms
#: the node for a re-probe; ``won't do`` drops it.
_WEB_ASK_OPTIONS: list[str] = [
    "retry the web probe",
    "won't do",
]


#: Options of the ask an MCP-only probe failure files (gr685985).
_MCP_ASK_OPTIONS: list[str] = [
    "retry the MCP check",
    "won't do",
]


def _mint_probe_ask(
    store: Store,
    todo_id: int,
    ev: Evidence,
    *,
    head: str,
    title: str,
    body: str,
    options: list[str],
) -> int:
    """The ruling ask a lone probe failure files on the confirm node: the
    workers already attest, so the ask carries the evidence the tick's
    retry-cap mint would not, and its ``blocked-by`` edge suppresses that
    mint (``_Plan.open_asks``); answering re-arms ``todo_id`` with a fresh
    retry budget for the re-probe. Insert shape of
    ``plan_dispatch._mint_ask``; returns the ask's todo id."""
    from precis.plans.nodes import finish_mint
    from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
    from precis.todo.guards import check_ask_meta

    root = _release.plan_root(store, todo_id)
    tail = failure_reason(ev)
    context = f"{head}: {tail[: _ASK_CONTEXT_CAP - len(head) - 3]}"
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(options),
            "blocks": f"td{todo_id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=todo_id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
        finish_mint(store, ref.id, conn=conn)
    return int(ref.id)


def _mint_web_ask(store: Store, todo_id: int, rn: str, ev: Evidence) -> int:
    """The ruling ask a web-only probe failure files on the confirm node
    (gr607271 ruling): every live worker already attests the sha — the
    workers decide — and only the operator-set ``release.health_url``
    probe failed."""
    short = ev.sha[:12] if ev.sha else "unset"
    tail = failure_reason(ev)
    title = (
        f"{rn or 'release'}: workers attest {short} but the web probe "
        "failed — fix or clear release.health_url, or drop the confirm?"
    )
    body = (
        f"{ev.text}\n\nEvery live worker host's heartbeat already reports "
        "the release sha — the workers decide — but the probe of the "
        f"operator-set release.health_url failed ({tail}). Fix the web or "
        "the setting (scripts/prod-precis settings set release.health_url "
        "<url>; clearing it attests the workers only — the URL itself is "
        "never printed), then answer 'retry the web probe' and close "
        f"this todo: closing re-arms td{todo_id} with a fresh retry "
        "budget and the confirm re-probes. Pick 'won't do' by closing "
        f"td{todo_id} STATUS:won't-do — the release then stays "
        "unconfirmed until a hand `scripts/round deploy "
        "--confirm-runtime … && scripts/round close` lands."
    )
    return _mint_probe_ask(
        store,
        todo_id,
        ev,
        head=f"{NAME}: {rn or 'r?'} workers all at {short}; web probe failed",
        title=title,
        body=body,
        options=_WEB_ASK_OPTIONS,
    )


def _mint_mcp_ask(store: Store, todo_id: int, rn: str, ev: Evidence) -> int:
    """The ruling ask an MCP-only probe failure files on the confirm node
    (gr685985): every live worker attests the sha and only the shared
    session MCP's served-sha stamp still names another one — its watched
    checkout was not pinned. The one step is ``scripts/host-ops/pin-prod``
    by whoever owns the prod checkout; answering re-arms the confirm."""
    short = ev.sha[:12] if ev.sha else "unset"
    served = (ev.mcp_sha or "unknown")[:12]
    title = (
        f"{rn or 'release'}: session MCP still serves {served}, release is "
        f"{short} — pin the prod checkout, then answer"
    )
    body = (
        f"{ev.text}\n\nThe workers are confirmed: every live worker host "
        f"already reports {short}. Only the shared session MCP still serves "
        f"{served} — its watched checkout has not been pinned to the "
        "release. As the owner of the prod checkout run "
        "scripts/host-ops/pin-prod, then answer 'retry the MCP check' and "
        f"close this todo: closing re-arms td{todo_id} with a fresh retry "
        "budget and the confirm re-reads the server's stamp. Pick 'won't "
        f"do' by closing td{todo_id} STATUS:won't-do — the release then "
        "stays unconfirmed until a hand `scripts/round deploy "
        "--confirm-runtime … && scripts/round close` lands."
    )
    return _mint_probe_ask(
        store,
        todo_id,
        ev,
        head=f"{NAME}: {rn or 'r?'} workers all at {short}; session MCP serves {served}",
        title=title,
        body=body,
        options=_MCP_ASK_OPTIONS,
    )


def _probe_failed(ctx: Any, ev: Evidence, rn: str, todo_id: Any) -> None:
    """The probe failed. A web-only failure — every live worker already
    attests, only the optional web half did — files the ruling ask
    itself (gr607271) and the failure names it; so does an MCP-only one
    (the session MCP still serves another sha, gr685985); the tick's
    ``open_asks`` check then never mints its retry-cap showstopper. Any
    other problem — or a failed ask mint — is a plain failure and the
    tick asks."""
    mint = _mint_web_ask if ev.web_only else _mint_mcp_ask if ev.mcp_only else None
    if mint is not None and isinstance(todo_id, int):
        try:
            ask_id = mint(ctx.store, todo_id, rn, ev)
        except Exception:
            log.warning("%s: ask mint failed", NAME, exc_info=True)
        else:
            ctx.record_failure(
                f"{NAME}: probe failed — {failure_reason(ev)}; asked in td{ask_id}"
            )
            return
    ctx.record_failure(f"{NAME}: probe failed — {failure_reason(ev)}")


# ── the job ─────────────────────────────────────────────────────────


def _refusal(
    store: Any, params: dict[str, Any], *, running: bool = False
) -> tuple[str | None, str | None]:
    """``(refusal, rN)`` — ``release_deploy._refusal`` without the
    approval gate: the confirm sits where the cut ran
    (``meta.release.host`` / ``checkout``), pinned at dispatch by
    ``params.target_node`` and once running by this worker's own node
    and checkout."""
    todo_id = params.get("todo_id")
    reason = _rc.factory_refusal(NAME) or _rc.todo_refusal(
        store, NAME, todo_id, required=True
    )
    if reason is not None:
        return reason, None
    if store is None or not isinstance(todo_id, int):
        return None, None
    rel = _release.release_of(store, todo_id)
    if rel is None:
        return (
            f"{NAME}: no release on td{_release.plan_root(store, todo_id)}'s "
            "plan root (meta.release)",
            None,
        )
    host = os.environ.get("PRECIS_NODE") if running else params.get("target_node")
    checkout = None
    if running:
        try:
            checkout = str(_rs.slice_root())
        except RuntimeError as exc:
            return f"{NAME}: {exc}", None
    pin = _release.pin_refusal(rel, host=host or None, checkout=checkout)
    if pin is not None:
        return f"{NAME}: {pin}", None
    return None, str(rel["rN"])


def validate_submit(
    store: Any, *, gripe_id: int | None = None, params: dict[str, Any]
) -> str | None:
    del gripe_id
    return _refusal(store, params)[0]


def _launch_confirm(ctx: Any, rn: str, ev: Evidence) -> dict[str, Any] | None:
    """The launch half of the probe's verdict: stamp the release and the
    evidence on the job, run ``scripts/round deploy --confirm-runtime``;
    the handle carries ``phase``/``rn``/``sha``/``evidence``."""
    ctx.set_meta(release=rn, confirm_evidence=ev.text)
    handle = _rc.launch_script(
        ctx,
        NAME,
        [
            "deploy",
            "--confirm-runtime",
            str(ev.sha),
            "--runtime-evidence",
            ev.text,
        ],
        script="round",
        env=_rc.pass_env(ctx),
    )
    if isinstance(handle, dict):
        handle.update(phase="confirm", rn=rn, sha=ev.sha, evidence=ev.text)
    return handle


def _park_probe(
    ctx: Any, ev: Evidence, rn: str, now: datetime, handle: dict[str, Any] | None = None
) -> dict[str, Any]:
    """The probe is waiting on hosts that have not restarted since the
    deploy finished: park the job on ``not_before`` (crd's park pattern —
    the handle is mutated in place and persisted as ``meta.compute_handle``)
    and re-gather the evidence then; ``deadline`` is the restart-race
    window's end, the wall covers the wait plus one run."""
    due = now + _REPROBE_EVERY
    if ev.deadline is not None and ev.deadline < due:
        due = ev.deadline
    fields: dict[str, Any] = {
        "phase": "probe",
        "rn": rn,
        "sha": ev.sha,
        "not_before": due.strftime(_TS_FMT),
        "deadline": ev.deadline.strftime(_TS_FMT) if ev.deadline else "",
    }
    if handle is None:
        handle = dict(fields)
    else:
        handle.update(fields)
    note = (
        f"{NAME}: waiting on {', '.join(ev.waiting)} to restart — "
        f"re-probe after {due:%H:%M}Z"
    )
    if ev.deadline is not None:
        note += f" (deadline {ev.deadline:%H:%M}Z)"
    ctx.append_chunk("job_event", note)
    wait = (
        (ev.deadline - now).total_seconds()
        if ev.deadline is not None
        else _REPROBE_EVERY.total_seconds()
    )
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + wait + wall_seconds(),
    )
    return handle


def _merge_back_settled(rel: dict[str, Any]) -> bool:
    """The pass's merge-back put the release head on main, or had nothing
    to put there: ``meta.release.merge_back`` reads ``merged``/``noop``."""
    from precis.workers.job_types import release_merge_back as _mb

    outcome = rel.get(_mb.MERGE_BACK_KEY)
    return isinstance(outcome, dict) and outcome.get("state") in ("merged", "noop")


def _merge_back_wait(
    rel: dict[str, Any], handle: dict[str, Any] | None, now: datetime
) -> str | None:
    """Why the retirement script must not run yet, else ``None``: the
    pass's merge-back is not settled (:func:`_merge_back_settled`) and
    the wait — clocked from the parked handle's ``merge_wait_since`` —
    is inside :data:`_MERGE_BACK_WAIT`."""
    from precis.workers.job_types import release_merge_back as _mb

    if _merge_back_settled(rel):
        return None
    outcome = rel.get(_mb.MERGE_BACK_KEY)
    state = outcome.get("state") if isinstance(outcome, dict) else None
    since = None
    if handle is not None:
        since = _parse_ts(handle.get("merge_wait_since"))
    if since is not None and now - since >= _MERGE_BACK_WAIT:
        return None
    if state == "conflict" and isinstance(outcome, dict):
        return (
            "the merge-back hit a conflict and its resolver "
            f"td{outcome.get('resolver')} has not put the release on main yet"
        )
    return "the merge-back has not put the release head on main yet"


def _park_merge_back(
    ctx: Any,
    ev: Evidence,
    rn: str,
    now: datetime,
    why: str,
    handle: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The runtime is attested but the merge-back is not settled: park
    like :func:`_park_probe` and look again in :data:`_REPROBE_EVERY`.
    One ``job_event`` per distinct reason, not one per look."""
    due = now + _REPROBE_EVERY
    fields: dict[str, Any] = {
        "phase": "probe",
        "rn": rn,
        "sha": ev.sha,
        "not_before": due.strftime(_TS_FMT),
        "deadline": "",
    }
    if handle is None:
        handle = dict(fields)
    else:
        handle.update(fields)
    handle.setdefault("merge_wait_since", now.strftime(_TS_FMT))
    if handle.get("merge_wait_why") != why:
        handle["merge_wait_why"] = why
        ctx.append_chunk(
            "job_event",
            f"{NAME}: {rn} is attested at {str(ev.sha)[:12]}; holding "
            f"scripts/round deploy --confirm-runtime — {why} (its own "
            "forward merge would stop on the same conflict)",
        )
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + _REPROBE_EVERY.total_seconds() + wall_seconds(),
    )
    return handle


def submit(ctx: Any, spec: Any) -> Any:
    del spec
    params = dict((ctx.meta or {}).get("params") or {})
    reason, rn = _refusal(ctx.store, params, running=True)
    if reason is not None or rn is None:
        ctx.record_failure(reason or f"{NAME}: no release on the plan root")
        return None
    if params.get("merge_back"):
        # release-train T3: the merge-back mode — synchronous, terminal.
        from precis.workers.job_types import release_merge_back as _mb

        _mb.run(ctx, params)
        return None
    rel = _release.release_of(ctx.store, int(params["todo_id"])) or {}
    now = datetime.now(UTC)
    ev = gather_evidence(ctx.store, rel, now=now)
    if ev.ok and ev.sha is not None:
        why = _merge_back_wait(rel, None, now)
        if why is not None:
            return _park_merge_back(ctx, ev, rn, now, why)
        return _launch_confirm(ctx, rn, ev)
    if ev.problems:
        _probe_failed(ctx, ev, rn, params.get("todo_id"))
        return None
    return _park_probe(ctx, ev, rn, now)


def _probe_poll(ctx: Any, handle: dict[str, Any]) -> bool:
    """The parked probe: ``False`` before ``not_before``; at or after it,
    re-gather the evidence — ok launches the confirm (the handle swaps in
    place), problems fail, still waiting re-parks."""
    try:
        due = datetime.strptime(str(handle["not_before"]), _TS_FMT).replace(tzinfo=UTC)
    except (KeyError, ValueError):
        due = datetime.now(UTC)  # unreadable marker: do not park forever
    now = datetime.now(UTC)
    if now < due:
        return False
    params = dict((ctx.meta or {}).get("params") or {})
    todo_id = params.get("todo_id")
    rel = (
        _release.release_of(ctx.store, int(todo_id))
        if isinstance(todo_id, int)
        else None
    )
    if rel is None:
        ctx.record_failure(f"{NAME}: no release on the plan root")
        return True
    ev = gather_evidence(ctx.store, rel, now=now)
    if ev.ok and ev.sha is not None:
        rn = str(handle.get("rn") or rel["rN"])
        why = _merge_back_wait(rel, handle, now)
        if why is not None:
            _park_merge_back(ctx, ev, rn, now, why, handle)
            return False
        if handle.get("merge_wait_since") and not _merge_back_settled(rel):
            ctx.append_chunk(
                "job_event",
                f"{NAME}: the merge-back is still not settled after "
                f"{int(_MERGE_BACK_WAIT.total_seconds() // 3600)} h — running "
                "scripts/round deploy --confirm-runtime anyway",
            )
        fresh = _launch_confirm(ctx, rn, ev)
        if fresh is None:
            return True
        handle.clear()
        handle.update(fresh)
        ctx.set_meta(
            compute_handle=handle,
            compute_handle_released=False,
            deadline=time.time() + wall_seconds(),
        )
        return False
    if ev.problems:
        _probe_failed(ctx, ev, str(handle.get("rn") or rel["rN"]), todo_id)
        return True
    _park_probe(ctx, ev, str(handle.get("rn") or rel["rN"]), now, handle)
    return False


def _confirmed(ctx: Any, handle: dict[str, Any]) -> bool:
    """Exit 0 of ``round deploy --confirm-runtime``: merge the
    confirmation into the store (the script's own publish wins where it
    wrote first) and swap the handle in place onto ``scripts/round
    close`` — ``ci_red_digest._relaunch_when_due``'s pattern: a fresh
    handle persisted as ``compute_handle`` with a fresh ``deadline``."""
    params = dict((ctx.meta or {}).get("params") or {})
    todo_id = params.get("todo_id")
    notes: list[str] = []
    if isinstance(todo_id, int):
        try:
            notes = _release.record_confirm(
                ctx.store,
                todo_id,
                str(handle.get("sha") or ""),
                str(handle.get("evidence") or ""),
            )
        except Exception as exc:
            log.warning("%s: record_confirm failed", NAME, exc_info=True)
            notes = [f"record_confirm failed: {type(exc).__name__}"]
    ctx.append_chunk(
        "job_event",
        f"{NAME}: confirmed {str(handle.get('sha') or '')[:12]}"
        + ("" if not notes else " — " + "; ".join(notes)),
    )
    fresh = _launch_close(ctx)
    if fresh is None:
        return _closed(
            ctx,
            handle,
            exit_code=None,
            close_problem="scripts/round close failed to launch",
        )
    keep = {k: handle.get(k) for k in ("rn", "sha", "evidence")}
    handle.clear()
    handle.update(fresh)
    handle.update(keep)
    handle["phase"] = "close"
    ctx.set_meta(
        compute_handle=handle,
        compute_handle_released=False,
        deadline=time.time() + wall_seconds(),
    )
    return False


def _launch_close(ctx: Any) -> dict[str, Any] | None:
    """``scripts/round close`` detached — ``_rc.launch_script``'s
    mechanics, except a failed launch is a job_event note, never a job
    failure: the release is already confirmed and tagged ``deployed/r<N>``
    and ``scripts/release`` retries the close on its next run."""
    try:
        root = _rs.slice_root()
        path = root / "scripts" / "round"
        if not path.exists():
            raise RuntimeError(f"no scripts/round in {root}")
        base = _rc._run_dir() / f"{NAME}-{ctx.ref_id}-close"
        log_path = base.with_suffix(".log")
        exit_file = base.with_suffix(".exit")
        exit_file.unlink(missing_ok=True)
        pid = _rc._launch(
            [str(path), "close"],
            cwd=root,
            log_path=log_path,
            exit_file=exit_file,
            extra_env=_rc.pass_env(ctx),
        )
    except Exception as exc:
        log.warning("%s: scripts/round close failed to launch", NAME, exc_info=True)
        ctx.append_chunk(
            "job_event", f"{NAME}: scripts/round close failed to launch: {exc}"
        )
        return None
    ctx.append_chunk(
        "job_event",
        f"{NAME}: launched scripts/round close pid {pid} in {root}",
    )
    handle: dict[str, Any] = {
        "pid": pid,
        "pgid": pid,
        "root": str(root),
        "log": str(log_path),
        "exit_file": str(exit_file),
        "started_at": datetime.now(UTC).strftime(_TS_FMT),
    }
    identity = _rs._process_identity(pid)
    if identity is not None:
        handle["boot_id"], handle["start_time"] = identity
    return handle


def _closed(
    ctx: Any,
    handle: dict[str, Any],
    exit_code: int | None,
    close_problem: str | None = None,
) -> bool:
    """The ``close`` phase's verdict: exit 0 closed the round; any other
    exit — or a close that never launched — still succeeds: the release
    is already confirmed and tagged ``deployed/r<N>`` and
    ``scripts/release`` retries the close on its next run (its
    close-retry branch)."""
    rn = str(handle.get("rn") or "r?")
    sha = str(handle.get("sha") or "")
    lines = [f"{NAME}: {rn} confirmed at {sha[:12]} — {handle.get('evidence') or ''}"]
    if close_problem is not None:
        lines.append(
            f"{close_problem} — {rn} is already confirmed and tagged "
            f"deployed/{rn}; scripts/release retries the close on its next run"
        )
    elif exit_code == 0:
        lines.append("scripts/round close: round closed")
    else:
        lines.append(
            f"scripts/round close exit {exit_code} — {rn} is already "
            f"confirmed and tagged deployed/{rn}; scripts/release retries "
            "the close on its next run"
        )
    lines.extend(_waiver_reverts(ctx, rn))
    ctx.append_chunk("job_summary", "\n".join(lines))
    ctx.set_meta(release_confirm={"rN": rn, "sha": sha})
    ctx.set_status("succeeded")
    return True


def _waiver_reverts(ctx: Any, rn: str) -> list[str]:
    """The red-test waiver's safety net (:mod:`precis.plans.red_waiver`):
    this release confirmed, so any earlier release's waiver gripe still
    open gets its culprit-revert chain (once per gripe). Summary lines;
    best-effort — a fault here never fails the confirm."""
    try:
        minted = _red_waiver.mint_reverts(ctx.store, rn)
    except Exception:
        log.warning("%s: waiver revert mint failed", NAME, exc_info=True)
        return ["red-waiver: revert mint for unfixed waivers failed (see worker log)"]
    return [
        f"red-waiver: gr{gid} is still not done — minted td{root} "
        f"'Revert the culprits of gr{gid}'"
        for gid, root in minted
    ]


def poll(ctx: Any, handle: Any) -> bool:
    if isinstance(handle, dict) and handle.get("phase") == "probe":
        return _probe_poll(ctx, handle)  # parked probe: no pid/exit_file
    done = _rc.finish_run(ctx, handle, NAME)
    if done is None:
        return not isinstance(handle, dict)
    exit_code, log_text = done
    phase = str(handle.get("phase") or "confirm")
    if phase == "confirm":
        if exit_code != 0:
            # Exit 3 is the script refusing the evidence (a rollout done
            # but this confirmation not accepted), 1 a hard refusal.
            _rc.fail_with_tail(
                ctx,
                f"{NAME}: scripts/round deploy --confirm-runtime exit {exit_code}",
                mask_secrets(log_text),
            )
            return True
        return _confirmed(ctx, handle)
    return _closed(ctx, handle, exit_code)


kill = _rc.kill


__all__ = [
    "BOOT_SHA_HEADER",
    "COMPATIBLE_EXECUTORS",
    "DESCRIPTION",
    "HEALTH_URL_KEY",
    "NAME",
    "PARAMS_SCHEMA",
    "REQUIRES",
    "Evidence",
    "failure_reason",
    "gather_evidence",
    "kill",
    "poll",
    "submit",
    "validate_submit",
    "wall_seconds",
]
