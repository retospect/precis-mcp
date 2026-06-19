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


def _parse_tag_string(s: str) -> list[tuple[str, str]]:
    """Parse an agent-facing canonical tag string into one or more
    ``(namespace, value)`` rows.

    Mirrors :mod:`precis.store._tags_ops`'s canonical mapping:

    - ``"PREFIX:value"`` with uppercase prefix → single ``(prefix, value)``
    - bare string ``"workspace"``            → both ``(OPEN, "workspace")``
                                               and ``(FLAG, "workspace")``

    The bare-string expansion makes cross-kind tag filtering namespace-
    agnostic: a caller writing ``tags=['workspace']`` doesn't have to
    know whether the ref carries the tag in the ``OPEN`` or the
    ``FLAG`` namespace. The SQL planner emits one combined IN-tuple
    and counts *distinct values* (not tag_ids), so the bare tag
    still counts once whether it matched the open or flag row.
    """
    if ":" in s:
        prefix, _, value = s.partition(":")
        if prefix and prefix.isupper():
            return [(prefix, value)]
    return [("OPEN", s), ("FLAG", s)]


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
        ``tags`` is None or empty, otherwise a standalone boolean
        predicate (``<alias>.ref_id IN (...)``) with **no** leading
        connector — callers append it to their ``clauses`` list and the
        outer query joins with ``" AND "``. ``params`` is a list of bind
        parameters in the order they appear in the fragment (namespace,
        value, namespace, value, ..., N).

    Semantics:
        AND across all tags — a ref must carry **every** tag in
        ``tags`` to pass. The ``HAVING COUNT(*) = N`` clause
        enforces this without requiring N self-joins.
    """
    if not tags:
        return "", []

    # Each input tag expands to one or more (namespace, value) rows.
    # Bare tags expand into both OPEN and FLAG; closed-prefix tags
    # stay single. We collect all rows for the IN-tuple, then count
    # *distinct values* in the HAVING so a bare tag still counts once
    # whether it landed in the open or the flag namespace.
    flat: list[tuple[str, str]] = []
    distinct_count = 0
    for s in tags:
        rows = _parse_tag_string(s)
        flat.extend(rows)
        distinct_count += 1
    tuple_placeholders = ", ".join(["(%s, %s)"] * len(flat))

    if block_level:
        # Chunk-level: filter refs whose chunks collectively carry
        # all N tags. AND semantics across distinct tags, but the
        # tags don't have to be on the same chunk.
        # Chunk-level tags don't carry expires_at in v1 (migration 0010
        # added the column only on ref_tags) — no expiry filter needed.
        fragment = (
            f"{ref_alias}.ref_id IN ("
            f"  SELECT c.ref_id "
            f"  FROM chunks c "
            f"  JOIN chunk_tags ct ON ct.chunk_id = c.chunk_id "
            f"  JOIN tags t ON t.tag_id = ct.tag_id "
            f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders}) "
            f"  GROUP BY c.ref_id "
            f"  HAVING COUNT(DISTINCT t.value) = %s"
            f")"
        )
    else:
        # Ref-level tags can carry TTL via ref_tags.expires_at (migration
        # 0009). Exclude expired tags from the filter — agents that pin a
        # memory with ttl_days=30 expect the memory to drop out of
        # ``tags=['sticky:thread']`` filters once the TTL passes. Expired
        # rows stay in the table for audit; the runtime just doesn't see
        # them through this verb.
        fragment = (
            f"{ref_alias}.ref_id IN ("
            f"  SELECT rt.ref_id "
            f"  FROM ref_tags rt "
            f"  JOIN tags t ON t.tag_id = rt.tag_id "
            f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders}) "
            f"    AND (rt.expires_at IS NULL OR rt.expires_at > now()) "
            f"  GROUP BY rt.ref_id "
            f"  HAVING COUNT(DISTINCT t.value) = %s"
            f")"
        )

    params: list[Any] = []
    for ns, val in flat:
        params.append(ns)
        params.append(val)
    params.append(distinct_count)
    return fragment, params


#: Canonical control tag for fenced, speculative dream output
#: (docs/design/dreaming.md, §Inspire behavior). The ``DREAM`` axis is
#: a closed vocabulary; ``speculative`` is its low-confidence value.
SPECULATIVE_TAG = "DREAM:speculative"
_SPECULATIVE_NS = "DREAM"
_SPECULATIVE_VALUE = "speculative"


def is_speculative_tag(tag: str) -> bool:
    """True iff ``tag`` is the ``DREAM:speculative`` control tag.

    Used to detect an *explicit* opt-in: a caller that lists this tag in
    ``tags=`` is asking to see fenced inspirations, so the fence lifts
    for that query (docs/design/dreaming.md §Inspire — "surface on
    explicit ask").
    """
    return tag.strip() == SPECULATIVE_TAG


def speculative_fence(ref_alias: str = "r") -> str:
    """SQL clause excluding refs tagged ``DREAM:speculative``.

    Returns a bare ``NOT EXISTS (...)`` predicate (no leading ``AND``,
    no bind params — the namespace/value are fixed constants, not user
    input). Parameterless on purpose: the fused-search CTE splices its
    shared WHERE fragment twice, and a param-free clause stays correct
    under that duplication. Splice into a ``clauses`` list that gets
    ``AND``-joined.

    Default search fences speculative dream output so inspirations never
    pollute authoritative results; the fence is a no-op for every kind
    that never carries the tag (paper/todo/...).
    """
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        f"WHERE rt.ref_id = {ref_alias}.ref_id "
        f"AND t.namespace = '{_SPECULATIVE_NS}' "
        f"AND t.value = '{_SPECULATIVE_VALUE}')"
    )


#: Canonical control tag for on-demand Wikipedia content. Stamped on
#: every ``wikipedia`` ref at fetch time. The ``ORIGIN`` axis is a closed
#: vocabulary; ``wikipedia`` is its first value (room for ``gutenberg``,
#: …). Fenced from default search so tertiary encyclopedic prose never
#: dilutes the curated paper library — the whole point of fetching
#: Wikipedia on demand instead of bulk-embedding a dump.
WIKI_TAG = "ORIGIN:wikipedia"
_WIKI_NS = "ORIGIN"
_WIKI_VALUE = "wikipedia"


def is_wiki_tag(tag: str) -> bool:
    """True iff ``tag`` is the ``ORIGIN:wikipedia`` control tag.

    Used to detect an *explicit* opt-in: a caller listing this tag in
    ``tags=`` is asking to include Wikipedia content, so the fence lifts
    for that query (mirrors :func:`is_speculative_tag`).
    """
    return tag.strip() == WIKI_TAG


def wiki_fence(ref_alias: str = "r") -> str:
    """SQL clause excluding refs tagged ``ORIGIN:wikipedia``.

    Parameterless ``NOT EXISTS (...)`` predicate (no leading ``AND``,
    fixed namespace/value constants) — safe under the fused-search CTE's
    double-splice, exactly like :func:`speculative_fence`. Splice into a
    ``clauses`` list that gets ``AND``-joined.

    Default + cross-kind search fences Wikipedia content so on-demand
    encyclopedic fetches never compete with the curated corpus for top-k
    slots; the fence is a no-op for every kind that never carries the
    tag. Callers lift it for an explicit ``kind='wikipedia'`` scope or an
    ``ORIGIN:wikipedia`` opt-in (see ``_blocks_ops._fence_wiki``).
    """
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        f"WHERE rt.ref_id = {ref_alias}.ref_id "
        f"AND t.namespace = '{_WIKI_NS}' "
        f"AND t.value = '{_WIKI_VALUE}')"
    )


__all__ = [
    "SPECULATIVE_TAG",
    "WIKI_TAG",
    "build_tag_filter",
    "is_speculative_tag",
    "is_wiki_tag",
    "speculative_fence",
    "wiki_fence",
]
