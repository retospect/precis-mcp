"""devin_api executor — mint a cloud Devin session, poll it, collect.

Sibling of :mod:`claude_docker` / :mod:`ssh_node` — the detached
submit/poll shape, except the remote handle is a Devin API v3 session
instead of a container or a compute node. ``devin_task`` is the only
job_type it drains (gr543831): a ``put(kind='job',
job_type='devin_task', executor='devin_api', params={'prompt': …})``
lands ``STATUS:queued``; this pass claims it, POSTs
``/v3/organizations/<org>/sessions``, records the session id, polls it
each tick, and drives the job to ``succeeded`` / ``failed`` / parked
(``STATUS:waiting_time``) — the "clean paused" terminal the gripe asks
for on quota exhaustion.

**Official door only.** All traffic is REST against
``https://api.devin.ai`` with a ``cog_`` service-user or personal access
token resolved fresh per call from ``DEVIN_API_TOKEN``
(:func:`precis.secrets.get_secret` — env leg first, then the vault; the
plaintext never lands on the job row, a chunk, or a log line). No
reverse-engineered proxies, no CLI login reuse — Reto 2026-10-31 ruling
on the gripe. The org id is non-secret config: ``PRECIS_DEVIN_ORG_ID``
env, else the ``devin.org_id`` ``app_state`` row, then the vault entry
``PRECIS_DEVIN_ORG_ID`` so operators can configure it on ``/secrets``.

**Not a router rung.** Devin sessions are tasks, not completions — this
executor never touches the ``llm.op`` chains. It is in
:data:`EXECUTOR_PROVIDES` with an empty capability set and in
:data:`ZERO_LLM_EXECUTORS` (it writes no ``llm_call_log`` rows; the
spend is provider-side ACUs).

**Ships dark.** The ``job_devin_api`` ServiceSpec carries
``enable_env`` but no ``default_profiles``: the pass registers
structurally and is gated off every cycle until a ``service_config``
row lights it — ``precis service prio '*' job_devin_api 1``. The
``--only job_devin_api`` path passes ``enabled=True`` (the operator
escape hatch). Without a token or an org id the pass no-ops quietly —
claimed jobs stay queued for the operator, never failed for our config
gap.

**Quota.** A session suspended with a quota-class ``status_detail``
(``out_of_quota``, ``out_of_credits``, ``usage_limit_exceeded``, …)
parks its job until the next UTC-day quota window
(:func:`park_job_until`, capped by ``MAX_PAUSE_PARKS``) and parks the
*lane* too: ``devin_exec:paused_until`` in ``app_state`` holds new
claims until that instant — paid plans refresh on daily/weekly
allowances and we never buy overage. A session blocked on human input
(``waiting_for_user``/``waiting_for_approval``/``inactivity``) parks
the job for ``PAUSE_BACKOFF`` without pausing the lane.

**Usage.** Each poll mirrors the provider's usage fields onto the job:
``meta.devin`` (session id, url, status, status_detail, pull_requests,
structured_output) and ``meta.usage`` (``acus_consumed``,
``on_demand_usd``, ``checked_at``) — the per-job usage field the gripe
asks for.

**Trust.** ``meta.devin`` is provider-reported *evidence*: status and
PR links straight from the session object, ``structured_output``
verbatim. Builder-lane rules carry over — a consumer verifies every
claimed commit by sha before trusting it; the executor never hands the
session a prod-write credential and never will.

**Failure classes.** A 402/403-class refusal on create or poll fails
the job ``failure_class='config'`` with an explicit "plan lacks Devin
API" line (whether our plan includes the API is an open question —
the gripe's first step). A 401 means the token is rejected — config
too. A 404 poll means the session vanished — plain failed. Transient
transport/rate-limit outcomes neither fail nor heartbeat the job; the
next tick retries the poll.
"""

from __future__ import annotations

import logging
import os
import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Protocol

from precis.utils.http import VendorError, VendorUnavailable, with_backoff
from precis.utils.rate_limit import RateLimited
from precis.workers.executors._common import (
    FAILED as _FAILED,
)
from precis.workers.executors._common import (
    JOB_EVENT_KIND as _JOB_EVENT_KIND,
)
from precis.workers.executors._common import (
    JOB_SUMMARY_KIND as _JOB_SUMMARY_KIND,
)
from precis.workers.executors._common import (
    QUEUED as _QUEUED,
)
from precis.workers.executors._common import (
    RUNNING as _RUNNING,
)
from precis.workers.executors._common import (
    SUCCEEDED as _SUCCEEDED,
)
from precis.workers.executors._common import (
    append_chunk as _append_chunk,
)
from precis.workers.executors._common import (
    claim_executor_jobs,
)
from precis.workers.executors._common import (
    park_job_until as _park_job_until,
)
from precis.workers.executors._common import (
    poison_guard as _poison_guard,
)
from precis.workers.executors._common import (
    release_job_reservation as _release_job_reservation,
)
from precis.workers.executors._common import (
    renew_lease_if_mine as _renew_lease_if_mine,
)
from precis.workers.executors._common import (
    set_meta as _set_meta,
)
from precis.workers.executors._common import (
    set_status as _set_status,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_EXECUTOR_NAME = "devin_api"

#: The ``service_config`` service name an operator lights (``prio > 0``)
#: to arm the lane — same name as the ServiceSpec, so
#: ``precis service prio '*' job_devin_api 1`` flips both the per-cycle
#: pass gate and this module's own dark check in one write.
SERVICE_NAME = "job_devin_api"

_SECRET_NAME = "DEVIN_API_TOKEN"
_ORG_ENV = "PRECIS_DEVIN_ORG_ID"
_ORG_SETTING_KEY = "devin.org_id"
_BASE_ENV = "PRECIS_DEVIN_API_BASE"
_DEFAULT_BASE = "https://api.devin.ai"

#: ``app_state`` key holding the lane's quota-pause expiry (ISO UTC).
_PAUSED_KEY = "devin_exec:paused_until"

#: Poll-path lease (seconds): renewed while a session is still in
#: flight, generous over the pass cadence so a legit long session never
#: trips the stuck-job sweeper.
_LEASE_S = 3600

#: Default session wall-clock budget when params omit
#: ``resources.wall_seconds`` — six hours, generous over a normal task.
_DEFAULT_WALL_S = 6 * 3600

#: Max concurrent minted-but-unresolved sessions per pass tick.
_DEFAULT_CONCURRENCY = 2
_CONCURRENCY_ENV = "PRECIS_DEVIN_CONCURRENCY"

#: Devin ``status`` values meaning the session is still in flight.
_IN_FLIGHT = frozenset({"new", "claimed", "running", "resuming"})

#: ``status_detail`` values meaning quota/credit exhaustion — the job
#: parks to the next UTC-day quota window AND the lane pauses (no new
#: mints) until then. Never auto-buy overage.
_QUOTA_DETAILS = frozenset(
    {
        "usage_limit_exceeded",
        "out_of_credits",
        "out_of_quota",
        "no_quota_allocation",
        "payment_declined",
        "org_usage_limit_exceeded",
        "user_usage_limit_exceeded",
        "contract_expired",
    }
)

#: ``status_detail`` values meaning the session is parked on human
#: input we cannot give — park the job, do NOT pause the lane.
_BLOCKED_DETAILS = frozenset(
    {"waiting_for_user", "waiting_for_approval", "inactivity", "user_request"}
)

_HTTP_TIMEOUT_S = 30.0


class SessionAPI(Protocol):
    """The injectable seam every test uses — no network anywhere.

    Methods return the decoded session dict on 2xx and raise
    :class:`VendorError` (``status`` set) on any non-2xx, or
    :class:`VendorUnavailable` / ``RateLimited`` on transient
    conditions — the same contract ``with_backoff`` gives the real
    transport below.
    """

    def create_session(
        self, org_id: str, payload: dict[str, Any]
    ) -> dict[str, Any]: ...

    def get_session(self, org_id: str, devin_id: str) -> dict[str, Any]: ...

    def terminate_session(self, org_id: str, devin_id: str) -> None: ...


class _HttpxSessionAPI:
    """The real transport — ``with_backoff`` around every send."""

    def __init__(self, token: str, base_url: str) -> None:
        self._token = token
        self._base = base_url.rstrip("/")

    def _url(self, org_id: str, devin_id: str | None = None) -> str:
        url = f"{self._base}/v3/organizations/{org_id}/sessions"
        return f"{url}/{devin_id}" if devin_id else url

    def _send(
        self,
        method: str,
        url: str,
        *,
        payload: dict[str, Any] | None = None,
        idempotent: bool,
    ) -> dict[str, Any]:
        from precis.utils.http import http_client

        def _do() -> Any:
            with http_client(
                timeout=_HTTP_TIMEOUT_S,
                headers={"Authorization": f"Bearer {self._token}"},
            ) as client:
                return client.request(method, url, json=payload)

        resp = with_backoff(
            _do,
            service="devin",
            url=url,
            idempotent=idempotent,
        )
        if resp.status_code == 200:
            body = resp.json()
            return body if isinstance(body, dict) else {}
        raise VendorError(
            "devin",
            f"{method} {url} -> HTTP {resp.status_code}",
            status=resp.status_code,
        )

    def create_session(self, org_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        # Non-idempotent: only connect-phase errors and 429 may retry —
        # a read timeout or 5xx may follow a server-side session commit.
        return self._send("POST", self._url(org_id), payload=payload, idempotent=False)

    def get_session(self, org_id: str, devin_id: str) -> dict[str, Any]:
        return self._send("GET", self._url(org_id, devin_id), idempotent=True)

    def terminate_session(self, org_id: str, devin_id: str) -> None:
        try:
            self._send("DELETE", self._url(org_id, devin_id), idempotent=True)
        except Exception:
            log.warning(
                "devin_api: session terminate %s raised (best-effort)",
                devin_id,
                exc_info=True,
            )


def devin_exec_enabled(store: Store, *, conn: Any = None) -> bool:
    """True iff a ``service_config`` row lights ``job_devin_api``.
    Never raises — an unreadable gate must stay dark. Mirrors
    ``citer_watch_enabled``'s gate shape."""

    def _q(c: Any) -> bool:
        row = c.execute(
            "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
            (SERVICE_NAME,),
        ).fetchone()
        return row is not None

    try:
        if conn is not None:
            return _q(conn)
        with store.pool.connection() as c:
            return _q(c)
    except Exception:
        log.warning("devin_api: service_config lookup failed", exc_info=True)
        return False


def _resolve_org(store: Store) -> str | None:
    """The org id — ``PRECIS_DEVIN_ORG_ID`` env, else the
    ``devin.org_id`` app_state row, then the vault. Non-secret config."""
    from precis import secrets as _secrets

    env = os.environ.get(_ORG_ENV)
    if env and env.strip():
        return env.strip()
    try:
        raw = store.get_setting(_ORG_SETTING_KEY)
    except Exception:
        raw = None
    if raw and raw.strip():
        return raw.strip()
    raw = _secrets.get_secret(_ORG_ENV)
    return raw.strip() if raw and raw.strip() else None


def _paused_until(store: Store, now: datetime) -> bool:
    """True while the lane's quota pause (``devin_exec:paused_until``)
    is still in the future — stops NEW mints, not in-flight polls."""
    try:
        raw = store.get_setting(_PAUSED_KEY)
    except Exception:
        return False
    if not raw:
        return False
    try:
        until = datetime.fromisoformat(raw)
    except ValueError:
        return False
    if until.tzinfo is None:
        until = until.replace(tzinfo=UTC)
    return until > now


def _next_quota_window(now: datetime) -> datetime:
    """Next UTC-day boundary — the daily-allowance refresh instant
    (paid plans refresh daily/weekly; the daily window is the earliest
    the lane may mint again)."""
    return (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)


def _wall_seconds(meta: dict[str, Any]) -> int:
    params = meta.get("params") or {}
    resources = params.get("resources") if isinstance(params, dict) else None
    wall = resources.get("wall_seconds") if isinstance(resources, dict) else None
    if isinstance(wall, int) and not isinstance(wall, bool) and wall > 0:
        return wall
    return _DEFAULT_WALL_S


def _concurrency() -> int:
    try:
        n = int(os.environ.get(_CONCURRENCY_ENV, str(_DEFAULT_CONCURRENCY)))
    except ValueError:
        return _DEFAULT_CONCURRENCY
    return max(1, min(8, n))


def _usage_meta(session: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The per-job usage field — provider-reported ACUs (+ Teams
    overage dollars when the plan exposes them)."""
    return {
        "acus_consumed": session.get("acus_consumed"),
        "on_demand_usd": session.get("on_demand_usage"),
        "checked_at": now.isoformat(),
    }


def _devin_meta(session: dict[str, Any]) -> dict[str, Any]:
    """Provider-reported session evidence mirrored onto the job."""
    prs = session.get("pull_requests")
    return {
        "session_id": session.get("session_id"),
        "url": session.get("url"),
        "status": session.get("status"),
        "status_detail": session.get("status_detail"),
        "pull_requests": [
            {"pr_url": p.get("pr_url"), "pr_state": p.get("pr_state")}
            for p in prs
            if isinstance(p, dict)
        ]
        if isinstance(prs, list)
        else [],
        "structured_output": session.get("structured_output"),
        "devin_mode": session.get("devin_mode"),
    }


def _fail(
    store: Store,
    ref_id: int,
    reason: str,
    *,
    failure_class: str | None = None,
) -> None:
    """Terminal fail before/without a session — event chunk +
    STATUS:failed (+ optional ``failure_class`` meta) + failure bubble.
    Mirrors ``claude_docker._fail``."""
    with store.pool.connection() as conn:
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, reason, conn=conn)
        if failure_class is not None:
            _set_meta(conn, ref_id, failure_class=failure_class, error=reason[:500])
        _set_status(store, ref_id, _FAILED, conn=conn)
        conn.commit()
    from precis.utils.job_bubble import bubble_job_failure

    bubble_job_failure(store, ref_id)


def _succeed(
    store: Store,
    ref_id: int,
    session: dict[str, Any],
    now: datetime,
) -> None:
    """Terminal success — final devin/usage mirror + job_summary chunk
    + STATUS:succeeded. The recorded fields are provider-reported
    evidence; a consumer still verifies any claimed commit by sha."""
    devin = _devin_meta(session)
    prs = devin["pull_requests"]
    pr_note = " PRs: " + ", ".join(str(p["pr_url"]) for p in prs) if prs else ""
    acus = session.get("acus_consumed")
    with store.pool.connection() as conn:
        _set_meta(conn, ref_id, devin=devin, usage=_usage_meta(session, now))
        _append_chunk(
            store,
            ref_id,
            _JOB_SUMMARY_KIND,
            f"devin_task job:{ref_id}: session {devin['session_id']} "
            f"finished (acus={acus}).{pr_note} Verify any claimed commit "
            "by sha before trusting it.",
            conn=conn,
        )
        _set_status(store, ref_id, _SUCCEEDED, conn=conn)
        conn.commit()


def _requeue(store: Store, ref_id: int, note: str, *, recheck_s: int = 900) -> None:
    """Put a claimed job back to ``queued`` behind a short re-check
    lease — the transient-API-fault path (mirrors claude_docker's
    stage requeue). Reservation refunded; a queued row isn't claimable
    before its ``lease_until``."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => %s))::text"
            ") WHERE ref_id = %s",
            (recheck_s, ref_id),
        )
        _release_job_reservation(conn, ref_id)
        _append_chunk(store, ref_id, _JOB_EVENT_KIND, note, conn=conn)
        _set_status(store, ref_id, _QUEUED, conn=conn)
        conn.commit()


def _park(store: Store, ref_id: int, wake_at: datetime | None, note: str) -> bool:
    """``STATUS:waiting_time`` park — the clean "paused" outcome."""
    with store.pool.connection() as conn:
        parked = _park_job_until(store, conn, ref_id, wake_at, note)
        conn.commit()
    if not parked:
        _fail(store, ref_id, f"{note} — park cap reached; failing")
    return parked


def _plan_failure_reason(exc: VendorError) -> str | None:
    """The "plan lacks Devin API" / auth-reject line for a fatal
    vendor status, else ``None`` (leave it to the caller's mapping)."""
    if exc.status in (402, 403):
        return (
            f"devin_api: plan lacks Devin API access (HTTP {exc.status}) "
            "— the subscription has no API entitlement or the service "
            "user lacks UseDevinSessions; next: confirm the plan on "
            "devin.ai/pricing before enabling this lane"
        )
    if exc.status == 401:
        return (
            "devin_api: DEVIN_API_TOKEN rejected (HTTP 401) — rotate or "
            "replace the token; next: check the /secrets entry"
        )
    return None


def _launch(
    store: Store, ref_id: int, meta: dict[str, Any], api: SessionAPI, org_id: str
) -> str:
    """Mint the session for a freshly-claimed job. Returns the outcome
    bucket for the pass counters: ``ok`` / ``failed`` / ``queued``."""
    from precis.workers.job_types import devin_task

    params = dict(meta.get("params") or {})
    reason = devin_task.params_rejection(params)
    if reason is not None:
        _fail(store, ref_id, reason)
        return _FAILED

    payload: dict[str, Any] = {"prompt": str(params["prompt"]).strip()}
    if params.get("title"):
        payload["title"] = str(params["title"])
    if params.get("repos"):
        payload["repos"] = list(params["repos"])
    if params.get("max_acu_limit"):
        payload["max_acu_limit"] = int(params["max_acu_limit"])
    # Provider-side tags: always traceable back to this job.
    payload["tags"] = [
        "precis",
        f"precis-job-{ref_id}",
        *list(params.get("tags") or []),
    ]

    try:
        session = api.create_session(org_id, payload)
    except VendorError as exc:
        reason = _plan_failure_reason(exc)
        if reason is not None:
            _fail(store, ref_id, reason, failure_class="config")
        else:
            _fail(
                store,
                ref_id,
                f"devin_api: session create refused: {exc}",
            )
        return _FAILED
    except (VendorUnavailable, RateLimited) as exc:
        # Transient: the API is rate-starved or mid-outage — requeue,
        # don't burn the job.
        _requeue(store, ref_id, f"devin_api: create deferred: {exc}")
        return _QUEUED

    session_id = session.get("session_id")
    if not isinstance(session_id, str) or not session_id:
        _fail(
            store,
            ref_id,
            "devin_api: create returned no session_id — unexpected "
            "response shape; not retrying blindly",
            failure_class="infra",
        )
        return _FAILED

    deadline = time.time() + _wall_seconds(meta)
    with store.pool.connection() as conn:
        _set_meta(
            conn,
            ref_id,
            devin_session_id=session_id,
            devin=_devin_meta(session),
            deadline=deadline,
        )
        _append_chunk(
            store,
            ref_id,
            _JOB_EVENT_KIND,
            f"minted Devin session {session_id} ({session.get('url') or 'no url'})",
            conn=conn,
        )
        conn.commit()
    log.info("devin_api: job %d minted session %s", ref_id, session_id)
    return "ok"


def _poll_job(
    store: Store,
    ref_id: int,
    meta: dict[str, Any],
    api: SessionAPI,
    org_id: str,
    now: datetime,
) -> str | None:
    """Poll one in-flight session. Returns the terminal/park STATUS
    applied (``succeeded``/``failed``/``waiting_time``), or ``None``
    while still in flight (lease heartbeated)."""
    session_id = str(meta.get("devin_session_id"))
    try:
        session = api.get_session(org_id, session_id)
    except VendorError as exc:
        reason = _plan_failure_reason(exc)
        if reason is not None:
            _fail(store, ref_id, reason, failure_class="config")
            return _FAILED
        if exc.status == 404:
            _fail(
                store,
                ref_id,
                f"devin_api: session {session_id} not found (HTTP 404) — "
                "terminated or deleted upstream",
            )
            return _FAILED
        # Other vendor refusal — log and leave the job running; the
        # next tick retries while the lease holds.
        log.warning("devin_api: poll of job %d refused: %s", ref_id, exc)
        return None
    except (VendorUnavailable, RateLimited) as exc:
        log.info("devin_api: poll of job %d deferred: %s", ref_id, exc)
        return None

    status = str(session.get("status") or "")
    detail = session.get("status_detail")
    detail = str(detail) if detail else None

    # Mirror the latest provider evidence every poll (cheap jsonb merge).
    with store.pool.connection() as conn:
        _set_meta(
            conn, ref_id, devin=_devin_meta(session), usage=_usage_meta(session, now)
        )
        conn.commit()

    if status == "exit":
        if detail in _QUOTA_DETAILS:
            wake = _next_quota_window(now)
            _park(
                store,
                ref_id,
                wake,
                f"devin_api: session {session_id} ended on quota "
                f"({detail}) — parked until {wake:%Y-%m-%dT%H:%MZ}; "
                "never buying overage",
            )
            store.set_setting(_PAUSED_KEY, wake.isoformat())
            return "waiting_time"
        if detail is None or detail == "finished":
            _succeed(store, ref_id, session, now)
            return _SUCCEEDED
        _fail(
            store,
            ref_id,
            f"devin_api: session {session_id} exited ({detail})",
        )
        return _FAILED

    if status == "error" or detail == "error":
        _fail(store, ref_id, f"devin_api: session {session_id} in error")
        return _FAILED

    if status == "suspended":
        if detail in _QUOTA_DETAILS:
            wake = _next_quota_window(now)
            _park(
                store,
                ref_id,
                wake,
                f"devin_api: session {session_id} suspended on quota "
                f"({detail}) — parked until {wake:%Y-%m-%dT%H:%MZ}; "
                "never buying overage",
            )
            store.set_setting(_PAUSED_KEY, wake.isoformat())
            return "waiting_time"
        _park(
            store,
            ref_id,
            None,
            f"devin_api: session {session_id} suspended "
            f"({detail or 'no detail'}) — parked; a human can resume it "
            "in the Devin app",
        )
        return "waiting_time"

    if detail in _BLOCKED_DETAILS:
        _park(
            store,
            ref_id,
            None,
            f"devin_api: session {session_id} blocked on human input "
            f"({detail}) — parked; resume via the Devin app",
        )
        return "waiting_time"

    if status in _IN_FLIGHT:
        # Operator kill / wall-clock deadline first — same terminal
        # path either way, plus a best-effort remote terminate so a
        # billed session isn't left running.
        kill_requested = meta.get("kill_requested")
        deadline = float(meta.get("deadline") or 0.0)
        if kill_requested or (deadline and time.time() > deadline):
            note = (
                kill_requested.get("note") if isinstance(kill_requested, dict) else None
            )
            why = (
                f"killed by operator{f' ({note})' if note else ''}"
                if kill_requested
                else "killed at wall-clock deadline"
            )
            api.terminate_session(org_id, session_id)
            _fail(
                store,
                ref_id,
                f"devin_task job:{ref_id}: {why} (session {session_id}; "
                "remote terminate best-effort).",
            )
            return _FAILED
        # Alive and within budget — heartbeat the lease. A lost lease
        # means another generation owns the row; stop touching it.
        with store.pool.connection() as conn:
            mine = _renew_lease_if_mine(conn, ref_id, meta, _LEASE_S)
            conn.commit()
        if not mine:
            log.info("devin_api: lease of job %d lost mid-poll", ref_id)
        return None

    # Unknown status — keep polling (provider may add states).
    log.warning(
        "devin_api: job %d session %s unknown status %r/%r",
        ref_id,
        session_id,
        status,
        detail,
    )
    with store.pool.connection() as conn:
        mine = _renew_lease_if_mine(conn, ref_id, meta, _LEASE_S)
        conn.commit()
    return None


def _running_jobs(store: Store, host: str | None) -> list[tuple[int, dict[str, Any]]]:
    """In-flight devin_api jobs (``STATUS:running`` +
    ``meta.devin_session_id``) this worker's lease owns. ``lease_host``
    IS NOT DISTINCT FROM the host this worker would stamp — matching
    what :func:`worker_identity` records at claim."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.meta
              FROM refs r
             WHERE r.kind = 'job'
               AND r.retired_at IS NULL
               AND r.meta->>'executor' = %s
               AND r.meta ? 'devin_session_id'
               AND (r.meta->>'lease_host') IS NOT DISTINCT FROM %s
               AND EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id
                        AND t.namespace = 'STATUS'
                        AND t.value = %s
                   )
             ORDER BY r.ref_id
            """,
            (_EXECUTOR_NAME, host, _RUNNING),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {})) for r in rows]


def run_devin_api_pass(
    store: Store,
    *,
    limit: int = 4,
    enabled: bool | None = None,
    api: SessionAPI | None = None,
    now: datetime | None = None,
) -> dict[str, int]:
    """One executor tick: poll in-flight sessions, then claim + mint.

    Returns ``{claimed, ok, failed, parked}`` for runner aggregation
    (``parked`` folds into neither — a parked job is non-terminal).

    ``enabled=None`` resolves the ``service_config`` gate (dark until a
    row lights ``job_devin_api``); the wired registration's per-cycle
    gate and the ``--only`` escape hatch both pass ``True`` explicitly.
    ``api`` is the recorded-fixture seam — the default builds
    :class:`_HttpxSessionAPI`, never raw HTTP outside ``with_backoff``.
    """
    from precis import secrets as _secrets
    from precis.liveness import worker_identity

    now = now or datetime.now(UTC)
    zero = {"claimed": 0, "ok": 0, "failed": 0, "parked": 0}
    if enabled is None:
        enabled = devin_exec_enabled(store)
    if not enabled:
        log.info("devin_api: dark — no live service_config row; no-op")
        return zero

    token = _secrets.get_secret(_SECRET_NAME)
    org_id = _resolve_org(store)
    if not token or not org_id:
        log.warning(
            "devin_api: missing configuration — set %s and %s on /secrets "
            "or in the environment (org id also accepts the %s setting). "
            "Copy the org id from Settings > Devin API in the Devin app; "
            "jobs stay queued until configured",
            _SECRET_NAME,
            _ORG_ENV,
            _ORG_SETTING_KEY,
        )
        return zero

    if api is None:
        api = _HttpxSessionAPI(token, os.environ.get(_BASE_ENV) or _DEFAULT_BASE)

    host = worker_identity()[2]
    ok = 0
    failed = 0
    parked = 0

    # 1) Poll in-flight sessions this worker owns.
    for ref_id, meta in _running_jobs(store, host):
        try:
            terminal = _poll_job(store, ref_id, meta, api, org_id, now)
            if terminal == _SUCCEEDED:
                ok += 1
            elif terminal == _FAILED:
                failed += 1
            elif terminal == "waiting_time":
                parked += 1
        except Exception:  # pragma: no cover - defensive
            log.warning("devin_api: poll of job %d raised", ref_id, exc_info=True)

    # 2) Claim + mint queued jobs — skipped entirely while the lane's
    # quota pause is live (quota exhaustion pauses the executor, not
    # just the one job; in-flight polls above still run).
    if _paused_until(store, now):
        log.info("devin_api: quota pause in force — no new session mints")
        return {"claimed": 0, "ok": ok, "failed": failed, "parked": parked}

    inflight = len(_running_jobs(store, host))
    slots = max(0, _concurrency() - inflight)
    if slots <= 0:
        return {"claimed": 0, "ok": ok, "failed": failed, "parked": parked}

    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor=_EXECUTOR_NAME,
            limit=min(limit, slots),
            node=os.environ.get("PRECIS_NODE"),
            parent_not_paused=True,
            reclaim_stale_running=True,
        )
        claimed: list[tuple[int, str, dict[str, Any]]] = []
        poisoned = 0
        for ref_id, title, meta in rows:
            if _poison_guard(store, conn, ref_id, meta):
                poisoned += 1
                continue
            conn.execute(
                "UPDATE refs SET meta = meta || jsonb_build_object("
                "  'lease_until', (now() + make_interval(secs => %s))::text"
                ") WHERE ref_id = %s",
                (_LEASE_S, ref_id),
            )
            _set_status(store, ref_id, _RUNNING, conn=conn)
            claimed.append((ref_id, title, meta))
        conn.commit()
    failed += poisoned

    for ref_id, _title, meta in claimed:
        if meta.get("devin_session_id"):
            # Re-adopt (never re-mint): the session id is already
            # stamped, so the NEXT tick's step-1 poll resumes ownership
            # — mirrors claude_docker's meta.container re-adopt branch.
            _append_chunk(
                store,
                ref_id,
                _JOB_EVENT_KIND,
                f"runner: re-adopted job {ref_id} (session "
                f"{meta.get('devin_session_id')} already minted by a "
                "prior worker generation) — resuming poll",
            )
            ok += 1
            continue
        outcome = _launch(store, ref_id, meta, api, org_id)
        if outcome == _FAILED:
            failed += 1
        elif outcome != _QUEUED:
            ok += 1
    return {
        "claimed": len(claimed) + poisoned,
        "ok": ok,
        "failed": failed,
        "parked": parked,
    }


__all__ = [
    "SERVICE_NAME",
    "SessionAPI",
    "devin_exec_enabled",
    "run_devin_api_pass",
]
