"""Boot-epoch liveness — the ONE "is this claim's holder dead" predicate.

Extracted from ``workers/executors/_common.py`` (self-healing-spine
Layer 1, slice 1) so every claim type — job leases, `resource_slot_holds`,
agentlogs — shares the same hard-won predicate instead of forking it.
Structural peer of :mod:`precis.agentlog` / :mod:`precis.alerts`:
top-level so both ``workers/`` and ``utils/`` may import it.

The predicate: a claim is reclaimable on the **epoch arm** when its
stamped ``boot_id`` is *provably replaced* — the currently-advertised
generation for its ``(host, process)`` (in ``host_heartbeat.meta.
boot_ids``) is a different id, or there is no live advertisement for
that key at all (host row gone, or that process's entry missing — the
generation is provably not currently advertised, the same "provably
gone" signal as an explicit mismatch). A claim with no stamped
``boot_id`` can never be proven replaced and falls to its TTL/expiry
arm. No TTL wait on the epoch arm: recovery is one detection pass after
the replacement generation advertises.

Interface decision (spine doc): this module takes **explicit**
``(boot_id, host, process)`` parameters; each caller keeps its own
storage-key mapping (job leases: ``meta.lease_*``; slot holds:
``holder_*`` columns; agentlogs: ``meta.worker``). The storage names
never leak in here.
"""

from __future__ import annotations

import os
import socket

from psycopg import Connection

#: Sentinel used where SQL needs a value that can never equal a real
#: boot_id (a uuid4 hex, always 32 lowercase hex chars — see
#: :func:`precis.workers.heartbeat.mint_boot_id`) — so
#: ``COALESCE(<no live advertisement>, this)`` reliably compares UNEQUAL
#: and the epoch arm fires when there's no live (host, process) row to
#: compare against at all. Must be a plain SQL-safe string (NUL bytes
#: aren't valid in a postgres ``text`` value/param).
NO_ADVERTISED_BOOT_ID = "__no_advertised_boot_id__"

#: Reclaim-reason tags (forensic) — ``EPOCH`` = the holder was provably
#: replaced (redeploy/bounce); ``EXPIRY`` = the lease/TTL ran out with no
#: proof the holder changed (a hang, or no successor info). Job leases
#: additionally use the distinction for the poison-guard attempt bump
#: (only EXPIRY burns it — see ``claim_executor_jobs``).
RECLAIM_WHY_EPOCH = "epoch"
RECLAIM_WHY_EXPIRY = "expiry"


def reserve_host() -> str:
    """This host's identity for resource reservation / claim stamping.

    Must match the key the ``heartbeat`` self-probe writes
    ``resource_slots`` and ``host_heartbeat`` rows under
    (``PRECIS_HOST_NAME`` or the hostname) — so a reclaim comparison
    lands on the same row the probe advertised.
    """
    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


def worker_identity() -> tuple[str | None, str | None, str | None]:
    """``(boot_id, process, host)`` this worker stamps onto every claim.

    Returns the real triple ONLY when BOTH a boot_id has been minted AND
    ``PRECIS_PROCESS`` is set — i.e. only when this worker's boot_id is
    actually *advertised* in ``host_heartbeat.meta.boot_ids`` (see
    ``heartbeat._own_boot_ids_meta``, which requires the same two
    things). Otherwise ``(None, None, None)`` so the claim stamps no
    identity and falls to the safe TTL/expiry-only arm.

    The asymmetry matters: ``mint_boot_id()`` can succeed even when
    ``PRECIS_PROCESS`` is unset — but such a worker's id is never
    advertised, so a stamped claim would read as "provably gone" to the
    epoch arm and be stolen out from under a live holder. Unadvertised
    workers must not stamp.
    """
    from precis.workers.heartbeat import current_boot_id

    process = os.environ.get("PRECIS_PROCESS") or None
    boot_id = current_boot_id()
    if boot_id is None or process is None:
        return None, None, None
    return boot_id, process, reserve_host()


def advertised_boot_ids(conn: Connection) -> dict[tuple[str, str], str]:
    """``(host, process) -> currently-advertised boot_id`` from
    ``host_heartbeat.meta.boot_ids`` (the worker boot epoch).

    One snapshot per detection pass — reclaim classification then runs
    in Python against it instead of re-querying per row. Destructive
    actions must still re-check the target row by id inside their own
    transaction (spine safety law #3).
    """
    rows = conn.execute(
        "SELECT host, meta -> 'boot_ids' FROM host_heartbeat"
    ).fetchall()
    out: dict[tuple[str, str], str] = {}
    for host, boot_ids in rows:
        if not boot_ids:
            continue
        for process, boot_id in dict(boot_ids).items():
            out[(str(host), str(process))] = str(boot_id)
    return out


def reclaim_reason(
    boot_id: str | None,
    host: str | None,
    process: str | None,
    advertised: dict[tuple[str, str], str],
) -> str:
    """Classify a claim's reclaim as ``epoch`` or ``expiry``.

    ``epoch`` iff the claim carries a ``boot_id`` that does NOT match the
    currently-advertised generation for its ``(host, process)`` —
    including "no advertisement at all" (provably not current). A claim
    with no ``boot_id``, or one still matching the live advertisement
    (the plain-hang case), is ``expiry``.
    """
    if boot_id:
        if advertised.get((str(host), str(process))) != boot_id:
            return RECLAIM_WHY_EPOCH
    return RECLAIM_WHY_EXPIRY
