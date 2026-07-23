"""Runtime construction: ``build_runtime`` + the store-connect retry loop."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.config import PrecisConfig
from precis.runtime.core import PrecisRuntime

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


def _connect_store_or_raise(dsn: str, retry_seconds: float) -> Any:
    """Connect the store, retrying a transient DB outage for a bounded window.

    ``Store.connect`` itself fails fast (the pool ``open_timeout`` bounds each
    attempt to ≈10s), so a DB that is genuinely down raises promptly. But a
    node reboot leaves the DB briefly unreachable while the MCP subprocess is
    already coming up; a single attempt would crash and the parent would
    respawn into the same window. Retrying for ``retry_seconds`` rides that out
    without a tight crash loop. If the window elapses we **raise** — a crash
    the parent can respawn — rather than returning ``None`` and letting the
    server come up storeless (the failure mode this exists to prevent). See
    ``PrecisConfig.db_connect_retry_seconds``.
    """
    import time

    from precis.store import Store

    deadline = time.monotonic() + max(0.0, retry_seconds)
    attempt = 0
    while True:
        attempt += 1
        try:
            store = Store.connect(dsn)
            if attempt > 1:
                log.warning("store connected on attempt %d", attempt)
            return store
        except Exception as exc:
            if time.monotonic() >= deadline:
                log.error(
                    "store connect failed after %d attempt(s) / %.0fs budget "
                    "(%s: %s) — crashing so the supervisor respawns rather "
                    "than serving a storeless surface",
                    attempt,
                    retry_seconds,
                    type(exc).__name__,
                    exc,
                )
                raise
            log.warning(
                "store connect attempt %d failed (%s: %s); retrying within "
                "%.0fs budget",
                attempt,
                type(exc).__name__,
                exc,
                retry_seconds,
            )
            time.sleep(2.0)


def build_runtime(
    config: PrecisConfig | None = None,
) -> PrecisRuntime:
    """Construct a runtime, connecting the store if `config.database_url` is set.

    Stateless setups (no DB) work fine — pass a config without a
    database_url, or rely on the default. Ref-backed handlers are
    skipped when there's no store.

    The active embedder is selected by `config.embedder`:
        ``"mock"``  → deterministic in-process (default; CI-safe)
        ``"bge-m3"`` → real `BAAI/bge-m3` via sentence-transformers

    Caller owns the returned runtime; if it has a store, call
    `runtime.store.close()` before exit.

    Composition root goes through :func:`precis.dispatch.boot`,
    which constructs every handler, wraps each in
    :func:`precis.dispatch._try` (swallows ``InitError`` + missing
    optional deps), and populates the flat dispatch table. The
    returned :class:`Hub` carries the store / embedder / hints; the
    runtime is a thin wrapper around it. See
    ``docs/user-facing/seven-verb-surface-migration.md`` D7/D8.
    """
    from precis.config import load_config
    from precis.dispatch import boot
    from precis.embedder import Embedder, make_embedder

    if config is None:
        config = load_config()

    # Guard against the storeless-after-scrub trap: a prior ``build_runtime``
    # (or worker) called ``adopt_process_store``, which pops
    # ``PRECIS_DATABASE_URL`` from the environment. If this process later
    # builds another runtime without an explicit config, recover the DSN from
    # the secrets module rather than coming up storeless. See OPEN-ITEMS
    # residual "build_runtime is storeless-after-scrub by construction".
    if not config.database_url:
        from precis import secrets as _secrets

        adopted = _secrets.get_adopted_dsn()
        if adopted:
            config = config.model_copy(update={"database_url": adopted})

    store: Store | None = None
    embedder: Embedder | None = None
    if config.database_url:
        store = _connect_store_or_raise(
            config.database_url, config.db_connect_retry_seconds
        )
        # Bind the store for the secrets resolver + scrub the DSN from the
        # environment (parameter, not env) so subprocess spawns don't inherit
        # it — see docs/design/secrets-vault.md.
        from precis import secrets as _secrets

        _secrets.adopt_process_store(store)
        # Bind the same store for the full LLM interaction log (route_log,
        # migration 0061). Best-effort; dark until bound.
        from precis import route_log as _route_log

        _route_log.bind_store(store)
        # Bind the same store for the budget circuit breaker's rolling meter.
        # Best-effort; dark (breaker never trips) until bound.
        from precis.budget import bind_store as _bind_budget_store

        _bind_budget_store(store)
        embedder = make_embedder(
            config.embedder,
            dim=store.embedding_dim(),
            url=config.embedder_url,
            timeout=config.embedder_timeout,
            max_retries=config.embedder_max_retries,
        )

    from precis import default_tags as _dt
    from precis.kind_gate import parse_disabled, parse_disabled_reasons

    hub = boot(
        store=store,
        embedder=embedder,
        precis_root=config.root,
        python_roots=config.python_roots,
        kinds_disabled=parse_disabled(config.kinds_disabled),
        kinds_disabled_reasons=parse_disabled_reasons(config.kinds_disabled),
    )
    return PrecisRuntime(
        config=config,
        hub=hub,
        default_tags_resolved=_dt.parse(config.default_tags),
    )
