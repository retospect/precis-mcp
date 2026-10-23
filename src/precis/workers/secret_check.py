"""The weekly credential check as a scheduled pass (gr496305 item 4).

``precis secret check`` (runbook ``docs/runbooks/secrets-check.md``) probes
every registry secret that has a cheap auth-only endpoint — but it only runs
when someone runs ``/whatneedsdoing``, so a revoked key otherwise surfaces
as a failing job. This pass runs :func:`precis.secret_status.weekly_report`
from the weekly, host-agnostic ``secret_check`` cadence and turns each
``failed`` row into one ``secret_check`` alert.

Dedup is the alerts producer's own: fingerprint ``failed:<name>`` keeps one
open alert per credential (a still-failing repeat sighting bumps
``seen_count``). Only a verified ``ok`` resolves it — ``unprobed`` (an
outage, a 5xx, a 429) and ``missing`` (the host-agnostic lease may land on
a host that simply lacks the key) keep the alert open, so a real "key
rejected" finding can't silently clear and re-raise as a fresh alert next
week. A credential dropped from the registry has no row and resolves.

Same security contract as the manual check: probes hit only the fixed
:data:`precis.secret_status._PROBES` endpoints, and a secret value is never
printed, logged, stored or put in alert text — the alert carries the
credential's name, the HTTP-status detail and the provider key URL.

This module also owns minting the ``secret_status`` checker job: the
``secret_status`` scheduler cadence mints one periodically and the
/secrets page's "Check now" POST mints one on demand; ``job_inproc`` runs
:func:`precis.secret_status.record_status`, which persists the per-secret
status the page reads. The weekly pass below persists the same status
after its own probes, so the page and the alerts agree.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from precis import secret_status
from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

ALERT_SOURCE = RULES_BY_ID["secret_check/failed:{secret}"].rule

#: The checker job's job_type (``workers/job_types/secret_status.py``).
STATUS_JOB_TYPE = "secret_status"

#: A pending check older than this is wedged, not queued — ~2x the probe
#: budget. The pending check ignores it so a dead job can't block mints
#: (or the page's "queued" hint) forever.
_PENDING_FRESH_S = 30 * 60

#: pg_advisory_xact_lock key for mint_status_job's check-then-insert.
_MINT_LOCK_KEY = 0x7365635F73746174  # "sec_stat"


def _fingerprint(name: str) -> str:
    return f"failed:{name}"


def _pending_fresh_status_job(conn: Any) -> bool:
    """A live ``secret_status`` job pending for less than the fresh bound —
    same non-terminal semantics as ``dispatch._pending_job_of_type_exists``,
    plus the age cutoff (wedged pending rows stop counting)."""
    row = conn.execute(
        """
        SELECT 1 FROM refs r
         WHERE r.kind = 'job' AND r.retired_at IS NULL
           AND r.meta->>'job_type' = %s
           AND r.created_at > now() - make_interval(secs => %s)
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) NOT IN ('succeeded', 'failed', 'cancelled')
         LIMIT 1
        """,
        (STATUS_JOB_TYPE, _PENDING_FRESH_S),
    ).fetchone()
    return row is not None


def mint_status_job(store: Store, *, origin: str, title: str) -> int | None:
    """Queue one ``secret_status`` checker job unless one is already pending.

    The single mint path shared by the ``secret_status`` cadence
    (``origin='background'``, prio 8) and the page's "Check now" POST
    (``origin='human'``, prio 2). ``meta`` carries no secret names or
    values — just routing. Returns the minted job's id, or ``None`` when a
    fresh queued/running/waiting job of the type already exists (a pending
    check refreshes the same blob; a second adds nothing). The check and
    the insert run under one advisory xact lock so two concurrent minters
    can't both pass the pending check and double-mint.
    """
    from precis.store.types import Tag

    prio = 2 if origin == "human" else 8
    with store.pool.connection() as conn:
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_MINT_LOCK_KEY,))
        if _pending_fresh_status_job(conn):
            conn.commit()
            return None
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=title,
            meta={
                "job_type": STATUS_JOB_TYPE,
                "executor": "job_inproc",
                "params": {},
                "origin": origin,
            },
            prio=prio,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.commit()
    return int(ref.id)


def pending_status_check(store: Store) -> bool:
    """True while a fresh ``secret_status`` job is queued/running/waiting —
    the page's "a check is queued" hint (a coalesced "Check now" reads as
    this). A wedged pending job older than the bound reads as not queued."""
    try:
        with store.pool.connection() as conn:
            return _pending_fresh_status_job(conn)
    except Exception:
        log.debug("secret_check: pending-check query failed", exc_info=True)
        return False


def run_secret_check_pass(store: Store) -> BatchResult:
    """One weekly check. ``claimed`` = registry secrets evaluated, ``ok`` =
    rows not ``failed``, ``failed`` = rows that raised/refreshed an alert.
    Also refreshes the stored /secrets status — its probes are the same
    ones the status record needs, so persisting them is free."""
    probe_results = asyncio.run(secret_status.run_checks(store))
    rows = asyncio.run(secret_status.weekly_report(store, results=probe_results))
    live = [_fingerprint(row.name) for row in rows if row.state != "ok"]
    n_failed = 0
    for row in rows:
        if row.state != "failed":
            continue
        n_failed += 1
        fingerprint = _fingerprint(row.name)
        # name, status detail and key URL only — never the value (see the
        # module docstring; raise_alert masks on top of that).
        raise_alert(
            store,
            source=ALERT_SOURCE,
            fingerprint=fingerprint,
            title=f"[secret_check] {row.name}: credential rejected",
            detail=(
                f"{row.detail}."
                + (f"\nKey URL: {row.get_url}" if row.get_url else "")
                + "\n\nRotate with `precis secret set`; the next weekly pass "
                "resolves this alert once the probe passes. Runbook: "
                "docs/runbooks/secrets-check.md"
            ),
        )
    resolved = resolve_stale_alerts(store, source=ALERT_SOURCE, live_fingerprints=live)
    if resolved:
        log.info("secret_check: resolved %d alert(s) no longer failing", resolved)
    try:
        secret_status.record_status(store, results=probe_results)
    except Exception:
        # The page's blob is a side-product here — a persist failure must
        # not fail the pass and mask the alert result above.
        log.warning("secret_check: status persist failed", exc_info=True)
    return BatchResult(
        handler="secret_check",
        claimed=len(rows),
        ok=len(rows) - n_failed,
        failed=n_failed,
    )


__all__ = [
    "ALERT_SOURCE",
    "STATUS_JOB_TYPE",
    "mint_status_job",
    "pending_status_check",
    "run_secret_check_pass",
]
