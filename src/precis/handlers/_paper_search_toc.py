"""Keyword navigation for paper search's retrieved pool, never the corpus.

Only matched chunks contribute labels. Missing labels use section headings,
then bounded compute-and-discard KeyBERT with stored, current embeddings.
No read-path keyword writes occur; the benchmark keyword switch is respected.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from typing import Any

from precis.bench import switches
from precis.errors import BadInput
from precis.format import render_agent_table
from precis.handlers._paper_search import (
    BlockSearchResult,
    PaperSearchResultRenderer,
    _hint_args_suffix,
)
from precis.response import Response
from precis.utils.keyword_toc import clean_keyword, cluster_pool
from precis.utils.next_block import render_next_section
from precis.utils.toc_db import _section_path

log = logging.getLogger(__name__)


def validate_navigation(
    *, kind: str, q: str | None, kw: Any, good: bool, title: Any, author: Any
) -> None:
    """Refuse unsupported doors instead of silently losing navigation filters."""
    if kind != "paper" or not q or good or title or author:
        raise BadInput(
            "keyword navigation needs an inline paper q= search",
            next="search(kind='paper', q='catalysis', view='toc')",
        )
    if not switches.current().kwd:
        raise BadInput(
            "keyword navigation is unavailable under no-kwd",
            next="search(kind='paper', q='catalysis')",
        )
    if kw is not None and (
        not isinstance(kw, (str, list))
        or not kw
        or any(
            not isinstance(k, str) or not clean_keyword(k)
            for k in ([kw] if isinstance(kw, str) else kw)
        )
    ):
        raise BadInput(
            "kw must be a nonempty keyword string or list of strings (AND)",
            next="search(kind='paper', q='catalysis', args={'kw': 'photocatalysis'})",
        )


def pool_keywords(
    hits: list[tuple[Any, Any, float]], store: Any, embedder: Any
) -> tuple[dict[int, list[str]], str]:
    """Resolve pool labels with at most 200 extraction candidates and one batch."""
    keywords: dict[int, list[str]] = {}
    pending: dict[int, tuple[Any, Any]] = {}
    for block, ref, _ in hits:
        labels = getattr(block, "keywords", None) or _section_path(block)[-1:]
        keywords[block.id] = [k for raw in labels if (k := clean_keyword(raw))]
        if not keywords[block.id]:
            pending[block.id] = (block, ref)
    if not pending or embedder is None:
        return keywords, ""
    from precis.workers.chunk_keywords import (
        KEYWORDS_VERSION,
        _prepare_keyword_candidates,
        _score_keyword_candidates,
        is_keyword_eligible,
    )

    pending = {
        cid: pair for cid, pair in pending.items() if is_keyword_eligible(pair[0])
    }
    pending = dict(list(pending.items())[:200])
    if not pending:
        return keywords, ""
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ce.chunk_id, ce.vector::text FROM chunk_embeddings ce "
                "JOIN chunks c ON c.chunk_id = ce.chunk_id "
                "WHERE ce.chunk_id = ANY(%s) AND ce.embedder = %s "
                "AND ce.status = 'ok' AND ce.vector IS NOT NULL "
                "AND ce.content_sha IS NOT DISTINCT FROM c.content_sha",
                (list(pending), embedder.model),
            ).fetchall()
        prepared = []
        texts: list[str] = []
        for cid, vector in rows:
            block, ref = pending[cid]
            candidates = _prepare_keyword_candidates(
                block.text, (getattr(ref, "meta", None) or {}).get("abbrevs", {})
            )
            prepared.append((cid, json.loads(vector), candidates))
            texts.extend(long for long, _short in candidates)
        vectors = embedder.embed(texts) if texts else []
        offset = 0
        for cid, vector, candidates in prepared:
            rich = _score_keyword_candidates(
                vector, candidates, vectors[offset : offset + len(candidates)]
            )
            keywords[cid] = [
                k
                for item in rich
                if (k := clean_keyword(item["short"] or item["long"]))
            ]
            offset += len(candidates)
        log.info(
            "paper keyword TOC extraction version=%s embedder=%s chunks=%d",
            KEYWORDS_VERSION,
            embedder.model,
            len(prepared),
        )
    except Exception:
        log.warning("paper keyword TOC extraction failed", exc_info=True)
        return (
            keywords,
            "\nKeyword extraction unavailable; unlabelled hits remain in (none).",
        )
    return keywords, ""


def render_navigation(
    result: BlockSearchResult, *, kw: str | list[str] | None, store: Any, embedder: Any
) -> Response:
    """Filter before paper collapse; recurse through strict shrinking facets."""
    filters = [kw] if isinstance(kw, str) else list(kw or [])
    filters = [clean_keyword(k) for k in filters]
    labels, notice = pool_keywords(result.hits, store, embedder)
    paper_terms: dict[int, set[str]] = {}
    for block, ref, _ in result.hits:
        paper_terms.setdefault(ref.id, set()).update(labels[block.id])
    selected = {
        rid
        for rid, terms in paper_terms.items()
        if all((not terms if k == "(none)" else k in terms) for k in filters)
    }
    hits = [h for h in result.hits if h[1].id in selected]
    clusters, none = cluster_pool(
        [(ref.id, rank, labels[block.id]) for rank, (block, ref, _) in enumerate(hits)],
        [result.q, *filters],
    )
    log.info(
        "paper keyword TOC depth=%d clusters=%d none_share=%.4f mean_clusters_per_paper=%.4f",
        len(filters),
        len(clusters),
        len(none) / max(1, len(selected)),
        sum(len(c.papers) for c in clusters) / max(1, len(selected)),
    )
    echo_args = dict(result.cohort_args)
    if result.extra_queries:
        echo_args["queries"] = result.extra_queries
    if result.hyde_answers:
        echo_args["answers"] = result.hyde_answers
    if result.per_paper_cap is not None:
        echo_args["per_paper"] = result.per_paper_cap
    echo_kwargs = dict(result.cohort_kwargs)
    if result.scope:
        echo_kwargs["scope"] = result.scope
    if result.mode:
        echo_kwargs["mode"] = result.mode
    echo_kwargs["page_size"] = result.page_size
    prefix = "kw: " + " + ".join(filters) + "\n" if filters else ""
    if len(selected) <= result.page_size or not clusters or filters == ["(none)"]:
        counts = Counter(ref.id for _b, ref, _ in hits)
        seen: set[int] = set()
        unique = []
        for hit in hits:
            if hit[1].id not in seen:
                seen.add(hit[1].id)
                unique.append(hit)
        result.hits = unique[: result.page_size]
        result.unique_per = "paper"
        result.paper_ref_counts = dict(counts)
        result.paper_total = len(selected)
        result.total = len(hits)
        result.broad_has_more = len(unique) > result.page_size
        result.single_page_has_more = result.broad_has_more
        if filters:
            result.cohort_args["kw"] = filters
        body = PaperSearchResultRenderer(kind=result.kind).render(result).body
        if len(selected) > result.page_size:
            notice += (
                "\nNo further shrinking keyword facets; refine q=, scope= or exclude=."
            )
        return Response(body=prefix + body + notice)
    rows = [
        {
            "cluster": "kw:" + c.label,
            "papers": str(len(c.papers)),
            "keywords": ", ".join(c.keywords),
        }
        for c in clusters
    ]
    if none:
        rows.append(
            {
                "cluster": "(none)",
                "papers": str(len(none)),
                "keywords": "no keyworded chunk in the pool",
            }
        )
    body = (
        prefix
        + f"# {len(selected)} papers for {result.q!r} ({len(hits)} chunk hits in the retrieved pool)"
        f" - {len(clusters)} keyword clusters, {len(none)} papers without keywords\n"
        "Keyword facets overlap; counts cover the retrieved pool only.\n\n"
        + render_agent_table(rows, schema=["cluster", "papers", "keywords"])
        + result.year_notice
        + result.exclude_notice
        + notice
    )
    nav = []
    for label in [c.label for c in clusters] + (["(none)"] if none else []):
        args = {**echo_args, "kw": [*filters, label]}
        call = f"search(kind='paper', q={result.q!r}, view='toc'{_hint_args_suffix(args, echo_kwargs)})"
        nav.append((call, "drill kw:" + label))
    tightened = f"{result.q} {clusters[0].label}"
    nav.append(
        (
            f"search(kind='paper', q={tightened!r}{_hint_args_suffix(echo_args, echo_kwargs)})",
            "refine the query",
        )
    )
    return Response(body=body + render_next_section(nav))
