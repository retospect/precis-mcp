"""Store ops for the ``rxn`` kind — the sourced reaction-fact store.

Deliberately the ``material`` shape (:mod:`precis.store._material_ops`) with a
transformation as the entity instead of a substance, because the load-bearing
property is identical: **many rows per (entity, property) is the feature**. The
spread of reported yields across sources IS the finding; nothing here collapses
it.

- the **entity** is a slug-addressed ``refs`` row (``kind='rxn'``); ``meta``
  carries ``rxn_smiles``, ``uid_strict``, ``uid_transform`` and
  ``reaction_class``;
- the **property registry** (``rxn_properties``) is typed and growable, seeded
  ``core`` by migration 0157 and mintable ``proposed`` at write time;
- the **values** (``rxn_values``) are a plain fact table — no card, no
  embedding; the reaction page is a SQL join, not a search.

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
    "id, rxn_ref_id, property_id, value_num, value_low, value_high, "
    "value_text, value_bool, input_unit, conditions, maturity, method, "
    "source_licence, source_ref_id, source_chunk, source_url, as_of, set_by, "
    "created_at, notes"
)

#: Number of columns in ``_VALUE_COLS`` — read paths append ``source_kind``.
_N_VALUE_COLS = len(_VALUE_COLS.split(", "))


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
    it lands under that key, ``None`` when the row is exactly as wide as
    ``_VALUE_COLS``."""
    out = {
        "id": row[0],
        "rxn_ref_id": row[1],
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
        "source_licence": row[12],
        "source_ref_id": row[13],
        "source_chunk": row[14],
        "source_url": row[15],
        "as_of": row[16],
        "set_by": row[17],
        "created_at": row[18],
        "notes": row[19],
    }
    out["source_kind"] = row[_N_VALUE_COLS] if len(row) > _N_VALUE_COLS else None
    return out


class RxnMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any

    # -- entity ----------------------------------------------------------

    def rxn_entity_upsert(
        self,
        *,
        slug: str,
        title: str,
        meta_patch: dict[str, Any],
    ) -> tuple[Any, bool]:
        """Create-or-update the reaction entity ``refs`` row.

        On create ``meta_patch`` is the whole ``meta``; on update it is
        shallow-merged, so recording a ``reaction_class`` later does not
        clobber the already-stored identity keys. Returns ``(ref, created)``.
        """
        existing = self.get_ref(kind="rxn", id=slug)
        with self.tx() as conn:
            if existing is None:
                ref = self.insert_ref(
                    kind="rxn",
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
        updated = self.get_ref(kind="rxn", id=slug)
        assert updated is not None
        return updated, False

    def rxn_find_by_uid(
        self, uid: str, *, axis: str = "transform"
    ) -> list[tuple[int, str | None, str, dict[str, Any]]]:
        """Live ``rxn`` refs whose identity key matches ``uid``.

        ``axis='transform'`` (default) queries ``meta.uid_transform`` — the key
        that ignores byproducts, and therefore the one that actually converges
        precedent across sources that record them inconsistently.
        ``axis='strict'`` queries ``meta.uid_strict`` (whole balanced equation),
        which is the import-collapse key.

        Returns ``(ref_id, slug, title, meta)``. A list rather than a scalar
        because nothing in the schema enforces uniqueness on either key — the
        caller decides whether a second hit is a duplicate to merge or a
        genuinely distinct record.
        """
        if axis not in ("transform", "strict"):
            raise ValueError(f"axis must be 'transform' or 'strict', got {axis!r}")
        col = "uid_transform" if axis == "transform" else "uid_strict"
        with self.pool.connection() as conn:
            rows = conn.execute(
                "SELECT r.ref_id, "
                "  (SELECT id_value FROM ref_identifiers ri "
                "   WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
                "   LIMIT 1) AS slug, "
                "  r.title, r.meta "
                "FROM refs r "
                "WHERE r.kind = 'rxn' AND r.retired_at IS NULL "
                f"  AND r.meta ->> '{col}' = %s "
                "ORDER BY r.ref_id",
                (uid,),
            ).fetchall()
        return [(r[0], r[1], r[2], r[3] or {}) for r in rows]

    # -- property registry -------------------------------------------------

    def rxn_property_get(
        self, prop_id: str, *, conn: Connection | None = None
    ) -> dict[str, Any] | None:
        """Look up one property row by ``prop_id``, or ``None``."""
        sql = f"SELECT {_PROPERTY_COLS} FROM rxn_properties WHERE prop_id = %s"
        if conn is not None:
            row = conn.execute(sql, (prop_id,)).fetchone()
        else:
            with self.pool.connection() as c:
                row = c.execute(sql, (prop_id,)).fetchone()
        return None if row is None else _row_to_property(row)

    def rxn_properties_list(self) -> list[dict[str, Any]]:
        """The whole registry, core first then proposed, then alphabetical."""
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {_PROPERTY_COLS} FROM rxn_properties "
                "ORDER BY (status = 'core') DESC, prop_id"
            ).fetchall()
        return [_row_to_property(r) for r in rows]

    def rxn_property_mint(
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
        """Insert a new ``proposed``-tier property. Caller has already checked
        ``prop_id`` does not exist. Never mints ``core`` — that tier is curated
        by migration only."""
        sql = (
            "INSERT INTO rxn_properties "
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

    def rxn_value_insert(
        self,
        *,
        rxn_ref_id: int,
        property_id: str,
        value_num: float | None = None,
        value_low: float | None = None,
        value_high: float | None = None,
        value_text: str | None = None,
        value_bool: bool | None = None,
        conditions: dict[str, Any] | None = None,
        maturity: str = "lab",
        method: str | None = None,
        source_licence: str | None = None,
        source_ref_id: int | None = None,
        source_chunk: str | None = None,
        source_url: str | None = None,
        as_of: str | None = None,
        set_by: str | None = None,
        notes: str | None = None,
    ) -> int:
        """Insert one sourced measurement row. Returns the new ``id``.

        Never updates: a second report of the same property is a second row.
        """
        with self.tx() as conn:
            row = conn.execute(
                "INSERT INTO rxn_values "
                "(rxn_ref_id, property_id, value_num, value_low, value_high, "
                " value_text, value_bool, conditions, maturity, method, "
                " source_licence, source_ref_id, source_chunk, source_url, "
                " as_of, set_by, notes) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    rxn_ref_id,
                    property_id,
                    value_num,
                    value_low,
                    value_high,
                    value_text,
                    value_bool,
                    Jsonb(conditions or {}),
                    maturity,
                    method,
                    source_licence,
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

    def rxn_values_for_ref(self, rxn_ref_id: int) -> list[dict[str, Any]]:
        """Every value row for one reaction, ordered by property then
        most-recent-first — the reaction-page read. Each row carries
        ``source_kind`` (the source ref's kind) so the renderer can format a
        handle without a second query."""
        cols = ", ".join("rv." + c.strip() for c in _VALUE_COLS.split(", "))
        with self.pool.connection() as conn:
            rows = conn.execute(
                f"SELECT {cols}, sr.kind AS source_kind "
                "FROM rxn_values rv "
                "LEFT JOIN refs sr ON sr.ref_id = rv.source_ref_id "
                "WHERE rv.rxn_ref_id = %s "
                "ORDER BY rv.property_id, rv.created_at DESC",
                (rxn_ref_id,),
            ).fetchall()
        return [_row_to_value(r) for r in rows]

    # -- search ----------------------------------------------------------

    def rxn_search_entities(
        self, q: str, *, limit: int = 20
    ) -> list[tuple[int, str | None, str, dict[str, Any]]]:
        """Lexical match over title, reaction SMILES and reaction class.

        Case-insensitive substring, mirroring ``material_search_entities`` —
        the entity count is small enough that a full hybrid search is overkill,
        and a caller searching a SMILES fragment wants a substring hit.
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
                "WHERE r.kind = 'rxn' AND r.retired_at IS NULL AND ("
                "  r.title ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'rxn_smiles', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'reaction_class', '') ILIKE %(pat)s "
                "  OR COALESCE(r.meta->>'class_name', '') ILIKE %(pat)s "
                ") "
                "ORDER BY r.updated_at DESC LIMIT %(limit)s",
                {"pat": pat, "limit": limit},
            ).fetchall()
        return [(r[0], r[1], r[2], r[3] or {}) for r in rows]

    def rxn_search_values(
        self,
        *,
        property_id: str,
        min_val: float | None = None,
        max_val: float | None = None,
        maturity: str | None = None,
        reaction_class: str | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        """Range filter over ``rxn_values`` for one property, joined back to
        the owning reaction.

        Bounds are inclusive and in the property's canonical unit (v1 does no
        conversion). The filter is an **interval overlap**, not a
        point-in-range test: a banded value matches whenever its band overlaps
        the query range, and a point value (band NULL) matches exactly.

        ``reaction_class`` narrows to one RXNO id — this is the precedent
        query ("what yields do amide couplings actually give"), and it is the
        reason the class axis exists.
        """
        clauses = ["rv.property_id = %s"]
        params: list[Any] = [property_id]
        if min_val is not None:
            clauses.append("COALESCE(rv.value_high, rv.value_num) >= %s")
            params.append(min_val)
        if max_val is not None:
            clauses.append("COALESCE(rv.value_low, rv.value_num) <= %s")
            params.append(max_val)
        if maturity is not None:
            clauses.append("rv.maturity = %s")
            params.append(maturity)
        if reaction_class is not None:
            clauses.append("r.meta ->> 'reaction_class' = %s")
            params.append(reaction_class)
        params.append(limit)
        cols = ", ".join("rv." + c.strip() for c in _VALUE_COLS.split(", "))
        sql = (
            f"SELECT {cols}, sr.kind AS source_kind, r.title AS rxn_title "
            "FROM rxn_values rv "
            "JOIN refs r ON r.ref_id = rv.rxn_ref_id AND r.retired_at IS NULL "
            "LEFT JOIN refs sr ON sr.ref_id = rv.source_ref_id "
            f"WHERE {' AND '.join(clauses)} "
            "ORDER BY rv.value_num ASC NULLS LAST "
            "LIMIT %s"
        )
        with self.pool.connection() as conn:
            rows = conn.execute(sql, params).fetchall()
        out = []
        for r in rows:
            base = _row_to_value(r[: _N_VALUE_COLS + 1])  # + source_kind
            base["rxn_title"] = r[-1]
            out.append(base)
        return out


__all__ = ["RxnMixin"]
