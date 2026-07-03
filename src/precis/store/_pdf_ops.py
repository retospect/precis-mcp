"""Held-PDF storage path + per-host presence ledger. Mixin on
:class:`precis.store.Store`.

Two concerns, both about *where a held paper's PDF actually is*:

* **``pdfs.storage_path``** (Step 1) — the authoritative path the ingest
  host recorded. :meth:`pdf_storage_path` / :meth:`set_pdf_storage_path`
  read and correct it; the web PDF resolver prefers it over the re-derived
  cite_key convention (``precis_web.corpus.resolve_pdf_for_ref``), so a PDF
  filed off-convention (a rename, a bib-key alias) still resolves.

* **``pdf_locations``** (Step 2, migration 0052) — a per-host presence
  ledger: one row per ``(pdf_sha256, host)`` recording whether *that node*
  found the file, and when. The ``corpus_reconcile`` worker maintains it;
  the draft reader's "held but missing" ▲ becomes a corpus-wide DB read
  (:meth:`pdf_missing`) with zero request-time filesystem stats, so adding
  or removing a corpus mount on the *web* process no longer changes the
  marker — only whether some node actually holds the bytes does.

Presence semantics. A ledger row is a *verdict*: ``path`` is where the host
found the PDF, or ``''`` when it looked and the file was absent. So:

* held-anywhere ⇔ some host has a **fresh, non-empty** row (``seen_at``
  within the TTL);
* missing ⇔ the sha has been checked (**some** row exists) **and** no host
  has a fresh non-empty row — a never-checked sha is *unknown*, not
  missing, so the marker never false-fires before the pass has swept.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from psycopg import Connection
    from psycopg_pool import ConnectionPool


def _location_ttl_days() -> int:
    """Days a ``pdf_locations`` verdict is trusted before it ages out.

    ``PRECIS_PDF_LOCATION_TTL_DAYS`` (default 7). Longer than the reconcile
    refresh cadence, so a live node keeps its rows fresh well inside the
    window; a node that goes offline lets its rows lapse, and its PDFs stop
    counting as held (unless another node also holds them).
    """
    raw = os.environ.get("PRECIS_PDF_LOCATION_TTL_DAYS")
    if not raw:
        return 7
    try:
        return max(1, int(raw))
    except ValueError:
        return 7


@dataclass(frozen=True, slots=True)
class DuePdf:
    """A held PDF this host should (re)check: its sha, the ingest-recorded
    ``storage_path`` (``""`` when unknown), and every cite_key alias + slug
    the file might be filed under."""

    pdf_sha256: str
    storage_path: str
    cite_keys: tuple[str, ...]


class PdfMixin:
    """``pdfs.storage_path`` accessors + the ``pdf_locations`` ledger."""

    pool: ConnectionPool

    # --- Step 1: authoritative storage_path -------------------------------

    def pdf_storage_path(
        self, pdf_sha256: str, *, conn: Connection | None = None
    ) -> str | None:
        """The path recorded for this PDF at ingest, or ``None``.

        ``pdfs.storage_path`` is ``NOT NULL`` but the ingest writer stores
        ``paper.pdf_storage_path or ""`` — a blank string for rows written
        before it was populated. A blank value is "unknown", so we return
        ``None`` (the resolver then falls back to the cite_key convention).
        """
        sql = "SELECT storage_path FROM pdfs WHERE pdf_sha256 = %s"

        def _do(c: Connection) -> str | None:
            row = c.execute(sql, (pdf_sha256,)).fetchone()
            val = (row[0] if row else None) or ""
            return val or None

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def set_pdf_storage_path(
        self, pdf_sha256: str, path: str, *, conn: Connection | None = None
    ) -> bool:
        """Record ``path`` as this PDF's authoritative on-disk location.

        Called when a move makes the ingest-time path stale — the web
        ``/rename`` route relocates the file but the pure-convention resolver
        would otherwise keep guessing the old shard. Blank ``pdf_sha256`` /
        ``path`` is a no-op. Returns whether a row was updated.
        """
        if not pdf_sha256 or not path:
            return False
        sql = "UPDATE pdfs SET storage_path = %s WHERE pdf_sha256 = %s"

        def _do(c: Connection) -> bool:
            return (c.execute(sql, (path, pdf_sha256)).rowcount or 0) > 0

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            with c.transaction():
                return _do(c)

    # --- Step 2: the per-host presence ledger -----------------------------

    def record_pdf_location(
        self,
        pdf_sha256: str,
        host: str,
        path: str,
        *,
        conn: Connection | None = None,
    ) -> None:
        """UPSERT this host's verdict for ``pdf_sha256``, stamping
        ``seen_at = now()``.

        ``path`` is where the host found the file, or ``""`` to record a
        *checked-and-absent* verdict (which keeps the sha out of the "due"
        set until the next refresh, so a genuinely-missing PDF doesn't
        busy-loop the reconcile pass).
        """
        sql = (
            "INSERT INTO pdf_locations (pdf_sha256, host, path, seen_at) "
            "VALUES (%s, %s, %s, now()) "
            "ON CONFLICT (pdf_sha256, host) DO UPDATE SET "
            "path = EXCLUDED.path, seen_at = now()"
        )

        def _do(c: Connection) -> None:
            c.execute(sql, (pdf_sha256, host, path))

        if conn is not None:
            _do(conn)
            return
        with self.pool.connection() as c:
            with c.transaction():
                _do(c)

    def pdf_held_anywhere(
        self, pdf_sha256: str, *, ttl_days: int | None = None
    ) -> bool:
        """True when some host has a **fresh, non-empty** verdict for this
        PDF — i.e. a node actually holds the bytes, TTL-current."""
        ttl = _location_ttl_days() if ttl_days is None else ttl_days
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT EXISTS (SELECT 1 FROM pdf_locations "
                "WHERE pdf_sha256 = %s AND path <> '' "
                "  AND seen_at > now() - %s::interval)",
                (pdf_sha256, f"{ttl} days"),
            ).fetchone()
        return bool(row and row[0])

    def pdf_missing(self, pdf_sha256: str, *, ttl_days: int | None = None) -> bool:
        """True when this held PDF is genuinely missing: it has been checked
        (some ledger row exists) yet no host holds a fresh copy.

        A never-checked sha (no rows at all) is *unknown*, not missing — the
        reader must not flag it before the reconcile pass has swept — so this
        returns ``False`` there.
        """
        ttl = _location_ttl_days() if ttl_days is None else ttl_days
        with self.pool.connection() as c:
            row = c.execute(
                "SELECT "
                "  EXISTS (SELECT 1 FROM pdf_locations WHERE pdf_sha256 = %s) "
                "  AND NOT EXISTS (SELECT 1 FROM pdf_locations "
                "    WHERE pdf_sha256 = %s AND path <> '' "
                "      AND seen_at > now() - %s::interval)",
                (pdf_sha256, pdf_sha256, f"{ttl} days"),
            ).fetchone()
        return bool(row and row[0])

    def pdfs_due_for_host(
        self,
        host: str,
        *,
        refresh_hours: float,
        limit: int,
    ) -> list[DuePdf]:
        """Held PDFs this ``host`` should (re)check now: those with no verdict
        for the host, or a verdict older than ``refresh_hours``, stalest
        first. Aggregates every cite_key alias across the paper refs that
        point at each sha, so the pass can probe the convention when the
        recorded ``storage_path`` doesn't resolve.
        """
        with self.pool.connection() as c:
            rows = c.execute(
                """
                SELECT p.pdf_sha256,
                       p.storage_path,
                       array_remove(array_agg(DISTINCT ri.id_value), NULL)
                         AS cite_keys
                  FROM pdfs p
                  JOIN refs r
                    ON r.pdf_sha256 = p.pdf_sha256
                   AND r.kind = 'paper'
                   AND r.deleted_at IS NULL
                  LEFT JOIN ref_identifiers ri
                    ON ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key'
                  LEFT JOIN pdf_locations loc
                    ON loc.pdf_sha256 = p.pdf_sha256 AND loc.host = %s
                 WHERE loc.pdf_sha256 IS NULL
                    OR loc.seen_at < now() - %s::interval
                 GROUP BY p.pdf_sha256, p.storage_path, loc.seen_at
                 ORDER BY loc.seen_at ASC NULLS FIRST
                 LIMIT %s
                """,
                (host, f"{refresh_hours} hours", limit),
            ).fetchall()
        # A paper's display slug is itself a ``cite_key`` identifier, so the
        # aggregated ``ri.id_value`` set already covers every alias
        # ``ref_pdf_keys`` would probe — the pass tries them all.
        return [
            DuePdf(
                pdf_sha256=str(r[0]),
                storage_path=str(r[1] or ""),
                cite_keys=tuple(k for k in (r[2] or ()) if k),
            )
            for r in rows
        ]


__all__ = ["DuePdf", "PdfMixin"]
