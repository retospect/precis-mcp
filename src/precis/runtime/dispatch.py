"""Core dispatch: verb routing, kind/handler resolution, handler invocation.

``DispatchMixin`` carries the main ``dispatch()`` / ``dispatch_with_status()``
entry points, the single-kind ``kind=`` resolution chain (including the
2-char handle-code expansion and the id-prefix / sigil / relative-handle
inference helpers), and handler invocation (extras whitelist, required-kwarg
check, default-tags policy). The cross-kind / source-search fan-out lives in
:mod:`precis.runtime.search`; the angle spray + dreamable region in
:mod:`precis.runtime.angle`; hint emission in :mod:`precis.runtime.hints`;
error string rendering in :mod:`precis.runtime.error`. ``PrecisRuntime``
(``precis.runtime.core``) composes all of them via multiple inheritance —
every method below runs against the same ``self`` regardless of which file
it's defined in.
"""

from __future__ import annotations

import inspect
import logging
import re
from typing import Any

from precis.errors import BadInput, Internal, NotFound, PrecisError, Unsupported
from precis.protocol import _ALL_VERBS, Handler, Verb
from precis.response import Response
from precis.runtime._shared import CROSS_KIND_WILDCARD as _CROSS_KIND_WILDCARD
from precis.runtime._shared import RuntimeShape
from precis.utils import handle_registry

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = _ALL_VERBS

#: Sentinel key used by `precis.server` to forward the MCP tool's
#: ``args={...}`` payload through to the dispatcher without colliding
#: with the explicit positional kwargs. The dispatcher pops it before
#: calling the handler method and validates the keys against the
#: method's accepted-kwargs whitelist.
_EXTRAS_KEY = "__extras__"

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
    # ("draft", "link") moved into DraftHandler.link itself — the verb
    # now exists for folder placement (ADR 0045), so the prose-ref
    # teaching rides on its BadInput for every other relation.
    ("draft", "tag"): (
        "drafts have no whole-ref tag axis; tag the owning project todo "
        "instead, or use a glossary term / inline markup inside the prose."
    ),
}


def _tick_disabled_hint(kind: str) -> str | None:
    """The per-tick disable hint for ``kind``, or ``None``.

    Reads the thread-scoped in-process tick ContextVar
    (:func:`precis.utils.inproc_context.current`): if the active tick prohibits
    ``kind``, returns its contextual hint (what to do instead), else ``None``.
    Unset outside an in-process agent tick — the MCP server / CLI / tests never
    bind it — so this is a no-op there. The import is local so ``runtime`` keeps
    no import-time coupling to the loop context (``inproc_context`` is stdlib-
    only, but the read is per-call and cheap either way)."""
    from precis.utils.inproc_context import current

    ctx = current()
    if ctx is None or not ctx.disabled_kinds:
        return None
    return dict(ctx.disabled_kinds).get(kind)


class DispatchMixin(RuntimeShape):
    """Verb dispatch, kind/handler resolution, and handler invocation."""

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

        # ``folder=`` scope (ADR 0045): any folder-scoped search runs
        # through the cross-kind fan-out — the structured SearchHit
        # stream is what makes subtree post-filtering possible — even
        # when a single ``kind=`` was named (it becomes a one-kind
        # "comma list").
        if verb == "search" and args.get("folder") is not None:
            return self._dispatch_cross_kind(
                (
                    self._expand_kind_code(str(kind))
                    if kind is not None
                    else _CROSS_KIND_WILDCARD
                ),
                dict(args),
            )

        # Source search (unified-item-view Slice 2): a ``sort=`` /
        # ``since=`` / ``until=`` search routes to the chunk-level
        # cross-kind primitive — one store query over ``refs.kind =
        # ANY(...)`` that RRF-fuses lexical+semantic, collapses to one
        # best chunk per ref, bounds by ``refs.created_at``, and orders
        # by relevance (default) or recency. Distinct from the per-handler
        # fan-out below. Intercept before kind resolution so it composes
        # with a single kind, a comma-list, a wildcard, or an omitted kind.
        if verb == "search" and self._is_source_search_request(args):
            return self._dispatch_source_search(kind, dict(args))

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
            Unsupported: the kind is prohibited *for the duration of the
                active in-process tick* (the ContextVar gate, below).
        """
        # Per-tick prohibition (in-process agent loop only): a background pass
        # may disable a kind for one tick via the tick ContextVar — plan_tick
        # gates the draft's colliding prose-file kind so the planner writes into
        # the draft, not a freestanding file. This is the in-process twin of the
        # claude path's PRECIS_KINDS_DISABLED env entry (the spawned MCP server
        # honors *that* at construction; the in-process Hub is built once at
        # boot, so the per-tick prohibition has to be a per-call check). No-op
        # outside a tick — the ContextVar is unset for the MCP server / CLI /
        # tests, so this is byte-identical there.
        tick_hint = _tick_disabled_hint(kind)
        if tick_hint is not None:
            raise Unsupported(
                f"kind {kind!r} is disabled for this tick ({tick_hint})",
                next=tick_hint,
            )
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
        # resolve_handle already emitted the merge-redirect hint (via the
        # store's wired hint bus) if it followed a supersede; nothing to do here.
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
