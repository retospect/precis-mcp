"""Shared lookups for the ontology-v2 driver scripts (onto-*).

Factored out of ``scripts/_onto_repoint.py`` when onto-repoint-fix dropped
its reparent half — onto-groups still needs them and later slice drivers
will too.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from psycopg import Connection


def _taxon_by_slug(conn: Connection, slug: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT ref_id, kind, title, retired_at IS NOT NULL, meta "
        "FROM refs WHERE kind = 'taxon' AND retired_at IS NULL "
        "  AND meta ->> 'slug' = %s ORDER BY ref_id LIMIT 1",
        (slug,),
    ).fetchone()
    if row is None:
        return None
    return {
        "id": int(row[0]),
        "kind": row[1],
        "title": row[2],
        "retired": bool(row[3]),
        "meta": row[4] or {},
    }


def _measurand_root(conn: Connection) -> int:
    row = conn.execute(
        "SELECT ref_id FROM refs WHERE kind = 'taxon' "
        "AND retired_at IS NULL AND meta ->> 'slug' = 'measurand' "
        "ORDER BY ref_id LIMIT 1"
    ).fetchone()
    assert row is not None, "no live 'measurand' root taxon"
    return int(row[0])


def _has_specialises_edge(conn: Connection, child: int, parent: int) -> bool:
    row = conn.execute(
        "SELECT 1 FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
        "AND relation = 'specialises'",
        (child, parent),
    ).fetchone()
    return row is not None
