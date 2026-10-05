"""Postgres-backed store for precis V2 (psycopg3, sync).

Public surface:
    Store              — high-level store handle, owns the psycopg pool
    Migrator           — forward-only migration runner

    Ref, ChunkRow, Link, Tag, CacheEntry, ChunkInsert  — frozen row types
    Density, CacheFreshness, Namespace, Relation, ActorSlug  — type aliases

Typing the seam: functions that need only a sliver of the Store take a
role protocol from :mod:`precis.store.protocols` (import-light, no
cycles) instead of ``Store`` or ``Any``. ``Hub.store`` is typed
``Store | None``; ``Hub.live_store`` narrows it for store-backed paths.

Drive date ordering uses existing ref timestamps: ``recent_refs(created=True)``
orders browse by creation; cross-kind chunk search accepts ``created`` and
``modified`` alongside legacy ``recency``. Explicit new names avoid changing
existing callers' modification-first browse or creation-first search order;
both new orders use ref id ties and require no derived clock or schema.

Capped paper search opts into ``search_chunks_multi(prefer_body=True)``:
discard a synthetic card when the fused candidate pool contains a body hit
for that ref, before the diversity cap and page slice. Otherwise a stronger
card can consume the paper's only slot and hide quotable evidence. Card-only
matches survive; other store consumers retain their ranking by default.

Decomposition (in progress, codereview-store-decomposition): the
stateful pool/tx lifecycle lives in :class:`precis.store.core.StoreCore`;
domain sub-stores hold a core and are reached as composed properties —
``store.drafts`` (:class:`precis.store._draft_ops.DraftStore`) is the
first, fully carved: draft ops exist only on the sub-store (no flat
delegations remain on ``Store``).

The schema is defined in `src/precis/migrations/0001_initial.sql`.
Generated baselines provision every extension prerequisite and load the full
seed vocabulary with a transaction-local public search path, so sealed seed
triggers resolve their helpers without changing function bodies or permissions.

Body word counts derive from current chunks in a batched read, so existing
refs need no counter backfill. POSIX whitespace avoids double interpretation
of backslashes by Python, SQL escape strings and the regex engine.

Measure ranking reads the newest content-current ledger verdict in its
candidate query and excludes rejected rows. Historical or stale rejection
cannot veto a current approval; proposed and unreviewed values retain the
numeric ordering. The winning row exposes ``review_state`` so consumers can
distinguish eligible values from approved ones without another ledger read.
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
    CacheEntry,
    CacheFreshness,
    ChunkInsert,
    ChunkRow,
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
    "CacheEntry",
    "CacheFreshness",
    "ChunkInsert",
    "ChunkRow",
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
