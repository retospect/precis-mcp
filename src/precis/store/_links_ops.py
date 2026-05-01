"""Link CRUD. Mixin on :class:`precis.store.Store`.

One row per edge. Asymmetric relations (``cites`` / ``cited-by``)
are stored only in the direction the agent named at write time;
the inverse is re-derived at read time in :meth:`LinksMixin.links_for`
via :data:`precis.store.types._INVERSE_RELATIONS`. This keeps the
unique-edge invariant intact and lets agents query either side
without knowing the schema-side asymmetry.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any, Literal

from psycopg import Connection
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from precis.errors import BadInput
from precis.store._mappers import _pos_to_db, _row_to_link
from precis.store.types import _INVERSE_RELATIONS, ActorSlug, Link, Relation


class LinksMixin:
    """Link insert / remove / read with inverse-relation rewrite."""

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
        """Insert a link row, idempotent on the unique tuple.

        The schema's ``UNIQUE (src_ref_id, src_pos, dst_ref_id,
        dst_pos, relation)`` means a re-insert with the same
        arguments is a no-op. We use ``ON CONFLICT (...) DO UPDATE
        SET set_by = links.set_by`` so the ``RETURNING`` clause
        yields the existing row on conflict — this avoids the extra
        SELECT that ``DO NOTHING`` would force.

        Identity self-loops (same ref + same pos) are rejected by
        the schema's ``CHECK`` constraint, surfaced here as a
        ``BadInput`` because that's the right error class for an
        agent-driven misuse.

        Same-ref different-pos links are allowed (e.g. ``block~5 →
        block~7`` within one long memory ref) — the check is
        position-aware.

        **One row per edge.** Asymmetric pairs (``cites`` /
        ``cited-by``) are NOT auto-mirrored — exactly one row is
        inserted regardless of relation. The "who cites me?"
        filter that motivated the MCP critic's request is solved
        at *read* time in :meth:`links_for`, which rewrites
        ``relation='cited-by'`` into a dst-side match against
        ``relation='cites'``. This keeps the unique-edge invariant
        intact, avoids drift between the two sides, and matches
        the design choice documented in
        ``migrations/0005_link_relations.sql``.
        """
        if src_ref_id == dst_ref_id and _pos_to_db(src_pos) == _pos_to_db(dst_pos):
            raise BadInput(
                "cannot link a ref to itself at the same position",
                next=(
                    "use different src_pos/dst_pos if linking blocks "
                    "within one ref, or pick a different target"
                ),
            )
        sql = """
            INSERT INTO links
                (src_ref_id, src_pos, dst_ref_id, dst_pos,
                 relation, set_by, meta)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (src_ref_id, src_pos, dst_ref_id, dst_pos, relation)
            DO UPDATE SET set_by = links.set_by
            RETURNING id, src_ref_id, src_pos, dst_ref_id, dst_pos,
                      relation, set_by, meta, created_at
        """
        params = (
            src_ref_id,
            _pos_to_db(src_pos),
            dst_ref_id,
            _pos_to_db(dst_pos),
            relation,
            set_by,
            Jsonb(meta or {}),
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_link(row)

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
        """Remove links matching the given ``(src, dst, [pos pair, [relation]])``.

        ``relation=None`` removes **all** links between the given
        positions regardless of relation. The handler-level
        ``unlink=`` kwarg always passes a specific relation so this
        broader form is a Store-only escape hatch (used by tests
        and future bulk operations). Returns the number of rows
        deleted; missing links are a silent no-op (rowcount=0).

        Asymmetric pairs (``cites`` / ``cited-by``) are stored as a
        single row whose direction is the one the agent named at
        write time. Removing it removes the edge regardless of
        which inverse name is in flight at read time (see
        :meth:`links_for` for the read-side rewrite).
        """
        clauses = [
            "src_ref_id = %s",
            "src_pos = %s",
            "dst_ref_id = %s",
            "dst_pos = %s",
        ]
        params: list[Any] = [
            src_ref_id,
            _pos_to_db(src_pos),
            dst_ref_id,
            _pos_to_db(dst_pos),
        ]
        if relation is not None:
            clauses.append("relation = %s")
            params.append(relation)
        sql = f"DELETE FROM links WHERE {' AND '.join(clauses)}"
        if conn is not None:
            cur = conn.execute(sql, params)
        else:
            with self.pool.connection() as c:
                cur = c.execute(sql, params)
        return cur.rowcount

    def links_for(
        self,
        ref_id: int,
        *,
        direction: Literal["out", "in", "both"] = "both",
        relation: Relation | None = None,
    ) -> list[Link]:
        """Fetch links touching ``ref_id``.

        ``direction='out'``: rows where ref_id is the source.
        ``direction='in'``:  rows where ref_id is the destination.
        ``direction='both'`` (default): both, no deduplication —
        a self-link (different positions) shows up twice and that's
        correct.

        ``relation=None`` returns every relation. Inbound rows keep
        their stored relation slug; the renderer maps to inverse
        labels via ``relations.inverse_slug`` for human-readable
        prose.

        **Inverse-relation rewrite.** When ``relation`` is the
        inverse half of an asymmetric pair (e.g. ``'cited-by'``,
        which is never stored — only ``'cites'`` is) the filter
        is rewritten so both physical encodings of the edge are
        returned. Concretely, ``relation='cited-by',
        direction='out'`` matches:

        * literal ``cited-by`` rows where this ref is src (rare),
          AND
        * ``cites`` rows where this ref is dst (the canonical
          encoding of the same edge from the cited side).

        This solves the "who cites me?" filter the MCP critic
        flagged: agents can write
        ``links_for(B, relation='cited-by', direction='out')`` and
        get the citation graph from B's perspective without
        knowing the schema-side asymmetry. Returned ``Link`` rows
        keep their *stored* relation slug — the caller compares
        against the requested filter to label them, exactly the
        same job the renderer already does for ``direction='both'``.
        """
        # The role-match logic: when the caller asks for relation X
        # in direction D, also accept rows storing inverse(X) in
        # the opposite direction. The two conditions are unioned.
        inverse = _INVERSE_RELATIONS.get(relation) if relation is not None else None

        clauses: list[str] = []
        params: list[Any] = []

        def _direction_clause(direction: str) -> tuple[str, list[Any]]:
            if direction == "out":
                return "src_ref_id = %s", [ref_id]
            if direction == "in":
                return "dst_ref_id = %s", [ref_id]
            return "(src_ref_id = %s OR dst_ref_id = %s)", [ref_id, ref_id]

        if inverse is None:
            # No inverse rewrite needed — single direction clause +
            # optional relation filter.
            d_clause, d_params = _direction_clause(direction)
            clauses.append(d_clause)
            params.extend(d_params)
            if relation is not None:
                clauses.append("relation = %s")
                params.append(relation)
        else:
            # Disjunction: literal-relation in the requested
            # direction OR inverse-relation in the opposite
            # direction. ``opposite`` is straightforward; for
            # ``both``, both halves use ``both`` — every row
            # qualifies under the relation OR inverse-relation
            # branch. We then dedupe by id at the Python boundary.
            opposite_dir = {"out": "in", "in": "out", "both": "both"}[direction]
            d_left, p_left = _direction_clause(direction)
            d_right, p_right = _direction_clause(opposite_dir)
            clauses.append(
                f"(({d_left} AND relation = %s) OR ({d_right} AND relation = %s))"
            )
            params.extend([*p_left, relation, *p_right, inverse])

        sql = (
            "SELECT id, src_ref_id, src_pos, dst_ref_id, dst_pos, "
            "       relation, set_by, meta, created_at "
            f"FROM links WHERE {' AND '.join(clauses)} "
            "ORDER BY created_at ASC"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        # Dedupe by id — when ``direction='both'`` and an inverse
        # rewrite is in play, the same row could match both halves
        # of the OR (a self-link with different positions is the
        # only realistic case in this schema, but the dedupe is
        # cheap and defensive).
        seen: set[int] = set()
        out: list[Link] = []
        for r in rows:
            link_id = r[0]
            if link_id in seen:
                continue
            seen.add(link_id)
            out.append(_row_to_link(r))
        return out


__all__ = ["LinksMixin"]
