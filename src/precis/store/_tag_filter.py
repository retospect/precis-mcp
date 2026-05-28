"""Tag filter SQL helper — DRY across every store query that selects refs.

v2 schema notes:

- ``tags(tag_id, namespace, value)`` is the canonical vocabulary table
- ``ref_tags(ref_id, tag_id, set_by, created_at)`` attaches tags to refs
- ``chunk_tags(chunk_id, tag_id, set_by, created_at)`` attaches tags
  to chunks
- The mapping between agent-facing tag strings and ``(namespace,
  value)`` mirrors :mod:`precis.store._tags_ops`:
    ``"STATUS:open"``      → (``"STATUS"``, ``"open"``)  closed-prefix
    ``"pinned"``           → (``"FLAG"``,   ``"pinned"``)  v1-flag-shape
    ``"topic-x"``          → (``"OPEN"``,   ``"topic-x"``) bare word

We can filter "ref carries all of these tags" with one IN-subquery
that joins ``ref_tags``+``tags``, uses tuple-IN on ``(namespace,
value)``, then ``GROUP BY ref_id HAVING COUNT(*) = N`` to enforce
AND semantics.

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
  the chunks of those refs only. Two orders of magnitude fewer rows
  for the lexical/semantic ranking pass.
"""

from __future__ import annotations

from typing import Any


def _parse_tag_string(s: str) -> tuple[str, str]:
    """Parse an agent-facing canonical tag string into ``(namespace, value)``.

    Mirrors :mod:`precis.store._tags_ops`'s canonical mapping:

    - ``"PREFIX:value"`` with uppercase prefix → (``"PREFIX"``, ``"value"``)
    - otherwise → (``"OPEN"``, raw string)

    Flag-shaped bare strings (``"pinned"``) end up under the ``OPEN``
    namespace by default. Callers that need to filter on a flag
    specifically should pass the canonical ``"FLAG:pinned"`` form
    (the upstream tag normalisation rewrites bare flags to that
    shape when known_flags is provided).
    """
    if ":" in s:
        prefix, _, value = s.partition(":")
        if prefix and prefix.isupper():
            return (prefix, value)
    return ("OPEN", s)


def build_tag_filter(
    tags: list[str] | None,
    *,
    ref_alias: str = "r",
    block_level: bool = False,
) -> tuple[str, list[Any]]:
    """Build the SQL ``AND`` fragment + params for a tags filter.

    Args:
        tags:        List of tag strings (``'STATUS:open'``,
                     ``'topic:co2-capture'``, ``'pinned'``). Closed-
                     prefix tags must be in their canonical
                     ``PREFIX:value`` form — the runtime is
                     responsible for validating via
                     :meth:`precis.store.Tag.parse_strict` before
                     calling this helper.
        ref_alias:   The SQL alias used for ``refs`` in the outer
                     query (typically ``r``). The fragment references
                     ``{ref_alias}.ref_id``.
        block_level: If True, match chunk-level tags (via
                     ``chunk_tags``); default False matches
                     ref-level tags (via ``ref_tags``).

    Returns:
        ``(fragment, params)``. ``fragment`` is the empty string when
        ``tags`` is None or empty, otherwise begins with a leading
        ``" AND "`` so callers can splice it without conditional logic.
        ``params`` is a list of bind parameters in the order they
        appear in the fragment (namespace, value, namespace, value,
        ..., N).

    Semantics:
        AND across all tags — a ref must carry **every** tag in
        ``tags`` to pass. The ``HAVING COUNT(*) = N`` clause
        enforces this without requiring N self-joins.
    """
    if not tags:
        return "", []

    parsed = [_parse_tag_string(s) for s in tags]
    # IN((%s, %s), (%s, %s), ...)
    tuple_placeholders = ", ".join(["(%s, %s)"] * len(parsed))

    if block_level:
        # Chunk-level: filter refs whose chunks collectively carry
        # all N tags. AND semantics across distinct tags, but the
        # tags don't have to be on the same chunk.
        fragment = (
            f" AND {ref_alias}.ref_id IN ("
            f"  SELECT c.ref_id "
            f"  FROM chunks c "
            f"  JOIN chunk_tags ct ON ct.chunk_id = c.chunk_id "
            f"  JOIN tags t ON t.tag_id = ct.tag_id "
            f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders}) "
            f"  GROUP BY c.ref_id "
            f"  HAVING COUNT(DISTINCT t.tag_id) = %s"
            f")"
        )
    else:
        fragment = (
            f" AND {ref_alias}.ref_id IN ("
            f"  SELECT rt.ref_id "
            f"  FROM ref_tags rt "
            f"  JOIN tags t ON t.tag_id = rt.tag_id "
            f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders}) "
            f"  GROUP BY rt.ref_id "
            f"  HAVING COUNT(DISTINCT t.tag_id) = %s"
            f")"
        )

    params: list[Any] = []
    for ns, val in parsed:
        params.append(ns)
        params.append(val)
    params.append(len(parsed))
    return fragment, params


__all__ = ["build_tag_filter"]
