"""Postgres advisory-lock based work claims for multi-host ingest.

Replaces the v2's filesystem-based ``.processing/*.lock`` mechanism
with a database-backed claim that's correct across hosts — file-lock
semantics over a shared SMB inbox are unreliable, and stale-lock
recovery would need heartbeats the DB gives for free. Used by
``precis_add`` to ensure that at most one host runs the
expensive Marker pipeline on any given PDF content (keyed by
``pdf_sha256``), even when multiple hosts share an SMB-mounted
``/inbox``.

Why advisory locks specifically:

* **Auto-release.** The lock is released when its transaction ends
  (``pg_try_advisory_xact_lock``). If the process dies mid-claim
  (container OOM, mac crashes, network partition, ...), Postgres — or
  pgbouncer, which closes a server connection whose client vanished
  mid-transaction — ends the transaction and the lock goes with it. No
  heartbeat, no TTL reaper, no stale-row sweeper required.

* **No schema change.** Advisory locks live in shared memory inside
  Postgres; no new table to migrate, no constraints to design.

* **Cheap.** Acquiring + releasing is a single round-trip each;
  contention is fast-fail via the ``try`` variant (the non-blocking
  one — we don't want hosts queueing up on a contended hash).

The lock key is the first 64 bits of the ``pdf_sha256`` interpreted
as a signed bigint. Collision probability across a 5,900-PDF corpus
is ~10^-15, well below any other failure mode we care about.

Critical implementation note (gr463966): the claim is **transaction**-
scoped, held by :func:`precis.store.advisory.try_xact_advisory_lock` on a
**dedicated** psycopg connection, NOT a pooled one. The earlier
session-scoped ``pg_try_advisory_lock`` is broken behind prod's pgbouncer
``pool_mode = transaction``: the lock and its unlock land on different
server backends, so there is no mutual exclusion and the lock leaks. An
open transaction pins one server connection for the claim's whole
lifetime, and the lock ends with it; that module's docstring has the full
mechanism (including why ``idle_in_transaction_session_timeout`` cannot
kill a long Marker run).
"""

from __future__ import annotations

import logging
from contextlib import AbstractContextManager
from typing import Any

from precis.store.advisory import try_xact_advisory_lock

log = logging.getLogger(__name__)


def _key_for(pdf_sha256: str) -> int:
    """Lock key from the leading 64 bits of the hash.

    ``pg_try_advisory_xact_lock(bigint)`` takes a signed 64-bit integer;
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
    """Context manager wrapping a transaction-scoped advisory lock on a
    ``pdf_sha256``.

    Usage::

        with Claim(dsn, pdf_sha256) as claim:
            if not claim.acquired:
                return  # another host owns this work
            # ... run Marker, write_paper, etc.

    On exit (normal or exception), the lock's transaction is rolled back
    and its dedicated connection closed, releasing the lock. If the
    process dies hard, the transaction ends with the connection and the
    lock goes with it.

    The ``Claim`` is **not** thread-safe — each ingest should
    instantiate its own.
    """

    def __init__(self, dsn: str, pdf_sha256: str) -> None:
        self._dsn = dsn
        self._pdf_sha256 = pdf_sha256
        self._key = _key_for(pdf_sha256)
        self._cm: AbstractContextManager[bool] | None = None
        self.acquired: bool = False

    def __enter__(self) -> Claim:
        cm = try_xact_advisory_lock(self._dsn, self._key)
        self.acquired = cm.__enter__()
        if not self.acquired:
            # Close immediately on a miss — there's nothing to hold.
            cm.__exit__(None, None, None)
            log.info(
                "claim: %s already held by another host; skipping",
                self._pdf_sha256[:12],
            )
        else:
            self._cm = cm
            log.debug("claim: acquired %s", self._pdf_sha256[:12])

        return self

    def __exit__(self, *_exc: Any) -> None:
        if self._cm is None:
            return
        cm, self._cm = self._cm, None
        try:
            cm.__exit__(None, None, None)
        finally:
            log.debug("claim: released %s", self._pdf_sha256[:12])


__all__ = ["Claim"]
