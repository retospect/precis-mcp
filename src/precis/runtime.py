"""Server runtime.

`PrecisRuntime` owns the registry, hint bus, config, and dispatch
logic. The MCP server (in `precis.server`) is a thin FastMCP wrapper
around it; tests dispatch directly without going through MCP.
"""

from __future__ import annotations

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

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = ("get", "search", "put", "move")


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
        out (MCP expects a string return)."""
        with self.hints.request():
            try:
                if verb not in _VERBS:
                    raise BadInput(
                        f"unknown verb: {verb}",
                        options=list(_VERBS),
                    )
                response = self._dispatch_inner(verb, dict(args))
                return self._render(response)
            except PrecisError as e:
                return self._render_error(e)
            except Exception as e:
                log.exception("internal error in %s", verb)
                return self._render_error(Internal(f"internal error: {e}"))

    def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
        # Drop None values so handlers see absence as missing, not None
        kind = args.pop("kind", None)
        kind_was_defaulted = False
        if kind is None:
            if verb == "search":
                # Cross-kind fan-out isn't built yet, but a 7B caller
                # routinely forgets ``kind=``. Pick the most recently
                # touched search-supporting kind as a sensible default
                # and echo the choice back in the response so the
                # caller can see what we did. Falls back to a sharp
                # error when there's nothing to default to (empty
                # corpus, stateless deployment, …). The user requested
                # this defaulting in the third critic pass: small
                # models bounce off ``kind=`` requirements far more
                # than frontier models, and "what was I just working
                # on?" is the right disambiguation rule.
                search_kinds = [
                    k
                    for k in self.registry.kinds()
                    if self.registry.get(k).spec.supports_search
                ]
                kind = self._default_search_kind(search_kinds)
                if kind is None:
                    raise BadInput(
                        "cross-kind search not yet implemented",
                        options=search_kinds,
                        next=(
                            "pass kind=<one of the listed kinds> — comma-lists "
                            "(kind='paper,memory') are not supported either"
                        ),
                    )
                kind_was_defaulted = True
            else:
                raise BadInput("missing kind=", options=self.registry.kinds())

        # Comma-list kinds are documented historically but not implemented.
        # Catch them here with a precise hint rather than letting the
        # registry surface a generic "unknown kind" error that mentions
        # only single-kind options.
        if isinstance(kind, str) and "," in kind:
            raise BadInput(
                f"comma-list kind not supported: {kind!r}",
                options=self._kinds_for_verb(verb),
                next=(
                    "pass exactly one kind — multi-kind search is not "
                    "implemented yet; merge results client-side"
                ),
            )

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
        # Strip None args so handlers see absence as missing
        clean = {k: v for k, v in args.items() if v is not None}
        response = method(**clean)
        if kind_was_defaulted:
            response = self._tag_defaulted_kind(response, kind)
        return response

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
