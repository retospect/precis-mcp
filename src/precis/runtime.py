"""Server runtime.

`PrecisRuntime` wraps the :class:`~precis.dispatch.Hub` (which owns
the registration table, store, embedder, and hint bus) with config
and dispatch logic. The MCP server (in `precis.server`) is a thin
FastMCP wrapper around it; tests dispatch directly without going
through MCP.

Lifecycle: the runtime owns the *close* of the store — callers do
``runtime.store.close()`` (or rely on a context manager wrapping the
runtime) to release the connection pool. The Hub merely *holds* the
store reference; whoever opened it is responsible for closing it.
"""

from __future__ import annotations

import inspect
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import (
    BadInput,
    Internal,
    NotFound,
    PrecisError,
    Unsupported,
    Upstream,
)
from precis.protocol import _ALL_VERBS, Handler, Verb
from precis.response import Response
from precis.store.types import Tag
from precis.utils import handle_registry
from precis.utils.search_merge import SearchHit, merge_and_render

if TYPE_CHECKING:
    from precis._pagination import PaginationCache
    from precis.hints import HintBus
    from precis.store import Store


def _new_pagination_cache() -> PaginationCache:
    """Late import so the runtime module load doesn't pull in
    threading / uuid eagerly."""
    from precis._pagination import PaginationCache

    return PaginationCache()


log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = _ALL_VERBS

# Wildcard token for cross-kind search. Equivalent to a comma-list
# of every kind whose ``KindSpec.supports_search_hits`` is True.
_CROSS_KIND_WILDCARD = "*"

# English aliases for the wildcard, accepted from agent callers who
# write the most natural shorthand they know. Every entry behaves
# identically to ``_CROSS_KIND_WILDCARD``: it expands to every
# search-hits-capable kind.
_CROSS_KIND_ALIASES: frozenset[str] = frozenset({"*", "", "all", "any", "*all*"})

#: Sentinel key used by `precis.server` to forward the MCP tool's
#: ``args={...}`` payload through to the dispatcher without colliding
#: with the explicit positional kwargs. The dispatcher pops it before
#: calling the handler method and validates the keys against the
#: method's accepted-kwargs whitelist.
_EXTRAS_KEY = "__extras__"

#: Preview length for an angle-spray hit. Short — the spray is a
#: breadth-first scan, not a read; the agent drills with ``get`` once a
#: neighbour looks worth chasing.
_ANGLE_PREVIEW_CHARS = 200

#: Address sigils that self-identify a kind, so ``get(id='¶handle')``
#: works without ``kind=`` (the draft skill documents exactly that).
#: Value is ``(kind, keep_sigil)``: ``¶`` stays in ``id=`` because the
#: draft handler matches on a leading ``¶``; ``§`` is stripped to the
#: bare ``slug~n`` the paper handler resolves. Distinct from the
#: ``kind:slug`` colon prefix (also self-identifying) handled alongside.
_SIGIL_KIND: dict[str, tuple[str, bool]] = {
    "¶": ("draft", True),
    "§": ("paper", False),
}

#: A draft chunk handle ``dc<chunk_id>`` and any trailing relative operator;
#: group 1 is the bare ``<chunk_id>`` (existence probe), group 2 the operator.
_DRAFT_DC_RE = re.compile(r"^dc(\d+)(.*)$")

#: Per-(kind, verb) recovery hints for "kind does not support verb" — a
#: generic "try get(kind=…)" is a dead-end when the right move is a
#: *different shape entirely*. Drafts are the case that bites: an agent
#: reaches for the universal ``link``/``tag`` verbs, but a draft's
#: cross-references live in prose (the autolinker backlinks them), and it
#: has no whole-ref tag axis. Teach the real move inline.
_VERB_REDIRECTS: dict[tuple[str, str], str] = {
    ("draft", "link"): (
        "drafts link via prose, not the link verb: edit the source chunk "
        "to embed a handle ref — edit(kind='draft', id='dc<src>', "
        "text='…existing… [dc<target>]') — and the autolinker materialises "
        "the related-to backlink for you."
    ),
    ("draft", "tag"): (
        "drafts have no whole-ref tag axis; tag the owning project todo "
        "instead, or use a glossary term / inline markup inside the prose."
    ),
}


#: Kind → skill alias map for the auto-discovery hint.
#: Used by :meth:`PrecisRuntime._maybe_add_skill_hint` when
#: ``precis-{kind}-help`` doesn't exist because the kind was renamed
#: but the skill kept its broader name (provider-rooted vs.
#: capability-rooted naming — see ADR 0030 + the rename slice).
_KIND_SKILL_ALIASES: dict[str, str] = {
    "perplexity-research": "precis-perplexity-help",
    "perplexity-reasoning": "precis-perplexity-help",
}


def _angle_excerpt(text: str) -> str:
    """One-line, length-capped preview of a snapped chunk's text."""
    flat = " ".join((text or "").split())
    if len(flat) <= _ANGLE_PREVIEW_CHARS:
        return flat
    return flat[: _ANGLE_PREVIEW_CHARS - 1].rstrip() + "…"


@dataclass
class PrecisRuntime:
    """Server-wide singleton: config + hub + dispatch logic.

    The :class:`~precis.dispatch.Hub` carries the dispatch table, the
    store (or ``None`` for stateless deployments), the embedder, and
    the hint bus. Tests and external callers reach those through the
    runtime's delegating properties (``runtime.hints``,
    ``runtime.store``) so the rename of internal field names didn't
    cascade through every test fixture.
    """

    config: PrecisConfig
    hub: Hub

    #: Parsed ``PRECIS_DEFAULT_TAGS`` tuple, resolved once at runtime
    #: build. Empty tuple when the env var is unset; the dispatch
    #: hook short-circuits in that case so unconfigured deployments
    #: pay zero per-call cost. Populated by :func:`build_runtime`;
    #: tests that construct a ``PrecisRuntime`` directly use the
    #: empty default unless they need to exercise the merge path.
    default_tags_resolved: tuple[str, ...] = field(default_factory=tuple)

    #: Process-local cache for chunked responses. Built fresh per
    #: runtime so test fixtures get a clean cache; production has
    #: exactly one runtime per worker so cursors survive across
    #: tool calls within the worker's lifetime.
    pagination: PaginationCache = field(default_factory=lambda: _new_pagination_cache())

    # ----- delegating properties ---------------------------------------

    @property
    def hints(self) -> HintBus:
        """Per-request hint collector. Delegates to ``self.hub.hints``."""
        return self.hub.hints

    @property
    def store(self) -> Store | None:
        """Connected store, or ``None`` for stateless deployments."""
        return self.hub.store

    @property
    def registry(self) -> Hub:
        """Backwards-compat alias for ``self.hub``.

        Kept so test fixtures that still spell ``runtime.registry``
        continue to work; new code should use ``runtime.hub`` (or
        the typed delegators on this class).
        """
        return self.hub

    def dispatch(self, verb: str, args: dict[str, Any]) -> str:
        """Run one verb call. Returns the rendered string for the agent.

        Errors are caught and rendered as text — they never propagate
        out (MCP expects a string return). Tests rely on this shape;
        callers that need the protocol-level error flag (e.g. the MCP
        tool wrappers) should use :meth:`dispatch_with_status`."""
        body, _is_error = self.dispatch_with_status(verb, args)
        return body

    def dispatch_with_status(self, verb: str, args: dict[str, Any]) -> tuple[str, bool]:
        """Run one verb call and report whether it errored.

        Returns ``(body, is_error)``. ``body`` is the same rendered text
        that :meth:`dispatch` returns; ``is_error`` is True when the
        call raised a :class:`PrecisError` or unhandled exception.
        Lets the MCP tool wrapper raise so FastMCP sets the protocol-
        level ``isError`` flag while keeping a single source of truth
        for the rendering. (MCP critic MAJOR — errors silently masked
        as content because ``isError`` was never set.)
        """
        with self.hub.request_scope():
            try:
                if verb not in _VERBS:
                    raise BadInput(
                        f"unknown verb: {verb}",
                        options=list(_VERBS),
                    )
                response = self._dispatch_inner(verb, dict(args))
                # Chunk over-large bodies so they don't blow the
                # MCP stdio frame. The pagination cache stashes
                # the tail under a cursor; the agent calls
                # ``more(cursor=...)`` to retrieve it.
                body, _cursor = self.pagination.split(self._render(response))
                return body, False
            except PrecisError as e:
                self._maybe_add_skill_hint(e, verb, args)
                return self.render_error(e), True
            except Exception as e:
                # F10: full traceback (with SQL fragments, Python
                # signatures, file paths) goes to the server log only.
                # The user-visible body keeps the exception *type* —
                # enough signal for the LLM to recover ("UndefinedTable
                # → run migrations") — but strips the message body that
                # leaks internals. Specific exception classes that have
                # a clean recovery story should be caught upstream and
                # converted to a typed PrecisError (Unavailable,
                # NotFound, etc.) before reaching this fallback.
                log.exception("internal error in %s", verb)
                return (
                    self.render_error(
                        Internal(
                            f"internal error in {verb}: "
                            f"{type(e).__name__} "
                            f"(see server log)"
                        )
                    ),
                    True,
                )

    def fetch_more(self, cursor: str) -> tuple[str, bool]:
        """Return the next page for a pagination cursor.

        Mirrors :meth:`dispatch_with_status`'s ``(body, is_error)``
        return shape so the ``more`` MCP tool's wrapper code is
        identical to the seven-verb wrappers. Returns
        ``(error_body, True)`` when the cursor is unknown or
        expired so the protocol-level ``isError`` flag flips.

        Recursive cursors: if the popped tail is itself oversized,
        :class:`PaginationCache` re-splits and embeds the new
        cursor in the returned body's footer.
        """
        tail = self.pagination.pop(cursor)
        if tail is None:
            err = BadInput(
                f"unknown or expired pagination cursor {cursor!r}",
                next=(
                    "Cursors are single-use and expire after a few "
                    "minutes. Re-issue the original call to get a "
                    "fresh page."
                ),
            )
            return self.render_error(err), True
        return tail, False

    def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
        """Orchestrate one verb call.

        Three responsibilities, each delegated to a helper:
          1. Cross-kind fan-out (``kind='*'`` / comma-list).
          2. Single-kind resolution including ``kind=`` defaulting
             for ``search`` calls that omit it.
          3. Handler invocation with extras whitelist + defaulted-kind
             error annotation.
        """
        # D1: accept URI-style ``id='kind:slug[~sel]'`` on input. Extract
        # the kind prefix into ``args['kind']`` (if not already set) and
        # leave the unprefixed identifier in ``args['id']``. Validation
        # that any explicit ``kind=`` matches the prefix lives in the
        # helper. Output stays kind-explicit — this is an *input*
        # convenience that mirrors the canonical ``kind:identifier``
        # grammar already used by ``link=`` / ``unlink=``.
        self._maybe_split_prefixed_id(args)
        kind = args.pop("kind", None)

        # Broad usability pass 2026-05-30 (#6): when an agent passes a
        # tag-shaped string as ``q=`` with no ``tags=`` filter, the
        # semantic search is statistically guaranteed to drown the
        # intended tagged refs in unrelated paper hits. Catch the
        # likely intent at the boundary and emit a deduplicated tip.
        if verb == "search":
            self._maybe_hint_tag_shaped_q(args)

        # Focus region: ``search(view='dreamable')`` is the salience
        # seed + its ANN ring (docs/design/dreaming.md, §view='dreamable'),
        # not the lexical+RRF path. Intercept before kind resolution —
        # it picks its own seed and cross-kind target set.
        if verb == "search" and str(args.get("view") or "").strip() == "dreamable":
            return self._dispatch_dreamable(kind, dict(args))

        # Backlog view: ``search(view='stubs')`` is the "papers we still
        # need to get" list — paper refs with an external id but no PDF
        # yet (docs/design/stubs-mcp-and-skill.md). Paper-only; ignores
        # ``q=``. Intercept before kind resolution.
        if verb == "search" and str(args.get("view") or "").strip() == "stubs":
            return self._dispatch_stubs(dict(args))

        # Angle spray: ``search`` with ``angle=`` or ``like=`` is the
        # diverse-cone semantic sampler (docs/design/dreaming.md), not
        # the lexical+RRF path. Intercept before kind resolution — it
        # owns its own seed resolution and cross-kind target set.
        if verb == "search" and ("angle" in args or "like" in args):
            return self._dispatch_angle(kind, dict(args))

        # Compact keywords-only TOON: ``search(view='keywords', ...)``
        # — discovery shape that returns just the keyword arrays for
        # the top hits (no preview text). Cross-kind by default (so
        # ``view='keywords'`` alone works as "what topics span the
        # corpus"); a specific ``kind=`` narrows the fan-out the same
        # way the cross-kind path does.
        if verb == "search" and str(args.get("view") or "").strip() == "keywords":
            return self._dispatch_cross_kind(
                kind if kind is not None else _CROSS_KIND_WILDCARD,
                dict(args),
            )

        # Cross-kind: ``kind='*'`` or comma-list. Other verbs keep the
        # single-kind contract — multi-kind get is meaningless and
        # multi-kind put would silently scatter writes.
        if verb == "search" and self._is_cross_kind_request(kind):
            return self._dispatch_cross_kind(kind, dict(args))

        # Resolve the kind. ``_resolve_kind`` may itself short-circuit
        # to a cross-kind merge when ``kind`` is None on a search call
        # and the corpus has >=2 search-supporting kinds.
        resolved_kind, kind_was_defaulted, cross_kind_resp = self._resolve_kind(
            verb, kind, args
        )
        if cross_kind_resp is not None:
            return cross_kind_resp

        handler = self._resolve_handler(resolved_kind, verb)
        return self._invoke_handler(
            handler,
            verb,
            kind=resolved_kind,
            kind_was_defaulted=kind_was_defaulted,
            args=args,
        )

    def _resolve_kind(
        self,
        verb: str,
        kind: Any,
        args: dict[str, Any],
    ) -> tuple[str, bool, Response | None]:
        """Pin ``kind`` to a single string, or short-circuit to cross-kind.

        Returns ``(kind, defaulted, cross_kind_response)``. When the
        third element is non-None, the caller must return it directly
        — the resolver decided to fan out across kinds because no
        single defensible default exists.

        Raises ``BadInput`` when ``kind`` is missing for a non-search
        verb, or when ``search`` has no usable default and no eligible
        cross-kind targets.
        """
        if kind is not None:
            return self._expand_kind_code(str(kind)), False, None

        if verb != "search":
            raise BadInput(
                "missing kind=",
                options=sorted(self.hub.kinds),
                next=self._missing_kind_hints(verb),
            )

        # ``search()`` without ``kind=`` defaults to cross-kind
        # fan-out across every search-hits-capable kind. Earlier
        # versions defaulted to the most-recently-touched single
        # kind as a 7B affordance, but a "what do I know about X"
        # query is the natural shape of an unscoped search and
        # the user should see hits from every corner of the corpus
        # — biasing toward the last-touched kind hid useful answers
        # in the other kinds. The MCP critic flagged the gap as a
        # design hole (gripe:3681 #2, 2026-05-01); this commit
        # closes it by reversing the precedence: cross-kind first,
        # single-kind fallback only when the hub has <2 eligible
        # kinds. (MCP critic MAJOR-C 2026-05-02.)
        search_kinds = [
            k
            for k in sorted(self.hub.kinds)
            if self.hub.handler_for(k).spec.supports_search
        ]
        cross_kind = self._cross_kind_kinds()
        if len(cross_kind) >= 2:
            return (
                _CROSS_KIND_WILDCARD,
                False,
                self._dispatch_cross_kind(_CROSS_KIND_WILDCARD, dict(args)),
            )

        # ≤1 search-hits-capable kind in this build: fall back to
        # the most-recently-touched kind so a single-kind deployment
        # still works without forcing the agent to spell ``kind=``.
        defaulted = self._default_search_kind(search_kinds)
        if defaulted is not None:
            return defaulted, True, None

        raise BadInput(
            "missing kind= and no defensible default available",
            options=search_kinds,
            next=(
                "pass kind=<one of the listed kinds>, or use "
                "kind='*' / kind='all' / kind='paper,memory' for cross-kind merge"
            ),
        )

    def _expand_kind_code(self, kind: str) -> str:
        """Accept a 2-char handle code as ``kind=`` (ADR 0038 §7).

        ``kind='dr'`` ≡ ``kind='draft'``, ``kind='pa'`` ≡ ``kind='paper'`` —
        the same registry that legends a handle now also resolves the code
        when it's passed as ``kind=``, so the ``kinds`` table is one legend
        for *reading* handles and *choosing* the kind.

        A literal that's already a registered kind always wins (never
        shadowed by a code), and only **record** codes expand: chunk codes
        (``dc``/``pc``) are address-only — you ``get``/``edit`` a chunk by
        its handle, never ``put(kind='dc', …)`` — so a code the registry
        flags ``is_chunk`` is left untouched (and falls through to the
        normal unknown-kind error). Anything that isn't a known code is
        returned verbatim."""
        if kind in self.hub.kinds:
            return kind
        try:
            resolved, is_chunk = handle_registry.kind_for_code(kind)
        except KeyError:
            return kind
        return resolved if not is_chunk else kind

    def _missing_kind_hints(self, verb: str) -> list[str]:
        """Recovery hints for a non-``search`` verb called without ``kind=``.

        Leads with the most-recently-touched kind — the agent almost
        always means to keep operating on whatever it was just working
        on, so ``edit({})`` / ``delete({})`` should bounce back a
        runnable ``edit(kind='draft', id=…)`` rather than a 30-item
        menu. This kills the empty-call retry loop a small model gets
        stuck in: a bare "missing kind=" with every kind listed gives it
        nothing to pick, so it re-fires the same empty call. Falls back
        to the generic "pick one" when the corpus is empty or the
        recency lookup fails. The per-verb help-skill pointer is still
        appended by :meth:`_maybe_add_skill_hint` after this.
        """
        recent: str | None = None
        if self.store is not None:
            try:
                recent = self.store.most_recent_kind()
            except Exception:  # pragma: no cover — store outage etc.
                log.exception("most_recent_kind lookup failed")
        hints: list[str] = []
        if recent is not None:
            hints.append(
                f"you were last working on kind={recent!r} — retry e.g. "
                f"{verb}(kind={recent!r}, id=…)"
            )
        hints.append("pass kind=<one of the listed options>")
        return hints

    def _resolve_handler(self, kind: str, verb: str) -> Handler:
        """Look up the handler for ``kind`` and verify it supports ``verb``.

        Raises:
            NotFound: ``kind`` is not registered at all (unknown name).
                Options carries only the verb-supporting kinds so an
                agent retrying against a suggested kind doesn't
                cascade into a second error (MCP critic MAJOR #12).
            Unsupported: handler is registered-but-disabled for this
                build (missing env var, missing optional dep), OR
                handler exists but does not implement ``verb``. The
                first variant names the missing precondition so the
                agent can route to the operator instead of guessing
                — see broad usability pass 2026-05-30 (#8). The
                second variant enumerates the verbs this kind *does*
                support so the recovery hint is sharp.
        """
        handler = self.hub.handler_for(kind)
        if handler is None:
            # Distinguish "registered-but-disabled in this build" from
            # "unknown kind". The hub records every gated-out kind in
            # ``loadabilities``; if ``kind`` is in there, the right
            # error class is ``Unsupported`` (the agent can't fix it
            # by retrying — the operator has to enable the kind),
            # and the breadcrumb should name the missing precondition.
            verdict = getattr(self.hub, "loadabilities", {}).get(kind)
            if verdict is not None and not verdict.loaded:
                reason = verdict.reason or "disabled"
                raise Unsupported(
                    f"kind {kind!r} is registered but disabled in this build "
                    f"({reason})",
                    next=(
                        "see get(kind='skill', id='precis-kinds-disabled-help') "
                        "and precis-overview Needs column"
                    ),
                )
            # Broad usability pass 2026-05-30 (#10): the previous
            # ``options:`` trailer silently filtered to kinds that
            # support the calling verb — agents reading the list
            # could conclude the omitted kinds didn't exist at all
            # (precis-help shows 17 total; the options here show
            # 12 for search). Name the filter in ``next:`` so a
            # reader knows the list is verb-scoped, not the full
            # registry.
            verb_kinds = self._kinds_for_verb(verb)
            # Round-2 picky N-2, 2026-05-30: when no kinds support
            # this verb in the current build (e.g. ``edit`` with no
            # file kinds wired — markdown/plaintext/tex/python all
            # need PRECIS_ROOT/PRECIS_PYTHON_ROOTS), the previous
            # breadcrumb said *"options above are kinds that support
            # verb='edit'"* — but no options were printed above, so
            # the agent was told to consult a list that wasn't there.
            # Distinguish the empty case explicitly.
            if verb_kinds:
                next_hint = (
                    f"options above are kinds that support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') for the complete "
                    f"kind table"
                )
            else:
                next_hint = (
                    f"no kinds in this build support verb={verb!r}; "
                    f"get(kind='skill', id='precis-help') lists every "
                    f"kind and the verbs each one accepts. The most "
                    f"likely cause is a missing env var "
                    f"(see get(kind='skill', id='precis-kinds-disabled-help'))."
                )
            # Federation hint: if any other process in the cluster
            # currently advertises this kind via ``kind_provider``,
            # name the hosts so the caller knows where to route. Pure
            # informational — the local process still rejects the
            # call. Skipped on stateless boots (no store) and on any
            # query error (kind_provider may be absent on a fresh DB
            # that hasn't run migration 0022 yet).
            route_hint: str | None = None
            store = getattr(self.hub, "store", None)
            if store is not None:
                try:
                    hosts = store.find_kind_providers(kind)
                except Exception:  # pragma: no cover - missing table / DB error
                    hosts = []
                if hosts:
                    route_hint = (
                        f"kind {kind!r} routes through host(s): {', '.join(hosts)}"
                    )
            if route_hint is not None:
                next_hint = f"{route_hint}; {next_hint}"
            raise NotFound(
                f"unknown kind: {kind}",
                options=verb_kinds,
                next=next_hint,
            )

        if not handler.spec.supports(verb):  # type: ignore[arg-type]
            verbs = [v for v in _VERBS if handler.spec.supports(v)]
            # ``options`` enumerates the supported verbs as the
            # recovery vocabulary; ``next`` gives a concrete
            # *callable* shape rather than re-listing the same
            # names so the LLM can copy-paste-execute. Pick ``get``
            # as the safest recovery suggestion when available —
            # every kind supports it and a minimum-arg ``get(kind=
            # X)`` either returns a list view (numeric/file kinds)
            # or fails with a kind-specific BadInput pointing at
            # the right next step (calc/math/web/etc. requiring
            # ``q=`` or ``id=``). Either way the LLM lands one
            # call closer to the answer.
            recovery = "get" if "get" in verbs else (verbs[0] if verbs else None)
            # A kind-specific redirect (e.g. how to "link" a draft) beats
            # the generic "try get(kind=…)" — it's the actual recovery the
            # agent needs, so lead with it.
            redirect = _VERB_REDIRECTS.get((kind, verb))
            if recovery is None and redirect is None:
                # Defensive: shouldn't happen — a kind with no
                # supported verbs would be useless. Drop the next:
                # trailer rather than render a meaningless one.
                raise Unsupported(
                    f"{kind} does not support {verb}",
                    options=verbs,
                )
            next_hints: list[str] = []
            if redirect is not None:
                next_hints.append(redirect)
            if recovery is not None:
                next_hints.append(f"try {recovery}(kind={kind!r})")
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=next_hints[0] if len(next_hints) == 1 else next_hints,
            )
        return handler

    def _invoke_handler(
        self,
        handler: Handler,
        verb: str,
        *,
        kind: str,
        kind_was_defaulted: bool,
        args: dict[str, Any],
    ) -> Response:
        """Call ``handler.<verb>`` with extras-whitelisted kwargs.

        ``args=`` extras forwarded by the MCP boundary are validated
        against the handler's signature *before* the call so
        ``**_kw`` doesn't swallow typos silently. Errors raised by
        the handler are annotated with ``(searched kind=…)`` when the
        caller omitted ``kind=`` and we defaulted, so failures stay
        traceable to the specific kind that was tried.
        """
        method = getattr(handler, verb)

        extras = args.pop(_EXTRAS_KEY, None)
        if extras:
            accepted = self._accepted_kwargs(method)
            # Handlers that opt into an explicit ``args: dict``
            # parameter (today: ``random.get`` — slug minting takes
            # ``len`` / ``alphabet`` inside ``args=``) want the whole
            # extras dict passed through, NOT flattened into top-level
            # kwargs. Without this branch, ``get(kind='random',
            # view='slug', args={'len': 4})`` errored with
            # ``args= keys ['len'] not accepted by random.get`` and the
            # error breadcrumb confusingly suggested using ``args`` or
            # ``view`` as args-dict keys (round-2 picky F-1). Detect
            # the opt-in by signature membership and forward extras
            # via the ``args`` kwarg unchanged.
            if "args" in accepted:
                args["args"] = dict(extras)
            else:
                unknown = self._unknown_extras(method, extras)
                if unknown:
                    accepted_kwargs = sorted(k for k in accepted if k not in ("args",))
                    raise BadInput(
                        f"args= keys {unknown!r} not accepted by {kind}.{verb}",
                        options=accepted_kwargs,
                        next=(
                            f"drop the unknown keys; {kind}.{verb} accepts "
                            f"top-level kwargs: {accepted_kwargs or '(none)'}"
                        ),
                    )
                args.update(extras)

        self._apply_default_tags_policy(handler, verb, args)

        # Strip None args so handlers see absence as missing.
        clean = {k: v for k, v in args.items() if v is not None}

        # F7: catch handler-signature-required kwargs that the caller
        # forgot, before ``method(**clean)`` raises a raw TypeError and
        # leaks Python signature internals through the [error:Internal]
        # envelope. Per-handler BadInput paths (e.g. NumericRefHandler.
        # link's "requires target=" check) still fire for *semantic*
        # requirements like "id must be paired with target"; this gate
        # only catches truly-missing keyword-only args with no default.
        missing = self._missing_required_kwargs(method, clean)
        if missing:
            accepted_kwargs = sorted(
                k for k in self._accepted_kwargs(method) if k != "args"
            )
            missing_str = ", ".join(f"{m}=" for m in missing)
            raise BadInput(
                f"{verb}(kind={kind!r}) requires {missing_str}",
                options=accepted_kwargs,
                next=f"get(kind='skill', id='precis-{verb}-help')",
            )

        try:
            response = method(**clean)
        except PrecisError as exc:
            if kind_was_defaulted:
                exc.cause = f"(searched kind={kind!r}) {exc.cause}"
            raise
        except Exception as exc:
            # Non-Precis exceptions get wrapped as ``Internal`` at
            # the dispatcher boundary; do the wrap here when the
            # kind was defaulted so the annotation lands on the
            # final rendered error rather than being lost in the
            # generic ``internal error: ...`` shape.
            if kind_was_defaulted:
                raise Internal(
                    f"(searched kind={kind!r}) internal error: {exc}"
                ) from exc
            raise

        if kind_was_defaulted:
            response = self._tag_defaulted_kind(response, kind)
        return response

    def _apply_default_tags_policy(
        self,
        handler: Handler,
        verb: str,
        args: dict[str, Any],
    ) -> None:
        """Apply ``PRECIS_DEFAULT_TAGS`` policy at the dispatch boundary.

        Behaviour matrix:

        - ``defaults`` empty (env unset): no-op for every verb.
        - ``handler.spec.note_like`` False: no-op for every verb.
          Ingested kinds (paper, patent), fetched caches (web,
          wolfram, youtube), and generators (oracle, random,
          skill) don't accumulate session-context tags.
        - verb ``put`` on a note-like kind: merge defaults into
          ``args['tags']`` (preserving caller's explicit-first
          ordering) and emit an info hint listing the additions.
          Existing tags are never duplicated.
        - verb ``tag`` on a note-like kind: emit a suggestion hint
          listing defaults missing from ``args.get('add')``. The
          set is **not** mutated — ``tag`` is the agent's explicit
          op, and silent mutation would surprise both the agent
          and the operator. The hint surfaces the suggestion so
          the agent can decide.
        - Any other verb (get, search, edit, delete, link): no-op.
          ``edit`` on note-like file kinds doesn't change tags via
          its core surface, so default-tag interaction is moot
          there. ``delete`` removes the ref entirely.

        Mutates ``args`` in place when applicable (``put`` only).
        Returns ``None``; observable effect is the merged ``tags``
        and any emitted hint visible at end-of-request.
        """
        defaults = self.default_tags_resolved
        if not defaults:
            return
        spec = handler.spec
        if not getattr(spec, "note_like", False):
            return

        from precis import default_tags as _dt
        from precis.hints import Hint

        if verb == "put":
            added = _dt.apply_to_put_args(args, defaults)
            if added:
                self.hub.emit_hint(
                    Hint(
                        text=("Added PRECIS_DEFAULT_TAGS to put: " + ", ".join(added)),
                        topic="default_tags.merged",
                    )
                )
        elif verb == "tag":
            missing = _dt.suggest_missing(args.get("add"), defaults)
            if missing:
                self.hub.emit_hint(
                    Hint(
                        text=(
                            "PRECIS_DEFAULT_TAGS suggested for tag add: "
                            + ", ".join(missing)
                        ),
                        topic="default_tags.suggested",
                    )
                )

    @staticmethod
    def _accepted_kwargs(method: Any) -> set[str]:
        """Return the set of explicit keyword names accepted by ``method``.

        ``self`` and any VAR_KEYWORD (``**kw``) catch-all are excluded
        — the catch-all is what we're working around. Used for the
        args= validation gate so ``**_kw`` no longer swallows typos.
        """
        sig = inspect.signature(method)
        return {
            name
            for name, p in sig.parameters.items()
            if p.kind
            in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name != "self"
        }

    @classmethod
    def _unknown_extras(cls, method: Any, extras: dict[str, Any]) -> list[str]:
        """Return the args= keys that aren't on the handler's signature."""
        accepted = cls._accepted_kwargs(method)
        return sorted(k for k in extras if k not in accepted)

    @staticmethod
    def _missing_required_kwargs(method: Any, clean: dict[str, Any]) -> list[str]:
        """Return required kwargs of ``method`` missing from ``clean``.

        A parameter is required when it has no default value AND is
        keyword-accessible (``POSITIONAL_OR_KEYWORD`` or ``KEYWORD_ONLY``).
        ``self`` and the magic ``args`` extras-passthrough parameter
        are excluded; ``**kw`` catch-alls don't count as required.

        Used by :meth:`_invoke_handler` to convert what would have been
        a raw ``TypeError: ... missing 1 required keyword-only
        argument: 'id'`` into a clean ``BadInput`` envelope (F7).
        """
        sig = inspect.signature(method)
        missing: list[str] = []
        for name, p in sig.parameters.items():
            if name in ("self", "args"):
                continue
            if p.kind not in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            ):
                continue
            if p.default is not inspect.Parameter.empty:
                continue
            if name not in clean:
                missing.append(name)
        return missing

    def _render(self, response: Response) -> str:
        out = [response.body]
        hints = self.hints.collect()
        for h in hints:
            out.append(f"\n[{h.level}] {h.text}")
        if response.cost:
            # Handlers return a fully-formatted cost string like
            # ``[cost: ~$0.0020 — cached]``. Don't prepend "— cost:" —
            # that produced the double-"cost:" trailer flagged by the
            # MCP critic ("— cost: [cost: ~$0.0020]").
            out.append(f"\n{response.cost}")
        return "".join(out)

    def _default_search_kind(self, search_kinds: list[str]) -> str | None:
        """Pick a sensible default kind for ``search()`` calls without one.

        Strategy:
          1. If the store has any live ref in a search-supporting
             kind, use the kind of the most recently updated one —
             this biases the default toward "what was the agent just
             working on?".
          2. Otherwise (no store, empty store), return None and let
             the caller raise the canonical missing-kind error.

        Returning None signals "I don't have a defensible default" so
        the dispatcher falls through to the explicit BadInput.
        """
        if self.store is None or not search_kinds:
            return None
        try:
            return self.store.most_recent_kind(kinds=search_kinds)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("most_recent_kind lookup failed")
            return None

    @staticmethod
    def _tag_defaulted_kind(response: Response, kind: str) -> Response:
        """Prepend a ``(searched kind=...)`` annotation to a response.

        Surfaced when the caller omitted ``kind=`` and the runtime
        defaulted to the most recently touched search-supporting
        kind. Naming it explicitly lets the caller see and steer
        the choice on retry.
        """
        annotated = f"(searched kind={kind!r})\n{response.body}"
        return Response(body=annotated, cost=response.cost)

    # ── tag-shaped q hint ──────────────────────────────────────────────

    # Tag tokens are unmistakably tag-shaped: either a closed-prefix
    # axis (UPPERCASE letters + colon + value, e.g. ``STATUS:done``),
    # a lowercase namespace + colon + value (``topic:co2-capture``),
    # or a kebab-case slug with multiple hyphens
    # (``exercise-mcp-throwaway``). Bare single words — `cells`,
    # `photocatalysis`, `pinned`, `topic` — are NOT tag-shaped for
    # this heuristic: the round-2 picky pass found that matching any
    # lowercase token fired the tip on basically every common
    # English search query (F-2). Requiring a colon or ≥1 hyphen
    # tightens the gate to actually-tag-looking strings.
    _TAG_SHAPED_Q_RE = re.compile(
        r"^(?:"
        # closed prefix: UPPERCASE letters/digits/_, colon, value chars
        r"[A-Z][A-Z0-9_]*:[A-Za-z0-9][\w.'-]*"
        # lowercase namespace + colon + value
        r"|[a-z][a-z0-9_]*:[\w.'-]+"
        # kebab-case slug with ≥1 hyphen
        r"|[a-z0-9]+(?:-[a-z0-9]+)+"
        r")$"
    )

    def _maybe_hint_tag_shaped_q(self, args: dict[str, Any]) -> None:
        """Emit a HintBus tip when ``q=`` looks like a tag string.

        Fires only when the call provides ``q=`` but no ``tags=`` —
        semantic search on a single tag-shaped token tends to match
        the substring against unrelated bodies (the broad usability
        pass saw ``q='exercise-mcp-throwaway'`` return paper-block
        hits about "exercise"). The hint is a HintBus tip rather
        than an error so the call still runs; agents that genuinely
        wanted the semantic match are not blocked.
        """
        q = args.get("q")
        if not isinstance(q, str):
            return
        token = q.strip()
        if not token or " " in token:
            return
        if args.get("tags"):
            return
        if not self._TAG_SHAPED_Q_RE.match(token):
            return
        from precis.hints import Hint

        # Round-2 picky N-3, 2026-05-30: dedup on the *query value*
        # rather than a static topic. The static-topic form
        # (``topic="search.tag_shaped_q"``) suppressed the hint on
        # every subsequent tag-shape call after the first — even when
        # the query was different, which is genuinely new information
        # the agent should see. Per-query dedup means the same query
        # repeated keeps suppressing (correct), but a different
        # tag-shape query re-fires (correct).
        self.hub.emit_hint(
            Hint(
                text=(
                    f"q={token!r} looks like a tag — semantic search "
                    "will match the substring against unrelated bodies. "
                    f"If you meant the tag filter, retry with "
                    f"tags=[{token!r}] (and pass q='...' as the topic "
                    "to rank within, or omit q= to list by recency)."
                ),
                topic=f"search.tag_shaped_q:{token}",
                cooldown=6,
            )
        )

    # ── cross-kind search ──────────────────────────────────────────────

    def _is_cross_kind_request(self, kind: Any) -> bool:
        """True iff ``kind`` asks for a cross-kind merge.

        Forms accepted (case-insensitive on aliases):

        - the wildcard ``'*'`` and its English aliases ``'all'`` /
          ``'any'`` (see :data:`_CROSS_KIND_ALIASES`);
        - any comma-list (``'paper,memory'`` or ``'paper, memory'``);
        - an explicit empty string (``''``) is treated like the
          wildcard for symmetry with MCP clients that send ``kind=""``.

        ``None`` does NOT count here — it goes through the
        single-kind defaulting path so callers that forgot
        ``kind=`` get the friendly "what were you working on"
        nudge before being escalated to cross-kind merge.
        """
        if not isinstance(kind, str):
            return False
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
            return True
        if "," in kind:
            return True
        return False

    def _cross_kind_kinds(self) -> list[str]:
        """Active kinds whose ``KindSpec.supports_search_hits`` is True.

        These are the kinds the cross-kind merge knows how to
        ingest.  Excluded handlers (calc, skill, python, perplexity,
        …) keep their single-kind ``search()`` contract; their
        absence from this list is by design.
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            spec = self.hub.handler_for(k).spec
            if spec.supports_search and spec.supports_search_hits:
                out.append(k)
        return out

    def _cross_kind_excluded_kinds(self) -> list[str]:
        """Active kinds searchable per-kind but opted out of cross-kind.

        These are kinds with ``supports_search=True`` but
        ``supports_search_hits=False`` — they carry per-kind result
        shapes (TOON tables with kind-specific columns, score-
        annotated skill rows, tag rows from a different table) that
        the cross-kind SearchHit substrate would have to flatten and
        lose information from. The wildcard cross-kind footer names
        them so the agent knows the kinds exist and where to look.
        Broad-pass finding #7.
        """
        out: list[str] = []
        for k in sorted(self.hub.kinds):
            spec = self.hub.handler_for(k).spec
            if spec.supports_search and not spec.supports_search_hits:
                out.append(k)
        return out

    def _resolve_cross_kind_request(self, kind: str) -> list[str]:
        """Expand ``kind`` into the concrete list of kinds to fan out to.

        Wildcard expands to every search-hits-capable kind.  Comma-
        lists are split, normalised (trim whitespace), and validated:
        unknown kinds and kinds that don't support cross-kind search
        raise ``BadInput`` with the recoverable list as ``options``.
        """
        eligible = self._cross_kind_kinds()
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
            return eligible

        requested = [tok.strip() for tok in kind.split(",")]
        requested = [t for t in requested if t]
        if not requested:
            return eligible

        bad = [t for t in requested if t not in eligible]
        if bad:
            registered = self.hub.kinds
            unknown = [t for t in bad if t not in registered]
            unsupported = [t for t in bad if t in registered]
            if unknown:
                raise BadInput(
                    f"unknown kind(s) in cross-kind request: {unknown!r}",
                    options=eligible,
                    next=(
                        "drop the unknown kind(s); cross-kind merge accepts "
                        f"the listed kinds, or use kind={_CROSS_KIND_WILDCARD!r} for all"
                    ),
                )
            if unsupported:
                raise BadInput(
                    (f"kind(s) do not support cross-kind search: {unsupported!r}"),
                    options=eligible,
                    next=(
                        "the listed kinds opt into the merge via "
                        "supports_search_hits; the others keep their "
                        "single-kind search() contract - call them one at a time"
                    ),
                )
        # Preserve caller order (first-occurrence) so the output
        # rendering deterministically reflects what the agent
        # asked for.  Dedup while preserving order.
        seen: set[str] = set()
        out: list[str] = []
        for t in requested:
            if t not in seen:
                seen.add(t)
                out.append(t)
        return out

    # Default dream-target kinds for the angle spray when the caller
    # doesn't pin one (docs/design/dreaming.md, §Scope). ``draft`` is
    # in here so the dreamer wanders the project write-up we're actively
    # building — the live prose we think about most, not just the frozen
    # corpus (paper) and crystallised thoughts (memory).
    _ANGLE_DEFAULT_KINDS: tuple[str, ...] = ("paper", "memory", "draft")

    # Focus-region size when the caller doesn't pin ``n=`` — wide
    # enough to read a theme, small enough to stay in one prompt.
    _DREAMABLE_DEFAULT_N: int = 12

    # Default backlog size for ``search(view='stubs')`` — enough to
    # scan in one prompt without dumping the whole queue.
    _STUBS_DEFAULT_N: int = 25

    def _dispatch_stubs(self, args: dict[str, Any]) -> Response:
        """The required-papers backlog: ``search(view='stubs')``.

        Lists ``paper`` refs with an external identifier (DOI / arXiv /
        S2) registered but no PDF yet — the "papers we still need to
        get" queue the chase worker and the dream ``acquire`` tool both
        feed (docs/design/stubs-mcp-and-skill.md). Paper-only; ``q=`` is
        ignored (the view *is* the filter). ``n=`` / ``page_size=`` cap
        the row count; newest stub first. Read-only — surfacing the
        backlog does not touch salience or the fetch pipeline.
        """
        from precis.utils.next_block import render_next_section

        store = self.hub.store
        if store is None:
            raise Unsupported("view='stubs' needs a store-backed deployment")

        n = int(args.get("n") or args.get("page_size") or self._STUBS_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='stubs', n=25)")

        rows = store.stub_backlog(limit=n)
        if not rows:
            return Response(
                body=(
                    "no stub papers — every paper has a PDF or no external "
                    "identifier to fetch one with. Nothing to acquire."
                )
            )

        lines = [f"papers we still need to get ({len(rows)} shown):", ""]
        for r in rows:
            ident = r["identifier"] or "(no external id)"
            cite = r["cite_key"] or f"ref {r['ref_id']}"
            lines.append(f"  ref {r['ref_id']}  {ident}  [{cite}]")
            lines.append(f"      {r['state']}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='paper', id={rows[0]['ref_id']})",
                    "open a stub to see what links to it",
                ),
                (
                    "search(kind='paper', tags=['DREAM:acquire'])",
                    "just the papers a dream wanted",
                ),
                (
                    "get(kind='skill', id='precis-stubs-help')",
                    "how the backlog works",
                ),
            ]
        )
        return Response(body=body)

    def _dispatch_dreamable(self, kind: Any, args: dict[str, Any]) -> Response:
        """The focus region: the salience seed + its ANN neighbourhood.

        ``search(view='dreamable')`` — pick the most-due seed
        (``argmax(last_seen - last_dreamt)`` over target kinds), return
        its nearest neighbourhood, and **stamp ``last_dreamt`` on every
        surfaced chunk** so the region rotates out and a different one
        tops the next run (docs/design/dreaming.md, §view='dreamable').
        No sub-clustering — the cosine ring *is* the region. Unlike the
        angle spray this does **not** bump salience: looking at a region
        counts as *dreaming* it, not as an external access.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("view='dreamable' needs a store-backed deployment")

        n = int(args.get("n") or self._DREAMABLE_DEFAULT_N)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(view='dreamable', n=12)")

        kinds = self._angle_target_kinds(kind)
        seed_id, region = store.dreamable_region(kinds=kinds, n=n)

        # The rotation: surfacing a region IS dreaming it. Stamp the
        # seed and every surfaced chunk so the next run picks elsewhere.
        touched = [block.id for block, _ref, _score in region]
        if seed_id is not None and seed_id not in touched:
            touched.append(seed_id)
        if touched:
            store.touch_last_dreamt(touched)

        stream = [
            SearchHit(
                score=cosine,
                kind=ref.kind,
                title=ref.title or (ref.slug or f"#{ref.id}"),
                preview=_angle_excerpt(block.text),
                slug=ref.slug,
                pos=block.pos if block.pos is not None and block.pos >= 0 else None,
                ref_id=ref.id,
                dedupe_key=f"{ref.kind}:{block.id}",
            )
            for block, ref, cosine in region
        ]
        # ``query=`` populates the rendered header (``... for 'X'``);
        # passing the literal view name read as if the agent had typed
        # ``q='dreamable'`` (broad-pass usability finding R2#3). Use a
        # label that names what the view actually picked.
        seed_label = (
            f"most-due seed ref_id={seed_id}"
            if seed_id is not None
            else "most-due seed"
        )
        return merge_and_render(
            [stream],
            page_size=n,
            query=seed_label,
            header_noun="region member",
            mode="priority",
            show_label=True,
            empty_body="no dreamable region — corpus has no embedded target chunks yet",
        )

    def _dispatch_angle(self, kind: Any, args: dict[str, Any]) -> Response:
        """Diverse-cone semantic spray: ``n`` items at cosine ``angle``.

        ``search(q=... | like=<id>, angle=<float>, n=<int>)`` — seed by
        a query string or an existing item's stored vector, then return
        ``n`` mutually-distinct items at the requested cosine from the
        seed (docs/design/dreaming.md, §The ``angle`` spray). Card
        chunks are valid snap targets so a memory's only embedding is
        reachable. Surfacing bumps salience (suppressed for the dreamer).
        """
        store = self.hub.store
        if store is None:
            raise Unsupported("angle search needs a store-backed deployment")

        angle = self._angle_float(args.get("angle", 1.0))
        n = int(args.get("n") or args.get("top_k") or 8)
        if n < 1:
            raise BadInput("n must be >= 1", next="search(q='...', angle=0.5, n=8)")

        kinds = self._angle_target_kinds(kind)
        seed_vec, seed_chunk_id, label = self._resolve_angle_seed(
            args.get("like"), args.get("q")
        )

        exclude = [seed_chunk_id] if seed_chunk_id is not None else None
        hits = store.angle_neighbours(
            seed_vec, angle=angle, n=n, kinds=kinds, exclude_chunk_ids=exclude
        )
        # Surfacing is an external access → heat the snapped chunks
        # (no-op inside as_dream_actor); the dreamer stamps last_dreamt
        # itself at run end.
        store.bump_salience([block.id for block, _ref, _score in hits])

        stream = [
            SearchHit(
                score=cosine,
                kind=ref.kind,
                title=ref.title or (ref.slug or f"#{ref.id}"),
                preview=_angle_excerpt(block.text),
                slug=ref.slug,
                pos=block.pos if block.pos is not None and block.pos >= 0 else None,
                ref_id=ref.id,
                dedupe_key=f"{ref.kind}:{block.id}",
            )
            for block, ref, cosine in hits
        ]
        return merge_and_render(
            [stream],
            page_size=n,
            query=label,
            header_noun="neighbour",
            mode="priority",
            show_label=True,
            empty_body=f"no neighbours at angle={angle:g} from {label}",
        )

    @staticmethod
    def _angle_float(raw: Any) -> float:
        try:
            angle = float(raw)
        except (TypeError, ValueError) as exc:
            raise BadInput(
                f"angle must be a number in [-1, 1], got {raw!r}",
                next="search(q='...', angle=0.5)  # 1=near, 0=orthogonal, -1=opposite",
            ) from exc
        if not -1.0 <= angle <= 1.0:
            raise BadInput(
                f"angle must be in [-1, 1], got {angle}",
                next="1=same direction, 0=orthogonal, -1=opposite pole",
            )
        return angle

    def _angle_target_kinds(self, kind: Any) -> tuple[str, ...]:
        """Resolve the snap-target kinds for an angle spray.

        ``None`` / ``'*'`` / ``'all'`` → the default dream targets
        (paper+memory). A comma-list or single kind is honoured as-is
        so a caller can spray within one corpus.
        """
        if kind is None:
            return self._ANGLE_DEFAULT_KINDS
        if str(kind).strip().lower() in _CROSS_KIND_ALIASES:
            return self._ANGLE_DEFAULT_KINDS
        parsed = tuple(tok.strip() for tok in str(kind).split(",") if tok.strip())
        return parsed or self._ANGLE_DEFAULT_KINDS

    def _resolve_angle_seed(
        self, like: Any, q: Any
    ) -> tuple[list[float], int | None, str]:
        """Return ``(seed_vec, seed_chunk_id, label)`` for the spray.

        ``like='kind:id[~sel]'`` seeds from an existing item's stored
        vector (ref-level → its card/head chunk; block-level → that
        chunk) and reports the seed chunk so it can be excluded from
        the results. ``q=`` embeds the query string. Exactly one is
        required.
        """
        store = self.hub.store
        assert store is not None  # guarded by caller
        if like:
            from precis.handlers._link_target import parse_link_target

            tgt = parse_link_target(str(like), store=store)
            if tgt.pos is None:
                chunk_id = store.seed_chunk_for_ref(tgt.ref_id)
            else:
                block = store.get_block(tgt.ref_id, pos=tgt.pos)
                chunk_id = block.id if block is not None else None
            vec = store.get_chunk_vector(chunk_id) if chunk_id is not None else None
            if vec is None:
                raise BadInput(
                    f"like={like!r} has no embedding yet",
                    next="run `precis worker` to embed it, or seed with q=",
                )
            return vec, chunk_id, f"like={like}"

        if isinstance(q, str) and q.strip():
            embedder = getattr(self.hub, "embedder", None)
            if embedder is None:
                raise Unsupported(
                    "angle search by q= needs an embedder; "
                    "seed with like='kind:id' instead"
                )
            # Angle search is purely semantic — there's no lexical leg
            # to fall back to — so a failing embedder must surface as a
            # clean Upstream rather than a bare 500.
            try:
                return embedder.embed_one(q), None, q
            except Upstream:
                raise
            except Exception as exc:
                raise Upstream(
                    "angle search could not embed q=",
                    next="retry shortly (embedder may be warming)",
                ) from exc

        raise BadInput(
            "angle search requires q= or like=",
            next="search(q='topic', angle=0.5)  or  search(like='memory:42', angle=0)",
        )

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        """Fan out a search across multiple kinds and RRF-fuse the streams.

        Each handler's ``search_hits(q=..., top_k=...)`` is called
        with the same arguments; per-handler exceptions degrade to
        empty streams (logged) so one slow / broken kind doesn't
        crash the whole query.  Final ranking is reciprocal-rank
        fusion via ``merge_and_render(mode='rrf')``.
        """
        q = args.get("q")
        tags_in = args.get("tags")
        # Tags-only cross-kind path (R2#9 — finding "every throwaway
        # across kinds" used to force 4 single-kind calls). The lexical
        # leg doesn't need an embedder, so we can answer this in one
        # store query — list_refs accepts a kind=None tag filter and
        # returns a kind-mixed result set the renderer trivially flattens.
        if q is None or not (isinstance(q, str) and q.strip()):
            if tags_in:
                return self._dispatch_cross_kind_tags_only(kind, args)
            raise BadInput(
                "cross-kind search requires q= or tags=",
                next=(
                    f"search(kind={kind!r}, q='your query') - cross-kind merge "
                    "fans out via search_hits, which needs a non-empty query; "
                    f"or search(kind={kind!r}, tags=['<tag>']) for a tags-only sweep"
                ),
            )
        top_k = int(args.get("top_k") or 10)
        tags = args.get("tags")
        exclude = args.get("exclude")
        mode = args.get("mode")
        mode_lexical = isinstance(mode, str) and mode.strip().lower() == "lexical"

        kinds = self._resolve_cross_kind_request(kind)
        if not kinds:
            raise BadInput(
                "no kinds available for cross-kind search",
                next=(
                    "this build has no kinds that opt into cross-kind merge; "
                    "use single-kind search() against the kind you want"
                ),
            )

        # Canonicalise the tag filter once at the dispatch boundary.
        # ``Tag.normalize_filter`` round-trips each tag through
        # ``parse_strict`` (validates vocabulary, rejects typos) and
        # returns its canonical string form so the post-filter below
        # can match by string equality regardless of namespace.
        # ``kind=None`` because cross-kind doesn't know which axes
        # apply where — per-kind axis enforcement lives on writes.
        normalized_tags: list[str] | None = None
        if tags:
            normalized_tags = Tag.normalize_filter(tags, kind=None)

        # Build the kwargs dict once so the per-kind retry chain
        # below can drop unknown kwargs without re-listing them.
        # ``exclude=`` is fanned out to every kind (the per-handler
        # ``fetch_ref_ids_by_slugs`` filters by kind, so a paper
        # slug in the list silently no-ops on memory etc.). Kinds
        # that don't accept the kwarg fall through to the
        # ``TypeError`` retry below.
        #
        # ``query_vec=`` is computed once here and threaded into every
        # block-level handler that opts in. Without this the cross-
        # kind fan-out paid one embed_one(q) per kind — for kind='*'
        # over seven block-level handlers that's seven identical
        # transformer forward passes on the same query string. Kinds
        # whose ``search_hits`` signature doesn't accept ``query_vec=``
        # fall through the same TypeError-degradation chain as
        # ``exclude=`` / ``tags=``.
        base_kwargs: dict[str, Any] = {"q": q, "top_k": top_k}
        if mode is not None:
            base_kwargs["mode"] = mode
        semantic_degraded = False
        # ``mode='lexical'`` skips the embed entirely — the deterministic
        # keyword fan-out (and the right move when the embedder is down).
        embedder = None if mode_lexical else getattr(self.hub, "embedder", None)
        if embedder is not None:
            try:
                base_kwargs["query_vec"] = embedder.embed_one(q)
            except Upstream as exc:
                # Embedder is warming (or upstream is unavailable).
                # Falling back to lexical-only is the right runtime
                # move, but we must SURFACE the degraded state so the
                # agent doesn't read "no matches" as a definitive
                # answer. Round-2 picky finding R2#2: silent fallback
                # made `search(q='photocatalysis')` return zero hits
                # while `search(kind='paper', q='...')` raised
                # Upstream — same underlying state, two different
                # signals.
                from precis.hints import Hint

                self.hub.emit_hint(
                    Hint(
                        text=(
                            f"cross-kind semantic search degraded to "
                            f"lexical-only: {exc.cause}. Some matches "
                            "may be missing; retry shortly for the "
                            "full semantic fan-out."
                        ),
                        topic="search.embedder_warming",
                        cooldown=3,
                    )
                )
                log.info(
                    "cross-kind: embedder unavailable; falling back to lexical (%s)",
                    exc.cause,
                )
                semantic_degraded = True
            except Exception:
                # An embed failure here shouldn't kill the whole
                # cross-kind search — fall back to per-kind embed
                # (or lex-only when the kind's embedder is also
                # unavailable).
                log.exception("cross-kind: query embed failed; falling back per-kind")
        if tags:
            base_kwargs["tags"] = tags
        if exclude:
            base_kwargs["exclude"] = exclude

        streams: list[list[SearchHit]] = []
        per_kind_counts: list[tuple[str, int]] = []
        for k in kinds:
            handler = self.hub.handler_for(k)
            if handler is None:
                per_kind_counts.append((k, 0))
                continue
            hits = self._cross_kind_invoke_search_hits(handler, k, base_kwargs)
            if hits is None:
                per_kind_counts.append((k, 0))
                continue
            # Defensive post-filter: handler search_hits
            # implementations have inconsistent ``tags=`` support
            # (numeric-ref kinds honour the filter via
            # ``Tag.normalize_filter`` + SQL; most slug-ref and
            # block-level handlers accept it via ``**_kw`` and
            # silently ignore it). The dispatcher re-applies the
            # filter here so a caller who passed
            # ``tags=['workspace']`` never sees cross-kind hits
            # from kinds that can't carry that tag.
            if normalized_tags:
                hits = self._filter_hits_by_tags(list(hits), normalized_tags)
            hits_list = list(hits)
            per_kind_counts.append((k, len(hits_list)))
            streams.append(hits_list)

        # When the embedder was unavailable for this turn, change the
        # empty-result wording from "no matches" to a partial-result
        # headline — semantic side genuinely couldn't run, so claiming
        # zero matches is overconfident. Broad-pass finding #13.
        if semantic_degraded:
            empty_body = (
                f"no lexical matches across {', '.join(kinds)} for {q!r}; "
                "semantic search degraded to lexical-only this turn — "
                "retry in ~30s for ranked semantic fan-out"
            )
        else:
            empty_body = f"no matches across {', '.join(kinds)} for {q!r}"
        # ``view='keywords'`` swaps the renderer for a compact
        # id|kind|keywords TOON table — no preview text. Same fan-out
        # / dedup / RRF; only the projection differs.
        view = str(args.get("view") or "").strip()
        output_shape: Literal["keywords", "toon"] = (
            "keywords" if view == "keywords" else "toon"
        )
        response = merge_and_render(
            streams,
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=empty_body,
            output_shape=output_shape,
        )

        # Round-2 picky F-8: prepend a per-kind hit-count line under the
        # headline so the agent can see which kinds contributed and
        # which returned empty. Without this, a comma-list call
        # ``search(kind='paper,memory', q='...')`` that surfaces only
        # paper hits looks identical to a single-kind paper call —
        # the agent has no way to know memory was searched and empty.
        if len(per_kind_counts) >= 2:
            breakdown = ", ".join(f"{k}: {n}" for k, n in per_kind_counts)
            lines = response.body.splitlines()
            if lines:
                lines.insert(1, f"_(per kind: {breakdown})_")
                response = Response(body="\n".join(lines), cost=response.cost)

        # Broad-pass finding #7: when cross-kind ran with the wildcard
        # (kind=None or kind='*'), surface the kinds that have search
        # but opt out of search_hits (skill, citation, finding, tag —
        # each by design, because their per-kind renderer carries
        # structure the SearchHit shape would flatten). Lets the agent
        # know those kinds exist + were skipped on purpose, and where
        # to look. Only emit for wildcards; an explicit comma-list is
        # a deliberate choice and doesn't need the breadcrumb.
        if kind.strip().lower() in _CROSS_KIND_ALIASES:
            excluded = self._cross_kind_excluded_kinds()
            if excluded:
                lines = response.body.splitlines()
                tip = (
                    "_(not included: "
                    + ", ".join(sorted(excluded))
                    + " — search each kind explicitly for the "
                    "richer per-kind view)_"
                )
                if len(per_kind_counts) >= 2 and len(lines) >= 2:
                    lines.insert(2, tip)
                elif lines:
                    lines.insert(1, tip)
                else:
                    lines.append(tip)
                response = Response(body="\n".join(lines), cost=response.cost)

        return response

    def _dispatch_cross_kind_tags_only(
        self, kind: Any, args: dict[str, Any]
    ) -> Response:
        """Tags-only fan-out — one store query, no embedder needed.

        Use case: "find every ref tagged ``topic:exercise-mcp-throwaway``
        across all the kinds I created throwaways on" — previously
        forced four single-kind calls. Now a single
        ``search(tags=['<tag>'])`` (kind omitted) returns the kind-mixed
        set in one call. Restricted to live numeric-ref kinds (memory,
        todo, gripe, flashcard, conv, finding, job, pres) — the slug-ref and
        cache-backed kinds don't share the same tag-indexing path.
        """
        store = self.hub.store
        if store is None:
            raise Unsupported(
                "tags-only cross-kind search needs a store-backed deployment"
            )
        tags = args.get("tags") or []
        # ``Tag.normalize_filter(kind=None)`` validates each tag against
        # the registered vocabulary (typo-rejection on closed axes) and
        # returns canonical string form so the store filter matches by
        # string equality. Mirrors the validation in the q= path above.
        normalized = Tag.normalize_filter(tags, kind=None)
        page_size = max(1, int(args.get("page_size") or 10))

        allowed_kinds = set(self._resolve_cross_kind_request(kind))
        # Pull enough to cover the requested page after the kind filter.
        # 5x oversample is generous — the store's tag filter is fast and
        # the typical "find my throwaways" call returns < 20 rows.
        raw = store.list_refs(
            kind=None,
            tags=normalized,
            limit=page_size * 5,
        )
        refs = [r for r in raw if r.kind in allowed_kinds][:page_size]

        if not refs:
            body = f"no refs match tags={normalized!r} across {sorted(allowed_kinds)}"
        else:
            head = (
                f"# {len(refs)} ref{'s' if len(refs) != 1 else ''} "
                f"tagged {normalized!r} (kind-mixed, by recency)"
            )
            lines = [head]
            for r in refs:
                title = r.title or r.slug or f"#{r.id}"
                lines.append(f"{r.kind}:{r.id}  {title}")
            body = "\n".join(lines)
        return Response(body=body)

    def _cross_kind_invoke_search_hits(
        self,
        handler: Handler,
        kind: str,
        base_kwargs: dict[str, Any],
    ) -> list[SearchHit] | None:
        """Call ``handler.search_hits`` with progressive-degradation retries.

        Handlers' ``search_hits`` signatures vary in which optional
        kwargs they accept (``tags=``, ``exclude=``, …). Rather than
        introspect the signature ahead of time, we try the full
        kwargs set first and drop unknown kwargs on ``TypeError``,
        most-recent-addition first (``exclude``, then ``tags``). Any
        non-TypeError exception is logged and degraded to ``None``
        so one slow / broken kind doesn't crash the whole query.
        """
        # Try the full set first.
        try:
            return list(handler.search_hits(**base_kwargs))
        except TypeError:
            pass
        except Exception:
            log.exception("cross-kind search_hits failed for %s", kind)
            return None

        # Drop ``exclude=`` (most recent kwarg addition) and retry.
        if "exclude" in base_kwargs:
            without_exclude = {k: v for k, v in base_kwargs.items() if k != "exclude"}
            try:
                return list(handler.search_hits(**without_exclude))
            except TypeError:
                pass
            except Exception:
                log.exception("cross-kind search_hits failed for %s", kind)
                return None
        else:
            without_exclude = base_kwargs

        # Drop ``tags=`` too (oracle doesn't filter by tag).
        if "tags" in without_exclude:
            minimal = {k: v for k, v in without_exclude.items() if k != "tags"}
            try:
                return list(handler.search_hits(**minimal))
            except Exception:
                log.exception("cross-kind search_hits failed for %s", kind)
                return None

        # Already minimal (q + top_k only) and still failing.
        log.exception("cross-kind search_hits failed for %s", kind)
        return None

    def _filter_hits_by_tags(
        self,
        hits: list[SearchHit],
        required_tag_strings: list[str],
    ) -> list[SearchHit]:
        """Drop hits whose refs don't carry every required tag.

        Correctness backstop for cross-kind fan-out. The fan-out
        passes ``tags=`` to each handler's ``search_hits``, but most
        handlers' signatures take ``**_kw`` and silently ignore
        unknown kwargs — so ``tags=['workspace']`` was effectively a
        no-op for every kind except numeric refs. That made the
        advertised ``search(tags=['workspace'])`` scope-to-workspace
        filter return hits from kinds (``think``, ``websearch``,
        …) that can't carry the tag at all.

        This method runs after every stream is collected: for each
        hit it resolves ``ref_id`` (looking up via ``slug`` when
        needed), fetches the ref-level tag set, and keeps only hits
        whose tags are a superset of the required ones. Comparison
        is on the canonical string form (``__str__``) so a flag
        ``workspace`` and an open tag ``workspace`` are treated as
        equivalent matches — there's no practical reason the agent
        should care about the namespace of the tag they're
        filtering by.

        Cost: one extra ``tags_for`` DB hit per surviving hit.
        Acceptable for this axis (tag filters are relatively rare
        in cross-kind search; correctness dwarfs throughput).
        """
        if self.hub.store is None or not required_tag_strings:
            return hits
        required = set(required_tag_strings)
        kept: list[SearchHit] = []
        for hit in hits:
            ref_id = hit.ref_id
            if ref_id is None and hit.slug:
                ref = self.hub.store.get_ref(kind=hit.kind, id=hit.slug)
                if ref is None:
                    continue
                ref_id = ref.id
            if ref_id is None:
                # Producer provided neither ref_id nor slug — can't
                # check tags. Drop rather than leak an unfiltered
                # hit.
                continue
            tags_have = self.hub.store.tags_for(ref_id)
            have_strings = {str(t) for t in tags_have}
            if required.issubset(have_strings):
                kept.append(hit)
        return kept

    def _kinds_for_verb(self, verb: str) -> list[str]:
        """Return the active kinds whose KindSpec supports ``verb``.

        Used by error paths so an "unknown kind" reply on a search
        request lists only kinds that *do* support search — agents
        that retry against the suggested options shouldn't cascade
        into a second error. (MCP critic MAJOR #12.)
        """
        return [
            k
            for k in sorted(self.hub.kinds)
            if self.hub.handler_for(k).spec.supports(verb)  # type: ignore[arg-type]
        ]

    def _infer_sigil_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a leading address sigil (``¶`` → draft,
        ``§`` → paper). ``¶`` is kept in ``id`` (the draft handler matches
        on it); ``§`` is stripped to the bare ``slug~n`` paper resolves.

        Leaves ``id`` alone when the implied kind isn't in this build
        (fail downstream with a real "no such kind", not a silent
        mis-route). Raises on an explicit ``kind=`` that contradicts the
        sigil, mirroring the colon-prefix conflict check.
        """
        kind, keep_sigil = _SIGIL_KIND[ident[:1]]
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind not in live_kinds:
            return
        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != kind:
            raise BadInput(
                f"id={ident!r} sigil implies kind={kind!r}, "
                f"conflicts with kind={existing_kind!r}",
                next=f"drop kind= — id={ident!r} already names the kind",
            )
        args["kind"] = kind
        if not keep_sigil:
            args["id"] = ident[1:]

    def _infer_slug_kind(self, args: dict[str, Any], ident: str) -> None:
        """Pin ``kind`` from a bare slug address (no ``kind:`` prefix, no
        sigil) when the slug uniquely identifies one live ref — e.g.
        ``wu22c~312`` → ``kind='paper'``. The ``~selector`` / ``/view``
        suffix stays on ``id`` for the handler to parse. No-op when the
        store is absent, ``kind=`` was already given, or the base slug is
        ambiguous / unknown (so a non-slug id falls through to the normal
        missing-kind error unchanged)."""
        if args.get("kind") is not None or self.store is None:
            return
        base = re.split(r"[~/?]", ident, maxsplit=1)[0].strip()
        if not base:
            return
        try:
            kind = self.store.kind_for_slug(base)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("kind_for_slug lookup failed")
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if kind is not None and kind in live_kinds:
            args["kind"] = kind

    def _maybe_route_draft_chunk(self, args: dict[str, Any], ident: str) -> bool:
        """ADR 0036: route a draft chunk handle ``dc<id>`` (optionally with a
        relative operator ``^`` / ``+N`` / ``-lo..hi``) to the draft handler,
        which resolves it (drafts have no slug, so they can't go through the
        generic ``slug~ord`` chunk-handle rewrite). Confirms the base chunk
        exists so a bogus ``dc999`` falls through to a clean not-found.
        Returns ``True`` if it routed."""
        if self.store is None:
            return False
        m = _DRAFT_DC_RE.match(ident)
        if m is None:
            return False
        # A trailing operator must be a valid relative handle, else this is
        # not a draft address (``dc42garbage`` falls through).
        if m.group(2) and handle_registry.parse_relative(ident) is None:
            return False
        explicit = args.get("kind")
        if explicit is not None and explicit != "draft":
            return False
        try:
            if self.store.get_draft_chunk("dc" + m.group(1)) is None:
                return False
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("draft chunk routing lookup failed")
            return False
        args["kind"] = "draft"
        args["id"] = ident
        return True

    def _maybe_infer_kind_from_relative(self, args: dict[str, Any], ident: str) -> bool:
        """ADR 0036 relative navigation: route ``pc10+1`` / ``pc10-2..3``.

        Resolves the relative chunk handle to its kind + the per-kind chunk
        selector (e.g. ``slug~ord`` for a paper) and rewrites ``args`` so the
        existing per-kind ``get`` renders the target with no change. Returns
        ``True`` if it routed a relative handle, ``False`` otherwise (not a
        relative handle, unresolvable, out of range, or an explicit ``kind=``
        that disagrees) so the caller falls through untouched.
        """
        if self.store is None:
            return False
        try:
            resolved = self.store.resolve_relative(ident)
        except Exception:  # pragma: no cover — store outage etc.
            log.exception("resolve_relative lookup failed")
            return False
        if resolved is None:
            return False
        kind, selector = resolved
        explicit = args.get("kind")
        if explicit is not None and explicit != kind:
            return False
        args["kind"] = kind
        args["id"] = selector
        return True

    def _maybe_infer_kind_from_handle(self, args: dict[str, Any], ident: str) -> bool:
        """ADR 0036 surface dispatch: route a universal handle.

        If ``ident`` is a well-formed, resolvable record handle, set
        ``args['kind']`` from its 2-char type code and rewrite
        ``args['id']`` to the per-kind public id (slug or ``str(ref_id)``)
        — so the existing per-kind handler resolves it with no change. A
        record handle may carry a trailing chunk/view selector
        (``pa123~0..5``, ``pa123/toc``), which is reattached to the public
        id so the per-kind handler parses it as it would on a slug.

        Returns ``True`` if it routed the handle, ``False`` otherwise
        (non-handle, unknown/chunk handle, or an explicit ``kind=`` that
        disagrees — left for normal validation to flag), so the caller
        falls through to bare-slug inference untouched.
        """
        if self.store is None:
            return False
        # Split off a trailing ``~selector`` / ``/view`` so the base handle
        # parses; the suffix is reattached to the resolved public id below.
        mm = re.match(r"^([a-zA-Z]{2}\d+)([~/].*)$", ident.strip())
        base, suffix = (mm.group(1), mm.group(2)) if mm else (ident, "")
        normalized = handle_registry.normalize(base)
        if not handle_registry.is_well_formed(normalized):
            return False
        resolved = self.store.resolve_handle(normalized)
        if resolved is None:
            return False
        explicit = args.get("kind")
        if explicit is not None and explicit != resolved.kind:
            return False
        if resolved.chunk_id is not None:
            # Chunk handle → per-kind chunk selector. Slug-document kinds take
            # ``slug~ord`` (Block.pos == chunks.ord). Numeric-chunk kinds
            # (gripe/message/…) have no ``~ord`` selector yet, so fall through
            # (a chunk handle has no slug match → natural NotFound). A chunk
            # handle takes no further selector.
            if (
                suffix
                or resolved.chunk_ord is None
                or resolved.public_id == str(resolved.ref_id)
            ):
                return False
            args["kind"] = resolved.kind
            args["id"] = f"{resolved.public_id}~{resolved.chunk_ord}"
            return True
        args["kind"] = resolved.kind
        args["id"] = resolved.public_id + suffix
        return True

    def _maybe_split_prefixed_id(self, args: dict[str, Any]) -> None:
        """D1: extract a self-identifying kind from ``id=`` into ``args['kind']``.

        Two grammars, both letting an agent address a ref without
        spelling ``kind=``:

        * ``kind:identifier[~selector]`` colon prefix (the canonical
          handle grammar ``link=`` / ``unlink=`` already use)::

            id='paper:chung19~4'   → kind='paper', id='chung19~4'
            id='memory:158'        → kind='memory', id=158 (coerced by handler)
            id='todo:42'           → kind='todo', id=42
            id='chung19~4'         → unchanged (no colon, no extraction)

        * a leading **address sigil** — the sigil *is* the kind tag::

            id='¶YP377G'           → kind='draft', id='¶YP377G' (sigil kept)
            id='§chung19~4'        → kind='paper', id='chung19~4' (sigil stripped)

        Only fires when:
          - ``id`` is a string containing exactly one ``:`` before any
            ``/``, ``~``, or ``?`` (avoiding collision with URL-ish
            paths a future kind might accept).
          - The prefix is one of the live kinds in this build.
          - If ``kind=`` is already set, it must match the prefix;
            otherwise a clean ``BadInput`` fires (don't silently
            override the caller's explicit choice).

        Path views like ``id='/recent'`` are skipped (leading slash).
        Anything not matching the recognition rules passes through
        unchanged so existing callers stay unaffected.
        """
        ident = args.get("id")
        if not isinstance(ident, str):
            return
        # Address sigil (``¶`` draft chunk, ``§`` paper citation) is
        # self-identifying — route it before the colon logic. The draft
        # skill documents ``get(id='¶handle')`` with no ``kind=``.
        if ident[:1] in _SIGIL_KIND:
            self._infer_sigil_kind(args, ident)
            return
        # Leading slash → path view (/recent etc.). Don't extract.
        if ident.startswith("/"):
            return
        if ":" not in ident:
            # ADR 0036: a draft chunk handle ``dc<id>`` (optionally with a
            # ``-B+A`` reading window) routes to the draft handler, which
            # parses the window. Drafts have no slug, so the generic
            # chunk-handle path below can't rewrite them to ``slug~ord``.
            if self._maybe_route_draft_chunk(args, ident):
                return
            # ADR 0036 relative navigation: ``pc10+1`` / ``pc10-2..3`` /
            # ``pc10^`` resolves against current structure to a per-kind
            # chunk selector. Try this before the absolute-handle path
            # (a relative handle is not a well-formed absolute one).
            if self._maybe_infer_kind_from_relative(args, ident):
                return
            # ADR 0036: a universal handle (``<2-char code><decimal id>``)
            # self-identifies — resolve it to (kind, public_id) before the
            # bare-slug fallback, so it isn't mis-read as a slug.
            if self._maybe_infer_kind_from_handle(args, ident):
                return
            # No ``kind:`` prefix and no sigil — try resolving a bare
            # slug (optionally with a ``~selector`` / ``/view``) to its
            # owning kind, so ``get(id='wu22c~312')`` self-identifies as
            # the paper that owns ``wu22c``.
            self._infer_slug_kind(args, ident)
            return
        # Only honour a colon that comes before any selector / view
        # path separator. ``markdown:notes/a.md`` is fine; ``foo/bar:x``
        # is not — the colon there isn't a kind prefix.
        for sep in ("/", "?"):
            if sep in ident and ident.find(sep) < ident.find(":"):
                return
        prefix, _, rest = ident.partition(":")
        prefix = prefix.strip()
        if not prefix or not rest:
            return
        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if prefix not in live_kinds:
            # Not a recognised kind prefix — leave the value alone.
            # Better to surface a "no such kind" error downstream than
            # eat a legitimate identifier that happens to contain ":".
            return

        existing_kind = args.get("kind")
        if existing_kind is not None and existing_kind != prefix:
            raise BadInput(
                f"id={ident!r} prefix kind={prefix!r} conflicts with "
                f"kind={existing_kind!r}",
                next=(
                    f"drop one: either pass id={rest!r} with "
                    f"kind={prefix!r}, or pass id={ident!r} without kind="
                ),
            )
        args["kind"] = prefix
        args["id"] = rest

    def _maybe_add_skill_hint(
        self, err: PrecisError, verb: str, args: dict[str, Any]
    ) -> None:
        # See _KIND_SKILL_ALIASES below for the module-level map.
        """F6: append a per-kind / per-verb help-skill `next:` hint.

        Mutates ``err.next`` in place to add a discoverability pointer
        without losing whatever the handler already put there. Order:
        (1) caller-supplied hints, then (2) per-kind skill if the call
        named one, else per-verb skill, else the overview. The LLM
        reads top-down and grabs the most-specific recovery action
        first; the new hint is the second-best option.
        """
        kind = args.get("kind") if isinstance(args, dict) else None
        # Drop list/wildcard kinds — the help skill for "paper,patent"
        # or "*" doesn't exist; fall through to the verb/overview hint.
        if isinstance(kind, str) and ("," in kind or kind == "*"):
            kind = None

        live_kinds = set(self.hub.kinds) if self.hub is not None else set()
        if isinstance(kind, str) and kind in live_kinds:
            # ``kind='skill'`` has no ``precis-skill-help`` — skills ARE
            # the help system, so the auto-generated breadcrumb points at
            # an id the caller just failed to fetch (broad-pass R3#3:
            # the NotFound for a bad skill slug ended with a self-
            # referential ``next: get(kind='skill', id='precis-skill-help')``).
            # Route to the live catalogue instead so the recovery hint
            # is always runnable.
            if kind == "skill":
                hint = "get(kind='skill', id='toc')"
            elif kind in _KIND_SKILL_ALIASES:
                # Renamed kinds whose `precis-{kind}-help` doesn't
                # exist (the skill kept the broader provider-rooted
                # name). Mapped here so the auto-hint stays runnable.
                hint = f"get(kind='skill', id='{_KIND_SKILL_ALIASES[kind]}')"
            else:
                hint = f"get(kind='skill', id='precis-{kind}-help')"
        elif verb in {"get", "search", "put", "edit", "delete", "tag", "link"}:
            hint = f"get(kind='skill', id='precis-{verb}-help')"
        else:
            hint = "get(kind='skill', id='precis-overview')"

        existing = err.next
        if existing is None:
            err.next = hint
        elif isinstance(existing, str):
            if hint not in existing:
                err.next = [existing, hint]
        else:
            if hint not in existing:
                err.next = [*existing, hint]

    def render_error(self, err: PrecisError) -> str:
        """Render a :class:`PrecisError` as the canonical agent-facing string.

        Public surface so transport layers (``precis.server``) can format
        pre-dispatch validation errors with the same shape the runtime
        produces on raise. Was previously named ``_render_error`` and
        accessed via ``# type: ignore[attr-defined]`` from the MCP tool
        wrappers; the underscore-prefixed alias is kept for backwards
        compatibility with anything still calling the old name.
        """
        parts = [f"[error:{err.__class__.__name__}] {err.cause}"]
        if err.options:
            parts.append(f"  options: {', '.join(map(str, err.options))}")
        if err.next:
            # F12: ``next`` may be a string (one hint) or a list of
            # strings (multiple hints). Render each on its own
            # ``next:`` line so the rendered envelope remains
            # backwards-compatible — a caller scanning for "next:"
            # finds every hint without needing to know the difference.
            if isinstance(err.next, str):
                parts.append(f"  next: {err.next}")
            else:
                for hint in err.next:
                    parts.append(f"  next: {hint}")
        return "\n".join(parts)

    # Backwards-compatible alias. Internal callers that pre-date the
    # promotion to a public method still reference ``_render_error``.
    _render_error = render_error


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
    from precis.store import Store

    if config is None:
        config = load_config()

    store: Store | None = None
    embedder: Embedder | None = None
    if config.database_url:
        store = Store.connect(config.database_url)
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
