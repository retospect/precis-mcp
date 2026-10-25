"""Shared claim query for the per-ref body-text sweeps.

``bib_parse``, ``bib_retag``, ``paper_glossary`` and ``classify_topics`` each
select "live refs of these kinds that have body chunks and are not yet done"
as ``(ref_id, title)``. They differ only in four things, which are the
parameters here: the ref kinds, the SQL predicate (and binds) that says "not
done yet", whether a claim-time attempt lease excludes recently-attempted
refs, and whether the SELECT takes ``FOR UPDATE OF r SKIP LOCKED``.

Claims order by plain ``ref_id`` (the PK early-stop the bib passes rely
on). The categorizer callers opt into the shared backfill priority bands
(:func:`priority_band_sql`, gr515633) with ``banded=True``: quest-linked
refs first (whatever their body state), then usable-body refs, then
recently touched refs, then the tail — ``ref_id`` breaks ties inside a
band.

Pure SQL plumbing; the caller owns the connection and its commit.
"""

from __future__ import annotations

import time
from bisect import bisect_left
from typing import Any

from precis.store._body_predicate import unusable_body_tag_sql
from precis.workers import ref_lease

__all__ = [
    "BAND_REF_BATCH",
    "RECENT_TOUCH_DAYS",
    "band_batches",
    "band_predicates",
    "band_ref_ids",
    "claim_batch",
    "drop_band_ids",
    "invalidate_band_ids",
    "priority_band_sql",
    "quest_linked_sql",
    "random_chunk_anchor",
    "random_ref_anchor",
]

#: "Recently touched" window for priority band 3 (gr515633): a ref whose
#: ``GREATEST(updated_at, last_viewed_at, last_recalled_at)`` is inside this
#: many days outranks the untouched tail. The same column set
#: ``dream_consolidate``'s "touched" notion uses — a write, a human view, or
#: an agent recall all count.
RECENT_TOUCH_DAYS = 30


def quest_linked_sql(alias: str = "r") -> str:
    """``(A OR B)`` boolean fragment: ref ``<alias>`` is part of a live quest.

    Two membership signals: a ``serves`` link from the ref to a live
    ``kind='quest'`` ref (the canonical "this paper serves that quest"
    edge), or the ``quest:<public_id>`` OPEN tag
    :func:`precis.quest.tagging.tag_serving_papers` stamps on serving
    papers — resolving the tag's ``<public_id>`` suffix against a live
    quest (numeric ``ref_id`` or a ``cite_key`` identifier, mirroring
    :attr:`precis.store.types.Ref.public_id`), so a tag naming a retired
    quest stops counting. References ``<alias>.ref_id``; contains ``%%``
    for the LIKE — it must sit inside a parameterised query like
    everything else here.
    """
    return (
        "(EXISTS (SELECT 1 FROM links ql JOIN refs qq "
        "ON qq.ref_id = ql.dst_ref_id "
        f"WHERE ql.src_ref_id = {alias}.ref_id AND ql.relation = 'serves' "
        "AND qq.kind = 'quest' AND qq.retired_at IS NULL) "
        "OR EXISTS (SELECT 1 FROM ref_tags qrt JOIN tags qt "
        "ON qt.tag_id = qrt.tag_id "
        f"WHERE qrt.ref_id = {alias}.ref_id AND qt.namespace = 'OPEN' "
        "AND qt.value LIKE 'quest:%%' "
        "AND (qrt.expires_at IS NULL OR qrt.expires_at > now()) "
        "AND EXISTS (SELECT 1 FROM refs qq2 "
        "WHERE qq2.kind = 'quest' AND qq2.retired_at IS NULL "
        "AND (qq2.ref_id::text = substring(qt.value from 7) "
        "OR EXISTS (SELECT 1 FROM ref_identifiers ri "
        "WHERE ri.ref_id = qq2.ref_id AND ri.id_kind = 'cite_key' "
        "AND ri.id_value = substring(qt.value from 7))))))"
    )


def priority_band_sql(alias: str = "r") -> str:
    """``CASE`` expression → backfill priority band 1-4 for ref ``<alias>``
    (gr515633 — "papers with a body or in a quest first").

    Bands, first match wins:

    1. quest-linked (:func:`quest_linked_sql`), whatever the body state —
       Reto's "papers with a body OR in a quest" first (gr515633 review);
    2. usable body,
    3. recently touched (see :data:`RECENT_TOUCH_DAYS`),
    4. everything else.

    "Usable body" reduces to "not tagged ``body:partial`` /
    ``body:needs_ocr``": every caller's eligibility already requires an
    ``ord >= 0`` body chunk, so chunk existence is not re-checked here —
    only the unusable-body tag decides. References ``<alias>.ref_id`` and
    its timestamp columns; contains ``%%`` (parameterised queries only).
    """
    usable = f"NOT ({unusable_body_tag_sql(alias)})"
    return (
        f"CASE WHEN {quest_linked_sql(alias)} THEN 1 "
        f"WHEN {usable} THEN 2 "
        f"WHEN GREATEST({alias}.updated_at, {alias}.last_viewed_at, "
        f"{alias}.last_recalled_at) > now() - "
        f"interval '{RECENT_TOUCH_DAYS} days' THEN 3 "
        "ELSE 4 END"
    )


def band_predicates(alias: str = "r") -> tuple[str, str, str, str]:
    """The four bands as independent ``WHERE`` fragments on ref ``<alias>`` —
    for claimers that drain bands with one slice per band instead of a
    ``CASE`` sort (the chunk-level sweeps: ``classify``, chunk-level
    ``axis_pass``). Same partition as :func:`priority_band_sql`, with one
    deliberate difference: the band-2 fragment is just "usable body" — a
    band-1 leftover (a quest-linked usable-body ref the band-1 slice
    couldn't lock) is still correctly preferred over bands 3/4, and in
    the sequential claim loop band 1 has always drained first anyway.
    Bands 3/4 exclude quest-linked refs explicitly so the fragments stay
    an exact partition of the CASE (a quest-linked ref with an unusable
    body is band 1, never the touched tail)."""
    unusable = unusable_body_tag_sql(alias)
    quest = quest_linked_sql(alias)
    touched = (
        f"GREATEST({alias}.updated_at, {alias}.last_viewed_at, "
        f"{alias}.last_recalled_at) > now() - "
        f"interval '{RECENT_TOUCH_DAYS} days'"
    )
    return (
        quest,
        f"NOT ({unusable})",
        f"({unusable}) AND {touched} AND NOT ({quest})",
        f"({unusable}) AND NOT ({touched}) AND NOT ({quest})",
    )


#: Refs per band-1/3 chunk-slice batch — a resolved band claims in
#: batches this size so each slice reads only that batch's paragraphs
#: through ``chunks_ref_id_idx`` (~3k rows, a few ms) instead of
#: fetching the whole band's chunks into an external-merge sort (918ms
#: plus a 190MB disk spill per claim at 3050 band-1 refs, prod
#: EXPLAIN ANALYZE, gr515633).
BAND_REF_BATCH = 32

#: TTL for the resolved band-ref sets — re-resolve (the ~80ms band-1
#: refs scan on prod) at most this often per worker process. A stale set
#: only delays a newly-quest-linked ref's promotion by a sweep or two —
#: ordering, never eligibility, so drift is safe.
_BAND_IDS_TTL_S = 300.0
_band_ids_cache: dict[tuple[str, tuple[str, ...]], tuple[float, list[int]]] = {}


def invalidate_band_ids() -> None:
    """Drop every cached band-ref set — test isolation (the ``store``
    fixture calls it per test, like the settings/secrets caches) and a
    hook for a forced re-resolution."""
    _band_ids_cache.clear()


def band_ref_ids(conn: Any, *, kinds: list[str], ref_pred: str) -> list[int]:
    """The ref set behind one *small* claim band (1: quest-linked, 3:
    touched unusable body), resolved in ONE refs scan and cached for
    :data:`_BAND_IDS_TTL_S`. Chunk-level claimers then drive their slice
    from ``c.ref_id = ANY(...)`` on ``chunks_ref_id_idx`` — letting the
    planner inline the band predicate in a CTE instead walked the whole
    paragraph-chunk index probing refs per chunk (the 7.7s band-1 probe
    on prod, gr515633). Skip the slice entirely when this returns
    empty."""
    key = (ref_pred, tuple(sorted(kinds)))
    now = time.monotonic()
    hit = _band_ids_cache.get(key)
    if hit is not None and now - hit[0] < _BAND_IDS_TTL_S:
        return hit[1]
    rows = conn.execute(
        "SELECT r.ref_id FROM refs r "
        "WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL "
        f"AND {ref_pred}",
        {"kinds": list(kinds)},
    ).fetchall()
    ids = [int(r[0]) for r in rows]
    _band_ids_cache[key] = (now, ids)
    return ids


def drop_band_ids(*, kinds: list[str], ref_pred: str, done: list[int]) -> None:
    """Drop ``done`` ref ids from the cached band set — a batch whose
    chunk slice claimed nothing is drained (tagged, leased, or locked
    in-flight) for the rest of the TTL, so re-probing its refs on every
    claim only burns ceil(N/32) empty slices (~96 queries/claim once a
    3050-ref band drains). Eligibility is unaffected: an id wrongly
    dropped just rejoins on the next resolution."""
    hit = _band_ids_cache.get((ref_pred, tuple(sorted(kinds))))
    if hit is None:
        return
    drop = set(done)
    hit[1][:] = [rid for rid in hit[1] if rid not in drop]


def band_batches(ids: list[int], floor: int | None) -> list[list[int]]:
    """``ids`` split into :data:`BAND_REF_BATCH` runs in claim order:
    the wrap-around ref anchor applied in Python — refs at/above
    ``floor`` first, then the wrap, both id-ascending (the same global
    order the old ``ORDER BY (ref_id < floor), ref_id, chunk_id``
    produced, one cheap batch at a time). Every batch sits on one side
    of the floor, so a batch slice orders by plain ``ref_id``."""
    ordered = sorted(ids)
    if floor is None:
        segments = [ordered]
    else:
        i = bisect_left(ordered, floor)
        segments = [ordered[i:], ordered[:i]]
    return [
        seg[i : i + BAND_REF_BATCH]
        for seg in segments
        for i in range(0, len(seg), BAND_REF_BATCH)
    ]


def random_chunk_anchor(conn: Any) -> int | None:
    """A random ``chunk_id`` in ``[0, max]`` — the fair-claim scan anchor
    for a band-2 chunk-PK walk (gr515633; Slice 3 fair-claim ordering,
    ``docs/backlog/small-llm-derived-drain-band.md``). One cheap scalar
    (``max(chunk_id)`` is an index lookup); ``None`` on an empty corpus."""
    row = conn.execute(
        "SELECT floor(random() * (max(chunk_id) + 1))::bigint FROM chunks"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def random_ref_anchor(conn: Any) -> int | None:
    """A random ``ref_id`` in ``[0, max]`` — the fair-claim scan anchor for
    the ref-driven band slices (gr515633): within a band the ref order
    wraps at this anchor. One cheap scalar; ``None`` on an empty corpus."""
    row = conn.execute(
        "SELECT floor(random() * (max(ref_id) + 1))::bigint FROM refs"
    ).fetchone()
    return int(row[0]) if row and row[0] is not None else None


def claim_batch(
    conn: Any,
    *,
    kinds: list[str],
    not_done_sql: str,
    params: dict[str, Any],
    limit: int,
    ref_ids: list[int] | None = None,
    lease_ns: str | None = None,
    skip_locked: bool = False,
    banded: bool = False,
) -> list[tuple[int, str]]:
    """``(ref_id, title)`` for up to ``limit`` live refs with body chunks.

    Args:
        kinds: ``refs.kind`` values to sweep.
        not_done_sql: SQL boolean expression (over alias ``r``) that is true
            while the ref still needs work; may use binds from ``params``.
        params: bind values for ``not_done_sql``.
        ref_ids: restrict the sweep to these refs (targeted backfill / tests).
        lease_ns: when set, also exclude refs under an unexpired attempt lease
            in this namespace (pass the already-built
            ``ref_lease.attempt_ns(...)`` value).
        skip_locked: append ``FOR UPDATE OF r SKIP LOCKED``.
        banded: claim in the shared backfill priority order
            (:func:`band_predicates`), sliced — the tiny quest-linked set
            via a CTE, then one ``ORDER BY r.ref_id`` PK walk per band,
            stopping when the batch is full. Opt-in for the categorizer
            sweeps (gr515633); a whole-candidate ``ORDER BY CASE`` had to
            evaluate every ref's band before LIMIT (~39k papers, 666ms
            per claim on prod). The default keeps plain ``r.ref_id``
            order, the PK early-stop ``bib_parse``/``bib_retag``/
            ``paper_glossary`` rely on.

    Ordering only — eligibility is unchanged, owned by the caller's
    ``not_done_sql``.
    """
    ref_filter = "AND r.ref_id = ANY(%(ref_ids)s)" if ref_ids else ""
    lease = ref_lease.exclude_clause("r.ref_id", "attempt_ns") if lease_ns else ""
    lock = "FOR UPDATE OF r SKIP LOCKED" if skip_locked else ""
    elig = f"""
        WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL
          {ref_filter}
          AND EXISTS (
            SELECT 1 FROM chunks c
            WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
          )
          AND ({not_done_sql})
          {lease}
    """
    binds: dict[str, Any] = {**params, "kinds": list(kinds)}
    if lease_ns:
        binds["attempt_ns"] = lease_ns
    if ref_ids:
        binds["ref_ids"] = list(ref_ids)
    if not banded:
        sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        {elig}
        ORDER BY r.ref_id
        LIMIT %(limit)s
        {lock}
        """
        rows = conn.execute(sql, {**binds, "limit": limit}).fetchall()
        return [(int(r[0]), str(r[1] or "")) for r in rows]
    band1, band2, band3, band4 = band_predicates("r")
    sql = f"""
        WITH bs AS (
          SELECT r.ref_id FROM refs r
          WHERE r.kind = ANY(%(kinds)s) AND r.retired_at IS NULL AND {band1}
        )
        SELECT r.ref_id, r.title
        FROM bs JOIN refs r ON r.ref_id = bs.ref_id
        {elig}
        ORDER BY r.ref_id
        LIMIT %(limit)s
        {lock}
    """
    rows = conn.execute(sql, {**binds, "limit": limit}).fetchall()
    out = [(int(r[0]), str(r[1] or "")) for r in rows]
    for band_pred in (band2, band3, band4):
        if len(out) >= limit:
            break
        got = "AND r.ref_id <> ALL(%(got_ids)s)" if out else ""
        sql = f"""
        SELECT r.ref_id, r.title
        FROM refs r
        {elig}
          AND {band_pred}
          {got}
        ORDER BY r.ref_id
        LIMIT %(limit)s
        {lock}
        """
        slice_binds = {**binds, "limit": limit - len(out)}
        if out:
            slice_binds["got_ids"] = [rid for rid, _ in out]
        rows = conn.execute(sql, slice_binds).fetchall()
        out += [(int(r[0]), str(r[1] or "")) for r in rows]
    return out


# ── opt-in finding / claim-hub kinds ────────────────────────────────────

#: Ref kinds the categorizers (``classify_topics``, ref-level ``axis:*``) skip
#: unless an operator opts in via a ``service_config`` row (see
#: :func:`opt_in_service`). Taproot claim hubs are ``finding`` refs tagged
#: ``TAPROOT:claim`` (~4k on prod); enabling them spends LLM credit, so they
#: are default-OFF (gr182078).
OPT_IN_KINDS = frozenset({"finding"})


def opt_in_service(kind: str) -> str:
    """``service_config`` service name that opts ``kind`` into the
    categorizers (default-OFF; flip with ``service prio`` / ``/categorizers``)."""
    return f"categorize_kind:{kind}"


#: SQL predicate (alias ``r``): editorial review notes (``TAPROOT:review``)
#: are findings but not claims, so they are never categorized.
NOT_REVIEW_NOTE_SQL = """
    NOT EXISTS (
      SELECT 1 FROM ref_tags rvt JOIN tags rvg ON rvg.tag_id = rvt.tag_id
      WHERE rvt.ref_id = r.ref_id AND rvg.namespace = 'TAPROOT'
        AND rvg.value = 'review'
    )
"""


def effective_kinds(
    base: list[str], candidates: Any = OPT_IN_KINDS, enabled: Any = ()
) -> list[str]:
    """``base`` plus each opt-in kind that is both a ``candidates`` entry
    (the pass/axis declares it applies) and in ``enabled`` (operator flipped
    its ``categorize_kind:<kind>`` service on)."""
    on = set(enabled or ())
    extra = [k for k in sorted(set(candidates or ()) & OPT_IN_KINDS) if k in on]
    return [*base, *(k for k in extra if k not in base)]


def finding_text(title: str, meta: Any) -> str:
    """Classifier text for a finding / claim hub: the claim sentence plus its
    ``meta.scope`` (a finding has no abstract or body beyond the sentence)."""
    scope = (meta or {}).get("scope") if isinstance(meta, dict) else None
    parts = [str(title or "").strip()]
    if isinstance(scope, dict):
        bits = [f"{k}: {v}" for k, v in scope.items() if v not in (None, "", [], {})]
        if bits:
            parts.append("Scope: " + "; ".join(bits))
    return "\n".join(p for p in parts if p)
