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
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.config import PrecisConfig
from precis.dispatch import Hub
from precis.errors import (
    BadInput,
    Internal,
    PrecisError,
)
from precis.protocol import _ALL_VERBS, Handler, Verb
from precis.response import Response
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
                return self.render_error(e), True
            except Exception as e:
                log.exception("internal error in %s", verb)
                return (
                    self.render_error(Internal(f"internal error: {e}")),
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
        kind = args.pop("kind", None)

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
            NotFound: ``kind`` is not registered. Options carries
                only the verb-supporting kinds so an agent retrying
                against a suggested kind doesn't cascade into a
                second error (MCP critic MAJOR #12).
            Unsupported: handler exists but does not implement
                ``verb``. The reply enumerates the verbs this kind
                *does* support so the recovery hint is sharp.
        """
        handler = self.hub.handler_for(kind)
        if handler is None:
            from precis.errors import NotFound as _NF

            raise _NF(
                f"unknown kind: {kind}",
                options=self._kinds_for_verb(verb),
                next="see precis-overview for the kind list",
            )

        if not handler.spec.supports(verb):  # type: ignore[arg-type]
            from precis.errors import Unsupported

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
            unknown = self._unknown_extras(method, extras)
            if unknown:
                accepted = sorted(self._accepted_kwargs(method))
                raise BadInput(
                    f"args= keys {unknown!r} not accepted by {kind}.{verb}",
                    options=accepted,
                    next=(
                        f"drop the unknown keys; {kind}.{verb} accepts "
                        f"args= keys: {accepted or '(none)'}"
                    ),
                )
            args.update(extras)

        # Strip None args so handlers see absence as missing.
        clean = {k: v for k, v in args.items() if v is not None}
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
                        "single-kind search() contract — call them one at a time"
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
                    f"search(kind={kind!r}, q='your query') — cross-kind merge "
                    "fans out via search_hits, which needs a non-empty query"
                ),
            )
        top_k = int(args.get("top_k") or 10)
        tags = args.get("tags")

        kinds = self._resolve_cross_kind_request(kind)
        if not kinds:
            raise BadInput(
                "no kinds available for cross-kind search",
                next=(
                    "this build has no kinds that opt into cross-kind merge; "
                    "use single-kind search() against the kind you want"
                ),
            )

        streams: list[list[SearchHit]] = []
        for k in kinds:
            handler = self.hub.handler_for(k)
            if handler is None:
                continue
            try:
                hits = handler.search_hits(q=q, tags=tags, top_k=top_k)
            except TypeError:
                # Handler's signature doesn't accept tags=; retry
                # without (e.g. oracle/quest don't filter by tag).
                try:
                    hits = handler.search_hits(q=q, top_k=top_k)
                except Exception:
                    log.exception("cross-kind search_hits failed for %s", k)
                    continue
            except Exception:
                log.exception("cross-kind search_hits failed for %s", k)
                continue
            streams.append(list(hits))

        return merge_and_render(
            streams,
            top_k=top_k,
            query=q,
            header_noun="match",
            mode="rrf",
            empty_body=(f"no matches across {', '.join(kinds)} for {q!r}"),
        )

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
            parts.append(f"  next: {err.next}")
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
    ``docs/seven-verb-surface-migration.md`` D7/D8.
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

    hub = boot(
        store=store,
        embedder=embedder,
        markdown_root=config.markdown_root,
        plaintext_root=config.plaintext_root,
        python_roots=config.python_roots,
    )
    return PrecisRuntime(config=config, hub=hub)
