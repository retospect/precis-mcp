"""Reserve a LOCAL serving slot around a dispatch (slice 7 part 2 / §6).

When an ``llm`` card declares ``served_by`` on this host, slice-7 part 1 seeds a
``resource_slots`` row ``llm:<model>`` (``max_parallel`` = capacity). This module
is the consumer: an inline dispatch to that model reserves one of the host's
local slots for the call's duration, calls localhost, releases — so the number
of concurrent local calls to a model can never exceed what the host declared it
can serve. It replaces litellm's load-balancer with claim-gated local
reservation (no cross-node balancer; reservation target is always ``(me,
resource)``).

**Ships dark.** A model that is *not* served on this host — every model today,
until ``served_by`` is populated at the Phase-2 cutover — is a no-op: dispatch
proceeds exactly as before. The dark path is guarded by a short-TTL cache of
"which ``llm:`` resources this host serves", so it costs a set membership test,
never a DB round-trip. Only a model actually served here opens a connection to
reserve. Any failure degrades to a no-op — a slot bookkeeping error must never
break an LLM call.

Follows the same dark-gate discipline as :mod:`precis.utils.llm.admit` and the
budget breaker: read the process store via :func:`precis.budget.meter.active_store`,
return ``None`` (allow) whenever the machinery has nothing to say.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass

log = logging.getLogger(__name__)

#: TTL for the per-host served-resource set. The set changes only when
#: ``served_by`` is added/removed on a card (reconcile, ~daily), so a minute is
#: ample and keeps the dark hot path off the DB.
_CACHE_TTL_S = 60.0

#: {host -> {resource}} — the ``llm:`` resources this host serves. Single-process
#: (one host), refreshed past the TTL.
_served: dict[str, set[str]] = {}
_served_at: float = 0.0


@dataclass(frozen=True, slots=True)
class LocalSlot:
    """Outcome of an :func:`acquire`. ``reserved`` → the call may proceed and the
    caller MUST :func:`release`; ``paused`` → the host serves the model but every
    local slot is busy (the caller should back off, not spin)."""

    host: str
    resource: str
    reserved: bool
    paused: bool


def reset_cache() -> None:
    """Drop the served-resource cache (tests + after a known slot write)."""
    global _served, _served_at
    _served = {}
    _served_at = 0.0


def _local_host() -> str:
    """This node's name, matching the key the heartbeat probe writes slots under
    (``PRECIS_HOST_NAME`` then the hostname — the flagless
    ``heartbeat.resolve_host`` precedence)."""
    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


def _served_resources(store: object, host: str) -> set[str]:
    global _served, _served_at
    now = time.monotonic()
    if host not in _served or now - _served_at > _CACHE_TTL_S:
        try:
            with store.pool.connection() as conn:  # type: ignore[attr-defined]
                rows = conn.execute(
                    "SELECT resource FROM resource_slots "
                    "WHERE host = %s AND resource LIKE 'llm:%%'",
                    (host,),
                ).fetchall()
            _served = {host: {str(r[0]) for r in rows}}
            _served_at = now
        except Exception:  # pragma: no cover — a lookup must never break dispatch
            log.warning("local_serving: served-slot lookup failed", exc_info=True)
            return set()
    return _served.get(host, set())


def acquire(model: str) -> LocalSlot | None:
    """Reserve a local serving slot for ``model`` if this host serves it.

    Returns ``None`` when there is nothing to reserve — no process store, or the
    model is not served on this host (the dark case for every model today): the
    caller proceeds unreserved, byte-identical to pre-slice-7. Otherwise returns
    a :class:`LocalSlot` with ``reserved=True`` (proceed, then :func:`release`)
    or ``paused=True`` (host serves it but all slots busy — back off).
    """
    if not model:
        return None
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return None
    host = _local_host()
    resource = f"llm:{model}"
    if resource not in _served_resources(store, host):
        return None  # not served here — dark no-op, no DB hit
    from precis.store._resource_slots_ops import reserve_resource_slots

    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                ok = reserve_resource_slots(conn, host, {resource: 1})
    except Exception:  # pragma: no cover — reservation must never break dispatch
        log.warning("local_serving: reserve failed for %s", resource, exc_info=True)
        return None
    return LocalSlot(host=host, resource=resource, reserved=ok, paused=not ok)


def release(slot: LocalSlot | None) -> None:
    """Refund a slot reserved by :func:`acquire`. No-op for ``None`` / a slot
    that was never reserved (the paused or dark outcomes)."""
    if slot is None or not slot.reserved:
        return
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return
    from precis.store._resource_slots_ops import release_resource_slots

    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                release_resource_slots(conn, slot.host, {slot.resource: 1})
    except Exception:  # pragma: no cover — release must never break the caller
        log.warning(
            "local_serving: release failed for %s", slot.resource, exc_info=True
        )


__all__ = ["LocalSlot", "acquire", "release", "reset_cache"]
