"""Store ops for the ``component`` kind (docs/proposals/component-kind.md).

Mirrors ``_material_ops.py``'s star schema, plus a category dimension:

- the **entity** is a slug-addressed ``refs`` row (``kind='component'``);
  ``title`` = canonical name, ``meta`` = ``{category, mpn, manufacturer,
  sku, uom, package, aliases, notes}``;
- the **category registry** (``component_categories``) is a growable, flat
  typed vocabulary, seeded ``core`` by migration 0093 and mintable
  ``proposed`` at entity-write time;
- the **spec registry** (``component_specs``) is a typed, growable
  vocabulary like ``material_properties``, plus a nullable
  ``category_id`` (NULL = universal, non-NULL = scoped to that category);
- the **values** (``component_spec_values``) are a plain fact table, one
  row per sourced measurement — no card, no embedding.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx`` /
``self.add_link``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

_CATEGORY_COLS = "category_id, name, status, description"

_SPEC_COLS = (
    "spec_id, name, canonical_unit, dimension, value_type, allowed_values, "
    "standard_ref, status, higher_is_better, description, category_id"
)

_VALUE_COLS = (
    "id, component_ref_id, spec_id, value_num, value_low, value_high, "
    "value_text, value_bool, input_unit, conditions, maturity, method, "
    "source_ref_id, source_chunk, source_url, as_of, set_by, created_at, notes"
)


def _row_to_category(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "category_id": row[0],
        "name": row[1],
        "status": row[2],
        "description": row[3],
    }


def _row_to_spec(row: tuple[Any, ...]) -> dict[str, Any]:
    return {
        "spec_id": row[0],
        "name": row[1],
        "canonical_unit": row[2],
        "dimension": row[3],
        "value_type": row[4],
        "allowed_values": row[5],
        "standard_ref": row[6],
        "status": row[7],
        "higher_is_better": row[8],
        "description": row[9],
        "category_id": row[10],
    }


def _row_to_value(row: tuple[Any, ...]) -> dict[str, Any]:
    """Map a ``_VALUE_COLS`` row to a dict. ``row`` may carry one trailing
    ``source_kind`` column (from the ``refs`` LEFT JOIN the read paths add);
    it lands under that key, ``None`` if the row is exactly 19-wide."""
    out = {
        "id": row[0],
        "component_ref_id": row[1],
        "spec_id": row[2],
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


class ComponentMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    add_link: Any

    # -- entity ----------------------------------------------------------

    def component_entity_upsert(
        self,
        *,
        slug: str,
        title: str,
        meta_patch: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Create-or-update the component entity ``refs`` row.

        On create, ``meta_patch`` is the whole ``meta``. On update, it is
        shallow-merged onto the existing ``meta`` (adding an alias/note
        doesn't clobber an already-recorded ``category``/``mpn``). Returns
        ``(ref, created)``.
        """
        existing = self.get_ref(kind="component", id=slug)
        with self.tx() as conn:
            if existing is None:
                ref = self.insert_ref(
                    kind="component",
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
            updated = self.get_ref(kind="component", id=slug)
            assert updated is not None
            return updated, False

    # -- category registry ------------------------------------------------

    def component_category_get(
        self, category_id: str, *, conn: Connection | None = None
    ) -> dict[str, Any] | None:
        """Look up one category row by ``category_id``, or ``None``."""
        sql = (
            f"SELECT {_CATEGORY_COLS} FROM component_categories WHERE category_id = %s"
        )
        if conn is not None:
            row = conn.execute(sql, (category_id,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (category_id,)).fetchone()
        return None if row is None else _row_to_category(row)

    def component_category_list(self) -> list[dict[str, Any]]:
        """The whole registry, core first then proposed, then alphabetical."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_CATEGORY_COLS} FROM component_categories "
                "ORDER BY (status = 'core') DESC, category_id"
            ).fetchall()
        return [_row_to_category(r) for r in rows]

    def component_category_mint(
        self,
        *,
        category_id: str,
        name: str,
        description: str | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        """Insert a new ``proposed``-tier category. Caller has already
        checked ``category_id`` doesn't exist. Never mints ``core`` — that
        tier is curated by migration only."""
        sql = (
            "INSERT INTO component_categories "
            "(category_id, name, status, description) "
            "VALUES (%s, %s, 'proposed', %s) "
            f"RETURNING {_CATEGORY_COLS}"
        )
        params = (category_id, name, description)
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_category(row)

    # -- spec registry -----------------------------------------------------

    def component_spec_get(
        self, spec_id: str, *, conn: Connection | None = None
    ) -> dict[str, Any] | None:
        """Look up one spec row by ``spec_id``, or ``None``."""
        sql = f"SELECT {_SPEC_COLS} FROM component_specs WHERE spec_id = %s"
        if conn is not None:
            row = conn.execute(sql, (spec_id,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (spec_id,)).fetchone()
        return None if row is None else _row_to_spec(row)

    def component_specs_list(
        self, *, category_id: str | None = None
    ) -> list[dict[str, Any]]:
        """The spec registry, core first then proposed, then alphabetical.

        ``category_id=None`` (default) lists every spec. Pass a category to
        get only what's "applicable to category" — universal specs
        (``category_id IS NULL``) plus that category's own.
        """
        sql = f"SELECT {_SPEC_COLS} FROM component_specs "
        params: tuple[Any, ...] = ()
        if category_id is not None:
            sql += "WHERE category_id IS NULL OR category_id = %s "
            params = (category_id,)
        sql += "ORDER BY (status = 'core') DESC, spec_id"
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [_row_to_spec(r) for r in rows]

    def component_spec_mint(
        self,
        *,
        spec_id: str,
        name: str,
        canonical_unit: str | None,
        dimension: str | None,
        value_type: str,
        category_id: str | None,
        allowed_values: list[Any] | None = None,
        description: str | None = None,
        conn: Connection | None = None,
    ) -> dict[str, Any]:
        """Insert a new ``proposed``-tier spec, scoped to ``category_id``
        (``None`` mints a universal spec — the handler only does this for a
        component-scoped mint in practice, per the proposal's runtime-mint
        resolution, but the store op itself stays general). Caller has
        already checked ``spec_id`` doesn't exist. Never mints ``core``."""
        sql = (
            "INSERT INTO component_specs "
            "(spec_id, name, canonical_unit, dimension, value_type, "
            " allowed_values, status, description, category_id) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'proposed', %s, %s) "
            f"RETURNING {_SPEC_COLS}"
        )
        params = (
            spec_id,
            name,
            canonical_unit,
            dimension,
            value_type,
            Jsonb(allowed_values) if allowed_values is not None else None,
            description,
            category_id,
        )
        if conn is not None:
            row = conn.execute(sql, params).fetchone()
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = c.execute(sql, params).fetchone()
        assert row is not None
        return _row_to_spec(row)

    # -- values --------------------------------------------------------

    def component_value_insert(
        self,
        *,
        component_ref_id: int,
        spec_id: str,
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
                "INSERT INTO component_spec_values "
                "(component_ref_id, spec_id, value_num, value_low, "
                " value_high, value_text, value_bool, conditions, maturity, "
                " method, source_ref_id, source_chunk, source_url, as_of, "
                " set_by, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    component_ref_id,
                    spec_id,
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

    def component_values_for_ref(self, component_ref_id: int) -> list[dict[str, Any]]:
        """Every value row for one component, grouped by spec (ordered
        ``spec_id``, most-recent first within a spec) — the component-page
        read. Each row carries ``source_kind`` (the source ref's kind, e.g.
        ``paper``/``datasheet``; ``None`` when there is no ``source_ref_id``
        or the source_url/no-source shape is used) so the renderer can
        format a handle without a second query."""
        cols = ", ".join("cv." + c.strip() for c in _VALUE_COLS.split(", "))
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {cols}, sr.kind AS source_kind "
                "FROM component_spec_values cv "
                "LEFT JOIN refs sr ON sr.ref_id = cv.source_ref_id "
                "WHERE cv.component_ref_id = %s "
                "ORDER BY cv.spec_id, cv.created_at DESC",
                (component_ref_id,),
            ).fetchall()
        return [_row_to_value(r) for r in rows]

    # -- made-of link ------------------------------------------------------

    def component_link_made_of(
        self, *, component_ref_id: int, material_ref_id: int
    ) -> None:
        """Create the ``made-of`` edge (component -> material). Idempotent —
        ``add_link`` dedupes on the unique ``(src, dst, relation)`` tuple."""
        self.add_link(
            src_ref_id=component_ref_id,
            dst_ref_id=material_ref_id,
            relation="made-of",
        )

    # -- search ----------------------------------------------------------

    def component_search_entities(
        self, q: str, *, limit: int = 20
    ) -> list[tuple[int, str | None, str, dict[str, Any]]]:
        """Lexical match over the component entity's name / aliases / mpn /
        manufacturer / category.

        Returns ``(ref_id, slug, title, meta)`` tuples, most-recently-updated
        first. ``q`` is matched as a case-insensitive substring, same
        leniency as ``material_search_entities``.
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
                "WHERE r.kind = 'component' AND r.deleted_at IS NULL AND ("
                "  r.title ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'category', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'mpn', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'manufacturer', '') ILIKE %(pat)s "
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

    def component_search_values(
        self,
        *,
        spec_id: str,
        min_val: float | None = None,
        max_val: float | None = None,
        maturity: str | None = None,
        category_id: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Range filter over ``component_spec_values`` for one spec, joined
        back to the owning component's title/slug/category.

        Bounds are inclusive and in the spec's canonical unit (v1 does no
        conversion — the caller/handler is responsible for that contract).
        The filter is an **interval overlap**, not a point-in-range test:
        ``min_val``/``max_val`` are compared against
        ``value_high``/``value_low`` (falling back to ``value_num`` when a
        row has no band), so a banded value matches whenever its band
        overlaps the query range — a point value (band NULL) keeps
        matching exactly as before. ``category_id=`` optionally narrows to
        components in that category. Returns rows ordered by ``value_num``
        ascending, each carrying the owning component's ``ref_id``/``title``.
        """
        clauses = ["cv.spec_id = %s"]
        params: list[Any] = [spec_id]
        if min_val is not None:
            clauses.append("COALESCE(cv.value_high, cv.value_num) >= %s")
            params.append(min_val)
        if max_val is not None:
            clauses.append("COALESCE(cv.value_low, cv.value_num) <= %s")
            params.append(max_val)
        if maturity is not None:
            clauses.append("cv.maturity = %s")
            params.append(maturity)
        if category_id is not None:
            clauses.append("r.meta->>'category' = %s")
            params.append(category_id)
        params.append(limit)
        cols = ", ".join("cv." + c.strip() for c in _VALUE_COLS.split(", "))
        sql = (
            f"SELECT {cols}, sr.kind AS source_kind, "
            "r.title AS component_title, r.ref_id AS component_ref_id_out "
            "FROM component_spec_values cv "
            "JOIN refs r ON r.ref_id = cv.component_ref_id AND r.deleted_at IS NULL "
            "LEFT JOIN refs sr ON sr.ref_id = cv.source_ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY cv.value_num ASC NULLS LAST "
            "LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        n_value_cols = len(_VALUE_COLS.split(", "))
        out = []
        for r in rows:
            base = _row_to_value(r[: n_value_cols + 1])  # + source_kind
            base["component_title"] = r[-2]
            out.append(base)
        return out


__all__ = ["ComponentMixin"]
