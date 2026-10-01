"""Store ops for the ``taxon`` hierarchy (stage B of
docs/backlog/term-taxonomy.md): traversal over the ``specialises`` /
``generalises`` DAG by recursive CTE. No closure table, no materialised
view, no index.

**Edge model.** "Y specialises X" is the directed edge child Y -> parent X.
The link row may be stored either way round (``specialises`` src=child,
dst=parent, or its inverse ``generalises`` src=parent, dst=child); the shared
``_EDGES`` CTE folds both forms into one ``(child, parent, axis)`` relation,
so every traversal below treats them as the same edge. ``axis`` is
``links.meta->>'axis'`` (free string, unvalidated in v1). Only ref-level
links between live (``retired_at IS NULL``) taxon refs are edges.

**Cycle safety.** Corrupted data (a cycle written around the guard) must not
hang a query: depth-tracking walks carry a path array and refuse to revisit a
node on their own path; set-valued walks use ``UNION`` (not ``UNION ALL``),
which terminates on a revisit.

**Rejected shapes.** No closure table or materialised view (10²–10³ nodes,
depth under ten: the CTE is milliseconds; revisit above 10⁴ nodes or a
``view='path'`` p95 over 50 ms on prod), no ``ltree`` (a tree cannot hold a
node with two parents), no stored primary parent or canonical path (a
reparent would have to rewrite every descendant). The display path is the
shortest chain to a start node, computed on read.

Mixin assumes the concrete Store provides ``self.pool``.
"""

from __future__ import annotations

from typing import Any

#: child -> parent edges among live taxon refs, both stored forms folded in.
_EDGES = """
e AS (
  SELECT x.child, x.parent, x.axis FROM (
    SELECT l.src_ref_id AS child, l.dst_ref_id AS parent,
           l.meta->>'axis' AS axis
      FROM links l
     WHERE l.relation = 'specialises'
       AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
    UNION
    SELECT l.dst_ref_id AS child, l.src_ref_id AS parent,
           l.meta->>'axis' AS axis
      FROM links l
     WHERE l.relation = 'generalises'
       AND l.src_chunk_id IS NULL AND l.dst_chunk_id IS NULL
  ) x
  WHERE EXISTS (SELECT 1 FROM refs c WHERE c.ref_id = x.child
                  AND c.kind = 'taxon' AND c.retired_at IS NULL)
    AND EXISTS (SELECT 1 FROM refs p WHERE p.ref_id = x.parent
                  AND p.kind = 'taxon' AND p.retired_at IS NULL)
)
"""

#: live taxon nodes with their start flag.
_NODES = """
nodes AS (
  SELECT ref_id, COALESCE((meta->>'start') = 'true', FALSE) AS is_start
    FROM refs
   WHERE kind = 'taxon' AND retired_at IS NULL
)
"""

#: Hard ceiling on enumerated chains — a diamond lattice multiplies paths,
#: and a read path must stay bounded whatever the data does.
_CHAIN_CEILING = 5000


class TaxonMixin:
    pool: Any

    def taxon_ancestors(self, ref_id: int) -> list[tuple[int, int, str | None]]:
        """Every ancestor of ``ref_id`` as ``(ref_id, depth, axis)`` rows —
        ``axis`` is the axis of the edge taken to reach it (depth 1 = a
        direct parent). A node reachable by several routes appears once per
        distinct ``(depth, axis)``. Ordered by depth, then id."""
        sql = f"""
        WITH RECURSIVE {_EDGES},
        walk(ref_id, depth, axis, path) AS (
          SELECT e.parent, 1, e.axis, ARRAY[%(r)s::bigint, e.parent]
            FROM e WHERE e.child = %(r)s
          UNION ALL
          SELECT e.parent, w.depth + 1, e.axis, w.path || e.parent
            FROM walk w JOIN e ON e.child = w.ref_id
           WHERE NOT e.parent = ANY(w.path)
        )
        SELECT DISTINCT ref_id, depth, axis FROM walk ORDER BY depth, ref_id, axis
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"r": ref_id}).fetchall()
        return [(int(r[0]), int(r[1]), r[2]) for r in rows]

    def taxon_descendants(
        self,
        ref_id: int,
        *,
        axis: str | None = None,
        max_depth: int | None = None,
    ) -> list[tuple[int, int, str | None]]:
        """Every descendant of ``ref_id`` as ``(ref_id, depth, axis)`` rows
        (``axis`` = the edge taken to reach it). ``axis=`` restricts EVERY
        hop to edges carrying that ``meta.axis``; ``max_depth=N`` means
        depth <= N (``None`` = unbounded). Stage C's ``search(under=,
        axis=, depth=)`` calls this — keep the signature stable."""
        sql = f"""
        WITH RECURSIVE {_EDGES},
        walk(ref_id, depth, axis, path) AS (
          SELECT e.child, 1, e.axis, ARRAY[%(r)s::bigint, e.child]
            FROM e
           WHERE e.parent = %(r)s
             AND (%(axis)s::text IS NULL OR e.axis = %(axis)s::text)
             AND (%(maxd)s::int IS NULL OR %(maxd)s::int >= 1)
          UNION ALL
          SELECT e.child, w.depth + 1, e.axis, w.path || e.child
            FROM walk w JOIN e ON e.parent = w.ref_id
           WHERE NOT e.child = ANY(w.path)
             AND (%(axis)s::text IS NULL OR e.axis = %(axis)s::text)
             AND (%(maxd)s::int IS NULL OR w.depth < %(maxd)s::int)
        )
        SELECT DISTINCT ref_id, depth, axis FROM walk ORDER BY depth, ref_id, axis
        """
        with self.pool.connection() as conn:
            rows = conn.execute(
                sql, {"r": ref_id, "axis": axis, "maxd": max_depth}
            ).fetchall()
        return [(int(r[0]), int(r[1]), r[2]) for r in rows]

    def _taxon_chains(
        self, ref_id: int
    ) -> list[tuple[bool, list[tuple[int, str | None]]]]:
        """Every maximal upward chain from ``ref_id``: ``(reaches_start,
        chain)``. A chain ends at the first start node (``True``) or, if it
        never meets one, at a node with no live parent (``False``). Each
        chain is ``[(ref_id, axis_of_edge_to_next), ...]`` node first; the
        last element's axis is ``None``. Shortest chains first, ties by
        axis names then ids."""
        sql = f"""
        WITH RECURSIVE {_EDGES}, {_NODES},
        walk(node, is_start, ids, axes) AS (
          SELECT n.ref_id, n.is_start, ARRAY[n.ref_id], ARRAY[]::text[]
            FROM nodes n WHERE n.ref_id = %(r)s
          UNION ALL
          SELECT e.parent, np.is_start, w.ids || e.parent,
                 array_append(w.axes, e.axis)
            FROM walk w
            JOIN e ON e.child = w.node
            JOIN nodes np ON np.ref_id = e.parent
           WHERE NOT w.is_start AND NOT e.parent = ANY(w.ids)
        )
        SELECT w.is_start, w.ids, w.axes FROM walk w
         WHERE w.is_start
            OR NOT EXISTS (SELECT 1 FROM e WHERE e.child = w.node)
         ORDER BY w.is_start DESC, array_length(w.ids, 1),
                  array_to_string(w.axes, ','), w.ids
         LIMIT {_CHAIN_CEILING}
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"r": ref_id}).fetchall()
        out: list[tuple[bool, list[tuple[int, str | None]]]] = []
        for is_start, ids, axes in rows:
            chain: list[tuple[int, str | None]] = [
                (int(rid), axes[i] if i < len(axes) else None)
                for i, rid in enumerate(ids)
            ]
            out.append((bool(is_start), chain))
        return out

    def taxon_paths(
        self, ref_id: int, *, limit: int = 20
    ) -> list[list[tuple[int, str | None]]]:
        """Every chain from ``ref_id`` up to a start node (``meta.start``),
        each an ordered list of ``(ref_id, axis_of_edge_to_next)`` with the
        start node last (axis ``None``). A start node's own path is the
        one-element chain. Shortest first; at most ``limit`` chains."""
        chains = [c for ok, c in self._taxon_chains(ref_id) if ok]
        return chains[:limit]

    def taxon_paths_report(
        self, ref_id: int, *, limit: int = 20
    ) -> tuple[
        list[list[tuple[int, str | None]]], int, list[list[tuple[int, str | None]]]
    ]:
        """``(chains, total, partial)``: the first ``limit`` start-reaching
        chains (as :meth:`taxon_paths`), how many exist in all, and — only
        when none reach a start node — the node's dead-end chains (each
        ending at a node with no live parent), capped at ``limit``."""
        every = self._taxon_chains(ref_id)
        rooted = [c for ok, c in every if ok]
        if rooted:
            return rooted[:limit], len(rooted), []
        return [], 0, [c for _ok, c in every][:limit]

    def taxon_start_nodes_reached(self, ref_id: int) -> list[int]:
        """Start-node ref ids reachable upward from ``ref_id`` (a start node
        reaches itself). Walks through start nodes — a start node that itself
        specialises another start node keeps both in play, since the
        contract is inherited from every start node above."""
        sql = f"""
        WITH RECURSIVE {_EDGES}, {_NODES},
        anc(ref_id) AS (
          SELECT n.ref_id FROM nodes n WHERE n.ref_id = %(r)s
          UNION
          SELECT e.parent FROM e JOIN anc a ON e.child = a.ref_id
        )
        SELECT a.ref_id FROM anc a JOIN nodes n ON n.ref_id = a.ref_id
         WHERE n.is_start ORDER BY a.ref_id
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"r": ref_id}).fetchall()
        return [int(r[0]) for r in rows]

    def taxon_would_cycle(self, child_id: int, parent_id: int) -> bool:
        """True when ``parent_id`` is ``child_id`` or ``child_id`` is
        already an ancestor of ``parent_id`` — i.e. adding the edge
        child -> parent would close a cycle."""
        if child_id == parent_id:
            return True
        sql = f"""
        WITH RECURSIVE {_EDGES},
        anc(ref_id) AS (
          SELECT %(p)s::bigint
          UNION
          SELECT e.parent FROM e JOIN anc a ON e.child = a.ref_id
        )
        SELECT EXISTS (SELECT 1 FROM anc WHERE ref_id = %(c)s)
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, {"p": parent_id, "c": child_id}).fetchone()
        return bool(row[0])

    def taxon_parents(self, ref_id: int) -> list[tuple[int, str | None]]:
        """Direct parents of ``ref_id`` as ``(ref_id, axis)``, ordered."""
        sql = f"""
        WITH {_EDGES}
        SELECT parent, axis FROM e WHERE child = %(r)s ORDER BY parent, axis
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"r": ref_id}).fetchall()
        return [(int(r[0]), r[1]) for r in rows]

    def taxon_child_count(self, ref_id: int) -> int:
        """Number of distinct direct children of ``ref_id``."""
        sql = f"""
        WITH {_EDGES}
        SELECT count(DISTINCT child) FROM e WHERE parent = %(r)s
        """
        with self.pool.connection() as conn:
            row = conn.execute(sql, {"r": ref_id}).fetchone()
        return int(row[0])

    def taxon_find_by_term(self, *, slug: str, norm: str) -> list[int]:
        """Live taxon refs whose ``meta.slug`` equals ``slug``, or whose
        ``meta.norm_name`` / any alias (whitespace-collapsed, lowercased)
        equals ``norm``. The resolver for path-form ids and the lexical leg
        of put-time dedup. Ordered by ref id."""
        sql = """
        SELECT r.ref_id FROM refs r
         WHERE r.kind = 'taxon' AND r.retired_at IS NULL
           AND (
                 (%(slug)s <> '' AND r.meta->>'slug' = %(slug)s)
              OR r.meta->>'norm_name' = %(norm)s
              OR EXISTS (
                   SELECT 1
                     FROM jsonb_array_elements_text(
                            CASE WHEN jsonb_typeof(r.meta->'aliases') = 'array'
                                 THEN r.meta->'aliases' ELSE '[]'::jsonb END
                          ) AS a(alias)
                    WHERE lower(regexp_replace(btrim(a.alias), '\\s+', ' ', 'g'))
                          = %(norm)s)
               )
         ORDER BY r.ref_id
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql, {"slug": slug, "norm": norm}).fetchall()
        return [int(r[0]) for r in rows]

    def taxon_unmapped(self) -> list[int]:
        """Live taxon refs seeded from a legacy registry (``meta.legacy_source``
        set) whose dimension label had no mapping (no ``dimension_kind``) — the
        seed's post-migration report. Categories are subjects and carry no
        dimension, so they are never "unmapped". Ordered by ref id."""
        sql = """
        SELECT ref_id FROM refs
         WHERE kind = 'taxon' AND retired_at IS NULL
           AND meta ? 'legacy_source'
           AND NOT (meta ? 'dimension_kind')
           AND meta->'legacy_source'->>'table' <> 'component_categories'
         ORDER BY ref_id
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [int(r[0]) for r in rows]

    def taxon_unrooted(self) -> list[int]:
        """Live taxon refs that reach no start node (the integrity check:
        every node must reach >= 1). Ordered by ref id."""
        sql = f"""
        WITH RECURSIVE {_EDGES}, {_NODES},
        reach(ref_id) AS (
          SELECT n.ref_id FROM nodes n WHERE n.is_start
          UNION
          SELECT e.child FROM e JOIN reach r ON e.parent = r.ref_id
        )
        SELECT n.ref_id FROM nodes n
         WHERE n.ref_id NOT IN (SELECT ref_id FROM reach) ORDER BY n.ref_id
        """
        with self.pool.connection() as conn:
            rows = conn.execute(sql).fetchall()
        return [int(r[0]) for r in rows]
