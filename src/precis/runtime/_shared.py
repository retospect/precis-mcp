"""Constants + typing shape shared by two or more of the ``PrecisRuntime`` mixins.

Everything else that used to live at module scope in the monolithic
``runtime.py`` is local to the one mixin file that uses it — this module
only holds the handful of names referenced from more than one of
``dispatch.py`` / ``search.py`` / ``angle.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis._pagination import PaginationCache
    from precis.dispatch import Hub
    from precis.errors import PrecisError
    from precis.hints import HintBus
    from precis.response import Response
    from precis.store import Store

# Wildcard token for cross-kind search. Equivalent to a comma-list
# of every kind whose ``KindSpec.supports_search_hits`` is True.
CROSS_KIND_WILDCARD = "*"

# English aliases for the wildcard, accepted from agent callers who
# write the most natural shorthand they know. Every entry behaves
# identically to ``CROSS_KIND_WILDCARD``: it expands to every
# search-hits-capable kind.
CROSS_KIND_ALIASES: frozenset[str] = frozenset({"*", "", "all", "any", "*all*"})


class RuntimeShape:
    """Typing-only cross-mixin shape (never instantiated on its own).

    Every ``PrecisRuntime`` mixin (``DispatchMixin`` / ``SearchMixin`` /
    ``AngleMixin`` / ``HintsMixin`` / ``ErrorMixin``) references attributes
    and methods that live on the composed dataclass or on a *sibling*
    mixin — mypy type-checks each class against only its own declared
    bases, so it can't see across siblings on its own. Each mixin
    additionally subclasses this shape so mypy has something to resolve
    ``self.hub`` / ``self._dispatch_cross_kind`` / etc. against.

    This is deliberately a plain class, not a ``Protocol``: every mixin
    that needs a stub here is a *common* base of the final
    ``PrecisRuntime(DispatchMixin, SearchMixin, AngleMixin, HintsMixin,
    ErrorMixin)``, so C3 linearization always places ``RuntimeShape``
    *after* every mixin in the MRO — the real implementations (defined
    directly on ``PrecisRuntime`` or on whichever mixin actually owns
    them) are found first, and these stub bodies are never reached at
    runtime. (If a mixin stopped being a common ancestor of all the
    others this reasoning would need re-checking — the stubs would then
    risk shadowing a real implementation.) Every stub body raises
    ``NotImplementedError`` rather than using ``...`` — mypy's
    ``empty-body`` check rejects a bare ``...`` with a non-``None``
    return type outside an ``abstractmethod``/stub file, and a real
    raise is honest about "this is never meant to run" without pulling
    in ``abc.ABCMeta`` (which would change how these classes can be
    instantiated in isolation, e.g. by tests).
    """

    # ── core dataclass fields ────────────────────────────────────────────
    hub: Hub
    pagination: PaginationCache
    default_tags_resolved: tuple[str, ...]

    # ``store`` / ``hints`` are read-only properties on ``PrecisRuntime``
    # (delegating to ``self.hub``), not plain settable fields — declared
    # the same way here so the override is property-over-property, not
    # property-over-writable-attribute (mypy's ``override`` check).
    @property
    def store(self) -> Store | None:
        raise NotImplementedError

    @property
    def hints(self) -> HintBus:
        raise NotImplementedError

    # ── error.py (ErrorMixin) ────────────────────────────────────────────
    def render_error(self, err: PrecisError) -> str:
        raise NotImplementedError

    # ── hints.py (HintsMixin) ────────────────────────────────────────────
    def _maybe_add_skill_hint(
        self, err: PrecisError, verb: str, args: dict[str, Any]
    ) -> None:
        raise NotImplementedError

    def _maybe_hint_tag_shaped_q(self, args: dict[str, Any]) -> None:
        raise NotImplementedError

    # ── angle.py (AngleMixin) ────────────────────────────────────────────
    def _dispatch_dreamable(self, kind: Any, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _dispatch_angle(self, kind: Any, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    # ── search.py (SearchMixin) ──────────────────────────────────────────
    def _dispatch_stubs(self, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _is_source_search_request(self, args: dict[str, Any]) -> bool:
        raise NotImplementedError

    def _dispatch_source_search(
        self, kind: str | None, args: dict[str, Any]
    ) -> Response:
        raise NotImplementedError

    def _is_cross_kind_request(self, kind: Any) -> bool:
        raise NotImplementedError

    def _cross_kind_kinds(self) -> list[str]:
        raise NotImplementedError

    # ── dispatch.py (DispatchMixin) ──────────────────────────────────────
    def _expand_kind_code(self, kind: str) -> str:
        raise NotImplementedError
