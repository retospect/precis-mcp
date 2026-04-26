"""Handler ABC and KindSpec.

Every kind subclasses `Handler` and exposes a `KindSpec` ClassVar
declaring which verbs it supports, what views/modes it understands, and
any runtime-required env vars. The dispatcher uses KindSpec to validate
calls and to hide kinds whose env requirements aren't met.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, ClassVar, Literal

from precis.errors import Unsupported
from precis.response import Response

Verb = Literal["get", "search", "put", "move"]


@dataclass(frozen=True, slots=True)
class KindSpec:
    """Declarative metadata for a kind."""

    kind: str
    title: str
    description: str

    supports_get: bool = False
    supports_search: bool = False
    supports_put: bool = False
    supports_move: bool = False

    is_numeric: bool = False  # public id is int (else str slug)
    id_required: bool = True  # False if get may omit id

    views: tuple[str, ...] = ()  # supported view= values
    modes: tuple[str, ...] = ()  # supported mode= values for put

    requires_env: tuple[str, ...] = ()  # all must be set or kind is hidden

    def is_available(self) -> bool:
        """True iff every required env var is set with a non-empty value."""
        return all(os.environ.get(v) for v in self.requires_env)

    def supports(self, verb: Verb) -> bool:
        return getattr(self, f"supports_{verb}")


class Handler:
    """Base for all handlers.

    Subclasses override the verbs they support and declare a `KindSpec`
    ClassVar. The default implementations raise `Unsupported` so a
    handler that lies about its KindSpec is detectable.
    """

    spec: ClassVar[KindSpec]

    async def get(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support get")

    async def search(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support search")

    async def put(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support put")

    async def move(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support move")
