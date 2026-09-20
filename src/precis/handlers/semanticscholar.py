"""Semantic Scholar — cache-backed paper search + citation-graph nav.

The ``semanticscholar`` kind wraps Semantic Scholar's Graph API. The
``id`` selects between three modes:

* ``get(kind='semanticscholar', id='<query>')`` — a paper *search*:
  the top-10 ranked hits for a natural-language query, as a structured
  markdown listing (same shape as perplexity-*).
* ``get(kind='semanticscholar', id='refs:<paper-id>')`` — the papers
  *this* paper cites (its reference list / bibliography).
* ``get(kind='semanticscholar', id='cites:<paper-id>')`` — the papers
  that cite this one (its forward citations).

The ``refs:`` / ``cites:`` modes are how you **navigate a known
paper's citation graph** to discover a primary source the corpus
doesn't hold yet: every returned row carries the cited/citing paper's
DOI / arXiv id, which feeds straight into a
``put(kind='paper', doi=…)`` acquisition stub. ``<paper-id>`` is any
S2-resolvable handle — a bare DOI (``10.x/y``), an arXiv id
(``2401.00001``), a raw S2 paper hash, or an explicitly-prefixed
``DOI:`` / ``ARXIV:`` / ``CorpusId:`` / ``PMID:`` form.

Optional ``SEMANTIC_SCHOLAR_API_KEY`` env var raises the rate limit
from the public tier (~1 req/s) to the partner tier; the handler
works without one but is slower. We surface the missing-key state
as a one-time hint rather than an init failure.

Cache TTL: 30 days. S2 indexes new papers continuously but the
top-10 for a query — and a paper's reference list — are stable on
that timescale.
"""

from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult, _cite_pointer
from precis.handlers._exclude_closure import resolve_exclude_paper_ids
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.utils import handle_registry
from precis.utils.http import http_client, require_httpx
from precis.utils.slug import slug_from_text

if TYPE_CHECKING:
    from precis.store.types import CacheEntry, Ref

log = logging.getLogger(__name__)

_S2_PAPER_BASE = "https://api.semanticscholar.org/graph/v1/paper"
_S2_URL = f"{_S2_PAPER_BASE}/search"

#: Fields we request from the API. Trimmed to what we render — full
#: paper records carry citations/references which would inflate the
#: cached row 10x without a clear render use.
_S2_FIELDS = (
    "title,authors.name,year,abstract,externalIds,venue,"
    "citationCount,referenceCount,openAccessPdf"
)

#: How many top hits to RENDER (the corpus-diff survivor page). Keeps the
#: body bounded and the chunker-emitted chunks meaningful per-paper.
_S2_LIMIT = 10

#: How many hits to FETCH+CACHE from S2 for a plain topic search — well
#: above :data:`_S2_LIMIT` so an ``exclude=`` skip-list (or a corpus that
#: already holds several top hits) still leaves ``_S2_LIMIT`` survivors to
#: render instead of a thinned-out page (docs/backlog/
#: discovery-exclude-by-container.md — "over-fetch, render top-10
#: survivors"). Raw hits are cached in ``meta['papers']`` so the exclude/
#: corpus-diff pass re-runs at RENDER time on every call (exclude= is
#: per-call, the held corpus drifts) without re-fetching from S2.
_S2_FETCH_LIMIT = 30

#: Trigram-similarity floor for the "normalized title" corpus-diff tier —
#: the last-resort match when an S2 hit carries neither a DOI nor an arXiv
#: id. High bar (near-exact) so an unrelated paper that merely shares a
#: few title words never renders as a false ``held:``/``stub:`` match.
_TITLE_MATCH_FLOOR = 0.9

#: Per-paper fields for the citation-graph (references / citations)
#: endpoints. The nested paper record shares the search field shape,
#: so ``_format_paper`` renders both — we only trim ``referenceCount``
#: (not useful one hop out).
_NAV_FIELDS = (
    "title,authors.name,year,abstract,externalIds,venue,citationCount,openAccessPdf"
)

#: Page size for a citation-graph walk. Reference lists run to
#: hundreds; cap the cached body so it stays a bounded, scannable
#: page (the agent is hunting for one missing source, not archiving
#: the whole bibliography). Truncation is surfaced in the meta.
_NAV_LIMIT = 50

#: ``id=`` prefixes that switch ``get`` from search to a graph walk.
#: ``refs`` → papers this one cites; ``cites`` → papers citing it.
_NAV_PREFIXES = ("refs", "cites")

#: Author-navigation prefixes — the bridge into
#: ``kind='orcid'`` and the outbound frontier for author-network BFS.
#: ``authors:<paper-id>`` → that paper's authors (each carrying their
#: ORCID + hIndex + affiliations, senior author flagged by position);
#: ``author:<authorId>`` → that author's top papers.
_AUTHOR_PREFIXES = ("authors", "author")

_S2_AUTHOR_BASE = "https://api.semanticscholar.org/graph/v1/author"

#: Fields for ``/paper/{id}/authors`` — name + the externalIds that
#: surface ORCID (the key into kind='orcid') + hIndex + affiliations.
_PAPER_AUTHORS_FIELDS = "name,externalIds,hIndex,affiliations"

#: Fields for ``/author/{id}/papers`` — the outbound BFS frontier.
#: ``paperId`` backs the compact table's ``s2 id`` column.
_AUTHOR_PAPERS_FIELDS = (
    "title,year,externalIds,venue,citationCount,authors.name,paperId"
)

#: Page size for an author's default (non-``complete``) paper list.
_AUTHOR_PAPERS_LIMIT = 50

#: Suffix appended to an ``author:<id>`` canonical key when
#: ``args={'complete': True}`` — gives the walked listing its own cache
#: row, distinct from the capped default page (gr346833).
_AUTHOR_COMPLETE_SUFFIX = ":complete"

#: Page size for the ``complete=True`` offset/limit walk — S2's own max.
_AUTHOR_COMPLETE_PAGE_LIMIT = 100

#: Hard stop on a ``complete=True`` walk regardless of how many works
#: the author actually has — one ``get()`` must never turn into an
#: unbounded fetch.
_AUTHOR_COMPLETE_HARD_CAP = 2000

#: Retry budget for a 429 hit mid-``complete=True`` walk (exponential
#: backoff: 1s, 2s, 4s, 8s between the 5 attempts).
_AUTHOR_BACKOFF_ATTEMPTS = 5
_AUTHOR_BACKOFF_BASE_S = 1.0

#: Title truncation for the compact author-papers table (gr346833).
_AUTHOR_TITLE_TRUNCATE = 70

#: In ``complete=True`` mode, cap on actual title-similarity fallback
#: lookups :meth:`SemanticScholarHandler._corpus_flags_bulk` will run —
#: up to 2000 works, mostly identifier-less, would otherwise fire up to
#: 2000 serial trigram queries. Works past the cap render ``?`` in the
#: table's corpus column instead of a false ``NEW``. The default
#: (≤50-work) path is uncapped — unchanged from before this review.
_AUTHOR_COMPLETE_TITLE_FALLBACK_CAP = 200

#: Bare-arXiv-id shape (new-style ``2401.00001`` with optional ``vN``).
#: Used to auto-prefix a path id when the caller passes a naked id.
_ARXIV_RE = re.compile(r"^\d{4}\.\d{4,5}(v\d+)?$")

_ATTRIBUTION = (
    "Source: Semantic Scholar (https://www.semanticscholar.org). "
    "Each cited paper is an external work — verify and cite the "
    "primary paper, not this aggregator query."
)


class SemanticScholarHandler(CacheBackedHandler):
    """``semanticscholar`` — paper search via the S2 Graph API.

    A plain topic search (``id='<query>'``) ALWAYS diffs its hits against
    the held corpus (DOI → arXiv id → normalized title) and renders each
    one flagged ``held: pa…`` / ``stub: pa…`` / ``NEW`` — no more guessing
    whether a promising S2 hit is something the corpus already has.
    ``exclude=`` additionally drops papers already cited by a container:
    a paper slug/id (today's ``search(kind='paper')`` behavior), a whole
    draft (``dr…``), or a draft-chunk subtree (``dc…``) — resolved via the
    shared cite-closure walk (:mod:`precis.handlers._exclude_closure`).
    See docs/backlog/discovery-exclude-by-container.md.
    """

    #: Set for the duration of one ``get()`` call — the container-aware
    #: exclude= entries this request wants dropped from a topic-search
    #: render. ``get()`` isn't part of the base ``CacheBackedHandler``
    #: signature (it only accepts the shared cache-flow kwargs), so this
    #: mirrors the ``_pending_*`` convention other handlers use (e.g.
    #: ``memory.py``'s ``_pending_title``) to thread a subclass-only kwarg
    #: through to ``_render`` without changing the base class's contract.
    _pending_exclude: list[str] | None = None

    #: Set for the duration of one ``get()`` call — mirrors
    #: ``_pending_exclude`` above. Read by :meth:`_canonical_key` (only
    #: reachable on the ``author:`` nav prefix) to fold ``complete=True``
    #: into a distinct cache key, so a ``complete=True`` walk never reads
    #: back a stale capped-at-50 row (or vice versa).
    _pending_complete: bool = False

    spec: ClassVar[KindSpec] = KindSpec(
        kind="semanticscholar",
        title="Semantic Scholar paper search",
        description=(
            "Search Semantic Scholar's paper graph by natural-language "
            "query (over-fetches ~30, renders the top 10 survivors after "
            "corpus-diffing: each hit flagged held:pa…/stub:pa…/NEW with "
            "title, authors, year, DOI / arXiv id, venue, abstract, "
            "citation count). exclude=['dr…'|'dc…'|paper-slug] drops "
            "papers already cited by a draft/subtree/paper-list — see "
            "get(kind='skill', id='precis-stubs-help'). OR walk a "
            "known paper's citation graph: id='refs:<paper-id>' lists the "
            "papers it cites, id='cites:<paper-id>' the papers citing "
            "it — each row carrying the DOI to feed a "
            "put(kind='paper', doi=…) acquisition stub. Or walk the "
            "author graph: id='authors:<paper-id>' lists that paper's "
            "authors (each with their ORCID — the key into kind='orcid' — "
            "h-index, affiliations, senior author flagged), "
            "id='author:<authorId>' that author's works as one compact "
            "table (year/cites/corpus/title/s2-id, ranked by citations, "
            "capped at 50 by default — args={'complete': True} walks the "
            "full paginated list, up to 2000, instead)."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
    )

    # Must match a row in the `providers` table. The Semantic Scholar
    # provider is registered under the slug `s2` (0001_initial seed) —
    # stamping the literal `semanticscholar` here violated the
    # refs.provider FK on every cache write, so the kind raised a
    # ForeignKeyViolation after a successful API fetch (gripe #39242).
    provider: ClassVar[str] = "s2"
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60  # 30 days
    attribution: ClassVar[str] = _ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "carbon nanotube field-effect transistors"
    #: Per-call cost — the public tier is free, partner tier charges
    #: per query but it's tiny. Record None so the dashboard doesn't
    #: invent a per-call dollar figure.
    cost_per_call_usd: ClassVar[float] = 0.0

    # ── cache key + slug ──────────────────────────────────────────────

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        q = (query or "").strip()
        if not q:
            raise BadInput(
                "semanticscholar requires a non-empty query",
                next="get(kind='semanticscholar', id='your search terms')",
            )
        # Lower-case + collapse whitespace so the same query in
        # different casings shares one cache row. Identifiers under a
        # nav prefix lower-case safely too (DOIs are case-insensitive,
        # arXiv ids numeric, S2 hashes lower-hex).
        low = " ".join(q.lower().split())
        for mode in (*_NAV_PREFIXES, *_AUTHOR_PREFIXES):
            prefix = f"{mode}:"
            if low.startswith(prefix):
                ident = low[len(prefix) :].strip()
                if not ident:
                    needs = (
                        "an S2 authorId"
                        if mode == "author"
                        else "a paper id (DOI / arXiv / S2)"
                    )
                    example = (
                        f"{prefix}1741101"
                        if mode == "author"
                        else f"{prefix}10.1038/nature12373"
                    )
                    raise BadInput(
                        f"semanticscholar {prefix} needs {needs}",
                        next=f"get(kind='semanticscholar', id='{example}')",
                    )
                if mode == "author" and self._pending_complete:
                    ident = f"{ident}{_AUTHOR_COMPLETE_SUFFIX}"
                return f"{prefix}{ident}"
        return low

    @staticmethod
    def _parse_nav_key(key: str) -> tuple[str, str] | None:
        """Split a canonical key into ``(mode, ident)`` or ``None``.

        ``None`` is the plain-search path; ``('refs', '10.x/y')`` /
        ``('cites', '10.x/y')`` are the two graph-walk paths.
        """
        for mode in (*_NAV_PREFIXES, *_AUTHOR_PREFIXES):
            prefix = f"{mode}:"
            if key.startswith(prefix):
                return mode, key[len(prefix) :]
        return None

    @staticmethod
    def _s2_path_id(ident: str) -> str:
        """Map an agent-supplied paper id to an S2 graph path segment.

        S2's ``/paper/{id}`` accepts a bare hash or a prefixed handle
        (``DOI:`` / ``ARXIV:`` / ``CorpusId:`` / ``PMID:`` / …). We let
        an already-prefixed id through (normalising the two common
        casings) and auto-prefix a naked DOI or arXiv id; anything else
        is assumed to be a raw S2 paper hash.
        """
        r = ident.strip()
        low = r.lower()
        if low.startswith("doi:"):
            return "DOI:" + r[4:]
        if low.startswith("arxiv:"):
            return "ARXIV:" + r[6:]
        if low.startswith("s2:"):
            return r[3:]  # bare S2 paper hash, no prefix in the path
        if low.startswith(("corpusid:", "pmid:", "pmcid:", "mag:", "acl:", "url:")):
            return r  # S2 accepts these verbatim
        if r.startswith("10."):
            return "DOI:" + r
        if _ARXIV_RE.match(r):
            return "ARXIV:" + r
        return r  # assume a raw S2 paper hash

    def _slug_for(self, key: str) -> str:
        return slug_from_text(key, max_len=60) or "semanticscholar-query"

    def _recover_key(self, ref, cache):
        meta = cache.meta or {}
        # New rows stamp the canonical key directly; fall back to the
        # legacy ``query`` field for search rows written before that.
        return meta.get("key") or meta.get("query")

    # ── exclude= plumbing ─────────────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        exclude: list[str] | None = None,
        view: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        mode: str | None = None,
        ttl_days: int | None = None,
        refresh: bool = False,
        no_fetch: bool = False,
        literal: bool = False,
        complete: bool = False,
        **_kw: Any,
    ) -> Response:
        """Like the base ``get()``, plus ``exclude=`` for a plain topic
        search: drop papers already cited by a container (paper slug/id,
        ``dr…`` whole draft, ``dc…`` draft-chunk subtree — see
        :func:`precis.handlers._exclude_closure.resolve_exclude_paper_ids`).
        Meaningless (silently ignored) on the ``refs:``/``cites:``/
        ``authors:`` graph-walk modes, which carry no per-paper corpus-diff
        render to filter.

        ``complete=True`` (``args={'complete': True}``) is ``author:``-only
        (gr346833): instead of the default capped 50-work page, walks the
        S2 API's full offset/limit pagination — see :meth:`_fetch_author_papers`.
        """
        self._pending_exclude = exclude
        self._pending_complete = complete
        try:
            return super().get(
                id=id,
                q=q,
                view=view,
                tags=tags,
                untags=untags,
                mode=mode,
                ttl_days=ttl_days,
                refresh=refresh,
                no_fetch=no_fetch,
                literal=literal,
                **_kw,
            )
        finally:
            self._pending_exclude = None
            self._pending_complete = False

    def _render(self, ref: Ref, cache: CacheEntry, *, hit: bool) -> Response:
        """Corpus-diff render for a plain topic-search ref; everything
        else (graph-walk modes, the injection-withheld/suspect banners)
        defers to the base class unchanged.

        Discriminator: only :meth:`_fetch_search` stamps ``meta['papers']``
        — the graph-walk fetchers (`_fetch_graph` / `_fetch_paper_authors`
        / `_fetch_author_papers`) never do, so they always fall through to
        ``super()._render()``. A ``high`` injection verdict also defers to
        the base class, which withholds the body — that gate must apply
        regardless of which render path would otherwise run.
        """
        meta = cache.meta or {}
        inject = meta.get("inject") or {}
        papers = meta.get("papers")
        if inject.get("verdict") == "high" or not papers:
            return super()._render(ref, cache, hit=hit)
        return self._render_topic_search(ref, cache, papers, hit=hit)

    def _render_topic_search(
        self,
        ref: Ref,
        cache: CacheEntry,
        papers: list[dict[str, Any]],
        *,
        hit: bool,
    ) -> Response:
        # ``.dead_handles`` (gr340059) isn't surfaced here — this view has
        # no exclude= notice slot; the single-kind paper search path is
        # where a dead exclude= handle gets a footer.
        exclude_ids = resolve_exclude_paper_ids(self._pending_exclude, store=self.store)
        flags = self._corpus_flags_bulk(papers)
        lines = [f"# {ref.title}", ""]
        shown = held_n = stub_n = new_n = excluded_n = 0
        for p, (flag, matched_ref_id) in zip(papers, flags, strict=True):
            if shown >= _S2_LIMIT:
                break
            if matched_ref_id is not None and matched_ref_id in exclude_ids:
                excluded_n += 1
                continue
            shown += 1
            if flag == "NEW":
                new_n += 1
            elif flag.startswith("held"):
                held_n += 1
            else:
                stub_n += 1
            lines.append(_format_paper_with_flag(p, flag))
        if shown == 0:
            lines.append(
                "_(every fetched hit was excluded — widen exclude= or the query.)_"
            )
        lines.append("")
        counts = f"{shown} shown ({new_n} NEW, {held_n} held, {stub_n} stub"
        if excluded_n:
            counts += f", {excluded_n} excluded"
        counts += f") of {len(papers)} fetched"
        lines.append(f"_{counts}._")
        if new_n:
            lines.append(
                "\nAccept a NEW hit: `put(kind='paper', doi='<doi>')` (or "
                "`arxiv=`/`title=`) — mints a DREAM:acquire stub the "
                "fetch_oa worker picks up automatically."
            )
        lines.append("")
        lines.append(f"- {self.attribution}")
        cite = _cite_pointer(self.spec.kind, ref.id)
        if cite:
            lines.append(cite)
        return Response(body="\n".join(lines), cost=self._cost_str(cache, hit=hit))

    def _corpus_flags_bulk(
        self,
        papers: list[dict[str, Any]],
        *,
        title_fallback_cap: int | None = None,
    ) -> list[tuple[str, int | None]]:
        """Diff every fetched S2 hit against the held corpus — DOI → arXiv
        id → normalized title (the order docs/backlog/
        discovery-exclude-by-container.md specifies) — in a BOUNDED
        number of queries regardless of ``len(papers)`` (this runs on
        EVERY call, including a cache hit, so the render stays cheap
        even though it re-diffs from scratch each time):

        1. ONE bulk identifier lookup
           (:meth:`Store.find_paper_refs_by_identifiers_bulk`) over every
           hit's DOI/arXiv id at once.
        2. A per-hit trigram title query ONLY for hits an identifier
           didn't resolve (the fallback tier — rare, since S2 hits mostly
           carry a DOI or arXiv id). ``title_fallback_cap`` bounds how many
           of THESE per-hit queries actually run — needed by the
           ``author:``/``complete=True`` walk (gr346833 follow-up), where
           ``len(papers)`` can run to 2000 and an uncapped fallback would
           fire up to 2000 serial trigram queries. Candidates beyond the
           cap are left unresolved (flagged ``'UNRESOLVED'`` below) rather
           than silently skipped as ``'NEW'`` — ``'NEW'`` means "checked,
           not found"; these were never checked.
        3. ONE bulk :meth:`Store.fetch_refs_by_ids` over every distinct
           matched ``ref_id`` to read back ``pdf_sha256`` (held vs. stub).

        Returns one ``(flag, matched_ref_id)`` pair per input paper, same
        order — ``flag`` is ``'NEW'`` / ``'UNRESOLVED'`` (title-fallback
        budget exhausted before this candidate) or ``'held: pa…'`` /
        ``'stub: pa…'``; ``matched_ref_id`` is ``None`` except on the
        ``held``/``stub`` states, where it's the paper's ``ref_id`` (what
        ``exclude=`` closure ids compare against).
        """

        def _doi_arxiv(p: dict[str, Any]) -> tuple[str, str]:
            ext = p.get("externalIds") or {}
            doi = str(ext.get("DOI") or "").strip()
            arxiv = str(ext.get("ArXiv") or "").strip()
            return doi, arxiv

        id_values = [v for p in papers for v in _doi_arxiv(p) if v]
        id_map = (
            self.store.find_paper_refs_by_identifiers_bulk(id_values)
            if id_values
            else {}
        )

        matched_ref_ids: list[int | None] = []
        for p in papers:
            doi, arxiv = _doi_arxiv(p)
            matched_ref_ids.append(id_map.get(doi) if doi else None)
            if matched_ref_ids[-1] is None and arxiv:
                matched_ref_ids[-1] = id_map.get(arxiv)

        # Fallback tier: only the hits an identifier left unresolved pay a
        # (per-hit, unavoidable — trigram similarity has no bulk form here)
        # title query — capped at ``title_fallback_cap`` actual lookups
        # when set; candidates past the cap land in ``unresolved_idx``.
        unresolved_idx: set[int] = set()
        title_lookups_done = 0
        for i, p in enumerate(papers):
            if matched_ref_ids[i] is not None:
                continue
            title = str(p.get("title") or "").strip()
            if not title:
                continue
            if (
                title_fallback_cap is not None
                and title_lookups_done >= title_fallback_cap
            ):
                unresolved_idx.add(i)
                continue
            title_lookups_done += 1
            title_hits = self.store.find_refs_by_title_similarity(
                kind="paper", q=title, limit=1, min_similarity=_TITLE_MATCH_FLOOR
            )
            if title_hits:
                matched_ref_ids[i] = title_hits[0][0]

        unique_ids = {rid for rid in matched_ref_ids if rid is not None}
        refs_map = self.store.fetch_refs_by_ids(list(unique_ids)) if unique_ids else {}

        flags: list[tuple[str, int | None]] = []
        for i, rid in enumerate(matched_ref_ids):
            if i in unresolved_idx:
                flags.append(("UNRESOLVED", None))
                continue
            matched = refs_map.get(rid) if rid is not None else None
            if matched is None:  # NEW, or vanished between resolve + fetch
                flags.append(("NEW", None))
                continue
            handle = handle_registry.try_format("paper", rid) or f"pa{rid}"
            state = "held" if matched.pdf_sha256 is not None else "stub"
            flags.append((f"{state}: {handle}", rid))
        return flags

    # ── fetch + render ────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        nav = self._parse_nav_key(key)
        if nav is not None:
            mode, ident = nav
            if mode == "authors":
                return self._fetch_paper_authors(key, ident)
            if mode == "author":
                return self._fetch_author_papers(key, ident)
            return self._fetch_graph(key, mode, ident)
        return self._fetch_search(key)

    def _fetch_paper_authors(self, key: str, ident: str) -> FetchResult:
        """List a paper's authors — the bridge into ORCID.

        Surfaces each author's ORCID (the key for kind='orcid'), hIndex,
        and affiliations, and flags the **senior (last) author** by
        position — the densest vein for an author-network BFS.
        """
        path_id = self._s2_path_id(ident)
        url = f"{_S2_PAPER_BASE}/{path_id}/authors"
        data = self._s2_get_json(url, {"fields": _PAPER_AUTHORS_FIELDS, "limit": 100})
        authors = data.get("data") or []
        if not authors:
            text = f"No authors found for {ident} on Semantic Scholar."
            return FetchResult(
                title=f"S2 authors: {ident}",
                body_blocks=[ChunkInsert(ord=0, text=text)],
                cost_usd=None,
                meta={"key": key, "nav": "authors", "paper": ident, "result_count": 0},
            )
        n = len(authors)
        blocks = [
            ChunkInsert(
                ord=i,
                text=_format_author(a, position=i, n_authors=n),
            )
            for i, a in enumerate(authors)
        ]
        return FetchResult(
            title=f"S2 authors of {ident} ({n})",
            body_blocks=blocks,
            cost_usd=None,
            meta={
                "key": key,
                "nav": "authors",
                "paper": ident,
                "result_count": n,
            },
        )

    def _fetch_author_papers(self, key: str, ident: str) -> FetchResult:
        """List an author's works — the BFS frontier — as one compact,
        citation-ranked table with a per-row corpus status.

        Default: the same single S2 page as before (≤``_AUTHOR_PAPERS_LIMIT``
        works), just re-rendered as the table. ``args={'complete': True}``
        (``ident`` carrying the ``:complete`` suffix minted by
        :meth:`_canonical_key`) instead WALKS the S2 API's full offset/
        limit pagination via :meth:`_walk_author_papers` — this is what
        surfaced a 190-citation JACS paper the 50-work cap silently hid
        from an author search (gr346833).
        """
        complete = ident.endswith(_AUTHOR_COMPLETE_SUFFIX)
        author_id = (
            ident[: -len(_AUTHOR_COMPLETE_SUFFIX)] if complete else ident
        ).strip()
        url = f"{_S2_AUTHOR_BASE}/{author_id}/papers"

        stopped_at_cap = False
        total_hint: int | None = None
        requests = 0
        retried = 0
        if complete:
            papers, stopped_at_cap, requests, retried = self._walk_author_papers(url)
        else:
            data = self._s2_get_json(
                url, {"fields": _AUTHOR_PAPERS_FIELDS, "limit": _AUTHOR_PAPERS_LIMIT}
            )
            papers = data.get("data") or []
            reported = data.get("total")
            total_hint = reported if isinstance(reported, int) else None

        if not papers:
            text = f"No papers found for author {author_id} on Semantic Scholar."
            return FetchResult(
                title=f"S2 author papers: {author_id}",
                body_blocks=[ChunkInsert(ord=0, text=text)],
                cost_usd=None,
                meta={
                    "key": key,
                    "nav": "author",
                    "author": author_id,
                    "result_count": 0,
                    "complete": complete,
                },
            )

        n = len(papers)
        capped = (not complete) and n >= _AUTHOR_PAPERS_LIMIT
        if complete:
            flags = self._corpus_flags_bulk(
                papers, title_fallback_cap=_AUTHOR_COMPLETE_TITLE_FALLBACK_CAP
            )
        else:
            flags = self._corpus_flags_bulk(papers)
        table = _format_author_papers_table(papers, flags)
        has_unresolved = any(flag == "UNRESOLVED" for flag, _rid in flags)

        if complete:
            req_note = f"{requests} request{'' if requests == 1 else 's'}"
            if retried:
                req_note += f", {retried} retried"
            note = f"_{n} works (complete, {req_note})._"
            if stopped_at_cap:
                note += (
                    f" _stopped at {_AUTHOR_COMPLETE_HARD_CAP} works — more remain._"
                )
            if has_unresolved:
                note += (
                    " _? = not resolvable by id; title fallback capped at "
                    f"{_AUTHOR_COMPLETE_TITLE_FALLBACK_CAP}._"
                )
            title = f"S2 papers by author {author_id} — {n} works (complete)"
        elif capped:
            n_of = str(total_hint) if total_hint is not None else f"{n}+"
            note = f"showing {n} of {n_of} — args={{'complete': True}} for all"
            title = f"S2 papers by author {author_id} ({n} shown)"
        else:
            note = f"_{n} works._"
            title = f"S2 papers by author {author_id} ({n} shown)"

        # Two SEPARATE blocks, not one "note\n\ntable" blob — a single
        # blob over ``chunk_target_chars`` gets auto-split
        # (``_split_body_blocks``) with a carried-forward overlap tail;
        # the short ``note`` paragraph is below the seam-dedupe floor
        # (``_MIN_SEAM_OVERLAP`` in _cache_base.py), so the render used
        # to reconstruct it as two consecutive identical lines under the
        # title (gr356740). Splitting the table on its own — independent
        # of ``note`` — means the note is never a candidate for that
        # seam at all.
        return FetchResult(
            title=title,
            body_blocks=[
                ChunkInsert(ord=0, text=note),
                ChunkInsert(ord=1, text=table),
            ],
            cost_usd=None,
            meta={
                "key": key,
                "nav": "author",
                "author": author_id,
                "result_count": n,
                "complete": complete,
                "capped": capped,
                "stopped_at_cap": stopped_at_cap,
                "s2_requests": requests,
                "s2_requests_retried": retried,
            },
        )

    def _walk_author_papers(
        self, url: str
    ) -> tuple[list[dict[str, Any]], bool, int, int]:
        """Walk every page of an author's works via S2's offset/limit
        pagination (``_AUTHOR_COMPLETE_PAGE_LIMIT`` per call), following
        ``next`` until the API reports exhaustion (no ``next`` key) or
        ``_AUTHOR_COMPLETE_HARD_CAP`` works have been collected — an
        author with a very long tail must never turn one ``get()`` into
        an unbounded fetch. A 429 mid-walk is retried with exponential
        backoff (:meth:`_s2_get_json_backoff`) rather than aborting the
        whole walk.

        Dedupes by ``paperId`` across pages (a repeat/replayed page must
        not double-count toward the hard cap or the render) and bails out
        — logging one warning, not raising — if the API ever reports a
        ``next`` that doesn't advance past the offset just fetched, which
        would otherwise loop forever re-requesting the same page.

        Returns ``(papers, stopped_at_cap, requests, retried)``:
        ``stopped_at_cap`` is True only when the hard cap was hit AND the
        API says more remain (a ``next``-less final page landing exactly
        on the cap is a normal exhaustion, not a truncation);
        ``requests`` is the total count of actual HTTP calls the walk
        made (one per page, plus one per 429 :meth:`_s2_get_json_backoff`
        retried) and ``retried`` is how many of those were 429 retries —
        both surfaced in the render so the cost of a ``complete=True``
        walk is visible (gr356740).
        """
        papers: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        offset = 0
        requests = 0
        retried = 0
        while True:
            data, attempts = self._s2_get_json_backoff(
                url,
                {
                    "fields": _AUTHOR_PAPERS_FIELDS,
                    "limit": _AUTHOR_COMPLETE_PAGE_LIMIT,
                    "offset": offset,
                },
            )
            requests += attempts
            retried += attempts - 1
            page = data.get("data") or []
            if not page:
                break
            new_count = 0
            for work in page:
                paper_id = work.get("paperId")
                if paper_id and paper_id in seen_ids:
                    continue  # duplicate (replayed page) — never re-counted
                if paper_id:
                    seen_ids.add(paper_id)
                papers.append(work)
                new_count += 1
            nxt = data.get("next")
            if len(papers) >= _AUTHOR_COMPLETE_HARD_CAP:
                return (
                    papers[:_AUTHOR_COMPLETE_HARD_CAP],
                    nxt is not None,
                    requests,
                    retried,
                )
            if new_count == 0:
                # Every work on this page was already seen — a replayed
                # duplicate page. Nothing more to gain from continuing.
                break
            if nxt is None:
                break
            if nxt <= offset:
                log.warning("S2 returned a non-advancing offset at %s; stopped", nxt)
                break
            offset = nxt
        return papers, False, requests, retried

    def _s2_get_json_backoff(
        self, url: str, params: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        """Like :meth:`_s2_get_json`, but retries an HTTP 429 with
        exponential backoff (:data:`_AUTHOR_BACKOFF_BASE_S` doubling,
        :data:`_AUTHOR_BACKOFF_ATTEMPTS` attempts) instead of raising
        immediately — the 5th (last) consecutive 429 raises rather than
        sleeping again, with a "retries exhausted" message distinct from
        :meth:`_s2_parse_response`'s generic 429 message.

        Returns ``(json, attempts)`` — ``attempts`` is how many actual
        HTTP calls this one page needed (1 when no 429 was hit, more
        when it was retried) — :meth:`_walk_author_papers` sums these
        across pages for the ``complete=True`` render's request count.

        Only the ``complete=True`` author walk uses this — it makes
        several sequential calls and is far likelier to cross the public
        tier's ~1 req/s ceiling than the other (single-call) fetch paths,
        which keep the immediate-raise behaviour of :meth:`_s2_get_json`.
        """
        last_attempt = _AUTHOR_BACKOFF_ATTEMPTS - 1
        for attempt in range(_AUTHOR_BACKOFF_ATTEMPTS):
            resp = self._s2_raw_get(url, params)
            if resp.status_code == 429:
                if attempt == last_attempt:
                    raise Upstream(
                        "Semantic Scholar rate-limited (HTTP 429): retries exhausted"
                    )
                time.sleep(_AUTHOR_BACKOFF_BASE_S * (2**attempt))
                continue
            return self._s2_parse_response(resp), attempt + 1
        raise AssertionError("unreachable")  # loop always returns/raises above

    @staticmethod
    def _s2_raw_get(url: str, params: dict[str, Any]) -> Any:
        """Issue one GET against the S2 Graph API and return the raw
        ``httpx.Response`` — no status/JSON handling. Shared by
        :meth:`_s2_get_json` (single-shot, raises immediately on 429) and
        :meth:`_s2_get_json_backoff` (retrying multi-page walk).
        """
        httpx = require_httpx()
        from precis.secrets import get_secret

        api_key = (get_secret("SEMANTIC_SCHOLAR_API_KEY") or "").strip()
        headers: dict[str, str] = {}
        if api_key:
            headers["x-api-key"] = api_key
        try:
            with http_client(timeout=30.0, headers=headers) as client:
                return client.get(url, params=params)
        except httpx.HTTPError as exc:
            raise Upstream(f"Semantic Scholar transport error: {exc}") from exc

    @staticmethod
    def _s2_parse_response(resp: Any) -> dict[str, Any]:
        """Status-check + JSON-parse a raw S2 response (or raise)."""
        if resp.status_code == 429:
            raise Upstream(
                "Semantic Scholar rate-limited (HTTP 429); the public tier "
                "is ~1 req/s. Set SEMANTIC_SCHOLAR_API_KEY for the partner "
                "tier or retry later.",
            )
        if resp.status_code == 401:
            raise Upstream(
                "Semantic Scholar rejected the API key (HTTP 401). "
                "Check SEMANTIC_SCHOLAR_API_KEY.",
            )
        if resp.status_code == 404:
            raise Upstream(
                "Semantic Scholar has no record for that paper id (HTTP 404). "
                "Check the DOI / arXiv / S2 id you passed after refs:/cites:.",
            )
        if resp.status_code != 200:
            raise Upstream(
                f"Semantic Scholar HTTP {resp.status_code}: {resp.text[:200]}"
            )

        try:
            return resp.json()
        except Exception as exc:
            raise Upstream(f"Semantic Scholar returned non-JSON: {exc}") from exc

    @staticmethod
    def _s2_get_json(url: str, params: dict[str, Any]) -> dict[str, Any]:
        """Issue one S2 Graph GET and return parsed JSON (or raise).

        Shared by the search and citation-graph paths so the
        rate-limit / auth / transport handling lives in one place.
        """
        resp = SemanticScholarHandler._s2_raw_get(url, params)
        return SemanticScholarHandler._s2_parse_response(resp)

    def _fetch_search(self, key: str) -> FetchResult:
        # Over-fetch (``_S2_FETCH_LIMIT`` ~30) vs. what actually renders
        # (``_S2_LIMIT`` 10) — the corpus-diff / exclude= filtering runs at
        # RENDER time (see :meth:`_render`), on every call, not here: a
        # cache hit days later must re-diff against whatever the corpus
        # holds NOW and whatever THIS call's exclude= says, not what was
        # true at fetch time. Caching the raw hits (below, ``meta['papers']``)
        # instead of a pre-filtered page is what makes that possible.
        params = {"query": key, "fields": _S2_FIELDS, "limit": _S2_FETCH_LIMIT}
        data = self._s2_get_json(_S2_URL, params)

        papers = data.get("data") or []
        if not papers:
            text = f'No Semantic Scholar results for "{key}".'
            return FetchResult(
                title=f"Semantic Scholar: {key}",
                body_blocks=[ChunkInsert(ord=0, text=text)],
                cost_usd=None,
                meta={"key": key, "query": key, "result_count": 0, "papers": []},
            )

        # One block per paper — the base-class auto-chunker would split
        # a single long blob anyway, but emitting per-paper blocks keeps
        # the chunk-level granularity meaningful for citation surface
        # (the chunk's text *is* a paper's entry, not a fragment of one).
        # These blocks back the plain block-level search()/search_hits()
        # surface (base class) and the injection scan; the agent-facing
        # `get()` render instead reads ``meta['papers']`` (below) so the
        # corpus-diff flags stay live across cache hits.
        blocks: list[ChunkInsert] = []
        for i, p in enumerate(papers):
            blocks.append(ChunkInsert(ord=i, text=_format_paper(p)))

        return FetchResult(
            title=f"Semantic Scholar: {key}",
            body_blocks=blocks,
            cost_usd=None,
            meta={
                "key": key,
                "query": key,
                "result_count": len(papers),
                "total_available": data.get("total"),
                # Raw S2 hit dicts (JSON-serialisable — the API response
                # shape verbatim) — the render-time corpus-diff pass reads
                # this instead of re-parsing the formatted block text.
                "papers": papers,
            },
        )

    def _fetch_graph(self, key: str, mode: str, ident: str) -> FetchResult:
        """Walk one hop of a paper's citation graph.

        ``mode='refs'`` → ``/paper/{id}/references`` (papers this one
        cites); ``mode='cites'`` → ``/paper/{id}/citations`` (papers
        citing it). The endpoint returns the *neighbour* paper nested
        under ``citedPaper`` / ``citingPaper``; we lift it out and
        render each with the shared per-paper formatter.
        """
        endpoint = "references" if mode == "refs" else "citations"
        nested = "citedPaper" if mode == "refs" else "citingPaper"
        verb = "cited by" if mode == "refs" else "citing"
        path_id = self._s2_path_id(ident)
        url = f"{_S2_PAPER_BASE}/{path_id}/{endpoint}"
        data = self._s2_get_json(url, {"fields": _NAV_FIELDS, "limit": _NAV_LIMIT})

        rows = data.get("data") or []
        papers = [
            row[nested]
            for row in rows
            if isinstance(row, dict) and isinstance(row.get(nested), dict)
        ]
        if not papers:
            text = f"No {endpoint} found for {ident} on Semantic Scholar."
            return FetchResult(
                title=f"S2 {endpoint}: {ident}",
                body_blocks=[ChunkInsert(ord=0, text=text)],
                cost_usd=None,
                meta={"key": key, "nav": mode, "paper": ident, "result_count": 0},
            )

        blocks = [
            ChunkInsert(ord=i, text=_format_paper(p)) for i, p in enumerate(papers)
        ]
        # Title says which way the hop runs + how many we kept, so a
        # capped page reads as "first N", not "the complete list".
        suffix = (
            f" ({len(papers)} shown, capped at {_NAV_LIMIT})"
            if (len(papers) >= _NAV_LIMIT)
            else f" ({len(papers)})"
        )
        return FetchResult(
            title=f"S2 papers {verb} {ident}{suffix}",
            body_blocks=blocks,
            cost_usd=None,
            meta={
                "key": key,
                "nav": mode,
                "paper": ident,
                "result_count": len(papers),
                "capped": len(papers) >= _NAV_LIMIT,
            },
        )


def _format_author(a: dict[str, Any], *, position: int, n_authors: int) -> str:
    """Format one author row, flagging the senior (last) author.

    Surfaces the ORCID (the bridge into ``kind='orcid'``) so an LLM can
    hop ``get(kind='orcid', id=<iD>)`` straight from here.
    """
    name = (a.get("name") or "(unknown)").strip()
    author_id = a.get("authorId") or ""
    ext = a.get("externalIds") or {}
    orcid = ext.get("ORCID") or ""
    h_index = a.get("hIndex")
    affils = a.get("affiliations") or []
    is_senior = position == n_authors - 1 and n_authors > 1
    role = " — senior (last) author" if is_senior else ""
    lines: list[str] = [f"## {position + 1}. {name}{role}"]
    if orcid:
        lines.append(f"_ORCID:_ {orcid} → get(kind='orcid', id='{orcid}')")
    if author_id:
        lines.append(
            f"_S2 author:_ {author_id} → "
            f"get(kind='semanticscholar', id='author:{author_id}')"
        )
    if h_index is not None:
        lines.append(f"_h-index:_ {h_index}")
    if affils:
        lines.append(f"_Affiliations:_ {', '.join(affils[:4])}")
    return "\n".join(lines)


def _format_paper(p: dict[str, Any]) -> str:
    """Format one paper hit into a markdown-style block."""
    title = (p.get("title") or "(untitled)").strip()
    year = p.get("year") or "?"
    authors = p.get("authors") or []
    author_names = ", ".join(a.get("name", "") for a in authors[:6] if a.get("name"))
    if len(authors) > 6:
        author_names += f", et al. ({len(authors)} authors)"
    ext = p.get("externalIds") or {}
    doi = ext.get("DOI") or ""
    arxiv = ext.get("ArXiv") or ""
    venue = (p.get("venue") or "").strip()
    abstract = (p.get("abstract") or "").strip()
    cite_n = p.get("citationCount")
    oa = p.get("openAccessPdf") or {}
    oa_url = oa.get("url") if isinstance(oa, dict) else None

    lines: list[str] = [f"## {title} ({year})"]
    if author_names:
        lines.append(f"_Authors:_ {author_names}")
    if venue:
        lines.append(f"_Venue:_ {venue}")
    if cite_n is not None:
        lines.append(f"_Cited:_ {cite_n}")
    if doi:
        lines.append(f"_DOI:_ {doi} (https://doi.org/{doi})")
    if arxiv:
        lines.append(f"_arXiv:_ {arxiv} (https://arxiv.org/abs/{arxiv})")
    if oa_url:
        lines.append(f"_Open access PDF:_ {oa_url}")
    if abstract:
        lines.append("")
        lines.append(abstract)
    return "\n".join(lines)


def _format_paper_with_flag(p: dict[str, Any], flag: str) -> str:
    """:func:`_format_paper`, plus the corpus-diff flag
    (``held: pa…`` / ``stub: pa…`` / ``NEW``, from
    :meth:`SemanticScholarHandler._corpus_flags_bulk`) as a line under the
    heading — every rendered hit in a topic search carries this, so "have
    we already got this?" never requires a separate lookup."""
    base = _format_paper(p)
    heading, _sep, rest = base.partition("\n")
    body = f"{heading}\n_Corpus:_ {flag}"
    return f"{body}\n{rest}" if rest else body


def _format_author_papers_table(
    papers: list[dict[str, Any]], flags: list[tuple[str, int | None]]
) -> str:
    """Render an author's works as one markdown table — columns
    ``year · cites · corpus · title (truncated) · s2 id`` — rows sorted
    by citation count descending (unknown counts sort last). ``flags`` is
    :meth:`SemanticScholarHandler._corpus_flags_bulk`'s per-paper output,
    same order as ``papers``; the ``NEW``/``UNRESOLVED``/``held: pa…``/
    ``stub: pa…`` spelling collapses to this table's ``—``/``?``/
    ``held pa…``/``stub pa…`` (gr346833)."""

    def _cites_key(pf: tuple[dict[str, Any], tuple[str, int | None]]) -> int:
        cites = pf[0].get("citationCount")
        return int(cites) if isinstance(cites, int | float) else -1

    ranked = sorted(zip(papers, flags, strict=True), key=_cites_key, reverse=True)
    lines = ["| year | cites | corpus | title | s2 id |", "|---|---|---|---|---|"]
    for p, (flag, _rid) in ranked:
        year = p.get("year")
        year_s = str(year) if year is not None else "?"
        cites = p.get("citationCount")
        cites_s = str(cites) if cites is not None else "?"
        if flag == "NEW":
            corpus = "—"
        elif flag == "UNRESOLVED":
            corpus = "?"
        else:
            corpus = flag.replace(": ", " ", 1)
        title = (p.get("title") or "(untitled)").strip().replace("|", "/")
        if len(title) > _AUTHOR_TITLE_TRUNCATE:
            title = title[: _AUTHOR_TITLE_TRUNCATE - 1].rstrip() + "…"
        s2_id = str(p.get("paperId") or "")
        lines.append(f"| {year_s} | {cites_s} | {corpus} | {title} | {s2_id} |")
    return "\n".join(lines)


__all__ = ["SemanticScholarHandler"]
