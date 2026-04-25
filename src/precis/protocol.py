"""Handler protocol, Node model, and shared types.

This module defines the abstract base class that every document handler must
implement, plus the Node/Path data structures shared across all handlers.

**Plugin protocol v2** (Phase 0 additions — see docs/plugin-architecture.md):

- ``PLUGIN_PROTOCOL_VERSION`` — bump when protocol changes in breaking ways.
- ``KindSpec`` — agent-facing capability declaration (name, description,
  aliases, required env vars, cost hint, examples).
- ``CallContext``, ``HintContext``, ``NotificationContext`` — per-call and
  per-session context passed to handler hooks.
- ``ErrorCode`` — frozen enum of the 16 standard error codes.
- ``PrecisError`` — now carries structured ``(code, cause, options, next)``
  fields for unified error formatting. Backward-compatible with
  ``PrecisError("just a message")`` callers (defaults to ``code=UNEXPECTED``).

These types are additive. Existing handlers keep working unchanged; new code
paths (``invoke_handler()`` wrapper, tool-schema generator, startup probes)
consume the new types as they land.
"""

from __future__ import annotations

import abc
import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, ClassVar

# ---------------------------------------------------------------------------
# Protocol version
# ---------------------------------------------------------------------------

#: Bumped when the plugin contract changes in a breaking way.  Plugins declare
#: the range of protocol versions they are compatible with; precis refuses to
#: load incompatible majors with a clean error.
PLUGIN_PROTOCOL_VERSION = "1"


#: The four agent-facing verbs — frozen.  Phase 1 consumers: the
#: ``PRECIS_KINDS`` parser (validates bracket verbs), the registry
#: (``visible_kinds(verb)``), and the server's tool wrappers.  Anything
#: emitted to the agent as a verb name must be one of these.
VERBS: frozenset[str] = frozenset({"search", "get", "put", "move"})


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ErrorCode(StrEnum):
    """Frozen catalogue of standard error codes.

    See docs/plugin-architecture.md §11.3 for the full catalogue and the
    agent-facing shape of each code.
    """

    KIND_UNKNOWN = "kind_unknown"
    KIND_UNAVAILABLE = "kind_unavailable"
    VERB_UNSUPPORTED = "verb_unsupported"
    VIEW_UNKNOWN = "view_unknown"
    MODE_UNSUPPORTED = "mode_unsupported"
    ID_NOT_FOUND = "id_not_found"
    ID_AMBIGUOUS = "id_ambiguous"
    ID_MALFORMED = "id_malformed"
    PARAM_INVALID = "param_invalid"
    READONLY = "readonly"
    DENIED = "denied"
    TIMEOUT = "timeout"
    RATE_LIMITED = "rate_limited"
    UPSTREAM_ERROR = "upstream_error"
    UNAVAILABLE = "unavailable"
    UNEXPECTED = "unexpected"


#: Codes that should surface a "gripe about it" next-hint when they fire
#: (i.e. errors that aren't the agent's fault).  See §11.2.
GRIPE_HINT_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.UNEXPECTED,
        ErrorCode.TIMEOUT,
        ErrorCode.UNAVAILABLE,
        ErrorCode.RATE_LIMITED,
        ErrorCode.UPSTREAM_ERROR,
    }
)


class PrecisError(Exception):
    """Structured error carrying code + cause + options + next hint.

    Every raise site supplies an :class:`ErrorCode` first.  ``cause``
    is the concrete one-sentence reason (lowercase, no period); the
    framework auto-fills ``options=`` and ``next=`` from the handler's
    declared vocabulary (``views``, ``allowed_modes``, ``writable``)
    via :func:`precis.registry._enrich_error`, so a typical raise site
    is just::

        raise PrecisError(
            ErrorCode.ID_NOT_FOUND,
            cause="paper 'wang2020state' not in corpus",
        )

    Pass ``options=`` / ``next=`` explicitly only when the default
    enrichment isn't good enough (e.g. priority enums that aren't on
    the handler vocabulary).  Handler values always win over
    auto-fill.

    The unified multi-line rendering (§11.2) is produced by
    :func:`precis.registry._format_error`.
    """

    def __init__(
        self,
        code: ErrorCode,
        cause: str = "",
        *,
        options: list[str] | None = None,
        next: str = "",
    ):
        if not isinstance(code, ErrorCode):
            raise TypeError(
                "PrecisError first argument must be an ErrorCode; "
                f"got {type(code).__name__}: {code!r}.  "
                "Use e.g. ErrorCode.ID_NOT_FOUND, ErrorCode.PARAM_INVALID, "
                "or ErrorCode.UNEXPECTED for unclassified failures."
            )
        self.code = code
        self.cause = cause
        self.options: list[str] = list(options) if options else []
        self.next: str = next
        super().__init__(self.cause or self.code.value)

    def format(self) -> str:
        """Legacy single-line error format: ``!! ERROR <cause>``."""
        return f"!! ERROR {self.cause or self.code.value}"


def extract_kwargs(
    kwargs: dict[str, Any],
    keys: tuple[str, ...],
    *,
    required: tuple[str, ...] = (),
    context: str = "",
) -> tuple[Any, ...]:
    """Validate and extract kwargs for a handler view/mode method.

    Given the variadic ``**kwargs`` a dispatch method receives, check that
    only declared keys were passed, that required ones are present, and
    return the values in ``keys`` order for tuple-unpacking at the call
    site.  Typical use at the top of a view method::

        def _read_wibble_view(self, store, ref, sel, sub, **kwargs):
            wibble, size = extract_kwargs(
                kwargs,
                ("wibble", "size"),
                required=("wibble",),
                context="wibble view",
            )

    Unknown kwargs raise :class:`PrecisError` with ``PARAM_INVALID`` and
    the allowed list in ``options=``.  Missing required kwargs raise the
    same code with the missing list in ``options=``.  Missing optional
    kwargs return ``None`` in the output tuple.
    """
    extra = set(kwargs) - set(keys)
    if extra:
        where = f" on {context}" if context else ""
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"unexpected kwarg(s){where}: {', '.join(sorted(extra))}",
            options=list(keys),
        )
    missing = set(required) - set(kwargs)
    if missing:
        where = f" on {context}" if context else ""
        raise PrecisError(
            ErrorCode.PARAM_INVALID,
            cause=f"required kwarg(s) missing{where}: {', '.join(sorted(missing))}",
            options=list(required),
        )
    return tuple(kwargs.get(k) for k in keys)


# ---------------------------------------------------------------------------
# Call / hint / notification context
# ---------------------------------------------------------------------------


@dataclass
class CallContext:
    """Per-invocation context threaded through the handler wrapper.

    Populated by ``invoke_handler()`` at the start of every call; passed to
    hooks that need to reason about what the agent just asked for.
    """

    kind: str = ""
    verb: str = ""  # 'get' | 'search' | 'put' | 'move'
    args: dict[str, Any] = field(default_factory=dict)
    started: float = field(default_factory=time.monotonic)

    @property
    def elapsed_s(self) -> float:
        """Seconds since the call started."""
        return time.monotonic() - self.started


@dataclass
class HintContext:
    """Derived context for ``Handler.hints()`` — result-count-driven heuristics.

    Built from the raw result + CallContext by ``invoke_handler()``; handlers
    use it to decide whether to emit context-aware hints (§5.3).
    """

    call: CallContext = field(default_factory=CallContext)
    result_count: int | None = None  # None when not meaningful (non-list results)

    @classmethod
    def from_result(cls, result: Any, call: CallContext) -> HintContext:
        """Derive a HintContext from a handler result and the CallContext."""
        count: int | None = None
        if isinstance(result, (list, tuple)):
            count = len(result)
        elif isinstance(result, dict):
            # Common shapes: {'items': [...]}, {'results': [...]}
            for key in ("items", "results", "hits"):
                if isinstance(result.get(key), (list, tuple)):
                    count = len(result[key])
                    break
        return cls(call=call, result_count=count)


@dataclass
class NotificationContext:
    """Session-start context for ``Handler.notifications()`` (§5.5).

    Carries agent-identifying data so handlers can filter — e.g. gripe
    notifications suppress themselves for non-admin agents.
    """

    agent_id: str = ""
    kinds_mask: frozenset[str] = frozenset()  # active kinds from PRECIS_KINDS


# ---------------------------------------------------------------------------
# Capability declaration
# ---------------------------------------------------------------------------


@dataclass
class KindSpec:
    """Agent-facing capability declaration for a single kind.

    Plugins declare one ``KindSpec`` per kind they expose.  Plugins that
    don't declare ``kinds`` get a default spec synthesised per scheme by the
    registry (see §16 Phase 0 and ``registry._synthesise_kind_spec``).

    Fields:
        name: Canonical enum value used in ``type='paper'`` etc.
        description: One-liner shown in the tool-schema enum docs.  May be
            either a literal string or a zero-argument callable that
            returns a string; callables are evaluated lazily every time
            the tool schema is built, so kinds like ``clock:`` can surface
            live data (current time + durations) in their enum entry.  The
            registry's ``RegisteredKind.description`` property handles
            both shapes uniformly.  Callables MUST be cheap and side-effect
            free — they run synchronously in the schema-build hot path.
        aliases: Legacy scheme names accepted at URI parse (hidden from the
            enum). E.g. ``['perplexity']`` redirects to ``'web'``.
        requires: Env vars that must be set for this kind to be available.
            Missing env → kind hidden from the enum with a startup warning.
        cost_hint: Freeform string, e.g. ``"~$0.002/call"`` or ``"free"`` or
            ``None`` to omit the cost line in the response footer.
        examples: Reserved for future per-kind example snippets in the tool
            description.  Currently unused.
    """

    name: str
    description: str | Callable[[], str]
    aliases: list[str] = field(default_factory=list)
    requires: list[str] = field(default_factory=list)
    cost_hint: str | None = None
    examples: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Result — unified response envelope from invoke_handler()
# ---------------------------------------------------------------------------


@dataclass
class Result:
    """Wrapper around either a successful handler result or a formatted error.

    Produced by ``registry.invoke_handler()``.  Carries the raw handler
    output plus response-footer data (cost, hints) on success, or a
    pre-formatted multi-line error string on failure.

    Use ``render()`` to flatten into the final string the MCP server
    returns to the agent.
    """

    success: bool
    data: Any = None
    kind: str = ""
    cost: str | None = None
    hints: list[str] = field(default_factory=list)
    error: str = ""

    @classmethod
    def ok(
        cls,
        data: Any,
        *,
        kind: str = "",
        cost: str | None = None,
        hints: list[str] | None = None,
    ) -> Result:
        """Construct a success result with optional cost footer and hints."""
        return cls(
            success=True,
            data=data,
            kind=kind,
            cost=cost,
            hints=list(hints) if hints else [],
        )

    @classmethod
    def err(
        cls,
        error: str,
        *,
        kind: str = "",
        cost: str | None = None,
    ) -> Result:
        """Construct an error result carrying a pre-formatted error string.

        ``cost`` is preserved through to ``render()`` so error responses
        emit the same ``[cost: …]`` footer as success responses.  This
        matters because:

        - Paid backends (Wolfram, Perplexity) charge for failed calls
          too — the agent still needs to see the cost line to keep
          session accounting honest.
        - Without the footer the agent has no signal that the call ran
          at all; it can't distinguish "the kind isn't installed" from
          "the kind ran but failed" by looking at the response shape.
        """
        return cls(success=False, error=error, kind=kind, cost=cost)

    def render(self) -> str:
        """Flatten to the final agent-visible string.

        On both success and failure: handler data (or error string),
        optional ``Hints:`` block (success only — error paths don't
        produce hints), then a ``[cost: …]`` footer when ``cost`` is
        set.  Matches the response-footer format in §11.

        Cost on error matters: failed Wolfram / Perplexity calls still
        charged the API.  Dropping the footer on errors silently
        misled the session-cost counter for any agent counting cost
        from response strings.
        """
        parts: list[str] = []
        if self.success:
            # Handler data — already a string for v1 handlers; pass through.
            if isinstance(self.data, str):
                parts.append(self.data)
            elif self.data is not None:
                parts.append(str(self.data))
            if self.hints:
                parts.append("")
                parts.append("Hints:")
                for h in self.hints:
                    parts.append(f"  - {h}")
        else:
            # Error path — the formatted multi-line ``ERROR [<code>]: …``
            # envelope is already in ``self.error``.
            parts.append(self.error)
        if self.cost:
            parts.append("")
            parts.append(f"[cost: {self.cost}]")
        return "\n".join(parts)


# ---------------------------------------------------------------------------
# Slugs
# ---------------------------------------------------------------------------

SLUG_CHARS = "0123456789ABCDEFGHJKLMNPQRSTUVWXYZ"
SLUG_LEN = 5


def make_slug(text: str) -> str:
    """Generate a 5-char base34 content slug from text."""
    h = int(hashlib.sha256(text.strip().encode()).hexdigest()[:8], 16)
    out = []
    for _ in range(SLUG_LEN):
        h, r = divmod(h, len(SLUG_CHARS))
        out.append(SLUG_CHARS[r])
    return "".join(out)


def resolve_slug(slug: str, slug_counts: dict[str, int]) -> str:
    """Resolve a slug with collision suffix if needed."""
    count = slug_counts.get(slug, 0) + 1
    slug_counts[slug] = count
    if count == 1:
        return slug
    return f"{slug}.{count}"


# ---------------------------------------------------------------------------
# Path — hierarchical position within a document
# ---------------------------------------------------------------------------

PATH_RE = re.compile(
    r"^S(\d+)(?:\.(\d+)(?:\.(\d+)(?:\.(\d+))?)?)?(?:([ptfeb¶])(\d+))?$"
)

_TYPE_DISPLAY = {"p": "¶"}
_TYPE_INTERNAL = {"¶": "p"}


@dataclass
class Path:
    """Positional heading-path ID: S1.2¶3."""

    h1: int = 0
    h2: int = 0
    h3: int = 0
    h4: int = 0
    node_type: str = ""  # p, t, f, e, b, or "" for headings
    index: int = 0  # 1-indexed within parent section

    def __str__(self) -> str:
        parts = [str(self.h1)]
        if self.h2 or self.h3 or self.h4:
            parts.append(str(self.h2))
        if self.h3 or self.h4:
            parts.append(str(self.h3))
        if self.h4:
            parts.append(str(self.h4))
        base = "S" + ".".join(parts)
        if self.node_type:
            display = _TYPE_DISPLAY.get(self.node_type, self.node_type)
            return f"{base}{display}{self.index}"
        return base

    @classmethod
    def parse(cls, s: str) -> Path:
        """Parse a path string like S1.2¶3."""
        m = PATH_RE.match(s)
        if not m:
            raise ValueError(f"Invalid path: {s!r}")
        h1 = int(m[1])
        h2 = int(m[2]) if m[2] is not None else 0
        h3 = int(m[3]) if m[3] is not None else 0
        h4 = int(m[4]) if m[4] is not None else 0
        node_type = _TYPE_INTERNAL.get(m[5], m[5]) if m[5] else ""
        index = int(m[6]) if m[6] else 0
        return cls(h1=h1, h2=h2, h3=h3, h4=h4, node_type=node_type, index=index)

    def is_heading(self) -> bool:
        return not self.node_type

    def heading_level(self) -> int:
        """Return the deepest non-zero heading level (1-4)."""
        if self.h4:
            return 4
        if self.h3:
            return 3
        if self.h2:
            return 2
        if self.h1:
            return 1
        return 0

    def is_child_of(self, other: Path) -> bool:
        """Check if this path is a child of another path."""
        if other.h1 and self.h1 != other.h1:
            return False
        if other.h2 and self.h2 != other.h2:
            return False
        if other.h3 and self.h3 != other.h3:
            return False
        if other.h4 and self.h4 != other.h4:
            return False
        return True


class PathCounter:
    """Tracks heading counters and assigns paths to nodes."""

    def __init__(self):
        self.h1 = 0
        self.h2 = 0
        self.h3 = 0
        self.h4 = 0
        self._counters: dict[str, int] = {}

    def _section_key(self) -> str:
        return f"{self.h1}.{self.h2}.{self.h3}.{self.h4}"

    def next_heading(self, level: int) -> Path:
        """Advance heading counter and return the path."""
        if level == 1:
            self.h1 += 1
            self.h2 = 0
            self.h3 = 0
            self.h4 = 0
        elif level == 2:
            self.h2 += 1
            self.h3 = 0
            self.h4 = 0
        elif level == 3:
            self.h3 += 1
            self.h4 = 0
        elif level == 4:
            self.h4 += 1
        self._counters = {}
        return Path(h1=self.h1, h2=self.h2, h3=self.h3, h4=self.h4)

    def next_child(self, node_type: str) -> Path:
        """Get the next child path for a given node type."""
        key = self._section_key()
        type_key = f"{key}:{node_type}"
        count = self._counters.get(type_key, 0) + 1
        self._counters[type_key] = count
        return Path(
            h1=self.h1,
            h2=self.h2,
            h3=self.h3,
            h4=self.h4,
            node_type=node_type,
            index=count,
        )


# ---------------------------------------------------------------------------
# Node — a single element in any document
# ---------------------------------------------------------------------------


@dataclass
class Node:
    """A document node — heading, paragraph, table, figure, or equation."""

    slug: str
    path: Path
    node_type: str  # h, p, t, f, e, b
    text: str  # full content
    index: int = 0  # sequential position in document (0-indexed)
    precis: str = ""  # compressed summary (RAKE keywords or enrichment)
    page: int = 0  # source page number (if applicable)
    style: str = ""  # DOCX style name or LaTeX command
    source_file: str = ""  # LaTeX: which .tex file
    source_line_start: int = 0  # LaTeX: start line
    source_line_end: int = 0  # LaTeX: end line
    label: str = ""  # LaTeX: \label{} value
    comments: list[dict] = field(default_factory=list)  # [{id, author, text}]

    def heading_level(self) -> int:
        return self.path.heading_level()


# ---------------------------------------------------------------------------
# Handler — abstract base for all document types
# ---------------------------------------------------------------------------


@dataclass
class Plugin:
    """A precis plugin — declares corpus metadata + handler behavior.

    Plugins are the unit of packaging and discovery.  Each pip-installable
    precis extension (precis-papers, precis-todos, …) exposes one or more
    Plugin instances via the ``precis.plugins`` entry point group.

    File-based handlers (DocxHandler, TexHandler) use ``file_types`` and
    leave ``corpus_id`` as None.  Corpus-based plugins (papers, todos, …)
    use ``schemes`` and set ``corpus_id``.

    Attributes:
        name: Plugin name used for logging and disable-list matching.
        handler_cls: The Handler subclass this plugin provides.
        schemes: URI schemes to register (e.g. ["paper", "doi", "arxiv"]).
        file_types: File extensions to register (e.g. [".docx"]).
        corpus_id: Corpus this plugin manages, or None for file handlers.
        write_policy: "ingestion" | "direct" | "system" — enforced by MCP.
        block_type_seeds: Extra (name, provenance, description) tuples.
        link_type_seeds: Extra (name, inverse, description) tuples.
        kinds: Agent-facing ``KindSpec`` declarations (plugin protocol v2).
            Plugins that omit this get a default spec synthesised per scheme
            by the registry.  File-type-only plugins (word, tex, markdown)
            don't declare kinds since they share the pseudo-kind ``doc``.
        protocol_version: Plugin-side declaration of which protocol
            major the plugin was written against.  Registry refuses to
            load plugins with a mismatching major version.
    """

    name: str
    handler_cls: type[Handler]
    schemes: list[str] = field(default_factory=list)
    file_types: list[str] = field(default_factory=list)
    corpus_id: str | None = None
    write_policy: str = "ingestion"
    block_type_seeds: list[tuple] = field(default_factory=list)
    link_type_seeds: list[tuple] = field(default_factory=list)
    # Plugin protocol v2 additions — optional, empty defaults keep v1 plugins working.
    kinds: list[KindSpec] = field(default_factory=list)
    protocol_version: str = PLUGIN_PROTOCOL_VERSION


class Handler(abc.ABC):
    """Base class for document type handlers.

    Subclass this to add support for a new document type (scheme or file
    extension). Implement ``read`` at minimum; override ``put`` to enable
    writing, and ``_write_note`` to enable annotations.

    **Plugin protocol v2 optional hooks** — override any of these to
    participate in the unified response footer / hint channel / boot-time
    notifications (see docs/plugin-architecture.md §5). Defaults are safe
    no-ops; existing handlers need no changes.
    """

    scheme: str = ""  # e.g. "file", "paper"
    writable: bool = False
    #: Supported ``/view`` names.  Two shapes are accepted:
    #:
    #: *  ``set[str]`` — stateless handlers (web, math, youtube) that
    #:    inline their dispatch in ``read()``.  The framework only uses
    #:    the set for ``VIEW_UNKNOWN`` options-enrichment.
    #: *  ``dict[str, str]`` — ``RefHandler`` subclasses map each view
    #:    to the name of a dispatch method.  ``read()`` does
    #:    ``getattr(self, views[view])(store, ref, sel, sub, **kwargs)``.
    #:
    #: The framework iterates keys in either case.
    views: ClassVar[set[str] | dict[str, str]] = set()
    #: Modes accepted by ``put()``.  Used by ``_enrich_error`` to
    #: auto-fill ``options=`` on ``MODE_UNSUPPORTED`` errors so the agent
    #: sees the valid alternatives without the handler having to repeat
    #: the list in every raise site.  Default empty means "override me
    #: in writable handlers".
    allowed_modes: ClassVar[set[str]] = set()

    #: Optional slug of the onboarding skill for this kind (Phase 12b).
    #: When set, the handler automatically exposes a ``/help`` view that
    #: inlines the full ``skill:<slug>`` body, and ``_enrich_error``
    #: appends a ``see get(id='skill:<slug>')`` pointer to the ``next=``
    #: slot on agent-confusion errors (``PARAM_INVALID`` /
    #: ``MODE_UNSUPPORTED`` / ``VIEW_UNKNOWN``).  Kinds with obvious
    #: semantics (todo, memory) can leave this unset.
    onboarding_skill: ClassVar[str | None] = None

    @abc.abstractmethod
    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        """Read/navigate/search document content."""
        ...

    def put(
        self,
        path: str,
        selector: str | None,
        text: str,
        mode: str,
        **kwargs,
    ) -> str:
        """Write to the document. Override in writable handlers."""
        if mode == "note":
            return self._write_note(path, selector, text, **kwargs)
        raise PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause=f"{self.scheme}: kind is read-only",
            next="put(mode='note') to annotate",
        )

    def _write_note(
        self,
        path: str,
        selector: str | None,
        text: str,
        **kwargs,
    ) -> str:
        """Attach an annotation. Override per handler."""
        raise PrecisError(
            ErrorCode.MODE_UNSUPPORTED,
            cause=f"annotations not supported for {self.scheme}",
        )

    # ---- Plugin protocol v2 optional hooks ---------------------------------

    def cost_of(self, ctx: CallContext) -> str | None:
        """Return a ``cost_hint`` string for the just-completed call, or None.

        Called after ``read``/``put`` to compute the cost line of the response
        footer.  Override in handlers that touch paid APIs; default returns
        ``None`` (free / omit cost line).
        """
        return None

    def hints(self, result: Any, ctx: HintContext) -> list[str]:
        """Return contextual hints appended to successful responses (§5.3).

        Invoked by the ``invoke_handler()`` wrapper only on success.  Hints
        are per-kind suggestions driven by the result shape.  Default
        returns ``[]`` (no hints).  See docs §5.3 for hint-worthy scenarios.
        """
        return []

    def notifications(self, ctx: NotificationContext) -> list[str]:
        """Return boot-time "current business" lines for this kind (§5.5).

        Called once at tool-description build.  Each line should convey a
        count + a call-to-fetch, e.g.::

            "20 todos due today → get(type='todo', id='/today')"

        Return ``[]`` when there's nothing to report (typical default).
        Stateless kinds never notify; state-backed kinds may.
        """
        return []
