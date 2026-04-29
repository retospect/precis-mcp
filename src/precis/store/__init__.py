"""Async postgres-backed store for precis V2.

Public surface:
    Store              — high-level store handle, owns the asyncpg pool
    Migrator           — forward-only migration runner

    Ref, Block, Link, Tag, CacheEntry, BlockInsert  — frozen row types
    Density, CacheFreshness, Namespace, Relation, ActorSlug  — type aliases

The schema is defined in `src/precis/migrations/0001_initial.sql`.
"""

from __future__ import annotations

from precis.store.migrate import Migrator
from precis.store.store import SEMANTIC_DISTANCE_FLOOR, Store
from precis.store.types import (
    ActorSlug,
    Block,
    BlockInsert,
    CacheEntry,
    CacheFreshness,
    Density,
    Link,
    Namespace,
    Ref,
    Relation,
    Tag,
)

__all__ = [
    "SEMANTIC_DISTANCE_FLOOR",
    "ActorSlug",
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
    "Store",
    "Tag",
]
