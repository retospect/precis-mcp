"""The stateful core of the store: pool / dsn / hint-bus / transaction
lifecycle, and nothing else.

:class:`StoreCore` is the only stateful part of the store — every
domain sub-store (e.g. :class:`~precis.store._draft_ops.DraftStore`)
holds a reference to one shared :class:`StoreCore` instead of owning
its own connection pool. The :class:`~precis.store.store.Store` facade
owns the one true :class:`StoreCore` and aliases its ``pool`` /
``dsn`` / ``hint_bus`` attributes onto itself so existing call sites
(``store.pool``, ``store.hint_bus = ...``) keep working unchanged. See
``docs/backlog/codereview-store-decomposition.md`` for the carve plan.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.hints import Hint, HintBus


class StoreCore:
    """Owns the psycopg connection pool and the per-request hint bus.

    Every domain sub-store is constructed with a reference to one
    :class:`StoreCore` rather than a pool directly, so they all share
    the same lifecycle (one pool, one hint bus) without each having to
    re-implement ``tx``/``emit_hint``.
    """

    def __init__(self, pool: ConnectionPool, *, dsn: str | None = None) -> None:
        self.pool = pool
        # Original DSN string — used by callers that need to open a
        # dedicated (non-pooled) connection, e.g. for session-scoped
        # advisory locks in ``precis.ingest.claim`` where pool-based
        # connections aren't usable. ``None`` when the Store was
        # constructed without going through :meth:`connect` (tests
        # using a pre-built pool); claim acquisition falls back to a
        # no-op in that case.
        self.dsn = dsn
        # Optional back-reference to the per-request hint bus, wired by
        # ``Hub.__post_init__``. Lets low-level store ops (e.g. the
        # merged-handle redirect in ``resolve_handle``) emit a non-breaking
        # agent hint from deep in the call tree without every caller
        # threading a ``hub``. ``None`` on a store built outside a Hub (most
        # worker paths) — :meth:`emit_hint` is then a no-op, as it is outside
        # any request scope.
        self.hint_bus: HintBus | None = None

    def emit_hint(self, hint: Hint) -> None:
        """Emit a non-breaking agent hint if a bus is wired and we're inside a
        request scope; a no-op otherwise (worker paths, no bus)."""
        if self.hint_bus is not None:
            self.hint_bus.emit(hint)

    @contextmanager
    def tx(self) -> Iterator[Connection]:
        """Acquire a connection inside an explicit transaction.

        Auto-commits on clean exit; rolls back on exception. Used
        by handler ``put`` paths that bundle multiple writes so a
        downstream constraint violation rolls back the whole unit
        rather than leaving half-written state.
        """
        with self.pool.connection() as conn:
            with conn.transaction():
                yield conn

    def close(self) -> None:
        """Close the underlying connection pool."""
        self.pool.close()
