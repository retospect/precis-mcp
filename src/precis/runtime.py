"""Server runtime.

`PrecisRuntime` owns the registry, hint bus, config, and dispatch
logic. The MCP server (in `precis.server`) is a thin FastMCP wrapper
around it; tests dispatch directly without going through MCP.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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

log = logging.getLogger(__name__)


_VERBS: tuple[Verb, ...] = ("get", "search", "put", "move")


@dataclass
class PrecisRuntime:
    """Server-wide singleton: config + registry + hint bus + dispatch."""

    config: PrecisConfig
    registry: Registry
    hints: HintBus

    async def dispatch(self, verb: str, args: dict[str, Any]) -> str:
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
                response = await self._dispatch_inner(verb, dict(args))
                return self._render(response)
            except PrecisError as e:
                return self._render_error(e)
            except Exception as e:
                log.exception("internal error in %s", verb)
                return self._render_error(Internal(f"internal error: {e}"))

    async def _dispatch_inner(self, verb: str, args: dict[str, Any]) -> Response:
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
        return await method(**clean)

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


def build_runtime(config: PrecisConfig | None = None) -> PrecisRuntime:
    """Construct a runtime from the in-tree BUILTINS list."""
    from precis.config import load_config
    from precis.registry import builtins

    if config is None:
        config = load_config()
    handlers = [cls() for cls in builtins()]
    return PrecisRuntime(
        config=config,
        registry=Registry(handlers),
        hints=HintBus(),
    )
