"""Per-repo lease: at most one landing or releasing job per second repo.

Lands of a second repo (``repo_land``, behind the ``land_slice`` job type)
are not serialised by anything else, and the mechanical ``repo_release``
job must hold every land of its repo while it waits for CI on the release
commit. This module is that hold. ``repo_land`` and ``repo_release``
call it.

The lease is *derived from live job rows*: no table, no setting, no todo
meta. A job holds the lease for repo R while its status is non-terminal and
its ``meta.repo_lease`` is ``{"repo": R, "kind": "land" | "release", "at":
<iso utc>}``. A release job waiting for the lease carries
``meta.repo_lease_wanted = {"repo": R, "at": <iso utc>}``. Deriving it from
the job row means every terminal path of a job frees the lease with no
cleanup code to forget: a failure, a deadline kill, an operator cancel and
a dead-node reap all end in a terminal status, and a terminal job simply
does not count. :func:`release` exists only for a job that finishes its
critical section before it turns terminal.

Two kinds, one priority rule: a ``land`` is blocked by any other live
holder (``held``) *and* by any other live job that wants the lease for the
repo (``wanted``) -- a waiting release outranks new lands, or a steady
stream of lands would starve it. A ``release`` is blocked only by another
live holder.

A holder or wanter whose ``meta.deadline`` (epoch seconds, the ssh_node
wall-clock kill deadline) passed more than :data:`STALE_AFTER_S` ago is
ignored and flagged with one alert: a dead job whose row never went
terminal must not freeze the repo. A job that freshly takes the lease
strips the lease key from the stale holders it skipped (take-over evicts),
and a job that loses to a live rival strips its own key, so a revived
holder never sits beside the job that took over; of two live holders the
lower job id keeps it. The decision itself is pure (:func:`decide`, rows in,
decision out); the DB part only reads candidate rows and stamps meta.

:func:`acquire` runs in one ``store.atomic()`` under a transaction-scoped
advisory lock keyed on the repo name, so two acquirers cannot both pass
the check.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from psycopg import Connection

from precis.alert_ids import RULES_BY_ID
from precis.store import Store
from precis.workers._mint import JOB_TERMINAL_STATUSES
from precis.workers.executors._common import set_meta

log = logging.getLogger(__name__)

LEASE_KEY = "repo_lease"
WANT_KEY = "repo_lease_wanted"
KINDS = ("land", "release")
#: Job types whose rows are scanned for a lease or a want.
JOB_TYPES = ("land_slice", "repo_release")
#: A holder this long past its ``meta.deadline`` no longer holds the lease.
STALE_AFTER_S = 900
#: Newest rows scanned per job type (live_deploy_job's shape).
_SCAN_LIMIT = 50

_ALERT_SOURCE = RULES_BY_ID["repo_lease/stale-holder:{repo}/{job_id}"].rule


@dataclass(frozen=True, slots=True)
class LeaseRow:
    """The slice of a job row the decision reads."""

    job_id: int
    terminal: bool
    lease: dict[str, Any] | None = None
    want: dict[str, Any] | None = None
    deadline: float | None = None


@dataclass(slots=True)
class Decision:
    """``blocker`` is the job in the way (``None`` = free or already ours);
    ``holder`` is the live, non-stale holder; ``stale`` lists the stale
    holders and wanters skipped, to be flagged."""

    blocker: dict[str, Any] | None = None
    holder: dict[str, Any] | None = None
    stale: list[int] = field(default_factory=list)


def _is_stale(row: LeaseRow, now: float) -> bool:
    return row.deadline is not None and now - row.deadline > STALE_AFTER_S


def decide(
    rows: list[LeaseRow],
    repo: str,
    *,
    job_id: int | None,
    kind: str,
    now: float,
) -> Decision:
    """Pure decision over candidate rows. ``job_id`` is the asker (``None``
    for a plain holder query, which never blocks anything)."""
    out = Decision()
    wanters: list[int] = []
    own: dict[str, Any] | None = None
    other: dict[str, Any] | None = None
    for row in sorted(rows, key=lambda r: r.job_id):
        if row.terminal:
            continue
        lease = row.lease
        if lease and lease.get("repo") == repo:
            info = {
                "job_id": row.job_id,
                "kind": lease.get("kind"),
                "at": lease.get("at"),
            }
            if row.job_id == job_id:
                own = info
            elif _is_stale(row, now):
                out.stale.append(row.job_id)
            elif other is None:
                other = info
        want = row.want
        if want and want.get("repo") == repo and row.job_id != job_id:
            if _is_stale(row, now):
                if row.job_id not in out.stale:
                    out.stale.append(row.job_id)
            else:
                wanters.append(row.job_id)
    if job_id is None:
        out.holder = other
        return out
    if own is not None and (other is None or job_id < other["job_id"]):
        # Ours, and no live rival with a lower id: the lower id keeps it.
        out.holder = own
        return out
    out.holder = other
    if other is not None:
        out.blocker = {
            "job_id": other["job_id"],
            "kind": other["kind"],
            "reason": "held",
        }
    elif kind == "land" and wanters:
        out.blocker = {"job_id": wanters[0], "kind": "release", "reason": "wanted"}
    return out


def _status(store: Store, job_id: int) -> str:
    return next(
        (str(t.value) for t in store.tags_for(job_id) if t.prefix == "STATUS"),
        "open",
    )


def _row(store: Store, job_id: int, meta: dict[str, Any]) -> LeaseRow:
    lease = meta.get(LEASE_KEY)
    want = meta.get(WANT_KEY)
    dl = meta.get("deadline")
    return LeaseRow(
        job_id=job_id,
        terminal=_status(store, job_id) in JOB_TERMINAL_STATUSES,
        lease=lease if isinstance(lease, dict) else None,
        want=want if isinstance(want, dict) else None,
        deadline=float(dl)
        if isinstance(dl, (int, float)) and not isinstance(dl, bool)
        else None,
    )


def _rows(
    store: Store, conn: Connection, repo: str, job_id: int | None
) -> list[LeaseRow]:
    """Candidate rows: the newest ``_SCAN_LIMIT`` jobs per type carrying a
    lease or want key for ``repo``, plus the asker's own row."""
    found: dict[int, dict[str, Any]] = {}
    for jt in JOB_TYPES:
        for jid, meta in conn.execute(
            """
            SELECT j.ref_id, j.meta FROM refs j
             WHERE j.kind = 'job' AND j.retired_at IS NULL
               AND j.meta ->> 'job_type' = %s
               AND (j.meta -> %s ->> 'repo' = %s OR j.meta -> %s ->> 'repo' = %s)
             ORDER BY j.ref_id DESC
             LIMIT %s
            """,
            (jt, LEASE_KEY, repo, WANT_KEY, repo, _SCAN_LIMIT),
        ).fetchall():
            found[int(jid)] = meta or {}
    if job_id is not None and job_id not in found:
        own = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND kind = 'job'", (job_id,)
        ).fetchone()
        if own is not None:
            found[job_id] = own[0] or {}
    return [_row(store, jid, meta) for jid, meta in found.items()]


def _flag_stale(store: Store, repo: str, stale: list[int]) -> None:
    """One alert per (repo, job): ``raise_alert`` dedups on the fingerprint."""
    from precis.alerts import raise_alert

    for jid in stale:
        try:
            raise_alert(
                store,
                source=_ALERT_SOURCE,
                fingerprint=f"stale-holder:{repo}/{jid}",
                title=f"repo lease for {repo} blocked by stale job #{jid}",
                detail=(
                    f"job #{jid} still carries the {repo} lease or want but its "
                    f"deadline passed over {STALE_AFTER_S}s ago; it is ignored "
                    "so the repo is not frozen. Cancel or reap the job."
                ),
                severity="warn",
                subject_ref_id=jid,
            )
        except Exception:  # an alert failure must not block a land
            log.warning("repo_lease: stale-holder alert failed", exc_info=True)


def holder(
    store: Store, repo: str, *, now: float | None = None
) -> dict[str, Any] | None:
    """The live, non-stale job holding the lease for ``repo`` as
    ``{"job_id", "kind", "at"}``, else ``None``. Stale holders are flagged."""
    ts = time.time() if now is None else now
    with store.pool.connection() as conn:
        rows = _rows(store, conn, repo, None)
    dec = decide(rows, repo, job_id=None, kind="land", now=ts)
    _flag_stale(store, repo, dec.stale)
    return dec.holder


def acquire(
    store: Store, repo: str, *, job_id: int, kind: str, now: float | None = None
) -> dict[str, Any] | None:
    """Take the lease for ``job_id``. ``None`` = it holds it now (also when
    it already did); else the job in the way as ``{"job_id", "kind",
    "reason": "held" | "wanted"}``. Acquiring clears the caller's want."""
    if kind not in KINDS:
        raise ValueError(f"repo_lease kind {kind!r} not in {KINDS}")
    ts = time.time() if now is None else now
    with store.atomic() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(hashtext('repo_lease'), hashtext(%s))",
            (repo,),
        )
        dec = decide(
            _rows(store, conn, repo, job_id), repo, job_id=job_id, kind=kind, now=ts
        )
        if dec.blocker is not None:
            conn.execute(
                "UPDATE refs SET meta = meta - %s WHERE ref_id = %s"
                " AND meta -> %s ->> 'repo' = %s",
                (LEASE_KEY, job_id, LEASE_KEY, repo),
            )
        else:
            conn.execute(
                "UPDATE refs SET meta = meta - %s WHERE ref_id = %s", (WANT_KEY, job_id)
            )
            if dec.holder is None:
                if dec.stale:
                    conn.execute(
                        "UPDATE refs SET meta = meta - %s"
                        " WHERE ref_id = ANY(%s) AND meta -> %s ->> 'repo' = %s",
                        (LEASE_KEY, dec.stale, LEASE_KEY, repo),
                    )
                set_meta(
                    conn,
                    job_id,
                    **{
                        LEASE_KEY: {
                            "repo": repo,
                            "kind": kind,
                            "at": datetime.now(UTC).isoformat(),
                        }
                    },
                )
    _flag_stale(store, repo, dec.stale)
    return dec.blocker


def want(store: Store, repo: str, *, job_id: int) -> None:
    """Stamp the want flag on ``job_id`` (idempotent: a present flag for
    this repo keeps its original ``at``)."""
    with store.atomic() as conn:
        row = conn.execute(
            "SELECT meta -> %s ->> 'repo' FROM refs WHERE ref_id = %s",
            (WANT_KEY, job_id),
        ).fetchone()
        if row is not None and row[0] == repo:
            return
        set_meta(
            conn,
            job_id,
            **{WANT_KEY: {"repo": repo, "at": datetime.now(UTC).isoformat()}},
        )


def release(store: Store, repo: str, *, job_id: int) -> None:
    """Clear both keys on ``job_id`` (idempotent). A terminal status frees
    the lease anyway; this is for a job that is done with its critical
    section before it turns terminal."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta - %s - %s WHERE ref_id = %s",
            (LEASE_KEY, WANT_KEY, job_id),
        )


__all__ = [
    "JOB_TYPES",
    "KINDS",
    "LEASE_KEY",
    "STALE_AFTER_S",
    "WANT_KEY",
    "Decision",
    "LeaseRow",
    "acquire",
    "decide",
    "holder",
    "release",
    "want",
]
