"""In-tree handler registry.

V2 drops setuptools entry-point plugin discovery. New kinds = append a
class to `BUILTINS()` here and add a row to the `kinds` reference table
in a migration. Two-step, explicit, greppable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from precis.errors import NotFound

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.protocol import Handler
    from precis.store import Store


def builtins(
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    markdown_root: str | None = None,
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
    from precis.handlers.calc import CalcHandler

    handlers: list[Handler] = [CalcHandler()]

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
        # `KindSpec.requires_env`; the dispatcher hides them from the
        # agent enum when the env vars aren't set, and the actual
        # network call only fires inside ``_fetch`` (lazy, on cache miss).
        # Optional deps (wolframalpha, httpx, etc.) are guarded by the
        # `[external]` extra; if missing, importing the handler raises
        # ImportError and the kind is skipped silently below.
        try:
            from precis.handlers.math import MathHandler

            handlers.append(MathHandler(store=store))
        except ImportError:
            pass  # missing wolframalpha → kind not available

        try:
            from precis.handlers.youtube import YouTubeHandler

            handlers.append(YouTubeHandler(store=store))
        except ImportError:
            pass  # missing youtube-transcript-api → kind not available

        try:
            from precis.handlers.web import WebHandler

            handlers.append(WebHandler(store=store))
        except ImportError:
            pass  # missing httpx/trafilatura → kind not available

        # File handler — markdown. Hidden when no root is configured;
        # also hidden when the root path doesn't exist (treat as
        # mis-configuration; better to skip than to crash on every call).
        if markdown_root:
            try:
                from pathlib import Path

                from precis.handlers.markdown import MarkdownHandler

                handlers.append(
                    MarkdownHandler(
                        store=store,
                        embedder=eff_embedder,
                        root=Path(markdown_root),
                    )
                )
            except (ImportError, ValueError):
                pass  # bad root or missing dep → kind not available

        # Perplexity Sonar trio (websearch / think / research). All three
        # share httpx + the PERPLEXITY_API_KEY env var. Hidden when
        # either is absent.
        try:
            from precis.handlers.perplexity import (
                ResearchHandler,
                ThinkHandler,
                WebsearchHandler,
            )

            handlers.append(WebsearchHandler(store=store))
            handlers.append(ThinkHandler(store=store))
            handlers.append(ResearchHandler(store=store))
        except ImportError:
            pass  # missing httpx → not available

    return handlers


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
