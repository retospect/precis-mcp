"""Link CRUD against the v2 ``links`` table. Mixin on
:class:`precis.store.Store`.

**Phase 4 stub** — every public method raises ``NotImplementedError``
pointing at the plan file. v1 stored link endpoints as ``src_pos`` /
``dst_pos`` ints with a ``-1`` sentinel for "ref-level"; v2 schema
uses NULL-able ``src_chunk_id`` / ``dst_chunk_id`` FKs to
``chunks(chunk_id)``. The translation between agent-facing ``pos``
(the chunk's ord) and the DB's ``chunk_id`` requires a per-call
lookup on insert (``SELECT chunk_id FROM chunks WHERE ref_id = %s
AND ord = %s``) and reverse-lookup on read.

Inverse-relation rewrite (``cites`` ↔ ``cited-by`` etc.) survives
the rewrite unchanged — it's a pure Python concern that doesn't
touch SQL.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection
from psycopg_pool import ConnectionPool

from precis.store.types import ActorSlug, Link, Relation

_PHASE_4_MSG = (
    "phase 4 (links v2 rewrite — pos↔chunk_id translation) not yet "
    "implemented; see /Users/reto/.claude/plans/lively-yawning-kahn.md"
)


class LinksMixin:
    """v2 link CRUD. All methods stubbed — Phase 4 of the store rewrite."""

    pool: ConnectionPool

    def add_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: Relation = "related-to",
        src_pos: int | None = None,
        dst_pos: int | None = None,
        set_by: ActorSlug = "agent",
        meta: dict[str, Any] | None = None,
        conn: Connection | None = None,
    ) -> Link:
        raise NotImplementedError(f"add_link: {_PHASE_4_MSG}")

    def remove_link(
        self,
        *,
        src_ref_id: int,
        dst_ref_id: int,
        relation: Relation | None = None,
        src_pos: int | None = None,
        dst_pos: int | None = None,
        conn: Connection | None = None,
    ) -> int:
        raise NotImplementedError(f"remove_link: {_PHASE_4_MSG}")

    def links_for(
        self,
        ref_id: int,
        *,
        relation: Relation | None = None,
        direction: Literal["in", "out", "both"] = "both",
        pos: int | None = None,
    ) -> list[Link]:
        raise NotImplementedError(f"links_for: {_PHASE_4_MSG}")


__all__ = ["LinksMixin"]
