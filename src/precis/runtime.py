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
        if kind is None:
            if verb == "search":
                # Phase 1: cross-corpus search not yet wired. Future:
                # iterate registry, fan-out, RRF-merge.
                raise BadInput(
                    "cross-kind search not yet implemented",
                    next="add kind=<one of: calc>",
                )
            raise BadInput("missing kind=", options=self.registry.kinds())

        handler = self.registry.get(kind)
        if not handler.spec.supports(verb):  # type: ignore[arg-type]
            from precis.errors import Unsupported

            verbs = [v for v in _VERBS if handler.spec.supports(v)]
            raise Unsupported(
                f"{kind} does not support {verb}",
                options=verbs,
                next=f"try one of {verbs} on kind={kind!r}",
            )

        method = getattr(handler, verb)
        # Strip None args so handlers see absence as missing
        clean = {k: v for k, v in args.items() if v is not None}
        return method(**clean)

    def _render(self, response: Response) -> str:
        out = [response.body]
        hints = self.hints.collect()
        for h in hints:
            out.append(f"\n[{h.level}] {h.text}")
        if response.cost:
            out.append(f"\n— cost: {response.cost}")
        return "".join(out)

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

    Caller owns the returned runtime; if it has a store, call
    `runtime.store.close()` before exit.
    """
    from precis.config import load_config
    from precis.registry import builtins
    from precis.store import Store

    if config is None:
        config = load_config()

    store: Store | None = None
    if config.database_url:
        store = Store.connect(config.database_url)

    handlers = builtins(store=store)
    return PrecisRuntime(
        config=config,
        registry=Registry(handlers),
        hints=HintBus(),
        store=store,
    )
