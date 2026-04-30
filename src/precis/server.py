"""FastMCP server — unified tools for papers and documents.

4 tools: search(), get(), put(), move(), plus a read-only stats().

**Plugin protocol v2 (Phase 1) additions**:

- Tools accept an optional ``type=`` argument naming the kind. Aliases are
  resolved to canonical names at URI parse. Bare-id dispatch still works
  (back-compat) via :func:`_to_uri`'s auto-detection.
- ``PRECIS_KINDS`` (see §13 of docs/plugin-architecture.md) is parsed at
  startup in :func:`_load_kinds_mask` and stored in the registry.  Fatal
  config errors (alias in config, unknown verb, empty brackets, duplicate
  kind) print a single line to stderr and exit with code 2.
- :func:`stats` surfaces the list of enabled kinds per verb and any
  accumulated startup warnings.
"""

from __future__ import annotations

import os.path
import re
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from precis import tools
from precis.kinds_config import ConfigError, load_from_env
from precis.paper_id import classify_paper_id
from precis.protocol import CallContext, ErrorCode
from precis.registry import (
    ALIASES,
    KINDS,
    SCHEMES,
    STARTUP_WARNINGS,
    RegistryError,
    _discover,
    _format_error,
    add_startup_warning,
    invoke_handler,
    resolve,
    resolve_alias,
    set_kinds_mask,
    visible_kinds,
)
from precis.uri import _SEP_CHARS, SEP

mcp = FastMCP("precis")

# File extensions that trigger the file: scheme
_FILE_EXTENSIONS = {".docx", ".tex", ".md", ".markdown", ".rst", ".txt"}

# Max chars for multi-ID results before paginating
_MULTI_ID_BUDGET = 6000


def _split_sep(text: str) -> list[str]:
    """Split text at the first selector separator (~ or legacy ›)."""
    import re as _re

    return _re.split(r"[" + _re.escape(_SEP_CHARS) + r"]", text, maxsplit=1)


# Visually-similar characters that small models routinely substitute
# for ASCII ``~`` when the canonical form is presented in unfamiliar
# fonts.  Each one is rejected at the dispatcher boundary with a
# structured error suggesting ``~`` as the canonical separator.
# Review 2026-04-25 mcp-critic finding E3.
#
# ``›`` (U+203A, single right-pointing angle quotation mark) is
# *not* in this list — it is the legacy canonical from v5.x and
# remains accepted on input for back-compat (see
# :data:`precis.uri._LEGACY_SEP` and ``test_uri.TestSeparatorFlip``).
_LOOKALIKE_SEPS: dict[str, str] = {
    "\u2010": "U+2010 (Unicode hyphen)",
    "\u2011": "U+2011 (non-breaking hyphen)",
    "\u2013": "U+2013 (en-dash)",
    "\u2014": "U+2014 (em-dash)",
    "\u2212": "U+2212 (Unicode minus)",
    "\u2500": "U+2500 (box drawing horizontal)",
    "\uFE63": "U+FE63 (small hyphen-minus)",
    "\uFF0D": "U+FF0D (full-width hyphen-minus)",
}


def _check_lookalike_sep(id: str) -> str | None:
    """Return a structured-error string if ``id`` contains a non-ASCII
    separator lookalike, else ``None``.

    Catches the case where an agent (or a paste from rich text) writes
    ``wu2008first–38`` instead of ``wu2008first~38``.  Without this,
    such ids fall through to ``ID_NOT_FOUND`` with the unhelpful
    cause ``paper 'wu2008first–38' not in corpus``.
    """
    for ch, desc in _LOOKALIKE_SEPS.items():
        if ch in id:
            # Replace the lookalike with the canonical SEP, then collapse
            # any adjacent SEPs.  Without this, ``~–5`` (tilde + en-dash
            # + 5) becomes ``~~5`` instead of ``~5`` — review 2026-04-25
            # mcp-critic finding D4: the suggested fix in the error
            # message must itself parse.
            fixed = id.replace(ch, SEP)
            fixed = re.sub(rf"{re.escape(SEP)}{{2,}}", SEP, fixed)
            ctx = CallContext(
                kind="", verb="", args={"id": id}
            )
            from precis.registry import record_call as _record_call

            _record_call("", "free", errored=True)
            err = _format_error(
                ErrorCode.ID_MALFORMED,
                ctx,
                cause=(
                    f"id contains {desc} which looks like a separator "
                    "but isn't — the canonical chunk separator is "
                    "ASCII ``~`` (U+007E)"
                ),
                next_hint=f"try get(id={fixed!r})",
            )
            return f"{err}\n\n[cost: free]"
    return None


def _has_identifier_hint(id: str) -> bool:
    """True if ``id`` carries an unambiguous routing signal.

    Signals that qualify as a hint:

    - Scheme prefix (``paper:``, ``doi:``, ``memory:``, …).
    - Utility prefix (``slug:``, ``s2:``, ``ref:``) we routinely strip.
    - Known file extension (``.docx``, ``.tex``, ``.md``, …).
    - Bare pattern that classifies as DOI / arXiv / PMCID / ISBN / ISSN.

    A bare alphanumeric slug like ``ni2024atomic`` carries **no** hint
    and falls through to ``classify_paper_id``'s final "paper" fallback
    (``value == input``).  That case was silently routed to ``paper:``
    before the 2026-04-22 default-to-paper cleanup; now it triggers
    ``KIND_UNKNOWN`` so the caller is forced to say ``type='paper'`` or
    an explicit scheme prefix — same treatment as bare ``search`` and
    ``put`` calls.  BUG-C regression.
    """
    _discover()
    # Explicit scheme prefix — matches the SCHEMES loop in _to_uri.
    for scheme in SCHEMES:
        if id.startswith(scheme + ":"):
            return True
    # Utility prefixes we strip in _to_uri.
    for prefix in ("slug:", "s2:", "ref:"):
        if id.startswith(prefix):
            return True
    # File extension — matches the _FILE_EXTENSIONS check in _to_uri.
    bare = _split_sep(id)[0]
    base = bare.split("/")[0]
    _, ext = os.path.splitext(base)
    if ext.lower() in _FILE_EXTENSIONS:
        return True
    # Confident paper-id classification — DOI / arXiv / PMCID / ISBN /
    # ISSN all mutate ``value`` during normalisation, so a value that
    # still equals the input means the classifier fell through to the
    # "Everything else → slug" branch (paper_id.py line 419).
    classified = classify_paper_id(bare)
    if classified.scheme != "paper":
        return True
    if classified.value != bare:
        return True
    return False


def _to_uri(id: str, kind: str = "") -> str:
    """Convert a user-facing id to an internal URI.

    Phase 1: when ``kind`` is provided, the scheme is taken from
    ``kind`` (after alias resolution) and prepended to ``id`` unless
    ``id`` already carries a scheme prefix.  Alias hop::

        _to_uri("foo", kind="perplexity")  # → "web:foo" when 'perplexity'
                                           #   is an alias of 'web'

    When ``kind`` is empty, falls through to the legacy auto-detection:

    1. Known scheme prefix (``doi:``, ``arxiv:``, …) → keep as-is
    2. File extension (``.docx``, ``.tex``, …) → ``file:`` scheme
    3. Bare DOI pattern (``10.NNNN/...``) → ``doi:`` scheme
    4. Otherwise → ``paper:`` scheme (slug lookup)
    """
    _discover()  # ensure SCHEMES / ALIASES are populated

    # Phase 1: explicit kind hint — resolve alias, stamp scheme, done.
    # Phase 5 refinement: when the user-supplied kind also exists as a
    # registered scheme (e.g. ``pmid``, ``doi``, ``arxiv`` are aliases of
    # the ``paper`` kind but also schemes on the PaperHandler), emit the
    # kind name as the URI scheme so the handler can branch on identifier
    # type.  Fall back to canonical-kind-as-scheme otherwise.
    if kind:
        # If id already carries a scheme prefix, keep it — respect the
        # caller's specificity.
        for scheme in SCHEMES:
            if id.startswith(scheme + ":"):
                return id
        scheme_name = kind if kind in SCHEMES else resolve_alias(kind)
        return f"{scheme_name}:{id}" if id else f"{scheme_name}:"

    # Legacy path (no kind hint) — Phase 5: delegate to classify_paper_id
    # for paper-ish ids after stripping accidental prefixes and handling
    # the file-extension short-circuit.
    if not id:
        return "paper:"
    # Known scheme prefixes — keep their scheme intact
    for scheme in SCHEMES:
        prefix = scheme + ":"
        if id.startswith(prefix):
            return id  # already a valid URI
    # Strip accidental scheme prefixes the LLM might copy
    for prefix in ("slug:", "s2:", "ref:"):
        if id.startswith(prefix):
            id = id[len(prefix) :]
            break
    # File-extension short-circuit — .docx/.tex/.md/... go to file:
    # scheme, which the paper-id classifier doesn't (and shouldn't) know
    # about.  Check this BEFORE classification so documents don't get
    # misrouted as slugs.
    bare = _split_sep(id)[0]
    base = bare.split("/")[0]
    _, ext = os.path.splitext(base)
    if ext.lower() in _FILE_EXTENSIONS:
        return f"file:{id}"
    # Phase 5: classify DOI / arXiv / PMCID / ISBN / ISSN / slug.
    # Selector suffix (``~chunk``, ``/view``) must ride along; strip for
    # classification, re-attach for the final URI.
    suffix = id[len(bare) :]
    classified = classify_paper_id(bare)
    return f"{classified.scheme}:{classified.value}{suffix}"


# ── PRECIS_KINDS startup wiring (§13) ───────────────────────────────


def _load_kinds_mask(*, env: dict[str, str] | None = None) -> None:
    """Parse ``PRECIS_KINDS`` and install the mask.

    Fatal config errors (``ConfigError``) print to stderr and exit(2).
    Non-fatal warnings (unknown kinds) are funnelled into
    ``STARTUP_WARNINGS`` via :func:`add_startup_warning`.  No-ops when the
    env var is empty or unset; the server then exposes every registered
    kind with every verb.
    """
    _discover()  # so KINDS / ALIASES are populated for known_kinds check
    warnings: list[str] = []
    try:
        mask = load_from_env(
            env=env,
            aliases=ALIASES,
            known_kinds=KINDS,
            warnings_out=warnings,
        )
    except ConfigError as exc:
        print(f"precis-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    for w in warnings:
        add_startup_warning(w)
    set_kinds_mask(mask)


# ── Dispatch helper (Phase 2) ────────────────────────────────────────


def _kind_from_uri(uri: str) -> str:
    """Return the canonical kind name for a URI's scheme.

    Resolution order:

    1. :func:`resolve_alias` \u2014 explicit ``KindSpec.aliases`` entries.
    2. Direct hit in :data:`KINDS` (kind name == scheme name, e.g.
       ``paper:`` \u2192 ``paper``).
    3. **Scheme \u2192 canonical-kind fallback** \u2014 when a scheme is
       registered (via :data:`SCHEMES`) but isn't itself a kind and
       isn't an alias, we walk :data:`PLUGINS` to find the plugin
       that owns it and return that plugin's first KindSpec name.

    Step (3) is what makes ``doi:10.x/y``, ``arxiv:2207.09327``,
    ``pmid:12345``, etc. dispatch through the canonical ``paper``
    kind's :data:`KindSpec` entry in :func:`_dispatch`, which in
    turn means they go through ``Result.render()`` and pick up the
    standard ``[cost: free]`` footer \u2014 the parity the previous
    behaviour broke (mcp-critic finding D12).

    Distinct from the alias path: the scheme \u2192 kind fallback only
    fires when the kind name is *implicit* in the URI scheme, not
    when the agent typed ``type='doi'``.  ``type='doi'`` still has
    to go through ``ALIASES`` and is still explicitly rejected (per
    Apr 2026 cleanup) \u2014 ``type=`` is the agent-facing kind enum
    and only canonical kinds belong there.
    """
    from precis.registry import PLUGINS

    scheme = uri.split(":", 1)[0] if ":" in uri else uri
    aliased = resolve_alias(scheme)
    if aliased != scheme:
        return aliased
    if aliased in KINDS:
        return aliased
    # Scheme→canonical-kind fallback (D12).
    handler_cls = SCHEMES.get(scheme)
    if handler_cls is not None:
        for plugin in PLUGINS.values():
            if plugin.handler_cls is handler_cls and plugin.kinds:
                return plugin.kinds[0].name
    return aliased


# Kinds whose ``id`` is an opaque expression or natural-language query.
# For these, commas are part of the input syntax (``calc:integrate(sin(x), x)``,
# ``websearch:foo, bar baz``) and MUST NOT be treated as a batch
# separator.  Ref-backed kinds (paper/doi/arxiv/memory/todo/…) keep the
# comma-batch behaviour so ``get(id='slug1~5,slug2~9')`` still works.
#
# Note: even kinds that *do* support batching get balanced-paren
# protection from :func:`_split_top_level_commas` — a comma inside
# parentheses is never a batch boundary regardless of kind.  This
# list is the second line of defence: kinds whose entire id is
# semantically opaque skip splitting altogether.
_NO_COMMA_SPLIT_KINDS: frozenset[str] = frozenset({
    "calc", "math", "plot",
    "websearch", "research", "think",
    "youtube",
    # Stateless primitives whose id may legitimately carry commas as
    # *list* separators inside the URI itself: ``rng:choice/a,b,c``,
    # ``rng:shuffle/a,b,c,d``, ``random:choice/x,y,z``.  Without this,
    # the batch splitter silently dispatches the head element only
    # ("a"), then yells about the rest as ambiguous bare slugs — the
    # silent-success-then-spurious-errors failure mode mcp-critic
    # M1 called out.  ``clock`` and ``oracle`` join for symmetry with
    # the search-incompatible set above (their ids are also opaque).
    "rng", "random",
    "clock", "oracle",
})

# Kinds where ``search()`` doesn't apply: there is no corpus to vector-
# search.  ``calc`` evaluates an expression, ``math`` queries Wolfram,
# ``websearch`` calls Perplexity — all of these are ``get()`` operations
# that happen to take a query as the id.  Routing them through
# ``search()`` was a TypeError before because handler ``read()`` signatures
# don't accept ``top_k``.  We now reject at the router with a hint.
_SEARCH_INCOMPATIBLE_KINDS: frozenset[str] = frozenset({
    "calc", "math", "plot",
    "websearch", "research", "think",
    "youtube",
    # Stateless primitives that take a query as the id and have no
    # meaningful ``top_k`` notion (single-value evaluators).
    # ``random`` and ``oracle`` ARE compatible — random implements
    # internal sample semantics, and oracle is paper-shaped (search
    # against chunks via the ref join) — so they're absent from
    # this set.
    "clock", "rng",
})


def _supports_comma_batch(type_arg: str, id: str) -> bool:
    """True when ``id`` should be split on commas as a ref batch.

    The decision is layered: an explicit ``scheme:`` prefix on the id
    wins (so ``get(id='calc:1+2,foo')`` is honoured even when ``type=``
    is unset), then the user-supplied ``type=`` is consulted, then the
    default is True (ref-backed kinds support batch).

    This is the first line of defence against the comma-split crash
    for ``get(type='calc', id='integrate(sin(x), x)')`` — the input
    was being split into ``['integrate(sin(x)', 'x)']`` and dispatched
    as two ids.  The second line of defence is
    :func:`_split_top_level_commas`, which protects commas inside
    parentheses on every kind regardless of this flag.
    """
    # Explicit scheme prefix on the id wins.
    if ":" in id:
        scheme = id.split(":", 1)[0]
        if resolve_alias(scheme) in _NO_COMMA_SPLIT_KINDS:
            return False
    # Otherwise consult ``type=``.
    if type_arg and resolve_alias(type_arg) in _NO_COMMA_SPLIT_KINDS:
        return False
    return True


def _split_top_level_commas(text: str) -> list[str]:
    """Split ``text`` on commas that sit at parenthesis depth 0.

    Commas inside ``()``, ``[]`` or ``{}`` are part of the inner
    expression syntax (Python function calls, matrix literals, set
    builders) and must never be treated as batch separators.  Without
    this guard, ``rng:int(1,100)`` got chopped into ``rng:int(1`` and
    ``100)`` and surfaced two confusing error envelopes per call.
    Review 2026-04-25 mcp-critic finding C5/E3.

    Behaviour:

    - Same output as ``text.split(',')`` when no parens are present.
    - Preserves nested-paren commas verbatim: ``a(b,c),d`` →
      ``['a(b,c)', 'd']``.
    - Whitespace is stripped from each part; empty strings are
      dropped (mirrors the previous list-comprehension idiom in
      ``server.get``).
    - Treats unbalanced trailing parens as content (no exception);
      the downstream URI parser will surface the malformed id with
      its own structured error.
    """
    out: list[str] = []
    current: list[str] = []
    depth = 0
    openers = "([{"
    closers = ")]}"
    for ch in text:
        if ch in openers:
            depth += 1
            current.append(ch)
        elif ch in closers:
            if depth > 0:
                depth -= 1
            current.append(ch)
        elif ch == "," and depth == 0:
            out.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if current:
        out.append("".join(current).strip())
    return [part for part in out if part]


def _strip_inner_cost_footers(parts: list[str]) -> str:
    """Join multi-id batch parts with one trailing cost footer.

    Each rendered ``Result`` carries its own ``[cost: <hint>]`` line on
    a fresh line at the end of the body.  Concatenating N such bodies
    repeats the footer N times \u2014 ~9 tokens of pure noise per chunk,
    which adds up fast for large batches.  Review 2026-04-25 finding
    D7.

    Strategy:

    - Find every ``[cost: \u2026]`` line embedded in the parts.
    - Pick the **highest-cost** one as the surviving footer (so a
      mixed paid/free batch never silently downgrades).
    - Remove the cost line from every part body.
    - Join with the existing ``\\n---\\n`` separator and append a single
      trailing ``[cost: \u2026]`` line.

    Cost ranking is naive (string sort with ``free`` as the floor)\u2014
    accurate enough because the only batch shapes that actually mix\u2014
    paid + free are rare and either footer is correct in those cases.\u2014
    """
    surviving_cost = ""

    def _strip(part: str) -> str:
        nonlocal surviving_cost
        if not part:
            return part
        body_lines: list[str] = []
        for line in part.splitlines():
            stripped = line.strip()
            if stripped.startswith("[cost: ") and stripped.endswith("]"):
                cost = stripped[len("[cost: "):-1]
                # Prefer non-free over free (paid calls take priority
                # in the surviving footer).  Otherwise keep the first.
                if (
                    not surviving_cost
                    or (surviving_cost == "free" and cost != "free")
                ):
                    surviving_cost = cost
                continue
            body_lines.append(line)
        # Drop trailing blank lines left by the stripped footer.
        while body_lines and not body_lines[-1].strip():
            body_lines.pop()
        return "\n".join(body_lines)

    cleaned = [_strip(p) for p in parts]
    joined = "\n---\n".join(cleaned)
    if surviving_cost:
        joined = f"{joined}\n\n[cost: {surviving_cost}]"
    return joined


def _ambiguous_kind_error(
    verb: str,
    *,
    cause: str,
    args: dict[str, object] | None = None,
) -> str:
    """Render a ``KIND_UNKNOWN`` error for an ambiguous no-type call.

    Appends ``[cost: free]`` so every error response in the server
    carries a cost footer regardless of which path produced it — the
    bare-call disambiguator bypasses ``Result.render()`` and would
    otherwise drop the footer.

    The previous behaviour was to silently default to ``paper:`` whenever
    a caller omitted both ``type=`` and any disambiguating id/scope.
    That made it easy to, e.g. search todos but get paper results back
    because the agent forgot a keyword.  The strict form instead lists
    the visible kinds and tells the caller to re-issue with ``type=``.

    Ordering mirrors :func:`visible_kinds` output (alphabetical), so the
    agent always sees a stable enum.
    """
    kinds = [k.spec.name for k in visible_kinds(verb)]
    ctx = CallContext(kind="", verb=verb, args=dict(args) if args else {})
    err = _format_error(
        ErrorCode.KIND_UNKNOWN,
        ctx,
        cause=cause,
        options=kinds or None,
        next_hint=(
            "re-issue with an explicit type=, e.g. "
            f"{verb}(type='paper', ...) — "
            "stats() lists every kind currently available"
        ),
    )
    # Phase 6c — record the call as an error in session stats so
    # ``stats()`` reflects the true error rate.  Empty kind is
    # intentional: this call never resolved to a concrete kind and
    # charging it to a specific bucket would be misleading.
    from precis.registry import record_call as _record_call

    _record_call("", "free", errored=True)
    # Cost-footer parity with every other error path (Phase 6b): no
    # handler was dispatched, so cost is ``free``.
    return f"{err}\n\n[cost: free]"


def _dispatch(
    kind: str,
    verb: str,
    call,  # type: ignore[no-untyped-def]  # Callable[[], str]
    args: dict[str, object] | None = None,
) -> str:
    """Wrap a handler call in :func:`invoke_handler` and render the Result.

    Phase 2: every tool response flows through here, so every response
    carries a cost footer (default ``[cost: free]``) and session stats
    accumulate for the ``stats()`` tool.

    Parameters:
        kind: Canonical kind name, already alias-resolved.
        verb: One of ``"search" | "get" | "put" | "move"``.
        call: Zero-arg callable that does the actual work (usually a
            closure over ``tools.read`` / ``tools.put``).
        args: Optional call-argument dict, passed to the wrapper for
            error formatting (``CallContext.args`` populates the
            ``where:`` line in error messages).

    Returns the rendered response string (``Result.render()``).  If the
    kind is not in ``KINDS`` (e.g. orphan scheme from a legacy plugin
    that skipped ``KindSpec``), falls back to calling ``call()`` raw so
    behaviour stays identical to the pre-wrapping path.  This keeps the
    server forgiving for third-party plugins that haven't migrated to
    plugin protocol v2 yet.
    """
    registered = KINDS.get(kind)
    if registered is None:
        # Unknown canonical kind — no KindSpec to drive the full Result
        # pipeline, but we still emit the same structured envelope as
        # invoke_handler.  BUG-E fix: the old path emitted a bespoke
        # ``!! ERROR <ExcType>: <msg>`` shape that the agents had to
        # parse as a special case; now every error carries the standard
        # ``ERROR [<code>]: …`` envelope with cause / options / next.
        from precis.protocol import PrecisError

        ctx = CallContext(
            kind=kind, verb=verb, args=dict(args) if args else {}
        )

        from precis.registry import record_call as _record_call

        def _finalise(err_str: str) -> str:
            # The unknown-kind path bypasses ``Result.render()`` so we
            # apply the same finalisation here: stamp the cost footer
            # (Phase 6b) AND record the call as an error in session
            # stats (Phase 6c).  Without these, agents lost both cost
            # accounting and error-rate visibility for unknown-scheme
            # calls.  The kind has no KindSpec so we use the
            # conservative ``free`` default — no handler ran, no API
            # was billed.
            _record_call(kind, "free", errored=True)
            return f"{err_str}\n\n[cost: free]"

        try:
            return call()
        except PrecisError as exc:
            return _finalise(
                _format_error(
                    exc.code,
                    ctx,
                    cause=exc.cause or str(exc),
                    options=list(exc.options) if exc.options else None,
                    next_hint=exc.next,
                )
            )
        except Exception as exc:
            return _finalise(
                _format_error(
                    ErrorCode.UNEXPECTED,
                    ctx,
                    cause=f"{type(exc).__name__}: {exc}",
                )
            )
    # Resolve the SAME cached handler instance that ``tools.read`` /
    # ``tools.put`` will use inside ``call``.  Constructing a fresh
    # ``registered.handler_cls()`` here would defeat the per-process
    # memoisation in ``_SCHEME_INSTANCES`` (warm DB pools, lazy HTTP
    # clients, parsed indexes — see registry.py:62-77) and would split
    # ``handler.hints()`` / ``handler.cost_of()`` invocations onto a
    # different object than the one that actually ran the call.
    #
    # Strategy: every plugin registers its canonical kind name as one
    # of its ``schemes`` (see ``_register_plugin``), so ``resolve(kind,
    # "")`` is guaranteed to land on the cached handler for v2 plugins.
    # The bare ``except`` falls back to a fresh instance for legacy
    # plugins that bend the convention — better to lose the cache than
    # crash the dispatch.
    try:
        handler = resolve(kind, "")
    except Exception:
        handler = registered.handler_cls()
    result = invoke_handler(kind, verb, handler, call, args=args)
    return result.render()


# ── Tools ────────────────────────────────────────────────────────────


@mcp.tool()
def search(
    query: str = "",
    top_k: int = 5,
    scope: str = "",
    type: str = "",
    grep: str = "",
) -> str:
    """Semantic search or external query — dispatches by type=.

    Three filter layers compose, applied in order:

      type=  → which corpus(es) to search (paper, memory, …, or
               'all' / 'a,b,c').  REQUIRED unless scope= is set.
      grep=  → metadata predicate over many refs (tag:review,
               year:2020-, author:wang).  Pre-filter before vectors.
      scope= → exact slug or filename — restricts to ONE ref.
               Hits from any other ref are dropped.  Cannot combine
               with type='all' or comma-list types.
      query= → semantic similarity over whatever survived the above.

    top_k: number of results returned after filtering (default 5).

    type accepted values:
      - Single ref-backed: paper, memory, conversation, todo,
        flashcard, web, book — vector search within one corpus.
      - Cross-corpus: type='all' or 'paper,memory,web' — unified
        ranking grouped by kind.
      - External services: websearch, research, think (Perplexity);
        math (Wolfram, paid); calc (local SymPy, free); youtube;
        skill, quest (stateful).  Cannot appear in a comma-list.

    Examples:
      # Single-kind semantic search
      search(query='CO2 capture MOFs', type='paper')
      search(query='nitrate', scope='ni2024atomic')
      search(query='membrane', type='paper', grep='tag:review')

      # Cross-corpus — search everything at once
      search(query='MOFs', type='all')
      search(query='MOFs', type='paper,memory,web')

      # Compute — calc (free, offline)
      search(query='2+3*4', type='calc')
      search(query='integrate sin(x)*cos(x) dx', type='calc')

      # Compute — math (Wolfram Alpha, paid)
      search(query='population of Ireland', type='math')
      search(query='orbital period of Jupiter', type='math')

      # External data
      search(query='latest perovskite results', type='websearch')
      search(query='mechanistic review of X', type='research')

      # Stateful
      search(query='design decision', type='memory')
      search(query='acquire paper', type='skill')
    """
    if not query.strip():
        return "ERROR: query is required. Example: search(query='CO2 capture MOF')"

    # Cross-corpus dispatch — ``type='all'`` or a comma-separated list
    # like ``type='paper,memory,web'`` routes to a single
    # ``store.search_text(corpora=[...])`` call that returns a unified
    # ranking and a grouped-by-kind rendering.  Must be checked BEFORE
    # the per-kind ``_to_uri`` path below because "all" / "a,b" aren't
    # valid kind names on their own.
    from precis.cross_corpus import (
        expand_type_to_corpora,
        format_cross_corpus_error,
        is_cross_corpus_request,
        search_across_corpora,
    )
    from precis.protocol import PrecisError

    if is_cross_corpus_request(type):
        try:
            corpora = expand_type_to_corpora(type)
            return search_across_corpora(
                query=query,
                corpora=corpora,
                top_k=top_k,
                scope=scope,
            )
        except PrecisError as exc:
            return format_cross_corpus_error(
                exc, query=query, type_arg=type, top_k=top_k
            )

    if scope:
        uri = _to_uri(scope, kind=type)
    elif type:
        uri = _to_uri("", kind=type)
    else:
        # No type, no scope — don't silently default to the paper corpus.
        # Silent defaults caused silent cross-kind leaks in agent flows
        # (e.g. an intended todo search returning paper chunks).
        return _ambiguous_kind_error(
            "search",
            cause=(
                "search() requires an explicit type= when no scope is given — "
                "otherwise the call is ambiguous across kinds"
            ),
            args={"query": query, "top_k": top_k, "grep": grep},
        )
    kind = _kind_from_uri(uri)

    # Compute/external kinds (calc, math, websearch, …) accept the query
    # as the expression itself — ``search(type='calc', query='2+3*4')``
    # is semantically the same as ``get(type='calc', id='2+3*4')``.
    # Their handler ``read()`` signatures do not declare ``top_k`` (and
    # the concept of "top k results" is meaningless for a single-value
    # evaluator), so forwarding it crashes with TypeError at the
    # ``handler.read(**kwargs)`` boundary.  Drop it here at the router.
    if kind in _SEARCH_INCOMPATIBLE_KINDS:
        return _dispatch(
            kind,
            "search",
            lambda: tools.read(uri=uri, query=query, page=1),
            args={"id": scope, "query": query, "grep": grep},
        )

    # BUG-F fix — forward ``grep`` to the handler so it can apply the
    # metadata pre-filter on top of the vector search.  The kwarg was
    # declared only on ``get()`` before, so any MCP client that passed
    # ``grep=`` to ``search()`` had it silently dropped at the tool
    # boundary and got an unfiltered search back.
    extra = {"grep": grep} if grep else {}
    return _dispatch(
        kind,
        "search",
        lambda: tools.read(uri=uri, query=query, page=1, top_k=top_k, **extra),
        args={"id": scope, "query": query, "top_k": top_k, "grep": grep},
    )


@mcp.tool()
def get(
    id: str = "",
    grep: str = "",
    depth: int = 0,
    type: str = "",
) -> str:
    """Read content by identifier.

    id:    identifier (selector ~N, view /V, subview /V/S)
    grep:  filter — plain text, /regex/, /regex/i
    depth: heading depth (0=all, 1=H1, 2=H1+H2)
    type:  kind name — REQUIRED for bare slugs.  Accepted: paper,
           todo, skill, memory, conversation, flashcard, quest,
           websearch, research, think, math, calc, youtube.

    Papers (bare slugs need type='paper' or scheme prefix):
      get(type='paper', id='ni2024atomic')          — overview
      get(type='paper', id='ni2024atomic~38')       — chunk 38
      get(type='paper', id='ni2024atomic~38..42')   — chunk range
      get(type='paper', id='ni2024atomic/toc')      — chunk index
      get(type='paper', id='ni2024atomic/abstract') — abstract
      get(type='paper', id='ni2024atomic/cite/bib') — BibTeX
      get(type='paper', id='ni2024atomic/fig/3')    — figure 3
      get(id='paper:ni2024atomic')                  — scheme prefix
      get(id='doi:10.1002/aenm.202400065')             — DOI
      get(id='arxiv:2207.09327')                     — arXiv
      get(type='paper', grep='tag:review')           — filter list

    Files (extension auto-classifies — no type= needed):
      get(id='report.docx')                 — DOCX TOC
      get(id='report.docx~PLXDX')           — paragraph by slug
      get(id='report.docx', grep='methods') — grep
      get(id='report.docx', depth=2)        — outline only

    Compute (query goes in id=):
      get(type='calc', id='2+3*4')                   — exact arithmetic
      get(type='calc', id='integrate(sin(x), x)')    — calculus
      get(type='calc', id='0xff')                    — base conversion
      get(type='calc', id='Matrix([[1,2],[3,4]]).det()') — linalg
      get(type='math', id='population of Ireland')   — Wolfram (paid)
      get(type='math', id='orbital period of Jupiter') — world data

    External data:
      get(type='websearch', id='latest perovskite results') — Perplexity
      get(type='research',  id='mechanistic review of X')   — deep research
      get(type='youtube',   id='dQw4w9WgXcQ')                — transcript

    Stateful kinds:
      get(type='todo',   id='/recent')     — recent todos
      get(type='skill',  id='find-paper')  — render skill
      get(type='memory', id='/recent')     — recent memories
      get(type='quest',  id='/recent')     — request queue
    """
    if not id and not grep:
        # Every example below has been hand-verified to dispatch
        # cleanly post-BUG-C (Apr 2026, bare-slug rejection): each
        # one either has a ``type=`` qualifier, a scheme prefix, or
        # a file extension — i.e. an identifier hint that
        # :func:`_has_identifier_hint` accepts.  The previous
        # version taught the pre-BUG-C behaviour and every example
        # rejected, breaking onboarding for any agent that hit this
        # recovery path.  Review 2026-04-25 finding C2.
        #
        # Footer parity: ``_dispatch`` appends ``[cost: free]`` on
        # every other error path; this branch short-circuits before
        # dispatch, so we add it inline.
        examples: list[tuple[str, str]] = [
            ("get(type='paper', id='ni2024atomic')",       "paper overview"),
            (f"get(type='paper', id='ni2024atomic{SEP}5')", "read chunk 5"),
            ("get(type='paper', id='ni2024atomic/toc')",  "table of contents"),
            ("get(id='paper:ni2024atomic')",              "scheme prefix"),
            ("get(id='report.docx')",                     "file extension auto-routes"),
            ("get(type='memory', id='/recent')",          "recent memories"),
            ("stats()",                                   "list every kind"),
        ]
        call_width = max(len(call) for call, _ in examples)
        body_lines = [
            f"  {call:<{call_width}}  — {desc}" for call, desc in examples
        ]
        return (
            "ERROR: id or grep is required. Do not call get() with empty parameters.\n"
            + "\n".join(body_lines)
            + "\n\n[cost: free]"
        )
    # Reject visually-similar separators (en-dash, em-dash, U+2010
    # hyphen, ...) before they fall through to ID_NOT_FOUND with a
    # confusing cause line.  Keeps the recovery hint pointing at the
    # canonical ASCII ``~``.  Review 2026-04-25 finding E3.
    if id:
        lookalike_err = _check_lookalike_sep(id)
        if lookalike_err is not None:
            return lookalike_err
    # Comma-separated multi-ID: dispatch each, paginate if over budget.
    # Skipped for compute/external kinds (calc, math, websearch, …)
    # where the *whole* id is opaque — see :func:`_supports_comma_batch`.
    # On every kind that does support batching, the split is still
    # paren-aware so commas inside ``int(1,100)`` / ``Matrix([[1,2]])``
    # never become batch boundaries.  Review 2026-04-25 finding C5/E3.
    if id and _supports_comma_batch(type, id):
        ids = _split_top_level_commas(id)
    else:
        ids = []
    if len(ids) > 1:
        parts: list[str] = []
        total = 0
        for i, single_id in enumerate(ids):
            # Same BUG-C check as the single-id path — bare slug with
            # no type= and no identifier hint errors out.
            if not type and not _has_identifier_hint(single_id):
                result = _ambiguous_kind_error(
                    "get",
                    cause=(
                        f"get(id={single_id!r}) is ambiguous — no scheme "
                        "prefix and doesn't match any known identifier "
                        "pattern (DOI / arXiv / PMCID / ISBN / file ext)."
                    ),
                    args={"id": single_id, "grep": grep, "depth": depth},
                )
                total += len(result)
                parts.append(result)
                continue
            uri = _to_uri(single_id, kind=type)
            kind = _kind_from_uri(uri)
            extra = {"grep": grep} if grep else {}
            result = _dispatch(
                kind,
                "get",
                lambda uri=uri, extra=extra: tools.read(
                    uri=uri, query="", depth=depth, **extra
                ),
                args={"id": single_id, "grep": grep, "depth": depth},
            )
            total += len(result)
            parts.append(result)
            # Check budget after adding (always include at least 1 result)
            if total > _MULTI_ID_BUDGET and i < len(ids) - 1:
                remaining = ids[i + 1 :]
                parts.append(
                    f"\n[{i + 1} of {len(ids)} IDs shown. "
                    f"Remaining: get(id='{','.join(remaining)}')]"
                )
                break
        # Dedupe per-chunk ``[cost: free]`` footers (D7).  Each
        # ``_dispatch`` returned a fully rendered Result with its own
        # cost line; for a 50-id batch that adds up to ~450 tokens of
        # repetition.  Strip every embedded footer and append a single
        # one at the end.  We keep the per-chunk cost on the trailing
        # element so any non-free batch (mixed paid/free kinds) still
        # surfaces the most expensive call rather than being silently
        # downgraded to free.
        joined = _strip_inner_cost_footers(parts)
        return joined
    if id:
        # BUG-C — bare slug with no type= and no identifier hint used to
        # silently route to paper.  Now rejected for parity with the
        # ``search`` and ``put`` no-type paths: force the caller to
        # disambiguate with type= or an explicit scheme prefix.
        if not type and not _has_identifier_hint(id):
            return _ambiguous_kind_error(
                "get",
                cause=(
                    f"get(id={id!r}) is ambiguous — {id!r} has no scheme "
                    "prefix and doesn't match any known identifier "
                    "pattern (DOI / arXiv / PMCID / ISBN / file ext)."
                ),
                args={"id": id, "grep": grep, "depth": depth},
            )
        uri = _to_uri(id, kind=type)
    elif type:
        uri = _to_uri("", kind=type)
    else:
        # Only grep= provided — was previously a silent paper-list filter.
        # Make the caller commit to a kind so agents that meant
        # get(type='todo', grep='foo') don't accidentally browse papers.
        return _ambiguous_kind_error(
            "get",
            cause=(
                "get() with only grep= is ambiguous — specify type= so the "
                "filter runs over the intended corpus"
            ),
            args={"grep": grep, "depth": depth},
        )
    kind = _kind_from_uri(uri)
    extra = {"grep": grep} if grep else {}
    return _dispatch(
        kind,
        "get",
        lambda: tools.read(uri=uri, query="", depth=depth, **extra),
        args={"id": id, "grep": grep, "depth": depth},
    )


@mcp.tool()
def put(
    id: str,
    text: str = "",
    mode: str = "replace",
    tracked: bool = True,
    note: str = "",
    link: str = "",
    unlink: str = "",
    type: str = "",
    tags: list[str] | None = None,
    archive: bool | None = None,
) -> str:
    """Write, annotate, or delete content.

    id: target identifier (file~slug for docs, paper slug for notes)
    text: content to write.
    mode: append / replace / after / before / delete / comment / note
    tracked: DOCX track-changes (default true). LaTeX: ignored.
    note: annotation text — creates a note on the target ref or block.
    link: link spec as 'target_slug:relation' — creates a typed link.
    unlink: link spec as 'target_slug[:relation]' — deletes outbound
            links to that target (any relation when unspecified).  Dual
            of link=; works on every state-backed kind.
    tags: list[str] — ref kinds that accept it (todo, memory). Forwarded only when non-None.
    archive: bool — web: kind, archive to web.archive.org on capture (default on; private URLs never archived).

    Headings: start line with # markers. Never number them.
      # Document Title    (Title style — one per document)
      ## Section           (Heading 1)
      ### Subsection       (Heading 2)
      #### Sub-subsection  (Heading 3, max depth)

    NEW content → mode='append' (creates file if needed):
      put(id='report.docx', text='## Methods', mode='append')
      put(id='report.docx', text='First paragraph.', mode='append')

    EDIT existing content → mode='replace' (requires ~SLUG in id):
      put(id='report.docx~PLXDX', text='Revised.', mode='replace')
      put(id='report.docx~PLXDX', text='New para.', mode='after')
      put(id='report.docx~PLXDX', mode='delete')
      put(id='report.docx~PLXDX', text='Fix this.', mode='comment')

    Citations (DOCX): [@slug] in text — NEVER [slug~chunk].
      ✓ [@piscopo2020strategies]  ✗ [piscopo2020strategies~54]
      Define: put(id='report.docx', text='[@slug]: Author, Title, 2024.', mode='append')

    Notes (on any ref or block):
      put(id='ni2024atomic', note='Key finding about MOFs')
      put(id='ni2024atomic~38', note='Important result here')

    Links (between refs or blocks):
      put(id='ni2024atomic', link='jones2023surface:cites')
      put(id='ni2024atomic', link='jones2023surface')  — defaults to 'references'

    Tags (todo, memory):
      put(type='todo', text='Fix parser', tags=['urgent'], mode='append')
      put(id='todo:fix-parser', text='urgent', mode='tag')    — also: mode='untag'

    Multiple paragraphs separated by newlines are auto-split.
    """
    if not id and not type:
        # Previously fell through to ``paper:`` which then returned a
        # readonly error — confusing the agent about whether ``put`` was
        # wrong or the kind was wrong.  Be explicit: list the writable
        # kinds and tell the caller to re-issue with type=.
        return _ambiguous_kind_error(
            "put",
            cause=(
                "put() requires either an id with a scheme prefix or an "
                "explicit type= — neither was provided"
            ),
            args={"mode": mode},
        )
    uri = _to_uri(id, kind=type)
    kind = _kind_from_uri(uri)

    # Only forward ``tags`` / ``archive`` when the caller set them —
    # handlers that don't accept the kwarg would otherwise reject
    # ``tags=None`` via ``extract_kwargs``.  Same gating pattern as
    # ``tracked`` inside ``tools.put()``.
    extra: dict[str, Any] = {}
    if tags is not None:
        extra["tags"] = tags
    if archive is not None:
        extra["archive"] = archive

    return _dispatch(
        kind,
        "put",
        lambda: tools.put(
            uri=uri,
            text=text,
            mode=mode,
            tracked=tracked,
            note=note,
            link=link,
            unlink=unlink,
            **extra,
        ),
        args={"id": id, "mode": mode},
    )


@mcp.tool()
def move(
    id: str,
    after: str,
    type: str = "",
) -> str:
    """Reorder nodes within a document.

    id: doc.docx~SLUG or doc.docx~SLUG1,SLUG2 to move
    after: doc.docx~SLUG — moved nodes placed after this node
    type: optional kind name (aliases accepted).

    Slugs don't change. Paths are recomputed.
    """
    uri = _to_uri(id, kind=type)
    # Extract the 'after' slug from id format (strip file part if present)
    after_sel = _split_sep(after)[-1] if any(c in after for c in _SEP_CHARS) else after
    kind = _kind_from_uri(uri)
    return _dispatch(
        kind,
        "move",
        lambda: tools.put(uri=uri, text=after_sel, mode="move"),
        args={"id": id, "after": after},
    )


@mcp.tool()
def stats() -> str:
    """Read-only server introspection — §8, §10.2.

    Lists what the server is currently exposing: enabled kinds per verb,
    scheme aliases each kind responds to, active ``PRECIS_KINDS`` mask
    (if any), session call counts + last cost per kind, and accumulated
    startup warnings.  No secrets.  Always public — there is no hidden
    admin mode (§18 non-goal).

    Example output::

        service: precis-mcp
        mask: unset (expose all)
        kinds by verb:
          search paper, memory, web
          get    paper, memory, web
          put    memory
          move   (none)
        scheme aliases:
          book   ← isbn
          paper  ← doi, arxiv, pmid, pmcid, isbn, issn
        session:
          paper   calls=12  errors=0  last_cost=free
          web     calls=3   errors=1  last_cost=~$0.002/call
        startup warnings:
          - kind 'news' hidden — missing env: PG_DATABASE_URL
    """
    from precis.registry import KINDS, PLUGINS, get_kinds_mask, get_session_stats

    _discover()
    lines: list[str] = ["service: precis-mcp"]
    mask = get_kinds_mask()
    lines.append(
        f"mask: {'PRECIS_KINDS set' if mask is not None else 'unset (expose all)'}"
    )
    lines.append("kinds by verb:")
    # Width tracks the longest verb name in :data:`VERBS` so adding a
    # new verb won't silently misalign the column.  Alphabetical
    # display order isn't great for scanning ("search" first reads
    # oddly) — keep the canonical read-then-write order explicit.
    verb_order = ("search", "get", "put", "move")
    verb_width = max(len(v) for v in verb_order)
    for verb in verb_order:
        kinds = [k.spec.name for k in visible_kinds(verb)]
        shown = ", ".join(kinds) if kinds else "(none)"
        lines.append(f"  {verb:<{verb_width}} {shown}")

    # Scheme aliases — surface the alternate URI prefixes each kind
    # responds to.  Without this section, an agent sees ``paper`` in
    # ``kinds by verb`` and has no way to learn that ``doi:10.x/y``,
    # ``arxiv:2207.09327``, ``pmid:12345`` etc. all route to the same
    # handler.  The ``ERROR [kind_unknown]`` envelope only lists
    # canonical kinds, so this is the one place the full URI vocabulary
    # is discoverable from the running server.
    alias_rows: list[tuple[str, list[str]]] = []
    for name in sorted(KINDS):
        kind = KINDS[name]
        plugin = PLUGINS.get(kind.plugin_name)
        if plugin is None:
            continue
        alts = [s for s in plugin.schemes if s != name]
        if alts:
            alias_rows.append((name, alts))
    if alias_rows:
        lines.append("scheme aliases:")
        # Pad to longest kind name so the arrows align — the agent
        # scans this section by column.
        width = max(len(name) for name, _ in alias_rows)
        for name, alts in alias_rows:
            lines.append(f"  {name:<{width}}  ← {', '.join(alts)}")

    session = get_session_stats()
    if session:
        lines.append("session:")
        # Sort by kind name so output is stable across runs.  The
        # empty-kind bucket is reserved for bare-call ambiguous-kind
        # errors that never resolved to a concrete handler — render
        # it as ``(no-kind, ambiguous)`` so the reader understands
        # why the row always has calls == errors (mcp-critic finding
        # N4).  Column width is computed from the actual labels (same
        # pattern as the alias block above) so the bucket label can
        # grow without dragging a magic number along with it.
        rows = [
            (name or "(no-kind, ambiguous)", session[name])
            for name in sorted(session)
        ]
        width = max(len(label) for label, _ in rows)
        for label, s in rows:
            lines.append(
                f"  {label:<{width}}  calls={s.calls}  errors={s.errors}  "
                f"last_cost={s.last_cost}"
            )
    else:
        lines.append("session: (no calls yet)")
    if STARTUP_WARNINGS:
        lines.append("startup warnings:")
        for w in STARTUP_WARNINGS:
            lines.append(f"  - {w}")
    else:
        lines.append("startup warnings: none")
    return "\n".join(lines)


def main() -> None:
    """Run the MCP server.

    Startup order (§10.1):

    1. Discover plugins via :func:`precis.registry._discover`.  A kind-name
       collision raises :class:`RegistryError` here.
    2. Parse ``PRECIS_KINDS`` and install the mask.  Grammar / alias /
       verb errors raise :class:`ConfigError` here.
    3. Hand off to FastMCP's stdio loop.

    Any fatal error in steps 1 or 2 prints one line to stderr and exits
    with code 2.  The agent-side MCP client then sees a clean launch
    failure it can surface to the operator.
    """
    try:
        _load_kinds_mask()
    except RegistryError as exc:
        # _discover() inside _load_kinds_mask could fail on kind collisions.
        print(f"precis-mcp: {exc}", file=sys.stderr)
        sys.exit(2)
    mcp.run()
