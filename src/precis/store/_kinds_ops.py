"""Boot-time upsert of the registered ``kinds`` (and one day,
``chunk_kinds``) catalogue.

Architectural note — see ``docs/decisions/`` (forthcoming) and the
session-summary commit body for the rationale.

Until 2026-06-16 the ``kinds`` table was maintained by hand-written
migrations: every new kind shipped an ``INSERT INTO kinds`` and every
rename shipped an ``UPDATE`` (e.g. 0018 ``UPDATE … WHERE slug='think'
→ 'perplexity-reasoning'``). The hub's runtime registry (code-driven)
and the table (migration-driven) drifted whenever a UPDATE matched
zero rows — exactly what bit us on 2026-06-16 when the renamed
``perplexity-*`` kinds were code-registered but absent from the table,
so the validator silently rejected every API call with ``unknown
kind``.

The fix is to make the **code** the canonical source: each handler's
:class:`KindSpec` carries the slug + title + description + is_numeric
flag the table needs, and at boot we just ``INSERT … ON CONFLICT
(slug) DO UPDATE`` for every registered kind. The runtime hub becomes
the per-process subset (kinds gated on requires_env drop out via
``KindSpec.requires_env``); the DB table is the union across every
process that ever booted with that kind enabled (suitable as FK
target for ``refs.kind``).

Migrations now stop touching ``kinds`` for kind introductions or
renames; they stay for schema changes (new columns, new tables,
new constraints).
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection

from precis.utils.hostname import is_container_id_host

log = logging.getLogger(__name__)


class KindsMixin:
    """Mixin: assumes the concrete Store provides ``self.pool``."""

    pool: Any

    def upsert_kind_providers(
        self,
        specs: list[Any],
        *,
        host: str,
        process: str,
        conn: Connection | None = None,
    ) -> int:
        """Per-process record of which kinds we currently advertise.

        Called from boot right after :meth:`upsert_kinds`. Each
        ``(slug, host, process)`` row gets a fresh ``last_seen``; the
        validator queries this table to render "kind X routes through
        hosts Y/Z" hints when a kind is missing from the local hub.

        Stale entries (rows whose owning process crashed and didn't
        re-upsert) are tolerated by the read side via a freshness
        cutoff (see :meth:`find_kind_providers`).

        A ``process="unknown"`` boot (``PRECIS_PROCESS`` unset — a local
        dev run or, in the fleet, a throwaway container that took its
        hex container ID as its hostname) is skipped entirely (gr452084
        defect 4). Such a boot only ever writes a single roster under an
        identity that never boots again: it can never form a two-boot
        comparison, so it is pure ballast for ``kind_provider`` (2108
        distinct hex hosts, ~107k rows for a six-machine fleet) and, when
        several untagged processes on one real host all fold into the
        same ``(host, "unknown")`` pair, it feeds the kind-shrinkage
        detector a roster stitched from unrelated processes. Every
        fleet process that serves kinds sets ``PRECIS_PROCESS`` via its
        plist, so nothing routable is lost by not recording an
        unknown-process roster.
        """
        if not specs or process == "unknown":
            return 0
        if is_container_id_host(host):
            # gr461595: throwaway container id, never boots again — pure ballast.
            log.debug("upsert_kind_providers: skipping ephemeral host %r", host)
            return 0
        sql = (
            "INSERT INTO kind_provider (slug, host, process, last_seen) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (slug, host, process) DO UPDATE SET "
            "last_seen = now()"
        )
        rows = [(spec.kind, host, process) for spec in specs]

        def _do(c: Connection) -> int:
            with c.cursor() as cur:
                cur.executemany(sql, rows)
            return len(rows)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            with c.transaction():
                return _do(c)

    def find_kind_providers(
        self,
        slug: str,
        *,
        max_age_seconds: int = 3600,
    ) -> list[str]:
        """Return distinct hosts currently advertising ``slug``.

        Sorted alphabetically for deterministic error messages.
        ``max_age_seconds`` filters out entries whose owning process
        hasn't checked in recently — a host whose worker crashed and
        never restarted shouldn't appear as a viable route.
        """
        sql = (
            "SELECT DISTINCT host FROM kind_provider "
            "WHERE slug = %s AND last_seen > now() - %s::interval "
            "ORDER BY host"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (slug, f"{max_age_seconds} seconds")).fetchall()
        return [str(r[0]) for r in rows]

    def prune_kind_providers(
        self, *, retention_days: int, conn: Connection | None = None
    ) -> int:
        """Drop ``kind_provider`` rows whose ``last_seen`` aged past
        ``retention_days``; return the number deleted (gr452084 defect 4).

        ``kind_provider`` is an UPSERT keyed on ``(slug, host, process)``
        — a live process bumps ``last_seen`` on every boot, so its rows
        never age out. Only rows for a ``(host, process)`` that has
        stopped booting entirely (a retired daemon, or a throwaway
        container that booted once under its hex hostname and died) fall
        past the window, and those are exactly the ballast: the table had
        grown to ~107k rows / 2108 distinct hosts for a six-machine
        fleet with nothing ever pruning it. The window mirrors
        ``worker_logs`` retention (30d, the fleet's evidence-of-a-boot
        floor) — past it there is no boot to route to and nothing for the
        kind-shrinkage detector to compare against.

        ``retention_days <= 0`` disables the prune. A caller may pass its
        own ``conn`` (the sweeper holds a fleet advisory lock on it so the
        DELETE single-flights); otherwise one is drawn from the pool.
        """
        if retention_days <= 0:
            return 0
        sql = (
            "DELETE FROM kind_provider "
            "WHERE last_seen < now() - (%s || ' days')::interval"
        )

        def _do(c: Connection) -> int:
            cur = c.execute(sql, (str(retention_days),))
            return cur.rowcount or 0

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            with c.transaction():
                return _do(c)

    def upsert_kinds(
        self,
        specs: list[Any],
        *,
        conn: Connection | None = None,
    ) -> int:
        """Idempotent upsert of every registered KindSpec into ``kinds``.

        Returns the number of rows touched (INSERTed or UPDATEed). On
        re-boot when nothing changed the row count stays the same as
        the spec count — the UPDATE branch fires every time but is a
        no-op write.

        Title and description are last-write-wins so a freshly-deployed
        spec with edited copy reaches the table without a migration.
        ``is_numeric`` is structural — if a handler ever changes it,
        the upsert will overwrite, and the next ``insert_ref`` enforces
        the new shape; we trust the spec.

        Boot is the only caller. Concurrent boots race-safe via
        ``ON CONFLICT``: two processes inserting the same slug from
        different hosts both succeed and both end up with the same
        row.
        """
        if not specs:
            return 0
        sql = (
            "INSERT INTO kinds (slug, is_numeric, title, description) "
            "VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (slug) DO UPDATE SET "
            "is_numeric = EXCLUDED.is_numeric, "
            "title = EXCLUDED.title, "
            "description = EXCLUDED.description"
        )
        rows = [
            (spec.kind, bool(spec.is_numeric), spec.title, spec.description)
            for spec in specs
        ]

        def _do(c: Connection) -> int:
            with c.cursor() as cur:
                cur.executemany(sql, rows)
            return len(rows)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            with c.transaction():
                return _do(c)


def boot_process_identity() -> tuple[str, str]:
    """``(host, process)`` for the boot-time ``kind_provider`` upsert.

    Host falls back to ``socket.gethostname()``; process to the
    ``PRECIS_PROCESS`` env var (set by every plist in the cluster) or
    ``"unknown"`` for local dev.
    """
    import os
    import socket

    host = (
        os.environ.get("PRECIS_HOST_NAME") or socket.gethostname() or "unknown"
    ).lower()
    process = os.environ.get("PRECIS_PROCESS") or "unknown"
    return host, process


__all__ = ["KindsMixin", "boot_process_identity"]
