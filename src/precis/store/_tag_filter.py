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
    ``"=tier:dream"``      → (``"tier"``,   ``"dream"``)   exact-axis
                           escape — a stored lowercase/mixed-case
                           namespace the plain ``ns:value`` spelling
                           can't reach (it would parse as OPEN/FLAG).
    ``"studytype:conceptual"`` → (``"STUDYTYPE"``, ``"conceptual"``)
                           plus the OPEN/FLAG literal pair — the
                           lowercase facet spelling of an aliased
                           paper axis reaches both stored shapes
                           (gr718845).

We can filter "ref carries all of these tags" with one IN-subquery
that joins ``ref_tags``+``tags`` against a ``VALUES`` table of the
expanded ``(namespace, value)`` rows — each row stamped with the slot
of the input tag that produced it — then ``GROUP BY ref_id HAVING
COUNT(DISTINCT slot) = N`` to enforce AND semantics.

This module exposes one helper that returns a SQL fragment + params,
which any caller in :mod:`precis.store.store` can splice into its
WHERE clause unconditionally — passing ``None`` or ``[]`` returns
``("", [])`` so the splice is a no-op.

Paper access filters derive their markers at read time from OpenAlex
status and arXiv identity. This covers old records and keeps the search
cohort current when metadata changes.

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

from precis.store._stub_predicate import arxiv_fallback_sql

PAPER_ACCESS_FILTERS = frozenset({"ACCESS:open", "ACCESS:arxiv"})


def _paper_access_predicate(tag: str, alias: str) -> str:
    arxiv = (
        "EXISTS (SELECT 1 FROM ref_identifiers ai "
        f"WHERE ai.ref_id = {alias}.ref_id AND (ai.id_kind = 'arxiv' "
        "OR (ai.id_kind = 'doi' AND starts_with(ai.id_value, '10.48550/arxiv.')))) "
        f"OR ({arxiv_fallback_sql(alias)}) IS NOT NULL"
    )
    evidence = arxiv
    if tag == "ACCESS:open":
        evidence += (
            f" OR COALESCE({alias}.meta #>> '{{openalex,oa_status}}', '') "
            "IN ('gold', 'green', 'hybrid', 'diamond', 'bronze')"
        )
    return f"({alias}.kind = 'paper' AND ({evidence}))"


# Only the seven ref-level facets documented in precis-paper-tag-axes alias.
# Other lowercase prefixes keep their literal meaning, including junk:x.
# The default paper junk exclusion lifts only for an uppercase JUNK: pin.
_PAPER_FACET_AXES = frozenset(
    {"DOMAIN", "MATERIAL", "PROPERTY", "DIM", "SCALE", "STUDYTYPE", "TRANSPORT"}
)


def _parse_tag_string(s: str) -> list[tuple[str, str]]:
    """Parse an agent-facing canonical tag string into one or more
    ``(namespace, value)`` rows.

    Mirrors :mod:`precis.store._tags_ops`'s canonical mapping:

    - ``"PREFIX:value"`` with uppercase prefix → single ``(prefix, value)``
    - ``"=ns:value"``                        → single ``(ns, value)`` —
      the exact-axis escape (gr636493): ``=`` forces the closed-pair
      read for a namespace that isn't all-uppercase, which the plain
      spelling would expand to the OPEN/FLAG pair below. Spelled by
      :func:`tag_filter_string` for stored lowercase-axis rows
      (``tier:dream`` …).
    - ``"ns:value"`` where ``ns.upper()`` is an aliased facet axis
      (``studytype:conceptual``; gr718845) → the closed row
      ``(ns.upper(), value)`` *plus* the OPEN/FLAG pair — the
      documented lowercase facet spelling reaches both the axis
      pass's stored uppercase row and the literal open-string tag a
      manual ``tag(add=)`` writes. Only the paper-facet axes alias —
      see :data:`_PAPER_FACET_AXES`; a lowercase spelling of any other axis
      (``status:open``, ``junk:junk``) stays an open-tag filter.
    - bare string ``"workspace"``            → both ``(OPEN, "workspace")``
                                               and ``(FLAG, "workspace")``

    The bare-string expansion makes cross-kind tag filtering namespace-
    agnostic: a caller writing ``tags=['workspace']`` doesn't have to
    know whether the ref carries the tag in the ``OPEN`` or the
    ``FLAG`` namespace. The SQL planner emits one combined IN-tuple
    and counts *distinct input-tag slots* (the ``want.slot`` column of
    the VALUES leg), so a tag still counts once whichever spelling
    matched.
    """
    if s.startswith("="):
        ns, sep, v = s[1:].partition(":")
        if sep and ns and v:
            return [(ns, v)]
    elif ":" in s:
        prefix, _, value = s.partition(":")
        if prefix and prefix.isupper():
            return [(prefix, value)]
        if prefix and prefix.upper() in _PAPER_FACET_AXES:
            return [(prefix.upper(), value), ("OPEN", s), ("FLAG", s)]
    return [("OPEN", s), ("FLAG", s)]


def tag_filter_string(ns: str, value: str) -> str:
    """Canonical filter spelling of a stored ``(namespace, value)``
    row — the inverse of :func:`_parse_tag_string`, used wherever a
    ``/drive?tag=`` link or tag-filter value is built from a stored
    tag pair (row chips, autocomplete, ``/tags/refs`` redirects).

    ``OPEN`` rides bare unless its value would not parse back to the
    exact stored pair — then ``=OPEN:value`` preserves it literally.
    That covers values resembling filter syntax (``!``, ``=``, an
    uppercase ``NS:`` prefix — an uppercase prefix parses as the
    closed pair, never the OPEN row) and, since the facet alias
    (gr718845), values whose lowercase ``ns:`` prefix names an
    aliased paper axis: a stored OPEN ``studytype:review`` spelled
    bare would alias the ``STUDYTYPE:review`` row into a link that
    promises only the open tag. Otherwise the parse expands to
    OPEN+FLAG — deliberate:
    a bare-word filter doesn't care which sentinel row carries it. An
    uppercase axis spells ``NS:value`` (``FLAG`` included — it
    parses back to the exact ``(FLAG, value)`` row). A lowercase or
    mixed-case axis takes the ``=ns:value`` exact escape: the plain
    ``ns:value`` spelling would parse as the OPEN/FLAG pair and match
    nothing (gr636493 — ``tier:dream``, ``tier:synthetic-insight``).
    """
    if ns == "OPEN":
        # Stored values can themselves look like filter syntax. Escape
        # those before polarity splitting or namespace parsing sees them.
        head = value.partition(":")[0] if ":" in value else ""
        if (
            value.startswith(("!", "="))
            or head.isupper()
            or head.upper() in _PAPER_FACET_AXES
        ):
            return f"=OPEN:{value}"
        return value
    if ns.isupper():
        return f"{ns}:{value}"
    return f"={ns}:{value}"


def split_tag_polarity(
    tags: list[str] | None,
) -> tuple[list[str], list[str]]:
    """Split canonical filter tags into ``(positive, negated)``.

    ``Tag.normalize_filter`` emits ``'!<tag>'`` for a negated entry
    (gr568354); SQL callers get the split inside :func:`build_tag_filter`,
    and bespoke consumers (Python post-filters, array-contains
    predicates) call this to apply the same polarity. Negated entries
    come back *without* the ``!`` — they name the tag to exclude.
    """
    pos: list[str] = []
    neg: list[str] = []
    for t in tags or []:
        if t.startswith("!"):
            body = t[1:].strip()
            if body:
                neg.append(body)
        else:
            pos.append(t)
    return pos, neg


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
                     calling this helper. A leading ``!`` negates the
                     tag (gr568354): ``'!card:experiment'`` excludes
                     refs carrying that tag, composing with the
                     positive entries and ``q=``.
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
        parameters in the order they appear in the fragment (positive
        leg: namespace, value, slot, namespace, value, slot, ..., N;
        negated leg: namespace, value, ...).

    Semantics:
        Paper access markers use ref metadata and identifiers when
        filtering refs. They compose with stored tags in either polarity.
        AND across all tags — a ref must carry **every** positive tag
        in ``tags`` to pass, and **no** negated (``!``-prefixed) tag.
        The ``HAVING COUNT(DISTINCT want.slot) = N`` clause enforces
        the positive AND
        without requiring N self-joins; negations collapse into a
        single ``NOT IN`` over the union of the negated tag tuples —
        "carries none of these" is ``NOT (carries A OR carries B)``,
        which is exactly per-tag exclusion ANDed together.
    """
    if not tags:
        return "", []

    parts: list[str] = []
    params: list[Any] = []
    stored_tags: list[str] = []
    for raw in tags:
        tag = raw.removeprefix("!")
        if not block_level and tag in PAPER_ACCESS_FILTERS:
            predicate = _paper_access_predicate(tag, ref_alias)
            parts.append(f"NOT {predicate}" if raw.startswith("!") else predicate)
        else:
            stored_tags.append(raw)

    positive, negated = split_tag_polarity(stored_tags)

    if positive:
        # Each input tag expands to one or more (namespace, value) rows —
        # bare tags land in both OPEN and FLAG, an aliased lowercase
        # facet-axis spelling lands in the closed axis *and* the literal
        # OPEN/FLAG pair (gr718845 — see _PAPER_FACET_AXES for the alias set). A tag is satisfied when ANY of its rows is
        # present, so the AND counts *input tags*, not matched rows: the
        # ``want`` VALUES leg tags each row with the slot of the tag that
        # produced it and ``COUNT(DISTINCT want.slot) = N`` is the exact
        # AND. (``COUNT(DISTINCT t.value)`` only approximated that while
        # every expansion of one tag shared a value — the axis alias, or
        # value-colliding inputs like ``['STATUS:open', 'open']``, both
        # over/undercounted.)
        flat: list[tuple[str, str, int]] = []
        for slot, s in enumerate(positive):
            for ns, val in _parse_tag_string(s):
                flat.append((ns, val, slot))
        value_rows = ", ".join(["(%s, %s, %s)"] * len(flat))

        if block_level:
            # Chunk-level: filter refs whose chunks collectively carry
            # all N tags. AND semantics across distinct tags, but the
            # tags don't have to be on the same chunk.
            # Chunk-level tags don't carry expires_at in v1 (migration 0010
            # added the column only on ref_tags) — no expiry filter needed.
            parts.append(
                f"{ref_alias}.ref_id IN ("
                f"  SELECT c.ref_id "
                f"  FROM (VALUES {value_rows}) AS want(ns, val, slot) "
                f"  JOIN tags t ON t.namespace = want.ns "
                f"    AND t.value = want.val "
                f"  JOIN chunk_tags ct ON ct.tag_id = t.tag_id "
                f"  JOIN chunks c ON c.chunk_id = ct.chunk_id "
                f"  GROUP BY c.ref_id "
                f"  HAVING COUNT(DISTINCT want.slot) = %s"
                f")"
            )
        else:
            # Ref-level tags can carry TTL via ref_tags.expires_at (migration
            # 0009). Exclude expired tags from the filter — agents that pin a
            # memory with ttl_days=30 expect the memory to drop out of
            # ``tags=['sticky:thread']`` filters once the TTL passes. Expired
            # rows stay in the table for audit; the runtime just doesn't see
            # them through this verb.
            parts.append(
                f"{ref_alias}.ref_id IN ("
                f"  SELECT rt.ref_id "
                f"  FROM (VALUES {value_rows}) AS want(ns, val, slot) "
                f"  JOIN tags t ON t.namespace = want.ns "
                f"    AND t.value = want.val "
                f"  JOIN ref_tags rt ON rt.tag_id = t.tag_id "
                f"  WHERE (rt.expires_at IS NULL OR rt.expires_at > now()) "
                f"  GROUP BY rt.ref_id "
                f"  HAVING COUNT(DISTINCT want.slot) = %s"
                f")"
            )
        for ns, val, slot in flat:
            params.append(ns)
            params.append(val)
            params.append(slot)
        params.append(len(positive))

    if negated:
        # ``NOT IN`` over the union of the negated (namespace, value)
        # tuples: a ref carrying *any* excluded tag drops out. No
        # HAVING — one matching tag row is enough. Same expiry rule as
        # the positive leg: an expired tag attachment doesn't count as
        # "carrying" the tag, so it can't exclude either.
        neg_flat: list[tuple[str, str]] = []
        for s in negated:
            neg_flat.extend(_parse_tag_string(s))
        tuple_placeholders = ", ".join(["(%s, %s)"] * len(neg_flat))
        if block_level:
            parts.append(
                f"{ref_alias}.ref_id NOT IN ("
                f"  SELECT c.ref_id "
                f"  FROM chunks c "
                f"  JOIN chunk_tags ct ON ct.chunk_id = c.chunk_id "
                f"  JOIN tags t ON t.tag_id = ct.tag_id "
                f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders})"
                f")"
            )
        else:
            parts.append(
                f"{ref_alias}.ref_id NOT IN ("
                f"  SELECT rt.ref_id "
                f"  FROM ref_tags rt "
                f"  JOIN tags t ON t.tag_id = rt.tag_id "
                f"  WHERE (t.namespace, t.value) IN ({tuple_placeholders}) "
                f"    AND (rt.expires_at IS NULL OR rt.expires_at > now())"
                f")"
            )
        for ns, val in neg_flat:
            params.append(ns)
            params.append(val)

    return " AND ".join(parts), params


#: Canonical control tag for fenced, speculative dream output
#: (gr690614, §Inspire behavior). The ``DREAM`` axis is
#: a closed vocabulary; ``speculative`` is its low-confidence value.
SPECULATIVE_TAG = "DREAM:speculative"
_SPECULATIVE_NS = "DREAM"
_SPECULATIVE_VALUE = "speculative"


def is_speculative_tag(tag: str) -> bool:
    """True iff ``tag`` is the ``DREAM:speculative`` control tag.

    Used to detect an *explicit* opt-in: a caller that lists this tag in
    ``tags=`` is asking to see fenced inspirations, so the fence lifts
    for that query (gr690614 §Inspire — "surface on
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
    ``ORIGIN:wikipedia`` opt-in (see ``_chunks_ops._fence_wiki``).
    """
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        f"WHERE rt.ref_id = {ref_alias}.ref_id "
        f"AND t.namespace = '{_WIKI_NS}' "
        f"AND t.value = '{_WIKI_VALUE}')"
    )


#: Canonical control tag for the finding-search do-not-repropose ledger
#: (me690849 §"Refuted lifecycle"). Stamped
#: on the negative-ruling finding a rejected hypothesis is
#: ``retracts``/``superseded-by``-linked to. The ``STATUS`` axis is a
#: closed vocabulary; ``refuted`` is one of its values.
REFUTED_TAG = "STATUS:refuted"
_REFUTED_NS = "STATUS"
_REFUTED_VALUE = "refuted"


def is_refuted_tag(tag: str) -> bool:
    """True iff ``tag`` is the ``STATUS:refuted`` control tag.

    Used to detect an *explicit* opt-in: a caller listing this tag in
    ``tags=`` is asking to see refuted findings, so the fence lifts for
    that query (mirrors :func:`is_speculative_tag` / :func:`is_wiki_tag`).
    ``search(kind='finding', status='refuted')`` desugars to exactly this
    tag, so the fence lifts automatically for that shorthand too.
    """
    return tag.strip() == REFUTED_TAG


def refuted_fence(ref_alias: str = "r") -> str:
    """SQL clause excluding refs tagged ``STATUS:refuted``.

    Parameterless ``NOT EXISTS (...)`` predicate (no leading ``AND``,
    fixed namespace/value constants) — safe under the fused-search CTE's
    double-splice, exactly like :func:`speculative_fence` /
    :func:`wiki_fence`. Splice into a ``clauses`` list that gets
    ``AND``-joined.

    Default + cross-kind search fences refuted findings — the
    do-not-repropose ledger — so a rejected hypothesis never resurfaces
    as if it were still live; the fence is a no-op for every kind that
    never carries the tag. Callers lift it via ``status='refuted'``
    (which desugars to the ``STATUS:refuted`` tag) or an explicit
    ``STATUS:refuted`` opt-in in ``tags=``.
    """
    return (
        "NOT EXISTS ("
        "SELECT 1 FROM ref_tags rt "
        "JOIN tags t ON t.tag_id = rt.tag_id "
        f"WHERE rt.ref_id = {ref_alias}.ref_id "
        f"AND t.namespace = '{_REFUTED_NS}' "
        f"AND t.value = '{_REFUTED_VALUE}')"
    )


__all__ = [
    "REFUTED_TAG",
    "SPECULATIVE_TAG",
    "WIKI_TAG",
    "build_tag_filter",
    "is_refuted_tag",
    "is_speculative_tag",
    "is_wiki_tag",
    "refuted_fence",
    "speculative_fence",
    "split_tag_polarity",
    "tag_filter_string",
    "wiki_fence",
]
