"""Shared row helpers for the value-entity store mixins (``material`` and
``component``): the slug-entity upsert and the sourced-value INSERT.

Both kinds are the same star schema (entity ``refs`` row + typed registry +
append-only fact table); only the kind string, table and column names differ.
Table/column names passed in are module constants of the calling mixin, never
caller input.
"""

from __future__ import annotations

from typing import Any

from psycopg.types.json import Jsonb


def entity_upsert(
    store: Any,
    *,
    kind: str,
    slug: str,
    title: str,
    meta_patch: dict[str, Any],
) -> tuple[Any, bool]:
    """Create-or-update the ``kind`` entity ``refs`` row.

    On create, ``meta_patch`` is the whole ``meta``. On update, it is
    shallow-merged onto the existing ``meta``. Returns ``(ref, created)``.
    """
    existing = store.get_ref(kind=kind, id=slug)
    with store.tx() as conn:
        if existing is None:
            ref = store.insert_ref(
                kind=kind,
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
    # Read AFTER the transaction commits: ``get_ref`` takes no ``conn`` and
    # always opens its own pooled connection, so reading inside the block
    # above returns the PRE-update row under READ COMMITTED. gr329810.
    updated = store.get_ref(kind=kind, id=slug)
    assert updated is not None
    return updated, False


def value_insert(
    store: Any,
    *,
    table: str,
    ref_col: str,
    key_col: str,
    ref_id: int,
    key: str,
    value_num: float | None,
    value_low: float | None,
    value_high: float | None,
    value_text: str | None,
    value_bool: bool | None,
    conditions: dict[str, Any] | None,
    maturity: str,
    method: str | None,
    source_ref_id: int | None,
    source_chunk: str | None,
    source_url: str | None,
    as_of: str | None,
    set_by: str | None,
    notes: str | None,
) -> int:
    """Insert one sourced measurement row into ``table``. Returns the new
    ``id``."""
    with store.tx() as conn:
        row = conn.execute(
            f"INSERT INTO {table} "
            f"({ref_col}, {key_col}, value_num, value_low, "
            " value_high, value_text, value_bool, conditions, maturity, "
            " method, source_ref_id, source_chunk, source_url, as_of, "
            " set_by, notes) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "RETURNING id",
            (
                ref_id,
                key,
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
