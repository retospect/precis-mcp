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
from typing import TYPE_CHECKING, Any

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import (
    BadInput,
    Internal,
    NotFound,
    PrecisError,
    Unsupported,
)
from precis.protocol import _ALL_VERBS, Handler, Verb
from precis.response import Response
from precis.store.types import Tag
from precis.utils.search_merge import SearchHit, merge_and_render

if TYPE_CHECKING:
    from precis.hints import HintBus
    from precis.store import Store

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
                return self._render(response), False
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
            return str(kind), False, None

        if verb != "search":
            raise BadInput("missing kind=", options=sorted(self.hub.kinds))

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
            if recovery is None:
                # Defensive: shouldn't happen — a kind with no
                # supported verbs would be useless. Drop the next:
                # trailer rather than render a meaningless one.
                raise Unsupported(
                    f"{kind} does not support {verb}",
                    options=verbs,
                )
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=f"try {recovery}(kind={kind!r})",
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

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        """Fan out a search across multiple kinds and RRF-fuse the streams.

        Each handler's ``search_hits(q=..., top_k=...)`` is called
        with the same arguments; per-handler exceptions degrade to
        empty streams (logged) so one slow / broken kind doesn't
        crash the whole query.  Final ranking is reciprocal-rank
        fusion via ``merge_and_render(mode='rrf')``.
        """
        q = args.get("q")
        if q is None or not (isinstance(q, str) and q.strip()):
            raise BadInput(
                "cross-kind search requires q=",
                next=(
                    f"search(kind={kind!r}, q='your query') - cross-kind merge "
                    "fans out via search_hits, which needs a non-empty query"
                ),
            )
        top_k = int(args.get("top_k") or 10)
        tags = args.get("tags")
        exclude = args.get("exclude")

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
        embedder = getattr(self.hub, "embedder", None)
        if embedder is not None:
            try:
                base_kwargs["query_vec"] = embedder.embed_one(q)
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

        response = merge_and_render(
            streams,
            page_size=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=(f"no matches across {', '.join(kinds)} for {q!r}"),
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

        return response

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

    def _maybe_split_prefixed_id(self, args: dict[str, Any]) -> None:
        """D1: extract ``kind:`` prefix from ``id=`` into ``args['kind']``.

        Recognises the canonical handle grammar already used by
        ``link=`` / ``unlink=`` — ``kind:identifier[~selector]`` — when
        passed via the ``id=`` argument. Examples:

            id='paper:chung19~4'   → kind='paper', id='chung19~4'
            id='memory:158'        → kind='memory', id=158 (coerced by handler)
            id='todo:42'           → kind='todo', id=42
            id='chung19~4'         → unchanged (no colon, no extraction)

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
        # Leading slash → path view (/recent etc.). Don't extract.
        if ident.startswith("/"):
            return
        if ":" not in ident:
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
        embedder = make_embedder(config.embedder, dim=store.embedding_dim())

    from precis import default_tags as _dt
    from precis.kind_gate import parse_disabled

    hub = boot(
        store=store,
        embedder=embedder,
        precis_root=config.root,
        python_roots=config.python_roots,
        kinds_disabled=parse_disabled(config.kinds_disabled),
    )
    return PrecisRuntime(
        config=config,
        hub=hub,
        default_tags_resolved=_dt.parse(config.default_tags),
    )
