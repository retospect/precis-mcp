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
- the **values** are plain ``measures`` rows (migration 0188 dropped
  ``rxn_values``): subject = the reaction, measurand = the ``rxn_properties``
  taxon (``meta.legacy_source = {table: 'rxn_properties', key: prop_id}``),
  written through :meth:`~precis.store._measures_ops.MeasuresMixin.insert_measure`
  and read back through ``measures_for`` — no card, no embedding; the reaction
  page is a SQL join, not a search.

**Units.** ``rxn_properties.canonical_unit`` is the unit the handler's verbs
speak (``%`` for a yield); the taxon stores SI (a fraction), with a
``measure_unit_compat`` row between them. This module is the edge: a value goes
in as ``reported_unit = <legacy canonical unit>`` (so the store converts it) and
comes back out through the compat factor, so ``handlers/rxn.py`` still sees and
prints the numbers it always did.

Mixin assumes the concrete Store provides ``self.pool`` / ``self.tx``.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.errors import NotFound
from precis.store._measures_ops import (
    _REL_TOL,
    MeasureSpec,
    legacy_value,
    register_legacy_unit,
)

_PROPERTY_COLS = (
    "prop_id, name, canonical_unit, dimension, value_type, allowed_values, "
    "standard_ref, status, higher_is_better, description"
)

#: The measures-side read of one reaction value: the row, its source ref's kind,
#: its taxon's legacy key and the ``measure_unit_compat`` row (None for a taxon
#: that was never re-based) — what :func:`_measure_to_value` needs.
_RXN_SELECT = (
    "SELECT m.*, t.meta -> 'legacy_source' ->> 'key' AS property_key, "
    "c.factor AS legacy_factor, c.si_offset AS legacy_offset, "
    "sr.kind AS source_kind, r.title AS rxn_title "
    "FROM measures m "
    "JOIN refs t ON t.ref_id = m.measurand_ref_id AND t.kind = 'taxon' "
    "JOIN refs r ON r.ref_id = m.subject_ref_id "
    "LEFT JOIN measure_unit_compat c ON c.legacy_table = 'rxn_properties' "
    " AND c.legacy_key = t.meta -> 'legacy_source' ->> 'key' "
    " AND c.si_unit = t.meta ->> 'canonical_unit' "
    "LEFT JOIN refs sr ON sr.ref_id = m.source_ref_id "
    "WHERE t.meta -> 'legacy_source' ->> 'table' = 'rxn_properties' "
    # a pilot measure on a shared taxon (yield) with a paper subject is not a reaction
    "AND r.kind = 'rxn' "
    "AND m.direction = 'output' AND m.superseded_by IS NULL "
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


def _measure_to_value(row: dict[str, Any]) -> dict[str, Any]:
    """Map a ``measures`` row (with ``property_key`` and the compat factor from
    :data:`_RXN_SELECT`) to the dict the reaction handler has always read: the
    numbers back in the property's legacy unit, ``source_licence`` out of
    ``meta``, ``set_by`` None for the legacy sentinel actor."""
    factor, offset = row.get("legacy_factor"), row.get("legacy_offset")
    actor = row.get("actor")
    if actor == "migration-0188":  # a re-based row: the legacy writer, as the views do
        actor = (row.get("meta") or {}).get("legacy_actor")
    return {
        "id": row["id"],
        "rxn_ref_id": row["subject_ref_id"],
        "property_id": row["property_key"],
        "value_num": legacy_value(row["value_num"], factor, offset),
        "value_low": legacy_value(row["value_low"], factor, offset),
        "value_high": legacy_value(row["value_high"], factor, offset),
        "value_text": row["value_text"],
        "value_bool": row["value_bool"],
        "input_unit": row["input_unit"],
        "conditions": row["conditions"],
        "maturity": row["maturity"],
        "method": row["method"],
        "source_licence": (row.get("meta") or {}).get("source_licence"),
        "source_ref_id": row["source_ref_id"],
        "source_chunk": row["source_chunk"],
        "source_url": row["source_url"],
        "as_of": row["as_of"],
        "set_by": None if actor == "legacy" else actor,
        "created_at": row["created_at"],
        "notes": row["notes"],
        "source_kind": row.get("source_kind"),
    }


def _num_text(x: float) -> str:
    text = repr(float(x))
    return text[:-2] if text.endswith(".0") else text


def _literal(
    num: float | None,
    low: float | None,
    high: float | None,
    text: str | None,
    flag: bool | None,
) -> str:
    """The printed form of a value (the SQL ``precis_measure_literal`` rule)."""
    if text is not None:
        return text
    if num is not None:
        return _num_text(num)
    if low is not None and high is not None:
        return f"{_num_text(low)}–{_num_text(high)}"
    if low is not None:
        return f"≥{_num_text(low)}"
    if high is not None:
        return f"≤{_num_text(high)}"
    if flag is not None:
        return str(flag).lower()
    return "(none)"


def _form(
    num: float | None,
    low: float | None,
    high: float | None,
    text: str | None,
    flag: bool | None,
) -> str:
    """The ``measures.value_form`` of a value (SQL ``precis_measure_form``)."""
    if text is not None:
        return "categorical"
    if flag is not None:
        return "boolean"
    if num is not None:
        return "point"
    if low is not None and high is not None:
        return "interval"
    if low is not None:
        return "lower_bound"
    if high is not None:
        return "upper_bound"
    return "not_established"


class RxnMixin:
    pool: Any
    tx: Any
    insert_ref: Any
    get_ref: Any
    insert_measure: Any
    measures_for: Any

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
        by migration only.

        A unit that converts to SI is accepted and re-based at mint (see
        :func:`~precis.store._measures_ops.register_legacy_unit`)."""
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
            register_legacy_unit(conn, "rxn_properties", prop_id, canonical_unit)
        else:
            with self.pool.connection() as c:
                with c.transaction():
                    row = c.execute(sql, params).fetchone()
                    register_legacy_unit(c, "rxn_properties", prop_id, canonical_unit)
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
        The value is in the property's registry unit (``rxn_properties.
        canonical_unit``); the store converts it to the taxon's SI unit.
        """
        with self.tx() as conn:
            tax = conn.execute(
                "SELECT precis_measure_taxon('rxn_properties', %s, TRUE)",
                (property_id,),
            ).fetchone()
            if tax is None or tax[0] is None:
                raise NotFound(f"rxn property {property_id!r} is not registered")
            prop = self.rxn_property_get(property_id, conn=conn)
            numeric = any(v is not None for v in (value_num, value_low, value_high))
            unit = prop["canonical_unit"] if prop is not None and numeric else None
            spec = MeasureSpec(
                measurand_ref_id=int(tax[0]),
                literal=_literal(
                    value_num, value_low, value_high, value_text, value_bool
                ),
                subject_ref_id=rxn_ref_id,
                reported_unit=unit,
                value_num=value_num,
                value_low=value_low,
                value_high=value_high,
                value_text=value_text,
                value_bool=value_bool,
                value_form=_form(
                    value_num, value_low, value_high, value_text, value_bool
                ),
                maturity=maturity,
                method=method,
                conditions=conditions or {},
                source_ref_id=source_ref_id,
                source_chunk=source_chunk,
                source_url=source_url,
                as_of=as_of,
                notes=notes,
                meta={"source_licence": source_licence} if source_licence else {},
            )
            run = self.insert_measure(
                spec, actor=(set_by or "").strip() or "legacy", conn=conn
            )
        return int(run.output_id)

    def rxn_values_for_ref(self, rxn_ref_id: int) -> list[dict[str, Any]]:
        """Every value row for one reaction, ordered by property then
        most-recent-first — the reaction-page read. Each row carries
        ``source_kind`` (the source ref's kind) so the renderer can format a
        handle without a second query. Numbers are in the property's registry
        unit."""
        rows = [
            r
            for r in self.measures_for(rxn_ref_id)
            if r["direction"] == "output"
            and r["subject_kind"] == "rxn"
            and (r["legacy_source"] or {}).get("table") == "rxn_properties"
        ]
        for r in rows:
            r["property_key"] = r["legacy_source"].get("key")
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        rows.sort(key=lambda r: r["property_key"] or "")
        return [_measure_to_value(r) for r in rows]

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

    def rxn_precedent_count(self, reaction_class: str) -> tuple[int, int]:
        """``(yield row count, distinct rxn ref count)`` precedent for one
        RXNO ``reaction_class`` — the count half of
        :meth:`rxn_search_values`'s own join (``property_id='yield'``, no
        ``limit``: the blocktree slice-5 precedent DRC
        (:mod:`precis_se.precedent`) needs the TRUE count, not a capped
        page, to say "0 precedent" honestly rather than "0 shown"."""
        with self.pool.connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT m.subject_ref_id) "
                "FROM measures m "
                "JOIN refs t ON t.ref_id = m.measurand_ref_id AND t.kind = 'taxon' "
                "JOIN refs r ON r.ref_id = m.subject_ref_id AND r.retired_at IS NULL "
                "  AND r.kind = 'rxn' "
                "WHERE t.meta -> 'legacy_source' ->> 'table' = 'rxn_properties' "
                "  AND t.meta -> 'legacy_source' ->> 'key' = 'yield' "
                "  AND m.direction = 'output' AND m.superseded_by IS NULL "
                "  AND r.meta ->> 'reaction_class' = %s",
                (reaction_class,),
            ).fetchone()
        assert row is not None
        return int(row[0]), int(row[1])

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
        clauses = ["t.meta -> 'legacy_source' ->> 'key' = %s", "r.retired_at IS NULL"]
        params: list[Any] = [property_id]
        # bounds are in the property's registry unit; the rows are SI
        # (with the same relative tolerance as the Build B range search)
        bound = "(%s * COALESCE(c.factor, 1) + COALESCE(c.si_offset, 0))"
        if min_val is not None:
            clauses.append(
                f"COALESCE(m.value_high, m.value_num) >= {bound} - abs{bound} * %s"
            )
            params.extend([min_val, min_val, _REL_TOL])
        if max_val is not None:
            clauses.append(
                f"COALESCE(m.value_low, m.value_num) <= {bound} + abs{bound} * %s"
            )
            params.extend([max_val, max_val, _REL_TOL])
        if maturity is not None:
            clauses.append("m.maturity = %s")
            params.append(maturity)
        if reaction_class is not None:
            clauses.append("r.meta ->> 'reaction_class' = %s")
            params.append(reaction_class)
        params.append(limit)
        sql = (
            f"{_RXN_SELECT} AND {' AND '.join(clauses)} "
            "ORDER BY m.value_num ASC NULLS LAST, m.id "
            "LIMIT %s"
        )
        with self.pool.connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql, params)
                rows = list(cur.fetchall())
        out = []
        for r in rows:
            base = _measure_to_value(r)
            base["rxn_title"] = r["rxn_title"]
            out.append(base)
        return out


__all__ = ["RxnMixin"]
