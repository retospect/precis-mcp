"""Handler registry — maps schemes and file extensions to handlers.

Discovery order:
  1. Built-in handlers (WordHandler, TexHandler, PaperHandler)
  2. ``precis.plugins`` entry points (new — auto-discovers pip-installed plugins)
  3. ``precis.schemes`` / ``precis.file_types`` entry points (legacy compat)

Plugins can be disabled via ``PRECIS_DISABLE_PLUGINS=name1,name2`` env var.

**Plugin protocol v2 (Phase 0)**:

- ``KINDS: dict[str, RegisteredKind]`` — agent-facing kind enum, built from
  plugin ``KindSpec`` declarations (or synthesised defaults per scheme).
- ``ALIASES: dict[str, str]`` — alias → canonical kind redirect (resolved
  at URI parse, hidden from the enum).
- ``invoke_handler()`` — exception-isolated wrapper producing unified
  ``Result`` envelopes with ``_format_error()`` error strings, hint
  aggregation, and the response-footer cost line.

See docs/plugin-architecture.md §6, §10, §11.
"""

from __future__ import annotations

import logging
import os
import threading
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from precis.protocol import (
    GRIPE_HINT_CODES,
    PLUGIN_PROTOCOL_VERSION,
    VERBS,
    CallContext,
    ErrorCode,
    Handler,
    HintContext,
    KindSpec,
    Plugin,
    PrecisError,
    Result,
)

log = logging.getLogger(__name__)

# scheme → Handler class (for non-file schemes like paper:)
SCHEMES: dict[str, type[Handler]] = {}

# extension → Handler class (for file: scheme, dispatched by extension)
FILE_TYPES: dict[str, type[Handler]] = {}

# name → Plugin (all registered plugins)
PLUGINS: dict[str, Plugin] = {}

# corpus_id → Plugin (for write_policy enforcement and corpus dispatch)
CORPUS_PLUGINS: dict[str, Plugin] = {}

# scheme → cached Handler instance (populated lazily by resolve())
#
# Handlers are reused across calls rather than reconstructed per ``resolve()``.
# This lets handlers hold warm resources (DB pools, HTTP clients, parsed
# indexes) in ``self`` without paying the construction cost per tool call.
# FastMCP dispatches sync tools on a thread pool, but psycopg_pool and httpx
# ``Client`` are thread-safe, so a single instance per process is correct.
#
# The lock guards the first-resolve() race where two threads could both
# observe ``None`` for a scheme and both construct a handler (leaking one).
# It is NOT held during handler method calls — only around construction.
_SCHEME_INSTANCES: dict[str, Handler] = {}
_FILE_TYPE_INSTANCES: dict[str, Handler] = {}
_INSTANCE_LOCK = threading.Lock()

_discovered = False


# ---------------------------------------------------------------------------
# Plugin protocol v2 — KINDS + ALIASES registry
# ---------------------------------------------------------------------------


@dataclass
class RegisteredKind:
    """Registry entry for a single agent-facing kind.

    Wraps the ``KindSpec`` with the handler class that serves it and a
    back-reference to the owning plugin.  ``KINDS[kind_name]`` returns one
    of these.
    """

    spec: KindSpec
    handler_cls: type[Handler]
    plugin_name: str

    @property
    def name(self) -> str:
        return self.spec.name

    @property
    def description(self) -> str:
        return self.spec.description


#: Canonical kind name → RegisteredKind.  Populated at discovery time.
KINDS: dict[str, RegisteredKind] = {}

#: Alias kind name → canonical kind name.  E.g. ``"perplexity"`` → ``"web"``.
#: Aliases resolve at URI parse; they are hidden from the tool enum (§6.8).
ALIASES: dict[str, str] = {}

#: Accumulated non-fatal startup messages surfaced by ``get(id='/stats')``
#: and (when the shipper is wired) emitted as `startup_warning` log events.
#: Kept as an ordered list so the ``/stats`` view can render them in the
#: order they were produced.
STARTUP_WARNINGS: list[str] = []

#: Active ``PRECIS_KINDS`` mask: ``{kind_name: frozenset(verbs)}``, or
#: ``None`` when no mask is set (expose all kinds with all verbs).
#: Written once at server startup via :func:`set_kinds_mask`; read by
#: :func:`visible_kinds` on every tool-schema build.
_KINDS_MASK: dict[str, frozenset[str]] | None = None


class RegistryError(RuntimeError):
    """Fatal registry invariant violation — caller should exit(2).

    Raised for kind-name collisions between plugins (§6.9) and any other
    startup-time contract breaks that cannot be recovered from.  Callers
    in ``server.main()`` print ``str(err)`` to stderr and ``sys.exit(2)``.
    """


# ---------------------------------------------------------------------------
# Session cost accumulator (Phase 2)
# ---------------------------------------------------------------------------


@dataclass
class CallStats:
    """Running counters for a single kind across a server session.

    Populated by ``invoke_handler()`` on every successful or failed call.
    Agent reads these via the ``stats()`` MCP tool (§8).

    Fields:
        calls: Total invocations (success + error).
        errors: Subset of ``calls`` that returned an error Result.
        last_cost: The most recent ``cost_hint`` string the handler
            produced — "free" for the default-fallback case, or a
            vendor-specific string like ``"~$0.002/call"``.  Kept as a
            string so the struct is transport-agnostic; machine-parseable
            cost totals are a Future enhancement (§19).
    """

    calls: int = 0
    errors: int = 0
    last_cost: str = "free"


#: Kind name → CallStats.  Process-local in-memory counter.  Never crosses
#: the stdio boundary directly; the agent-facing view goes through
#: ``stats()`` in ``server.py``.
SESSION_STATS: dict[str, CallStats] = {}


def record_call(kind: str, cost_hint: str, *, errored: bool = False) -> None:
    """Increment the session counters for ``kind``.

    Called by ``invoke_handler()`` at the end of every invocation.
    Deduplicated entries for the same kind share a single ``CallStats``;
    ``cost_hint`` overwrites ``last_cost`` so the agent always sees the
    most recent cost string for that kind.

    Parameters:
        kind: Canonical kind name.  Alias-resolution is the caller's job.
        cost_hint: The resolved cost string (never None by the time we
            get here — ``invoke_handler`` runs the three-level fallback).
        errored: Whether the call returned an error result.  Counts
            toward ``calls`` either way; additionally bumps ``errors``.
    """
    stats = SESSION_STATS.get(kind)
    if stats is None:
        stats = CallStats()
        SESSION_STATS[kind] = stats
    stats.calls += 1
    if errored:
        stats.errors += 1
    stats.last_cost = cost_hint or "free"


def get_session_stats() -> dict[str, CallStats]:
    """Return a shallow copy of the session-stats dict.

    Consumers (e.g. the ``stats()`` MCP tool) should not mutate the
    returned dict; the copy keeps internal state insulated.
    """
    return dict(SESSION_STATS)


def clear_session_stats() -> None:
    """Reset all session counters.  Test helper; not exposed to agents."""
    SESSION_STATS.clear()


def cost_hint_for(kind: str, per_call: str | None) -> str:
    """Resolve the final ``cost_hint`` string to show the agent.

    Three-level fallback (Phase 2 §11 / §13):

    1. ``per_call`` — what ``Handler.cost_of(ctx)`` just returned for this
       specific call.  A handler that knows the cost (e.g. Perplexity
       token count → USD) returns a concrete string here.
    2. ``KindSpec.cost_hint`` — the static declaration on the registered
       kind.  Most handlers leave ``cost_of`` as the no-op default and
       rely on this.
    3. Default ``"free"`` — so the response footer is always present,
       including for local/zero-cost kinds (§11 "always-on footer").
    """
    if per_call:
        return per_call
    registered = KINDS.get(kind)
    if registered is not None and registered.spec.cost_hint:
        return registered.spec.cost_hint
    return "free"


# ---------------------------------------------------------------------------
# Default KindSpec synthesis for v1 plugins (no declared kinds)
# ---------------------------------------------------------------------------


def _synthesise_kind_specs(plugin: Plugin) -> list[KindSpec]:
    """Build default KindSpecs for a v1 plugin that didn't declare any.

    Rules:
    - One KindSpec per scheme in ``plugin.schemes``.  E.g. a plugin that
      registers schemes ``["paper", "doi", "arxiv"]`` gets three kinds:
      ``paper`` as canonical, ``doi`` and ``arxiv`` as aliases of ``paper``
      (the first scheme is the canonical).
    - File-type-only plugins (word, tex, markdown, plaintext) do not
      synthesise kinds — they share the pseudo-kind ``doc`` dispatched by
      extension.  These plugins return ``[]`` here.
    - Description is derived from the handler class's ``__doc__`` (first
      line) or falls back to a generic string.

    This keeps v1 plugins working through Phase 0 without any code changes.
    """
    if not plugin.schemes:
        return []
    canonical = plugin.schemes[0]
    aliases = list(plugin.schemes[1:])
    desc = _handler_description(plugin.handler_cls) or f"{canonical} resources"
    return [
        KindSpec(
            name=canonical,
            description=desc,
            aliases=aliases,
        )
    ]


def _handler_description(cls: type[Handler]) -> str:
    """Extract the first doc-string line from a handler class, if any."""
    doc = (cls.__doc__ or "").strip()
    if not doc:
        return ""
    return doc.splitlines()[0].strip()


def _is_allowed(name: str) -> bool:
    """Check if a plugin name passes the allow/deny filter.

    PRECIS_PLUGINS (allowlist, preferred):
        Comma-separated plugin names. Only these are loaded.
        Unset or empty = no allowlist filtering.

    PRECIS_DISABLE_PLUGINS (denylist, legacy):
        Ignored when PRECIS_PLUGINS is set.
        Comma-separated names to skip.

    Neither set = allow all.
    """
    allow_raw = os.environ.get("PRECIS_PLUGINS", "")
    if allow_raw.strip():
        allowed = {s.strip() for s in allow_raw.split(",") if s.strip()}
        return name in allowed
    deny_raw = os.environ.get("PRECIS_DISABLE_PLUGINS", "")
    if deny_raw.strip():
        denied = {s.strip() for s in deny_raw.split(",") if s.strip()}
        return name not in denied
    return True  # no filtering


def _register_plugin(plugin: Plugin) -> None:
    """Register a single plugin's schemes, file_types, kinds, corpus mapping.

    **Plugin protocol v2:** also populates ``KINDS`` and ``ALIASES`` from
    the plugin's declared ``kinds``, or from default specs synthesised per
    scheme when the plugin doesn't declare any.  Kind name collisions
    across plugins are logged at warn level (Phase 1 will make these
    fatal per §10.1 / §6.9).
    """
    # Protocol version check — refuse to load mismatched majors.
    plugin_major = plugin.protocol_version.split(".")[0]
    current_major = PLUGIN_PROTOCOL_VERSION.split(".")[0]
    if plugin_major != current_major:
        log.error(
            "Plugin '%s' declares protocol v%s but precis speaks v%s — skipping",
            plugin.name,
            plugin.protocol_version,
            PLUGIN_PROTOCOL_VERSION,
        )
        return

    # Dry-run collision check — a failed plugin must leave NO trace, so we
    # validate every declared kind before touching any registry state
    # (§6.9 "aborted to avoid a silent winner").
    kind_specs = list(plugin.kinds) if plugin.kinds else _synthesise_kind_specs(plugin)
    for spec in kind_specs:
        if spec.name in KINDS:
            existing = KINDS[spec.name]
            raise RegistryError(
                f"Kind '{spec.name}' is declared by two plugins: "
                f"'{existing.plugin_name}' (first) and '{plugin.name}' "
                f"(second). Rename one or gate them with PRECIS_PLUGINS. "
                f"Startup aborted to avoid a silent winner."
            )

    # All clear — commit plugin registration.
    PLUGINS[plugin.name] = plugin
    for scheme in plugin.schemes:
        SCHEMES[scheme] = plugin.handler_cls
    for ext in plugin.file_types:
        FILE_TYPES[ext] = plugin.handler_cls
    if plugin.corpus_id:
        CORPUS_PLUGINS[plugin.corpus_id] = plugin

    for spec in kind_specs:
        KINDS[spec.name] = RegisteredKind(
            spec=spec,
            handler_cls=plugin.handler_cls,
            plugin_name=plugin.name,
        )
        for alias in spec.aliases:
            if alias in KINDS:
                log.warning(
                    "Alias '%s' of kind '%s' clashes with registered kind; "
                    "skipping alias",
                    alias,
                    spec.name,
                )
                continue
            if alias in ALIASES and ALIASES[alias] != spec.name:
                log.warning(
                    "Alias '%s' already maps to '%s'; not overriding with '%s'",
                    alias,
                    ALIASES[alias],
                    spec.name,
                )
                continue
            ALIASES[alias] = spec.name

    log.debug(
        "Registered plugin '%s': schemes=%s file_types=%s corpus=%s kinds=%s",
        plugin.name,
        plugin.schemes,
        plugin.file_types,
        plugin.corpus_id,
        [s.name for s in kind_specs],
    )


def _register_builtins() -> None:
    """Register built-in handlers as Plugin objects.

    Each builtin is gated by ``_is_allowed(name)`` so that
    ``PRECIS_PLUGINS`` and ``PRECIS_DISABLE_PLUGINS`` apply
    to builtins too, not just entry-point plugins.
    """
    if _is_allowed("word"):
        try:
            from precis.handlers.word import WordHandler

            _register_plugin(
                Plugin(
                    name="word",
                    handler_cls=WordHandler,
                    file_types=[".docx"],
                )
            )
        except ImportError:
            log.debug("WordHandler not available (missing python-docx?)")

    if _is_allowed("tex"):
        try:
            from precis.handlers.tex import TexHandler

            _register_plugin(
                Plugin(
                    name="tex",
                    handler_cls=TexHandler,
                    file_types=[".tex"],
                )
            )
        except ImportError:
            log.debug("TexHandler not available")

    if _is_allowed("markdown"):
        try:
            from precis.handlers.markdown import MarkdownHandler

            _register_plugin(
                Plugin(
                    name="markdown",
                    handler_cls=MarkdownHandler,
                    file_types=[".md", ".markdown"],
                )
            )
        except ImportError:
            log.debug("MarkdownHandler not available")

    if _is_allowed("plaintext"):
        try:
            from precis.handlers.plaintext import PlainTextHandler

            _register_plugin(
                Plugin(
                    name="plaintext",
                    handler_cls=PlainTextHandler,
                    file_types=[".txt", ".text"],
                )
            )
        except ImportError:
            log.debug("PlainTextHandler not available")

    if _is_allowed("papers"):
        try:
            from precis.handlers.paper import PaperHandler

            _register_plugin(
                Plugin(
                    name="papers",
                    handler_cls=PaperHandler,
                    schemes=["paper", "doi", "arxiv", "pmid", "pmcid", "isbn", "issn"],
                    corpus_id="papers",
                    write_policy="ingestion",
                    kinds=[
                        KindSpec(
                            name="paper",
                            description=(
                                "Immutable academic corpus — chunks, figures, "
                                "citations.  Identified by slug, DOI, arXiv id, "
                                "PubMed id, PMCID, ISBN, or ISSN (see §13.5 / "
                                "Phase 5)."
                            ),
                            aliases=[
                                "doi",
                                "arxiv",
                                "pmid",
                                "pmcid",
                                "isbn",
                                "issn",
                            ],
                            cost_hint="free",
                            examples=[
                                "get(id='paper:wang2020state')",
                                "get(id='paper:10.1021/jacs.2c01234')",
                                "get(id='paper:arxiv:2508.20254')",
                                "search(query='anion exchange membranes', type='paper')",
                                "get(id='paper:wang2020state/fig/3')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("PaperHandler not available (missing acatome-store?)")

    if _is_allowed("todos"):
        try:
            from precis.handlers.todo import TodoHandler

            _register_plugin(
                Plugin(
                    name="todos",
                    handler_cls=TodoHandler,
                    schemes=["todo"],
                    corpus_id="todos",
                    write_policy="direct",
                    kinds=[
                        KindSpec(
                            name="todo",
                            description=(
                                "Agent-owned task list — integer ids, "
                                "state machine, priority, due dates."
                            ),
                            cost_hint="free",
                            examples=[
                                "get(id='todo:/open')",
                                "get(id='todo:42')",
                                "put(id='todo:', text='Review PR', mode='append')",
                                "put(id='todo:42', mode='done')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("TodoHandler not available (missing acatome-store?)")

    if _is_allowed("flashcards"):
        try:
            from precis.handlers.flashcard import FlashcardHandler

            _register_plugin(
                Plugin(
                    name="flashcards",
                    handler_cls=FlashcardHandler,
                    schemes=["fc"],
                    corpus_id="flashcards",
                    write_policy="direct",
                    kinds=[
                        KindSpec(
                            name="flashcard",
                            description=(
                                "Spaced-repetition deck — Q/A pairs with "
                                "SM-2 scheduling. Integer ids."
                            ),
                            aliases=["fc"],
                            cost_hint="free",
                            examples=[
                                "get(id='fc:/due')",
                                "get(id='fc:/recent')",
                                "put(id='fc:', text='Q: …\\nA: …', mode='append')",
                                "put(id='fc:17', mode='rate', grade=4)",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("FlashcardHandler not available (missing acatome-store?)")

    # ── Phase 12a: quest kind (paper-request lifecycle) ──────────────
    # Read-only in 12a (writes land in 12b).  Gated on ImportError so a
    # lean install (no ``[quest]`` extra) doesn't see the kind.  Further
    # gated on DATABASE_URL at runtime — the handler surfaces an
    # UPSTREAM_ERROR if PG is unreachable.

    if _is_allowed("quest"):
        try:
            from precis.handlers.quest import QuestHandler

            _register_plugin(
                Plugin(
                    name="quest",
                    handler_cls=QuestHandler,
                    schemes=["quest"],
                    write_policy="direct",
                    kinds=[
                        KindSpec(
                            name="quest",
                            description=(
                                "Paper-request queue — submit a DOI / "
                                "arXiv / title, resolver looks it up, "
                                "runner fetches, extractor ingests.  "
                                "Read-only in 12a."
                            ),
                            cost_hint="free",
                            examples=[
                                "get(id='quest:/recent')",
                                "get(id='quest:/needs-user')",
                                "get(id='quest:/agent/asa')",
                                "get(id='quest:<short-uuid>/candidates')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("QuestHandler not available — install precis-mcp[quest]")

    # ── Phase 12b: skill kind (filesystem-native) ────────────────────
    # Always available — reads SKILL.md directories from configured
    # scan paths.  No PG, no corpus, no deps.

    if _is_allowed("skill"):
        from precis.handlers.skill import SkillHandler

        _register_plugin(
            Plugin(
                name="skill",
                handler_cls=SkillHandler,
                schemes=["skill"],
                kinds=[
                    KindSpec(
                        name="skill",
                        description=(
                            "Agent Skills — SKILL.md directories with "
                            "YAML frontmatter.  Workflows, recipes, "
                            "domain knowledge.  Filesystem-native; "
                            "interops with Claude Code / Cursor skills."
                        ),
                        cost_hint="free",
                        examples=[
                            "get(id='skill:/')             — list all",
                            "get(id='skill:find-paper')    — render a skill",
                            "get(id='skill:/recent')       — recently changed",
                            "get(id='skill:/kind/quest')   — skills for a kind",
                            "search(type='skill', query='acquire paper')",
                        ],
                    )
                ],
            )
        )

    # ── Phase 6: journal kinds (memory, conversation) ────────────────
    # Both are state-backed via acatome-store (corpus: memories /
    # conversations).  Gated on ImportError so a store-less install
    # still serves the stateless kinds.

    if _is_allowed("memories"):
        try:
            from precis.handlers.memory import MemoryHandler

            _register_plugin(
                Plugin(
                    name="memories",
                    handler_cls=MemoryHandler,
                    schemes=["memory"],
                    corpus_id="memories",
                    write_policy="direct",
                    kinds=[
                        KindSpec(
                            name="memory",
                            description=(
                                "Long-term agent memory drawers — verbatim "
                                "content, slug-based ids, tag filtering, "
                                "pgvector search. /recent + /tags views."
                            ),
                            cost_hint="free",
                            examples=[
                                "get(type='memory', id='/recent')",
                                "put(type='memory', text='…', title='…')",
                                "search(query='…', type='memory')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("MemoryHandler not available (missing acatome-store?)")

    if _is_allowed("conversations"):
        try:
            from precis.handlers.conversation import ConversationHandler

            _register_plugin(
                Plugin(
                    name="conversations",
                    handler_cls=ConversationHandler,
                    schemes=["conversation"],
                    corpus_id="conversations",
                    write_policy="direct",
                    kinds=[
                        KindSpec(
                            name="conversation",
                            description=(
                                "Session-level transcripts — turn-per-block, "
                                "streamable via put(mode='append'). /recent "
                                "and /session views."
                            ),
                            aliases=["conv"],
                            cost_hint="free",
                            examples=[
                                "get(type='conversation', id='/recent')",
                                "put(id='conv:2026-04-21-asa', text='…', mode='append')",
                                "get(id='conv:2026-04-21-asa/session')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("ConversationHandler not available (missing acatome-store?)")

    # ── Phase 4: external stateless handlers ────────────────────────
    # Each gated on ImportError (missing pip extra) + ``KindSpec.requires``
    # env vars (so ``visible_kinds`` hides them from the agent when the
    # credential is absent — §6.2, §13).

    if _is_allowed("math"):
        try:
            from precis.handlers.math import MathHandler

            _register_plugin(
                Plugin(
                    name="math",
                    handler_cls=MathHandler,
                    schemes=["math"],
                    kinds=[
                        KindSpec(
                            name="math",
                            description=(
                                "Wolfram Alpha compute — math, science "
                                "facts, unit conversions, data lookups. "
                                "Requires WOLFRAM_APP_ID."
                            ),
                            requires=["WOLFRAM_APP_ID"],
                            cost_hint="~$0.0001/call",
                            examples=[
                                "get(type='math', id='integrate sin(x)cos(x)')",
                                "get(type='math', id='population of Ireland')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("MathHandler not available (missing wolframalpha?)")

    if _is_allowed("youtube"):
        try:
            from precis.handlers.youtube import YouTubeHandler

            _register_plugin(
                Plugin(
                    name="youtube",
                    handler_cls=YouTubeHandler,
                    schemes=["youtube"],
                    kinds=[
                        KindSpec(
                            name="youtube",
                            description=(
                                "YouTube video transcripts — fetch via "
                                "youtube-transcript-api.  No auth.  Accepts "
                                "video id, share URL, shorts, embed, or "
                                "live URL."
                            ),
                            cost_hint="free",
                            examples=[
                                "get(type='youtube', id='79-bApI3GIU')",
                                "get(id='youtube:79-bApI3GIU/languages')",
                            ],
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("YouTubeHandler not available (missing youtube-transcript-api?)")

    # ── Phase 3: Perplexity Sonar — web / think / research ──────────
    # Three kinds sharing the _WebBase class family but with different
    # models, timeouts, and cost hints.  All three require
    # PERPLEXITY_API_KEY and httpx (from the [external] extra).

    _PERPLEXITY_COMMON = {
        "requires": ["PERPLEXITY_API_KEY"],
    }

    if _is_allowed("web"):
        try:
            from precis.handlers.web import WebHandler

            _register_plugin(
                Plugin(
                    name="web",
                    handler_cls=WebHandler,
                    schemes=["web"],
                    kinds=[
                        KindSpec(
                            name="web",
                            description=(
                                "Perplexity Sonar web search — fast factual "
                                "answers with inline citations (2–5 s). Use "
                                "for definitions, current events, quick "
                                "lookups.  Requires PERPLEXITY_API_KEY."
                            ),
                            cost_hint="~$0.001/call",
                            examples=[
                                "get(type='web', id='current CEO of Anthropic')",
                                "search(query='Mars rover findings 2025', type='web')",
                            ],
                            **_PERPLEXITY_COMMON,
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("WebHandler not available (missing httpx?)")

    if _is_allowed("think"):
        try:
            from precis.handlers.web import ThinkHandler

            _register_plugin(
                Plugin(
                    name="think",
                    handler_cls=ThinkHandler,
                    schemes=["think"],
                    kinds=[
                        KindSpec(
                            name="think",
                            description=(
                                "Perplexity Sonar Reasoning Pro — detailed "
                                "analysis with explicit reasoning (5–30 s). "
                                "Use for comparisons, nuanced questions, "
                                "multi-source synthesis.  Requires "
                                "PERPLEXITY_API_KEY."
                            ),
                            cost_hint="~$0.005/call",
                            examples=[
                                "get(type='think', id='compare GPT-4o vs Claude 4 for code review')",
                            ],
                            **_PERPLEXITY_COMMON,
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("ThinkHandler not available (missing httpx?)")

    if _is_allowed("research"):
        try:
            from precis.handlers.web import ResearchHandler

            _register_plugin(
                Plugin(
                    name="research",
                    handler_cls=ResearchHandler,
                    schemes=["research"],
                    kinds=[
                        KindSpec(
                            name="research",
                            description=(
                                "Perplexity Sonar Deep Research — multi-step "
                                "investigation, 2–10 MIN per call, ~$0.50 "
                                "each.  Use only when the question justifies "
                                "the wait and spend.  Requires "
                                "PERPLEXITY_API_KEY."
                            ),
                            cost_hint="~$0.50/call",
                            examples=[
                                "get(type='research', id='landscape of post-quantum signature schemes')",
                            ],
                            **_PERPLEXITY_COMMON,
                        )
                    ],
                )
            )
        except ImportError:
            log.debug("ResearchHandler not available (missing httpx?)")


def _discover() -> None:
    """Load built-in handlers and entry-point plugins (once).

    Discovery sources (in order):
      1. Built-in handlers
      2. ``precis.plugins`` entry points (each returns a Plugin instance)
      3. ``precis.schemes`` / ``precis.file_types`` (legacy compat)
    """
    global _discovered
    if _discovered:
        return
    _discovered = True

    _register_builtins()

    # New plugin entry points
    for ep in entry_points(group="precis.plugins"):
        if not _is_allowed(ep.name):
            log.info(
                "Plugin '%s' filtered by PRECIS_PLUGINS / PRECIS_DISABLE_PLUGINS",
                ep.name,
            )
            continue
        try:
            obj = ep.load()
            # Entry point can be a Plugin instance, a Plugin class, or a callable
            if isinstance(obj, Plugin):
                plugin = obj
            elif (isinstance(obj, type) and issubclass(obj, Plugin)) or callable(obj):
                plugin = obj()
            else:
                raise TypeError(
                    f"precis.plugins entry point '{ep.name}' must return a Plugin, "
                    f"got {type(obj).__name__}"
                )
            if not isinstance(plugin, Plugin):
                raise TypeError(
                    f"precis.plugins entry point '{ep.name}' callable returned "
                    f"{type(plugin).__name__}, expected Plugin"
                )
            _register_plugin(plugin)
        except Exception:
            log.warning(
                "Failed to load plugin '%s'",
                ep.name,
                exc_info=True,
            )

    # Legacy entry points (backward compat)
    for ep in entry_points(group="precis.schemes"):
        if ep.name not in SCHEMES:
            try:
                cls = ep.load()
                SCHEMES[ep.name] = cls
                log.debug("Legacy scheme %s → %s", ep.name, cls.__name__)
            except Exception:
                log.warning("Failed to load scheme plugin: %s", ep.name, exc_info=True)

    for ep in entry_points(group="precis.file_types"):
        if ep.name not in FILE_TYPES:
            try:
                cls = ep.load()
                FILE_TYPES[ep.name] = cls
                log.debug("Legacy file type %s → %s", ep.name, cls.__name__)
            except Exception:
                log.warning(
                    "Failed to load file_type plugin: %s", ep.name, exc_info=True
                )


def register_scheme(name: str, handler_cls: type[Handler]) -> None:
    """Register a scheme handler programmatically."""
    SCHEMES[name] = handler_cls


def register_file_type(ext: str, handler_cls: type[Handler]) -> None:
    """Register a file extension handler programmatically."""
    FILE_TYPES[ext] = handler_cls


def register_plugin(plugin: Plugin) -> None:
    """Register a plugin programmatically (for testing or manual setup)."""
    _register_plugin(plugin)


# ---------------------------------------------------------------------------
# Plugin protocol v2 — mask + visibility API (Phase 1)
# ---------------------------------------------------------------------------


def set_kinds_mask(mask: dict[str, frozenset[str]] | None) -> None:
    """Install the parsed ``PRECIS_KINDS`` mask.

    Server calls this once at startup with the result of
    :func:`precis.kinds_config.load_from_env`.  ``None`` means "no mask
    is active — expose every registered kind with every verb".

    Does **not** validate the mask against ``KINDS`` — that's the
    parser's job (see ``kinds_config.parse_precis_kinds`` with
    ``known_kinds=KINDS``).  Unknown kind names in the mask are a no-op
    at visibility-check time.
    """
    global _KINDS_MASK
    _KINDS_MASK = dict(mask) if mask is not None else None


def clear_kinds_mask() -> None:
    """Drop the active mask.  Used by tests to reset between cases."""
    global _KINDS_MASK
    _KINDS_MASK = None


def get_kinds_mask() -> dict[str, frozenset[str]] | None:
    """Return a copy of the active ``PRECIS_KINDS`` mask, or ``None``."""
    return dict(_KINDS_MASK) if _KINDS_MASK is not None else None


def resolve_alias(name: str) -> str:
    """Return the canonical kind name for ``name``, resolving one alias hop.

    - If ``name`` is already a canonical kind, it is returned unchanged.
    - If ``name`` is a known alias (``ALIASES[name]`` exists), the
      canonical target is returned.
    - Otherwise ``name`` is returned as-is; callers decide whether that
      is an error (tool-schema build) or a pass-through (URI parse).
    """
    _discover()
    return ALIASES.get(name, name)


def visible_kinds(verb: str) -> list[RegisteredKind]:
    """Return the kinds the agent should see for ``verb`` in tool schemas.

    Order of filters:

    1. Canonical kinds only (``KINDS.values()``).  Aliases are never in
       the enum (§6.8).
    2. If ``PRECIS_KINDS`` mask is set:
       - Kinds not in the mask are hidden.
       - Kinds in the mask but whose verb-set excludes ``verb`` are
         hidden for *this* verb (the kind may still appear in the enum
         of a different verb — see §13.2).
    3. Required-env check: any ``KindSpec.requires`` env vars that are
       unset hide the kind entirely.  A warning is appended to
       :data:`STARTUP_WARNINGS` **once per kind per process** so ``/stats``
       can explain why.
    4. Returned list is **sorted by kind name** so the agent always sees
       a stable enum ordering across runs.

    PG-reachability gating for state-backed kinds is separate (§6.2) and
    lives on the plugin / handler side — not here.
    """
    _discover()
    if verb not in VERBS:
        raise ValueError(
            f"visible_kinds: unknown verb {verb!r}; expected one of {sorted(VERBS)}"
        )

    out: list[RegisteredKind] = []
    for name, kind in KINDS.items():
        if _KINDS_MASK is not None:
            allowed_verbs = _KINDS_MASK.get(name)
            if allowed_verbs is None:
                continue  # kind not listed → hidden
            if verb not in allowed_verbs:
                continue  # kind listed but verb not whitelisted
        if not _env_satisfied(kind.spec):
            continue  # requires-env unmet → hidden
        out.append(kind)
    out.sort(key=lambda k: k.spec.name)
    return out


# Track which kinds have already emitted a "requires unmet" warning so the
# message shows up once per process rather than on every tool-schema rebuild.
_ENV_WARNED: set[str] = set()


def _env_satisfied(spec: KindSpec) -> bool:
    """Return True iff every env var in ``spec.requires`` is set (non-empty).

    Side effect: on the first rejection for a given kind, append a
    human-readable entry to :data:`STARTUP_WARNINGS` so ``/stats`` can
    surface the reason.
    """
    if not spec.requires:
        return True
    missing = [env for env in spec.requires if not os.environ.get(env, "").strip()]
    if not missing:
        return True
    if spec.name not in _ENV_WARNED:
        _ENV_WARNED.add(spec.name)
        add_startup_warning(
            f"kind '{spec.name}' hidden — missing env: {', '.join(missing)}"
        )
    return False


def add_startup_warning(msg: str) -> None:
    """Append a one-line warning for later surfacing in ``/stats``.

    Thin helper so the server, kinds-config loader, and registry all
    funnel through one entry point.  Warnings are deduped on append —
    repeated messages collapse to a single entry, which matters when the
    same kind is probed multiple times.
    """
    if msg and msg not in STARTUP_WARNINGS:
        STARTUP_WARNINGS.append(msg)


def clear_startup_warnings() -> None:
    """Empty the startup-warnings buffer.  Test helper only."""
    STARTUP_WARNINGS.clear()
    _ENV_WARNED.clear()


def get_plugin(name: str) -> Plugin | None:
    """Get a registered plugin by name."""
    _discover()
    return PLUGINS.get(name)


def get_corpus_plugin(corpus_id: str) -> Plugin | None:
    """Get the plugin responsible for a corpus."""
    _discover()
    return CORPUS_PLUGINS.get(corpus_id)


def list_plugins() -> list[Plugin]:
    """List all registered plugins."""
    _discover()
    return list(PLUGINS.values())


def resolve(scheme: str, path: str) -> Handler:
    """Return the appropriate handler instance for a scheme + path.

    For ``file:`` scheme, dispatches by file extension.
    For other schemes, dispatches by scheme name.

    Handler instances are memoized per scheme / extension so expensive
    ``__init__`` setup (DB pools, HTTP clients, on-disk indexes) runs
    once per process rather than once per tool call.

    Raises:
        PrecisError: If no handler is found for the scheme or file type.
    """
    _discover()

    if scheme == "file":
        ext = Path(path).suffix.lower()
        handler_cls = FILE_TYPES.get(ext)
        if not handler_cls:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                cause=f"no handler for {ext!r} files",
                options=sorted(FILE_TYPES.keys()) or None,
            )
        inst = _FILE_TYPE_INSTANCES.get(ext)
        if inst is None:
            with _INSTANCE_LOCK:
                inst = _FILE_TYPE_INSTANCES.get(ext)
                if inst is None:
                    inst = handler_cls()
                    _FILE_TYPE_INSTANCES[ext] = inst
        return inst

    handler_cls = SCHEMES.get(scheme)
    if not handler_cls:
        raise PrecisError(
            ErrorCode.KIND_UNKNOWN,
            cause=f"unknown scheme: {scheme!r}",
            options=["file", *sorted(SCHEMES.keys())],
        )
    inst = _SCHEME_INSTANCES.get(scheme)
    if inst is None:
        with _INSTANCE_LOCK:
            inst = _SCHEME_INSTANCES.get(scheme)
            if inst is None:
                inst = handler_cls()
                _SCHEME_INSTANCES[scheme] = inst
    return inst


def _reset_instance_cache() -> None:
    """Test hook — drop cached handler instances.

    Handlers are memoized across calls; tests that mutate handler state
    (or replace a handler wholesale) can call this to guarantee the next
    :func:`resolve` returns a fresh instance.
    """
    _SCHEME_INSTANCES.clear()
    _FILE_TYPE_INSTANCES.clear()


# ---------------------------------------------------------------------------
# Error formatting — unified multi-line shape per §11.2
# ---------------------------------------------------------------------------


_HINT_CAP = 5


def _enrich_error(
    exc: PrecisError,
    handler: Handler | None,
    ctx: CallContext,
) -> tuple[list[str], str]:
    """Derive ``(options, next)`` for an error, auto-filling when the handler
    didn't supply them.

    Contract: the handler's own values always win.  We only fill a field
    when the handler left it empty AND the error code has a sensible
    default drawn from the handler's declared vocabulary (``views``,
    ``allowed_modes``, ``writable``) or the registry.

    This lets handler raise-sites stay terse: they provide a concrete
    ``cause`` and the framework supplies the copy-pasteable remedy.
    """
    # Handler overrides take priority.
    options: list[str] = list(exc.options) if exc.options else []
    next_hint: str = exc.next

    code = exc.code
    kind = ctx.kind or (handler.scheme if handler else "")

    if not options:
        if code == ErrorCode.VIEW_UNKNOWN and handler is not None:
            # ``views`` is either a dict (view → method) for RefHandler
            # subclasses or a set (legacy for file/math/web/youtube) — iterate
            # keys either way.
            views = getattr(handler, "views", None) or ()
            all_views = sorted({f"/{v}" for v in views})
            if all_views:
                options = all_views
        elif code == ErrorCode.MODE_UNSUPPORTED and handler is not None:
            modes = getattr(handler, "allowed_modes", set()) or set()
            if modes:
                options = sorted(modes)
        elif code == ErrorCode.VERB_UNSUPPORTED and handler is not None:
            verbs = {"get", "search"}
            if getattr(handler, "writable", False):
                verbs.add("put")
            options = sorted(verbs)
        elif code == ErrorCode.KIND_UNKNOWN:
            # Fall back to the registered kinds list.
            options = sorted(KINDS.keys())

    if not next_hint:
        if code == ErrorCode.ID_NOT_FOUND and kind:
            # Does the handler expose a /recent view?  Prefer that.
            has_recent = handler is not None and "recent" in (
                getattr(handler, "views", set()) or set()
            )
            if has_recent:
                next_hint = f"get(type='{kind}', id='/recent') to list existing"
            else:
                next_hint = f"search(query='...', type='{kind}') to locate it"
        elif code == ErrorCode.ID_AMBIGUOUS:
            next_hint = "disambiguate with an exact slug or fully-qualified id"
        elif code == ErrorCode.ID_MALFORMED:
            next_hint = "ids look like '<scheme>:<slug>' or '<scheme>:<slug>›<block>'"
        elif code == ErrorCode.KIND_UNAVAILABLE:
            # If the handler declares an install_extra, suggest it.
            registered = KINDS.get(kind) if kind else None
            extra = getattr(registered, "install_extra", "") if registered else ""
            if extra:
                next_hint = f"install with: pip install precis-mcp[{extra}]"
            else:
                next_hint = "stats() shows which kinds are currently available"
        elif code == ErrorCode.KIND_UNKNOWN:
            next_hint = "stats() lists registered kinds"
        elif code == ErrorCode.READONLY and kind:
            next_hint = (
                "this kind is read-only; use put(type='memory', ...) to save content"
            )

    # Phase 12b v1.1: skill pointer on agent-confusion codes.  The handler
    # opts in by setting its class-level ``onboarding_skill`` attribute.
    # We only append when the error points at the agent's understanding
    # of the tool surface — ID_NOT_FOUND is "the ref is gone", not "you
    # don't know how to use this kind", so it gets a search hint instead.
    if handler is not None and code in (
        ErrorCode.PARAM_INVALID,
        ErrorCode.MODE_UNSUPPORTED,
        ErrorCode.VIEW_UNKNOWN,
    ):
        skill_slug = getattr(handler, "onboarding_skill", None)
        if skill_slug:
            pointer = f"see get(id='skill:{skill_slug}') for the workflow"
            next_hint = f"{next_hint}; {pointer}" if next_hint else pointer

    return options, next_hint


def _format_error(
    code: ErrorCode | str,
    ctx: CallContext,
    cause: str,
    options: list[str] | None = None,
    next_hint: str = "",
) -> str:
    """Produce the unified multi-line error string.

    Shape (§11.2)::

        ERROR [<code>]: <one-line summary>
          where: type='<type>' verb='<verb>' id='<id_if_any>'
          cause: <concrete reason>
          options: <comma-separated valid alternatives>
          next: <one concrete action>

    ``cause`` is the only required field beyond ``code``.  Omitted fields
    are dropped from the output (no empty lines).  For errors that aren't
    the agent's fault (``GRIPE_HINT_CODES``), a gripe next-hint is
    appended when ``next_hint`` is empty.
    """
    code_str = code.value if isinstance(code, ErrorCode) else code
    # Build the single-line summary from the cause's first line.
    summary = (cause or code_str).splitlines()[0].strip()
    lines = [f"ERROR [{code_str}]: {summary}"]

    where_bits: list[str] = []
    if ctx.kind:
        where_bits.append(f"type='{ctx.kind}'")
    if ctx.verb:
        where_bits.append(f"verb='{ctx.verb}'")
    id_val = ctx.args.get("id") if ctx.args else None
    if id_val:
        where_bits.append(f"id='{id_val}'")
    if where_bits:
        lines.append(f"  where: {' '.join(where_bits)}")

    if cause:
        lines.append(f"  cause: {cause}")
    if options:
        lines.append(f"  options: {', '.join(options)}")

    # Gripe-next-hint for errors that aren't the agent's fault.
    is_gripe_code = (
        code in GRIPE_HINT_CODES
        if isinstance(code, ErrorCode)
        else code_str in {c.value for c in GRIPE_HINT_CODES}
    )
    if not next_hint and is_gripe_code:
        next_hint = (
            "if this error looks like a bug, gripe about it: "
            "put(type='gripe', text='<what you expected vs what happened>')"
        )
    if next_hint:
        lines.append(f"  next: {next_hint}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Hint aggregation
# ---------------------------------------------------------------------------


def _aggregate_hints(handler: Handler, result: Any, hint_ctx: HintContext) -> list[str]:
    """Collect handler hints, dedup, cap at ``_HINT_CAP``.

    Phase 0 scaffold: just calls ``handler.hints()`` and dedups preserving
    order.  Priority-based ranking (§11.4) lands in Phase 8.
    """
    try:
        raw = handler.hints(result, hint_ctx) or []
    except Exception:  # handler hints must never break the response
        log.exception("handler.hints() raised; dropping hint list")
        return []
    seen: set[str] = set()
    out: list[str] = []
    for hint in raw:
        if not isinstance(hint, str) or not hint.strip():
            continue
        if hint in seen:
            continue
        seen.add(hint)
        out.append(hint)
        if len(out) >= _HINT_CAP:
            break
    return out


# ---------------------------------------------------------------------------
# invoke_handler — exception-isolated wrapper (§11.1)
# ---------------------------------------------------------------------------


def invoke_handler(
    kind: str,
    verb: str,
    handler: Handler,
    handler_fn: Callable[[], Any],
    *,
    args: dict[str, Any] | None = None,
) -> Result:
    """Run ``handler_fn()`` with full exception isolation.

    Wraps a handler invocation so that:

    - ``PrecisError`` raised by the handler becomes a unified error string
      via ``_format_error()``.
    - Any other exception becomes ``ErrorCode.UNEXPECTED`` (logged with
      traceback; agent sees a concise ``<ExcType>: <msg>`` cause plus a
      gripe-next-hint).
    - Success results are wrapped with aggregated hints (``handler.hints``)
      and the cost-footer string (``handler.cost_of``).

    Parameters:
        kind: The agent-facing kind name (``"paper"``, ``"web"``, etc.).
        verb: One of ``"get" | "search" | "put" | "move"``.
        handler: The handler instance (used for ``hints``/``cost_of``/
            ``notifications`` hooks — not dispatched through here).
        handler_fn: Zero-arg callable that performs the actual work.  The
            caller wires in whatever call-site logic applies (``read()``,
            ``put()``, store lookups, etc.).  This keeps invoke_handler
            independent of v1-handler-specific dispatch signatures.
        args: Optional dict of call arguments, stashed in
            ``CallContext.args`` for use in error ``where:`` lines.

    Returns:
        A ``Result`` ready to be rendered for the agent.

    Note:
        Wired into ``server.py`` via the ``_dispatch`` helper — every
        tool response flows through here.  On ``PrecisError``, runs
        ``_enrich_error`` to auto-fill ``options=``/``next=`` from the
        handler's declared vocabulary before formatting.
    """
    ctx = CallContext(kind=kind, verb=verb, args=dict(args) if args else {})

    def _finalise_error(err_str: str) -> Result:
        """Shared tail for both error paths: record_call + build Result.

        Error calls still count toward session stats (so ``/stats`` shows
        error-rate alongside cost).  Cost uses the static-fallback path —
        we never asked the handler for a per-call cost because it crashed.
        """
        record_call(kind, cost_hint_for(kind, None), errored=True)
        return Result.err(err_str)

    try:
        raw = handler_fn()
    except PrecisError as exc:
        # Auto-fill options/next from the handler's vocabulary when the
        # raise-site didn't supply them.  Handler values always win.
        enriched_options, enriched_next = _enrich_error(exc, handler, ctx)
        return _finalise_error(
            _format_error(
                exc.code,
                ctx,
                cause=exc.cause or str(exc),
                options=enriched_options or None,
                next_hint=enriched_next,
            )
        )
    except Exception as exc:
        log.error(
            "Handler crash in %s.%s: %s\n%s",
            kind,
            verb,
            exc,
            traceback.format_exc(),
        )
        return _finalise_error(
            _format_error(
                ErrorCode.UNEXPECTED,
                ctx,
                cause=f"{type(exc).__name__}: {exc}",
            )
        )

    # Success path — compute hints and the three-level cost fallback.
    hint_ctx = HintContext.from_result(raw, ctx)
    hints = _aggregate_hints(handler, raw, hint_ctx)
    try:
        per_call_cost = handler.cost_of(ctx)
    except Exception:
        log.exception("handler.cost_of() raised; falling back to static hint")
        per_call_cost = None
    cost = cost_hint_for(kind, per_call_cost)
    record_call(kind, cost, errored=False)

    return Result.ok(raw, kind=kind, cost=cost, hints=hints)
