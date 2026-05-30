"""Postgres advisory-lock based work claims for multi-host ingest.

Replaces the v2's filesystem-based ``.processing/*.lock`` mechanism
(see ADR 0014) with a database-backed claim that's correct across
hosts. Used by ``precis_add`` to ensure that at most one host runs
the expensive Marker pipeline on any given PDF content (keyed by
``pdf_sha256``), even when multiple hosts share an SMB-mounted
``/inbox``.

Why advisory locks specifically:

* **Auto-release on disconnect.** ``pg_try_advisory_lock`` is bound
  to the session that acquired it. When the session goes away
  (process exit, container OOM, mac crashes, network partition, ...),
  Postgres releases the lock immediately. No heartbeat, no TTL
  reaper, no stale-row sweeper required.

* **No schema change.** Advisory locks live in shared memory inside
  Postgres; no new table to migrate, no constraints to design.

* **Cheap.** Acquiring + releasing is a single round-trip each;
  contention is fast-fail via ``pg_try_advisory_lock`` (the
  non-blocking variant — we don't want hosts queueing up on a
  contended hash).

The lock key is the first 64 bits of the ``pdf_sha256`` interpreted
as a signed bigint. Collision probability across a 5,900-PDF corpus
is ~10^-15, well below any other failure mode we care about.

Critical implementation note: the claim uses a **dedicated** psycopg
connection, NOT a pooled one. Session-scoped locks travel with the
connection; if we used a pooled connection and returned it to the
pool, the lock would persist and grant subsequent unrelated callers
the claim by accident. The dedicated connection's lifetime brackets
the claim exactly.
"""

from __future__ import annotations

import logging
from typing import Any

import psycopg

log = logging.getLogger(__name__)


def _key_for(pdf_sha256: str) -> int:
    """Lock key from the leading 64 bits of the hash.

    ``pg_try_advisory_lock(bigint)`` takes a signed 64-bit integer;
    we mask to that range. Collisions across 5K-10K PDFs are
    cryptographically negligible (~2^-50).
    """
    raw = int(pdf_sha256[:16], 16)
    # Map [0, 2^64) -> [-2^63, 2^63) so Postgres' signed bigint
    # accepts it without overflow.
    if raw >= 2**63:
        raw -= 2**64
    return raw


class Claim:
    """Context manager wrapping a session-scoped advisory lock on a
    ``pdf_sha256``.

    Usage::

        with Claim(dsn, pdf_sha256) as claim:
            if not claim.acquired:
                return  # another host owns this work
            # ... run Marker, write_paper, etc.

    On exit (normal or exception), the dedicated connection is
    closed, releasing the lock. If the process dies hard, Postgres
    sees the socket close and releases the lock on its own.

    The ``Claim`` is **not** thread-safe — each ingest should
    instantiate its own.
    """

    def __init__(self, dsn: str, pdf_sha256: str) -> None:
        self._dsn = dsn
        self._pdf_sha256 = pdf_sha256
        self._key = _key_for(pdf_sha256)
        self._conn: Any | None = None
        self.acquired: bool = False

    def __enter__(self) -> Claim:
        # autocommit avoids the lock sitting inside a never-committed
        # tx (which would block VACUUM / hold row locks unnecessarily).
        self._conn = psycopg.connect(self._dsn, autocommit=True)
        try:
            row = self._conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (self._key,)
            ).fetchone()
        except Exception:
            self._conn.close()
            self._conn = None
            raise

        self.acquired = bool(row and row[0])
        if not self.acquired:
            # Close immediately on a miss — there's nothing to hold.
            self._conn.close()
            self._conn = None
            log.info(
                "claim: %s already held by another host; skipping",
                self._pdf_sha256[:12],
            )
        else:
            log.debug("claim: acquired %s", self._pdf_sha256[:12])

        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._conn is None:
            return
        try:
            self._conn.execute("SELECT pg_advisory_unlock(%s)", (self._key,))
        except Exception as exc:
            # Best effort — connection close releases the lock anyway.
            log.warning("claim: unlock failed for %s: %s", self._pdf_sha256[:12], exc)
        finally:
            self._conn.close()
            self._conn = None
            log.debug("claim: released %s", self._pdf_sha256[:12])


__all__ = ["Claim"]
