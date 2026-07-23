"""Collaborators for ``PaperHandler.search()``.

Split out of the former ~600-line ``search()`` method (OPEN-ITEMS
"Refactor handlers/paper.py::search()"). Three seams, matching how the
method actually branches:

- :class:`BylineSearch` — the ``title=`` / ``author=`` record lookup
  (paper-level rows, not body-block hits).
- :class:`FusedBlockSearch` — parameter validation + the single-leg /
  broad (``queries=``/``answers=``/``per_paper=``) block retrieval,
  producing a :class:`BlockSearchResult`.
- :class:`PaperSearchResultRenderer` — renders a ``BlockSearchResult``
  into the final agent-facing :class:`~precis.response.Response`
  (empty-hits DOI-aware guidance, headline + TOON table, pagination
  trailer).

The ``good=True`` deep-search campaign is its own module
(:mod:`precis.handlers._good_search`) — already a thin ``submit_*``
function rather than a stateful search, so it isn't duplicated here;
``PaperHandler.search()`` calls it directly.

Each class takes only the slice of context it needs (``store`` /
``embedder`` / ``kind``) rather than the whole handler, so each is
independently testable without a full ``Hub`` fixture. Imported lazily
from ``paper.py`` (mirroring the existing lazy import of
``submit_good_search``) so this module can import paper.py's shared
helpers (``_maybe_resolve_doi``, ``_DOI_RE``, ``_suggest_paper_slugs``,
…) without a circular top-level import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.handlers._paper_format import _clean_inline_text, _format_authors
from precis.handlers._paper_text import _chunk_keywords_or_caption, _scrub_block_text
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Tag
from precis.utils import handle_registry
from precis.utils.embed_query import query_vec_for
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

#: Broad-retrieval fan-out cap: ``queries=`` and ``answers=`` each accept
#: at most this many entries. Mirrored at the MCP boundary
#: (``tools/core.py``) — the handler re-checks because the agentic tier
#: calls it directly, bypassing the MCP surface. Also re-exported so
#: ``PaperHandler.search()`` can apply the same cap before dispatching
#: here (the ``good=True`` deep-search leg shares the same limit).
_BROAD_LEG_CAP = 8


def _coerce_search_year(value: int | str | None, param: str) -> int | None:
    """Validate an ``after=`` / ``before=`` publish-year bound.

    Returns ``None`` when unset, else an int in 1500..2100. A
    non-integer or out-of-range value raises :class:`BadInput` at the
    agent boundary (the store-side guard in ``_blocks_ops`` re-checks).
    Year-grained because the corpus stores ``refs.year``, not full
    dates; a ``'2019-03'`` string keeps only the leading year via int().
    """
    if value is None:
        return None
    raw = str(value).strip().split("-", 1)[0]  # tolerate 'YYYY-MM' → 'YYYY'
    try:
        iv = int(raw)
    except ValueError:
        raise BadInput(
            f"{param}= must be a 4-digit year, got {value!r}",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        ) from None
    if not (1500 <= iv <= 2100):
        raise BadInput(
            f"{param}={iv} out of plausible range (1500..2100)",
            next="search(kind='paper', q='…', after=2019, before=2023)",
        )
    return iv


def _normalise_exclude_slug(raw: str, *, store: Any) -> str | None:
    """Coerce one ``exclude=`` entry to a bare paper slug.

    The exclude list is coarse-only (ref-level): ``wang2020state`` and
    ``wang2020state~38`` both drop every block of the paper. We
    therefore strip any chunk selector or view path the caller may
    have copy-pasted from a hit handle, then DOI-resolve so
    ``exclude=['10.1111/jnc.13915']`` is equivalent to passing the
    matching slug.

    Returns ``None`` when the input is empty / whitespace / unresolvable
    DOI — the caller silently drops nones so a stale slug in the
    exclude list never poisons the whole search call.
    """
    if not raw:
        return None
    cleaned = raw.strip()
    if not cleaned:
        return None
    # ADR 0036: an exclude entry copied from a hit is now a universal handle
    # (``pa5`` / ``pc40``). Resolve it to the owning paper slug (ref-level,
    # coarse) so ``exclude=['pc40']`` drops that paper's blocks.
    if handle_registry.parse(cleaned) is not None:
        resolved_handle = store.resolve_handle(cleaned)
        return resolved_handle.public_id if resolved_handle is not None else None
    # Coarse-only: trim the first ``~`` (chunk selector) or ``/``
    # (view path). DOI-form ids carry ``/`` in the suffix, but
    # _maybe_resolve_doi runs first and replaces the whole DOI with
    # a bare slug, so by the time we reach the split there's no DOI
    # ``/`` to confuse with a view path.
    from precis.handlers.paper import _maybe_resolve_doi

    try:
        resolved = _maybe_resolve_doi(store, cleaned)
    except Exception:
        # Unknown DOI raises NotFound from _maybe_resolve_doi; treat
        # the same as a stale slug in the exclude list — silent drop.
        return None
    for sep in ("~", "/"):
        if sep in resolved:
            resolved = resolved.split(sep, 1)[0]
    resolved = resolved.strip()
    return resolved or None


def _dedup_card_hits(
    hits: list[tuple[Any, Any, float]],
) -> list[tuple[Any, Any, float]]:
    """Drop a paper's synthetic ``card_*`` hit when a body block of the
    same paper is also on the page.

    Paper search opts ``card_combined`` into the legs so a title / meta
    query can surface a paper that has no matching body block. But when
    a real (quotable) body block of that paper *also* matched, the card
    is redundant noise — the body block is the better hit. So a card
    hit survives only for refs that have no body hit in this page; for
    refs with both, the card is dropped and ordering is otherwise
    preserved.
    """
    body_ref_ids = {
        ref.id
        for block, ref, _score in hits
        if not block.chunk_kind.startswith("card_")
    }
    return [
        (block, ref, score)
        for block, ref, score in hits
        if not (block.chunk_kind.startswith("card_") and ref.id in body_ref_ids)
    ]


def _embed_query_batch(embedder: Any | None, texts: list[str]) -> list[list[float]]:
    """Embed several search texts in ONE batch call, degrading to ``[]``.

    Broad retrieval embeds ``q`` + up to 8 rephrasings + up to 8 HyDE
    answers; one ``embed(texts)`` round trip replaces up to 17 serial
    ``embed_one`` calls (each of which would pay its own failure /
    timeout). The degrade contract mirrors
    :func:`precis.utils.embed_query.embed_query`: a missing OR failing
    embedder returns ``[]`` — the caller runs lexical-only — and never
    propagates. The failure is logged at WARNING with the traceback.
    """
    import logging

    log = logging.getLogger("precis.handlers.paper")

    if embedder is None or not texts:
        return []
    try:
        vecs = embedder.embed(texts)
    except Exception:
        log.warning(
            "broad search: batch embed failed for %d texts; "
            "falling back to lexical-only",
            len(texts),
            exc_info=True,
        )
        return []
    return [v for v in vecs if v is not None]


def _broad_args_suffix(
    queries: list[str],
    answers: list[str],
    per_paper: int | None,
) -> str:
    """Render the broad-retrieval knobs as a call-argument suffix.

    Used by the pagination continuation in the ``Next:`` trailer: a
    ``page=N+1`` follow-up that drops ``queries=``/``answers=``/
    ``per_paper=`` would run the *single-leg* path — a different
    ordering, so page 2 would carry duplicates of page 1 and gaps.
    The lists are ≤8 short strings each (leg cap), so rendering them
    verbatim stays compact.
    """
    parts: list[str] = []
    if queries:
        parts.append(f"queries={queries!r}")
    if answers:
        parts.append(f"answers={answers!r}")
    if per_paper is not None:
        parts.append(f"per_paper={per_paper}")
    return (", " + ", ".join(parts)) if parts else ""


@dataclass
class BylineSearch:
    """``title=`` / ``author=`` paper-level lookup — record rows, not
    body-block hits.

    Answers the block path's two weak spots: an exact-title query
    dives below content-dense bodies of *other* papers, and a bare
    author query surfaces those papers' bibliography lines instead of
    the paper itself (the combined card dilutes the byline). Matches
    ``refs.title`` (trigram + FTS) / ``refs.authors`` (jsonb) directly,
    held copies first, and returns each paper's handle + a one-line
    citation + a one-tap ``view='bibtex'`` path.
    """

    store: Any

    def run(
        self, *, field: str, q: str, page: int, page_size: int, kind: str
    ) -> Response:
        offset = max(0, (int(page) - 1) * int(page_size))
        # Probe one past the page so the has-more trailer is exact.
        want = offset + page_size + 1
        if field == "title":
            ids = self.store.find_papers_by_title(kind=kind, q=q, limit=want)
        else:
            ids = self.store.find_papers_by_author(kind=kind, q=q, limit=want)
        has_more = len(ids) > offset + page_size
        page_ids = ids[offset : offset + page_size]

        if not page_ids:
            body = f"no paper matches {field}={q!r}"
            fallback: list[tuple[str, str]] = [
                (
                    f"search(kind='paper', q={q!r})",
                    "fall back to full-text search across paper bodies",
                ),
            ]
            if field == "title":
                fallback.append(
                    (
                        f"put(kind='paper', title={q!r})",
                        "request the paper if the library doesn't hold it yet",
                    )
                )
            return Response(body=body + render_next_section(fallback))

        refs_map = self.store.fetch_refs_by_ids(page_ids)
        rows: list[dict[str, str]] = []
        for rid in page_ids:
            ref = refs_map.get(rid)
            if ref is None:
                continue
            handle = (
                handle_registry.try_format(ref.kind, ref.id)
                or ref.slug
                or f"paper:{rid}"
            )
            authors = _format_authors(ref.authors) or "(authors unknown)"
            year = ref.year if ref.year is not None else "n.d."
            title_disp = _clean_inline_text(ref.title) if ref.title else "(untitled)"
            rows.append(
                {
                    "handle": handle,
                    "held": "held" if ref.pdf_sha256 is not None else "want",
                    "citation": f"{authors} ({year}). {title_disp}",
                }
            )

        head = format_search_headline(
            n_returned=len(rows),
            total=None,
            noun="paper",
            query=f"{field}={q!r}",
        )
        table = render_agent_table(rows, schema=["handle", "held", "citation"])
        body = head + "\n\n" + table

        top = rows[0]["handle"]
        nav: list[tuple[str, str]] = [
            (
                f"get(id='{top}', view='bibtex')",
                "cite it — BibTeX (also view='ris' / 'endnote')",
            ),
            (
                f"get(kind='paper', id='{top}', view='toc')",
                "open the paper — the TOC reading entry point",
            ),
        ]
        if has_more:
            nav.append(
                (
                    f"search(kind='paper', {field}={q!r}, page={int(page) + 1})",
                    "see more matching papers",
                )
            )
        return Response(body=body + render_next_section(nav))


@dataclass
class BlockSearchResult:
    """Everything :class:`PaperSearchResultRenderer` needs to render a page.

    Populated by :meth:`FusedBlockSearch.run`. ``hits`` is empty when
    nothing matched — the renderer's job, not this class's, to turn
    that into the DOI-aware "no matches" guidance.
    """

    kind: str
    q: str
    page: int
    page_size: int
    scope: str | None
    hits: list[tuple[Any, Any, float]]
    year_notice: str
    broad: bool
    broad_has_more: bool
    extra_queries: list[str] = field(default_factory=list)
    hyde_answers: list[str] = field(default_factory=list)
    per_paper_cap: int | None = None
    total: int | None = None


@dataclass
class FusedBlockSearch:
    """Validation + retrieval for the block-hit search path.

    Resolves ``scope=`` / ``exclude=`` / ``tags=`` / ``after=``/
    ``before=`` into store-level filters, then runs either the plain
    single-query lex+sem search or (when ``queries=``/``answers=``/
    ``per_paper=`` are given) the broad RRF fusion across every
    reformulation + HyDE leg. Title-introducer promotion and the
    year-omitted notice run here too — both are retrieval-shaping,
    not presentation.
    """

    store: Any
    embedder: Any
    kind: str

    def _inject_title_matches(
        self,
        hits: list[Any],
        *,
        q: str,
        kind: str,
        page_size: int,
    ) -> list[Any]:
        """Promote near-exact title matches to the front of ``hits``.

        See the call site: FTS stop-word stripping buries an exact-title
        query's paper. We trigram-match the raw title (store method),
        fetch a representative block per match (first body chunk, else
        the title/abstract card), and prepend — deduping by ref_id so a
        paper already in ``hits`` is reordered rather than duplicated.
        Best-effort: any lookup hiccup returns ``hits`` unchanged.
        """
        import logging

        log = logging.getLogger("precis.handlers.paper")

        try:
            matches = self.store.find_refs_by_title_similarity(kind=kind, q=q, limit=3)
            if not matches:
                return hits
            match_ids = [rid for rid, _sim in matches]
            existing = {h[1].id: h for h in hits}
            refs_map = self.store.fetch_refs_by_ids(match_ids)
            front: list[Any] = []
            for rid in match_ids:
                if rid in existing:
                    front.append(existing[rid])  # reorder, don't refetch
                    continue
                ref = refs_map.get(rid)
                if ref is None:
                    continue
                block = self.store.get_block(rid, pos=0) or self.store.get_block(
                    rid, pos=-1
                )
                if block is None:
                    body = self.store.list_blocks_for_ref(rid)
                    block = body[0] if body else None
                if block is None:
                    continue
                front.append((block, ref, float("inf")))
            if not front:
                return hits
            front_ids = {h[1].id for h in front}
            rest = [h for h in hits if h[1].id not in front_ids]
            return (front + rest)[:page_size]
        except Exception:  # pragma: no cover — relevance aid, never fatal
            log.warning(
                "paper search: title introducer failed for %r", q, exc_info=True
            )
            return hits

    def run(
        self,
        *,
        q: str,
        scope: str | None,
        tags: list[str] | None,
        page_size: int,
        page: int,
        exclude: list[str] | None,
        mode: str | None,
        after: int | str | None,
        before: int | str | None,
        queries: list[str] | None,
        answers: list[str] | None,
        per_paper: int | None,
    ) -> BlockSearchResult:
        # Local import — paper.py imports this module lazily, so by the
        # time ``run`` executes paper.py is fully loaded; see this
        # module's docstring for why the import is deferred rather than
        # top-level.
        from precis.handlers.paper import _maybe_resolve_doi, _suggest_paper_slugs

        kind = self.kind

        # Publish-date filter: ``after`` / ``before`` are inclusive
        # publication-year bounds (the corpus stores year, not full
        # dates). Validate at the agent boundary so a bad bound is a
        # clean error rather than zero silent hits.
        year_from = _coerce_search_year(after, "after")
        year_to = _coerce_search_year(before, "before")
        if year_from is not None and year_to is not None and year_from > year_to:
            raise BadInput(
                f"after={year_from} is later than before={year_to}",
                next=f"search(kind='{kind}', q='…', after=2019, before=2023)",
            )

        # Validate the filter at the agent boundary. Same canonical-form
        # rejection as ``put(tags=...)`` so an agent that wrote
        # ``tags=['urgent']`` gets the same error shape it'd get on
        # write — not silently zero hits.
        # Per-kind axis enforcement: passing kind='paper' here means a
        # filter like ``tags=['STATUS:open']`` raises BadInput at the
        # boundary instead of silently returning zero hits — papers
        # have no STATUS axis. (Critic per-kind-axis follow-up.)
        normalized_tags = Tag.normalize_filter(tags, kind=kind)

        scope_ref_id: int | None = None
        if scope is not None:
            # ADR 0036: ``scope=`` accepts a universal handle (``pa<id>`` /
            # ``pc<id>``) — the form output now emits — resolving it to the
            # paper's ref_id; else the legacy slug / DOI path.
            scope_resolved = (
                self.store.resolve_handle(str(scope))
                if handle_registry.parse(str(scope)) is not None
                else None
            )
            if scope_resolved is not None:
                scope_ref_id = scope_resolved.ref_id
            else:
                scope_slug = _maybe_resolve_doi(self.store, str(scope))
                scope_ref = resolve_live_slug_ref(
                    self.store,
                    kind=kind,
                    id=scope_slug,
                    next_hint=f"search(kind='{kind}', q='...') to find one",
                    options=_suggest_paper_slugs(scope, store=self.store, kind=kind),
                )
                scope_ref_id = scope_ref.id

        # ``exclude=['slug1', 'slug2']`` drops every block of the
        # listed papers from the result set. Coarse / ref-level: a
        # ``slug~38`` entry is treated as ``slug``. Stale slugs are
        # silently dropped — the agent's exclude list may carry
        # ids that no longer resolve, and we'd rather quietly skip
        # than fail the whole search. The canonical use case is the
        # "show me hits 6..N" continuation rendered in the Next:
        # trailer below.
        excluded_slugs_in: list[str] = []
        exclude_ref_ids: list[int] = []
        if exclude:
            normalised: list[str] = []
            for raw in exclude:
                slug = _normalise_exclude_slug(str(raw), store=self.store)
                if slug is not None:
                    normalised.append(slug)
            # Dedup while preserving order so the trailer's "previous
            # exclude" cross-reference reads predictably.
            seen: set[str] = set()
            for s in normalised:
                if s not in seen:
                    seen.add(s)
                    excluded_slugs_in.append(s)
            if excluded_slugs_in:
                exclude_ref_ids = self.store.fetch_ref_ids_by_slugs(
                    excluded_slugs_in, kind=kind
                )

        # ``max_distance`` enforces a semantic relevance floor so a
        # nonsense query (``'xyzzy frobnicate quux'``) returns an
        # empty response instead of the top-K closest random blocks.
        # The lexical leg already has a natural zero (the tsquery
        # either matches or it doesn't); the floor is sem-only.
        # (Critic MAJOR #3.)
        # page=N → offset = (page-1) * page_size. Clamped to >= 0 so a 7B
        # caller passing ``page=0`` doesn't blow the query up.
        search_offset = max(0, (int(page) - 1) * int(page_size))

        # ``card_combined`` (the embedded title+authors+abstract+keywords
        # card, ``ord=-1``) is opted into the search so a *title* / meta
        # query surfaces the paper even when no body block matches — the
        # card is reachable in both the lexical and semantic legs. Body
        # chunks still win per paper (see the dedup below); the card is
        # only a fallback introducer, not a quotable hit.
        # Broad / high-recall retrieval (Tier-1 fusion): when the caller
        # supplies extra query reformulations (``queries=``) and/or
        # hypothetical-answer passages (``answers=``, HyDE), or asks for a
        # per-paper diversity cap, fuse every leg via reciprocal-rank
        # fusion instead of the single lex+sem pass — a chunk that
        # surfaces across phrasings wins, killing the single-query
        # formulation sensitivity. Plain calls (no extras, no cap) take
        # the unchanged single path. (precis-search-help → "Broad
        # retrieval".)
        extra_queries = [s for s in (queries or []) if s and s.strip()]
        hyde_answers = [s for s in (answers or []) if s and s.strip()]
        per_paper_cap = per_paper  # validated by the caller (positive int, no bool)
        broad = bool(extra_queries or hyde_answers) or per_paper_cap is not None
        # ``broad_has_more``: the fused candidate list extended past this
        # page's slice (probed via limit+1 below). Broad mode's pagination
        # signal — the lexical count of the primary q is the wrong
        # universe for fused results (it can be < len(hits), or 0 when
        # only a rephrasing/HyDE leg matched).
        broad_has_more = False
        if broad:
            # Semantic legs embed q + each reformulation + each HyDE
            # answer — in ONE batch call (q is NOT embedded separately;
            # up to 17 serial embed_one round trips collapsed). A missing
            # OR failing embedder degrades the whole broad search to its
            # lexical legs (empty vecs) — mirroring :func:`embed_query` —
            # and ``mode='lexical'`` skips embedding entirely.
            q_texts = [q, *extra_queries]
            query_vecs: list[list[float]] = []
            if (mode or "hybrid").strip().lower() not in ("lexical", "verbatim"):
                query_vecs = _embed_query_batch(
                    self.embedder, [q, *extra_queries, *hyde_answers]
                )
            # Probe one row past the page so ``broad_has_more`` is exact
            # for the next-page trailer, then slice back to page_size.
            probe = self.store.search_blocks_multi(
                q_texts=q_texts,
                query_vecs=query_vecs,
                mode=mode,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                limit=page_size + 1,
                offset=search_offset,
                max_distance=SEMANTIC_DISTANCE_FLOOR,
                exclude_ref_ids=exclude_ref_ids or None,
                year_from=year_from,
                year_to=year_to,
                card_kinds=("card_combined",),
                per_paper=per_paper_cap,
            )
            broad_has_more = len(probe) > page_size
            hits = probe[:page_size]
        else:
            # Compute the query embedding for the semantic leg. A missing
            # OR failing embedder degrades to lexical-only (query_vec=None)
            # rather than 500 the whole search — the lexical leg still
            # answers (gripe #38684: search q='*' returned a 500). See
            # :func:`embed_query`.
            query_vec = query_vec_for(self.embedder, q, mode)
            hits = self.store.search_blocks(
                q=q,
                query_vec=query_vec,
                mode=mode,
                kind=kind,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                limit=page_size,
                offset=search_offset,
                max_distance=SEMANTIC_DISTANCE_FLOOR,
                exclude_ref_ids=exclude_ref_ids or None,
                year_from=year_from,
                year_to=year_to,
                card_kinds=("card_combined",),
            )
        hits = _dedup_card_hits(hits)

        # Title introducer: a query that *is* a paper's title
        # ("attention is all you need") gets stripped by FTS to its
        # content words ('attent' & 'need'), so the exact paper's short
        # card loses on ts_rank and never reaches the first page. On an
        # unfiltered first-page search, surface near-exact title matches
        # at the top. Gated tightly (plain query only, high similarity
        # bar) so an ordinary keyword search is untouched.
        if (
            page == 1
            and scope_ref_id is None
            and year_from is None
            and year_to is None
            and not normalized_tags
        ):
            hits = self._inject_title_matches(hits, q=q, kind=kind, page_size=page_size)

        # When a publish-date filter is active, count papers that match
        # the query but have NO year — they're silently dropped by the
        # range predicate. Surfacing the count keeps an empty/short
        # year-range result from reading as "nothing exists" when the
        # real cause is missing metadata (the corpus has many such rows).
        year_notice = ""
        if year_from is not None or year_to is not None:
            omitted = self.store.count_paper_yearless_matches(
                q=q,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                exclude_ref_ids=exclude_ref_ids or None,
            )
            if omitted:
                lo = str(year_from) if year_from is not None else "…"
                hi = str(year_to) if year_to is not None else "…"
                year_notice = (
                    f"\n\n⚠ {omitted} matching paper(s) omitted — no publication "
                    f"year on record, so the {lo}..{hi} filter can't place them. "
                    "Fix metadata via /papers/triage, or drop after=/before= to "
                    "include them."
                )

        total: int | None = None
        if hits:
            # Salience: heat the chunks this page surfaced (block-level).
            # One set-based bump; no-op for dream-actor reads.
            self.store.bump_salience([block.id for block, _ref, _score in hits])

            # Total-hits header: count blocks the lexical filter would
            # match without the LIMIT, so the agent sees "10 of K" when
            # paginated. RRF only re-ranks lexically-matching rows, so
            # the lexical count is the meaningful "K". (MCP critic
            # MAJOR #10b.)
            #
            # ``exclude_ref_ids`` is forwarded so the K reported in the
            # header is the *remaining* universe — i.e. when the caller
            # already excluded 5 papers, "10 of 42" tells them how many
            # hits are still left after their skip-list. Without this,
            # the header would lie ("10 of 47") and a 7B model would
            # think they had more to paginate through than actually
            # exist.
            #
            # Broad mode has no honest K: the lexical count of the primary
            # ``q`` is the wrong universe for a *fused* result set — the
            # rephrasing / HyDE legs surface rows q's tsquery never matches,
            # so K can be < len(hits) or 0 (which used to silently drop the
            # "of K" headline AND mis-gate the pagination trailer). Pass
            # total=None (headline renders the plain count, no "of K") and
            # gate the nav on ``broad_has_more`` instead.
            if not broad:
                total = self.store.count_blocks_lexical(
                    q=q,
                    kind=self.kind,
                    scope_ref_id=scope_ref_id,
                    tags=normalized_tags,
                    exclude_ref_ids=exclude_ref_ids or None,
                    card_kinds=("card_combined",),
                )

        return BlockSearchResult(
            kind=kind,
            q=q,
            page=page,
            page_size=page_size,
            scope=scope,
            hits=hits,
            year_notice=year_notice,
            broad=broad,
            broad_has_more=broad_has_more,
            extra_queries=extra_queries,
            hyde_answers=hyde_answers,
            per_paper_cap=per_paper_cap,
            total=total,
        )


@dataclass
class PaperSearchResultRenderer:
    """Renders a :class:`BlockSearchResult` into the agent-facing ``Response``.

    Pure formatting — no store calls. The empty-hits branch (DOI-shaped
    query detection + fetch-pipeline guidance) and the non-empty branch
    (headline + TOON table + pagination trailer) mirror the former
    ``PaperHandler.search()`` tail exactly.
    """

    kind: str

    def render(self, result: BlockSearchResult) -> Response:
        # Local import — see this module's docstring: paper.py is fully
        # loaded by the time a search actually runs (this module is only
        # imported lazily from inside ``PaperHandler.search()``).
        from precis.handlers.paper import _DOI_RE

        kind = self.kind
        q = result.q
        hits = result.hits
        year_notice = result.year_notice

        if not hits:
            # Use the canonical Next: block shape rather than an
            # inline ``next: ...`` prose line. The critic rules
            # (C17 / D12) say trailers share one delimiter style
            # across kinds, and the empty-search branch was the
            # last holdout of the lowercase-prose shape. (c5
            # unified-trailer patch.)
            #
            # DOI-shaped queries that miss are the dominant friction
            # case: agents fire 3-5 keyword variants trying to find a
            # paper that isn't in the corpus. Detect the DOI shape and
            # route to the structured stub-fetch pathway (finding +
            # ``precis worker --only fetch``) instead of suggesting a
            # wider search that will also miss.
            # Noun is the actual kind, not a hardcoded "paper": cfp and
            # datasheet subclass PaperHandler and reuse this search path,
            # so a literal "paper" leaked the wrong kind
            # (`no paper blocks match` on a cfp/datasheet search).
            body = f"no {kind} blocks match {q!r}"
            doi_match = _DOI_RE.match(q.strip())
            if doi_match is not None:
                doi = doi_match.group(1)
                body += "\n\nThis DOI is not in the local corpus. "
                body += (
                    "Pull it into the corpus via the finding-chase + "
                    "Unpaywall/arXiv/S2 fetcher pipeline:"
                )
                body += render_next_section(
                    [
                        (
                            "put(kind='finding', title='<short claim>', "
                            f"body='<claim + setup>', cited_in='doi:{doi}', "
                            "scope={'...': '...'})",
                            "register the DOI as a chase target; the "
                            "fetcher will try Unpaywall/arXiv/S2 next pass",
                        ),
                        (
                            "precis stubs --awaiting",
                            "list stub backlog the fetcher will work on",
                        ),
                        (
                            "edit(kind='plaintext', id='./request_doi.md', "
                            f"mode='append', text='{doi} - <one-line reason>\\n')",
                            "(legacy) append to the plaintext queue — "
                            "deprecated; use put(kind='finding') above",
                        ),
                    ]
                )
            else:
                body += render_next_section(
                    [
                        (
                            f"search(kind='{kind}', q={q!r}, page_size=50)",
                            "widen the lexical net",
                        ),
                        (
                            "get(kind='skill', id='precis-search-help')",
                            "broad retrieval: add queries=/answers= "
                            "rephrasings so a missed phrasing still hits",
                        ),
                    ]
                )
            return Response(body=body + year_notice)

        total = result.total
        broad = result.broad

        # Hits arrive sorted by RRF fused rank — best first. The
        # raw fused number is, in absolute terms, a 1/(k+rank)
        # staircase that doesn't reflect query strength — but
        # every other block-level handler (web, think, markdown,
        # plaintext, conversation, python) renders the same value
        # as ``(score=X.XXXX)``, so paper opting out left agents
        # with a kind-specific shape inconsistency. The MCP critic
        # flagged this 2026-05-02; aligning here at cost of one
        # potentially-misleading float per hit, balanced by uniform
        # downstream parsing across kinds.
        # Round-2 picky 2026-05-31: stripped to two columns —
        # ``{handle, chunk_keywords}``. Score is internal-only
        # (it determines RRF order, which is the row order;
        # surfacing the float adds no signal once the rows are
        # already sorted). Title was dropped because the agent has
        # ``get(kind='paper', id='<handle-slug>')`` for the full
        # title + meta; spending 30-60 chars per row on the same
        # title for repeated hits on the same paper is wasteful.
        # The cluster-context hint (Phase E, once segmentation
        # lands) will be a single trailing line rather than a
        # per-row column.
        head = format_search_headline(
            n_returned=len(hits),
            total=total,
            noun="block hit",
            query=q,
        )
        # Each hit renders as a TOON row with its own per-chunk KeyBERT
        # keywords (from ``chunks.keywords``). Per-chunk keywords carry
        # the "what's in this specific chunk" signal directly — the
        # earlier segment-level excerpt sub-line had to go via a
        # central-sentence picker that produced too much noise.
        table_rows: list[dict[str, str]] = []
        for block, ref, _score in hits:
            slug = ref.slug or "???"
            # ADR 0036: the computed chunk handle (``pc<chunk_id>``) is the
            # one address form; fall back to the legacy ``slug~pos`` only for
            # a kind with no chunk code.
            handle = (
                handle_registry.try_format(ref.kind, block.id, chunk=True)
                or f"{slug}~{block.pos}"
            )
            kw_list = block.keywords or []
            if kw_list:
                kw_display = ", ".join(kw_list[:5])
            else:
                chunk_text = _scrub_block_text(block.text)
                kw_display = _chunk_keywords_or_caption(chunk_text)
            table_rows.append(
                {
                    "handle": handle,
                    "chunk_keywords": kw_display,
                }
            )
        rendered_table = render_agent_table(
            table_rows,
            schema=["handle", "chunk_keywords"],
        )
        body = head + year_notice + "\n\n" + rendered_table

        # Pagination affordance — when the lexical total exceeds what
        # we returned, surface the narrow-with-scope path explicitly.
        # Without this trailer a 7B caller seeing ``# 100 of 101`` often
        # reads the header as "this is everything" and stops short of
        # hit #101. (MCP critic MAJOR — search has no pagination
        # affordance when capped.)
        #
        # Single ``Next:`` trailer for the whole search response.
        # Order (most actionable first):
        #   1. Drill-into-chunk: ``get(kind='paper', id=<handle>)`` —
        #      teaches that every handle in the table is a valid id.
        #   2. Pagination via ``exclude=`` (when more hits remain).
        #   3. Narrow-to-paper via ``scope=`` (multi-paper case).
        #   4. Salient-term refinement.
        # Round-2 picky 2026-05-30: previous code emitted two
        # separate ``Next:`` blocks back-to-back; merging keeps the
        # response shape consistent across kinds.
        nav: list[tuple[str, str]] = []
        if hits:
            # ADR 0036: point at the top hit by its computed chunk handle.
            first_handle = (
                handle_registry.try_format(hits[0][1].kind, hits[0][0].id, chunk=True)
                or f"{hits[0][1].slug or '???'}~{hits[0][0].pos}"
            )
            nav.append(
                (
                    f"get(id='{first_handle}')",
                    "read the full text of any hit (paste any handle above)",
                )
            )
            # F20: cluster-context navigation hint now uses the
            # dynamic TOC. Pointing at view='toc' on the paper gives
            # the agent the freshly-clustered structure for the
            # current corpus state — no segment-containing-chunk
            # lookup needed because clusters are computed on demand.

        # Singleton-hit special case (MCP critic MINOR-$): when
        # ``len(hits) == 1`` the original nav was 46 % of the response
        # and 100 % redundant — the scope suggestion narrowed to the
        # only hit's own paper (a no-op), and the salient-term
        # suggestion is moot when the caller already has a tight
        # match. Just bump ``page_size`` in that branch.
        # Broad mode: gate on the fused-aware signal (the candidate list
        # extended past this page's slice), not the lexical count — and
        # echo queries=/answers=/per_paper= into every continuation, or
        # a caller following the trailer would land on the *single-leg*
        # path's page 2: a different ordering with duplicates + gaps.
        broad_suffix = (
            _broad_args_suffix(
                result.extra_queries, result.hyde_answers, result.per_paper_cap
            )
            if broad
            else ""
        )
        more_available = (
            result.broad_has_more if broad else total is not None and total > len(hits)
        )
        if more_available:
            if len(hits) == 1:
                nav.append(
                    (
                        f"search(kind='{kind}', q={q!r}{broad_suffix}, page_size=10)",
                        "see more of the fused matches"
                        if broad
                        else f"see more of the {total} matches",
                    )
                )
            else:
                # ADR 0036: scope by the top hit's paper record handle.
                top_handle = (
                    handle_registry.format_handle("paper", hits[0][1].id)
                    if hits
                    else None
                )

                # Pagination continuation: bump page=N. page= is the
                # canonical pagination knob; exclude= is a hand-skip
                # filter for known-irrelevant refs (kept available via
                # the arg but no longer the recommended next-step
                # because it bloats the call with a slug list).
                show_next_page = (
                    result.broad_has_more
                    if broad
                    else total is not None and total > result.page_size * result.page
                )
                if show_next_page:
                    nav.append(
                        (
                            f"search(kind='{kind}', q={q!r}{broad_suffix}, "
                            f"page={result.page + 1})",
                            "see the next fused page (keep the same "
                            "queries=/answers= or the ordering shifts)"
                            if broad
                            else f"see the next {result.page_size} of {total} hits",
                        )
                    )

                if result.scope is None and top_handle is not None:
                    nav.append(
                        (
                            f"search(kind='{kind}', q={q!r}, scope='{top_handle}')",
                            f"narrow to blocks inside {top_handle}",
                        )
                    )
                # Round-2 picky F-9, 2026-05-30: previous wording was
                # ``q={q!r} + ' <salient term>'`` — Python-flavoured
                # pseudo-code that a literal-paste agent would send as
                # the verbatim string ``'cells' + ' <salient term>'``.
                # Spell the placeholder in pure-string form instead.
                nav.append(
                    (
                        f"search(kind='{kind}', q='{q} <salient term>')",
                        "tighten the query with a hit-specific token "
                        "(replace <salient term> with one)",
                    )
                )
                # Discoverability: teach the broad / high-recall path
                # from a plain search. Only when this *was* a simple
                # call (no queries=/answers=/per_paper=), so broad-mode
                # callers aren't nagged. Full mechanics in the skill.
                if (
                    not result.extra_queries
                    and not result.hyde_answers
                    and result.per_paper_cap is None
                ):
                    nav.append(
                        (
                            "get(kind='skill', id='precis-search-help')",
                            "broaden recall: fuse rephrasings + "
                            "hypothetical answers (queries=/answers=) "
                            "and spread across papers (per_paper=)",
                        )
                    )
                body += render_next_section(nav)

        return Response(body=body)


__all__ = [
    "_BROAD_LEG_CAP",
    "BlockSearchResult",
    "BylineSearch",
    "FusedBlockSearch",
    "PaperSearchResultRenderer",
    "_dedup_card_hits",
    "_normalise_exclude_slug",
]
