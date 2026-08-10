"""stub_rank — S2-enrich, embed, and anchor-similarity re-rank paper stubs.

A paper *stub* (``kind='paper'``, ``pdf_sha256 IS NULL`` — see
:mod:`precis.store._stub_predicate`) has no chunk body for ``embed:bge-m3``
or ``chase``'s salience machinery to reach — it's title/abstract metadata
only, waiting on ``fetch_oa`` (or a human) to land the PDF. With thousands
of open stubs and only so much acquisition bandwidth, this pass answers
"which stubs matter *now*" so ``fetch_oa``'s quest-reweighted claim query
and the operator-facing backlog surfaces (``stub_backlog`` /
``search(view='stubs')``) can float the relevant ones instead of draining
newest-first.

Each pass run does three steps, in order:

1. **Enrich** (:func:`_run_enrich`) — batch-resolves up to
   :func:`_enrich_batch_size` pending stubs against Semantic Scholar
   (:func:`precis.ingest.semantic_scholar.get_papers_batch`), merging
   ``abstract`` (only when the ref has none yet — never clobbers an
   existing abstract from a richer source), ``s2_fields``, and
   ``s2_citation_count`` into ``refs.meta``. A stub S2 can't resolve still
   gets ``meta.s2_enriched_at`` stamped (+ ``s2_enrich_failed: true``) so
   it isn't retried every pass forever — mirrors ``openalex_enrich``'s
   miss-stamp convention.

2. **Embed** (:func:`_run_embed`) — every enriched-but-unvectored stub
   gets ``title + "\\n\\n" + abstract`` (title alone with no abstract)
   embedded and written to :data:`ref_embeddings <precis.migrations.
   0116_ref_embeddings>` (parallel to ``chunk_embeddings`` but keyed
   straight to ``ref_id`` — a stub has no chunk to hang a vector off of).

3. **Rank** (:func:`_run_rank`) — a *global* recompute over every pending
   stub that now has a vector: each stub's score is the best cosine match
   against a set of weighted anchor vectors —

   * every **active quest**'s ``card_combined`` chunk vector, weight 1.0
     (a quest's mission statement, reused verbatim — see
     :meth:`~precis.store.Store.upsert_card_combined`);
   * every paper opened in the last
     :func:`_opened_days` days (``ref_events.source='manual:open'``),
     anchored on the mean of its own first few chunk vectors, weight
     decaying with an exponential half-life (:func:`_half_life_days`) off
     the most recent open.

   A stub's percentile rank among all scored stubs maps to CANON prio
   (1=hottest..10=coldest, see ``store/types.py``); two tag-driven clamps
   then adjust it (an explicit acquisition request floors the prio at 3;
   an obscure citation-graph discovery with a poor score sinks to 9). A
   ref a human or a quest has already prioritised
   (``prio IS NOT NULL AND meta.prio_by != 'stub_rank'``) is never
   touched. No anchors available (no active quests, nothing opened
   recently) is a logged no-op — there's nothing to rank *against*.

Registered as the ``stub_rank`` :class:`~precis.workers.registry.
ServiceSpec` (system profile, alongside ``fetch``/``openalex_enrich`` —
see ``workers/registry.py``); wired in ``cli/worker.py``'s ``_register``.
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
from precis.ingest.semantic_scholar import get_papers_batch
from precis.store import Store
from precis.store._stub_predicate import stub_predicate_sql

log = logging.getLogger(__name__)

#: The one embedder this pass reads/writes — matches the corpus-wide
#: default (``embed:bge-m3``) so a stub's vector and a chunk's vector
#: live in the same similarity space.
_EMBEDDER_NAME = "bge-m3"

_DEFAULT_ENRICH_BATCH = 500
_DEFAULT_OPENED_DAYS = 90.0
_DEFAULT_HALF_LIFE_DAYS = 30.0

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
    forever.
    """
    if resolved is None:
        return {"s2_enriched_at": now.isoformat(), "s2_enrich_failed": True}
    patch: dict[str, Any] = {
        "s2_enriched_at": now.isoformat(),
        "s2_fields": resolved.get("fields") or [],
        "s2_citation_count": resolved.get("citation_count"),
    }
    existing_abstract = existing_meta.get("abstract")
    resolved_abstract = resolved.get("abstract")
    has_existing = isinstance(existing_abstract, str) and existing_abstract.strip()
    has_resolved = isinstance(resolved_abstract, str) and resolved_abstract.strip()
    if not has_existing and has_resolved:
        patch["abstract"] = resolved_abstract
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


def compute_stub_prios(
    stub_vectors: dict[int, list[float]],
    anchors: list[tuple[list[float], float]],
    flags: dict[int, dict[str, bool]],
) -> dict[int, int]:
    """Pure ranking math: ``ref_id -> new CANON prio (1..9)``.

    Score per stub = ``max_i(weight_i * cosine(stub_vec, anchor_i))``.
    Percentile ``p`` (0 worst, 1 best) is the stub's rank among
    ``stub_vectors`` by that score; CANON prio (1=hottest..10=coldest,
    but this pass only ever emits 1..9 — see the module docstring) is
    ``1 + round((1 - p) * 8)``, so the single best-scoring stub gets the
    LOWEST prio (1) and the worst gets 9. :func:`_clamp_prio` then applies
    the two tag-driven overrides per stub.

    Returns ``{}`` with no stubs or no anchors — "no anchors" is the
    "nothing to rank against" case the caller logs a warning for and
    writes nothing.
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

    result: dict[int, int] = {}
    for idx, ref_id in enumerate(ref_ids):
        base_prio = int(1 + round((1 - float(p[idx])) * 8))
        f = flags.get(ref_id, {})
        result[ref_id] = _clamp_prio(
            base_prio,
            float(p[idx]),
            dream_acquire=bool(f.get("dream_acquire")),
            requested_by_quest=bool(f.get("requested_by_quest")),
            discovered_via_cite=bool(f.get("discovered_via_cite")),
        )
    return result


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
    dict[int, dict[str, bool]],
]:
    """Pending stubs that carry a :data:`_EMBEDDER_NAME` vector, plus the
    bits :func:`_should_write_prio` / :func:`_clamp_prio` need per ref."""
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
                   ) AS discovered_via_cite
              FROM refs r
              JOIN ref_embeddings re ON re.ref_id = r.ref_id
                AND re.embedder = %s
             WHERE {stub_predicate_sql("r")}
            """,
            (_EMBEDDER_NAME,),
        ).fetchall()

    stub_vectors: dict[int, list[float]] = {}
    current: dict[int, tuple[int | None, str | None]] = {}
    flags: dict[int, dict[str, bool]] = {}
    for row in rows:
        ref_id = int(row[0])
        stub_vectors[ref_id] = _parse_vec(row[1])
        current[ref_id] = (row[2], row[3])
        flags[ref_id] = {
            "dream_acquire": bool(row[4]),
            "requested_by_quest": bool(row[5]),
            "discovered_via_cite": bool(row[6]),
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


def _run_rank(store: Store) -> int:
    """Step (c). Returns the number of refs whose ``prio`` was written."""
    stub_vectors, current, flags = _load_rank_candidates(store)
    if not stub_vectors:
        return 0

    anchors = _load_anchors(store)
    if not anchors:
        log.warning(
            "stub_rank rank: no anchors available (no active quests, no "
            "recently-opened papers) -- skipping this pass, writing nothing"
        )
        return 0

    new_prios = compute_stub_prios(stub_vectors, anchors, flags)
    updates = [
        (ref_id, new_prio)
        for ref_id, new_prio in new_prios.items()
        if _should_write_prio(current[ref_id][0], current[ref_id][1], new_prio)
    ]
    if not updates:
        return 0

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
    return len(updates)


# ── entry point ──────────────────────────────────────────────────────


def run_stub_rank_pass(
    store: Store,
    *,
    limit: int | None = None,
    embedder: Embedder | None = None,
    api_key: str | None = None,
    resolve_batch: ResolveBatchFn | None = None,
) -> dict[str, int]:
    """Run one stub_rank pass: enrich, embed, then re-rank every scored stub.

    ``limit`` bounds steps (a)/(b) (default :func:`_enrich_batch_size`,
    ``PRECIS_STUB_RANK_ENRICH_BATCH``); step (c) always recomputes over
    every pending stub with a vector (a single in-memory numpy pass, cheap
    even at corpus scale — see the module docstring). ``embedder=None``
    skips step (b) only (enrich + rank still run). ``api_key`` defaults to
    ``$SEMANTIC_SCHOLAR_API_KEY``; ``resolve_batch`` is the S2
    batch-resolve injection seam for tests (default
    :func:`~precis.ingest.semantic_scholar.get_papers_batch`).

    Returns the ``BatchResult`` shape ``{claimed, ok, failed}``:
    ``claimed`` sums every candidate touched across the three steps,
    ``ok`` sums the ones that landed a real write (resolved / embedded /
    re-ranked). Individual per-stub failures are logged and excluded from
    both counts rather than raising — a bad S2 id or a poison embed must
    not take the whole pass down.
    """
    if api_key is None:
        from precis.secrets import get_secret

        api_key = (get_secret("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
    resolve = resolve_batch or get_papers_batch
    batch_limit = limit if limit is not None else _enrich_batch_size()

    enrich_attempted, enrich_resolved = _run_enrich(
        store, limit=batch_limit, api_key=api_key, resolve_batch=resolve
    )
    embed_attempted, embed_ok = _run_embed(store, embedder=embedder, limit=batch_limit)
    ranked = _run_rank(store)

    return {
        "claimed": enrich_attempted + embed_attempted + ranked,
        "ok": enrich_resolved + embed_ok + ranked,
        "failed": 0,
    }


__all__ = [
    "ResolveBatchFn",
    "compute_stub_prios",
    "run_stub_rank_pass",
]
