"""find-citing-papers — sweep Semantic Scholar for new papers citing the precis corpus.

For each paper in precis with a DOI or arXiv id, fetch the citation
list via the S2 graph API, filter by date window and relevance, dedup
against the corpus, and write a markdown report plus a JSONL feed.

Per-paper results are cached as JSON under
``$PRECIS_CITING_CACHE_DIR`` (default
``paper-ingest/.citing-papers-cache/``) so the sweep is resumable —
re-runs reuse cache files unless ``--force`` is passed. A full sweep
across the ~4k corpus returns ~900k unique citing papers; the noise-
reduction flags below are how you turn that into a digestible report.

Usage (via the ``find-citing-papers`` shell wrapper):

  Sweep + cache control
    find-citing-papers                              # last 180 days, relevance gate on
    find-citing-papers --since 2026-02-01           # explicit window start
    find-citing-papers --until 2026-07-31           # explicit window end
    find-citing-papers --limit 100                  # only the N most-recent source papers
    find-citing-papers --slug-prefix abazari        # filter source corpus by slug prefix
    find-citing-papers --force                      # ignore cache, re-fetch every paper
    find-citing-papers --no-fetch                   # aggregate from existing cache only
    find-citing-papers --out citing.md              # report path (default: timestamped)

  Noise-reduction filters (stack freely)
    --influential-only                              # S2 isInfluential=True only (~3.5%)
    --keep-background                               # keep background-only intents
    --min-co-cites N                                # citing paper must cite ≥N of ours
                                                    # (909k → 212k @ 2; → 25k @ 5)
    --min-citing-citations N                        # drop low-traction citing papers
    --min-similarity X                              # bge-m3 cosine gate vs source text
                                                    # (typical usable threshold: 0.50-0.65)
    --top-n N                                       # hard cap after sort
    --per-source-top K                              # alt mode: top K citations per OUR paper

  Recommended starter digest:
    find-citing-papers --no-fetch \\
        --since 2026-01-01 --min-co-cites 3 \\
        --min-citing-citations 1 --min-similarity 0.55 \\
        --top-n 200

Sort precedence in global mode: co-citations DESC, similarity DESC
(when computed), publication date DESC, title.

Reads ``SEMANTIC_SCHOLAR_API_KEY`` from the environment (raises S2's
free-tier rate limit). Without it the script still runs but is slower
and more likely to hit 429.

See ``scripts/README.md`` for tuning guidance and
``paper-ingest/README.md`` for the cache + report layout.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

# Make `_common` importable regardless of CWD.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _common import open_store

CACHE_DIR_ENV = "PRECIS_CITING_CACHE_DIR"
DEFAULT_CACHE_DIR = Path(
    "/Users/bots/Documents/openclaw-cluster/paper-ingest/.citing-papers-cache"
)
DEFAULT_OUT_DIR = Path("/Users/bots/Documents/openclaw-cluster/paper-ingest")

# Fields the S2 graph API will return on each citation row. The
# top-level `intents`/`isInfluential` come from the citation edge;
# the rest are properties of the citing paper.
#
# `tldr` is a valid Paper field but the `/paper/{id}/citations`
# endpoint rejects it — keep it out of this list. We surface
# `abstract` instead (which the citations endpoint does accept) and
# render its preview in the report.
S2_FIELDS = [
    "intents",
    "isInfluential",
    "paperId",
    "externalIds",
    "title",
    "abstract",
    "year",
    "publicationDate",
    "venue",
    "citationCount",
    "influentialCitationCount",
    "authors",
]

# Soft delay between S2 calls to stay polite even with an API key.
INTER_CALL_SLEEP = 0.4


# ---------------------------------------------------------------------------
# Source corpus enumeration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourcePaper:
    """A paper in our precis corpus we'll look up citations for."""

    slug: str
    title: str
    year: int | None
    doi: str | None
    arxiv_id: str | None
    s2_id: str | None  # cached S2 paperId from ingest, when available

    @property
    def s2_lookup_id(self) -> str | None:
        """Pick the strongest identifier S2 will accept."""
        if self.s2_id:
            return self.s2_id
        if self.doi:
            return f"DOI:{self.doi}"
        if self.arxiv_id:
            return f"ARXIV:{self.arxiv_id}"
        return None


def _load_corpus(
    *, slug_prefix: str | None, limit: int | None
) -> tuple[list[SourcePaper], set[str]]:
    """Pull source papers from precis along with the full DOI dedup set.

    Returns ``(sources, known_dois)`` — ``sources`` is the list to look
    up, ``known_dois`` is every DOI already in the corpus, lowercased,
    used to drop citing papers we already have.
    """
    store, _cfg = open_store()
    try:
        with store.pool.connection() as conn:
            # Full DOI corpus for dedup. Lowercased for case-insensitive
            # matching against S2 responses.
            rows = conn.execute(
                "SELECT lower(meta->>'doi') FROM refs "
                "WHERE kind = 'paper' AND deleted_at IS NULL "
                "AND meta->>'doi' IS NOT NULL AND meta->>'doi' <> ''"
            ).fetchall()
            known_dois = {r[0] for r in rows if r[0]}

            sql = (
                "SELECT slug, title, "
                "(meta->>'year')::int AS yr, "
                "meta->>'doi', meta->>'arxiv_id', meta->>'s2_id' "
                "FROM refs "
                "WHERE kind = 'paper' AND deleted_at IS NULL "
                "AND ("
                "  (meta->>'doi'      IS NOT NULL AND meta->>'doi'      <> '') "
                "  OR (meta->>'arxiv_id' IS NOT NULL AND meta->>'arxiv_id' <> '') "
                "  OR (meta->>'s2_id'    IS NOT NULL AND meta->>'s2_id'    <> '') "
                ")"
            )
            params: list[Any] = []
            if slug_prefix:
                sql += " AND slug LIKE %s"
                params.append(f"{slug_prefix}%")
            sql += " ORDER BY created_at DESC NULLS LAST"
            if limit:
                sql += " LIMIT %s"
                params.append(limit)

            rows = conn.execute(sql, tuple(params)).fetchall()
    finally:
        store.close()

    out: list[SourcePaper] = []
    for slug, title, year, doi, arxiv_id, s2_id in rows:
        out.append(
            SourcePaper(
                slug=slug,
                title=title or "",
                year=year,
                doi=(doi or None),
                arxiv_id=(arxiv_id or None),
                s2_id=(s2_id or None),
            )
        )
    return out, known_dois


# ---------------------------------------------------------------------------
# S2 citation fetch
# ---------------------------------------------------------------------------


def _serialize_citation(cit: Any) -> dict[str, Any]:
    """Project an S2 ``Citation`` object into a stable JSON-friendly dict."""
    p = cit.paper
    pub_date = p.publicationDate
    pub_date_str = pub_date.strftime("%Y-%m-%d") if pub_date else None

    tldr = None
    if p.tldr is not None:
        tldr = getattr(p.tldr, "text", None)

    authors_raw = p.authors or []
    authors: list[str] = []
    for a in authors_raw[:10]:
        # ``a`` is a semanticscholar.Author; fall back to dict shape.
        name = getattr(a, "name", None)
        if name is None and isinstance(a, dict):
            name = a.get("name")
        if name:
            authors.append(name)

    ext = p.externalIds or {}
    return {
        "isInfluential": bool(cit.isInfluential),
        "intents": list(cit.intents or []),
        "paperId": p.paperId,
        "doi": ext.get("DOI"),
        "arxivId": ext.get("ArXiv"),
        "title": p.title or "",
        "abstract": p.abstract or "",
        "tldr": tldr or "",
        "year": p.year,
        "publicationDate": pub_date_str,
        "venue": p.venue or "",
        "citationCount": p.citationCount,
        "influentialCitationCount": p.influentialCitationCount,
        "authors": authors,
    }


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------
#
# Two layers of retry stack here:
#
#   * The ``semanticscholar`` library wraps each underlying HTTP request
#     with tenacity ``wait_exponential(min=5, max=60)`` × 10 attempts,
#     but ONLY on ``ConnectionRefusedError`` (which it raises for HTTP
#     429). 5xx and network errors bubble straight up.
#
#   * This wrapper covers the gap: on top of the lib's 429 retry, we
#     retry the whole paper fetch on transient 5xx + network failures.
#     We do NOT retry on 404 (paper not in S2) or BadQueryParameters
#     (programmer error) — those are terminal and are cached as
#     sentinel rows so the next sweep skips them.


# Exception classes that warrant a retry. Imported lazily inside the
# decorator builder so this module can be imported without installing
# httpx in environments that just want --help.
def _build_retry_decorator() -> Any:
    import httpx
    from semanticscholar.SemanticScholarException import (
        GatewayTimeoutException,
        InternalServerErrorException,
        ServerErrorException,
    )
    from tenacity import (
        retry,
        retry_if_exception_type,
        stop_after_attempt,
        wait_exponential,
    )

    transient = (
        ServerErrorException,
        InternalServerErrorException,
        GatewayTimeoutException,
        ConnectionRefusedError,  # backstop in case lib's 429 retry exhausted
        httpx.ReadError,
        httpx.ConnectError,
        httpx.TimeoutException,
        httpx.RemoteProtocolError,
    )
    return retry(
        wait=wait_exponential(min=5, max=120),
        stop=stop_after_attempt(5),
        retry=retry_if_exception_type(transient),
        reraise=True,
    )


def _do_fetch(sch: Any, sid: str, *, page_limit: int) -> list[dict[str, Any]]:
    """The actual S2 round-trip — wrapped by ``_fetch_one`` with retry."""
    paginated = sch.get_paper_citations(sid, fields=S2_FIELDS, limit=page_limit)
    out: list[dict[str, Any]] = []
    for c in paginated:
        out.append(_serialize_citation(c))
    return out


# Cache the decorated callable so we don't rebuild it for every paper.
_DO_FETCH_WITH_RETRY: Any = None


def _get_do_fetch_with_retry() -> Any:
    global _DO_FETCH_WITH_RETRY
    if _DO_FETCH_WITH_RETRY is None:
        _DO_FETCH_WITH_RETRY = _build_retry_decorator()(_do_fetch)
    return _DO_FETCH_WITH_RETRY


def _fetch_one(sch: Any, src: SourcePaper, *, page_limit: int) -> dict[str, Any] | None:
    """Fetch the citation list for a single source paper.

    On terminal failure (paper not found in S2, transient errors that
    survived all retries) returns a sentinel blob with ``error`` set
    so the caller can cache it and move on.

    The S2 lib's built-in tenacity wrapper handles 429s. This function
    layers a second tenacity retry over top of the whole fetch for
    5xx + network errors — see ``_build_retry_decorator``.
    """
    sid = src.s2_lookup_id
    if sid is None:  # belt-and-braces — _load_corpus filters these out
        return None

    base = {
        "slug": src.slug,
        "doi": src.doi,
        "arxiv_id": src.arxiv_id,
        "s2_lookup_id": sid,
    }

    try:
        citations = _get_do_fetch_with_retry()(sch, sid, page_limit=page_limit)
    except Exception as e:
        return {
            **base,
            "fetched_at": datetime.now(UTC).isoformat(),
            "error": f"{type(e).__name__}: {e}"[:500],
            "citations": [],
        }

    return {
        **base,
        "fetched_at": datetime.now(UTC).isoformat(),
        "citations": citations,
    }


# ---------------------------------------------------------------------------
# Filtering + aggregation
# ---------------------------------------------------------------------------


def _passes_date(c: dict[str, Any], since: str | None, until: str | None) -> bool:
    """Date-window filter using publicationDate first, falling back to year."""
    pd = c.get("publicationDate")
    if pd:
        if since and pd < since:
            return False
        if until and pd > until:
            return False
        return True
    # No publicationDate. Fall back to year. Be lenient at the boundaries.
    yr = c.get("year")
    if yr is None:
        return False
    if since and yr < int(since[:4]):
        return False
    if until and yr > int(until[:4]):
        return False
    return True


def _passes_relevance(
    c: dict[str, Any],
    *,
    influential_only: bool,
    keep_background: bool,
) -> bool:
    """Relevance filter on isInfluential + intents."""
    if influential_only:
        return bool(c.get("isInfluential"))
    if c.get("isInfluential"):
        return True
    intents = set(c.get("intents") or [])
    if keep_background:
        return True  # any citation with any intent counts
    # Drop pure-background mentions.
    if intents & {"methodology", "result"}:
        return True
    if not intents:
        # No intent classification at all — keep, since S2 doesn't always
        # populate this. Background-only is typically tagged explicitly.
        return True
    return False


@dataclass
class AggregatedHit:
    """A unique citing paper aggregated across multiple source papers."""

    key: str  # DOI lowercased if present, else paperId
    doi: str | None
    paper_id: str | None
    title: str
    year: int | None
    publication_date: str | None
    venue: str
    tldr: str
    abstract: str
    authors: list[str]
    citation_count: int | None
    cited_sources: set[str] = field(default_factory=set)
    influential_for: set[str] = field(default_factory=set)
    intents: set[str] = field(default_factory=set)
    similarity: float | None = None  # bge-m3 cosine, max across cited sources


def _hit_key(c: dict[str, Any]) -> str | None:
    if c.get("doi"):
        return f"doi:{c['doi'].lower()}"
    if c.get("paperId"):
        return f"s2:{c['paperId']}"
    return None  # nothing to dedup on


def _aggregate(
    cache_files: list[Path],
    *,
    since: str | None,
    until: str | None,
    influential_only: bool,
    keep_background: bool,
    known_dois: set[str],
    min_citing_citations: int = 0,
    min_co_cites: int = 1,
) -> tuple[dict[str, AggregatedHit], int, int]:
    """Walk cache files, apply filters, dedup, return ``(hits, scanned, kept)``.

    Filter ordering matters for cost. Per-row filters (date, relevance,
    citing-paper own citation count) run inside the scan loop and discard
    rows before they hit the dedup dict. The aggregate-level filter
    (``min_co_cites``) can only be applied AFTER the full scan because we
    don't know the final co-citation count for any citing paper until we've
    walked every cache file.
    """
    hits: dict[str, AggregatedHit] = {}
    scanned = 0
    kept = 0

    for path in cache_files:
        try:
            blob = json.loads(path.read_text())
        except Exception:
            continue

        src_slug = blob.get("slug") or path.stem
        for c in blob.get("citations", []):
            scanned += 1
            doi_lc = (c.get("doi") or "").lower()
            if doi_lc and doi_lc in known_dois:
                continue  # already in our corpus
            if not _passes_date(c, since, until):
                continue
            if not _passes_relevance(
                c, influential_only=influential_only, keep_background=keep_background
            ):
                continue
            cc = c.get("citationCount")
            if min_citing_citations > 0 and (cc is None or cc < min_citing_citations):
                continue

            key = _hit_key(c)
            if key is None:
                continue

            agg = hits.get(key)
            if agg is None:
                agg = AggregatedHit(
                    key=key,
                    doi=c.get("doi"),
                    paper_id=c.get("paperId"),
                    title=c.get("title", ""),
                    year=c.get("year"),
                    publication_date=c.get("publicationDate"),
                    venue=c.get("venue", ""),
                    tldr=c.get("tldr", ""),
                    abstract=c.get("abstract", ""),
                    authors=list(c.get("authors") or []),
                    citation_count=c.get("citationCount"),
                )
                hits[key] = agg
            agg.cited_sources.add(src_slug)
            if c.get("isInfluential"):
                agg.influential_for.add(src_slug)
            for intent in c.get("intents") or []:
                agg.intents.add(intent)
            kept += 1

    if min_co_cites > 1:
        hits = {k: h for k, h in hits.items() if len(h.cited_sources) >= min_co_cites}

    return hits, scanned, kept


# ---------------------------------------------------------------------------
# bge-m3 similarity rerank
# ---------------------------------------------------------------------------
#
# Optional gate: drop citing papers whose abstract+title doesn't look
# topically similar to ANY of the source papers it cites. Computes
# cosine similarity using the same bge-m3 model that precis already
# loads for its own embeddings — vectors are L2-normalized so cosine
# == dot product.


def _load_source_paper_text(slugs: set[str]) -> dict[str, str]:
    """Pull title + abstract for each source paper from precis.

    Returns ``{slug: "title abstract"}`` ready for embedding. Slugs
    with neither title nor abstract are omitted (we can't score
    against them).
    """
    if not slugs:
        return {}
    store, _cfg = open_store()
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT slug, title, meta->>'abstract' "
                "FROM refs WHERE kind='paper' AND deleted_at IS NULL "
                "AND slug = ANY(%s)",
                (list(slugs),),
            ).fetchall()
    finally:
        store.close()

    out: dict[str, str] = {}
    for slug, title, abstract in rows:
        text = " ".join(p for p in (title, abstract) if p).strip()
        if text:
            out[slug] = text
    return out


def _dot(a: list[float], b: list[float]) -> float:
    """Cosine similarity for normalized vectors == dot product."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def _apply_similarity_gate(
    hits: dict[str, AggregatedHit],
    *,
    min_similarity: float,
) -> dict[str, AggregatedHit]:
    """Embed surviving citing papers + their cited sources, gate by cosine.

    Each surviving citing paper gets a ``similarity`` score equal to
    the *maximum* cosine across the source papers it cites. The gate
    drops anything below ``min_similarity``. Source-paper texts come
    from precis (title + abstract); citing-paper texts come from the
    cache (title + abstract from S2).
    """
    if not hits or min_similarity <= 0:
        return hits

    # Lazy imports — bge-m3 pulls in sentence-transformers + torch
    # which take ~7s to load. Worth deferring until we actually need it.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from precis.embedder import make_embedder

    # Collect source slugs we need text for. Drop anything we can't score.
    needed_slugs: set[str] = set()
    for h in hits.values():
        needed_slugs.update(h.cited_sources)

    print(f"[bge-m3] loading source texts for {len(needed_slugs)} papers...")
    src_texts = _load_source_paper_text(needed_slugs)
    missing_src = needed_slugs - set(src_texts.keys())
    if missing_src:
        print(
            f"[bge-m3] {len(missing_src)} source papers have no title/abstract — "
            f"citing papers that ONLY cite those will fall through ungated"
        )

    print(f"[bge-m3] loading model + embedding {len(src_texts)} source papers...")
    embedder = make_embedder("bge-m3")
    src_keys = list(src_texts.keys())
    src_vecs_list = embedder.embed([src_texts[k] for k in src_keys])
    src_vecs = dict(zip(src_keys, src_vecs_list, strict=True))

    # Build embedding queue for citing papers. Skip ones with empty text —
    # they'll fall through ungated (similarity=None).
    cit_keys: list[str] = []
    cit_texts: list[str] = []
    for k, h in hits.items():
        text = " ".join(p for p in (h.title, h.abstract) if p).strip()
        if text:
            cit_keys.append(k)
            cit_texts.append(text)

    print(
        f"[bge-m3] embedding {len(cit_texts)} citing papers (this is the slow bit)..."
    )
    t0 = time.monotonic()
    cit_vecs_list = embedder.embed(cit_texts)
    elapsed = time.monotonic() - t0
    print(
        f"[bge-m3] embedded {len(cit_texts)} in {elapsed:.1f}s "
        f"({len(cit_texts) / elapsed:.0f}/s)"
    )
    cit_vecs = dict(zip(cit_keys, cit_vecs_list, strict=True))

    # Score + gate.
    out: dict[str, AggregatedHit] = {}
    n_no_text = 0
    n_no_src_text = 0
    n_dropped = 0
    for k, h in hits.items():
        cv = cit_vecs.get(k)
        if cv is None:
            # No citing-paper text. Pass through ungated — better to
            # show ungated than silently drop, since the user may have
            # other filters on.
            n_no_text += 1
            out[k] = h
            continue
        scores = [_dot(cv, src_vecs[s]) for s in h.cited_sources if s in src_vecs]
        if not scores:
            n_no_src_text += 1
            out[k] = h  # all cited sources lacked text; pass through
            continue
        max_sim = max(scores)
        h.similarity = max_sim
        if max_sim >= min_similarity:
            out[k] = h
        else:
            n_dropped += 1

    print(
        f"[bge-m3] kept {len(out)}/{len(hits)} "
        f"(threshold={min_similarity}, dropped={n_dropped}, "
        f"ungated_no_text={n_no_text + n_no_src_text})"
    )
    return out


# ---------------------------------------------------------------------------
# Per-source top-K mode
# ---------------------------------------------------------------------------
#
# Alternative output: instead of one global ranking, group citing papers
# by which source paper they cite, and surface the top K most-recent /
# most-influential citations per source. Useful for "what's new for paper
# X" digests rather than corpus-wide topic surveys.


def _aggregate_per_source(
    cache_files: list[Path],
    *,
    since: str | None,
    until: str | None,
    influential_only: bool,
    keep_background: bool,
    known_dois: set[str],
    min_citing_citations: int,
    top_k: int,
) -> tuple[dict[str, list[dict[str, Any]]], int, int]:
    """Build ``{source_slug: [top_k citations]}`` ranked per source.

    Within each source paper, citations are sorted by:
      1. ``isInfluential`` (True first)
      2. publication date (DESC)
      3. citing paper's own citation count (DESC)
    """
    by_source: dict[str, list[dict[str, Any]]] = {}
    scanned = 0
    kept = 0

    for path in cache_files:
        try:
            blob = json.loads(path.read_text())
        except Exception:
            continue
        src_slug = blob.get("slug") or path.stem
        bucket: list[dict[str, Any]] = []

        for c in blob.get("citations", []):
            scanned += 1
            doi_lc = (c.get("doi") or "").lower()
            if doi_lc and doi_lc in known_dois:
                continue
            if not _passes_date(c, since, until):
                continue
            if not _passes_relevance(
                c, influential_only=influential_only, keep_background=keep_background
            ):
                continue
            cc = c.get("citationCount")
            if min_citing_citations > 0 and (cc is None or cc < min_citing_citations):
                continue
            bucket.append(c)
            kept += 1

        if not bucket:
            continue
        bucket.sort(
            key=lambda c: (
                0 if c.get("isInfluential") else 1,
                -_date_to_int(
                    c.get("publicationDate")
                    or (f"{c.get('year')}-01-01" if c.get("year") else "")
                ),
                -(c.get("citationCount") or 0),
            )
        )
        by_source[src_slug] = bucket[:top_k]

    return by_source, scanned, kept


def _write_per_source_markdown(
    by_source: dict[str, list[dict[str, Any]]],
    *,
    out: Path,
    since: str | None,
    until: str | None,
    n_sources: int,
    n_scanned: int,
    n_kept: int,
    influential_only: bool,
    keep_background: bool,
    min_citing_citations: int,
    top_k: int,
) -> None:
    lines: list[str] = []
    lines.append("# Latest citations per source paper")
    lines.append("")
    lines.append(f"- Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"- Source papers with hits: {len(by_source)}/{n_sources}")
    lines.append(f"- Citations scanned: {n_scanned}, kept: {n_kept}")
    lines.append(f"- Date window: {since or '*'} → {until or '*'}")
    lines.append(
        f"- Filters: influential_only={influential_only}, "
        f"keep_background={keep_background}, "
        f"min_citing_citations={min_citing_citations}, top_k={top_k}"
    )
    lines.append("")

    sources_sorted = sorted(by_source.items(), key=lambda kv: -len(kv[1]))
    for slug, cites in sources_sorted:
        lines.append(f"## `{slug}` ({len(cites)} hit{'s' if len(cites) != 1 else ''})")
        lines.append("")
        for c in cites:
            title = c.get("title") or "(untitled)"
            mark = " ★" if c.get("isInfluential") else ""
            lines.append(f"- **{title}**{mark}")
            meta_bits: list[str] = []
            pd = c.get("publicationDate") or (
                str(c.get("year")) if c.get("year") else ""
            )
            if pd:
                meta_bits.append(pd)
            if c.get("venue"):
                meta_bits.append(c["venue"])
            if c.get("citationCount") is not None:
                meta_bits.append(f"{c['citationCount']} cites")
            if c.get("intents"):
                meta_bits.append("/".join(c["intents"]))
            if meta_bits:
                lines.append(f"  *{' · '.join(meta_bits)}*")
            if c.get("doi"):
                lines.append(f"  DOI: <https://doi.org/{c['doi']}>")
            elif c.get("paperId"):
                lines.append(
                    f"  S2: <https://www.semanticscholar.org/paper/{c['paperId']}>"
                )
        lines.append("")

    out.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Output writers
# ---------------------------------------------------------------------------


def _sort_hits(hits: dict[str, AggregatedHit]) -> list[AggregatedHit]:
    """Best-first ordering for the report.

    Sort precedence:
    1. Co-citation count (DESC) — papers citing many of ours go first.
    2. Similarity score (DESC) — ties broken by topical alignment.
    3. Publication date (DESC) — most recent within ties.
    4. Title — deterministic final tiebreak.
    """

    def key(h: AggregatedHit) -> tuple[int, float, int, str]:
        pd = h.publication_date or (f"{h.year}-01-01" if h.year else "0000-00-00")
        sim = h.similarity if h.similarity is not None else -1.0
        return (-len(h.cited_sources), -sim, -_date_to_int(pd), h.title.lower())

    return sorted(hits.values(), key=key)


def _date_to_int(s: str) -> int:
    """Turn a YYYY-MM-DD string into a sortable int. 0 for blanks."""
    if not s:
        return 0
    parts = s.split("-")
    if len(parts) < 3:
        parts += ["00"] * (3 - len(parts))
    try:
        y, m, d = int(parts[0]), int(parts[1]), int(parts[2])
    except ValueError:
        return 0
    return y * 10000 + m * 100 + d


def _write_jsonl(hits: list[AggregatedHit], path: Path) -> None:
    with path.open("w") as f:
        for h in hits:
            f.write(
                json.dumps(
                    {
                        "doi": h.doi,
                        "paperId": h.paper_id,
                        "title": h.title,
                        "year": h.year,
                        "publicationDate": h.publication_date,
                        "venue": h.venue,
                        "tldr": h.tldr,
                        "abstract": h.abstract,
                        "authors": h.authors,
                        "citationCount": h.citation_count,
                        "citedSources": sorted(h.cited_sources),
                        "influentialFor": sorted(h.influential_for),
                        "intents": sorted(h.intents),
                        "similarity": (
                            round(h.similarity, 4) if h.similarity is not None else None
                        ),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )


def _write_markdown(
    hits: list[AggregatedHit],
    *,
    out: Path,
    since: str | None,
    until: str | None,
    n_sources: int,
    n_scanned: int,
    n_kept: int,
    influential_only: bool,
    keep_background: bool,
    min_co_cites: int,
    min_citing_citations: int,
    min_similarity: float,
    top_n: int | None,
) -> None:
    lines: list[str] = []
    lines.append("# Papers citing the precis corpus")
    lines.append("")
    lines.append(f"- Generated: {datetime.now(UTC).isoformat()}")
    lines.append(f"- Source papers swept: {n_sources}")
    lines.append(f"- Citations scanned: {n_scanned}")
    lines.append(f"- Unique citing papers after filters: {len(hits)}")
    lines.append(f"- Date window: {since or '*'} → {until or '*'}")
    filter_parts = [
        f"influential_only={influential_only}",
        f"keep_background={keep_background}",
        f"min_co_cites={min_co_cites}",
        f"min_citing_citations={min_citing_citations}",
        f"min_similarity={min_similarity}",
    ]
    if top_n:
        filter_parts.append(f"top_n={top_n}")
    lines.append(f"- Filters: {', '.join(filter_parts)}")
    lines.append("")
    sort_desc = "co-citations (DESC)"
    if min_similarity > 0:
        sort_desc += ", bge-m3 similarity (DESC)"
    sort_desc += ", publication date (DESC)"
    lines.append(f"Sorted by: {sort_desc}.")
    lines.append("")

    for i, h in enumerate(hits, start=1):
        title = h.title or "(untitled)"
        lines.append(f"## {i}. {title}")
        meta_bits = []
        if h.publication_date:
            meta_bits.append(h.publication_date)
        elif h.year:
            meta_bits.append(str(h.year))
        if h.venue:
            meta_bits.append(h.venue)
        if h.citation_count is not None:
            meta_bits.append(f"{h.citation_count} cites")
        if h.similarity is not None:
            meta_bits.append(f"sim {h.similarity:.2f}")
        if meta_bits:
            lines.append(f"*{' · '.join(meta_bits)}*")
        if h.authors:
            lines.append(
                ", ".join(h.authors[:8]) + (" *et al.*" if len(h.authors) > 8 else "")
            )
        if h.doi:
            lines.append(f"DOI: <https://doi.org/{h.doi}>")
        elif h.paper_id:
            lines.append(f"S2: <https://www.semanticscholar.org/paper/{h.paper_id}>")

        lines.append("")
        lines.append(
            f"Cites **{len(h.cited_sources)}** of our papers"
            + (
                f" (influential for: {', '.join(sorted(h.influential_for))})"
                if h.influential_for
                else ""
            )
        )
        if h.intents:
            lines.append(f"Intents: {', '.join(sorted(h.intents))}")
        lines.append("")
        for slug in sorted(h.cited_sources):
            marker = " ★" if slug in h.influential_for else ""
            lines.append(f"- `{slug}`{marker}")
        lines.append("")
        if h.tldr:
            lines.append(f"> **TL;DR:** {h.tldr}")
            lines.append("")
        elif h.abstract:
            preview = h.abstract.replace("\n", " ")[:400]
            lines.append(f"> {preview}{'…' if len(h.abstract) > 400 else ''}")
            lines.append("")
        lines.append("---")
        lines.append("")

    out.write_text("\n".join(lines))


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def main() -> None:
    p = argparse.ArgumentParser(
        description="Sweep S2 for papers citing the precis corpus.",
    )
    today = datetime.now(UTC).date()
    default_since = (today - timedelta(days=180)).isoformat()
    p.add_argument(
        "--since",
        default=default_since,
        help=f"Citations published on/after this YYYY-MM-DD (default: {default_since}).",
    )
    p.add_argument(
        "--until",
        default=None,
        help="Citations published on/before this YYYY-MM-DD (default: open-ended).",
    )
    p.add_argument(
        "--influential-only",
        action="store_true",
        help="Keep only S2 isInfluential=True citations.",
    )
    p.add_argument(
        "--keep-background",
        action="store_true",
        help="Don't drop citations whose only intent is 'background'.",
    )
    p.add_argument(
        "--min-co-cites",
        type=int,
        default=1,
        help=(
            "Drop citing papers that cite fewer than N of our papers (default 1). "
            "Strongest signal — set 2-5 to cut noise dramatically."
        ),
    )
    p.add_argument(
        "--min-citing-citations",
        type=int,
        default=0,
        help=(
            "Drop citing papers with fewer than N citations themselves "
            "(default 0). Set >0 to filter out fresh preprints with no traction."
        ),
    )
    p.add_argument(
        "--min-similarity",
        type=float,
        default=0.0,
        help=(
            "bge-m3 cosine threshold (default 0.0 = off). Citing papers "
            "whose title+abstract scores below this against ALL of their "
            "cited source papers are dropped. Typical usable threshold: "
            "0.50-0.65."
        ),
    )
    p.add_argument(
        "--top-n",
        type=int,
        default=None,
        help="Hard cap on output: keep only the top N hits after sort.",
    )
    p.add_argument(
        "--per-source-top",
        type=int,
        default=0,
        help=(
            "Switch to per-source mode: emit the top K citations PER source "
            "paper instead of the global aggregate. Useful for 'what's new "
            "for paper X' digests. 0 = global mode (default)."
        ),
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Only sweep the N most-recently-ingested source papers.",
    )
    p.add_argument(
        "--slug-prefix",
        default=None,
        help="Restrict sweep to source papers whose slug starts with this prefix.",
    )
    p.add_argument(
        "--page-limit",
        type=int,
        default=1000,
        help="S2 max-results per source paper (default 1000, the API ceiling).",
    )
    p.add_argument(
        "--cache-dir",
        default=os.environ.get(CACHE_DIR_ENV) or str(DEFAULT_CACHE_DIR),
        help="Per-paper JSON cache directory (resumable).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Ignore cache, re-fetch every paper.",
    )
    p.add_argument(
        "--no-fetch",
        action="store_true",
        help="Skip S2 calls; aggregate from existing cache only.",
    )
    p.add_argument(
        "--out",
        default=None,
        help="Markdown report path (default: paper-ingest/citing-papers-<ts>.md).",
    )
    p.add_argument(
        "--jsonl-out",
        default=None,
        help="JSONL feed path (default: same stem as --out, .jsonl extension).",
    )
    args = p.parse_args()

    # Lazy imports so --help is fast and the script doesn't crash if the
    # corpus DB is offline.
    from semanticscholar import SemanticScholar  # type: ignore

    cache_dir = Path(args.cache_dir).expanduser().resolve()
    cache_dir.mkdir(parents=True, exist_ok=True)

    if args.out is None:
        ts = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.out = str(DEFAULT_OUT_DIR / f"citing-papers-{ts}.md")
    out_md = Path(args.out).expanduser().resolve()
    out_md.parent.mkdir(parents=True, exist_ok=True)

    if args.jsonl_out is None:
        args.jsonl_out = str(out_md.with_suffix(".jsonl"))
    out_jsonl = Path(args.jsonl_out).expanduser().resolve()

    # 1. Load source corpus + dedup set.
    sources, known_dois = _load_corpus(slug_prefix=args.slug_prefix, limit=args.limit)
    print(
        f"Source papers: {len(sources)} (slug-prefix={args.slug_prefix!r}, "
        f"limit={args.limit}); corpus dedup set has {len(known_dois)} DOIs"
    )

    # 2. Fetch loop (skip if cached and not --force).
    if not args.no_fetch:
        api_key = os.environ.get("SEMANTIC_SCHOLAR_API_KEY", "")
        sch = SemanticScholar(api_key=api_key) if api_key else SemanticScholar()
        if not api_key:
            print(
                "WARNING: SEMANTIC_SCHOLAR_API_KEY not set — using free tier "
                "(slow, prone to 429)."
            )

        n_total = len(sources)
        n_skipped = 0
        n_fetched = 0
        n_failed = 0

        for i, src in enumerate(sources, start=1):
            cache_path = cache_dir / f"{src.slug}.json"
            if cache_path.exists() and not args.force:
                n_skipped += 1
                continue

            start = time.monotonic()
            blob = _fetch_one(sch, src, page_limit=args.page_limit)
            elapsed = time.monotonic() - start

            if blob is None:
                n_failed += 1
                # Still write a sentinel so we don't re-attempt next run.
                cache_path.write_text(
                    json.dumps(
                        {
                            "slug": src.slug,
                            "doi": src.doi,
                            "arxiv_id": src.arxiv_id,
                            "fetched_at": datetime.now(UTC).isoformat(),
                            "error": "no s2 lookup id",
                            "citations": [],
                        }
                    )
                )
            else:
                cache_path.write_text(json.dumps(blob, ensure_ascii=False))
                n_fetched += 1
                if blob.get("error"):
                    n_failed += 1

            n_cites = len(blob.get("citations", [])) if blob else 0
            print(
                f"[{i:>5}/{n_total}] {src.slug:<40} "
                f"cites={n_cites:>4} t={elapsed:5.2f}s "
                f"(skipped={n_skipped} fetched={n_fetched} failed={n_failed})"
            )
            time.sleep(INTER_CALL_SLEEP)

        print(
            f"\nFetch summary: skipped={n_skipped} fetched={n_fetched} "
            f"failed={n_failed} (cache_dir={cache_dir})"
        )

    # 3. Aggregate from cache.
    cache_files = sorted(cache_dir.glob("*.json"))
    if args.slug_prefix:
        cache_files = [p for p in cache_files if p.stem.startswith(args.slug_prefix)]
    if args.limit:
        # Reflect the same source-paper subset on the aggregation pass
        # so --limit is meaningful even on cache-only runs.
        wanted = {s.slug for s in sources}
        cache_files = [p for p in cache_files if p.stem in wanted]

    if args.per_source_top > 0:
        # --- Per-source-top-K mode (alternative aggregation) ---
        by_source, scanned, kept = _aggregate_per_source(
            cache_files,
            since=args.since,
            until=args.until,
            influential_only=args.influential_only,
            keep_background=args.keep_background,
            known_dois=known_dois,
            min_citing_citations=args.min_citing_citations,
            top_k=args.per_source_top,
        )
        _write_per_source_markdown(
            by_source,
            out=out_md,
            since=args.since,
            until=args.until,
            n_sources=len(cache_files),
            n_scanned=scanned,
            n_kept=kept,
            influential_only=args.influential_only,
            keep_background=args.keep_background,
            min_citing_citations=args.min_citing_citations,
            top_k=args.per_source_top,
        )
        # JSONL: one line per (source, citation) pair so downstream
        # consumers can join either way.
        with out_jsonl.open("w") as f:
            for slug, cites in by_source.items():
                for c in cites:
                    f.write(
                        json.dumps({"sourceSlug": slug, **c}, ensure_ascii=False) + "\n"
                    )
        total_hits = sum(len(v) for v in by_source.values())
        print(
            f"\nAggregation (per-source-top-{args.per_source_top}): "
            f"scanned={scanned} kept={kept} sources_with_hits={len(by_source)} "
            f"total_emitted={total_hits}"
        )
        print(f"Markdown: {out_md}")
        print(f"JSONL:    {out_jsonl}")
        return

    # --- Global aggregate mode ---
    hits, scanned, kept = _aggregate(
        cache_files,
        since=args.since,
        until=args.until,
        influential_only=args.influential_only,
        keep_background=args.keep_background,
        known_dois=known_dois,
        min_citing_citations=args.min_citing_citations,
        min_co_cites=args.min_co_cites,
    )
    print(f"\nAggregation: scanned={scanned} kept={kept} unique={len(hits)}")

    if args.min_similarity > 0:
        hits = _apply_similarity_gate(hits, min_similarity=args.min_similarity)

    sorted_hits = _sort_hits(hits)
    if args.top_n is not None:
        sorted_hits = sorted_hits[: args.top_n]

    _write_markdown(
        sorted_hits,
        out=out_md,
        since=args.since,
        until=args.until,
        n_sources=len(cache_files),
        n_scanned=scanned,
        n_kept=kept,
        influential_only=args.influential_only,
        keep_background=args.keep_background,
        min_co_cites=args.min_co_cites,
        min_citing_citations=args.min_citing_citations,
        min_similarity=args.min_similarity,
        top_n=args.top_n,
    )
    _write_jsonl(sorted_hits, out_jsonl)

    print(f"Final report: {len(sorted_hits)} citing papers")
    print(f"Markdown: {out_md}")
    print(f"JSONL:    {out_jsonl}")


if __name__ == "__main__":
    main()
