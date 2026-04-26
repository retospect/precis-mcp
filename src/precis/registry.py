"""In-tree handler registry.

V2 drops setuptools entry-point plugin discovery. New kinds = append a
class to `BUILTINS()` here and add a row to the `kinds` reference table
in a migration. Two-step, explicit, greppable.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

from precis.errors import NotFound

if TYPE_CHECKING:
    from precis.protocol import Handler


def builtins() -> list[type[Handler]]:
    """The set of handler classes the server boots with.

    Phase 1: only `calc`. More land in subsequent phases.
    Lazy imports avoid heavy deps (sympy, pgvector, ...) at module load
    when only the registry shape is needed (e.g. tool-schema generation).
    """
    from precis.handlers.calc import CalcHandler

    return [CalcHandler]


class Registry:
    """Resolves a `kind=` string to a handler instance.

    Unavailable kinds (KindSpec.requires_env not satisfied) are silently
    omitted at construction time — the agent never sees them in the
    kind enum nor as a `NotFound.options` value.
    """

    def __init__(self, handlers: Iterable[Handler]) -> None:
        self._by_kind: dict[str, Handler] = {}
        for h in handlers:
            if not h.spec.is_available():
                continue
            if h.spec.kind in self._by_kind:
                raise ValueError(f"duplicate kind: {h.spec.kind}")
            self._by_kind[h.spec.kind] = h

    def get(self, kind: str) -> Handler:
        try:
            return self._by_kind[kind]
        except KeyError:
            raise NotFound(
                f"unknown kind: {kind}",
                options=sorted(self._by_kind.keys()),
                next="see precis-overview for the kind list",
            ) from None

    def kinds(self) -> list[str]:
        return sorted(self._by_kind.keys())

    def __contains__(self, kind: str) -> bool:
        return kind in self._by_kind

    def __len__(self) -> int:
        return len(self._by_kind)
