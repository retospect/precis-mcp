"""Handler ABC and KindSpec.

Every kind subclasses `Handler` and exposes a `KindSpec` ClassVar
declaring which verbs it supports, what views/modes it understands, and
any runtime-required env vars. The dispatcher uses KindSpec to validate
calls and to hide kinds whose env requirements aren't met.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from precis.errors import Unsupported
from precis.response import Response

if TYPE_CHECKING:
    from precis.dispatch import Registry
    from precis.utils.search_merge import SearchHit

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

    # Cross-kind search opt-in. When True, the handler's
    # ``search_hits`` method returns ``list[SearchHit]`` and
    # participates in fan-out merges (``kind='paper,memory'``,
    # ``kind='*'``). Independent of ``supports_search``: a handler
    # may serve a custom-shaped single-kind ``search()`` (skill,
    # python) without being eligible for the universal merge.
    supports_search_hits: bool = False

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

    Construction: handlers take ``r: Registry`` as a keyword argument
    and self-register by calling :meth:`_register_with` as the last
    statement of ``__init__``. See ``docs/seven-verb-surface-migration.md``
    D7 for the contract.
    """

    spec: ClassVar[KindSpec]

    #: Populated by :meth:`_register_with` so handlers that need
    #: registry introspection (e.g. SkillHandler rendering
    #: ``precis-help``) can read it without a separate late-bind
    #: hook. Typed ``Any`` to avoid a hard import of
    #: ``precis.dispatch.Registry`` in every handler module.
    registry: Any = None

    def _register_with(self, r: Registry) -> None:
        """Register every verb declared supported in ``self.spec``.

        Invoked by :func:`precis.dispatch._try` immediately after
        successful construction. Reads ``self.spec`` and populates
        the flat dispatch table with bound methods, and stashes
        ``r`` on ``self.registry`` for the handful of handlers that
        want to query the registry at runtime (skill rendering,
        cross-handler metadata lookups).

        ``mode`` on every ability is ``None`` under the v1 shape —
        ``put`` is still polymorphic over a mode-string. The seven-
        verb semantic migration splits ``put`` into ``put / edit /
        delete / tag / link`` and introduces (kind, verb, mode) keys
        with non-None modes.
        """
        self.registry = r
        spec = self.spec
        r.register_handler(spec.kind, self)
        for verb in ("get", "search", "put", "move"):
            if spec.supports(verb):  # type: ignore[arg-type]
                r.register_ability(spec.kind, verb, None, getattr(self, verb))
        r.register_overview(spec.kind, spec.description)

    def get(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support get")

    def search(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support search")

    def search_hits(self, **kw: Any) -> list[SearchHit]:
        """Structured search for cross-kind merge.

        Returns a list of ``SearchHit`` already sorted best-first.
        Used by the runtime when ``kind`` is a comma-list / ``'*'``
        / ``None``-with-cross-kind-default to fan out across every
        kind whose ``KindSpec.supports_search_hits`` is True.

        Default raises ``Unsupported``; concrete handlers override.
        Single-kind ``search()`` text rendering stays the canonical
        agent surface — this method is the structured input to the
        merge primitive, not a replacement.
        """
        raise Unsupported(f"{self.spec.kind} does not support cross-kind search")

    def put(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support put")

    def move(self, **kw: Any) -> Response:
        raise Unsupported(f"{self.spec.kind} does not support move")
