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
    *,
    interactive: bool = True,
) -> PrecisRuntime:
    """Construct a runtime, connecting the store if `config.database_url` is set.

    Stateless setups (no DB) work fine — pass a config without a
    database_url, or rely on the default. Ref-backed handlers are
    skipped when there's no store.

    The active embedder is selected by `config.embedder`:
        ``"mock"``  → deterministic in-process (default; CI-safe)
        ``"bge-m3"`` → real `BAAI/bge-m3` via sentence-transformers

    This is primarily the REQUEST-PATH composition root, so by default the
    embedder it wires is built with the interactive budget
    (``embedder_interactive_timeout`` / ``_max_retries``) and wrapped in
    :class:`~precis.embedder.BoundedConcurrencyEmbedder`. Callers that
    introspect ``hub.embedder``'s concrete type must unwrap via ``.inner``;
    the wrapper forwards the whole ``Embedder`` Protocol, so structural
    checks (``isinstance(e, Embedder)``) still hold.

    Pass ``interactive=False`` for an unattended BULK pass that happens to
    want a runtime — ``cli/taproot.py``'s ``backfill`` / ``direct-mint`` are
    the ones in tree. Those loop over many chunks through
    ``taproot/canon.py::block``, which embeds *unguarded*: on the
    interactive budget a single transient embedder blip would abort the run
    after ~31s and discard everything computed so far, where the patient
    budget rides it out. They get ``embedder_timeout`` / ``_max_retries``
    and no bulkhead.

    ``precis worker`` never comes through here at all — it builds its own
    via ``cli/worker.py::_resolve_embedder`` (gripe 244419).

    Caller owns the returned runtime; if it has a store, call
    `runtime.store.close()` before exit.

    Composition root goes through :func:`precis.dispatch.boot`,
    which constructs every handler, wraps each in
    :func:`precis.dispatch._try` (swallows ``InitError`` + missing
    optional deps), and populates the flat dispatch table. The
    returned :class:`Hub` carries the store / embedder / hints; the
    runtime is a thin wrapper around it.
    """
    from precis.config import load_config
    from precis.dispatch import boot
    from precis.embedder import (
        BoundedConcurrencyEmbedder,
        Embedder,
        make_embedder,
    )

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
        # it — see precis/secrets.py::adopt_process_store.
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
        # Bind the same store for DB-resident settings (precis.settings) so
        # registered keys resolve through the DB tier, mirroring secrets.
        from precis import settings as _settings

        _settings.bind_store(store)
        # Request-path budget, NOT the batch one (gripe 244419) — unless the
        # caller declared itself a batch (``interactive=False``). A request
        # path has a human or an agent waiting on the far end and reaches
        # the embedder only for short query/card embeds; a bulk CLI pass has
        # neither property and wants the patient budget instead.
        # ``precis worker`` never comes through here at all — it builds its
        # own via ``cli/worker.py::_resolve_embedder``.
        embedder = make_embedder(
            config.embedder,
            dim=store.embedding_dim(),
            url=config.embedder_url,
            timeout=(
                config.embedder_interactive_timeout
                if interactive
                else config.embedder_timeout
            ),
            max_retries=(
                config.embedder_interactive_max_retries
                if interactive
                else config.embedder_max_retries
            ),
        )
        # ...and the bulkhead: bound how many of this process's threads can
        # sit inside an embed at once, so a struggling embedder degrades
        # semantic search instead of starving every OTHER verb of a thread.
        # The timeout above bounds each wait; this bounds their number.
        #
        # Batch callers skip it. Shedding assumes the caller has a fallback
        # (lexical-only) or a retry; a bulk pass like
        # ``taproot/canon.py::block`` embeds unguarded and would abort the
        # whole run, and it has no thread pool to protect in the first place.
        if interactive:
            embedder = BoundedConcurrencyEmbedder(
                embedder, config.embedder_interactive_max_concurrency
            )

    from precis import default_tags as _dt
    from precis.kind_gate import parse_disabled, parse_disabled_reasons

    hub = boot(
        store=store,
        embedder=embedder,
        precis_root=config.root,
        python_roots=config.python_roots,
        md_roots=config.md_roots,
        kinds_disabled=parse_disabled(config.kinds_disabled),
        kinds_disabled_reasons=parse_disabled_reasons(config.kinds_disabled),
    )
    return PrecisRuntime(
        config=config,
        hub=hub,
        default_tags_resolved=_dt.parse(config.default_tags),
    )
