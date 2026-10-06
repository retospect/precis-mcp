"""Relation-vocabulary validation shared by handlers and workers.

Lives in ``utils`` so ``precis.workers`` can validate ``rel=`` without
importing ``precis.handlers``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, cast, get_args

from precis.errors import BadInput
from precis.store.types import Relation

if TYPE_CHECKING:
    from precis.store import Store

# Mirror of the ``Relation`` literal's allowed values. Surfacing
# the tuple form here lets ``validate_relation`` enumerate options
# for the agent's BadInput hint without re-importing typing
# internals at every call.
_VALID_RELATIONS: tuple[str, ...] = get_args(Relation)
_DEFAULT_RELATION: Relation = "related-to"


def validate_relation(rel: str | None, *, store: Store | None = None) -> Relation:
    """Validate ``rel=`` against the registered relations vocabulary.

    Returns the canonical default ``related-to`` when ``rel`` is
    ``None`` so callers can pass the result straight to
    :meth:`Store.add_link`. Raises ``BadInput`` with the full
    options list when an unknown relation is given — that's
    cheaper feedback than the FK violation the DB would otherwise
    return at insert time.

    Built-ins in the ``Relation`` literal are always accepted (fast,
    store-free path — keeps the typo-safety guarantee). When a
    ``store`` is supplied, an *unknown* slug is checked against the
    live ``relations`` table so a **plugin**-registered relation
    (seeded by the plugin's migration, e.g. autocatpath's ``pathway-node``
    or a chem route's ``consumes``/``produces``) is accepted without a
    core edit to the literal. The DB FK is the durable guard; this
    pre-flight just opens the friendly handler-layer check to plugins.
    """
    if rel is None:
        return _DEFAULT_RELATION
    if rel in _VALID_RELATIONS:
        # Narrow ``str`` → ``Relation`` (Literal) for downstream type-
        # checkers; the membership check above is the runtime guarantee.
        return cast(Relation, rel)
    if store is not None:
        extra = store.valid_relations()
        if rel not in extra:
            # Miss against the cache — re-read once in case a relation
            # was registered after the store's vocabulary was first read
            # (plugin migrated mid-process, or a test seeded one).
            extra = store.valid_relations(refresh=True)
        if rel in extra:
            return cast(Relation, rel)
        options = sorted({*_VALID_RELATIONS, *extra})
    else:
        options = list(_VALID_RELATIONS)
    raise BadInput(
        f"unknown relation: {rel!r}",
        options=options,
        next=(
            "pick from the registered relations or omit rel= "
            f"for the default {_DEFAULT_RELATION!r}"
        ),
    )
