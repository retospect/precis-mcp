"""Claim-registry reaper — ONE epoch-arm reclaim pass over every
registered claim type (self-healing-spine Layer 1, slice 1).

A claim type registers one :class:`ClaimRow`: how to *locate* candidate
orphans carrying a boot-epoch identity, and how to *reclaim* one whose
generation is provably replaced. Classification is shared —
:func:`precis.liveness.reclaim_reason` against one
``advertised_boot_ids`` snapshot per pass — and every reclaim action
re-verifies the advertisement *inside its own transaction* (spine
safety law #3: re-verify liveness immediately before a destructive
act), so a holder that re-advertises between snapshot and action is
never harmed.

Initial rows: ``slot_hold`` (delete + capped refund — the TTL arm stays
the heartbeat's ``reclaim_expired_slot_holds`` sweep) and ``agentlog``
(finalize ``status='aborted'`` — previously a zombie forever). Job
leases keep their in-claim machinery (`executors/_common.py`) — it must
stay in the claim path for the starvation-bound pre-pass.

Age floor: a claim younger than :func:`_min_age_s` is never considered,
so a just-booted worker's claims can't be reclaimed in the window
before its first heartbeat advertises the new boot_id.

One grep-able line per reclaim::

    reaper: reclaimed <type> #<id> arm=epoch holder=<host>/<process>/<boot8> age=<s>s
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from precis.liveness import (
    RECLAIM_WHY_EPOCH,
    advertised_boot_ids,
    reclaim_reason,
)
from precis.store import Store

log = logging.getLogger(__name__)


def _min_age_s() -> float:
    """Age floor (seconds) before a claim is reclaim-eligible.

    Default 600 — comfortably past worker-boot → first-heartbeat, and
    far under the 1 h slot-hold TTL this arm exists to beat.
    """
    raw = os.environ.get("PRECIS_CLAIM_REAPER_MIN_AGE_S")
    try:
        return float(raw) if raw is not None else 600.0
    except ValueError:
        return 600.0


@dataclass(frozen=True)
class Candidate:
    """One located claim carrying epoch identity."""

    claim_id: int
    boot_id: str
    host: str
    process: str
    age_s: float


@dataclass(frozen=True)
class ClaimRow:
    """One claim type's registry entry.

    ``locate`` returns candidates older than the age floor whose
    identity is stamped (NULL-identity claims belong to the TTL arm and
    are never surfaced here). ``reclaim`` performs the epoch-arm action
    for ONE candidate and must re-verify non-liveness inside its own
    transaction; it returns True iff the row was actually reclaimed
    (False = lost the race — released/finalized/re-advertised meanwhile,
    which is success by other means, not an error).
    """

    claim_type: str
    locate: Callable[[Store, float], list[Candidate]]
    reclaim: Callable[[Store, Candidate], bool]


# ── Guard fragment: the in-transaction re-verify (law #3) ────────────────
# True when the candidate's (host, process) currently advertises exactly
# the candidate's boot_id — i.e. the holder is provably still live and the
# action must NOT fire. The reclaim SQL requires NOT EXISTS of this.
_STILL_ADVERTISED = (
    "SELECT 1 FROM host_heartbeat "
    "WHERE host = %(host)s AND meta -> 'boot_ids' ->> %(process)s = %(boot_id)s"
)


# ── Row: resource_slot_holds ─────────────────────────────────────────────

_LOCATE_SLOT_HOLDS = (
    "SELECT id, holder_boot_id, holder_host, holder_process, "
    "       EXTRACT(EPOCH FROM (now() - acquired_at)) "
    "  FROM resource_slot_holds "
    " WHERE holder_boot_id IS NOT NULL "
    "   AND acquired_at < now() - make_interval(secs => %s)"
)

# Delete + refund in one statement, same capped-refund discipline as the
# TTL sweep's `_RECLAIM_EXPIRED_HOLDS` (`store/_resource_slots_ops.py`):
# refund is capped at capacity, a slot row deleted/reseeded by the
# capability probe simply drops the refund, and a hold already released
# normally matches nothing (the id + boot_id guard).
_REAP_SLOT_HOLD = (
    "WITH doomed AS ("
    "  DELETE FROM resource_slot_holds "
    "  WHERE id = %(claim_id)s AND holder_boot_id = %(boot_id)s "
    "    AND NOT EXISTS (" + _STILL_ADVERTISED + ") "
    "  RETURNING host, resource, units"
    "), refunded AS ("
    "  UPDATE resource_slots s "
    "     SET free = LEAST(s.capacity, s.free + d.units) "
    "    FROM doomed d "
    "   WHERE s.host = d.host AND s.resource = d.resource "
    "  RETURNING s.host"
    ") "
    "SELECT (SELECT COUNT(*) FROM doomed), (SELECT COUNT(*) FROM refunded)"
)


def _locate_slot_holds(store: Store, min_age_s: float) -> list[Candidate]:
    with store.pool.connection() as conn:
        rows = conn.execute(_LOCATE_SLOT_HOLDS, (min_age_s,)).fetchall()
    return [
        Candidate(int(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4])) for r in rows
    ]


def _reap_slot_hold(store: Store, c: Candidate) -> bool:
    with store.tx() as conn:
        row = conn.execute(
            _REAP_SLOT_HOLD,
            {
                "claim_id": c.claim_id,
                "boot_id": c.boot_id,
                "host": c.host,
                "process": c.process,
            },
        ).fetchone()
    return bool(row and int(row[0]))


# ── Row: agentlogs (zombie logs — opened, holder gone, never finalized) ──

_LOCATE_AGENTLOGS = (
    "SELECT ref_id, meta -> 'worker' ->> 'boot_id', "
    "       meta -> 'worker' ->> 'host', meta -> 'worker' ->> 'process', "
    "       EXTRACT(EPOCH FROM (now() - created_at)) "
    "  FROM refs "
    " WHERE kind = 'agentlog' AND deleted_at IS NULL "
    "   AND meta ? 'worker' AND NOT meta ? 'ended_at' "
    "   AND created_at < now() - make_interval(secs => %s)"
)

_REAP_AGENTLOG = (
    "UPDATE refs "
    "   SET meta = meta || %(patch)s::jsonb, updated_at = now() "
    " WHERE ref_id = %(claim_id)s AND kind = 'agentlog' "
    "   AND NOT meta ? 'ended_at' "
    "   AND meta -> 'worker' ->> 'boot_id' = %(boot_id)s "
    "   AND NOT EXISTS (" + _STILL_ADVERTISED + ") "
    "RETURNING ref_id"
)


def _locate_agentlogs(store: Store, min_age_s: float) -> list[Candidate]:
    with store.pool.connection() as conn:
        rows = conn.execute(_LOCATE_AGENTLOGS, (min_age_s,)).fetchall()
    return [
        Candidate(int(r[0]), str(r[1]), str(r[2]), str(r[3]), float(r[4])) for r in rows
    ]


def _reap_agentlog(store: Store, c: Candidate) -> bool:
    import json

    patch = json.dumps(
        {
            "ended_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S%z"),
            "status": "aborted",
            "reclaim": {"arm": RECLAIM_WHY_EPOCH, "boot_id": c.boot_id},
        }
    )
    with store.tx() as conn:
        row = conn.execute(
            _REAP_AGENTLOG,
            {
                "claim_id": c.claim_id,
                "boot_id": c.boot_id,
                "host": c.host,
                "process": c.process,
                "patch": patch,
            },
        ).fetchone()
    return row is not None


# ── The registry + the pass ──────────────────────────────────────────────

CLAIM_ROWS: tuple[ClaimRow, ...] = (
    ClaimRow("slot_hold", _locate_slot_holds, _reap_slot_hold),
    ClaimRow("agentlog", _locate_agentlogs, _reap_agentlog),
)


def run_claim_reaper(store: Store) -> int:
    """One epoch-arm pass over every registered claim type.

    Returns the number of claims reclaimed. NEVER raises — like its
    sibling sweeper sub-passes (`_reap_dead_node_orphans` et al), a
    failure here must not abort the rest of the sweeper tick; a failing
    row is logged and the pass continues.
    """
    min_age = _min_age_s()
    try:
        with store.pool.connection() as conn:
            advertised = advertised_boot_ids(conn)
    except Exception:
        log.warning("reaper: boot-id snapshot failed, skipping pass", exc_info=True)
        return 0
    reclaimed = 0
    for row in CLAIM_ROWS:
        try:
            candidates = row.locate(store, min_age)
        except Exception:
            log.warning("reaper: locate failed for %s", row.claim_type, exc_info=True)
            continue
        for c in candidates:
            why = reclaim_reason(c.boot_id, c.host, c.process, advertised)
            if why != RECLAIM_WHY_EPOCH:
                continue  # holder still advertised — the plain-hang/TTL case
            try:
                if row.reclaim(store, c):
                    reclaimed += 1
                    log.warning(
                        "reaper: reclaimed %s #%d arm=epoch holder=%s/%s/%s age=%ds",
                        row.claim_type,
                        c.claim_id,
                        c.host,
                        c.process,
                        c.boot_id[:8],
                        int(c.age_s),
                    )
            except Exception:
                log.warning(
                    "reaper: reclaim failed for %s #%d",
                    row.claim_type,
                    c.claim_id,
                    exc_info=True,
                )
    return reclaimed
