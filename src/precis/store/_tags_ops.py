"""Tag CRUD against the v2 unified ``tags`` + ``ref_tags`` /
``chunk_tags`` model. Mixin on :class:`precis.store.Store`.

**Phase 3 stub** — every public method raises ``NotImplementedError``
pointing at the plan file. v1 stored tags in three separate tables
(``ref_closed_tags`` / ``ref_flags`` / ``ref_open_tags``); v2 collapsed
them into ``tags(tag_id, namespace, value)`` + ``ref_tags``/``chunk_tags``
joins. The rewrite design lives in the plan; see Phase 3 §"unified tag
dispatch" for the SQL patterns (uses ``INSERT ... ON CONFLICT DO
UPDATE SET <no-op> RETURNING`` for the tags-table upsert so RETURNING
fires on both paths without a fragile fallback SELECT).

API changes landed by Phase 3:

- ``has_tag(ref_id, namespace, value) -> bool`` replaces v1
  ``has_flag(ref_id, name)``. The v2 ``tags(namespace, value)`` schema
  makes the v1 carve-out for flag-namespace tags pointless — one
  predicate (``WHERE t.namespace = 'FLAG' AND t.value = …``) serves
  every check uniformly. Six test sites need updating; no production
  handler calls ``has_flag``.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store.types import ActorSlug, Tag

_PHASE_3_MSG = (
    "phase 3 (tags v2 rewrite) not yet implemented; see "
    "/Users/reto/.claude/plans/lively-yawning-kahn.md"
)


class TagsMixin:
    """v2 tag CRUD. All methods stubbed — Phase 3 of the store rewrite."""

    pool: ConnectionPool

    def add_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        set_by: ActorSlug = "agent",
        replace_prefix: bool = False,
        conn: Connection | None = None,
    ) -> None:
        raise NotImplementedError(f"add_tag: {_PHASE_3_MSG}")

    def remove_tag(
        self,
        ref_id: int,
        tag: Tag,
        *,
        pos: int | None = None,
        conn: Connection | None = None,
    ) -> None:
        raise NotImplementedError(f"remove_tag: {_PHASE_3_MSG}")

    def tags_for(
        self,
        ref_id: int,
        *,
        pos: int | None = None,
    ) -> list[Tag]:
        raise NotImplementedError(f"tags_for: {_PHASE_3_MSG}")

    def has_tag(self, ref_id: int, namespace: str, value: str) -> bool:
        """v2 unified tag-presence probe.

        Phase 3 will resolve via ``SELECT 1 FROM ref_tags rt JOIN tags t
        USING (tag_id) WHERE rt.ref_id = %s AND t.namespace = %s AND
        t.value = %s LIMIT 1`` (or the ``chunk_tags`` variant when a
        chunk-level probe is needed). Replaces v1 ``has_flag``.
        """
        raise NotImplementedError(f"has_tag: {_PHASE_3_MSG}")

    def find_first_meta_for_open_tag(
        self,
        *,
        kind: str,
        tag: str,
    ) -> dict[str, Any] | None:
        raise NotImplementedError(f"find_first_meta_for_open_tag: {_PHASE_3_MSG}")


__all__ = ["TagsMixin"]
