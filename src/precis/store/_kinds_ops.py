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
the per-process subset (env-gated kinds drop out via
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
        """
        if not specs:
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
