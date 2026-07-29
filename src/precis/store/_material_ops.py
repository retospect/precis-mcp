"""Store ops for the ``material`` kind (materials-handbook-kind proposal).

Storage splits by what is a search target (the `cad`/`pcb`/`structure`
pattern):

- the **entity** is a slug-addressed ``refs`` row (``kind='material'``);
  ``title`` = canonical name, ``meta`` = ``{aliases, material_class,
  composition/formula, notes}``;
- the **property registry** (``material_properties``) is a typed, growable
  vocabulary, seeded ``core`` by migration 0092 and mintable ``proposed`` at
  write time;
- the **values** (``material_values``) are a plain fact table, one row per
  sourced measurement — no card, no embedding; the handbook page is a SQL
  join, not a search.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

_PROPERTY_COLS = (
    "prop_id, name, canonical_unit, dimension, value_type, allowed_values, "
    "standard_ref, status, higher_is_better, description"
)

_VALUE_COLS = (
    "id, material_ref_id, property_id, value_num, value_low, value_high, "
    "value_text, value_bool, input_unit, conditions, maturity, method, "
    "source_ref_id, source_chunk, source_url, as_of, set_by, created_at, notes"
)


def _row_to_property(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "prop_id": row[0],
        "name": row[1],
        "canonical_unit": row[2],
        "dimension": row[3],
        "value_type": row[4],
        "allowed_values": row[5],
        "standard_ref": row[6],
        "status": row[7],
        "higher_is_better": row[8],
        "description": row[9],
    }


def _row_to_value(row: tuple[Any, ...]) -> dict[str, Any]:
    """Map a ``_VALUE_COLS`` row to a dict. ``row`` may carry one trailing
    ``source_kind`` column (from the ``refs`` LEFT JOIN the read paths add);
    it lands under that key, ``None`` if the row is exactly 19-wide."""
    out = {
        "id": row[0],
        "material_ref_id": row[1],
        "property_id": row[2],
        "value_num": row[3],
        "value_low": row[4],
        "value_high": row[5],
        "value_text": row[6],
        "value_bool": row[7],
        "input_unit": row[8],
        "conditions": row[9],
        "maturity": row[10],
        "method": row[11],
        "source_ref_id": row[12],
        "source_chunk": row[13],
        "source_url": row[14],
        "as_of": row[15],
        "set_by": row[16],
        "created_at": row[17],
        "notes": row[18],
    }
    out["source_kind"] = row[19] if len(row) > 19 else None
    return out


class MaterialMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any

    # -- entity ----------------------------------------------------------

    def material_entity_upsert(
        self,
        *,
        slug: str,
        title: str,
        meta_patch: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Create-or-update the material entity ``refs`` row.

        On create, ``meta_patch`` is the whole ``meta``. On update, it is
        shallow-merged onto the existing ``meta`` (an alias/notes append
        doesn't clobber an already-recorded ``material_class``). Returns
        ``(ref, created)``.
        """
        existing = self.get_ref(kind="material", id=slug)
        with self.tx() as conn:
            if existing is None:
                ref = self.insert_ref(
                    kind="material",
                    slug=slug,
                    title=title,
                    meta=dict(meta_patch),
                    conn=conn,
                )
                return ref, True
            merged = {**(existing.meta or {}), **meta_patch}
            conn.execute(
                "UPDATE refs SET title = %s, meta = %s WHERE ref_id = %s",
                (title, Jsonb(merged), existing.id),
            )
            updated = self.get_ref(kind="material", id=slug)
            assert updated is not None
            return updated, False

    # -- property registry -------------------------------------------------

    def material_property_get(
        self, prop_id: str, *, conn: Connection | None = None
    ) -> dict[str, Any] | None:
        """Look up one property row by ``prop_id``, or ``None``."""
        sql = f"SELECT {_PROPERTY_COLS} FROM material_properties WHERE prop_id = %s"
        if conn is not None:
            row = conn.execute(sql, (prop_id,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (prop_id,)).fetchone()
        return None if row is None else _row_to_property(row)

    def material_properties_list(self) -> list[dict[str, Any]]:
        """The whole registry, core first then proposed, then alphabetical."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PROPERTY_COLS} FROM material_properties "
                "ORDER BY (status = 'core') DESC, prop_id"
            ).fetchall()
        return [_row_to_property(r) for r in rows]

    def material_property_mint(
        self,
        *,
        prop_id: str,
        name: str,
        canonical_unit: str | None,
        dimension: str | None,
        value_type: str,
        allowed_values: list[Any] | None = None,
        description: str | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        """Insert a new ``proposed``-tier property. Caller has already
        checked ``prop_id`` doesn't exist. Never mints ``core`` — that tier
        is curated by migration only."""
        sql = (
            "INSERT INTO material_properties "
            "(prop_id, name, canonical_unit, dimension, value_type, "
            " allowed_values, status, description) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s) "
            f"RETURNING {_PROPERTY_COLS}"
        )
        params = (
            prop_id,
            name,
            canonical_unit,
            dimension,
            value_type,
            Jsonb(allowed_values) if allowed_values is not None else None,
            description,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_property(row)

    # -- values --------------------------------------------------------

    def material_value_insert(
        self,
        *,
        material_ref_id: int,
        property_id: str,
        value_num: float | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
        value_text: str | None = None,
        value_bool: bool | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str = "lab",
        method: str | None = None,
        source_ref_id: int | None = None,
        source_chunk: str | None = None,
        source_url: str | None = None,
        as_of: str | None = None,
        set_by: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Insert one sourced measurement row. Returns the new ``id``."""
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO material_values "
                "(material_ref_id, property_id, value_num, value_low, "
                " value_high, value_text, value_bool, conditions, maturity, "
                " method, source_ref_id, source_chunk, source_url, as_of, "
                " set_by, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    material_ref_id,
                    property_id,
                    value_num,
                    value_low,
                    value_high,
                    value_text,
                    value_bool,
                    Jsonb(conditions or {}),
                    maturity,
                    method,
                    source_ref_id,
                    source_chunk,
                    source_url,
                    as_of,
                    set_by,
                    notes,
                ),
            ).fetchone()
        assert row is not None
        return int(row[0])

    def material_values_for_ref(self, material_ref_id: int) -> list[dict[str, Any]]:
        """Every value row for one material, grouped by property (ordered
        ``property_id``, most-recent first within a property) — the
        handbook-page read. Each row carries ``source_kind`` (the source
        ref's kind, e.g. ``paper``/``datasheet``; ``None`` when there is no
        ``source_ref_id`` or the source_url/no-source shape is used) so the
        renderer can format a handle without a second query."""
        cols = ", ".join("mv." + c.strip() for c in _VALUE_COLS.split(", "))
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {cols}, sr.kind AS source_kind "
                "FROM material_values mv "
                "LEFT JOIN refs sr ON sr.ref_id = mv.source_ref_id "
                "WHERE mv.material_ref_id = %s "
                "ORDER BY mv.property_id, mv.created_at DESC",
                (material_ref_id,),
            ).fetchall()
        return [_row_to_value(r) for r in rows]

    # -- search ----------------------------------------------------------

    def material_search_entities(
        self, q: str, *, limit: int = 20
    ) -> list[tuple[int, str | None, str, dict[str, Any]]]:
        """Lexical match over the material entity's name / aliases / class.

        Returns ``(ref_id, slug, title, meta)`` tuples, most-recently-updated
        first. ``q`` is matched as a case-insensitive substring — the
        entity count is small enough that a full hybrid search is
        overkill, and callers want "6061" to hit "6061-T6" as a name
        substring, an alias substring, or the material_class.
        """
        pat = f"%{q}%"
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, "
                "  (SELECT id_value FROM ref_identifiers ri "
                "   WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                "   LIMIT 1) AS slug, "
                "  r.title, r.meta "
                "FROM refs r "
                "WHERE r.kind = 'material' AND r.deleted_at IS NULL AND ("
                "  r.title ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'material_class', '') ILIKE %(pat)s "
                "  OR EXISTS ("
                "    SELECT 1 FROM jsonb_array_elements_text("
                "      COALESCE(r.meta->'aliases', '[]'::jsonb)) a(v) "
                "    WHERE a.v ILIKE %(pat)s"
                "  )"
                ") "
                "ORDER BY r.updated_at DESC LIMIT %(limit)s",
                {"pat": pat, "limit": limit},
            ).fetchall()
        return [(r[0], r[1], r[2], r[3] or {}) for r in rows]

    def material_search_values(
        self,
        *,
        property_id: str,
        min_val: float | None = None,
        max_val: float | None = None,
        maturity: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Range filter over ``material_values`` for one property, joined
        back to the owning material's title/slug.

        Bounds are inclusive and in the property's canonical unit (v1
        does no conversion — the caller/handler is responsible for that
        contract). The filter is an **interval overlap**, not a
        point-in-range test: ``min_val``/``max_val`` are compared against
        ``value_high``/``value_low`` (falling back to ``value_num`` when a
        row has no band), so a banded value matches whenever its band
        overlaps the query range — a point value (band NULL) keeps
        matching exactly as before. Returns rows ordered by ``value_num``
        ascending, each carrying the owning material's ``ref_id``/``title``.
        """
        clauses = ["mv.property_id = %s"]
        params: list[Any] = [property_id]
        if min_val is not None:
            clauses.append("COALESCE(mv.value_high, mv.value_num) >= %s")
            params.append(min_val)
        if max_val is not None:
            clauses.append("COALESCE(mv.value_low, mv.value_num) <= %s")
            params.append(max_val)
        if maturity is not None:
            clauses.append("mv.maturity = %s")
            params.append(maturity)
        params.append(limit)
        cols = ", ".join("mv." + c.strip() for c in _VALUE_COLS.split(", "))
        sql = (
            f"SELECT {cols}, sr.kind AS source_kind, "
            "r.title AS material_title, r.ref_id AS material_ref_id_out "
            "FROM material_values mv "
            "JOIN refs r ON r.ref_id = mv.material_ref_id AND r.deleted_at IS NULL "
            "LEFT JOIN refs sr ON sr.ref_id = mv.source_ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY mv.value_num ASC NULLS LAST "
            "LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        n_value_cols = len(_VALUE_COLS.split(", "))
        out = []
        for r in rows:
            base = _row_to_value(r[: n_value_cols + 1])  # + source_kind
            base["material_title"] = r[-2]
            out.append(base)
        return out


__all__ = ["MaterialMixin"]
