"""paper_rank — deterministic five-signal reading-priority score per paper.

Answers "which paper in the corpus should I read *next*", independent of any
particular search query — a per-``kind='paper'`` ref score written to
``refs.meta['paper_rank']`` (:data:`PAPER_RANK_VERSION`, ``read_first`` 0-100,
higher = read sooner). This is a *reading-priority* score, NOT a claim-quality
judgment — review tiers (``nursery``/``structural``/``deep_review``) own
whether a paper's claims hold up; this pass only estimates whether a paper is
worth a reader's time at all, from cheap bibliometric + textual signals no
LLM call is needed for.

Five 0-100 signals, weighted mean over the ones **available** for a given
paper (weights renormalized to sum 1 — a paper missing a signal doesn't get
penalized to zero, its remaining signals just carry proportionally more
weight, mirroring feynman's ``combineSignals``):

* ``citation_impact`` (weight 0.2) — OpenAlex ``fwci`` corpus percent-rank,
  or (no ``fwci``) a log-citation-count fallback.
* ``graph_prestige`` (weight 0.2) — this corpus's own citation-graph
  PageRank (``referenced_works`` edges among papers that have an OpenAlex
  id), min-max normalized.
* ``citation_velocity`` (weight 0.1) — citations per year-since-publication,
  log-normalized against the corpus.
* ``methodology`` (weight 0.1) — a deterministic marker screen (rigor
  vocabulary + presence of an abstract/body) over the abstract + body-chunk
  text.
* ``reproducibility`` (weight 0.1) — open-access/PDF-availability + a
  code/artifact marker screen over the same text.

Ported from `companion-inc/feynman <https://github.com/companion-inc/feynman>`_
(MIT), ``src/rank/paper-rank.ts`` (``DEFAULT_SCORE_WEIGHTS``, ``scorePapers``,
``computePageRank``) — the *rubric* is ported, not the TypeScript. Two
deliberate deviations from feynman:

1. feynman's ``topicalRelevance`` (weight 0.3) is query-dependent (cosine
   against a live search query) and is dropped entirely here — precis
   search already supplies topicality at query time; this pass only scores
   the query-independent residue.
2. feynman's citation-impact signal uses a normalized-citation-count
   percentile; this port prefers OpenAlex's own ``fwci`` (field-weighted
   citation impact, already field-normalized) percent-rank when available,
   falling back to a log-citation-count normalization only when ``fwci`` is
   absent.

A retracted paper (OpenAlex ``is_retracted``) has its composite capped at
20.0 regardless of its other signals (``meta.paper_rank.retracted=true``
records why).

Batch/global shape, NOT a per-ref claim (see :mod:`precis.workers.ref_lease`
for that pattern elsewhere): every :func:`run_paper_rank_pass` tick loads the
whole ``kind='paper'`` corpus in one query, recomputes the citation-graph
PageRank and every corpus-wide normalizer fresh, then (re)writes only the
papers that actually need it — missing/stale ``version`` first, then a
changed marker *fingerprint* (``f"{body_chunk_count}:{len(abstract)}"`` —
the only thing that would change the expensive text-marker scan's result),
then (cheapest: no chunk fetch, reusing cached marker counts) a recomputed
composite that's drifted more than 0.5 from the stored ``read_first`` (corpus
drift as the rest of the corpus's normalizers shift under it) — capped at
``batch_size`` per tick. An untouched paper's ``meta.paper_rank`` block
(including its ``computed_at`` stamp) is left byte-for-byte alone, so a
converged tick is a true no-op. Writes are a merge-patch
(``meta = meta || …``) — never rebuilds the whole ``meta`` object, never
touches ``refs.prio`` (:mod:`precis.workers.stub_rank` owns that column).

Registered as the ``paper_rank`` :class:`~precis.workers.registry.
ServiceSpec` (default-OFF, ``PRECIS_PAPER_RANK`` — a corpus-wide backfill is
a deliberate, node-targeted batch, like ``bib_retag``); wired in
``cli/worker.py``'s ``_register``. :func:`top_ranked_papers` is the read-side
seam a reading-queue / quest-frontier consumer calls.

v2 hook: feynman also scores a sectioned NeurIPS-checklist rubric (worth up
to 32) inside its methodology signal — out of scope here, a marker-count
screen is the v1 approximation.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from psycopg.types.json import Jsonb

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

PAPER_RANK_VERSION = 1

#: PageRank hyperparameters — mirrors feynman ``computePageRank``: uniform
#: init, dangling nodes redistribute their rank uniformly across every node.
_DAMPING = 0.85
_PAGERANK_ITERATIONS = 60

#: A paper whose recomputed composite has drifted more than this many points
#: from its stored ``read_first`` gets rewritten (corpus normalizers moved
#: under it) even though its own version/fingerprint are unchanged.
_DRIFT_THRESHOLD = 0.5

#: Methodology/availability threshold — an abstract shorter than this reads
#: as "no real abstract" for the purposes of the methodology signal.
_ABSTRACT_MIN_CHARS = 80

#: feynman's marker vocabularies, ported verbatim (lowercased substring
#: match against abstract + body-chunk text).
_METHOD_MARKERS = (
    "ablation",
    "analysis",
    "baseline",
    "benchmark",
    "compare",
    "comparison",
    "dataset",
    "empirical",
    "evaluation",
    "experiment",
    "metric",
    "result",
    "validation",
)
_UNCERTAINTY_MARKERS = (
    "confidence interval",
    "error bar",
    "limitation",
    "limitations",
    "significance",
    "statistical",
    "variance",
)
_REPRO_MARKERS = (
    "artifact",
    "checkpoint",
    "code",
    "dataset",
    "github",
    "open source",
    "reproduce",
    "reproducibility",
    "repository",
)
#: Subset of REPRO markers that count as "there is a code/artifact pointer" —
#: the reproducibility signal's separate +30 code-marker bonus.
_CODE_MARKERS = ("github", "code", "repository", "open source")

#: OpenAlex ``oa_status`` values that count as open access for the
#: reproducibility signal's +30 open-access-or-pdf bonus.
_OPEN_OA_STATUSES = frozenset({"gold", "green", "hybrid", "diamond", "bronze"})

#: feynman's raw per-signal weights (``topicalRelevance``'s 0.3 dropped —
#: see the module docstring's deviation (1)); renormalized to sum 1 over
#: whichever signals are available for a given paper at combine time.
_WEIGHTS: dict[str, float] = {
    "citation_impact": 0.2,
    "graph_prestige": 0.2,
    "citation_velocity": 0.1,
    "methodology": 0.1,
    "reproducibility": 0.1,
}


# ── row shape + pure math ────────────────────────────────────────────────


@dataclass
class _PaperRow:
    """One ``kind='paper'`` ref's inputs, as loaded by :func:`_load_papers`."""

    year: int | None
    has_pdf: bool
    openalex_id: str | None
    fwci: float | None
    cited_by_count: int | None
    referenced_works: list[str]
    is_retracted: bool
    oa_status: str | None
    abstract: str | None
    cached_paper_rank: dict[str, Any] | None
    body_chunk_count: int


@dataclass
class _Normalizers:
    """Corpus-wide normalizers, recomputed fresh every tick."""

    max_log_count: float
    max_velocity_log: float
    min_pr: float | None
    max_pr: float | None
    fwci_pct: dict[int, float]
    velocity: dict[int, float] = field(default_factory=dict)


def _clamp_round(x: float) -> float:
    """Clamp to ``[0, 100]`` and round to 1 decimal — every signal's shape."""
    return round(max(0.0, min(100.0, x)), 1)


def _rank_pct(mapping: dict[int, float]) -> dict[int, float]:
    """``ref_id -> percent_rank`` (0 worst .. 1 best) of ``mapping``'s values.

    A single-entry map is its own best (1.0) by convention — mirrors
    ``stub_rank.compute_stub_percentiles``'s single-stub case.
    """
    if not mapping:
        return {}
    items = sorted(mapping.items(), key=lambda kv: kv[1])
    n = len(items)
    if n == 1:
        return {items[0][0]: 1.0}
    return {ref_id: i / (n - 1) for i, (ref_id, _val) in enumerate(items)}


def _compute_pagerank(
    nodes: list[int],
    edges: list[tuple[int, int]],
    *,
    damping: float = _DAMPING,
    iterations: int = _PAGERANK_ITERATIONS,
) -> dict[int, float]:
    """Power-iteration PageRank over an explicit node/edge list.

    ``edges`` are ``(citing_ref_id, cited_ref_id)`` pairs; rank flows
    citing -> cited (mirrors feynman ``computePageRank``: a paper is
    "prestigious" because other corpus papers cite it). Uniform init
    (``1/n``); a dangling node (no outgoing edges) redistributes its rank
    uniformly across every node each iteration, so total rank mass stays
    ~1 throughout (verified by the PageRank unit test). Self-citations
    (a paper listing itself) are dropped as a no-op edge.
    """
    n = len(nodes)
    if n == 0:
        return {}
    index = {node: i for i, node in enumerate(nodes)}
    out_targets: list[list[int]] = [[] for _ in range(n)]
    for src, dst in edges:
        if src not in index or dst not in index or src == dst:
            continue
        out_targets[index[src]].append(index[dst])
    out_degree = [len(t) for t in out_targets]

    pr = [1.0 / n] * n
    for _ in range(iterations):
        dangling_mass = sum(pr[j] for j in range(n) if out_degree[j] == 0)
        base = (1.0 - damping) / n + damping * dangling_mass / n
        new_pr = [base] * n
        for j in range(n):
            if out_degree[j] == 0:
                continue
            share = damping * pr[j] / out_degree[j]
            for i in out_targets[j]:
                new_pr[i] += share
        pr = new_pr
    return {node: pr[index[node]] for node in nodes}


def _build_citation_graph(
    rows: dict[int, _PaperRow],
) -> tuple[list[int], list[tuple[int, int]]]:
    """Nodes = every paper with an OpenAlex id; edges citing->cited for each
    ``referenced_works`` W-id that resolves to another corpus paper's
    OpenAlex id."""
    oa_to_ref: dict[str, int] = {
        row.openalex_id: ref_id for ref_id, row in rows.items() if row.openalex_id
    }
    nodes = list(oa_to_ref.values())
    edges: list[tuple[int, int]] = []
    for ref_id, row in rows.items():
        if not row.openalex_id:
            continue
        for w_id in row.referenced_works:
            cited_ref = oa_to_ref.get(w_id)
            if cited_ref is not None and cited_ref != ref_id:
                edges.append((ref_id, cited_ref))
    return nodes, edges


def _fingerprint(row: _PaperRow) -> str:
    """The marker-scan cache key — changes iff the inputs the text-marker
    scan reads (body-chunk count, abstract length) change."""
    return f"{row.body_chunk_count}:{len(row.abstract or '')}"


def _scan_markers(text: str) -> dict[str, Any]:
    """Marker-hit counts over ``text`` (already ``abstract + body`` joined).
    Pure substring match, lowercased — no NLP. Returns the raw counts (not
    yet turned into a 0-100 score); ``_score_paper`` does that using the row's
    own availability flags."""
    lowered = (text or "").lower()
    return {
        "method": sum(1 for m in _METHOD_MARKERS if m in lowered),
        "uncertainty": sum(1 for m in _UNCERTAINTY_MARKERS if m in lowered),
        "repro": sum(1 for m in _REPRO_MARKERS if m in lowered),
        "code": any(m in lowered for m in _CODE_MARKERS),
    }


def _compute_normalizers(
    rows: dict[int, _PaperRow], pagerank: dict[int, float], *, current_year: int
) -> _Normalizers:
    """Corpus-wide normalizers over every loaded paper (not just the ones
    selected for a write this tick — every paper's availability/score reads
    off these)."""
    fwci_map = {rid: r.fwci for rid, r in rows.items() if r.fwci is not None}
    fwci_pct = _rank_pct(fwci_map)

    counts = [r.cited_by_count for r in rows.values() if r.cited_by_count is not None]
    max_log_count = max([1.0, *(math.log1p(c) for c in counts)])

    velocity: dict[int, float] = {}
    for rid, r in rows.items():
        if r.year is not None and r.cited_by_count is not None:
            velocity[rid] = r.cited_by_count / max(1, current_year - r.year + 1)
    max_velocity_log = max([1.0, *(math.log1p(v) for v in velocity.values())])

    if pagerank:
        pr_values = list(pagerank.values())
        min_pr: float | None = min(pr_values)
        max_pr: float | None = max(pr_values)
    else:
        min_pr = max_pr = None

    return _Normalizers(
        max_log_count=max_log_count,
        max_velocity_log=max_velocity_log,
        min_pr=min_pr,
        max_pr=max_pr,
        fwci_pct=fwci_pct,
        velocity=velocity,
    )


def _score_paper(
    ref_id: int,
    row: _PaperRow,
    norm: _Normalizers,
    pagerank: dict[int, float],
    markers: dict[str, Any],
) -> tuple[float, dict[str, float | None], list[str], bool]:
    """Pure scoring math for one paper: ``(read_first, components,
    unavailable, retracted)``. ``markers`` is either a fresh
    :func:`_scan_markers` result or a reused cached one — the caller decides
    which (see :func:`run_paper_rank_pass`'s fingerprint/rescan logic)."""
    components: dict[str, float | None] = {}
    unavailable: list[str] = []

    if row.fwci is not None:
        components["citation_impact"] = _clamp_round(
            norm.fwci_pct.get(ref_id, 0.0) * 100
        )
    elif row.cited_by_count is not None:
        components["citation_impact"] = _clamp_round(
            math.log1p(row.cited_by_count) / norm.max_log_count * 100
        )
    else:
        components["citation_impact"] = None
        unavailable.append("citation_impact")

    pr = pagerank.get(ref_id)
    if pr is not None and norm.min_pr is not None and norm.max_pr is not None:
        span = norm.max_pr - norm.min_pr
        components["graph_prestige"] = _clamp_round(
            (pr - norm.min_pr) / (span or 1) * 100
        )
    else:
        components["graph_prestige"] = None
        unavailable.append("graph_prestige")

    velocity = norm.velocity.get(ref_id)
    if velocity is not None:
        components["citation_velocity"] = _clamp_round(
            math.log1p(velocity) / norm.max_velocity_log * 100
        )
    else:
        components["citation_velocity"] = None
        unavailable.append("citation_velocity")

    has_abstract = len(row.abstract or "") >= _ABSTRACT_MIN_CHARS
    has_body = row.body_chunk_count > 0
    if has_abstract or has_body:
        method_hits = int(markers.get("method", 0))
        uncertainty_hits = int(markers.get("uncertainty", 0))
        raw = (
            method_hits * 7
            + uncertainty_hits * 6
            + (14 if has_abstract else 0)
            + (10 if has_body else 0)
        )
        components["methodology"] = _clamp_round(float(raw))
    else:
        components["methodology"] = None
        unavailable.append("methodology")

    # Reproducibility is always available — even a paper with no abstract/body
    # gets a (likely low) score from open-access/PDF status alone.
    open_access = (row.oa_status in _OPEN_OA_STATUSES) or row.has_pdf
    code_marker = bool(markers.get("code"))
    repro_hits = int(markers.get("repro", 0))
    repro_raw = (
        (30 if open_access else 0)
        + (30 if code_marker else 0)
        + min(repro_hits * 7, 25)
    )
    components["reproducibility"] = _clamp_round(float(repro_raw))

    available: list[tuple[float, float]] = [
        (_WEIGHTS[k], v) for k, v in components.items() if v is not None
    ]
    available_weight = sum(w for w, _v in available)
    if available_weight > 0:
        composite = sum(w * v for w, v in available) / available_weight
    else:
        composite = 0.0
    composite = _clamp_round(composite)

    retracted = bool(row.is_retracted)
    if retracted:
        composite = min(composite, 20.0)

    return composite, components, unavailable, retracted


# ── DB: load + fetch + write ─────────────────────────────────────────────


def _load_papers(store: Store) -> dict[int, _PaperRow]:
    """One query: every non-deleted ``kind='paper'`` ref's scoring inputs,
    plus its cached prior ``paper_rank`` block and its live body-chunk count
    (``ord >= 0 AND retired_at IS NULL``, the append-only body-chunk
    convention)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.year, r.pdf_sha256 IS NOT NULL AS has_pdf,
                   r.meta->'openalex'->>'id' AS openalex_id,
                   (r.meta->'openalex'->>'fwci')::float AS fwci,
                   (r.meta->'openalex'->>'cited_by_count')::int AS cited_by_count,
                   r.meta->'openalex'->'referenced_works' AS referenced_works,
                   (r.meta->'openalex'->>'is_retracted')::bool AS is_retracted,
                   r.meta->'openalex'->>'oa_status' AS oa_status,
                   r.meta->>'abstract' AS abstract,
                   r.meta->'paper_rank' AS paper_rank,
                   COALESCE(bc.n, 0) AS body_chunk_count
              FROM refs r
              LEFT JOIN LATERAL (
                     SELECT count(*) AS n FROM chunks c
                      WHERE c.ref_id = r.ref_id AND c.ord >= 0 AND c.retired_at IS NULL
                   ) bc ON true
             WHERE r.kind = 'paper' AND r.deleted_at IS NULL
            """
        ).fetchall()

    out: dict[int, _PaperRow] = {}
    for r in rows:
        ref_id = int(r[0])
        referenced_works = r[6] if isinstance(r[6], list) else []
        out[ref_id] = _PaperRow(
            year=r[1],
            has_pdf=bool(r[2]),
            openalex_id=r[3],
            fwci=r[4],
            cited_by_count=r[5],
            referenced_works=[str(w) for w in referenced_works],
            is_retracted=bool(r[7]),
            oa_status=r[8],
            abstract=r[9],
            cached_paper_rank=r[10] if isinstance(r[10], dict) else None,
            body_chunk_count=int(r[11]),
        )
    return out


def _fetch_body_text(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """One query: ``ref_id -> concatenated body-chunk text`` for every id in
    ``ref_ids`` — the only chunk fetch this pass ever does, and only for
    papers actually being (re)scanned this tick. A separate, monkeypatchable
    function so tests can assert the marker-cache short-circuit by call
    count."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, string_agg(text, ' ' ORDER BY ord) FROM chunks "
            "WHERE ref_id = ANY(%s) AND ord >= 0 AND retired_at IS NULL "
            "GROUP BY ref_id",
            (ref_ids,),
        ).fetchall()
    return {int(r[0]): (r[1] or "") for r in rows}


def top_ranked_papers(store: Store, *, limit: int = 20) -> list[dict[str, Any]]:
    """The seam a reading-queue / quest-frontier consumer calls: the
    ``limit`` highest-``read_first`` papers that carry a ``paper_rank``
    block, best first. Deliberately dumb — no filtering beyond "has been
    scored"; a caller wanting topical narrowing layers search on top."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, (meta->'paper_rank'->>'read_first')::float "
            "AS read_first FROM refs "
            "WHERE kind = 'paper' AND deleted_at IS NULL AND meta ? 'paper_rank' "
            "ORDER BY read_first DESC, ref_id LIMIT %s",
            (limit,),
        ).fetchall()
    return [{"ref_id": int(r[0]), "title": r[1], "read_first": r[2]} for r in rows]


# ── the pass ──────────────────────────────────────────────────────────────


def run_paper_rank_pass(store: Store, *, batch_size: int = 200) -> dict[str, int]:
    """One global tick: load the whole ``kind='paper'`` corpus, recompute the
    citation-graph PageRank + every normalizer fresh, then (re)write up to
    ``batch_size`` papers — missing/stale ``version`` first, then a changed
    marker fingerprint, then (cheapest, no chunk fetch) a corpus-drifted
    composite. Returns ``{"claimed": N, "ok": N, "failed": N}`` like other
    passes."""
    rows = _load_papers(store)
    if not rows:
        return {"claimed": 0, "ok": 0, "failed": 0}

    nodes, edges = _build_citation_graph(rows)
    # Zero edges -> graph_prestige is unavailable for everyone (see module
    # docstring); skip the (otherwise harmless) power-iteration entirely.
    pagerank = _compute_pagerank(nodes, edges) if edges else {}
    current_year = datetime.now(UTC).year
    norm = _compute_normalizers(rows, pagerank, current_year=current_year)

    stale: list[int] = []
    fingerprint_changed: list[int] = []
    drifted: list[int] = []
    for ref_id, row in rows.items():
        cached = row.cached_paper_rank
        fp = _fingerprint(row)
        if cached is None or cached.get("version") != PAPER_RANK_VERSION:
            stale.append(ref_id)
            continue
        cached_markers = cached.get("markers") or {}
        if cached_markers.get("fingerprint") != fp:
            fingerprint_changed.append(ref_id)
            continue
        # Cheapest check: reuse the cached marker counts (no chunk fetch) to
        # see whether the corpus-wide normalizers have moved this paper's
        # composite enough to be worth a rewrite.
        composite, _components, _unavailable, _retracted = _score_paper(
            ref_id, row, norm, pagerank, cached_markers
        )
        stored = cached.get("read_first")
        if (
            not isinstance(stored, (int, float))
            or abs(composite - stored) > _DRIFT_THRESHOLD
        ):
            drifted.append(ref_id)

    selected = (stale + fingerprint_changed + drifted)[:batch_size]
    if not selected:
        return {"claimed": 0, "ok": 0, "failed": 0}

    needs_rescan = set(stale) | set(fingerprint_changed)
    rescan_ids = [rid for rid in selected if rid in needs_rescan]
    body_text_by_ref = _fetch_body_text(store, rescan_ids)

    ok = failed = 0
    now = datetime.now(UTC)
    for ref_id in selected:
        try:
            row = rows[ref_id]
            if ref_id in needs_rescan:
                text = f"{row.abstract or ''}\n{body_text_by_ref.get(ref_id, '')}"
                markers = _scan_markers(text)
            else:
                markers = (row.cached_paper_rank or {}).get("markers") or {}
            composite, components, unavailable, retracted = _score_paper(
                ref_id, row, norm, pagerank, markers
            )
            block: dict[str, Any] = {
                "version": PAPER_RANK_VERSION,
                "read_first": composite,
                "components": components,
                "unavailable": unavailable,
                "markers": {**markers, "fingerprint": _fingerprint(row)},
                "retracted": retracted,
                "computed_at": now.isoformat(),
            }
            with store.pool.connection() as conn:
                conn.execute(
                    "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                    (Jsonb({"paper_rank": block}), ref_id),
                )
            ok += 1
        except Exception:
            log.exception("paper_rank: failed ref_id=%s", ref_id)
            failed += 1

    return {"claimed": len(selected), "ok": ok, "failed": failed}


__all__ = [
    "PAPER_RANK_VERSION",
    "run_paper_rank_pass",
    "top_ranked_papers",
]
