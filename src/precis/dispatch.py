"""Handler registration + flat dispatch table (seven-verb surface).

Replaces the v1 ``precis.registry`` module. The live server goes
through this module exclusively; ``precis.registry`` is gone.

## Handler-author contract

Handler ``__init__`` must do all validation before its first
``register_ability`` / ``register_skill`` / ``register_overview``
call. If any validation fails, raise :class:`InitError` with a short
actionable message. :func:`boot` catches ``InitError``, logs WARN,
and leaves the kind absent from the dispatch surface. Any other
exception propagates — it's a bug, not a missing dep, and should
crash boot so it gets noticed. Register as the last block of
``__init__``; once any ``register_*`` call has run, the instance is
committed to the registry.

## Failure-mode semantics

A handler's ``__init__`` has exactly two exit paths:

- *Returns normally.* Instance exists, fully wired, every ability
  in the dispatch table points at a working bound method. Kind is
  live on the LLM surface.
- *Raises ``InitError``.* No instance created (Python never binds a
  name on a raising ``__init__``). Registry state untouched because
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
from typing import Any

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
# Registry
# ---------------------------------------------------------------------------


@dataclass
class Registry:
    """Flat dispatch table: ``(kind, verb, mode) -> callable``.

    Owned by :func:`boot`. Handlers mutate it via the ``register_*``
    methods during their own ``__init__``. After boot it is read-only
    from the server's perspective; the MCP tool functions in
    ``server.py`` consult ``self.abilities`` on every call.

    No decorator magic, no reflection on method names — every entry
    is placed here by an explicit ``register_ability(...)`` call, which
    makes ``rg register_ability`` the authoritative "who handles X?"
    query.
    """

    abilities: dict[AbilityKey, Ability] = field(default_factory=dict)
    skills:    dict[str, str]            = field(default_factory=dict)
    overview:  dict[str, str]            = field(default_factory=dict)
    #: Handler instances keyed by kind. Holds the object that owns
    #: the bound methods in ``abilities``. Runtime reads these for
    #: per-kind metadata (``KindSpec`` today; dropped once everything
    #: is driven from ``abilities`` + ``overview``).
    handlers: dict[str, Any] = field(default_factory=dict)

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


# ---------------------------------------------------------------------------
# Boot helpers
# ---------------------------------------------------------------------------


def _try(cls: Callable[..., Any], *, r: Registry, **kw: Any) -> Any | None:
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

    On successful construction, calls ``inst._register_with(r)`` to
    populate the dispatch table. This is the seam that makes
    construction and registration atomic from the caller's
    perspective: :func:`_try` returns either a fully registered
    handler or ``None``; it never returns a constructed-but-
    unregistered instance.
    """
    try:
        inst = cls(**kw)
    except (InitError, ImportError, ValueError) as exc:
        log.warning("%s init failed: %s", getattr(cls, "__name__", cls), exc)
        return None
    inst._register_with(r)
    return inst


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def boot(
    *,
    store: Any = None,
    embedder: Any = None,
    markdown_root: str | None = None,
    plaintext_root: str | None = None,
    python_roots: str | None = None,
) -> Registry:
    """Build and return a fully-populated :class:`Registry`.

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

    See ``docs/seven-verb-surface-migration.md`` D7/D8 for the design
    rationale and rejected alternatives.
    """
    import os  # local import; dispatch shouldn't own env reading above

    r = Registry()

    # --- Stateless handlers (no store) ---------------------------------

    # Calc — local sympy-backed calculator. The handler raises
    # InitError when sympy isn't installed.
    from precis.handlers.calc import CalcHandler
    _try(CalcHandler, r=r)

    # Python — DB-free in-memory AST index. Skipped when no roots
    # are configured or every entry is malformed (parse_python_roots
    # logs each rejection).
    if python_roots:
        from precis.handlers.python import PythonHandler, parse_python_roots
        roots = parse_python_roots(python_roots)
        if roots:
            _try(PythonHandler, r=r, roots=roots)

    # --- Store-backed handlers ------------------------------------------

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

        eff_embedder = embedder or MockEmbedder(dim=store.embedding_dim())

        # Numeric- and slug-addressed refs. Cheap; always available
        # when the store is up.
        _try(MemoryHandler,       r=r, store=store)
        _try(TodoHandler,         r=r, store=store)
        _try(GripeHandler,        r=r, store=store)
        _try(FlashcardHandler,    r=r, store=store)
        _try(QuestHandler,        r=r, store=store)
        _try(ConversationHandler, r=r, store=store)
        _try(OracleHandler,       r=r, store=store)
        _try(SkillHandler,        r=r, store=store)
        _try(PaperHandler,        r=r, store=store, embedder=eff_embedder)

        # Cache-backed kinds. Each declares its env / optional-dep
        # requirements inside __init__ and raises InitError when
        # they aren't met.
        from precis.handlers.math import MathHandler
        _try(MathHandler,    r=r, store=store)

        from precis.handlers.youtube import YouTubeHandler
        _try(YouTubeHandler, r=r, store=store)

        from precis.handlers.web import WebHandler
        _try(WebHandler,     r=r, store=store)

        # File handlers — markdown / plaintext are hidden when no root
        # is configured; the handler __init__ raises InitError for a
        # missing / non-existent / non-directory root.
        if markdown_root:
            from pathlib import Path

            from precis.handlers.markdown import MarkdownHandler
            _try(
                MarkdownHandler,
                r=r,
                store=store,
                embedder=eff_embedder,
                root=Path(markdown_root),
            )

        if plaintext_root:
            from pathlib import Path

            from precis.handlers.plaintext import PlaintextHandler
            _try(
                PlaintextHandler,
                r=r,
                store=store,
                embedder=eff_embedder,
                root=Path(plaintext_root),
            )

        # Perplexity Sonar trio. Each raises InitError independently
        # when httpx or the API key is missing.
        from precis.handlers.perplexity import (
            ResearchHandler,
            ThinkHandler,
            WebsearchHandler,
        )
        _try(WebsearchHandler, r=r, store=store, embedder=eff_embedder)
        _try(ThinkHandler,     r=r, store=store, embedder=eff_embedder)
        _try(ResearchHandler,  r=r, store=store, embedder=eff_embedder)

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
                r=r,
                store=store,
                embedder=eff_embedder,
                ops=OpsClient(
                    key=epo_key,
                    secret=epo_secret,
                    user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
                ),
                raw_root=Path(epo_raw_root).expanduser(),
            )

    log.info(
        "precis dispatch boot: %d kinds live: %s",
        len(r.kinds),
        sorted(r.kinds),
    )
    return r


__all__ = [
    "Ability",
    "AbilityKey",
    "DuplicateRegistration",
    "InitError",
    "Registry",
    "boot",
]
