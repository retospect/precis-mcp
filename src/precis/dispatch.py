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
from importlib.metadata import entry_points as _entry_points
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
    skills: dict[str, str] = field(default_factory=dict)
    overview: dict[str, str] = field(default_factory=dict)
    #: Handler instances keyed by kind. Holds the object that owns
    #: the bound methods in ``abilities``. Runtime reads these for
    #: per-kind metadata (``KindSpec`` today; dropped once everything
    #: is driven from ``abilities`` + ``overview``).
    handlers: dict[str, Any] = field(default_factory=dict)

    #: Database-backed store. ``None`` for stateless deployments —
    #: store-backed handlers (memory, paper, todo, …) won't be
    #: registered in that case. Set once at boot; read freely.
    store: Any = None  # precis.store.Store | None
    #: Vector embedder. ``None`` when no embedder is configured —
    #: handlers that need vectors should call :meth:`embed_one`,
    #: which raises a clean error in that case rather than crashing
    #: deep inside the call.
    embedder: Any = None  # precis.embedder.Embedder | None
    #: Per-request hint collector. Always present; emit hints via
    #: :meth:`emit_hint`. Handlers don't need to know about the
    #: contextvar plumbing.
    hints: HintBus = field(default_factory=HintBus)

    #: Per-kind boot-time verdicts. Populated by :func:`_try` for
    #: every kind it considered — loaded or skipped. Kinds skipped
    #: because their outer if-guard short-circuits in :func:`boot`
    #: (e.g. file kinds when ``PRECIS_ROOT`` is unset) do not appear.
    #: :func:`precis.server._build_instructions` reads this to render
    #: the cold-start ``Kinds unavailable:`` banner line. Schema is
    #: a free-form :class:`precis.kind_gate.Loadability` so handlers
    #: that aren't ``KindSpec``-shaped (plugin entry-points, etc.)
    #: can also surface verdicts without dragging the protocol layer
    #: into ``dispatch``.
    loadabilities: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # Wire the store's back-reference to this hub's hint bus so low-level
        # store ops (the merged-handle redirect in ``resolve_handle``, the
        # bare-numeric admonish in ``resolve_live_slug_ref``) can emit a
        # non-breaking agent hint without every caller threading a ``hub``.
        # Single wiring point for prod (via ``boot``) and tests (the ``hub``
        # fixture builds ``Hub(store=...)`` directly).
        if self.store is not None:
            self.store.hint_bus = self.hints

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

    def kind_specs(self) -> list[Any]:
        """Return the :class:`KindSpec` for every registered handler.

        Used by the boot-time kinds-table upsert (see :func:`boot`):
        each process exports its hub's enabled kinds, the store does
        ``INSERT ... ON CONFLICT (slug) DO UPDATE`` for each, and the
        ``kinds`` table stays the union of every-kind-anyone-has-ever-
        registered. The runtime's per-call validator (numeric-vs-slug
        on insert_ref) keeps querying the table; the table is now a
        denormalised cache fed by code rather than a separate registry
        kept in sync by hand.

        Returned in deterministic slug order so the boot log is stable
        across runs.
        """
        out: list[Any] = []
        seen: set[str] = set()
        for kind in sorted(self.handlers):
            handler = self.handlers[kind]
            spec = getattr(handler, "spec", None)
            if spec is None or spec.kind in seen:
                continue
            seen.add(spec.kind)
            out.append(spec)
        return out

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


def _try(
    cls: Callable[..., Any],
    *,
    hub: Hub,
    disabled: frozenset[str] = frozenset(),
    reasons: dict[str, str] | None = None,
    **kw: Any,
) -> Any | None:
    """Construct a handler, auto-register it, swallow missing-dep errors.

    Pre-construction the kind-enablement gate
    (:func:`precis.kind_gate.gate`) runs against ``cls.spec`` to:

    - **Skip prohibited kinds** (listed in ``PRECIS_KINDS_DISABLED``,
      parsed into ``disabled``). The handler module is not imported.
    - **Skip kinds with missing declared envs** (every name in
      ``cls.spec.requires_env`` must be set non-empty). Today only
      ``math`` (``WOLFRAM_APP_ID``) declares envs this way; Phase-4
      convergence moves the patent's inline boot-site env check into
      its ``KindSpec.requires_env`` too.

    For both skip paths we record a :class:`precis.kind_gate.Loadability`
    on ``hub.loadabilities`` and return ``None`` without importing
    or constructing the handler.

    Caught construction-time exceptions:

    - :class:`InitError` — the handler's own ``__init__`` decided it
      can't usefully run. The canonical path for store / embedder /
      file-root unavailability.
    - ``ImportError`` — optional-dep handlers (math/sympy,
      patent/epo_ops) surface here when their module-level imports
      blow up.
    - ``ValueError`` — file-root handlers (markdown/plaintext/python)
      raise this from their existing ``__init__`` for malformed /
      non-existent roots. Legacy behaviour, preserved for now;
      eventually those paths convert to :class:`InitError`.

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
    from precis.kind_gate import Loadability, gate, loadability_from_exception

    spec = getattr(cls, "spec", None)
    if spec is not None:
        verdict = gate(spec, disabled=disabled, reasons=reasons)
        if not verdict.loaded:
            hub.loadabilities[spec.kind] = verdict
            log.info(
                "precis dispatch boot: skipped kind=%s (%s)",
                spec.kind,
                verdict.reason,
            )
            return None

    try:
        inst = cls(hub=hub, **kw)
    except (InitError, ImportError, ValueError) as exc:
        log.warning("%s init failed: %s", getattr(cls, "__name__", cls), exc)
        if spec is not None:
            hub.loadabilities[spec.kind] = loadability_from_exception(spec, exc)
        return None
    if spec is not None:
        hub.loadabilities[spec.kind] = Loadability(kind=spec.kind, loaded=True)
    inst._register_with(hub)
    return inst


# ---------------------------------------------------------------------------
# Plugin loading (third-party kinds via entry-points)
# ---------------------------------------------------------------------------


PLUGIN_GROUP = "precis.handlers"


def _load_plugins(hub: Hub) -> None:
    """Discover and register third-party handlers via entry-points.

    A plugin package advertises a handler class in its own
    ``pyproject.toml``::

        [project.entry-points."precis.handlers"]
        wikipedia = "precis_wikipedia:WikipediaHandler"

    The class must follow the same contract as a built-in handler:
    subclass :class:`~precis.protocol.Handler`, declare a
    :class:`~precis.protocol.KindSpec` ClassVar, accept
    ``*, hub: Hub`` in ``__init__``, and raise
    :class:`InitError` from ``__init__`` when it can't usefully run.
    See ``docs/user-facing/plugin-authoring.md`` for the full write-up; the
    canonical minimal example is
    :class:`precis.handlers.calc.CalcHandler`.

    Failure semantics are deliberately **wider** than :func:`_try`:
    built-in handlers are trusted code, so a stray ``RuntimeError``
    in one of them crashes boot to surface the bug. Plugin code is
    third-party — one buggy plugin must not brick the MCP server.
    Every ``Exception`` raised during plugin load / init /
    registration is caught and logged. Only ``BaseException``
    subclasses (``KeyboardInterrupt``, ``SystemExit``) propagate.

    Plugins load **after** built-ins, so a plugin attempting to
    claim a built-in kind hits
    :class:`DuplicateRegistration` and is logged; the built-in
    wins.
    """
    try:
        eps = _entry_points(group=PLUGIN_GROUP)
    except Exception as exc:  # defensive — importlib surface is stable
        log.warning("precis plugin discovery failed: %s", exc)
        return

    for ep in eps:
        name = getattr(ep, "name", "<unknown>")
        try:
            cls = ep.load()
        except Exception as exc:
            log.warning(
                "precis plugin %r failed to load (%s): %s",
                name,
                type(exc).__name__,
                exc,
            )
            continue

        cls_name = getattr(cls, "__name__", repr(cls))

        try:
            inst = cls(hub=hub)
        except (InitError, ImportError, ValueError) as exc:
            log.warning(
                "precis plugin %r (%s) init failed: %s",
                name,
                cls_name,
                exc,
            )
            continue
        except Exception as exc:
            log.warning(
                "precis plugin %r (%s) raised %s during __init__: %s",
                name,
                cls_name,
                type(exc).__name__,
                exc,
            )
            continue

        try:
            inst._register_with(hub)
        except DuplicateRegistration as exc:
            log.warning(
                "precis plugin %r (%s) could not register: %s",
                name,
                cls_name,
                exc,
            )
        except Exception as exc:
            log.warning(
                "precis plugin %r (%s) raised %s during registration: %s",
                name,
                cls_name,
                type(exc).__name__,
                exc,
            )


# ---------------------------------------------------------------------------
# Composition root
# ---------------------------------------------------------------------------


def boot(
    *,
    store: Store | None = None,
    embedder: Embedder | None = None,
    precis_root: str | None = None,
    python_roots: str | None = None,
    kinds_disabled: frozenset[str] = frozenset(),
    kinds_disabled_reasons: dict[str, str] | None = None,
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

    See ``docs/user-facing/seven-verb-surface-migration.md`` D7/D8 for the design
    rationale and rejected alternatives.
    """
    # If a store is wired but no embedder was provided, fall back to
    # the deterministic mock at the right dim. Doing this here —
    # rather than per-handler — means every handler that asks the
    # hub for an embedder gets the same instance.
    if store is not None and embedder is None:
        from precis.embedder import MockEmbedder

        embedder = MockEmbedder(dim=store.embedding_dim())

    hub = Hub(store=store, embedder=embedder)

    def _gated(cls: Callable[..., Any], **kw: Any) -> Any | None:
        """Local _try alias capturing ``hub`` + the parsed prohibition
        set. Each call site keeps only its handler-specific kwargs."""
        return _try(
            cls,
            hub=hub,
            disabled=kinds_disabled,
            reasons=kinds_disabled_reasons,
            **kw,
        )

    # --- Stateless handlers (no store) ---------------------------------

    # Calc — local sympy-backed calculator. The handler raises
    # InitError when sympy isn't installed.
    from precis.handlers.calc import CalcHandler

    _gated(CalcHandler)

    # Provenance — Crossref-backed retraction / amendment check.
    # Works with or without a store: when the parent paper is in
    # the store, write-through persists notice refs and STATUS tags;
    # otherwise the result is informational only. Handler raises
    # InitError when habanero isn't installed (matches the calc →
    # sympy missing-dep pattern).
    from precis.handlers.provenance import ProvenanceHandler

    _gated(ProvenanceHandler)

    # Python — DB-free in-memory AST index. Skipped when no roots
    # are configured or every entry is malformed (parse_python_roots
    # logs each rejection).
    if python_roots:
        from precis.handlers.python import PythonHandler, parse_python_roots

        roots = parse_python_roots(python_roots)
        if roots:
            _gated(PythonHandler, roots=roots)
        else:
            # Roots configured but every entry was malformed — same
            # deferred-kind treatment as markdown/plaintext/tex below.
            from precis.kind_gate import Loadability

            hub.loadabilities["python"] = Loadability(
                kind="python",
                loaded=False,
                reason="PRECIS_PYTHON_ROOTS parsed empty",
            )
    else:
        # PRECIS_PYTHON_ROOTS unset — record python as deferred so the
        # dispatcher returns Unsupported-with-env-var. (Round-2 picky
        # F-4 / N3.)
        from precis.kind_gate import Loadability

        hub.loadabilities["python"] = Loadability(
            kind="python", loaded=False, reason="missing PRECIS_PYTHON_ROOTS"
        )

    # --- Store-backed handlers ------------------------------------------

    if store is not None:
        from precis.handlers.agentlog import AgentLogHandler
        from precis.handlers.alert import AlertHandler
        from precis.handlers.cad import CadHandler
        from precis.handlers.citation import CitationHandler
        from precis.handlers.conversation import ConversationHandler
        from precis.handlers.cron import CronHandler
        from precis.handlers.datasheet import DatasheetHandler
        from precis.handlers.draft import DraftHandler
        from precis.handlers.finding import FindingHandler
        from precis.handlers.flashcard import FlashcardHandler
        from precis.handlers.folder import FolderHandler
        from precis.handlers.gripe import GripeHandler
        from precis.handlers.job import JobHandler
        from precis.handlers.memory import MemoryHandler
        from precis.handlers.message import MessageHandler
        from precis.handlers.oracle import OracleHandler
        from precis.handlers.paper import PaperHandler
        from precis.handlers.part import PartHandler
        from precis.handlers.pcb import PcbHandler
        from precis.handlers.plan import PlanHandler
        from precis.handlers.presentation import PresentationHandler
        from precis.handlers.random import RandomHandler
        from precis.handlers.skill import SkillHandler
        from precis.handlers.structure import StructureHandler
        from precis.handlers.tag import TagHandler
        from precis.handlers.todo import TodoHandler

        # Numeric- and slug-addressed refs. Cheap; always available
        # when the store is up. Each handler reads ``hub.store`` /
        # ``hub.embedder`` directly — boot only threads the hub.
        _gated(MemoryHandler)
        _gated(TodoHandler)
        _gated(FolderHandler)
        _gated(GripeHandler)
        _gated(AlertHandler)
        _gated(AgentLogHandler)
        _gated(JobHandler)
        _gated(FlashcardHandler)
        _gated(CitationHandler)
        _gated(FindingHandler)
        _gated(ConversationHandler)
        _gated(CronHandler)
        _gated(MessageHandler)
        _gated(PresentationHandler)
        _gated(DraftHandler)
        _gated(PlanHandler)
        _gated(CadHandler)
        _gated(StructureHandler)
        _gated(PcbHandler)
        _gated(PartHandler)
        _gated(DatasheetHandler)
        _gated(OracleHandler)
        # Oracle YAML lives in the wheel; reconcile it against the
        # DB-recorded version on every boot so a wheel upgrade or
        # local edit propagates without an explicit ingest run. Older
        # peers see their version is below the stored one and skip,
        # avoiding the cross-host stomp scenario. See
        # ``jobs/oracle_sync.py`` for the full gating logic. Best-
        # effort: any failure here is logged and ignored so a sync
        # hiccup never breaks startup.
        if "oracle" in hub.kinds:
            from precis.jobs.oracle_sync import is_disabled_by_env, maybe_reingest

            if not is_disabled_by_env():
                # F11: run oracle_sync on a daemon thread rather than
                # inline. Synchronous reconcile blocked every CLI
                # invocation behind the bge-m3 cold-start whenever the
                # bundled YAML version was newer than the stored
                # version — so read-only calls like ``view='bibtex'``
                # paid 30-50 s for no reason. Backgrounding it keeps
                # the boot path fast; the YAML changes propagate on
                # the first read that actually completes after sync
                # finishes (and the postgres advisory lock prevents
                # concurrent syncs from stepping on each other).
                import threading

                def _bg_sync() -> None:
                    try:
                        maybe_reingest(store=hub.store, embedder=hub.embedder)
                    except Exception:  # pragma: no cover
                        log.exception("oracle_sync: background reconcile failed")

                threading.Thread(
                    target=_bg_sync,
                    name="precis-oracle-sync",
                    daemon=True,
                ).start()
        _gated(SkillHandler)
        _gated(PaperHandler)
        # CFP — spec-role sibling of paper (same ingest + reader core,
        # non-citable). Imported lazily after PaperHandler since it
        # subclasses it.
        from precis.handlers.cfp import CfpHandler

        _gated(CfpHandler)

        # Tag — corpus-wide discovery surface over the tags table.
        # Always-on (store-only dep); the embedder is optional and
        # the handler falls back to lexical search without it.
        _gated(TagHandler)

        # Corpus-wide random-pick. Store-backed because it reads
        # ``blocks`` directly; no embedder needed (it uses the
        # stored embeddings as a "has content" filter, not for
        # similarity). Raises NotFound on an empty corpus.
        _gated(RandomHandler)

        # Cache-backed kinds. Each declares its env / optional-dep
        # requirements inside __init__ and raises InitError when
        # they aren't met.
        from precis.handlers.math import MathHandler

        _gated(MathHandler)

        from precis.handlers.youtube import YouTubeHandler

        _gated(YouTubeHandler)

        from precis.handlers.web import WebHandler

        _gated(WebHandler)

        # File handlers — markdown / plaintext / tex all walk the same
        # PRECIS_ROOT, scoped by extension. The whole trio is hidden
        # when no root is configured. Each handler's __init__ raises
        # InitError for a missing / non-existent / non-directory root.
        if precis_root:
            from pathlib import Path

            from precis.handlers.markdown import MarkdownHandler
            from precis.handlers.plaintext import PlaintextHandler
            from precis.handlers.tex import TexHandler

            root = Path(precis_root)
            _gated(MarkdownHandler, root=root)
            _gated(PlaintextHandler, root=root)
            _gated(TexHandler, root=root)
        else:
            # PRECIS_ROOT unset — record the file kinds as deferred so
            # the dispatcher (runtime._resolve_handler) returns
            # ``Unsupported`` with the missing env var named, rather
            # than ``NotFound: unknown kind``. Without this branch the
            # short-circuit above skipped `_gated` entirely and the
            # kinds were invisible to the loadability index. Round-2
            # picky F-4 / N3, 2026-05-30. We avoid importing the
            # handler modules to keep the no-root boot path lean.
            from precis.kind_gate import Loadability

            for kind in ("markdown", "plaintext", "tex"):
                hub.loadabilities[kind] = Loadability(
                    kind=kind, loaded=False, reason="missing PRECIS_ROOT"
                )

        # Perplexity Sonar trio. Each raises InitError independently
        # when httpx or the API key is missing.
        from precis.handlers.perplexity import (
            ResearchHandler,
            ThinkHandler,
            WebsearchHandler,
        )

        _gated(WebsearchHandler)
        _gated(ThinkHandler)
        _gated(ResearchHandler)

        # Semantic Scholar — paper search via the S2 Graph API.
        # API key is optional (just raises the rate limit), so this
        # always registers when httpx is available.
        from precis.handlers.semanticscholar import SemanticScholarHandler

        _gated(SemanticScholarHandler)

        # ORCID — durable author-identity node (ADR 0039). Resolves an
        # iD via the ORCID Public API, stores a refreshable link hub,
        # links works already held, and reports the missing ones (fetching
        # is LLM-gated via args={'enqueue': N}). The handler raises
        # InitError when the client-credentials env vars (ORCID_CLIENT_ID /
        # ORCID_CLIENT_SECRET) are missing, so the kind degrades to
        # disabled rather than blocking boot.
        from precis.handlers.orcid import OrcidHandler

        _gated(OrcidHandler)

        # Wikipedia — on-demand article fetch via the MediaWiki API.
        # No API key; httpx is the only requirement (declared in the
        # handler), so this always registers when httpx is available.
        from precis.handlers.wikipedia import WikipediaHandler

        _gated(WikipediaHandler)

        # News — RSS/Atom article fetch. Like web/wikipedia, httpx +
        # trafilatura are the only requirements (declared on the
        # handler); always registers when those are available. The
        # news_poll worker feeds it from the news_sources registry.
        from precis.handlers.news import NewsHandler

        _gated(NewsHandler)

        # Patent — EPO OPS. ``PatentHandler.spec.requires_env``
        # declares EPO_OPS_CLIENT_KEY / EPO_OPS_CLIENT_SECRET /
        # PRECIS_PATENT_RAW_ROOT, so the kind_gate skips the handler
        # cleanly when any of the three is missing (and surfaces it
        # on the cold-start ``Kinds unavailable:`` banner). The
        # ``epo_ops`` import is deferred inside the handler's
        # ``__init__`` so a missing optional dep doesn't take down
        # other handlers' boot path.
        #
        # Banner honesty (#40): the env-var gate alone wasn't enough —
        # if the operator set the EPO env trio but never installed
        # the ``[patent]`` extra, the cold-start banner said
        # "available" and the first call crashed with the lazy
        # ``import epo_ops`` failure. Probe for the package here so
        # the kind appears as deferred with an actionable reason
        # instead of pretending to work. We don't import — just
        # check for spec presence — to keep boot fast.
        import importlib.util

        if importlib.util.find_spec("epo_ops") is None:
            from precis.kind_gate import Loadability

            hub.loadabilities["patent"] = Loadability(
                kind="patent",
                loaded=False,
                reason=(
                    "missing python-epo-ops-client; "
                    "install with `pip install precis-mcp[patent]`"
                ),
            )
        else:
            from precis.handlers.patent import PatentHandler

            _gated(PatentHandler)

        # EDGAR — SEC filings. ``EdgarHandler.spec.requires_env`` declares
        # PRECIS_EDGAR_USER_AGENT / PRECIS_EDGAR_RAW_ROOT, so the kind_gate
        # skips the handler cleanly when either is missing (and surfaces it
        # on the cold-start ``Kinds unavailable:`` banner). Unlike patent,
        # the SEC APIs need no credentials and the HTTP dep (``httpx``) is
        # already a top-level dep (web / news), so no package probe is
        # needed — the env gate alone is honest here.
        from precis.handlers.edgar import EdgarHandler

        _gated(EdgarHandler)

    # Third-party plugins load last. See ``docs/user-facing/plugin-authoring.md``
    # and :func:`_load_plugins` for the contract and failure modes.
    # Built-ins win on kind-name collisions because they register
    # first; a plugin attempting to claim an already-registered kind
    # is logged and skipped.
    _load_plugins(hub)

    # Boot-time auto-upsert: every enabled hub kind lands in the
    # ``kinds`` table so the FK target stays in sync with the code
    # registry without a hand-maintained migration. See
    # ``store/_kinds_ops.py`` for the design note. Skipped on stateless
    # boots (no store): nothing to upsert against.
    if store is not None:
        try:
            from precis.store._kinds_ops import boot_process_identity

            specs = hub.kind_specs()
            n = store.upsert_kinds(specs)
            host, process = boot_process_identity()
            store.upsert_kind_providers(specs, host=host, process=process)
            log.info(
                "precis dispatch boot: upserted %d kind row(s) into kinds table "
                "(host=%s process=%s)",
                n,
                host,
                process,
            )
        except Exception:
            # Boot-time upsert failure is non-fatal — a process can
            # still serve already-registered kinds; the operator sees
            # the error in logs and can ship a fix without the cluster
            # going dark.
            log.exception("precis dispatch boot: kinds upsert failed (non-fatal)")

    log.info(
        "precis dispatch boot: %d kinds live: %s",
        len(hub.kinds),
        sorted(hub.kinds),
    )
    return hub


__all__ = [
    "PLUGIN_GROUP",
    "Ability",
    "AbilityKey",
    "DuplicateRegistration",
    "Hub",
    "InitError",
    "boot",
]
