"""Live, DB-driven run control for worker passes (factory slice 2).

The ``service_config`` table (migration 0072) is the switch the worker
consults *live* instead of a plist ``EnvironmentVariables`` gate that
needs an edit → re-render → ``launchctl bootout``/``bootstrap`` cycle.
``prio`` is both the switch and the scheduling weight:

* ``0``      — do not run (the live off switch),
* ``1..10``  — run at this claim weight (fed into the scarcity+prio+age
  claim ordering the capability scheduler layers on in slice 6).

A missing row means "fall back to the env/profile default", so an empty
table is byte-identical to today's behaviour. :class:`ServiceConfigResolver`
is the read side (a short-TTL cache so the per-cycle gate is a dict
lookup, not a query per pass per cycle); :func:`set_service_prio` /
:func:`list_service_config` / :func:`clear_service_config` are the write +
inspect side the ``precis service`` CLI (and later the ``/factory``
console) drive.

``concurrency`` (migration 0091) is a second live knob resolved the same
way as ``prio`` (exact-host-over-``*``, TTL-cached): the in-pass thread-pool
width a cloud-calling categorizer (``classify``) fans its per-row
LLM cascade across. ``NULL``/no row -> 1 (today's serial behaviour, so an
empty table is unchanged); :func:`set_service_concurrency` is its write side.

``expires_at`` (migration 0104) backs the §B-2 **reserve mode**: a
``(host | '*', service='reserve')`` row is a pseudo-service — nothing calls
``enabled('reserve')``; it exists purely so :func:`reserve_active` (checked
live inside the claim transaction, :func:`~precis.workers.executors._common.
claim_executor_jobs`) can gate ALL new heavy (``ssh_node``/``claude_docker``)
claims on that host until the row's ``expires_at`` passes. :func:`set_reserve`
/ :func:`clear_reserve` are its write side (``precis service reserve|release``
CLI); :func:`reserve_active`'s predicate — not a background reaper — is what
makes an unattended reserve auto-expire.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime

from psycopg import Connection

from precis.store import Store

#: The claim weight a profile/enable_env pass runs at when it is enabled
#: and no explicit ``service_config`` row overrides it. Mid-point of the
#: refs.prio 1..10 scale the todo/quest layers already use.
DEFAULT_PRIO = 5

#: ``host`` value meaning "every host"; an exact-host row wins over it.
ALL_HOSTS = "*"


@dataclass
class ServiceConfigResolver:
    """Resolve the effective ``prio`` for a service on this host, cached.

    ``prio(service, default=)`` returns the DB override when a row exists
    (exact host preferred over the ``*`` wildcard), else ``default``. The
    cache refreshes every ``ttl_s`` seconds so a live flip is picked up
    within one TTL window without a query per pass per cycle.
    """

    store: Store
    host: str
    ttl_s: float = 5.0
    _cache: dict[str, tuple[int, int | None]] = field(default_factory=dict)
    _fetched_at: float = field(default=-1e18)

    def _rows(self) -> dict[str, tuple[int, int | None]]:
        """service -> (prio, concurrency), refreshed every ``ttl_s``.

        Both fields come from the SAME winning row (exact host beats the
        ``*`` wildcard) — a service's prio and concurrency are never mixed
        across two different-specificity rows.
        """
        now = time.monotonic()
        if now - self._fetched_at < self.ttl_s:
            return self._cache
        # service -> (specificity, prio, concurrency)
        rows: dict[str, tuple[int, int, int | None]] = {}
        try:
            with self.store.pool.connection() as conn:
                cur = conn.execute(
                    "SELECT service, host, prio, concurrency FROM service_config "
                    "WHERE host IN (%s, %s)",
                    (self.host, ALL_HOSTS),
                )
                for service, host, prio, concurrency in cur.fetchall():
                    # Exact-host row (specificity 1) wins over wildcard (0).
                    spec = 1 if host == self.host else 0
                    prev = rows.get(service)
                    if prev is None or spec >= prev[0]:
                        rows[service] = (
                            spec,
                            int(prio),
                            int(concurrency) if concurrency is not None else None,
                        )
        except Exception:
            # Table missing (pre-migration) / connection blip: fall back to
            # env/profile defaults rather than killing the gate. Cache the
            # empty result so we don't hammer a broken DB every cycle.
            rows = {}
        self._cache = {
            svc: (prio, concurrency) for svc, (_, prio, concurrency) in rows.items()
        }
        self._fetched_at = now
        return self._cache

    def prio(self, service: str, *, default: int = DEFAULT_PRIO) -> int:
        """Effective claim weight for ``service`` (DB override else default)."""
        row = self._rows().get(service)
        return row[0] if row is not None else default

    def concurrency(self, service: str, *, default: int = 1) -> int:
        """Effective in-pass LLM-call concurrency for ``service``.

        DB override (exact host beats ``*``) when a row carries a non-NULL
        ``concurrency``, else ``default`` — so an absent row OR a row with
        ``concurrency`` left NULL both fall through to ``default`` (today's
        serial behaviour, byte-identical). The caller (``cli/worker.py``) is
        responsible for clamping this at a hard ceiling before use.
        """
        row = self._rows().get(service)
        if row is None or row[1] is None:
            return default
        return row[1]

    def enabled(self, service: str, *, default_on: bool) -> bool:
        """True when ``service`` should run.

        ``default_on`` is the env/profile verdict (in the running profile's
        rotation, or its ``enable_env`` flag is set). With no DB row the
        service runs iff ``default_on``; a ``prio 0`` row forces it off and a
        ``prio >= 1`` row forces it on regardless — the live switch.
        """
        return self.prio(service, default=DEFAULT_PRIO if default_on else 0) > 0

    def invalidate(self) -> None:
        """Drop the cache so the next call re-reads (tests / after a write)."""
        self._fetched_at = -1e18


def set_service_prio(
    store: Store,
    host: str,
    service: str,
    prio: int,
    *,
    model_pref: str | None = None,
    actor: str | None = None,
) -> None:
    """Upsert the ``(host, service)`` run control. ``prio`` is 0..10."""
    if not 0 <= prio <= 10:
        raise ValueError(f"prio must be 0..10, got {prio}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config "
            "(host, service, prio, model_pref, actor, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (host, service) DO UPDATE SET "
            "  prio = EXCLUDED.prio, "
            # Only overwrite model_pref when a new one is supplied, so a
            # `service prio` flip doesn't wipe a model pin set separately.
            "  model_pref = COALESCE(EXCLUDED.model_pref, service_config.model_pref), "
            "  actor = EXCLUDED.actor, "
            "  updated_at = EXCLUDED.updated_at",
            (host, service, prio, model_pref, actor),
        )
        conn.commit()


def seed_service_prio(
    store: Store,
    host: str,
    service: str,
    prio: int,
    *,
    actor: str | None = None,
) -> bool:
    """Insert ``(host, service, prio)`` only if no row exists yet; a no-op
    otherwise. Returns True when a row was inserted.

    This is the §L deploy-time seed write — distinct from
    :func:`set_service_prio` (the operator-facing ``precis service prio`` /
    ``/factory`` console UPSERT, which INTENTIONALLY overwrites). A seed task
    runs on every deploy to mirror a retiring ``PRECIS_*_ENABLED`` plist flag
    into a row so the cutover is behaviour-preserving; using the UPSERT here
    would silently clobber a console operator's live override on the very
    next redeploy. ``ON CONFLICT DO NOTHING`` makes it safe to re-run forever.
    """
    if not 0 <= prio <= 10:
        raise ValueError(f"prio must be 0..10, got {prio}")
    with store.pool.connection() as conn:
        cur = conn.execute(
            "INSERT INTO service_config "
            "(host, service, prio, actor, updated_at) "
            "VALUES (%s, %s, %s, %s, now()) "
            "ON CONFLICT (host, service) DO NOTHING",
            (host, service, prio, actor),
        )
        conn.commit()
        return cur.rowcount > 0


def set_service_model(
    store: Store,
    host: str,
    service: str,
    model_pref: str | None,
    *,
    actor: str | None = None,
) -> None:
    """Set (or clear, with ``None``) a service's model pin without touching prio.

    Inserts at the default prio when the row is new so a model pin can be
    expressed before an explicit prio flip. Used by the slice-4 model picker.
    """
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config "
            "(host, service, prio, model_pref, actor, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (host, service) DO UPDATE SET "
            "  model_pref = EXCLUDED.model_pref, "
            "  actor = EXCLUDED.actor, "
            "  updated_at = EXCLUDED.updated_at",
            (host, service, DEFAULT_PRIO, model_pref, actor),
        )
        conn.commit()


def set_service_concurrency(
    store: Store,
    host: str,
    service: str,
    concurrency: int | None,
    *,
    actor: str | None = None,
) -> None:
    """Set (or clear, with ``None``) a service's in-pass concurrency without
    touching prio.

    Mirrors :func:`set_service_model` — inserts at the default prio when the
    row is new, so a concurrency knob can be set before an explicit prio
    flip. ``None`` reverts the service to the default (1, serial).
    """
    if concurrency is not None and concurrency < 1:
        raise ValueError(f"concurrency must be >= 1 or None, got {concurrency}")
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO service_config "
            "(host, service, prio, concurrency, actor, updated_at) "
            "VALUES (%s, %s, %s, %s, %s, now()) "
            "ON CONFLICT (host, service) DO UPDATE SET "
            "  concurrency = EXCLUDED.concurrency, "
            "  actor = EXCLUDED.actor, "
            "  updated_at = EXCLUDED.updated_at",
            (host, service, DEFAULT_PRIO, concurrency, actor),
        )
        conn.commit()


def clear_service_config(store: Store, host: str, service: str) -> bool:
    """Delete the ``(host, service)`` row (revert to env/profile default).

    Returns True when a row was removed.
    """
    with store.pool.connection() as conn:
        cur = conn.execute(
            "DELETE FROM service_config WHERE host = %s AND service = %s",
            (host, service),
        )
        conn.commit()
        return cur.rowcount > 0


def list_service_config(store: Store) -> list[dict[str, object]]:
    """All configured rows, ordered by host then service (for the CLI/console)."""
    with store.pool.connection() as conn:
        cur = conn.execute(
            "SELECT host, service, prio, model_pref, write_level, "
            "       concurrency, expires_at, updated_at, actor "
            "FROM service_config ORDER BY host, service"
        )
        assert cur.description is not None
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


# ── Reserve mode (the human-first GPU reservation) ─────────────────────
#
# A pseudo-service: `(host | '*', service='reserve', prio=1, expires_at=
# <required>)`. Nothing ever calls `ServiceConfigResolver.enabled('reserve')`
# — its only reader is `reserve_active`, called live inside the claim
# transaction. Showing it in `precis service list` (and later the §K
# console) is a feature, not a leak.

#: The pseudo-service name a reserve row is stored under.
RESERVE_SERVICE = "reserve"

#: Upper bound on `set_reserve`'s `hours` — a longer reserve is a config
#: change (use `service prio 0`), not a "mode" an operator might forget to
#: lift. One week.
_RESERVE_MAX_HOURS = 168.0


def set_reserve(
    store: Store,
    host: str,
    *,
    hours: float = 4.0,
    actor: str | None = None,
) -> datetime:
    """Put ``host`` (or ``ALL_HOSTS`` for every host) into reserve mode for
    ``hours`` (default 4; refuses ``<= 0`` or ``> 168`` — a week is a config
    change, not a mode). UPSERT — reserving an already-reserved host resets
    the expiry to ``now() + hours``. Returns the new ``expires_at`` (for the
    CLI to print).
    """
    if not 0 < hours <= _RESERVE_MAX_HOURS:
        raise ValueError(f"hours must be in (0, {_RESERVE_MAX_HOURS}], got {hours}")
    with store.pool.connection() as conn:
        cur = conn.execute(
            "INSERT INTO service_config "
            "(host, service, prio, expires_at, actor, updated_at) "
            "VALUES (%s, %s, 1, now() + make_interval(secs => %s), %s, now()) "
            "ON CONFLICT (host, service) DO UPDATE SET "
            "  prio = 1, "
            "  expires_at = EXCLUDED.expires_at, "
            "  actor = EXCLUDED.actor, "
            "  updated_at = EXCLUDED.updated_at "
            "RETURNING expires_at",
            (host, RESERVE_SERVICE, hours * 3600.0, actor),
        )
        row = cur.fetchone()
        conn.commit()
        assert row is not None
        return row[0]  # type: ignore[no-any-return]


def clear_reserve(store: Store, host: str) -> bool:
    """Delete ``host``'s reserve row. Returns True when one was removed."""
    return clear_service_config(store, host, RESERVE_SERVICE)


def reserve_active(conn: Connection, host: str) -> bool:
    """True when ``host`` (or the ``'*'`` wildcard) carries a live
    (unexpired) reserve row.

    Takes a LIVE conn so it can be checked inside the claim transaction
    (:func:`~precis.workers.executors._common.claim_executor_jobs`) — one
    cheap indexed ``SELECT`` per heavy-claim pass, no cache. Auto-expiry is
    this predicate alone: an expired row is simply inert (excluded by
    ``expires_at > now()``); nothing reaps it, and the next
    :func:`set_reserve` UPSERTs over it regardless.
    """
    row = conn.execute(
        "SELECT 1 FROM service_config "
        "WHERE service = %s AND host IN (%s, %s) "
        "  AND expires_at IS NOT NULL AND expires_at > now() "
        "LIMIT 1",
        (RESERVE_SERVICE, host, ALL_HOSTS),
    ).fetchone()
    return row is not None


__all__ = [
    "ALL_HOSTS",
    "DEFAULT_PRIO",
    "RESERVE_SERVICE",
    "ServiceConfigResolver",
    "clear_reserve",
    "clear_service_config",
    "list_service_config",
    "reserve_active",
    "seed_service_prio",
    "set_reserve",
    "set_service_concurrency",
    "set_service_model",
    "set_service_prio",
]
