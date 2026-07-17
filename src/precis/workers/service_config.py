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
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

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
    _cache: dict[str, int] = field(default_factory=dict)
    _fetched_at: float = field(default=-1e18)

    def _rows(self) -> dict[str, int]:
        now = time.monotonic()
        if now - self._fetched_at < self.ttl_s:
            return self._cache
        rows: dict[str, tuple[int, int]] = {}  # service -> (specificity, prio)
        try:
            with self.store.pool.connection() as conn:
                cur = conn.execute(
                    "SELECT service, host, prio FROM service_config "
                    "WHERE host IN (%s, %s)",
                    (self.host, ALL_HOSTS),
                )
                for service, host, prio in cur.fetchall():
                    # Exact-host row (specificity 1) wins over wildcard (0).
                    spec = 1 if host == self.host else 0
                    prev = rows.get(service)
                    if prev is None or spec >= prev[0]:
                        rows[service] = (spec, int(prio))
        except Exception:
            # Table missing (pre-migration) / connection blip: fall back to
            # env/profile defaults rather than killing the gate. Cache the
            # empty result so we don't hammer a broken DB every cycle.
            rows = {}
        self._cache = {svc: prio for svc, (_, prio) in rows.items()}
        self._fetched_at = now
        return self._cache

    def prio(self, service: str, *, default: int = DEFAULT_PRIO) -> int:
        """Effective claim weight for ``service`` (DB override else default)."""
        return self._rows().get(service, default)

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
            "       updated_at, actor "
            "FROM service_config ORDER BY host, service"
        )
        cols = [d.name for d in cur.description]
        return [dict(zip(cols, row, strict=True)) for row in cur.fetchall()]


__all__ = [
    "ALL_HOSTS",
    "DEFAULT_PRIO",
    "ServiceConfigResolver",
    "clear_service_config",
    "list_service_config",
    "set_service_model",
    "set_service_prio",
]
