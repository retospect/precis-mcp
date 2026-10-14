"""Reserve a LOCAL serving slot around a dispatch (slice 7 part 2 / §6).

When an ``llm`` card declares ``served_by`` on this host, slice-7 part 1 seeds a
``resource_slots`` row ``llm:<model>`` (``max_parallel`` = capacity). This module
is the consumer: an inline dispatch to that model reserves one of the host's
local slots for the call's duration, calls localhost, releases — so the number
of concurrent local calls to a model can never exceed what the host declared it
can serve. It replaces litellm's load-balancer with claim-gated local
reservation (no cross-node balancer).

**Cluster-scoped serving.** The reservation target is ``(me, resource)`` when
this host serves the model. When it doesn't, a ``served_by`` entry on ANOTHER
host whose ``endpoint`` is LAN-routable (not loopback) is acquirable from here
too: the slot is reserved against *that* entry's host row — ``resource_slots``
lives in the shared DB, so ``max_parallel`` stays one fleet-wide semaphore —
and the reserved slot carries the remote endpoint for direct dispatch. This is
what lets every node send ``llm.chain.big``'s local rung to the DGX-pair
llama-server (its ``served_by`` publishes a LAN URL) instead of falling back
to the hosted cloud endpoint; a loopback-only entry (melchior's llama-swap at
``127.0.0.1``) stays host-private exactly as before. The ``host`` label on a
``served_by`` entry is thus an *accounting key* (whose slot row is debited),
not necessarily the machine the server runs on — see the deepseek card, whose
label ``caspar`` is historical while the endpoint IP is castor.

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

**Crash-safe reclaim.** Every successful reserve also opens a TTL
``resource_slot_holds`` row (:data:`_HOLD_TTL_S`, migration 0118) in the same
transaction; :func:`release` closes it and refunds only if the close won the
race against the heartbeat sweep (:meth:`Store.reclaim_expired_slot_holds`).
So a process killed mid-call — the 2026-08-10 fleet-wide ``llm:*`` outage,
every host wedged at ``free = 0`` — self-heals within one TTL instead of
leaking the unit forever.
"""

from __future__ import annotations

import logging
import os
import socket
import time
from dataclasses import dataclass, replace
from typing import Any
from urllib.parse import urlparse

log = logging.getLogger(__name__)

#: Default TTL (seconds) for a reservation's crash-reclaim hold
#: (``resource_slot_holds``, migration 0118) — overridable via
#: ``PRECIS_SLOT_HOLD_TTL_S``. Sized above the longest legitimate call a
#: dispatch can make: a quest tick's per-rung wall ceiling is 900s
#: (``PRECIS_QUEST_TICK_LLM_TIMEOUT_S``) and a 2-rung BIG chain can spend
#: 2×900s back-to-back under one dispatch, so an hour bounds how long a
#: crashed holder's unit can stay leaked without the sweeper reclaiming a
#: slot out from under a live long-reasoning call.
_HOLD_TTL_S = 3600.0


def _hold_ttl_s() -> float:
    raw = os.environ.get("PRECIS_SLOT_HOLD_TTL_S")
    if raw:
        try:
            v = float(raw)
            if v > 0:
                return v
        except ValueError:
            pass
    return _HOLD_TTL_S


#: TTL for the per-host served-resource set. The set changes only when
#: ``served_by`` is added/removed on a card (reconcile, ~daily), so a minute is
#: ample and keeps the dark hot path off the DB.
_CACHE_TTL_S = 60.0

#: How long :func:`acquire` waits for a slot to free before returning
#: ``paused`` — the bounded-wait half of the capacity-backoff fix
#: (2026-10-14): a saturated local slot used to spill straight to the cloud
#: rung, which put ~78% of routed-local calls on the cloud at melchior's
#: cap-4 slot. A short in-line wait converts that spend into local latency
#: for bursty callers. ``PRECIS_LOCAL_SLOT_WAIT_S`` overrides; ``0`` restores
#: the old immediate-pause.
_WAIT_S = 10.0

#: Poll interval for the acquire wait loop. Small enough to catch a slot
#: the moment a short call releases it; large enough to stay off the DB.
_WAIT_POLL_S = 0.5


def _wait_s() -> float:
    raw = os.environ.get("PRECIS_LOCAL_SLOT_WAIT_S")
    if raw:
        try:
            v = float(raw)
            if v >= 0:
                return v
        except ValueError:
            pass
    return _WAIT_S


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
    default loopback wire (the Phase-2 litellm-retirement flip; §6/§15a). A
    ``served_by`` with NO ``endpoint`` leaves both ``None`` → today's slot-only
    behavior (the call still goes to whatever ``LlmConfig.from_env`` dials)."""

    host: str
    resource: str
    reserved: bool
    paused: bool
    endpoint: str | None = None
    served_model: str | None = None
    #: id of the crash-reclaim hold opened alongside a successful reserve
    #: (``None`` for a paused/unreserved outcome). :func:`release` closes it
    #: and only refunds if the close actually deleted a row — a miss means
    #: the heartbeat sweep already reclaimed it (and already refunded).
    hold_id: int | None = None


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

#: {resource -> [(declared host, _Served), ...]} — models served on OTHER
#: hosts behind a LAN-routable endpoint (the cluster-scoped acquire path),
#: candidates sorted by declared host so the pick is deterministic. Same TTL
#: discipline.
_remote: dict[str, list[tuple[str, _Served]]] = {}
_remote_at: float = 0.0


def reset_cache() -> None:
    """Drop the served-resource + endpoint caches (tests + after a slot write)."""
    global _served, _served_at, _endpoints, _endpoints_at, _mismatch_warned
    global _remote, _remote_at
    _served = {}
    _served_at = 0.0
    _endpoints = {}
    _endpoints_at = 0.0
    _remote = {}
    _remote_at = 0.0
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


def _served_resources(store: Any, host: str) -> set[str]:
    global _served, _served_at, _mismatch_warned
    now = time.monotonic()
    if host not in _served or now - _served_at > _CACHE_TTL_S:
        try:
            with store.pool.connection() as conn:
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


def _same_family(a: str, b: str) -> bool:
    """True when two model names look like the *same* served model under a
    naming variant — a served_by mismatch worth flagging — rather than two
    unrelated models. Either name is a token-boundary prefix of the other (a
    dropped quant/precision suffix, ``qwen3-next-80b`` vs the served
    ``qwen3-next-80b-a3b-q4_k_m``), or they share a two-token vendor+series
    stem (``gpt-oss-120b`` vs ``gpt-oss-20b``). A frontier cloud model shares
    neither with a served OSS model (``claude-opus-4-8`` vs ``qwen3-next-…``),
    so its mismatch warning is the false alarm gr178888 flagged."""
    if a == b or a.startswith(b + "-") or b.startswith(a + "-"):
        return True
    ta, tb = a.split("-"), b.split("-")
    return len(ta) >= 2 and len(tb) >= 2 and ta[:2] == tb[:2]


def _plausibly_served_here(model: str, served_resources: set[str]) -> bool:
    """Whether ``model`` looks like it *should* have been served on this host —
    it shares a model family (:func:`_same_family`) with some ``llm:`` resource
    the host actually serves. Only then is a fallback-to-local a likely
    ``served_by`` naming mistake worth a warning; for an unrelated (cloud) model
    the fallback is the intended path, not a misconfiguration (gr178888)."""
    return any(
        _same_family(model, r[4:] if r.startswith("llm:") else r)
        for r in served_resources
    )


def _routable(endpoint: str | None) -> bool:
    """Whether a ``served_by`` endpoint is reachable from OTHER hosts — a real
    address, not a loopback/wildcard bind. Loopback entries are host-private by
    construction (melchior's llama-swap publishes ``127.0.0.1``); only a
    routable one may be acquired cluster-wide."""
    if not endpoint:
        return False
    host = urlparse(endpoint).hostname or ""
    return host not in {"", "localhost", "127.0.0.1", "::1", "0.0.0.0"}


def _remote_served(store: object) -> dict[str, list[tuple[str, _Served]]]:
    """``{resource -> [(declared host, _Served), ...]}`` for models served on
    other hosts behind a LAN-routable endpoint (60s TTL, same discipline as
    the other caches). The declared host is the ``resource_slots`` accounting
    key the cluster-scoped acquire debits — NOT necessarily where the server
    runs (the module docstring's caspar/castor note). Candidates are sorted
    by declared host so the pick is deterministic across cache windows; every
    candidate is tried in turn so a second serving host takes the spill when
    the first is saturated. Consulted only after a local-serve miss or a
    busy local slot, so the common all-local path pays one cached dict
    lookup. Any failure degrades to an empty map (→ the ordinary dark
    no-op)."""
    global _remote, _remote_at
    now = time.monotonic()
    if _remote_at == 0.0 or now - _remote_at > _CACHE_TTL_S:
        try:
            m: dict[str, list[tuple[str, _Served]]] = {}
            local = _local_host()
            for card in store.list_refs(kind="llm", limit=1000):  # type: ignore[attr-defined]
                meta = getattr(card, "meta", None) or {}
                model_id = meta.get("model_id")
                if not model_id:
                    continue
                for entry in _iter_served_by(meta):
                    entry_host = entry.get("host")
                    ep = entry.get("endpoint")
                    if not entry_host or str(entry_host) == local:
                        continue
                    if not (isinstance(ep, str) and _routable(ep)):
                        continue
                    key = f"llm:{model_id}"
                    m.setdefault(key, []).append(
                        (
                            str(entry_host),
                            _Served(
                                endpoint=ep,
                                served_model=str(entry.get("model") or model_id),
                            ),
                        )
                    )
            for candidates in m.values():
                candidates.sort(key=lambda c: c[0])
            _remote = m
            _remote_at = now
        except Exception:  # pragma: no cover — must never break dispatch
            log.warning("local_serving: remote served_by lookup failed", exc_info=True)
            return {}
    return _remote


def served_locally(model: str) -> bool:
    """Whether THIS host can reach ``llm:<model>`` — advertised here or behind a
    LAN-routable ``served_by`` entry on another host — a read-only membership
    test with no slot reservation.

    For a caller that must decide, *before* dispatching, whether the local
    loopback wire has a live endpoint for ``model`` (a served host pins a real
    llama-swap endpoint via :func:`acquire`; a non-serving host would otherwise
    fall to the retired ``:4000`` default and ECONNREFUSE). Returns ``False``
    when there is no process store — can't tell, so assume not served, matching
    :func:`acquire`'s dark-path default. Reuses the same host-scoped served-set
    (and its cache) as :func:`acquire`, so it costs at most one cached set test.
    """
    if not model:
        return False
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return False
    resource = f"llm:{model}"
    if resource in _served_resources(store, _local_host()):
        return True
    # Also reachable through a LAN-routable served_by entry on another host
    # (the cluster-scoped path acquire() takes after a local miss): the loopback
    # wire is then repointed at that endpoint, so the rung is live here too.
    return resource in _remote_served(store)


def acquire(model: str, *, wait_s: float | None = None) -> LocalSlot | None:
    """Reserve a local serving slot for ``model`` if this host serves it.

    Returns ``None`` when there is nothing to reserve — no process store, or the
    model is served neither on this host nor on any host behind a LAN-routable
    endpoint (the dark case): the caller proceeds unreserved, byte-identical to
    pre-slice-7. Otherwise returns a :class:`LocalSlot` with ``reserved=True``
    (proceed, then :func:`release`) or ``paused=True`` (served, but every slot
    busy — back off).

    Capacity backoff (2026-10-14): a busy slot does NOT pause on first sight.
    Each attempt tries this host first, then every LAN-routable ``served_by``
    candidate on other hosts (``_remote_served``, lowest host name first), so
    a saturated local slot spills to another local box before the caller
    fails over to cloud; and the whole thing retries until the bounded
    ``wait_s`` deadline (default :data:`_WAIT_S`, env
    ``PRECIS_LOCAL_SLOT_WAIT_S``; ``0`` restores the old immediate pause),
    so a burst queues briefly on local capacity instead of spending on the
    fallback rung. The dark ``None`` path never waits.

    A host that serves *other* ``llm:`` resources but not this one (and no
    remote match) logs a rate-limited warning (once per cache window) — that
    combination is a name mismatch, not the ordinary dark case, and would
    otherwise silently degrade to the local transport.
    """
    if not model:
        return None
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return None
    host = _local_host()
    resource = f"llm:{model}"
    wait = _wait_s() if wait_s is None else max(0.0, wait_s)
    deadline = time.monotonic() + wait
    while True:
        slot = _acquire_once(store, host, resource, model)
        if slot is None or slot.reserved or time.monotonic() >= deadline:
            return slot
        time.sleep(min(_WAIT_POLL_S, deadline - time.monotonic()))


def _acquire_once(
    store: object, host: str, resource: str, model: str
) -> LocalSlot | None:
    """One reservation attempt for ``resource``: this host's row first, then
    each remote candidate when the model is not served here *or the local
    row is full*. ``None`` = nothing to reserve anywhere (dark); ``paused``
    = served somewhere but every candidate refused."""
    served_resources = _served_resources(store, host)
    if resource in served_resources:
        slot = _reserve_one(store, host, resource)
        if slot is None:
            return None  # reservation failure must never break dispatch
        if slot.reserved:
            # Enrich with the card's direct endpoint (if declared), so the
            # router can route to llama-swap instead of the default loopback
            # wire. Looked up only once served + reserved — the dark no-op
            # path never touches it.
            served = _served_endpoints(store, host).get(resource)
            if served is not None:
                slot = replace(
                    slot, endpoint=served.endpoint, served_model=served.served_model
                )
            return slot
        # Local row full — spill to another serving host before pausing.
        remote = _acquire_remote(store, resource)
        return remote if remote is not None else slot
    remote = _acquire_remote(store, resource)
    if remote is not None:
        return remote
    _warn_family_mismatch(host, model, resource, served_resources)
    return None  # not served here — dark no-op, no DB hit


def _reserve_one(store: object, host: str, resource: str) -> LocalSlot | None:
    """A single reserve against ``host``'s ``resource_slots`` row: reserved
    slot on success (with its crash-reclaim hold), ``paused`` when full,
    ``None`` when the bookkeeping itself fails — that must never break
    dispatch."""
    from precis.store._resource_slots_ops import (
        insert_slot_hold,
        reserve_resource_slots,
    )

    hold_id: int | None = None
    try:
        with store.pool.connection() as conn:  # type: ignore[attr-defined]
            with conn.transaction():
                ok = reserve_resource_slots(conn, host, {resource: 1})
                if ok:
                    from precis.liveness import worker_identity

                    hold_id = insert_slot_hold(
                        conn,
                        host,
                        resource,
                        1,
                        f"{_local_host()}:{os.getpid()}",
                        _hold_ttl_s(),
                        holder_identity=worker_identity(),
                    )
    except Exception:  # pragma: no cover — reservation must never break dispatch
        log.warning(
            "local_serving: reserve failed for %s on %s",
            resource,
            host,
            exc_info=True,
        )
        return None
    return LocalSlot(
        host=host,
        resource=resource,
        reserved=ok,
        paused=not ok,
        hold_id=hold_id,
    )


def _acquire_remote(store: object, resource: str) -> LocalSlot | None:
    """Try each LAN-routable ``served_by`` candidate for ``resource`` in
    declared-host order — reserve against THAT entry's host row (one shared
    semaphore in the DB, so ``max_parallel`` caps the fleet, not each host)
    and hand dispatch the remote endpoint exactly as a local slot would.
    First success wins; all-busy returns the first candidate's ``paused``
    outcome; no candidates (or every attempt erroring) returns ``None``."""
    candidates = _remote_served(store).get(resource)
    if not candidates:
        return None
    busy: LocalSlot | None = None
    for remote_host, remote_served in candidates:
        slot = _reserve_one(store, remote_host, resource)
        if slot is None:
            continue
        if slot.reserved:
            return replace(
                slot,
                endpoint=remote_served.endpoint,
                served_model=remote_served.served_model,
            )
        busy = busy or slot
    return busy


def _warn_family_mismatch(
    host: str, model: str, resource: str, served_resources: set[str]
) -> None:
    """A host that serves *other* llm: resources but not this one is usually a
    served_by name mismatch worth flagging — but only when the requested
    model *plausibly should* be served here. Two false-alarm classes are
    suppressed:

    * SMALL-tier loopback aliases (``summarizer`` / ``rake-lemma``) route
      through the loopback local transport by design, never a reserved
      llama-swap slot, so "falling back to local" is intended. The
      in-process dedup meant this flooded once per short-lived summarize
      worker (gr178498: 3907 hits/48h on melchior, all ``summarizer``).
    * Legitimately-cloud models (``claude-opus-4-8`` and other frontier
      tiers melchior is never expected to serve) — falling back to the
      cloud is correct, not a misconfiguration (gr178888).

    So warn only for a model in the same family as something served here
    (a real quant/suffix naming near-miss); an unrelated model stays dark.
    """
    from precis.utils.llm.router import _LOCAL_ONLY_MODEL_ALIASES

    if (
        served_resources
        and model not in _LOCAL_ONLY_MODEL_ALIASES
        and _plausibly_served_here(model, served_resources)
    ):
        warned = _mismatch_warned.setdefault(host, set())
        if resource not in warned:
            warned.add(resource)
            log.warning(
                "local_serving: host %s serves %s locally but dispatch asked "
                "for %s — falling back to local (check served_by naming)",
                host,
                sorted(served_resources),
                resource,
            )


def _served_endpoints(store: object, host: str) -> dict[str, _Served]:
    """Per-host ``{resource -> _Served}`` from the cards' ``served_by`` (60s TTL).

    The authoritative endpoint source: the ``llm`` card's ``served_by`` entry for
    this host carries ``endpoint`` (the local server's OpenAI base URL) and an
    optional server-side ``model`` name (defaults to the card's ``model_id``).
    Read only when a resource is already confirmed served, so the dark path pays
    nothing. Any failure degrades to an empty map (no direct routing → the call
    falls back to the default loopback wire)."""
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
    that was never reserved (the paused or dark outcomes).

    When the reservation carries a crash-reclaim hold (:attr:`LocalSlot.hold_id`
    — the normal case), the hold is closed first and the refund only fires if
    that close actually deleted a row: a miss means the heartbeat sweep
    already reclaimed the (expired) hold and already refunded it, so
    refunding again here would double-count (the ``LEAST`` cap masks it at
    full capacity but corrupts accounting below it). A ``None`` hold_id
    (legacy/job-path callers of the module-level helpers directly) keeps
    today's unconditional refund.
    """
    if slot is None or not slot.reserved:
        return
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return
    from precis.store._resource_slots_ops import (
        delete_slot_hold,
        release_resource_slots,
    )

    try:
        with store.pool.connection() as conn:
            with conn.transaction():
                if slot.hold_id is not None:
                    if delete_slot_hold(conn, slot.hold_id):
                        release_resource_slots(conn, slot.host, {slot.resource: 1})
                    # else: sweep already reclaimed + refunded this hold.
                else:
                    release_resource_slots(conn, slot.host, {slot.resource: 1})
    except Exception:  # pragma: no cover — release must never break the caller
        log.warning(
            "local_serving: release failed for %s", slot.resource, exc_info=True
        )


__all__ = ["LocalSlot", "acquire", "release", "reset_cache", "served_locally"]
