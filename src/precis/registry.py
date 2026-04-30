"""In-tree handler registry.

V2 drops setuptools entry-point plugin discovery. New kinds = append a
class to `BUILTINS()` here and add a row to the `kinds` reference table
in a migration. Two-step, explicit, greppable.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING

from precis.errors import NotFound

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.protocol import Handler
    from precis.store import Store

log = logging.getLogger(__name__)


def _try_register(
    handlers: list[Handler],
    factory: Callable[[], Handler | None],
    *,
    label: str,
) -> None:
    """Run ``factory`` and append the returned handler to ``handlers``.

    Centralises the ``try: import; except (ImportError, ValueError):
    pass`` pattern that used to be repeated five times for the
    optional-extra handlers (math, youtube, web, perplexity, patent)
    plus the markdown / python file handlers. Drops ``label`` into
    a debug log on import failure so a misconfigured environment
    surfaces in the server log without breaking startup.

    ``factory`` returning ``None`` is treated as a soft-skip — the
    same outcome as catching the exception, but the factory itself
    can decide to bail (e.g. when an env-driven config field is
    missing) without raising.
    """
    try:
        handler = factory()
    except (ImportError, ValueError) as exc:
        log.debug("optional kind %r unavailable: %s", label, exc)
        return
    if handler is not None:
        handlers.append(handler)


def builtins(
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    markdown_root: str | None = None,
    python_roots: str | None = None,
) -> list[Handler]:
    """Return handler instances for the active server configuration.

    Stateless handlers (e.g. `calc`) are always included. Ref-backed
    handlers (e.g. `memory`, `paper`) require a `store` and are skipped
    when none is provided — this lets phase-1-style stateless setups
    keep working without a database.

    The `embedder` (if provided) is given to handlers that semantic-
    search; otherwise we fall back to a deterministic ``MockEmbedder``
    sized to the store's `system.embedding_dim`. Production setups
    construct the real embedder in `build_runtime` from `config.embedder`.

    Lazy imports keep heavy deps (sympy, psycopg, pgvector,
    sentence-transformers) off the module-load critical path until
    they're actually needed.
    """
    handlers: list[Handler] = []

    # Calc — local sympy-backed calculator. Hidden when sympy is not
    # installed (it lives in the [calc] / [all] optional extras), so a
    # bare `pip install precis-mcp` doesn't crash the registry on
    # module import.
    def _build_calc() -> Handler:
        from precis.handlers.calc import CalcHandler

        return CalcHandler()

    _try_register(handlers, _build_calc, label="calc")

    # Python kind — DB-free, in-memory mtime-cached AST index. Hidden
    # when no roots are configured, or when ``PRECIS_PYTHON_ROOTS`` is
    # set but every entry is malformed (``parse_python_roots`` logs
    # each rejection). Independent of `store` — the python kind never
    # touches Postgres.
    if python_roots:

        def _build_python() -> Handler | None:
            from precis.handlers.python import PythonHandler, parse_python_roots

            roots = parse_python_roots(python_roots)
            if not roots:
                return None  # all entries malformed → soft-skip
            return PythonHandler(roots=roots)

        _try_register(handlers, _build_python, label="python")

    if store is not None:
        from precis.embedder import MockEmbedder
        from precis.handlers.conversation import ConversationHandler
        from precis.handlers.flashcard import FlashcardHandler
        from precis.handlers.gripe import GripeHandler
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.oracle import OracleHandler
        from precis.handlers.paper import PaperHandler
        from precis.handlers.quest import QuestHandler
        from precis.handlers.skill import SkillHandler
        from precis.handlers.todo import TodoHandler

        eff_embedder: Embedder = embedder or MockEmbedder(dim=store.embedding_dim())

        # State kinds — numeric and slug-addressed refs. Cheap to
        # instantiate (no network, no model load); always available.
        handlers.append(MemoryHandler(store=store))
        handlers.append(TodoHandler(store=store))
        handlers.append(GripeHandler(store=store))
        handlers.append(FlashcardHandler(store=store))
        handlers.append(QuestHandler(store=store))
        handlers.append(ConversationHandler(store=store))
        handlers.append(OracleHandler(store=store))
        handlers.append(SkillHandler(store=store))
        handlers.append(PaperHandler(store=store, embedder=eff_embedder))

        # Cache-backed kinds. Each declares its env requirements via
        # ``KindSpec.requires_env``; the dispatcher hides them from
        # the agent enum when the env vars aren't set, and the actual
        # network call only fires inside ``_fetch`` (lazy, on cache
        # miss). Optional deps (wolframalpha, httpx, etc.) are
        # guarded by the ``[external]`` extra; if missing, importing
        # the handler raises ``ImportError`` and the kind is skipped
        # silently via :func:`_try_register`.
        def _build_math() -> Handler:
            from precis.handlers.math import MathHandler

            return MathHandler(store=store)

        _try_register(handlers, _build_math, label="math")

        def _build_youtube() -> Handler:
            from precis.handlers.youtube import YouTubeHandler

            return YouTubeHandler(store=store)

        _try_register(handlers, _build_youtube, label="youtube")

        def _build_web() -> Handler:
            from precis.handlers.web import WebHandler

            return WebHandler(store=store)

        _try_register(handlers, _build_web, label="web")

        # File handler — markdown. Hidden when no root is configured;
        # also hidden when the root path doesn't exist (treat as
        # mis-configuration; better to skip than to crash on every
        # call). The handler's __init__ raises ``ValueError`` for a
        # missing/non-directory root, which ``_try_register`` catches.
        if markdown_root:

            def _build_markdown() -> Handler:
                from pathlib import Path

                from precis.handlers.markdown import MarkdownHandler

                return MarkdownHandler(
                    store=store,
                    embedder=eff_embedder,
                    root=Path(markdown_root),
                )

            _try_register(handlers, _build_markdown, label="markdown")

        # Perplexity Sonar trio (websearch / think / research). All
        # three share httpx + the ``PERPLEXITY_API_KEY`` env var.
        # Hidden when httpx is absent; per-key gating happens via
        # each handler's ``KindSpec.requires_env``. The embedder is
        # passed in so ``put(mode='import')`` — used by Pro
        # subscribers to cache free web-UI answers at $0 — produces
        # semantically searchable blocks.
        def _bind_perplexity(
            builder: Callable[..., Handler],
        ) -> Callable[[], Handler]:
            return lambda: builder(store=store, embedder=eff_embedder)

        for _label, _builder in (
            ("websearch", _build_perplexity_websearch),
            ("think", _build_perplexity_think),
            ("research", _build_perplexity_research),
        ):
            _try_register(handlers, _bind_perplexity(_builder), label=_label)

        # Patent kind — EPO Open Patent Services. Hidden unless
        # ``EPO_OPS_CLIENT_KEY``, ``EPO_OPS_CLIENT_SECRET`` and
        # ``PRECIS_PATENT_RAW_ROOT`` are all set; the
        # ``KindSpec.requires_env`` gate at ``Registry`` construction
        # then drops the kind. We construct the handler eagerly only
        # when all three are present so the live ``OpsClient`` (which
        # lazy-imports ``epo_ops``) gets exercised at startup just
        # enough to surface a missing-package error early.
        epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
        epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
        epo_raw_root = os.environ.get("PRECIS_PATENT_RAW_ROOT")
        if epo_key and epo_secret and epo_raw_root:

            def _build_patent() -> Handler:
                from pathlib import Path

                from precis.handlers._patent_ops import OpsClient
                from precis.handlers.patent import PatentHandler

                return PatentHandler(
                    store=store,
                    ops=OpsClient(
                        key=epo_key,
                        secret=epo_secret,
                        user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
                    ),
                    raw_root=Path(epo_raw_root).expanduser(),
                    embedder=eff_embedder,
                )

            _try_register(handlers, _build_patent, label="patent")

    return handlers


# ---------------------------------------------------------------------------
# Per-handler factories that the perplexity loop above feeds through
# ``_try_register``. Defined at module scope so each call gets a fresh
# import inside its own ``_try_register`` failure boundary — if the
# trio's shared ``httpx`` isn't installed, ``websearch`` fails first
# and the next two are still attempted (and will fail the same way,
# matching the previous "drop all three" behaviour without sharing
# their failure mode at the import level).
# ---------------------------------------------------------------------------


def _build_perplexity_websearch(*, store: Store, embedder: Embedder) -> Handler:
    from precis.handlers.perplexity import WebsearchHandler

    return WebsearchHandler(store=store, embedder=embedder)


def _build_perplexity_think(*, store: Store, embedder: Embedder) -> Handler:
    from precis.handlers.perplexity import ThinkHandler

    return ThinkHandler(store=store, embedder=embedder)


def _build_perplexity_research(*, store: Store, embedder: Embedder) -> Handler:
    from precis.handlers.perplexity import ResearchHandler

    return ResearchHandler(store=store, embedder=embedder)


class Registry:
    """Resolves a `kind=` string to a handler instance.

    Unavailable kinds (KindSpec.requires_env not satisfied) are silently
    omitted at construction time — the agent never sees them in the
    kind enum nor as a `NotFound.options` value.
    """

    def __init__(self, handlers: Iterable[Handler]) -> None:
        self._by_kind: dict[str, Handler] = {}
        for h in handlers:
            if not h.spec.is_available():
                continue
            if h.spec.kind in self._by_kind:
                raise ValueError(f"duplicate kind: {h.spec.kind}")
            self._by_kind[h.spec.kind] = h

    def get(self, kind: str) -> Handler:
        try:
            return self._by_kind[kind]
        except KeyError:
            raise NotFound(
                f"unknown kind: {kind}",
                options=sorted(self._by_kind.keys()),
                next="see precis-overview for the kind list",
            ) from None

    def kinds(self) -> list[str]:
        return sorted(self._by_kind.keys())

    def __contains__(self, kind: str) -> bool:
        return kind in self._by_kind

    def __len__(self) -> int:
        return len(self._by_kind)
