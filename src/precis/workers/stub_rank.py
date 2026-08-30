"""stub_rank — S2-enrich, embed, and anchor-similarity re-rank paper stubs.

A paper *stub* (``kind='paper'``, ``pdf_sha256 IS NULL`` —
:mod:`precis.store._stub_predicate`) is title/abstract-only, invisible to
``embed:bge-m3``/``chase``'s chunk-level machinery, waiting on
``fetch_oa``/a human for its PDF. With thousands of open stubs, this pass
ranks "which matter now" so ``fetch_oa``'s claim query and the
``stub_backlog``/``search(view='stubs')`` surfaces float relevant stubs
instead of draining newest-first.

Four steps per run:

1. **Enrich** (:func:`_run_enrich`) — batch-resolves pending stubs
   (up to :func:`_enrich_batch_size`) against Semantic Scholar, merging
   ``abstract`` (only if absent — never clobbers a richer source),
   ``s2_fields``, ``s2_citation_count`` into ``refs.meta``. An
   unresolvable stub gets ``meta.s2_enriched_at``+``s2_enrich_failed`` so
   it isn't retried forever (mirrors ``openalex_enrich``'s miss-stamp).
2. **Embed** (:func:`_run_embed`) — enriched-but-unvectored stubs get
   ``title + "\\n\\n" + abstract`` (title alone if no abstract) embedded
   into ``ref_embeddings`` (parallel to ``chunk_embeddings``, keyed on
   ``ref_id`` — a stub has no chunk).
3. **Rank** (:func:`_run_rank`) — global recompute over every vectored
   pending stub: score = best cosine match against weighted anchors
   (every active quest's ``card_combined`` vector, weight 1.0; every
   paper opened in the last :func:`_opened_days` days, anchored on its
   own mean early-chunk vector, weight decaying by
   :func:`_half_life_days` half-life since open). Percentile → CANON prio
   (1=hottest..10=coldest); tag clamps then adjust (explicit-acquisition
   floors at 3; obscure-discovery-poor-score sinks to 9). A ref already
   prioritised by a human/quest (``prio IS NOT NULL AND
   meta.prio_by != 'stub_rank'``) is untouched; no anchors → logged no-op.
4. **LLM band** (:func:`_run_llm_band`) — the uncertain
   :func:`_llm_band_lo`-:func:`_llm_band_hi` percentile middle (default
   30-70th) is where the anchor-cosine score is least trustworthy. Each
   still-unlabeled candidate in the band (capped by
   :func:`_llm_batch_limit`) gets one SMALL-tier LLM call against active
   quests' title+mission, labeled ``core``/``adjacent``/``explore``/``off``
   + reason, stamped once into ``refs.meta`` (decision log: percentile,
   model, timestamp, cost, tokens — queryable via ``llm_label IS NOT NULL``
   for a future outcome join against acquisition/open events). The label
   is one-time; step 3 applies it as a fixed prio delta (-2/0/+1/+2,
   clamped 1..9 before tag overrides) on every subsequent re-rank without
   re-calling the LLM. No mission-carrying quest, no ``band_client``, or a
   zero batch cap → logged no-op.

Registered as the ``stub_rank`` :class:`~precis.workers.registry.ServiceSpec`
(system profile, alongside ``fetch``/``openalex_enrich``); wired in
``cli/worker.py``'s ``_register``.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
from psycopg.types.json import Jsonb

from precis.embedder import Embedder
from precis.ingest.semantic_scholar import get_papers_batch, s2_stub_meta
from precis.store import Store
from precis.store._stub_predicate import stub_predicate_sql
from precis.utils.llm.json_reply import extract_json_object

log = logging.getLogger(__name__)

#: The one embedder this pass reads/writes — matches the corpus-wide
#: default (``embed:bge-m3``) so a stub's vector and a chunk's vector
#: live in the same similarity space.
_EMBEDDER_NAME = "bge-m3"

_DEFAULT_ENRICH_BATCH = 500
_DEFAULT_OPENED_DAYS = 90.0
_DEFAULT_HALF_LIFE_DAYS = 30.0
_DEFAULT_BAND_LO = 0.30
_DEFAULT_BAND_HI = 0.70
_DEFAULT_LLM_BATCH = 25

#: TTL (minutes) on a :func:`_claim_band_candidates` lease — the
#: ``meta.llm_band_claimed_at`` stamp IS the lease, so a claim expires and
#: frees the stub back up for re-claim after this long even if the pass
#: that took it crashed mid-batch before writing a label.
_BAND_CLAIM_TTL_MIN = 10

#: Lifetime cap on paid LLM-band retries per stub (``meta.
#: llm_band_failures``). Once a stub has failed this many times it's
#: excluded from every future claim and stays permanently unlabeled —
#: ranked by the anchor-cosine percentile alone (see
#: :func:`_run_llm_band`'s docstring).
_MAX_BAND_FAILURES = 3

#: Cap on how many of a recently-opened paper's own (lowest-``ord``) chunk
#: vectors get averaged into its anchor — a full-paper mean would dilute
#: the anchor with body sections unrelated to why it was opened; the first
#: few chunks (title/abstract/intro) carry the paper's actual topic.
_MAX_OPEN_ANCHOR_CHUNKS = 8

#: Weight of every active-quest anchor — quests don't compete on recency
#: (there's no natural decay for "this is still the mission"), so each
#: live one counts equally.
_QUEST_ANCHOR_WEIGHT = 1.0

#: Mirrors ``BgeM3Embedder._BGE_M3_MAX_CHARS`` (embedder.py) — a local
#: guard so the combined title+abstract text handed to ANY embedder
#: backend (a ``RemoteEmbedder`` doesn't self-truncate client-side) never
#: exceeds what the model can sanely encode.
_MAX_EMBED_CHARS = 16_000

#: Injectable batch-resolve signature for :func:`_run_enrich` — matches
#: :func:`precis.ingest.semantic_scholar.get_papers_batch`'s shape so
#: tests can swap in a deterministic fake without touching the network.
ResolveBatchFn = Callable[[list[str], str], "list[dict[str, Any] | None]"]


# ── env knobs ──────────────────────────────────────────────────────────


def _enrich_batch_size() -> int:
    """Stubs enriched per pass (``PRECIS_STUB_RANK_ENRICH_BATCH``, default 500)."""
    raw = os.environ.get("PRECIS_STUB_RANK_ENRICH_BATCH", "").strip()
    if not raw:
        return _DEFAULT_ENRICH_BATCH
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_ENRICH_BATCH


def _opened_days() -> float:
    """Recency window for the "opened" anchor set (``PRECIS_STUB_RANK_OPENED_DAYS``,
    default 90)."""
    raw = os.environ.get("PRECIS_STUB_RANK_OPENED_DAYS", "").strip()
    if not raw:
        return _DEFAULT_OPENED_DAYS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return _DEFAULT_OPENED_DAYS


def _half_life_days() -> float:
    """Exponential decay half-life for an "opened" anchor's weight
    (``PRECIS_STUB_RANK_HALF_LIFE_DAYS``, default 30)."""
    raw = os.environ.get("PRECIS_STUB_RANK_HALF_LIFE_DAYS", "").strip()
    if not raw:
        return _DEFAULT_HALF_LIFE_DAYS
    try:
        return max(0.1, float(raw))
    except ValueError:
        return _DEFAULT_HALF_LIFE_DAYS


def _llm_band_lo() -> float:
    """Lower percentile bound of the LLM-band "uncertain middle"
    (``PRECIS_STUB_RANK_BAND_LO``, default 0.30)."""
    raw = os.environ.get("PRECIS_STUB_RANK_BAND_LO", "").strip()
    if not raw:
        return _DEFAULT_BAND_LO
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BAND_LO


def _llm_band_hi() -> float:
    """Upper percentile bound of the LLM-band "uncertain middle"
    (``PRECIS_STUB_RANK_BAND_HI``, default 0.70)."""
    raw = os.environ.get("PRECIS_STUB_RANK_BAND_HI", "").strip()
    if not raw:
        return _DEFAULT_BAND_HI
    try:
        return float(raw)
    except ValueError:
        return _DEFAULT_BAND_HI


def _llm_batch_limit() -> int:
    """Max LLM band calls per pass run (``PRECIS_STUB_RANK_LLM_BATCH``,
    default 25) — the cost guard. ``0`` disables step (d) entirely."""
    raw = os.environ.get("PRECIS_STUB_RANK_LLM_BATCH", "").strip()
    if not raw:
        return _DEFAULT_LLM_BATCH
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_LLM_BATCH


# ── shared vector plumbing ───────────────────────────────────────────────


def _parse_vec(text: str) -> list[float]:
    """Parse pgvector text ``"[a, b, ...]"`` into floats.

    Mirrors ``workers/clusterize.py``'s ``_parse_vec`` — every raw-vector
    read in this module casts the column to ``::text`` and parses it here
    rather than trusting the registered pgvector adapter's return shape,
    so the dtype fed to numpy is always explicit.
    """
    s = text.strip()
    if s.startswith("[") and s.endswith("]"):
        s = s[1:-1]
    return [float(x) for x in s.split(",") if x.strip()]


# ── (a) enrich ────────────────────────────────────────────────────────


def _id_for_s2(s2_id: str | None, doi: str | None, arxiv: str | None) -> str:
    """The identifier :func:`~precis.ingest.semantic_scholar.get_papers_batch`
    resolves this stub by — S2 id preferred (needs no prefix), else the
    lib's ``DOI:`` form, else (a task-spec extension, not just the two
    forms called out) ``ARXIV:`` — an arXiv-only stub would otherwise never
    enrich. ``stub_predicate_sql`` guarantees at least one of the three is
    present, so an empty return is unreachable in practice.
    """
    if s2_id:
        return s2_id
    if doi:
        return f"DOI:{doi}"
    if arxiv:
        return f"ARXIV:{arxiv.removeprefix('arxiv:')}"
    return ""


def _merge_enrich_meta(
    existing_meta: dict[str, Any],
    resolved: dict[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any]:
    """The ``refs.meta`` patch :func:`_run_enrich` shallow-merges in.

    Pure — no DB access — so the abstract-not-clobbered rule and the
    failed-resolve stamp are unit-testable without a connection.
    ``resolved is None`` (S2 couldn't resolve this stub's id) stamps a
    failure marker only, so :func:`_claim_enrich_candidates`'s
    ``meta->>'s2_enriched_at' IS NULL`` predicate doesn't re-select it
    forever. The resolved-case patch itself is built by
    :func:`~precis.ingest.semantic_scholar.s2_stub_meta` (shared with the
    mint-time callers that write this same shape up front); this wrapper
    only adds the never-clobber rule on top — an existing non-empty
    ``abstract`` (from a richer source than S2) survives untouched.
    """
    if resolved is None:
        return {"s2_enriched_at": now.isoformat(), "s2_enrich_failed": True}
    patch = s2_stub_meta(resolved, now=now)
    existing_abstract = existing_meta.get("abstract")
    has_existing = isinstance(existing_abstract, str) and existing_abstract.strip()
    if has_existing:
        patch.pop("abstract", None)
    return patch


def _claim_enrich_candidates(
    store: Store, *, limit: int
) -> list[tuple[int, dict[str, Any], str]]:
    """Up to ``limit`` un-enriched pending stubs: ``(ref_id, meta, s2_lookup_id)``.

    ``FOR UPDATE OF r SKIP LOCKED`` lets more than one node run this pass
    without double-claiming the same stub within the same tick — same
    idiom as ``fetch_oa.claim_stubs_to_fetch``. The lock itself is
    short-lived (this SELECT is its own transaction); a stub processed
    twice across a race is harmless (idempotent stamp), so no lease table
    is needed on top.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, r.meta,
                   (SELECT min(id_value) FROM ref_identifiers
                     WHERE ref_id = r.ref_id AND id_kind = 's2')    AS s2_id,
                   (SELECT min(id_value) FROM ref_identifiers
                     WHERE ref_id = r.ref_id AND id_kind = 'doi')   AS doi,
                   (SELECT min(id_value) FROM ref_identifiers
                     WHERE ref_id = r.ref_id AND id_kind = 'arxiv') AS arxiv
              FROM refs r
             WHERE {stub_predicate_sql("r")}
               AND r.meta->>'s2_enriched_at' IS NULL
             ORDER BY r.ref_id DESC
             LIMIT %s
               FOR UPDATE OF r SKIP LOCKED
            """,
            (limit,),
        ).fetchall()
    return [(int(r[0]), dict(r[1] or {}), _id_for_s2(r[2], r[3], r[4])) for r in rows]


def _run_enrich(
    store: Store,
    *,
    limit: int,
    api_key: str,
    resolve_batch: ResolveBatchFn,
) -> tuple[int, int]:
    """Step (a). Returns ``(attempted, resolved)``."""
    candidates = _claim_enrich_candidates(store, limit=limit)
    if not candidates:
        return 0, 0

    lookup_ids = [c[2] for c in candidates]
    try:
        resolved_list = resolve_batch(lookup_ids, api_key)
    except Exception:
        log.warning(
            "stub_rank enrich: batch S2 resolve failed for %d stub(s)",
            len(candidates),
            exc_info=True,
        )
        resolved_list = [None] * len(candidates)

    now = datetime.now(UTC)
    resolved_count = 0
    with store.pool.connection() as conn:
        for (ref_id, existing_meta, _lookup_id), resolved in zip(
            candidates, resolved_list, strict=True
        ):
            if resolved is not None:
                resolved_count += 1
            patch = _merge_enrich_meta(existing_meta, resolved, now=now)
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb, updated_at = now() "
                "WHERE ref_id = %s",
                (Jsonb(patch), ref_id),
            )
    return len(candidates), resolved_count


# ── (b) embed ─────────────────────────────────────────────────────────


def _build_stub_text(
    title: str, abstract: str, *, max_chars: int = _MAX_EMBED_CHARS
) -> str:
    """The text embedded for one stub: ``title + "\\n\\n" + abstract``
    (title alone with no abstract), truncated to ``max_chars``."""
    title = (title or "").strip()
    abstract = (abstract or "").strip()
    text = f"{title}\n\n{abstract}" if abstract else title
    return text[:max_chars] if len(text) > max_chars else text


def _claim_embed_candidates(store: Store, *, limit: int) -> list[tuple[int, str]]:
    """Up to ``limit`` enriched-but-unvectored stubs: ``(ref_id, text)``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, r.title, r.meta->>'abstract'
              FROM refs r
             WHERE {stub_predicate_sql("r")}
               AND r.meta->>'s2_enriched_at' IS NOT NULL
               AND NOT EXISTS (
                     SELECT 1 FROM ref_embeddings re
                      WHERE re.ref_id = r.ref_id AND re.embedder = %s
                   )
             ORDER BY r.ref_id DESC
             LIMIT %s
               FOR UPDATE OF r SKIP LOCKED
            """,
            (_EMBEDDER_NAME, limit),
        ).fetchall()
    return [(int(r[0]), _build_stub_text(r[1] or "", r[2] or "")) for r in rows]


def _run_embed(
    store: Store, *, embedder: Embedder | None, limit: int
) -> tuple[int, int]:
    """Step (b). Returns ``(attempted, embedded)``. No-op with no embedder."""
    if embedder is None:
        return 0, 0
    candidates = _claim_embed_candidates(store, limit=limit)
    if not candidates:
        return 0, 0

    ref_ids = [c[0] for c in candidates]
    texts = [c[1] for c in candidates]
    try:
        vectors = embedder.embed(texts)
    except Exception:
        log.warning(
            "stub_rank embed: embedder.embed failed for %d stub(s)",
            len(ref_ids),
            exc_info=True,
        )
        return len(ref_ids), 0

    with store.pool.connection() as conn:
        for ref_id, vector in zip(ref_ids, vectors, strict=True):
            conn.execute(
                "INSERT INTO ref_embeddings (ref_id, embedder, embedding) "
                "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                (ref_id, _EMBEDDER_NAME, vector),
            )
    return len(ref_ids), len(vectors)


# ── (c) rank ──────────────────────────────────────────────────────────


def _clamp_prio(
    prio: int,
    p: float,
    *,
    dream_acquire: bool,
    requested_by_quest: bool,
    discovered_via_cite: bool,
) -> int:
    """Apply the two tag-driven clamps to a percentile-derived ``prio``.

    An explicit acquisition request (``DREAM:acquire``, or the stub's
    provenance is a quest) never sinks below hot-ish (``prio <= 3``) no
    matter how the anchors score it. An obscure citation-graph discovery
    (``discovered-via:cite:%``) that scores in the bottom 30th percentile
    is pinned to the coldest band (``prio == 9``) — corroborating context,
    not something to actively chase.
    """
    if dream_acquire or requested_by_quest:
        prio = min(prio, 3)
    if discovered_via_cite and p < 0.3:
        prio = max(prio, 9)
    return prio


#: The one-time LLM label's fixed prio delta, applied on *every* re-rank
#: (:func:`_prio_from_percentile`) so the Tier-2 judgment survives without
#: re-calling the LLM. ``core`` (directly serves a current interest) pulls
#: hotter, ``off`` (unrelated) pushes colder, ``adjacent`` is a no-op, and
#: ``explore`` (novel but interesting) nudges slightly colder than
#: ``adjacent`` — interesting, but not as trusted as a same-subfield match.
_LLM_LABEL_DELTA: dict[str, int] = {"core": -2, "adjacent": 0, "explore": 1, "off": 2}


def compute_stub_percentiles(
    stub_vectors: dict[int, list[float]],
    anchors: list[tuple[list[float], float]],
) -> dict[int, float]:
    """Pure scoring math: ``ref_id -> percentile rank (0 worst, 1 best)``.

    Score per stub = ``max_i(weight_i * cosine(stub_vec, anchor_i))``; the
    percentile is the stub's rank among ``stub_vectors`` by that score.
    Factored out of :func:`compute_stub_prios` so :func:`_run_rank` can
    hand the same percentile map to the step (d) LLM band without
    recomputing the cosine pass. Returns ``{}`` with no stubs or no
    anchors.
    """
    if not stub_vectors or not anchors:
        return {}

    ref_ids = list(stub_vectors.keys())
    stub_mat = np.array([stub_vectors[rid] for rid in ref_ids], dtype=np.float64)
    stub_norms = np.linalg.norm(stub_mat, axis=1, keepdims=True)
    stub_norms[stub_norms == 0] = 1.0
    stub_unit = stub_mat / stub_norms

    scores = np.full(len(ref_ids), -np.inf, dtype=np.float64)
    for anchor_vec, weight in anchors:
        a = np.array(anchor_vec, dtype=np.float64)
        a_norm = np.linalg.norm(a)
        if a_norm == 0:
            continue
        cos = stub_unit @ (a / a_norm)
        scores = np.maximum(scores, weight * cos)

    order = np.argsort(scores)  # ascending: worst -> best
    ranks = np.empty(len(scores), dtype=np.float64)
    ranks[order] = np.arange(len(scores), dtype=np.float64)
    p = ranks / (len(scores) - 1) if len(scores) > 1 else np.ones_like(ranks)

    return {ref_id: float(p[idx]) for idx, ref_id in enumerate(ref_ids)}


def _prio_from_percentile(p: float, flags: dict[str, Any]) -> int:
    """One stub's final CANON prio from its percentile + flags.

    CANON prio (1=hottest..10=coldest, but this pass only ever emits
    1..9 — see the module docstring) starts at ``1 + round((1 - p) * 8)``,
    so the single best-scoring stub gets the LOWEST prio (1) and the
    worst gets 9. A one-time :data:`_LLM_LABEL_DELTA` (from
    ``flags['llm_label']``, when set) then nudges that base prio and is
    clamped back to ``1..9`` — *before* :func:`_clamp_prio`'s two
    tag-driven overrides run last, so an explicit acquisition request or
    a cite-cold pin always wins over the LLM's opinion.
    """
    base_prio = int(1 + round((1 - p) * 8))
    label = flags.get("llm_label")
    delta = _LLM_LABEL_DELTA.get(label, 0) if isinstance(label, str) else 0
    labeled_prio = max(1, min(9, base_prio + delta))
    return _clamp_prio(
        labeled_prio,
        p,
        dream_acquire=bool(flags.get("dream_acquire")),
        requested_by_quest=bool(flags.get("requested_by_quest")),
        discovered_via_cite=bool(flags.get("discovered_via_cite")),
    )


def compute_stub_prios(
    stub_vectors: dict[int, list[float]],
    anchors: list[tuple[list[float], float]],
    flags: dict[int, dict[str, Any]],
) -> dict[int, int]:
    """Pure ranking math: ``ref_id -> new CANON prio (1..9)``.

    Thin wrapper over :func:`compute_stub_percentiles` +
    :func:`_prio_from_percentile` — kept as a single-call entry point for
    callers (and tests) that don't need the intermediate percentile map.
    Returns ``{}`` with no stubs or no anchors — "no anchors" is the
    "nothing to rank against" case the caller logs a warning for and
    writes nothing.
    """
    percentiles = compute_stub_percentiles(stub_vectors, anchors)
    return {
        ref_id: _prio_from_percentile(p, flags.get(ref_id, {}))
        for ref_id, p in percentiles.items()
    }


def _should_write_prio(
    current_prio: int | None, current_prio_by: str | None, new_prio: int
) -> bool:
    """Whether :func:`_run_rank` should write ``new_prio`` for a ref.

    Never clobbers a human/quest-set prio: a non-NULL ``prio`` whose
    ``meta.prio_by`` isn't ``'stub_rank'`` is off-limits entirely. Among
    the rows this pass owns, only an actual change (a different prio
    value, or the first time this pass has stamped ``prio_by``) is worth
    a write — a no-op re-write every pass would still be correct but is
    pure UPDATE churn on rows whose ranking hasn't moved.
    """
    if current_prio is not None and current_prio_by != "stub_rank":
        return False
    return current_prio != new_prio or current_prio_by != "stub_rank"


def _load_rank_candidates(
    store: Store,
) -> tuple[
    dict[int, list[float]],
    dict[int, tuple[int | None, str | None]],
    dict[int, dict[str, Any]],
]:
    """Pending stubs that carry a :data:`_EMBEDDER_NAME` vector, plus the
    bits :func:`_should_write_prio` / :func:`_prio_from_percentile` need
    per ref (including the step (d) LLM label, when one has been
    stamped)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            SELECT r.ref_id, re.embedding::text, r.prio, r.meta->>'prio_by',
                   EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'DREAM'
                        AND t.value = 'acquire'
                        AND (rt.expires_at IS NULL OR rt.expires_at > now())
                   ) AS dream_acquire,
                   (
                     r.meta->>'set_by' = 'quest'
                     OR EXISTS (
                       SELECT 1 FROM links l JOIN refs q ON q.ref_id = l.dst_ref_id
                        WHERE l.src_ref_id = r.ref_id AND l.relation = 'serves'
                          AND q.kind = 'quest' AND q.deleted_at IS NULL
                     )
                   ) AS requested_by_quest,
                   EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'OPEN'
                        AND t.value LIKE 'discovered-via:cite:%%'
                        AND (rt.expires_at IS NULL OR rt.expires_at > now())
                   ) AS discovered_via_cite,
                   r.meta->>'llm_label' AS llm_label
              FROM refs r
              JOIN ref_embeddings re ON re.ref_id = r.ref_id
                AND re.embedder = %s
             WHERE {stub_predicate_sql("r")}
            """,
            (_EMBEDDER_NAME,),
        ).fetchall()

    stub_vectors: dict[int, list[float]] = {}
    current: dict[int, tuple[int | None, str | None]] = {}
    flags: dict[int, dict[str, Any]] = {}
    for row in rows:
        ref_id = int(row[0])
        stub_vectors[ref_id] = _parse_vec(row[1])
        current[ref_id] = (row[2], row[3])
        flags[ref_id] = {
            "dream_acquire": bool(row[4]),
            "requested_by_quest": bool(row[5]),
            "discovered_via_cite": bool(row[6]),
            "llm_label": row[7],
        }
    return stub_vectors, current, flags


def _load_anchors(store: Store) -> list[tuple[list[float], float]]:
    """The weighted anchor set — active-quest mission vectors plus
    recently-opened papers' mean chunk vectors, decayed by recency."""
    anchors: list[tuple[list[float], float]] = []

    with store.pool.connection() as conn:
        quest_rows = conn.execute(
            """
            SELECT ce.vector::text
              FROM refs q
              JOIN ref_tags rt ON rt.ref_id = q.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
               AND t.namespace = 'STATUS' AND t.value = 'active'
              JOIN chunks c ON c.ref_id = q.ref_id
               AND c.ord = -1 AND c.chunk_kind = 'card_combined'
              JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
               AND ce.embedder = %s AND ce.status = 'ok'
             WHERE q.kind = 'quest' AND q.deleted_at IS NULL
               AND (rt.expires_at IS NULL OR rt.expires_at > now())
            """,
            (_EMBEDDER_NAME,),
        ).fetchall()
        opened_rows = conn.execute(
            """
            WITH opened AS (
                SELECT ref_id, max(ts) AS last_open
                  FROM ref_events
                 WHERE source = 'manual:open'
                   AND ts > now() - (%(days)s || ' days')::interval
                 GROUP BY ref_id
            ),
            ranked AS (
                SELECT c.ref_id, ce.vector::text AS vec,
                       row_number() OVER (
                           PARTITION BY c.ref_id ORDER BY c.ord ASC
                       ) AS rn
                  FROM chunks c
                  JOIN chunk_embeddings ce ON ce.chunk_id = c.chunk_id
                   AND ce.embedder = %(embedder)s AND ce.status = 'ok'
                 WHERE c.ord >= 0
                   AND c.ref_id IN (SELECT ref_id FROM opened)
            )
            SELECT o.ref_id, o.last_open, r.vec
              FROM opened o
              JOIN ranked r ON r.ref_id = o.ref_id AND r.rn <= %(max_chunks)s
            """,
            {
                "days": _opened_days(),
                "embedder": _EMBEDDER_NAME,
                "max_chunks": _MAX_OPEN_ANCHOR_CHUNKS,
            },
        ).fetchall()

    for r in quest_rows:
        anchors.append((_parse_vec(r[0]), _QUEST_ANCHOR_WEIGHT))

    opened_vecs: dict[int, list[list[float]]] = {}
    opened_last: dict[int, datetime] = {}
    for ref_id, last_open, vec_text in opened_rows:
        rid = int(ref_id)
        opened_vecs.setdefault(rid, []).append(_parse_vec(vec_text))
        prev = opened_last.get(rid)
        if prev is None or last_open > prev:
            opened_last[rid] = last_open

    half_life = _half_life_days()
    now = datetime.now(UTC)
    for ref_id, vecs in opened_vecs.items():
        mean_vec = np.mean(np.array(vecs, dtype=np.float64), axis=0).tolist()
        age_days = max((now - opened_last[ref_id]).total_seconds() / 86400.0, 0.0)
        weight = 2.0 ** (-age_days / half_life)
        anchors.append((mean_vec, weight))

    return anchors


def _run_rank(store: Store) -> tuple[int, dict[int, float]]:
    """Step (c). Returns ``(written_count, percentiles)`` — the latter is
    reused by :func:`_run_llm_band` (step (d)) so the cosine pass runs
    once per tick, not twice."""
    stub_vectors, current, flags = _load_rank_candidates(store)
    if not stub_vectors:
        return 0, {}

    anchors = _load_anchors(store)
    if not anchors:
        log.warning(
            "stub_rank rank: no anchors available (no active quests, no "
            "recently-opened papers) -- skipping this pass, writing nothing"
        )
        return 0, {}

    percentiles = compute_stub_percentiles(stub_vectors, anchors)
    new_prios = {
        ref_id: _prio_from_percentile(p, flags.get(ref_id, {}))
        for ref_id, p in percentiles.items()
    }
    updates = [
        (ref_id, new_prio)
        for ref_id, new_prio in new_prios.items()
        if _should_write_prio(current[ref_id][0], current[ref_id][1], new_prio)
    ]
    if not updates:
        return 0, percentiles

    values_sql = ", ".join(["(%s, %s)"] * len(updates))
    params: list[Any] = [v for pair in updates for v in pair]
    with store.pool.connection() as conn:
        conn.execute(
            f"""
            UPDATE refs r
               SET prio = v.prio,
                   meta = r.meta || jsonb_build_object('prio_by', 'stub_rank'),
                   updated_at = now()
              FROM (VALUES {values_sql}) AS v(ref_id, prio)
             WHERE r.ref_id = v.ref_id
               -- never-clobber guard re-checked atomically at write time:
               -- a prio set by a human/quest between our snapshot read and
               -- this UPDATE must survive, so the filter in
               -- _should_write_prio alone (stale snapshot) is not enough.
               AND (r.prio IS NULL OR r.meta->>'prio_by' = 'stub_rank')
            """,
            params,
        )
    return len(updates), percentiles


# ── (d) LLM band ─────────────────────────────────────────────────────


#: Interest-profile text budget (chars) — total across every active
#: quest's line, and per-quest card-text truncation. Keeps the prompt
#: small even with a dozen live quests.
_INTEREST_PROFILE_MAX_CHARS = 1500
_INTEREST_PROFILE_PER_QUEST_CHARS = 300

#: Abstract truncation for the band prompt — generous enough to carry the
#: gist without ballooning the per-call token cost.
_BAND_ABSTRACT_MAX_CHARS = 1200

_VALID_LLM_LABELS = frozenset(_LLM_LABEL_DELTA)

_BAND_SYS = (
    "You triage a research-paper acquisition queue against the user's "
    "current interests. Reply with ONLY a JSON object: "
    '{"label": "core|adjacent|explore|off", "reason": "<one short line>"}. '
    "core = directly serves a current interest; adjacent = same subfield, "
    "plausibly useful; explore = novel but interesting direction; "
    "off = unrelated."
)


def _load_interest_profile(store: Store) -> str:
    """``- {quest title}: {card text}`` per active quest with a mission
    card, one line each, capped at :data:`_INTEREST_PROFILE_MAX_CHARS`
    total. Empty when no active quest carries a card — "nothing to judge
    the band against" is the caller's cue to skip step (d) entirely."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT q.title, c.text
              FROM refs q
              JOIN ref_tags rt ON rt.ref_id = q.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
               AND t.namespace = 'STATUS' AND t.value = 'active'
              JOIN chunks c ON c.ref_id = q.ref_id
               AND c.ord = -1 AND c.chunk_kind = 'card_combined'
             WHERE q.kind = 'quest' AND q.deleted_at IS NULL
               AND (rt.expires_at IS NULL OR rt.expires_at > now())
            """
        ).fetchall()

    lines: list[str] = []
    total = 0
    for title, card_text in rows:
        title = (title or "").strip()
        text = (card_text or "").strip()[:_INTEREST_PROFILE_PER_QUEST_CHARS]
        if not title and not text:
            continue
        line = f"- {title}: {text}"
        if total + len(line) > _INTEREST_PROFILE_MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


def _build_band_prompt(interest_profile: str, title: str, abstract: str) -> str:
    """The per-stub user turn: the interest profile, then the candidate's
    title + (truncated) abstract."""
    abstract = (abstract or "").strip()[:_BAND_ABSTRACT_MAX_CHARS]
    return (
        f"Current interests:\n{interest_profile}\n\n"
        f"Candidate paper:\nTitle: {title or '(no title)'}\n"
        f"Abstract: {abstract or '(no abstract)'}"
    )


def _claim_band_candidates(
    store: Store, *, band_ids: list[int], limit: int
) -> list[tuple[int, str, str]]:
    """Atomically claim up to ``limit`` still-unlabeled stubs among
    ``band_ids`` for one paid LLM-band call each: ``(ref_id, title,
    abstract)``.

    Unlike :func:`_claim_enrich_candidates`'s bare ``SELECT ... FOR
    UPDATE`` (whose lock releases at that same short transaction's commit,
    serializing nothing beyond the instant), this claim is a single
    ``UPDATE ... FROM (SELECT ... FOR UPDATE SKIP LOCKED) ... RETURNING``
    statement that stamps ``meta.llm_band_claimed_at`` on the rows it
    claims. That stamp IS the lease: a paid LLM call is expensive enough
    that two cluster nodes racing this pass in the same tick (``stub_rank``
    runs on the system profile, every node concurrently) must not both
    claim (and pay for) the same stub, which a lock that's already
    released by the time the network call runs cannot prevent. The lease
    expires after :data:`_BAND_CLAIM_TTL_MIN` minutes so a pass that
    crashes mid-batch (network down, worker killed) doesn't strand its
    claims forever. ``meta.llm_band_failures`` (incremented by
    :func:`_run_llm_band`'s except-path) additionally excludes a stub once
    it's failed :data:`_MAX_BAND_FAILURES` times — the lifetime cap on
    paid retries per stub.

    ``RETURNING`` row order isn't guaranteed to match the inner query's
    ``ORDER BY``, so the claimed rows are re-sorted in Python by the same
    hottest-first intent (``prio ASC NULLS LAST, ref_id DESC``) before
    they're handed back — the whole claimed batch still gets processed
    either way, but callers that care about processing order shouldn't
    rely on DB row order alone.
    """
    if not band_ids:
        return []
    predicate = stub_predicate_sql("r2")
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""
            UPDATE refs r
               SET meta = r.meta || jsonb_build_object(
                             'llm_band_claimed_at', now()::text)
              FROM (
                    SELECT r2.ref_id, r2.prio
                      FROM refs r2
                     WHERE r2.ref_id = ANY(%(band_ids)s)
                       AND {predicate}
                       AND r2.meta->>'llm_label' IS NULL
                       AND (r2.meta->>'llm_band_claimed_at' IS NULL
                            OR (r2.meta->>'llm_band_claimed_at')::timestamptz
                                 < now() - make_interval(mins => %(ttl_min)s))
                       AND COALESCE((r2.meta->>'llm_band_failures')::int, 0)
                             < %(max_failures)s
                     ORDER BY r2.prio ASC NULLS LAST, r2.ref_id DESC
                     LIMIT %(limit)s
                       FOR UPDATE OF r2 SKIP LOCKED
                   ) c
             WHERE r.ref_id = c.ref_id
             RETURNING r.ref_id, r.title, r.meta->>'abstract', c.prio
            """,
            {
                "band_ids": band_ids,
                "ttl_min": _BAND_CLAIM_TTL_MIN,
                "max_failures": _MAX_BAND_FAILURES,
                "limit": limit,
            },
        ).fetchall()
    ordered = sorted(
        rows, key=lambda r: (r[3] is None, r[3] if r[3] is not None else 0, -r[0])
    )
    return [(int(r[0]), r[1] or "", r[2] or "") for r in ordered]


def _run_llm_band(
    store: Store,
    *,
    client: Any | None,
    percentiles: dict[int, float],
    limit: int,
) -> tuple[int, int]:
    """Step (d). Returns ``(attempted, labeled)``.

    Skips entirely (``(0, 0)``) when there's no ``client``, the batch cap
    is closed (``limit <= 0``), there are no scored stubs (``percentiles``
    empty), no stub falls in the uncertain
    :func:`_llm_band_lo`..:func:`_llm_band_hi` band, or no active quest
    carries a mission card to judge against. Each claimed stub gets one
    ``client.complete`` call; a raised exception or an unparseable/unknown
    label leaves it unlabeled and increments ``meta.llm_band_failures``,
    still counting toward ``attempted`` but not ``labeled``. A failed stub
    retries on a later pass only until it hits :data:`_MAX_BAND_FAILURES`
    (see :func:`_claim_band_candidates`) — past that cap it's excluded from
    every future claim and stays permanently unlabeled, ranked by the
    anchor-cosine percentile alone (step (c)'s pure ranking, no LLM delta).
    """
    if client is None or limit <= 0 or not percentiles:
        return 0, 0

    lo, hi = _llm_band_lo(), _llm_band_hi()
    band_ids = [ref_id for ref_id, p in percentiles.items() if lo <= p <= hi]
    if not band_ids:
        return 0, 0

    interest_profile = _load_interest_profile(store)
    if not interest_profile:
        log.info(
            "stub_rank band: no active quest carries a mission card -- "
            "nothing to judge the band against, skipping"
        )
        return 0, 0

    candidates = _claim_band_candidates(store, band_ids=band_ids, limit=limit)
    if not candidates:
        return 0, 0

    now = datetime.now(UTC)
    labeled = 0
    cost_total = 0.0
    tokens_total = 0
    for ref_id, title, abstract in candidates:
        prompt = _build_band_prompt(interest_profile, title, abstract)
        try:
            result = client.complete(
                [
                    {"role": "system", "content": _BAND_SYS},
                    {"role": "user", "content": prompt},
                ]
            )
            parsed = extract_json_object(result.text)
            label = parsed.get("label") if parsed else None
            if not isinstance(label, str) or label not in _VALID_LLM_LABELS:
                raise ValueError(f"invalid/missing label: {label!r}")
            reason = parsed.get("reason") if parsed else None
        except Exception:
            log.warning(
                "stub_rank band: LLM label failed for ref %d",
                ref_id,
                exc_info=True,
            )
            # Bump the lifetime paid-retry counter on its own short
            # connection (same rationale as the per-stub write below: a
            # held-open connection across ~25 multi-second network calls
            # would starve the pool). _claim_band_candidates reads this
            # back next pass to enforce _MAX_BAND_FAILURES.
            with store.pool.connection() as conn:
                conn.execute(
                    """
                    UPDATE refs SET meta = jsonb_set(
                             meta, '{llm_band_failures}',
                             to_jsonb(COALESCE((meta->>'llm_band_failures')::int, 0) + 1)
                           ), updated_at = now()
                     WHERE ref_id = %s
                    """,
                    (ref_id,),
                )
            continue

        patch = {
            "llm_label": label,
            "llm_reason": str(reason or "").strip()[:300],
            "llm_band": {
                "p": round(percentiles.get(ref_id, 0.0), 4),
                "model": result.model,
                "ts": now.isoformat(),
                "cost_usd": result.cost_usd,
                "total_tokens": result.total_tokens,
            },
        }
        # A short-lived connection per write, NOT one held across the
        # loop: each ``client.complete`` is a multi-second network call,
        # and ~25 of them under one pooled connection would starve the
        # pool for minutes; per-stub writes also survive a mid-loop crash.
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb, updated_at = now() "
                "WHERE ref_id = %s",
                (Jsonb(patch), ref_id),
            )
        labeled += 1
        if result.cost_usd is not None:
            cost_total += result.cost_usd
        if result.total_tokens is not None:
            tokens_total += result.total_tokens

    log.info(
        "stub_rank band: labeled %d/%d stub(s), cost=$%.4f, tokens=%d",
        labeled,
        len(candidates),
        cost_total,
        tokens_total,
    )
    return len(candidates), labeled


# ── entry point ──────────────────────────────────────────────────────


def run_stub_rank_pass(
    store: Store,
    *,
    limit: int | None = None,
    embedder: Embedder | None = None,
    api_key: str | None = None,
    resolve_batch: ResolveBatchFn | None = None,
    band_client: Any | None = None,
    band_limit: int | None = None,
) -> dict[str, int]:
    """Run one stub_rank pass: enrich, embed, re-rank every scored stub,
    then LLM-label the uncertain middle of the score distribution.

    ``limit`` bounds steps (a)/(b) (default :func:`_enrich_batch_size`,
    ``PRECIS_STUB_RANK_ENRICH_BATCH``); step (c) always recomputes over
    every pending stub with a vector (a single in-memory numpy pass, cheap
    even at corpus scale — see the module docstring). ``embedder=None``
    skips step (b) only (enrich + rank still run). ``api_key`` defaults to
    ``$SEMANTIC_SCHOLAR_API_KEY``; ``resolve_batch`` is the S2
    batch-resolve injection seam for tests (default
    :func:`~precis.ingest.semantic_scholar.get_papers_batch`).
    ``band_client=None`` skips step (d) only (steps (a)-(c) still run),
    mirroring ``embedder=None``'s step-(b) skip — a duck-typed ``.complete``
    seam (like the summarize/classify passes' ``DispatchClient``).
    ``band_limit`` bounds step (d) (default :func:`_llm_batch_limit`,
    ``PRECIS_STUB_RANK_LLM_BATCH``; ``0`` disables it regardless of
    ``band_client``).

    Returns the ``BatchResult`` shape ``{claimed, ok, failed}``:
    ``claimed`` sums every candidate touched across the four steps,
    ``ok`` sums the ones that landed a real write (resolved / embedded /
    re-ranked / labeled). Individual per-stub failures are logged and
    excluded from both counts rather than raising — a bad S2 id, a poison
    embed, or an unparseable LLM reply must not take the whole pass down.
    """
    if api_key is None:
        from precis.secrets import get_secret

        api_key = (get_secret("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    resolve = resolve_batch or get_papers_batch
    batch_limit = limit if limit is not None else _enrich_batch_size()
    band_batch_limit = band_limit if band_limit is not None else _llm_batch_limit()

    enrich_attempted, enrich_resolved = _run_enrich(
        store, limit=batch_limit, api_key=api_key, resolve_batch=resolve
    )
    embed_attempted, embed_ok = _run_embed(store, embedder=embedder, limit=batch_limit)
    ranked, percentiles = _run_rank(store)
    band_attempted, band_labeled = _run_llm_band(
        store, client=band_client, percentiles=percentiles, limit=band_batch_limit
    )

    return {
        "claimed": enrich_attempted + embed_attempted + ranked + band_attempted,
        "ok": enrich_resolved + embed_ok + ranked + band_labeled,
        "failed": 0,
    }


__all__ = [
    "ResolveBatchFn",
    "compute_stub_percentiles",
    "compute_stub_prios",
    "run_stub_rank_pass",
]
