"""Link CRUD against the v2 ``links`` table. Mixin on
:class:`precis.store.Store`.

v2 schema notes:

- ``links.id`` → ``links.link_id`` (column renamed; aliased back
  to ``id`` in SELECT so the dataclass shape stays stable)
- ``links.src_pos`` / ``dst_pos`` (int, with v1 ``-1`` sentinel for
  "ref-level") → ``src_chunk_id`` / ``dst_chunk_id`` (NULL for
  ref-level; FK to ``chunks(chunk_id)``)
- UNIQUE ``(src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
  relation) NULLS NOT DISTINCT`` — NULLS NOT DISTINCT preserves
  the v1 dedup invariant for ref-level edges (two NULL chunk_ids
  collide as duplicates the way v1's two -1 sentinels did).

API-side ``pos`` (the chunk's ord) is the agent-facing convention;
this module translates pos↔chunk_id at the boundary:

- On INSERT: ``pos!=None`` triggers ``SELECT chunk_id FROM chunks
  WHERE ref_id = %s AND ord = %s`` lookup (raises ``BadInput`` on
  missing chunk — caller's contract is "the chunk exists").
- On SELECT: LEFT JOIN ``chunks`` twice (one per endpoint) and
  project ``ord`` back as ``pos``. NULL chunk_id → NULL ord →
  None ``pos`` directly; no sentinel translation.

Inverse-relation rewrite at read time
(``relation='cited-by'`` → match ``cites`` rows with the ref on
the dst side) carries over unchanged from v1.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import _lookup_chunk_id, _row_to_link
from precis.store.types import _INVERSE_RELATIONS, ActorSlug, Link, Relation


def _resolve_chunk_id_for_link(
    conn: Connection, ref_id: int, ord_: int | None
) -> int | None:
    """Translate ``pos`` (= ``chunks.ord``) → ``chunks.chunk_id``.

    Returns ``None`` when ``ord_`` is ``None`` (ref-level link).
    Raises ``BadInput`` when ``ord_`` is set but no chunk exists at
    ``(ref_id, ord)`` — silently inserting NULL there would dedupe
    against the wrong row under the NULLS NOT DISTINCT unique
    constraint and corrupt the link graph.
    """
    if ord_ is None:
        return None
    chunk_id = _lookup_chunk_id(conn, ref_id, ord_)
    if chunk_id is None:
        raise BadInput(
            f"no chunk at (ref_id={ref_id}, ord={ord_}) — "
            "can't link to a chunk that doesn't exist",
            next=f"check chunks: get(kind=..., id={ref_id})",
        )
    return chunk_id


# Standard SELECT projection for links: maps link_id back to id and
# resolves chunk_id endpoints back to ord via LEFT JOIN against
# chunks. Mirrors :func:`_row_to_link`'s tuple layout.
_LINK_SELECT_PROJ = (
    "l.link_id AS id, "
    "l.src_ref_id, "
    "sc.ord AS src_pos, "
    "l.dst_ref_id, "
    "dc.ord AS dst_pos, "
    "l.relation, "
    "l.set_by, "
    "l.meta, "
    "l.created_at"
)
_LINK_SELECT_FROM = (
    "FROM links l "
    "LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id "
    "LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id"
)


class LinksMixin:
    """v2 link insert / remove / read with inverse-relation rewrite."""

    pool: ConnectionPool
    soft_delete_ref: Any  # provided by RefsMixin (used by merge_refs)

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
        """Insert a link row, idempotent on the unique tuple.

        v2 ``UNIQUE (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
        relation) NULLS NOT DISTINCT`` preserves the v1 dedup invariant:
        re-inserting the same edge is a no-op via
        ``ON CONFLICT ... DO UPDATE SET set_by = links.set_by``, the
        same no-op-update trick used elsewhere so RETURNING fires on
        both insert and conflict paths.

        Self-loop CHECK: same ref + same chunk endpoint (both NULL or
        both same chunk_id) is rejected by the schema; app-layer
        ``BadInput`` here so an agent mistake surfaces with a recovery
        hint rather than a psycopg ``CheckViolation``.

        One row per edge — asymmetric pairs (``cites`` / ``cited-by``)
        are NOT auto-mirrored. The "who cites me?" filter is handled
        at read time in :meth:`links_for`.
        """

        def _do(c: Connection) -> Link:
            src_chunk_id = _resolve_chunk_id_for_link(c, src_ref_id, src_pos)
            dst_chunk_id = _resolve_chunk_id_for_link(c, dst_ref_id, dst_pos)

            # Self-loop check: same ref + same chunk endpoint (both
            # NULL ⇒ ref-level self-loop; both same chunk_id ⇒
            # chunk-level self-loop).
            if src_ref_id == dst_ref_id and src_chunk_id == dst_chunk_id:
                raise BadInput(
                    "cannot link a ref to itself at the same position",
                    next=(
                        "use different src_pos/dst_pos if linking chunks "
                        "within one ref, or pick a different target"
                    ),
                )

            sql = (
                "INSERT INTO links "
                "  (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, "
                "   relation, set_by, meta) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s) "
                "ON CONFLICT "
                "  (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation) "
                "DO UPDATE SET set_by = links.set_by "
                "RETURNING link_id"
            )
            row = c.execute(
                sql,
                (
                    src_ref_id,
                    src_chunk_id,
                    dst_ref_id,
                    dst_chunk_id,
                    relation,
                    set_by,
                    Jsonb(meta or {}),
                ),
            ).fetchone()
            assert row is not None, (
                "links INSERT returned no row — schema invariant violated"
            )
            link_id = int(row[0])

            # Re-SELECT through the standard projection so the
            # returned Link carries the LEFT-JOIN-translated pos
            # fields (ord values, not chunk_id ints).
            fetched = c.execute(
                f"SELECT {_LINK_SELECT_PROJ} {_LINK_SELECT_FROM} WHERE l.link_id = %s",
                (link_id,),
            ).fetchone()
            assert fetched is not None
            return _row_to_link(fetched)

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

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
        """Remove links matching ``(src, dst, [chunk pair, [relation]])``.

        ``relation=None`` removes **all** links between the given
        endpoints regardless of relation. Returns the number of
        rows deleted; missing links are a silent no-op.

        Uses ``IS NOT DISTINCT FROM`` for the chunk_id predicates so
        ``None`` ↔ NULL matching aligns with the UNIQUE NULLS NOT
        DISTINCT semantics on the index.
        """

        def _do(c: Connection) -> int:
            src_chunk_id = _resolve_chunk_id_for_link(c, src_ref_id, src_pos)
            dst_chunk_id = _resolve_chunk_id_for_link(c, dst_ref_id, dst_pos)
            clauses = [
                "src_ref_id = %s",
                "src_chunk_id IS NOT DISTINCT FROM %s",
                "dst_ref_id = %s",
                "dst_chunk_id IS NOT DISTINCT FROM %s",
            ]
            params: list[Any] = [
                src_ref_id,
                src_chunk_id,
                dst_ref_id,
                dst_chunk_id,
            ]
            if relation is not None:
                clauses.append("relation = %s")
                params.append(relation)
            sql = f"DELETE FROM links WHERE {' AND '.join(clauses)}"
            cur = c.execute(sql, params)
            return cur.rowcount or 0

        if conn is not None:
            return _do(conn)
        with self.pool.connection() as c:
            return _do(c)

    def links_for(
        self,
        ref_id: int,
        *,
        direction: Literal["out", "in", "both"] = "both",
        relation: Relation | None = None,
    ) -> list[Link]:
        """Fetch links touching ``ref_id``.

        Inverse-relation rewrite carries over unchanged from v1:
        ``relation='cited-by'`` matches literal ``cited-by`` rows
        on this ref's side OR ``cites`` rows on the opposite side
        (the v2 schema doesn't store ``cited-by`` rows — only
        ``cites`` — so the rewrite is the only way to surface
        "who cites me?" links).
        """
        inverse = _INVERSE_RELATIONS.get(relation) if relation is not None else None

        def _direction_clause(direction_: str) -> tuple[str, list[Any]]:
            if direction_ == "out":
                return "l.src_ref_id = %s", [ref_id]
            if direction_ == "in":
                return "l.dst_ref_id = %s", [ref_id]
            return (
                "(l.src_ref_id = %s OR l.dst_ref_id = %s)",
                [ref_id, ref_id],
            )

        clauses: list[str] = []
        params: list[Any] = []

        if inverse is None:
            d_clause, d_params = _direction_clause(direction)
            clauses.append(d_clause)
            params.extend(d_params)
            if relation is not None:
                clauses.append("l.relation = %s")
                params.append(relation)
        else:
            opposite_dir = {"out": "in", "in": "out", "both": "both"}[direction]
            d_left, p_left = _direction_clause(direction)
            d_right, p_right = _direction_clause(opposite_dir)
            clauses.append(
                f"(({d_left} AND l.relation = %s) OR ({d_right} AND l.relation = %s))"
            )
            params.extend([*p_left, relation, *p_right, inverse])

        sql = (
            f"SELECT {_LINK_SELECT_PROJ} {_LINK_SELECT_FROM} "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY l.created_at ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()

        # Dedupe by link_id — under inverse rewrite + direction='both'
        # a row could match both halves of the OR; cheap defensive
        # dedupe.
        seen: set[int] = set()
        out: list[Link] = []
        for r in rows:
            link_id = r[0]
            if link_id in seen:
                continue
            seen.add(link_id)
            out.append(_row_to_link(r))
        return out

    def count_links_for_refs(self, ref_ids: list[int]) -> dict[int, int]:
        """Return ``{ref_id: total_link_count}`` for a batch of refs.

        Total = incoming + outgoing edges, undeduped at the link_id
        level (a link with the same src and dst would count twice —
        not a real case in the schema). Designed for the list-view
        TOON column so a single SQL round-trip covers a page.
        Missing ref ids in the result dict mean zero links.
        """
        if not ref_ids:
            return {}
        sql = (
            "SELECT ref_id, COUNT(*)::int FROM ("
            "  SELECT src_ref_id AS ref_id FROM links "
            "    WHERE src_ref_id = ANY(%s)"
            "  UNION ALL"
            "  SELECT dst_ref_id AS ref_id FROM links "
            "    WHERE dst_ref_id = ANY(%s)"
            ") sub GROUP BY ref_id"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, (ref_ids, ref_ids)).fetchall()
        return {int(r[0]): int(r[1]) for r in rows}

    def migrate_links(
        self,
        old_ref_id: int,
        new_ref_id: int,
        *,
        conn: Connection,
    ) -> int:
        """Re-point every link touching ``old_ref_id`` onto ``new_ref_id``.

        The link-migration step of a memory ``supersede`` merge: when
        an old memory is absorbed into a freshly-minted consolidated
        one, its graph position must follow so the survivor inherits
        every edge (and inbound provenance from papers etc. is
        preserved rather than orphaned by the soft-delete).

        Requires a caller-supplied ``conn`` because it is only ever run
        inside the ``supersede`` transaction (insert survivor →
        migrate links → add ``supersedes`` edge → soft-delete old), so
        the whole merge is atomic.

        Mechanics (mirrors the design doc, §Consolidate behavior):

        1. INSERT a substituted copy of every link where ``old_ref_id``
           is on either endpoint, swapping that endpoint to
           ``new_ref_id`` and keeping ``src_chunk_id`` / ``dst_chunk_id``
           (memory links are ref-level so those are NULL; a paper→memory
           link keeps the paper's chunk endpoint, which stays valid).
           ``ON CONFLICT DO NOTHING`` dedups against the
           ``NULLS NOT DISTINCT`` unique index; the ``NOT (...)`` guard
           drops rows that would become self-loops after substitution
           (the schema CHECK would otherwise raise, not conflict).
        2. DELETE the original rows touching ``old_ref_id``.

        Returns the number of old rows deleted (the migrated count;
        deduped duplicates collapse into existing survivor edges).
        """
        conn.execute(
            """
            INSERT INTO links
              (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id,
               relation, set_by, meta)
            SELECT
              CASE WHEN src_ref_id = %(old)s THEN %(new)s ELSE src_ref_id END,
              src_chunk_id,
              CASE WHEN dst_ref_id = %(old)s THEN %(new)s ELSE dst_ref_id END,
              dst_chunk_id,
              relation, set_by, meta
            FROM links
            WHERE (src_ref_id = %(old)s OR dst_ref_id = %(old)s)
              AND NOT (
                (CASE WHEN src_ref_id = %(old)s THEN %(new)s ELSE src_ref_id END)
                = (CASE WHEN dst_ref_id = %(old)s THEN %(new)s ELSE dst_ref_id END)
                AND src_chunk_id IS NOT DISTINCT FROM dst_chunk_id
              )
            ON CONFLICT
              (src_ref_id, src_chunk_id, dst_ref_id, dst_chunk_id, relation)
              DO NOTHING
            """,
            {"old": old_ref_id, "new": new_ref_id},
        )
        cur = conn.execute(
            "DELETE FROM links WHERE src_ref_id = %(old)s OR dst_ref_id = %(old)s",
            {"old": old_ref_id},
        )
        return cur.rowcount or 0

    def merge_refs(self, victim_ref_id: int, survivor_ref_id: int) -> int:
        """Absorb ``victim_ref_id`` into ``survivor_ref_id`` and retire the victim.

        The duplicate-paper resolver's primitive (same DOI / arXiv held
        twice). In one transaction it:

        1. re-points every link touching the victim onto the survivor
           (:meth:`migrate_links`), so the survivor inherits the victim's
           graph position rather than orphaning its edges;
        2. drops the victim's ``ref_identifiers`` rows — the
           uniqueness check (``set_ref_identifier``) ignores
           ``deleted_at``, so a bare soft-delete would leave the
           duplicate's DOI / arXiv / cite_key claimed and unassignable to
           the survivor;
        3. soft-deletes the victim.

        Returns the number of migrated link rows. Raises ``BadInput`` on a
        self-merge and ``NotFound`` (from :meth:`soft_delete_ref`) if the
        victim is missing or already deleted.
        """
        if victim_ref_id == survivor_ref_id:
            raise BadInput("cannot merge a ref into itself")
        with self.pool.connection() as conn:
            migrated = self.migrate_links(victim_ref_id, survivor_ref_id, conn=conn)
            conn.execute(
                "DELETE FROM ref_identifiers WHERE ref_id = %s", (victim_ref_id,)
            )
            self.soft_delete_ref(victim_ref_id, conn=conn)
        return migrated


__all__ = ["LinksMixin"]
