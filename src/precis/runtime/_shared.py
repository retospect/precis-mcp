"""Constants + typing shape shared by two or more of the ``PrecisRuntime`` mixins.

Everything else that used to live at module scope in the monolithic
``runtime.py`` is local to the one mixin file that uses it — this module
only holds the handful of names referenced from more than one of
``dispatch.py`` / ``search.py`` / ``angle.py``.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput

if TYPE_CHECKING:
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

# ``kind='source'`` (the read-for-question loop slice 5, the ``kind='source'``
# fan-out): papers + live claim hubs in one RRF-fused table — the "settled
# claim or raw passage" question a drafting agent asks first. Unlike
# :data:`CROSS_KIND_ALIASES` (which all expand to *every* search-hits kind),
# this expands to exactly the two kinds below, never more, never fewer.
CROSS_KIND_SOURCE_ALIAS = "source"
CROSS_KIND_SOURCE_KINDS: tuple[str, ...] = ("paper", "finding")

# The citeable kinds (:data:`precis.utils.refeye._CITED_KINDS`, mirrored
# here to avoid an import into a low-level shared module) whose ``search()``
# / ``search_hits()`` have no SQL-level ``exclude_ref_ids`` wiring: patent's
# local+OPS-remote search and edgar's filing search never route through
# ``store.chunks.search_chunks*``, unlike paper/cfp/datasheet (the
# ``PaperHandler`` family). ``search(uncited=...)``'s exclusion set can
# legitimately contain a ref of either kind (a draft may cite a patent or an
# EDGAR filing), so combining ``uncited=`` with an *explicit* request for one
# of these kinds raises rather than silently returning hits that might
# already be cited; the default wildcard cross-kind fan-out instead drops
# them from the merge (footer-noted) so the common unscoped call still
# works. See ``runtime.search._dispatch_cross_kind`` /
# ``runtime.dispatch._dispatch_inner_core``.
UNCITED_UNSUPPORTED_KINDS: frozenset[str] = frozenset({"patent", "edgar"})

#: Payload key the search-facet resolutions (``uncited=``/``cited=``/
#: ``hubbed=``/``under=``) use to record the caller's original keys after
#: rewriting them into the ``exclude_ref_ids``/``include_ref_ids``
#: channel — ``runtime.search._search_call_text`` re-renders the
#: originals in ``next=`` hints instead of the resolved ref-id lists.
#: Dispatch-internal, never a caller key: popped before the handler
#: kwargs gate. Shape: ``{channel: [(orig_key, orig_value), ...]}``.
RESOLVED_FACETS_KEY = "__resolved_facets__"


def refuse_unapplied_args(
    args: dict[str, Any],
    *,
    consumed: frozenset[str],
    shape: str,
    next: str | Sequence[str] | Callable[[list[str]], str | Sequence[str]],
    tolerated: dict[str, tuple[Any, ...]] | None = None,
    ignored: frozenset[str] | set[str] = frozenset(),
) -> None:
    """Refuse caller args an intercepted ``search`` shape never reads.

    The single-kind path vets payload keys against the resolved
    handler's signature (gr334695); the dispatch-level interceptions
    (cross-kind fan-out, source search, the sprays and backlog views)
    build their own kwarg set for the store/per-kind calls and never
    see that gate — a key they don't consume was silently dropped
    (gr524663: ``kinds=[...]`` on a kind-less ``search`` ran the ~34-kind
    fan-out under only a deprecation note, looking applied). Each
    interception declares the payload keys it consumes in ``consumed``
    and calls this before doing any work; whatever remains — top-level
    keys, plus every leftover ``args={...}`` key (``__extras__`` mirrors
    ``dispatch._EXTRAS_KEY``) — is refused with ``next`` naming the
    corrected call. ``tolerated`` maps a key to the values that count
    as not supplied (the ``page=1`` wrapper default); ``ignored`` names
    keys a path deliberately no-ops on, a contract its tests pin (e.g.
    ``q=`` on the backlog views — documented, not a silent drop).
    ``next`` may be a callable taking the refused plain key names
    (top-level and ``args=`` alike) and returning the hint(s) — for a
    call site whose corrected call depends on which keys were refused
    (mcp-authoring rule 2: ``next=`` is a call that works).
    """
    tolerated = tolerated or {}
    stray = sorted(
        k
        for k, v in args.items()
        if k not in consumed
        and k not in ignored
        and not k.startswith("__")
        and v is not None
        and v not in tolerated.get(k, ())
    )
    extras = args.get("__extras__")
    stray_extras = (
        sorted(k for k, v in extras.items() if v is not None)
        if isinstance(extras, dict)
        else []
    )
    if not stray and not stray_extras:
        return
    names = stray + [f"args[{k!r}]" for k in stray_extras]
    built = next(stray + stray_extras) if callable(next) else next
    hints: list[str] = [built] if isinstance(built, str) else list(built)
    if "kinds" in stray:
        # The most-misread name (gr524663): on a kind-less call the
        # caller plainly meant to choose the searched kinds.
        hints.insert(
            0,
            "kinds= is the kind='skill' catalogue axis, not a kind "
            "selector — to choose which kinds are searched pass "
            "kind='a,b' (e.g. search(kind='memory,finding', q='…'))",
        )
    if "page" in stray:
        hints.append(
            "search returns a bounded top-N set — page_size= sizes it "
            "(max 100); refine q= for more, there is no page=2"
        )
    raise BadInput(
        f"{names!r} {'is' if len(names) == 1 else 'are'} not applied by "
        f"{shape} — silently dropping "
        f"{'it' if len(names) == 1 else 'them'} would return a result "
        "the call didn't ask for",
        next=hints,
    )


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
    default_tags_resolved: tuple[str, ...]
    boot_migration_head: str | None

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

    def _maybe_add_kind_skills_hint(
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
    def _dispatch_stubs(self, kind: Any, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _dispatch_chase_queue(self, kind: Any, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _dispatch_cross_kind(self, kind: str, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _dispatch_search_plan(self, kind: Any, args: dict[str, Any]) -> Response:
        raise NotImplementedError

    def _is_source_search_request(self, args: dict[str, Any]) -> bool:
        raise NotImplementedError

    def _routes_to_source_search(
        self, verb: str, kind: Any, args: dict[str, Any]
    ) -> bool:
        raise NotImplementedError

    def _dispatch_source_search(
        self, kind: str | None, args: dict[str, Any]
    ) -> Response:
        raise NotImplementedError

    def _is_cross_kind_request(self, kind: Any) -> bool:
        raise NotImplementedError

    def _resolve_folder_scope(self, value: Any) -> tuple[set[int], str]:
        raise NotImplementedError

    def _cross_kind_kinds(self) -> list[str]:
        raise NotImplementedError

    # ── dispatch.py (DispatchMixin) ──────────────────────────────────────
    def _expand_kind_code(self, kind: str) -> str:
        raise NotImplementedError

    # ── introspect.py (IntrospectionMixin) ───────────────────────────────
    def _dispatch_introspect_view(
        self, view: str, kind: str | None, args: dict[str, Any]
    ) -> Response:
        raise NotImplementedError
