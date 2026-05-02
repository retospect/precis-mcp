"""Handler registration + flat dispatch table + service hub (seven-verb surface).

Replaces the v1 ``precis.registry`` module. The live server goes
through this module exclusively; ``precis.registry`` is gone.

The :class:`Hub` is both the **registration table** (``(kind, verb,
mode) -> callable``) and the **service hub** that hands out shared
infrastructure to handlers: the store (DB pool), embedder, and the
hint bus. Handlers receive the Hub via :meth:`Handler._register_with`
and stash it on ``self.hub``; from there they can call
``self.hub.embed_one(...)``, ``self.hub.emit_hint(...)``, etc. The
underlying objects stay swappable behind the service methods.

## Handler-author contract

Handler ``__init__`` must do all validation before its first
``register_ability`` / ``register_skill`` / ``register_overview``
call. If any validation fails, raise :class:`InitError` with a short
actionable message. :func:`boot` catches ``InitError``, logs WARN,
and leaves the kind absent from the dispatch surface. Any other
exception propagates — it's a bug, not a missing dep, and should
crash boot so it gets noticed. Register as the last block of
``__init__``; once any ``register_*`` call has run, the instance is
committed to the hub.

## Failure-mode semantics

A handler's ``__init__`` has exactly two exit paths:

- *Returns normally.* Instance exists, fully wired, every ability
  in the dispatch table points at a working bound method. Kind is
  live on the LLM surface.
- *Raises ``InitError``.* No instance created (Python never binds a
  name on a raising ``__init__``). Hub state untouched because
  registration is the last block. Kind invisible: absent from
  ``tools/list`` dispatch, from ``precis-help``, from search
  suggestions. Operator sees one WARN line naming the reason.

See D7 in the migration doc for the rationale and rejected
alternatives.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from precis.hints import Hint, HintBus

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.store import Store

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------

# Dispatch key: (kind, verb, mode). ``mode`` is ``None`` when the verb
# takes a single shape for the kind (e.g. ``delete``, ``tag``, ``link``).
AbilityKey = tuple[str, str, str | None]

# An ability is any callable; handler methods bind self via ``self.method``,
# so the signature is homogenised to ``**kwargs`` at the dispatch boundary.
# Each tool function in server.py knows the concrete call shape for its verb.
Ability = Callable[..., Any]


class InitError(RuntimeError):
    """Raised by a handler's ``__init__`` when it can't be usefully constructed.

    The boot loop catches this, logs WARN, and leaves the kind
    unregistered. See module docstring for the full contract.
    """


class DuplicateRegistration(RuntimeError):
    """Two handlers tried to register the same ``(kind, verb, mode)``.

    This is always a programming error — one handler is stepping on
    another's namespace. Fail loud at boot so the operator notices,
    rather than silently shadowing abilities at dispatch time.
    """


# ---------------------------------------------------------------------------
# Hub
# ---------------------------------------------------------------------------


@dataclass
class Hub:
    """Composition root: dispatch table + shared infrastructure.

    Two roles in one object:

    1. **Registration table** — ``(kind, verb, mode) -> callable``.
       Owned by :func:`boot`. Handlers mutate it via the
       ``register_*`` methods during their own ``__init__``. After
       boot it is read-only from the server's perspective; the MCP
       tool functions in ``server.py`` consult ``self.abilities`` on
       every call.
    2. **Service hub** — holds ``store`` (DB pool wrapper),
       ``embedder`` (vector backend), and ``hints`` (per-request
       hint collector). Handlers reach these through service methods
       (``embed_one``, ``emit_hint``, …) so the underlying
       implementations stay swappable.

    No decorator magic, no reflection on method names — every entry
    is placed here by an explicit ``register_ability(...)`` call, which
    makes ``rg register_ability`` the authoritative "who handles X?"
    query.

    Lifecycle: the ``store`` field is set by :func:`boot` from the
    composition root that opened the connection pool. The Hub itself
    does **not** own the store's close — that stays with whoever
    opened it (``PrecisRuntime`` in production). The Hub is otherwise
    self-contained: ``HintBus`` is constructed in-place, and the
    embedder is either passed in or left ``None`` for stateless
    deployments.
    """

    abilities: dict[AbilityKey, Ability] = field(default_factory=dict)
    skills:    dict[str, str]            = field(default_factory=dict)
    overview:  dict[str, str]            = field(default_factory=dict)
    #: Handler instances keyed by kind. Holds the object that owns
    #: the bound methods in ``abilities``. Runtime reads these for
    #: per-kind metadata (``KindSpec`` today; dropped once everything
    #: is driven from ``abilities`` + ``overview``).
    handlers: dict[str, Any] = field(default_factory=dict)

    #: Database-backed store. ``None`` for stateless deployments —
    #: store-backed handlers (memory, paper, todo, …) won't be
    #: registered in that case. Set once at boot; read freely.
    store:    Any = None  # precis.store.Store | None
    #: Vector embedder. ``None`` when no embedder is configured —
    #: handlers that need vectors should call :meth:`embed_one`,
    #: which raises a clean error in that case rather than crashing
    #: deep inside the call.
    embedder: Any = None  # precis.embedder.Embedder | None
    #: Per-request hint collector. Always present; emit hints via
    #: :meth:`emit_hint`. Handlers don't need to know about the
    #: contextvar plumbing.
    hints: HintBus = field(default_factory=HintBus)

    # ----- registration primitives (called from handler __init__) -----

    def register_ability(
        self,
        kind: str,
        verb: str,
        mode: str | None,
        fn: Ability,
    ) -> None:
        """Record one dispatch entry. Raises on duplicate key.

        ``mode`` is ``None`` when the verb is single-shaped for the
        kind. ``put`` is the only verb with multiple modes after the
        seven-verb migration (``create`` and ``replace``).
        """
        key: AbilityKey = (kind, verb, mode)
        if key in self.abilities:
            raise DuplicateRegistration(f"duplicate ability: {key!r}")
        self.abilities[key] = fn

    def register_skill(self, slug: str, text: str) -> None:
        """Publish a skill document under ``slug``. Raises on duplicate."""
        if slug in self.skills:
            raise DuplicateRegistration(f"duplicate skill: {slug!r}")
        self.skills[slug] = text

    def register_overview(self, kind: str, blurb: str) -> None:
        """Publish a one-line overview of ``kind``.

        Later-registered blurbs for the same kind overwrite silently —
        this is intentional: a composite handler that hosts multiple
        kinds can set an aggregate blurb after its per-kind calls.
        """
        self.overview[kind] = blurb

    def register_handler(self, kind: str, handler: Any) -> None:
        """Record the handler instance that owns ``kind``.

        The runtime reads ``handlers[kind]`` for per-kind metadata
        (``KindSpec``, ``search_hits`` method, etc.). Raises on
        duplicate — two handlers owning the same kind is a boot-time
        bug.
        """
        if kind in self.handlers:
            raise DuplicateRegistration(f"duplicate handler for kind: {kind!r}")
        self.handlers[kind] = handler

    # ----- read views (for the server and for internal introspection) -----

    @property
    def kinds(self) -> set[str]:
        """All kinds with at least one registered ability."""
        return {k for (k, _v, _m) in self.abilities}

    def handler_for(self, kind: str) -> Any | None:
        """Return the handler instance for ``kind``, or ``None``."""
        return self.handlers.get(kind)

    def verbs_for(self, kind: str) -> set[str]:
        """Set of verbs the given kind supports. Empty if kind unknown."""
        return {v for (k, v, _m) in self.abilities if k == kind}

    def modes_for(self, kind: str, verb: str) -> set[str | None]:
        """Modes registered for ``(kind, verb)``. Empty if none."""
        return {m for (k, v, m) in self.abilities if k == kind and v == verb}

    def kinds_supporting(self, verb: str) -> set[str]:
        """All kinds that have at least one ``(kind, verb, *)`` entry."""
        return {k for (k, v, _m) in self.abilities if v == verb}

    def get(self, kind: str, verb: str, mode: str | None = None) -> Ability | None:
        """Look up one ability. Returns ``None`` on miss.

        The server's tool functions use this; miss-handling (building
        the typed error + hint from D8) lives in the dispatch layer,
        not here.
        """
        return self.abilities.get((kind, verb, mode))

    def __contains__(self, kind: object) -> bool:
        """Allow ``kind in hub`` to mean "kind is registered"."""
        return kind in self.kinds

    def __len__(self) -> int:
        """Number of live kinds."""
        return len(self.kinds)

    # ----- service methods (the "hub" half of the Hub) -----

    def embed_one(self, text: str) -> list[float]:
        """Embed a single text into the configured vector space.

        Raises :class:`RuntimeError` when no embedder is wired — a
        handler that reached for the embedder on a stateless build
        is mis-registered, since :func:`boot` skips embedder-needing
        handlers when ``embedder is None``. Hitting this means a
        handler optional-dep guard is missing.
        """
        if self.embedder is None:
            raise RuntimeError(
                "hub has no embedder configured; "
                "this handler should have raised InitError at boot"
            )
        return self.embedder.embed_one(text)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a batch of texts. See :meth:`embed_one` for failure mode."""
        if self.embedder is None:
            raise RuntimeError(
                "hub has no embedder configured; "
                "this handler should have raised InitError at boot"
            )
        return self.embedder.embed(texts)

    def emit_hint(self, hint: Hint) -> None:
        """Append a hint to the current request's collector.

        No-op outside a request scope (so module-import-time and
        boot-time emissions don't leak into the next request). The
        runtime opens the scope around every dispatch call.
        """
        self.hints.emit(hint)

    def request_scope(self) -> Any:
        """Open a hint-collection request scope. Use as a context manager.

        ``with hub.request_scope(): ...`` — hints emitted inside the
        block are collected for that request and rendered after the
        verb result. The runtime owns this; handlers shouldn't need
        to call it directly.
        """
        return self.hints.request()

    def get_conn_pool(self) -> Any:
        """Return the underlying ``psycopg_pool.ConnectionPool``, or ``None``.

        Provided so handlers that need raw SQL (cache primitives,
        bespoke queries) can reach the pool without hard-coding
        ``self.store.pool``. Returns ``None`` on stateless builds.
        Mostly future-facing; today's handlers use the higher-level
        ``Store`` API directly.
        """
        return None if self.store is None else self.store.pool


# ---------------------------------------------------------------------------
# Boot helpers
# ---------------------------------------------------------------------------


def _try(cls: Callable[..., Any], *, hub: Hub, **kw: Any) -> Any | None:
    """Construct a handler, auto-register it, swallow missing-dep errors.

    Caught exceptions:

    - :class:`InitError` — the handler's own ``__init__`` decided it
      can't usefully run. The canonical path.
    - ``ImportError`` — optional-dep handlers (math/sympy,
      patent/epo_ops) surface here when their module-level imports
      blow up. Treated as a missing dep, not a programmer bug.
    - ``ValueError`` — file-root handlers (markdown/plaintext/python)
      raise this from their existing ``__init__`` for malformed /
      non-existent roots. Legacy behaviour, preserved for now;
      eventually those paths convert to ``InitError``.

    Anything else propagates — a stray ``KeyError`` /
    ``AttributeError`` is a programmer bug and should crash boot so
    it gets noticed.

    On successful construction, calls ``inst._register_with(hub)`` to
    populate the dispatch table. This is the seam that makes
    construction and registration atomic from the caller's
    perspective: :func:`_try` returns either a fully registered
    handler or ``None``; it never returns a constructed-but-
    unregistered instance.

    ``hub`` is threaded into the constructor as a kwarg — every
    handler ``__init__`` takes ``*, hub: Hub`` (plus optional handler-
    specific extras like ``root=`` / ``ops=``). Boot sites pass only
    ``hub=hub`` plus those extras; the rest comes off the hub itself.
    """
    try:
        inst = cls(hub=hub, **kw)
    except (InitError, ImportError, ValueError) as exc:
        log.warning("%s init failed: %s", getattr(cls, "__name__", cls), exc)
        return None
    inst._register_with(hub)
    return inst


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def boot(
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    markdown_root: str | None = None,
    plaintext_root: str | None = None,
    python_roots: str | None = None,
) -> Hub:
    """Build and return a fully-populated :class:`Hub`.

    The composition root. Hand-ordered by dependency: construct
    infrastructure kinds first (embedder, store-backed primitives),
    then the kinds that consume them. Each step goes through
    :func:`_try` so any :class:`InitError` is logged and the kind
    silently drops off the LLM surface.

    Stateless handlers (calc) are always attempted. Store-backed
    handlers (memory, todo, paper, ...) are skipped when ``store`` is
    ``None`` — this preserves the phase-1 stateless deployment mode
    from the old ``registry.builtins()``.

    Optional-dependency handlers (math needs sympy, patent needs
    ``epo_ops``, etc.) raise :class:`InitError` from their own
    ``__init__`` when their deps aren't satisfied; :func:`_try`
    catches and logs.

    The returned :class:`Hub` carries the live ``store`` and
    ``embedder`` references so handlers can reach them via
    ``self.hub.embed_one(...)`` etc. without each one needing its
    own copy of the dependency wiring.

    See ``docs/seven-verb-surface-migration.md`` D7/D8 for the design
    rationale and rejected alternatives.
    """
    import os  # local import; dispatch shouldn't own env reading above

    # If a store is wired but no embedder was provided, fall back to
    # the deterministic mock at the right dim. Doing this here —
    # rather than per-handler — means every handler that asks the
    # hub for an embedder gets the same instance.
    if store is not None and embedder is None:
        from precis.embedder import MockEmbedder
        embedder = MockEmbedder(dim=store.embedding_dim())

    hub = Hub(store=store, embedder=embedder)

    # --- Stateless handlers (no store) ---------------------------------

    # Calc — local sympy-backed calculator. The handler raises
    # InitError when sympy isn't installed.
    from precis.handlers.calc import CalcHandler
    _try(CalcHandler, hub=hub)

    # Python — DB-free in-memory AST index. Skipped when no roots
    # are configured or every entry is malformed (parse_python_roots
    # logs each rejection).
    if python_roots:
        from precis.handlers.python import PythonHandler, parse_python_roots
        roots = parse_python_roots(python_roots)
        if roots:
            _try(PythonHandler, hub=hub, roots=roots)

    # --- Store-backed handlers ------------------------------------------

    if store is not None:
        from precis.handlers.conversation import ConversationHandler
        from precis.handlers.flashcard import FlashcardHandler
        from precis.handlers.gripe import GripeHandler
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.oracle import OracleHandler
        from precis.handlers.paper import PaperHandler
        from precis.handlers.quest import QuestHandler
        from precis.handlers.random import RandomHandler
        from precis.handlers.skill import SkillHandler
        from precis.handlers.todo import TodoHandler

        # Numeric- and slug-addressed refs. Cheap; always available
        # when the store is up. Each handler reads ``hub.store`` /
        # ``hub.embedder`` directly — boot only threads the hub.
        _try(MemoryHandler,       hub=hub)
        _try(TodoHandler,         hub=hub)
        _try(GripeHandler,        hub=hub)
        _try(FlashcardHandler,    hub=hub)
        _try(QuestHandler,        hub=hub)
        _try(ConversationHandler, hub=hub)
        _try(OracleHandler,       hub=hub)
        _try(SkillHandler,        hub=hub)
        _try(PaperHandler,        hub=hub)

        # Corpus-wide random-pick. Store-backed because it reads
        # ``blocks`` directly; no embedder needed (it uses the
        # stored embeddings as a "has content" filter, not for
        # similarity). Raises NotFound on an empty corpus.
        _try(RandomHandler,       hub=hub)

        # Cache-backed kinds. Each declares its env / optional-dep
        # requirements inside __init__ and raises InitError when
        # they aren't met.
        from precis.handlers.math import MathHandler
        _try(MathHandler,    hub=hub)

        from precis.handlers.youtube import YouTubeHandler
        _try(YouTubeHandler, hub=hub)

        from precis.handlers.web import WebHandler
        _try(WebHandler,     hub=hub)

        # File handlers — markdown / plaintext are hidden when no root
        # is configured; the handler __init__ raises InitError for a
        # missing / non-existent / non-directory root.
        if markdown_root:
            from pathlib import Path

            from precis.handlers.markdown import MarkdownHandler
            _try(MarkdownHandler, hub=hub, root=Path(markdown_root))

        if plaintext_root:
            from pathlib import Path

            from precis.handlers.plaintext import PlaintextHandler
            _try(PlaintextHandler, hub=hub, root=Path(plaintext_root))

        # Perplexity Sonar trio. Each raises InitError independently
        # when httpx or the API key is missing.
        from precis.handlers.perplexity import (
            ResearchHandler,
            ThinkHandler,
            WebsearchHandler,
        )
        _try(WebsearchHandler, hub=hub)
        _try(ThinkHandler,     hub=hub)
        _try(ResearchHandler,  hub=hub)

        # Patent — EPO OPS. Hidden unless the env trio is set; the
        # ``OpsClient`` construction (and thus the ``epo_ops`` import)
        # is deferred so missing env vars don't even reach the
        # handler.
        epo_key = os.environ.get("EPO_OPS_CLIENT_KEY")
        epo_secret = os.environ.get("EPO_OPS_CLIENT_SECRET")
        epo_raw_root = os.environ.get("PRECIS_PATENT_RAW_ROOT")
        if epo_key and epo_secret and epo_raw_root:
            from pathlib import Path

            from precis.handlers._patent_ops import OpsClient
            from precis.handlers.patent import PatentHandler
            _try(
                PatentHandler,
                hub=hub,
                ops=OpsClient(
                    key=epo_key,
                    secret=epo_secret,
                    user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
                ),
                raw_root=Path(epo_raw_root).expanduser(),
            )

    log.info(
        "precis dispatch boot: %d kinds live: %s",
        len(hub.kinds),
        sorted(hub.kinds),
    )
    return hub


__all__ = [
    "Ability",
    "AbilityKey",
    "DuplicateRegistration",
    "Hub",
    "InitError",
    "boot",
]
