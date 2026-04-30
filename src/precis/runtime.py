"""Server runtime.

`PrecisRuntime` owns the registry, hint bus, config, and dispatch
logic. The MCP server (in `precis.server`) is a thin FastMCP wrapper
around it; tests dispatch directly without going through MCP.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.config import PrecisConfig
from precis.errors import (
    BadInput,
    Internal,
    NotFound,
    PrecisError,
)
from precis.hints import HintBus
from precis.protocol import Verb
from precis.registry import Registry
from precis.response import Response
from precis.utils.search_merge import SearchHit, merge_and_render

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = ("get", "search", "put", "move")

# Wildcard token for cross-kind search. Equivalent to a comma-list
# of every kind whose ``KindSpec.supports_search_hits`` is True.
_CROSS_KIND_WILDCARD = "*"

#: Sentinel key used by `precis.server` to forward the MCP tool's
#: ``args={...}`` payload through to the dispatcher without colliding
#: with the explicit positional kwargs. The dispatcher pops it before
#: calling the handler method and validates the keys against the
#: method's accepted-kwargs whitelist.
_EXTRAS_KEY = "__extras__"


@dataclass
class PrecisRuntime:
    """Server-wide singleton: config + registry + hint bus + dispatch.

    `store` is None for stateless deployments (no database). Ref-backed
    handlers won't be in the registry in that case.
    """

    config: PrecisConfig
    registry: Registry
    hints: HintBus
    store: Store | None = None

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
        with self.hints.request():
            try:
                if verb not in _VERBS:
                    raise BadInput(
                        f"unknown verb: {verb}",
                        options=list(_VERBS),
                    )
                response = self._dispatch_inner(verb, dict(args))
                return self._render(response), False
            except PrecisError as e:
                return self._render_error(e), True
            except Exception as e:
                log.exception("internal error in %s", verb)
                return self._render_error(Internal(f"internal error: {e}")), True

    def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
        # Drop None values so handlers see absence as missing, not None
        kind = args.pop("kind", None)
        kind_was_defaulted = False

        # ── cross-kind search dispatch ────────────────────────────────
        # ``kind='*'`` (wildcard) and ``kind='paper,memory'`` (comma-
        # list) fan out across every requested kind whose
        # ``KindSpec.supports_search_hits`` is True, then RRF-fuse the
        # results via ``merge_and_render``.  Other verbs (get/put/move)
        # keep the single-kind contract — multi-kind get is meaningless
        # and multi-kind put would silently scatter writes.
        if verb == "search" and self._is_cross_kind_request(kind):
            return self._dispatch_cross_kind(kind, dict(args))

        if kind is None:
            if verb == "search":
                # 7B callers routinely forget ``kind=``.  Pick the most
                # recently touched search-supporting kind as a sensible
                # default and echo the choice back so the caller can
                # see what we did.  Falls back to ``kind='*'`` cross-
                # kind dispatch when the registry has more than one
                # search-hits-supporting kind and the store can't
                # narrow further (stateless deployment, empty corpus).
                search_kinds = [
                    k
                    for k in self.registry.kinds()
                    if self.registry.get(k).spec.supports_search
                ]
                kind = self._default_search_kind(search_kinds)
                if kind is None:
                    cross_kind = self._cross_kind_kinds()
                    if len(cross_kind) >= 2:
                        return self._dispatch_cross_kind(
                            _CROSS_KIND_WILDCARD, dict(args)
                        )
                    raise BadInput(
                        "missing kind= and no defensible default available",
                        options=search_kinds,
                        next=(
                            "pass kind=<one of the listed kinds>, or use "
                            "kind='*' / kind='paper,memory' for cross-kind merge"
                        ),
                    )
                kind_was_defaulted = True
            else:
                raise BadInput("missing kind=", options=self.registry.kinds())

        try:
            handler = self.registry.get(kind)
        except NotFound as exc:
            # The registry's "unknown kind" error carries every active
            # kind regardless of verb. The MCP critic flagged this as
            # MAJOR #12: an agent that asked ``search(kind='all', q='…')``
            # got back the *full* kind list including kinds that don't
            # support search at all (calc, math, web, …) — so its retry
            # against kind='calc' hit a *second* error. Re-raise with
            # the verb-filtered options so the recovery hint actually
            # works.
            from precis.errors import NotFound as _NF

            raise _NF(
                f"unknown kind: {kind}",
                options=self._kinds_for_verb(verb),
                next="see precis-overview for the kind list",
            ) from exc
        if not handler.spec.supports(verb):  # type: ignore[arg-type]
            from precis.errors import Unsupported

            verbs = [v for v in _VERBS if handler.spec.supports(v)]
            # Special case for ``move``: when *no* active kind implements
            # it, the agent has no recovery path inside this server. Say
            # so explicitly rather than letting it look like a per-kind
            # quirk; this prevents the loop where a small model retries
            # move() against another kind in the hope it'll work.
            if verb == "move":
                movers = [
                    k
                    for k in self.registry.kinds()
                    if self.registry.get(k).spec.supports_move
                ]
                if not movers:
                    raise Unsupported(
                        "no active kind currently supports move",
                        options=verbs,
                        next=(
                            "move is reserved for structured file kinds "
                            "(docx, tex) that aren't wired in this build; "
                            "use put for everything else"
                        ),
                    )
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=f"try one of {verbs} on kind={kind!r}",
            )

        method = getattr(handler, verb)
        # Pull off the args= extras forwarded by the MCP boundary so we
        # can validate them against the handler's signature *before*
        # the call. Without this gate, ``**_kw`` swallows unknown keys
        # silently and the agent gets no signal that ``args={'depth':3}``
        # vanished. (MCP critic MINOR — args= silently consumed.)
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
        # Strip None args so handlers see absence as missing
        clean = {k: v for k, v in args.items() if v is not None}
        try:
            response = method(**clean)
        except PrecisError as exc:
            # ``(searched kind=...)`` must surface on error paths too,
            # not just on success — otherwise a crash inside the
            # defaulted kind's handler leaves the caller blind to
            # which kind was actually tried.  (MCP critic MAJOR —
            # search with no kind= does not preface the chosen kind
            # on failure.)
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

        Three forms count:

        - the wildcard ``'*'``;
        - any comma-list (``'paper,memory'`` or even ``'paper, memory'``);
        - an explicit empty string (``''``) is treated like the
          wildcard for symmetry with MCP clients that send ``kind=""``.

        ``None`` does NOT count here — it goes through the
        single-kind defaulting path so callers that forgot
        ``kind=`` get the friendly "what were you working on"
        nudge before being escalated to cross-kind merge.
        """
        if not isinstance(kind, str):
            return False
        if kind == _CROSS_KIND_WILDCARD:
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
        for k in self.registry.kinds():
            spec = self.registry.get(k).spec
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
        if kind.strip() in (_CROSS_KIND_WILDCARD, ""):
            return eligible

        requested = [tok.strip() for tok in kind.split(",")]
        requested = [t for t in requested if t]
        if not requested:
            return eligible

        bad = [t for t in requested if t not in eligible]
        if bad:
            registered = set(self.registry.kinds())
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
            handler = self.registry.get(k)
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
            for k in self.registry.kinds()
            if self.registry.get(k).spec.supports(verb)  # type: ignore[arg-type]
        ]

    def _render_error(self, err: PrecisError) -> str:
        parts = [f"[error:{err.__class__.__name__}] {err.cause}"]
        if err.options:
            parts.append(f"  options: {', '.join(map(str, err.options))}")
        if err.next:
            parts.append(f"  next: {err.next}")
        return "\n".join(parts)


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
    """
    from precis.config import load_config
    from precis.embedder import Embedder, make_embedder
    from precis.registry import builtins
    from precis.store import Store

    if config is None:
        config = load_config()

    store: Store | None = None
    embedder: Embedder | None = None
    if config.database_url:
        store = Store.connect(config.database_url)
        embedder = make_embedder(config.embedder, dim=store.embedding_dim())

    handlers = builtins(
        store=store,
        embedder=embedder,
        markdown_root=config.markdown_root,
        python_roots=config.python_roots,
    )
    registry = Registry(handlers)
    # Wire the registry into SkillHandler so it can synthesize the
    # 'precis-help' meta-skill listing every active kind.
    for h in handlers:
        bind = getattr(h, "bind_registry", None)
        if callable(bind):
            bind(registry)
    return PrecisRuntime(
        config=config,
        registry=registry,
        hints=HintBus(),
        store=store,
    )
