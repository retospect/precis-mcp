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

A host that serves *some* local models but not the one requested (a naming
mismatch, distinct from serving nothing) logs a rate-limited ``log.warning`` —
still returns ``None`` (fully dark to the caller), purely for operator visibility.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

#: TTL for the per-host served-resource set. The set changes only when
#: ``served_by`` is added/removed on a card (reconcile, ~daily), so a minute is
#: ample and keeps the dark hot path off the DB.
_CACHE_TTL_S = 60.0

#: {host -> {resource}} — the ``llm:`` resources this host serves. Single-process
#: (one host), refreshed past the TTL.
_served: dict[str, set[str]] = {}
_served_at: float = 0.0

#: {host -> {resource}} already warned-about this cache window — a host that
#: serves *something* locally but not the requested resource (a real
#: misconfiguration, unlike the fully-dark "serves nothing" case) logs once per
#: (host, resource) per :data:`_CACHE_TTL_S` window, cleared alongside ``_served``.
_mismatch_warned: dict[str, set[str]] = {}


@dataclass(frozen=True, slots=True)
class LocalSlot:
    """Outcome of an :func:`acquire`. ``reserved`` → the call may proceed and the
    caller MUST :func:`release`; ``paused`` → the host serves the model but every
    local slot is busy (the caller should back off, not spin).

    When the card's ``served_by`` entry declares an ``endpoint`` (the local
    server's OpenAI base URL, e.g. llama-swap at ``http://127.0.0.1:11445/v1``),
    a reserved slot carries it in :attr:`endpoint` plus the server-side model
    name in :attr:`served_model` — the router overrides the local dispatch's URL
    + model with them so the call goes to llama-swap DIRECTLY instead of the
    litellm proxy (the Phase-2 litellm-retirement flip; §6/§15a). A ``served_by``
    with NO ``endpoint`` leaves both ``None`` → today's slot-only behavior (the
    call still goes to whatever ``LlmConfig.from_env`` dials)."""

    host: str
    resource: str
    reserved: bool
    paused: bool
    endpoint: str | None = None
    served_model: str | None = None


@dataclass(frozen=True, slots=True)
class _Served:
    """A host's ``served_by`` declaration for one model: the local endpoint URL
    (``None`` = slot-only, no direct routing) + the server-side model name."""

    endpoint: str | None
    served_model: str


#: {host -> {resource -> _Served}} — the endpoint/model each ``llm:`` resource is
#: served under on this host, from the cards' ``served_by``. Consulted only for a
#: resource already confirmed served (so the dark no-op path never loads it).
_endpoints: dict[str, dict[str, _Served]] = {}
_endpoints_at: float = 0.0


def reset_cache() -> None:
    """Drop the served-resource + endpoint caches (tests + after a slot write)."""
    global _served, _served_at, _endpoints, _endpoints_at, _mismatch_warned
    _served = {}
    _served_at = 0.0
    _endpoints = {}
    _endpoints_at = 0.0
    _mismatch_warned = {}


def _iter_served_by(meta: dict[str, Any]) -> list[dict[str, Any]]:
    """Every ``served_by`` entry on a card — card-level ``meta.served_by`` and
    each offering's nested ``served_by`` (§6 nests it under the local-serving
    offering). Mirrors ``llm_reconcile._iter_served_by`` (kept local so the hot
    path doesn't import the worker/DB chain)."""
    out: list[dict[str, Any]] = []
    for e in meta.get("served_by") or []:
        if isinstance(e, dict):
            out.append(e)
    for o in meta.get("offerings") or []:
        if isinstance(o, dict):
            for e in o.get("served_by") or []:
                if isinstance(e, dict):
                    out.append(e)
    return out


def _local_host() -> str:
    """This node's name, matching the key the heartbeat probe writes slots under
    (``PRECIS_HOST_NAME`` then the hostname — the flagless
    ``heartbeat.resolve_host`` precedence)."""
    return os.environ.get("PRECIS_HOST_NAME") or socket.gethostname()


def _served_resources(store: object, host: str) -> set[str]:
    global _served, _served_at, _mismatch_warned
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
            _mismatch_warned = {}  # new window — re-arm the mismatch warning
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
    or ``paused=True`` (host serves it but all slots busy — back off). A host
    that serves *other* ``llm:`` resources but not this one logs a rate-limited
    warning (once per cache window) — that combination is a name mismatch, not
    the ordinary dark case, and would otherwise silently degrade to litellm.
    """
    if not model:
        return None
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return None
    host = _local_host()
    resource = f"llm:{model}"
    served_resources = _served_resources(store, host)
    if resource not in served_resources:
        if served_resources:  # host serves *something* locally — a name mismatch
            warned = _mismatch_warned.setdefault(host, set())
            if resource not in warned:
                warned.add(resource)
                log.warning(
                    "local_serving: host %s serves %s locally but dispatch asked "
                    "for %s — falling back to litellm (check served_by naming)",
                    host,
                    sorted(served_resources),
                    resource,
                )
        return None  # not served here — dark no-op, no DB hit
    from precis.store._resource_slots_ops import reserve_resource_slots

    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                ok = reserve_resource_slots(conn, host, {resource: 1})
    except Exception:  # pragma: no cover — reservation must never break dispatch
        log.warning("local_serving: reserve failed for %s", resource, exc_info=True)
        return None
    # Enrich a reserved slot with the card's direct endpoint (if declared), so the
    # router can route to llama-swap instead of the litellm proxy. Looked up only
    # once served + reserved — the dark no-op path never touches it.
    endpoint: str | None = None
    served_model: str | None = None
    if ok:
        served = _served_endpoints(store, host).get(resource)
        if served is not None:
            endpoint = served.endpoint
            served_model = served.served_model
    return LocalSlot(
        host=host,
        resource=resource,
        reserved=ok,
        paused=not ok,
        endpoint=endpoint,
        served_model=served_model,
    )


def _served_endpoints(store: object, host: str) -> dict[str, _Served]:
    """Per-host ``{resource -> _Served}`` from the cards' ``served_by`` (60s TTL).

    The authoritative endpoint source: the ``llm`` card's ``served_by`` entry for
    this host carries ``endpoint`` (the local server's OpenAI base URL) and an
    optional server-side ``model`` name (defaults to the card's ``model_id``).
    Read only when a resource is already confirmed served, so the dark path pays
    nothing. Any failure degrades to an empty map (no direct routing → the call
    falls back to the litellm proxy)."""
    global _endpoints, _endpoints_at
    now = time.monotonic()
    if host not in _endpoints or now - _endpoints_at > _CACHE_TTL_S:
        try:
            m: dict[str, _Served] = {}
            for card in store.list_refs(kind="llm", limit=1000):  # type: ignore[attr-defined]
                meta = getattr(card, "meta", None) or {}
                model_id = meta.get("model_id")
                if not model_id:
                    continue
                for entry in _iter_served_by(meta):
                    if entry.get("host") != host:
                        continue
                    ep = entry.get("endpoint")
                    m[f"llm:{model_id}"] = _Served(
                        endpoint=ep if isinstance(ep, str) and ep else None,
                        served_model=str(entry.get("model") or model_id),
                    )
            _endpoints = {host: m}
            _endpoints_at = now
        except Exception:  # pragma: no cover — must never break dispatch
            log.warning("local_serving: endpoint lookup failed", exc_info=True)
            return {}
    return _endpoints.get(host, {})


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
