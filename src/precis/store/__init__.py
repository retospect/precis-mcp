"""Async postgres-backed store for precis V2.

Public surface:
    Store              — high-level store handle, owns the asyncpg pool
    Migrator           — forward-only migration runner

    Ref, Block, Link, Tag, CacheEntry, BlockInsert  — frozen row types
    Density, CacheFreshness, Namespace, Relation, ActorSlug  — type aliases

Typing the seam: functions that need only a sliver of the Store take a
role protocol from :mod:`precis.store.protocols` (import-light, no
cycles) instead of ``Store`` or ``Any``. ``Hub.store`` is typed
``Store | None``; ``Hub.live_store`` narrows it for store-backed paths.

Decomposition (in progress, codereview-store-decomposition): the
stateful pool/tx lifecycle lives in :class:`precis.store.core.StoreCore`;
domain sub-stores hold a core and are reached as composed properties —
``store.drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the
first, fully carved: draft ops exist only on the sub-store (no flat
delegations remain on ``Store``).

The schema is defined in `src/precis/migrations/0001_initial.sql`.
"""

from __future__ import annotations

from precis.store._salience import (
    as_background_actor,
    as_dream_actor,
    background_actor_active,
    current_background_actor,
)
from precis.store.migrate import Migrator
from precis.store.store import SEMANTIC_DISTANCE_FLOOR, Store
from precis.store.types import (
    ActorSlug,
    BibEntry,
    Block,
    BlockInsert,
    CacheEntry,
    CacheFreshness,
    Density,
    Link,
    Namespace,
    Ref,
    Relation,
    S2Direction,
    S2Neighbor,
    Tag,
)

__all__ = [
    "SEMANTIC_DISTANCE_FLOOR",
    "ActorSlug",
    "BibEntry",
    "Block",
    "BlockInsert",
    "CacheEntry",
    "CacheFreshness",
    "Density",
    "Link",
    "Migrator",
    "Namespace",
    "Ref",
    "Relation",
    "S2Direction",
    "S2Neighbor",
    "Store",
    "Tag",
    "as_background_actor",
    "as_dream_actor",
    "background_actor_active",
    "current_background_actor",
]
