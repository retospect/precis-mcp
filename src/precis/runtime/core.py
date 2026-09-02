"""``PrecisRuntime``: config + hub + dispatch logic, composed from mixins.

`PrecisRuntime` wraps the :class:`~precis.dispatch.Hub` (which owns
the registration table, store, embedder, and hint bus) with config
and dispatch logic. The MCP server (in `precis.server`) is a thin
FastMCP wrapper around it; tests dispatch directly without going
through MCP.

Lifecycle: the runtime owns the *close* of the store — callers do
``runtime.store.close()`` (or rely on a context manager wrapping the
runtime) to release the connection pool. The Hub merely *holds* the
store reference; whoever opened it is responsible for closing it.

The class body itself only carries the dataclass fields and the three
delegating properties (``hints`` / ``store`` / ``registry``) — every
behavioural method comes from one of the five mixins below, split out
by concern (each file's own module docstring explains its slice):

- :class:`precis.runtime.dispatch.DispatchMixin` — verb routing, kind/
  handler resolution, handler invocation.
- :class:`precis.runtime.search.SearchMixin` — cross-kind fan-out +
  source search.
- :class:`precis.runtime.angle.AngleMixin` — angle spray + dreamable
  region.
- :class:`precis.runtime.hints.HintsMixin` — tag-shaped-``q=`` tip +
  skill-help breadcrumb.
- :class:`precis.runtime.error.ErrorMixin` — error-envelope rendering.

They compose via ordinary multiple inheritance: every method call is
`self`-bound, so which file a given helper lives in is invisible to the
callers above — the split is purely a file-organisation concern.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.runtime.angle import AngleMixin
from precis.runtime.dispatch import DispatchMixin
from precis.runtime.error import ErrorMixin
from precis.runtime.hints import HintsMixin
from precis.runtime.search import SearchMixin

if TYPE_CHECKING:
    from precis._pagination import PaginationCache
    from precis.hints import HintBus
    from precis.store import Store


def _new_pagination_cache() -> PaginationCache:
    """Late import so the runtime module load doesn't pull in
    threading / uuid eagerly."""
    from precis._pagination import PaginationCache

    return PaginationCache()


def _shipped_migration_head() -> str | None:
    """Highest in-tree precis migration version on disk right now.

    Captured once per runtime construction (process boot for the MCP
    server / worker) so :meth:`DispatchMixin._schema_drift_note` can
    later tell "the DB migrated under this long-lived process" apart
    from a genuine schema bug — re-globbing at error time would read
    the *new* files a vcs-install deploy just dropped and mask exactly
    the stale-process case the probe exists for. ``None`` when the
    directory can't be read (frozen installs, tests without the tree).
    """
    try:
        from precis.store.schema_dump import builtin_migrations_dir

        return max(
            (p.stem for p in builtin_migrations_dir().glob("*.sql")),
            default=None,
        )
    except Exception:
        return None


@dataclass
class PrecisRuntime(DispatchMixin, SearchMixin, AngleMixin, HintsMixin, ErrorMixin):
    """Server-wide singleton: config + hub + dispatch logic.

    The :class:`~precis.dispatch.Hub` carries the dispatch table, the
    store (or ``None`` for stateless deployments), the embedder, and
    the hint bus. Tests and external callers reach those through the
    runtime's delegating properties (``runtime.hints``,
    ``runtime.store``) so the rename of internal field names didn't
    cascade through every test fixture.
    """

    config: PrecisConfig
    hub: Hub

    #: Parsed ``PRECIS_DEFAULT_TAGS`` tuple, resolved once at runtime
    #: build. Empty tuple when the env var is unset; the dispatch
    #: hook short-circuits in that case so unconfigured deployments
    #: pay zero per-call cost. Populated by :func:`build_runtime`;
    #: tests that construct a ``PrecisRuntime`` directly use the
    #: empty default unless they need to exercise the merge path.
    default_tags_resolved: tuple[str, ...] = field(default_factory=tuple)

    #: Process-local cache for chunked responses. Built fresh per
    #: runtime so test fixtures get a clean cache; production has
    #: exactly one runtime per worker so cursors survive across
    #: tool calls within the worker's lifetime.
    pagination: PaginationCache = field(default_factory=lambda: _new_pagination_cache())

    #: Whether this runtime's process is expected to still be around
    #: when the agent tries to redeem a pagination cursor. ``False``
    #: (the default) is the safe assumption for a one-shot invocation
    #: like `precis eval` — the process, and with it ``pagination``
    #: above, is gone the moment the call returns, so a
    #: ``more(cursor=...)`` footer would promise a capability that
    #: doesn't exist (gr267466). Set ``True`` by the two entry points
    #: that actually stick around long enough for a retry to land: the
    #: MCP `precis serve` boot path (`precis.server._init_runtime`) and
    #: `precis repl` (`precis.cli.repl.run`) — both build one runtime
    #: and keep dispatching through it for the life of the process.
    #: Read by :meth:`~precis.runtime.dispatch.DispatchMixin.dispatch_with_status`
    #: to pick which pagination footer to render.
    long_lived: bool = False

    #: In-tree migration head as of runtime construction (≈ process
    #: boot). The schema-drift probe compares this against the live
    #: ``_migrations`` ledger when a verb dies on
    #: UndefinedColumn/UndefinedTable, turning the opaque
    #: "[error:Internal] … UndefinedColumn" outage (gr281493 family)
    #: into "the DB migrated under this process — restart the server".
    boot_migration_head: str | None = field(default_factory=_shipped_migration_head)

    # ----- delegating properties ---------------------------------------

    @property
    def hints(self) -> HintBus:
        """Per-request hint collector. Delegates to ``self.hub.hints``."""
        return self.hub.hints

    @property
    def store(self) -> Store | None:
        """Connected store, or ``None`` for stateless deployments."""
        return self.hub.store

    @property
    def registry(self) -> Hub:
        """Backwards-compat alias for ``self.hub``.

        Kept so test fixtures that still spell ``runtime.registry``
        continue to work; new code should use ``runtime.hub`` (or
        the typed delegators on this class).
        """
        return self.hub
