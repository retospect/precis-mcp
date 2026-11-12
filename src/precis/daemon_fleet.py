"""The release daemon cohort — which live heartbeats attest to a deploy.

One definition shared by the attestors and the readers so an MCP view
can't drift from the evidence the factory counts (gr627361; extracted
from :mod:`precis.workers.job_types.release_confirm`):

- :mod:`precis.workers.job_types.release_confirm` — the dark release
  pass's confirm node: every cohort member's ``code_sha`` must equal the
  pinned release sha.
- ``get(kind='machine', view='daemons')``
  (:mod:`precis.handlers.machine`) — the read-only cohort view the MCP
  surface exposes for release attestation.
- :mod:`precis.workers.conditions` ``_probe_deploy_fleet`` — the hourly
  coverage arm; it selects the same rows under the same freshness gate
  (kept inline there because its window is env-tunable).

A heartbeat row is LIVE when ``ts`` is inside ``FLEET_FRESH_MIN``
minutes (60 — generous on purpose: an hourly eval that caught a host
mid-bounce would drop and re-fire the alert every deploy), the beat is
not ``meta.ephemeral``, and the host is not a 12-hex container id
(gr306275: a no-hostname container advertises its short id as a
throwaway host). A live row ATTESTS — joins the cohort — when
:func:`counts_as_daemon` holds: its ``worker_units`` snapshot observes
installed precis-worker units, per-process ``activity`` is recorded, or
the host is a covered member of the ``deploy.fleet`` ledger; a row with
no ``worker_units`` key at all predates the snapshot and stays eligible
(gr625282). A live worker-less non-member — a one-off checkout's lone
heartbeat — is a note, never counted (gr623769).

``code_sha`` is the reporting process's *served commit*
(:func:`precis.workers.heartbeat._served_code_sha`) — the actual
process boot version. ``worker_units`` is *installed metadata*: which
``precis-worker*`` unit files systemctl lists, not the version a running
process serves. The two stay separate fields on :class:`DaemonBeat` so
a caller can never read one as the other.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store

#: ``app_settings`` key ``scripts/deploy`` refreshes after every green
#: deploy with ``{"sha", "at", "covered": [<host>, …], "skipped"}`` —
#: the deploy fleet's known membership (``conditions._DEPLOY_LEDGER_KEY``,
#: restated here as the canonical spelling).
DEPLOY_LEDGER_KEY = "deploy.fleet"

#: A host heartbeating within this window counts as live for the cohort
#: (the canonical value ``conditions._DEPLOY_FLEET_FRESH_MIN`` restates).
FLEET_FRESH_MIN = 60

#: The live-row gate — the same WHERE clause
#: ``conditions._DEPLOY_FLEET_SQL`` selects under: a heartbeat in the
#: last ``fresh_min`` minutes, not ephemeral, host not a 12-hex
#: container id. ``boot_ids`` rides along for readers that name the
#: live daemons (release_confirm ignores it).
_LIVE_FLEET_SQL = """
SELECT host, ts, meta->>'code_sha' AS code_sha,
       meta->'worker_units' AS worker_units,
       meta->'activity' AS activity,
       meta->'boot_ids' AS boot_ids
  FROM host_heartbeat
 WHERE ts > now() - make_interval(mins => %(fresh_min)s)
   AND (meta->>'ephemeral') IS DISTINCT FROM 'true'
   AND host !~ '^[0-9a-f]{12}$'
 ORDER BY host
"""


@dataclass(frozen=True, slots=True)
class DaemonBeat:
    """One live ``host_heartbeat`` row, cohort fields only.

    ``ts`` is the heartbeat's write time (the restart race's clock).
    ``code_sha`` is the actual process boot version; ``worker_units``
    the installed-unit snapshot — ``None`` when the beat carries no
    snapshot key at all (a pre-snapshot beat, still eligible);
    ``activity`` and ``boot_ids`` are the nested-merged per-process
    maps whose keys name the live daemons on the host.
    """

    host: str
    ts: datetime | None
    code_sha: str | None
    worker_units: dict[str, Any] | None
    activity: dict[str, Any]
    boot_ids: dict[str, Any]


def _aware_ts(value: Any) -> datetime | None:
    """``value`` as an aware datetime (a timestamptz or an ISO/``Z``
    stamp), else ``None`` — ``release_confirm._parse_ts``'s twin."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    try:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def read_deploy_ledger(conn: Any) -> dict[str, Any]:
    """The parsed ``app_settings.deploy.fleet`` row — ``{}`` when the
    ledger is absent or unreadable (a missing row means no deploy has
    published fleet membership yet, not empty membership)."""
    try:
        row = conn.execute(
            "SELECT value FROM app_settings WHERE key = %s", (DEPLOY_LEDGER_KEY,)
        ).fetchone()
        ledger = json.loads(str(row[0])) if row is not None else {}
    except Exception:
        return {}
    return ledger if isinstance(ledger, dict) else {}


def fleet_members(conn: Any) -> frozenset[str]:
    """The deploy fleet's known members — the ``covered`` hosts of
    ``app_settings.deploy.fleet``. Empty when the ledger is absent or
    unreadable: membership then falls to the heartbeat's own worker
    evidence."""
    covered = read_deploy_ledger(conn).get("covered")
    if not isinstance(covered, list):
        return frozenset()
    return frozenset(str(h) for h in covered)


def counts_as_daemon(
    host: str, worker_units: Any, activity: Any, members: frozenset[str]
) -> bool:
    """Whether a live heartbeat row attests for the release (gr623769).

    Counts when the host runs workers — its ``worker_units`` snapshot
    observes installed precis-worker units — or is a known fleet member:
    recorded worker ``activity`` (per-process, nested-merged on write so
    it survives later beats) or a ``covered`` row in the deploy ledger.
    A row with no ``worker_units`` key predates the snapshot and stays
    eligible (gr625282's rule). A worker-less non-member — a one-off
    checkout's lone heartbeat — is a note, never counted."""
    if worker_units is None:
        return True
    units = worker_units.get("units") if isinstance(worker_units, dict) else None
    if isinstance(units, dict) and units:
        return True
    return bool(activity) or host in members


def live_fleet(
    store: Store, *, fresh_min: float = FLEET_FRESH_MIN
) -> tuple[list[DaemonBeat], list[str]]:
    """``(beats, workerless)`` of the live heartbeat rows, store only.

    ``beats`` is one :class:`DaemonBeat` per live row that attests
    (:func:`counts_as_daemon`); ``workerless`` names the live hosts
    that run no workers and are not known fleet members (gr623769):
    reported as a note, never counted.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(_LIVE_FLEET_SQL, {"fresh_min": fresh_min}).fetchall()
        members = fleet_members(conn)
    beats: list[DaemonBeat] = []
    workerless: list[str] = []
    for host, ts, code_sha, worker_units, activity, boot_ids in rows:
        host = str(host)
        if not counts_as_daemon(host, worker_units, activity, members):
            workerless.append(host)
            continue
        beats.append(
            DaemonBeat(
                host=host,
                ts=_aware_ts(ts),
                code_sha=None if code_sha is None else str(code_sha),
                # A present-but-malformed snapshot reads as an empty one,
                # never as the absent (still-eligible) key.
                worker_units=(
                    worker_units
                    if isinstance(worker_units, dict)
                    else ({} if worker_units is not None else None)
                ),
                activity=dict(activity) if isinstance(activity, dict) else {},
                boot_ids=dict(boot_ids) if isinstance(boot_ids, dict) else {},
            )
        )
    return beats, workerless


def deploy_ledger(store: Store) -> dict[str, Any]:
    """The parsed ``deploy.fleet`` ledger (``{}`` when absent) — the
    deployed sha the cohort's ``code_sha`` values attest against."""
    with store.pool.connection() as conn:
        return read_deploy_ledger(conn)


__all__ = [
    "DEPLOY_LEDGER_KEY",
    "FLEET_FRESH_MIN",
    "DaemonBeat",
    "counts_as_daemon",
    "deploy_ledger",
    "fleet_members",
    "live_fleet",
    "read_deploy_ledger",
]
