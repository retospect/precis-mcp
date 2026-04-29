"""Tag filter SQL helper — DRY across every store query that selects refs.

The schema gives us a unified ``ref_tags`` view over the three
narrow tag tables:

    ref_closed_tags  (ref_id, prefix, value, ...)   indexed (prefix, value)
    ref_flags        (ref_id, name, ...)            indexed (name)
    ref_open_tags    (ref_id, value, ...)           indexed (value)

The view projects each row to a single ``tag TEXT`` column with
``prefix || ':' || value`` for closed tags and the bare name for the
others. We can therefore filter "ref carries all of these tags" with
a single subquery that uses ``IN`` + ``GROUP BY`` + ``HAVING COUNT``,
and the planner pushes the predicate through the UNION ALL into the
narrow indexes.

This module exposes one helper that returns a SQL fragment + params,
which any caller in :mod:`precis.store.store` can splice into its
WHERE clause unconditionally — passing ``None`` or ``[]`` returns
``("", [])`` so the splice is a no-op.

Why this is a perf win, not a regression:

* Without it, an agent doing
  ``search(kind='todo', tags=['STATUS:open'], q='precis')`` had to
  pull every matching todo block, then filter in Python.
* With it, the planner narrows to the ~N ref rows that carry
  ``STATUS:open`` first, then runs the expensive ``ts_rank_cd`` on
  the blocks of those refs only. Two orders of magnitude fewer rows
  for the lexical/semantic ranking pass.
"""

from __future__ import annotations

from typing import Any


def build_tag_filter(
    tags: list[str] | None,
    *,
    ref_alias: str = "r",
    block_level: bool = False,
) -> tuple[str, list[Any]]:
    """Build the SQL ``AND`` fragment + params for a tags filter.

    Args:
        tags:        List of tag strings (``'STATUS:open'``,
                     ``'topic:co2-capture'``, ``'star'``). Closed-prefix
                     tags must be in their canonical ``PREFIX:value``
                     form — the runtime is responsible for validating
                     via :meth:`precis.store.Tag.parse_strict` before
                     calling this helper.
        ref_alias:   The SQL alias used for ``refs`` in the outer
                     query (typically ``r``). The fragment references
                     ``{ref_alias}.id``.
        block_level: If True, match block-level tags (``pos = N``);
                     default False matches ref-level tags only
                     (``pos = -1``, projected as ``NULL`` by the
                     view). Phase A only uses ref-level filtering.

    Returns:
        ``(fragment, params)``. ``fragment`` is the empty string when
        ``tags`` is None or empty, otherwise begins with a leading
        ``" AND "`` so callers can splice it without conditional logic.
        ``params`` is a list of bind parameters in the order they
        appear in the fragment.

    Semantics:
        AND across all tags — a ref must carry **every** tag in
        ``tags`` to pass. The ``HAVING COUNT(DISTINCT tag) = N``
        clause enforces this without requiring N self-joins.
    """
    if not tags:
        return "", []

    placeholders = ", ".join(["%s"] * len(tags))
    pos_clause = "pos IS NOT NULL" if block_level else "pos IS NULL"

    fragment = (
        f" AND {ref_alias}.id IN ("
        f"SELECT ref_id FROM ref_tags "
        f"WHERE tag IN ({placeholders}) AND {pos_clause} "
        f"GROUP BY ref_id "
        f"HAVING COUNT(DISTINCT tag) = %s"
        f")"
    )
    params: list[Any] = list(tags)
    params.append(len(tags))
    return fragment, params


__all__ = ["build_tag_filter"]
