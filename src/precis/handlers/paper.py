"""PaperHandler — read scientific papers ingested from .acatome bundles.

Phase 3: read-only via ``get`` / ``search``. Ingest happens out-of-band
via ``Store.ingest_bundle()`` (or the ``precis jobs ingest-bundles``
CLI). ``put`` lands in a later phase when paper edits are scoped.

Slug parsing supports the canonical slug-with-chunk syntax used across
v2:

    wang2020state              — overview
    wang2020state~38           — block at pos=38
    wang2020state~38..42       — block range pos∈[38,42]
    wang2020state/cite/bib     — view shortcut path
    wang2020state/abstract     — view shortcut path
    wang2020state/toc          — view shortcut path
    wang2020state~38..42/toc   — TOC scoped to a range (drill-down, phase 3.5)
"""

from __future__ import annotations

import difflib
import html
import re
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
)
from precis.handlers._paper_toc import build_toc, filter_toc_to_range, render_toc
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Store, Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline
from precis.utils.search_merge import SearchHit, block_hits_to_search_hits
from precis.utils.text import excerpt as _excerpt

# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------

_SUPPORTED_VIEWS = ("bibtex", "ris", "endnote", "abstract", "toc")


# Tunable knobs for the nearest-match suggester. The cutoff (0.6 of
# difflib's SequenceMatcher ratio) is deliberately conservative — it
# accepts ``wang2020stat`` → ``wang2020state`` (one missing char,
# ratio≈0.96) but rejects ``foo`` → ``wang2020state`` (ratio≈0.05),
# avoiding spurious "did you mean?" prompts when the agent is querying
# an obviously-different slug.
_SUGGEST_TOP_N = 3
_SUGGEST_CUTOFF = 0.6
# Hard cap on how many slugs we pull into memory for the close-match
# scan. At 30 chars per slug × 5K papers, that's ~150 KB of strings —
# well within the budget for an error path. If the corpus grows past
# this, the suggester silently truncates to the most-recent N papers
# (``list_refs`` orders by ``updated_at DESC``); that's a reasonable
# locality bias and keeps the worst-case cost bounded.
_SUGGEST_CORPUS_CAP = 5000


def _suggest_paper_slugs(slug: str, *, store: Any) -> list[str]:
    """Return up to ``_SUGGEST_TOP_N`` paper slugs that look like ``slug``.

    Uses :func:`difflib.get_close_matches` with a ratio cutoff that
    rejects far-off matches — see ``_SUGGEST_CUTOFF`` for the rationale.
    Returns an empty list when:

    - the corpus is empty (no papers ingested yet),
    - no slug clears the cutoff (typical when the user types a topic
      string into the slug slot, e.g. ``id='nitrate reduction'``),
    - the typed slug exists exactly (caller should have resolved
      already; defensive no-op).

    The helper does **not** raise; callers always pass the result
    straight into the ``options=`` field of a ``NotFound``. Empty list
    → no ``options:`` line in the rendered envelope, which is exactly
    what we want when there's nothing useful to suggest.

    Why a free function (not a method): keeping this off the handler
    makes it independently testable without spinning up a Hub fixture
    and means the same logic could be reused by patent/oracle/conv
    handlers if they grow nearest-match support too.
    """
    if not slug:
        return []
    refs = store.list_refs(kind="paper", limit=_SUGGEST_CORPUS_CAP)
    candidates = [r.slug for r in refs if r.slug]
    if not candidates:
        return []
    return difflib.get_close_matches(
        slug,
        candidates,
        n=_SUGGEST_TOP_N,
        cutoff=_SUGGEST_CUTOFF,
    )


class PaperHandler(Handler):
    """Slug-addressed, read-only paper handler.

    Stored data: each paper is a ``refs`` row with kind='paper' and one
    block per chunk in ``blocks`` (text + optional embedding + density).
    Bibliographic metadata (doi, authors, year, journal, ...) lives in
    ``refs.meta``.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="paper",
        title="Paper",
        description=(
            "Scientific paper. Slug-addressed; one ref per paper, blocks "
            "per chunk. Ingested from .acatome bundles (paper bodies are "
            "import-only). Use tag / link to classify and cross-cite."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        # Phase-9 / seven-verb cutover: paper bodies are import-only
        # (arrive via .acatome bundle ingest, never edited from the
        # agent surface). Cross-linking and tag classification ride
        # on the dedicated tag/link verbs; ``put`` is therefore not
        # exposed on this kind.
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("paper: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # -- get -----------------------------------------------------------------

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None:
            return self._render_list_papers()
        raw_id = _maybe_resolve_doi(self.store, str(id))
        slug, chunk_spec, path_view = _parse_paper_id(raw_id)

        ref = resolve_live_slug_ref(
            self.store,
            kind="paper",
            id=slug,
            next_hint="search(kind='paper', q='your query') to find existing",
            options=_suggest_paper_slugs(slug, store=self.store),
        )

        # Path view (`slug/cite/bib`) takes precedence over kwarg `view`,
        # because the agent is being explicit in the id. Whatever wins,
        # normalise it through the same alias map so view='cite/bib' and
        # view='bibtex' resolve identically — the MCP critic flagged the
        # asymmetry where the path form accepted 'cite/bib' but the kwarg
        # form rejected it.
        effective_view = _normalise_view(path_view or view)

        # Combined form: ``slug~A..B/toc`` → range-scoped TOC drill-down.
        # Only ``view='toc'`` is valid with a chunk_spec; other views
        # don't have a sensible "this range only" meaning yet.
        if chunk_spec is not None and effective_view is not None:
            if effective_view == "toc":
                return self._render_toc(ref, scope=chunk_spec)
            # Build the full id string first, then repr() it whole — the
            # MCP critic flagged ``id={slug!r}~{lo}..{hi}/toc`` as
            # producing ``id='slug'~38..38/toc`` (slug repr'd, suffix
            # outside the quotes) which is a SyntaxError when pasted.
            recovery_id = f"{slug}~{chunk_spec[0]}..{chunk_spec[1]}/toc"
            raise BadInput(
                f"cannot combine chunk selector (~N..M) with view={effective_view!r}",
                next=f"get(kind='paper', id={recovery_id!r})",
            )

        if chunk_spec is not None:
            return self._render_chunks(ref, chunk_spec)

        if effective_view is None:
            return self._render_overview(ref)

        return self._render_view(ref, effective_view)

    # -- search --------------------------------------------------------------

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        scope: str | None = None,
        tags: list[str] | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='paper', q='your query')",
            )

        # Validate the filter at the agent boundary. Same canonical-form
        # rejection as ``put(tags=...)`` so an agent that wrote
        # ``tags=['urgent']`` gets the same error shape it'd get on
        # write — not silently zero hits.
        # Per-kind axis enforcement: passing kind='paper' here means a
        # filter like ``tags=['STATUS:open']`` raises BadInput at the
        # boundary instead of silently returning zero hits — papers
        # have no STATUS axis. (Critic per-kind-axis follow-up.)
        normalized_tags = Tag.normalize_filter(tags, kind="paper")

        scope_ref_id: int | None = None
        if scope is not None:
            scope_slug = _maybe_resolve_doi(self.store, str(scope))
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="paper",
                id=scope_slug,
                next_hint="search(kind='paper', q='...') to find one",
                options=_suggest_paper_slugs(scope, store=self.store),
            )
            scope_ref_id = scope_ref.id

        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)

        # ``max_distance`` enforces a semantic relevance floor so a
        # nonsense query (``'xyzzy frobnicate quux'``) returns an
        # empty response instead of the top-K closest random blocks.
        # The lexical leg already has a natural zero (the tsquery
        # either matches or it doesn't); the floor is sem-only.
        # (Critic MAJOR #3.)
        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="paper",
            scope_ref_id=scope_ref_id,
            tags=normalized_tags,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        if not hits:
            # Use the canonical Next: block shape rather than an
            # inline ``next: ...`` prose line. The critic rules
            # (C17 / D12) say trailers share one delimiter style
            # across kinds, and the empty-search branch was the
            # last holdout of the lowercase-prose shape. (c5
            # unified-trailer patch.)
            body = f"no paper blocks match {q!r}"
            body += render_next_section(
                [
                    (
                        f"search(kind='paper', q={q!r}, top_k=50)",
                        "widen the lexical net",
                    ),
                    (
                        "get(kind='skill', id='precis-help')",
                        "see search tips for other kinds",
                    ),
                ]
            )
            return Response(body=body)

        # Total-hits header: count blocks the lexical filter would
        # match without the LIMIT, so the agent sees "10 of K" when
        # paginated. RRF only re-ranks lexically-matching rows, so
        # the lexical count is the meaningful "K". (MCP critic
        # MAJOR #10b.)
        total = self.store.count_blocks_lexical(
            q=q,
            kind="paper",
            scope_ref_id=scope_ref_id,
            tags=normalized_tags,
        )

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
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block hit",
                query=q,
            )
        ]
        for i, (block, ref, score) in enumerate(hits, 1):
            slug = ref.slug or "???"
            handle = f"{slug}~{block.pos}"
            # Scrub image markers + page anchors out of the preview
            # so the search hit lines never carry the raw
            # ``![](_page_*.jpeg)`` markdown.  The same substitution
            # ``_render_chunks`` applies on the get path; both
            # excerpt paths now stay on the same contract.
            # (MCP critic re-probe MAJOR — figure marker leaks in
            # search previews.)
            preview = _excerpt(_scrub_block_text(block.text), limit=280)
            lines.append(f"\n## {i}. {handle}  (score={score:.4f})")
            lines.append(f"_{_clean_inline_text(ref.title)}_")
            lines.append(preview)

        body = "\n".join(lines)

        # Pagination affordance — when the lexical total exceeds what
        # we returned, surface the narrow-with-scope path explicitly.
        # Without this trailer a 7B caller seeing ``# 100 of 101`` often
        # reads the header as "this is everything" and stops short of
        # hit #101. (MCP critic MAJOR — search has no pagination
        # affordance when capped.)
        #
        # Singleton-hit special case (MCP critic MINOR-$): when
        # ``len(hits) == 1`` the previous nav was 46 % of the response
        # and 100 % redundant — the scope suggestion narrowed to the
        # only hit's own paper (a no-op), and the salient-term
        # suggestion is moot when the caller already has a tight
        # match. Replace the two-line nav with a single ``top_k=``
        # widen hint, which is the only useful next step from a
        # one-of-many singleton.
        if total > len(hits):
            if len(hits) == 1:
                body += render_next_section(
                    [
                        (
                            f"search(kind='paper', q={q!r}, top_k=10)",
                            f"see more of the {total} matches",
                        ),
                    ]
                )
            else:
                top_slug = (hits[0][1].slug or "???") if hits else None
                nav: list[tuple[str, str]] = []
                if scope is None and top_slug is not None:
                    nav.append(
                        (
                            f"search(kind='paper', q={q!r}, scope={top_slug!r})",
                            f"narrow to blocks inside {top_slug}",
                        )
                    )
                nav.append(
                    (
                        f"search(kind='paper', q={q!r} + ' <salient term>')",
                        "tighten the query with a hit-specific token",
                    )
                )
                body += render_next_section(nav)

        return Response(body=body)

    # -- search_hits: structured form for cross-kind merge -------------------

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Block-level fused search returned as ``SearchHit``s.

        Same engine as :meth:`search`, but skips the per-handler
        rendering and surfaces the structured rows so the runtime
        cross-kind dispatcher can RRF-fuse them with hits from
        other kinds.  ``scope=`` is intentionally omitted — cross-
        kind merge has no per-paper scope.
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind="paper")
        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        triples = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="paper",
            tags=normalized_tags,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )
        return block_hits_to_search_hits(triples, kind="paper")

    # -- seven-verb surface --------------------------------------------------

    def _resolve_paper_slug(self, id: str | int) -> tuple[str, int]:
        """Coerce an agent-facing id to a (slug, ref_id) pair.

        Rejects chunk selectors and path views — link/tag ops live
        at the ref level only. Raises ``BadInput`` (selector
        present) or ``NotFound`` (slug unknown) so the caller can
        let those propagate.
        """
        raw_id = _maybe_resolve_doi(self.store, str(id))
        slug, chunk_spec, path_view = _parse_paper_id(raw_id)
        if chunk_spec is not None or path_view is not None:
            raise BadInput(
                "paper ops operate at ref level — drop the chunk "
                "selector / path view from id=",
                next=f"tag(kind='paper', id={slug!r}, ...) or link(kind='paper', id={slug!r}, ...)",
            )
        ref = resolve_live_slug_ref(
            self.store,
            kind="paper",
            id=slug,
            next_hint="search(kind='paper', q='...') to find existing slugs",
            options=_suggest_paper_slugs(slug, store=self.store),
        )
        return slug, ref.id

    def tag(  # type: ignore[override]
        self,
        *,
        id: str | int,
        add: list[str] | None = None,
        remove: list[str] | None = None,
        **_kw: Any,
    ) -> Response:
        """Add/remove paper tags. Allowed axes: ``SRC``, ``CACHE`` + open."""
        if not add and not remove:
            raise BadInput(
                "tag(kind='paper', id=...) requires add= or remove=",
                next="tag(kind='paper', id='<slug>', add=['CACHE:pinned'])",
            )
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_tag_ops(
            self.store, "paper", ref_id, tags=add, untags=remove
        )
        return Response(
            body=format_link_tag_ack(
                kind="paper",
                ref_label=slug,
                n_links_added=0,
                n_links_removed=0,
                n_tags_added=n_added,
                n_tags_removed=n_removed,
            )
        )

    def link(  # type: ignore[override]
        self,
        *,
        id: str | int,
        target: str | None = None,
        mode: str = "add",
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Add or remove a link from this paper to another ref."""
        if target is None:
            raise BadInput(
                "link(kind='paper', id=...) requires target=",
                next="link(kind='paper', id='<slug>', target='paper:other-slug', rel='cites')",
            )
        if mode not in ("add", "remove"):
            raise BadInput(
                f"link mode must be 'add' or 'remove', got {mode!r}",
                options=["add", "remove"],
            )
        slug, ref_id = self._resolve_paper_slug(id)
        n_added, n_removed = apply_link_ops(
            self.store,
            ref_id,
            link=target if mode == "add" else None,
            unlink=target if mode == "remove" else None,
            rel=rel,
        )
        return Response(
            body=format_link_tag_ack(
                kind="paper",
                ref_label=slug,
                n_links_added=n_added,
                n_links_removed=n_removed,
                n_tags_added=0,
                n_tags_removed=0,
            )
        )

    # -- rendering helpers ---------------------------------------------------

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        doi = meta.get("doi")
        year = meta.get("year")
        authors_raw = meta.get("authors")
        authors = _format_authors(authors_raw)
        journal = _clean_inline_text(str(meta.get("journal") or ""))
        n_blocks = self.store.count_blocks(ref.id)

        lines = [f"# {ref.slug}", f"_{_clean_inline_text(ref.title)}_"]
        if authors:
            lines.append(authors)
        venue: list[str] = []
        if journal:
            venue.append(journal)
        if year:
            venue.append(str(year))
        if venue:
            lines.append(", ".join(venue))
        if doi:
            lines.append(f"doi: {doi}")
        lines.append("")
        lines.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")
        abstract = meta.get("abstract")
        if abstract:
            lines.append("")
            # Strip JATS XML *before* excerpting — otherwise the
            # 500-char window can chop a tag mid-attribute and the
            # downstream "<jats:" sniff won't catch the dangling
            # garbage. The MCP critic flagged the default overview
            # leaking ``<jats:title>Abstract</jats:title><jats:p>…``
            # verbatim into the response body; ``_strip_jats`` is
            # the same helper view='abstract' uses, so the two
            # paths agree on cleanup.
            lines.append(_excerpt(_strip_jats(str(abstract)), limit=500))

        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    f"get(kind='paper', id='{ref.slug}', view='toc')",
                    "hierarchical table of contents",
                ),
                (
                    f"get(kind='paper', id='{ref.slug}~0..5')",
                    "read first chunks",
                ),
                (
                    f"get(kind='paper', id='{ref.slug}', view='bibtex')",
                    "BibTeX citation",
                ),
                (
                    f"search(kind='paper', q='...', scope='{ref.slug}')",
                    "search blocks within this paper",
                ),
            ]
        )
        return Response(body=body)

    def _render_view(self, ref: Ref, view: str) -> Response:
        if view == "abstract":
            abstract = (ref.meta or {}).get("abstract")
            if not abstract:
                # Empty result still teaches the next call shape:
                # the paper's body is reachable via TOC + chunk
                # ranges even when the publisher's abstract metadata
                # is missing. Without this trailer the bare
                # "no abstract on file" was a dead end (MCP critic
                # MINOR-C 2026-05-02).
                slug = ref.slug or "???"
                body = f"no abstract on file for {slug}"
                body += render_next_section(
                    [
                        (
                            f"get(kind='paper', id='{slug}', view='toc')",
                            "hierarchical TOC — find sections to read",
                        ),
                        (
                            f"get(kind='paper', id='{slug}~0..5')",
                            "read the first chunks (often opens with the abstract)",
                        ),
                        (
                            f"search(kind='paper', q='abstract', scope={slug!r})",
                            "search blocks within this paper",
                        ),
                    ]
                )
                return Response(body=body)
            # Strip JATS XML namespace tags (<jats:title>, <jats:p>, …)
            # that some publishers leave in the metadata, then run the
            # entity unescape pipeline so ``&amp;`` lands as ``&``.
            # The MCP critic flagged the body-only response as missing
            # any affordance — add a slug header + Next: trailer so
            # the caller knows which paper they're reading and where
            # to go next. (MCP critic NIT — abstract has no header /
            # Next:.)
            cleaned = _clean_inline_text(_strip_jats(str(abstract)))
            slug = ref.slug or "???"
            title = _clean_inline_text(ref.title)
            body = f"# {slug} — abstract\n_{title}_\n\n{cleaned}"
            body += render_next_section(
                [
                    (
                        f"get(kind='paper', id='{slug}', view='toc')",
                        "hierarchical TOC",
                    ),
                    (
                        f"get(kind='paper', id='{slug}', view='bibtex')",
                        "BibTeX citation",
                    ),
                ]
            )
            return Response(body=body)

        if view == "toc":
            return self._render_toc(ref, scope=None)

        if view in ("bibtex", "ris", "endnote"):
            return Response(body=_format_citation(ref, style=view))

        # The MCP critic flagged ``view='figures'`` as a silent
        # failure — the agent had no signal that figure retrieval
        # is unsupported. Surface a sharper hint pointing at the
        # caption-only figure workflow documented in
        # precis-paper-help so the agent doesn't keep retrying.
        # (Critic MAJOR #4.)
        if view in ("figures", "fig", "figure"):
            raise Unsupported(
                f"figure view {view!r} not implemented for kind='paper'",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "figure binaries aren't served — figures live as legend "
                    "blocks inside the body. Find the figure number via "
                    "view='toc', then read the legend block "
                    "(e.g. 'Figure 3. …' on a ~N block). See the "
                    "'Figures' section of precis-paper-help."
                ),
            )
        # ``view='fig/<N>'`` is documented in precis-paper-help as a
        # reserved-for-future affordance.  Without a dedicated branch
        # it falls into the generic "unknown view" error below,
        # which makes a caller who *has* read the help skill assume
        # the docs are wrong rather than the build being early.
        # Surface the reservation explicitly so the caller knows to
        # use the caption-only workaround until figure-binary
        # serving is wired.  (MCP critic MINOR — fig/<N> documented
        # but unrecognised view path returns the same enum as a
        # typo.)
        if view.startswith("fig/"):
            raise Unsupported(
                f"view={view!r} is reserved for a future build",
                options=list(_SUPPORTED_VIEWS),
                next=(
                    "view='fig/<N>' is documented in precis-paper-help as "
                    "reserved — figure-binary serving isn't wired yet.  "
                    "Until then, find the figure number via view='toc' "
                    "and read the legend block on the matching ~N "
                    "(e.g. 'Figure 3. …')."
                ),
            )
        raise Unsupported(
            f"unknown view {view!r} for kind='paper'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-paper-help — try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        blocks = self.store.list_blocks_for_ref(ref.id, pos_range=(lo, hi))
        if not blocks:
            raise NotFound(
                f"no blocks in {ref.slug} for range ~{lo}..{hi}",
                next=f"get(kind='paper', id='{ref.slug}', view='toc')",
            )

        # Figure-and-caption coalescing: when a single-block request
        # lands on an image-only block, fetch the next block too so the
        # caller sees the caption in the same response. Without this an
        # agent gets just ``![](_page_19_Figure_1.jpeg)`` — no number,
        # no caption, and a relative URL that nothing serves.
        # (MCP critic MAJOR — figure block returns image marker with no
        # caption.)
        if len(blocks) == 1 and lo == hi and _is_image_only_block(blocks[0].text):
            tail = self.store.list_blocks_for_ref(ref.id, pos_range=(hi + 1, hi + 1))
            if tail and _looks_like_caption(tail[0].text):
                blocks = [*blocks, *tail]
                hi = tail[0].pos

        lines: list[str] = []
        for b in blocks:
            lines.append(f"# {ref.slug}~{b.pos}")
            lines.append(_render_block_body(ref.slug or "???", b.pos, b.text))
            lines.append("")

        # Next: trailer — adjacent ranges + parent toc + citation.
        # Use the actual block count so the hint never points off the end.
        # Degenerate single-block ranges render as ``~N`` rather than
        # ``~N..N``: the MCP critic flagged ``~77..77`` as training the
        # wrong call shape — agents who saw a "range" hint then
        # extrapolated ``~5..5`` for unrelated singletons later. The
        # canonical single-block form is ``~N``. (Critic MINOR m6.)
        #
        # Single-block reads also widen the forward suggestion into a
        # range so a "next chunk" hint doesn't train a linear ~N → ~N+1
        # → ~N+2 sequential scan (observed pattern: agent reading
        # gerfen2011~13 through ~21 one-by-one across ~10 LLM turns
        # @ ~3min/turn when a single ~13..21 range read would have
        # finished in one).  The promoted "navigate via TOC" hint
        # comes first in single-block mode, since paging-by-block is
        # almost never the right strategy when scanning a paper.
        total = self.store.count_blocks(ref.id)
        nav: list[tuple[str, str]] = []
        single_block = lo == hi

        # In single-block mode, lead with the two structural reads
        # that are almost always more useful than paging linearly:
        #
        # 1. In-paper semantic search — when looking for a specific
        #    quote/section, scoped search beats reading sequentially.
        #    The same fused lexical+embedding index used by
        #    cross-paper search applies here, just narrowed to one
        #    paper.
        # 2. TOC — structural map of the whole paper, ~50 lines.
        #
        # In range mode these stay available but at lower priority
        # than the next/prev range hints.
        if single_block:
            nav.append(
                (
                    f"search(kind='paper', q='your query', scope='{ref.slug}')",
                    "search inside this paper "
                    "(fused lexical+embedding) — usually beats paging",
                )
            )
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}', view='toc')",
                    "TOC — structural map of the paper",
                )
            )

        if hi + 1 < total:
            nxt_lo = hi + 1
            if single_block:
                # Suggest a 5-block forward range, not the bare next
                # block — encourages wider reading on follow-up.
                nxt_hi = min(total - 1, hi + 5)
                hint = "next 5 chunks" if nxt_hi > nxt_lo else "next chunk"
            else:
                # Range read — same-sized forward window.
                nxt_hi = min(total - 1, hi + (hi - lo + 1))
                hint = (
                    "next chunk" if nxt_lo == nxt_hi else "next chunk range"
                )
            sel = f"~{nxt_lo}" if nxt_lo == nxt_hi else f"~{nxt_lo}..{nxt_hi}"
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}{sel}')",
                    hint,
                )
            )
        if lo > 0:
            prev_hi = lo - 1
            prev_lo = max(0, lo - (hi - lo + 1))
            sel = f"~{prev_lo}" if prev_lo == prev_hi else f"~{prev_lo}..{prev_hi}"
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}{sel}')",
                    "previous chunk" if prev_lo == prev_hi else "previous chunk range",
                )
            )
        if not single_block:
            # Range mode: full TOC is still useful but lower priority
            # than the next/prev range hints.
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}', view='toc')",
                    "full TOC",
                )
            )
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}~{lo}..{hi}/toc')",
                    "TOC of this range",
                )
            )
        nav.append(
            (
                f"get(kind='paper', id='{ref.slug}', view='bibtex')",
                "BibTeX citation",
            )
        )
        body = "\n".join(lines).rstrip() + render_next_section(nav)
        return Response(body=body)

    def _render_toc(
        self,
        ref: Ref,
        *,
        scope: tuple[int, int] | None,
    ) -> Response:
        """Render the hierarchical TOC, optionally scoped to a block range.

        ``scope=None`` renders the whole paper. ``scope=(lo, hi)`` clips
        the TOC to the range — used by the ``slug~A..B/toc`` drill-down
        form for recursive navigation.
        """
        # Pull every block (we need text to detect headings).
        blocks = self.store.list_blocks_for_ref(ref.id)
        if not blocks:
            return Response(body=f"{ref.slug}: no blocks")

        toc = build_toc(blocks)
        range_label: str | None = None
        if scope is not None:
            lo, hi = scope
            toc = filter_toc_to_range(toc, lo=lo, hi=hi)
            range_label = f"~{lo}..{hi}"

        body = render_toc(
            slug=ref.slug or "???",
            toc=toc,
            total_blocks=len(blocks),
            blocks_by_pos={b.pos: b for b in blocks},
            range_label=range_label,
        )
        return Response(body=body)

    def _render_list_papers(self) -> Response:
        # Cap the page at 50 — production corpora can be 1000s of papers
        # and a flat dump blows the agent's context. We expose the total
        # count and a search affordance so the agent has somewhere to go.
        limit = 50
        refs = self.store.list_refs(kind="paper", limit=limit)
        total = self.store.count_refs(kind="paper")
        if not refs:
            return Response(
                body=(
                    "no papers ingested yet — "
                    "use `precis jobs ingest-bundles <dir>` to populate"
                )
            )
        suffix = "" if total <= limit else f" of {total}"
        lines = [f"# {len(refs)} paper{'s' if len(refs) != 1 else ''}{suffix}"]
        for r in refs:
            year = (r.meta or {}).get("year") or ""
            # Run titles through the JATS/entity cleanup before
            # excerpting — otherwise a title like
            # ``Cu/ZnO<sub>x</sub>`` lands in the list verbatim and
            # any LLM reading it copies the markup back into prose.
            # (MCP critic MINOR — list view leaks raw HTML/JATS.)
            preview = _excerpt(_clean_inline_text(r.title), limit=80)
            yr = f"  ({year})" if year else ""
            lines.append(f"  {r.slug:<30}{yr}  {preview}")
        body = "\n".join(lines)
        body += render_next_section(
            [
                (
                    "search(kind='paper', q='your topic')",
                    "find a specific paper by topic",
                ),
                (
                    "get(kind='paper', id='<slug>')",
                    "open one paper from the list",
                ),
            ]
        )
        return Response(body=body)


# ---------------------------------------------------------------------------
# Slug + chunk parsing
# ---------------------------------------------------------------------------

# Slugs are lowercase alphanumeric + hyphens. The `~` introduces a chunk
# selector; the rest of the string is parsed as a path of `view/sub`
# segments.
_SLUG_RE = re.compile(r"^([a-z0-9][a-z0-9\-]*)(.*)$")
_RANGE_RE = re.compile(r"^(\d+)(?:\.\.|-)(\d+)$")
_CHUNK_RE = re.compile(r"^(\d+)$")

# A DOI-form paper id. DOIs start with ``10.<registrant>/<suffix>`` per
# the IDF spec; the suffix can legally contain slashes and dots (e.g.
# ``10.1038/s41598-023-44772-6``, ``10.1111/jnc.13915``), which is why
# they can't be routed through ``_SLUG_RE`` — the regex would try to
# split the DOI on ``/`` as a view path and fail. Chunk selectors
# (``~38``) still attach to DOI-form ids; view paths (``/abstract``)
# do not — use the ``view=`` kwarg instead, because we can't safely
# disambiguate ``/abstract`` as "view=abstract" vs "DOI suffix /abstract".
_DOI_RE = re.compile(r"^(10\.\d+/[^~]+?)(~.*)?$")

_VIEW_PATH_ALIASES: dict[tuple[str, ...], str] = {
    ("cite", "bib"): "bibtex",
    ("cite", "bibtex"): "bibtex",
    ("cite", "ris"): "ris",
    ("cite", "endnote"): "endnote",
    ("abstract",): "abstract",
    ("toc",): "toc",
    ("bibtex",): "bibtex",
    ("ris",): "ris",
    ("endnote",): "endnote",
}


def _maybe_resolve_doi(store: Store, raw: str) -> str:
    """Translate a DOI-form paper id to its slug form.

    When an agent hands us a DOI (e.g. ``10.1111/jnc.13915``) as the
    paper id, route it through ``meta->>'doi'`` and substitute the
    slug so the rest of the pipeline (``_parse_paper_id``,
    :func:`resolve_live_slug_ref`, rendering) stays slug-addressed
    and unchanged.

    Chunk selectors ride along: ``10.1111/jnc.13915~38`` →
    ``wang2020dopamine~38``. View paths are *not* supported on
    DOI-form ids — DOI suffixes can legally contain ``/``, so we
    can't disambiguate ``10.1000/foo/abstract`` between "DOI
    literal" and "DOI + view=abstract". The caller must use the
    ``view=`` kwarg alongside a DOI.

    Non-DOI inputs (starting with anything other than ``10.``) are
    returned unchanged — this function is a no-op on slug-form ids.

    Raises :class:`NotFound` when the DOI is well-formed but no live
    paper carries it; the error carries a ``search(kind='paper',
    q='...')`` hint rather than falling through to the generic
    ``"illegal character"`` message the slug regex would emit.
    """
    if not raw.startswith("10."):
        return raw
    m = _DOI_RE.match(raw)
    if m is None:
        # Looks like a DOI prefix but doesn't match the full shape —
        # let the slug parser emit its usual error.
        return raw
    doi, selector = m.group(1), (m.group(2) or "")
    slug = store.find_paper_slug_by_doi(doi)
    if slug is None:
        raise NotFound(
            f"paper with DOI {doi!r} not ingested",
            next=(
                "search(kind='paper', q='<title or authors>') to find "
                "an existing slug, or ingest the paper first"
            ),
        )
    return slug + selector


def _parse_paper_id(
    raw: str,
) -> tuple[str, tuple[int, int] | None, str | None]:
    """Return (slug, chunk_range, view).

    ``slug`` is mandatory. Both ``chunk_range`` and ``view`` may be set
    when the id carries both — e.g. ``slug~46..105/toc`` is the
    drill-down form (TOC scoped to that block range). For plain chunk
    selectors (``slug~38``) view is ``None``; for plain view paths
    (``slug/cite/bib``) chunk_range is ``None``.
    """
    # Friendly redirect for the cross-kind list-view shape. Numeric
    # kinds (memory, todo, ...) accept ``id='/recent'`` for their
    # listing, so a 7B caller learning the convention from one kind
    # naturally retries it on paper. The MCP critic flagged the
    # generic "invalid paper id" reply as a footgun — it sent the
    # caller down a slug-fixup detour when the actual fix is "drop
    # the id=, papers list via the bare get". (Critic MINOR #7.)
    if isinstance(raw, str) and raw.startswith("/"):
        raise BadInput(
            f"paper has no list view {raw!r} — list-view paths are "
            "specific to numeric kinds (memory/todo/fc/...)",
            next=(
                "papers don't accept '/recent' — use the bare list shape: "
                "get(kind='paper')"
            ),
        )
    m = _SLUG_RE.match(raw)
    if not m:
        raise BadInput(
            f"invalid paper id: {raw!r}",
            next="paper ids look like 'wang2020state' or 'wang2020state~38'",
        )
    slug, rest = m.group(1), m.group(2)
    # The slug regex is permissive at the right edge — `[a-z0-9][a-z0-9-]*`
    # matches the *prefix*, then `(.*)` swallows whatever's left. So
    # `nonexistent_paper_xyz` parses as slug='nonexistent' + rest='_paper_xyz'.
    # The rest then doesn't start with `~` or `/` and falls through to
    # the generic "unparseable" error at the bottom — which doesn't
    # name the actual rule. The MCP critic flagged this: a 7B model
    # using snake_case sees BadInput instead of NotFound and goes down
    # the wrong recovery branch. Catch the underscore case explicitly
    # before the chunk/view logic gets a chance. (Critic MINOR m3.)
    if rest and not rest.startswith(("~", "/")):
        first_bad = rest[0]
        if first_bad == "_":
            raise BadInput(
                f"paper slug contains '_' (illegal): {raw!r}",
                next=(
                    "paper slugs are lowercase a-z + digits + '-' only — "
                    "no underscores. Most slugs look like 'wang2020state'"
                ),
            )
        raise BadInput(
            f"paper slug contains illegal {first_bad!r}: {raw!r}",
            next="paper slugs match [a-z0-9-]+ (e.g. 'wang2020state')",
        )

    if not rest:
        return slug, None, None

    chunk_range: tuple[int, int] | None = None
    if rest.startswith("~"):
        # Split selector from optional view path: ``~46..105/toc``.
        sel_and_path = rest[1:]
        if "/" in sel_and_path:
            sel, _, path_part = sel_and_path.partition("/")
            rest_after_sel = "/" + path_part
        else:
            sel = sel_and_path
            rest_after_sel = ""

        rng = _RANGE_RE.match(sel)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                raise BadInput(
                    f"empty chunk range: {raw!r}",
                    next="ranges run lo..hi inclusive (e.g. '~3..7')",
                )
            chunk_range = (lo, hi)
        else:
            single = _CHUNK_RE.match(sel)
            if single:
                n = int(single.group(1))
                chunk_range = (n, n)
            else:
                raise BadInput(
                    f"unparseable chunk selector after ~: {sel!r}",
                    next="use '~N' for a single block or '~N..M' for a range",
                )

        if not rest_after_sel:
            return slug, chunk_range, None
        rest = rest_after_sel

    if rest.startswith("/"):
        parts = tuple(rest[1:].split("/"))
        view = _VIEW_PATH_ALIASES.get(parts)
        if view is None:
            # Specific hint for the figure family — the MCP critic
            # flagged ``slug/fig/N`` as failing silently with a
            # generic "unknown view" error. Surface the caption-only
            # workflow so the agent stops retrying. (Critic MAJOR #4.)
            if parts and parts[0] in ("fig", "figure", "figures"):
                raise BadInput(
                    f"figure view path {raw!r} not implemented",
                    options=list(_SUPPORTED_VIEWS),
                    next=(
                        "figure binaries aren't served — figures live as "
                        "legend blocks inside the body. Use view='toc' to "
                        "locate the figure number, then read the legend "
                        "block. See 'Figures' in precis-paper-help."
                    ),
                )
            raise BadInput(
                f"unknown view path: {raw!r}",
                options=list(_SUPPORTED_VIEWS),
                next="see precis-paper-help for the supported view paths",
            )
        return slug, chunk_range, view

    raise BadInput(
        f"unparseable paper id: {raw!r}",
        next="format: <slug> | <slug>~N | <slug>~N..M | <slug>/<view> | <slug>~N..M/<view>",
    )


# ---------------------------------------------------------------------------
# Author + citation rendering
# ---------------------------------------------------------------------------


def _format_authors(raw: Any) -> str:
    names = [_clean_inline_text(n) for n in _author_names(raw)]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) <= 3:
        return "; ".join(names)
    return f"{names[0]} et al."


def _author_names(raw: Any) -> list[str]:
    """Normalise ``authors`` into a flat list of name strings.

    Accepts list-of-dicts (``[{"name": "Smith, J."}, ...]``),
    list-of-strings, semicolon-packed string, or None/garbage.
    Pure — never raises.
    """
    if isinstance(raw, list):
        out: list[str] = []
        for item in raw:
            if isinstance(item, dict):
                name = str(item.get("name") or "").strip()
            else:
                name = str(item).strip()
            if name:
                out.append(name)
        return out
    if isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(";") if a.strip()]
    return []


def _format_citation(ref: Ref, *, style: str) -> str:
    """Render a citation in BibTeX / RIS / EndNote.

    All scalar metadata fields are run through :func:`_clean_inline_text`
    to strip JATS / HTML markup and unescape entities (``&amp;`` →
    ``&``); BibTeX additionally LaTeX-escapes ``& % _ #`` so the output
    compiles cleanly. (MCP critic MINOR — BibTeX leaks ``&amp;`` and
    paper list leaks ``<sub>``.)
    """
    meta = ref.meta or {}
    slug = ref.slug or "???"
    title = _clean_inline_text(ref.title)
    authors = [_clean_inline_text(a) for a in _author_names(meta.get("authors"))]
    authors = [a for a in authors if a]
    journal = _clean_inline_text(str(meta.get("journal") or ""))
    year = meta.get("year")
    doi = _clean_inline_text(str(meta.get("doi") or ""))

    if style == "bibtex":
        # LaTeX-escape every scalar field that might carry a special
        # char. ``and``/``year``/``doi`` rarely do but we run them
        # through anyway for symmetry; a stray ``&`` in the title was
        # the actual MCP-critic finding.
        bx_title = _latex_escape(title)
        bx_authors = " and ".join(_latex_escape(a) for a in authors)
        bx_journal = _latex_escape(journal)
        bx_doi = _latex_escape(doi)
        lines = [f"@article{{{slug},"]
        if bx_title:
            lines.append(f"  title = {{{bx_title}}},")
        if bx_authors:
            lines.append(f"  author = {{{bx_authors}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if bx_journal:
            lines.append(f"  journal = {{{bx_journal}}},")
        if bx_doi:
            lines.append(f"  doi = {{{bx_doi}}},")
        lines.append("}")
        return "\n".join(lines) + "\n"

    if style == "ris":
        out = ["TY  - JOUR"]
        if title:
            out.append(f"TI  - {title}")
        for a in authors:
            out.append(f"AU  - {a}")
        if year:
            out.append(f"PY  - {year}")
        if journal:
            out.append(f"JO  - {journal}")
        if doi:
            out.append(f"DO  - {doi}")
        out.append("ER  - ")
        return "\n".join(out)

    # endnote (subset)
    out = ["%0 Journal Article"]
    if title:
        out.append(f"%T {title}")
    for a in authors:
        out.append(f"%A {a}")
    if year:
        out.append(f"%D {year}")
    if journal:
        out.append(f"%J {journal}")
    if doi:
        out.append(f"%R {doi}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Inline-markup + LaTeX-escape helpers
# ---------------------------------------------------------------------------

# Strip a small whitelist of inline HTML / JATS tags that publishers
# leak into title/journal metadata: ``<sub>``, ``<sup>``, ``<i>``,
# ``<b>``, ``<em>``, ``<strong>`` plus any ``<jats:*>`` namespace tag.
# The tag *contents* are kept; only the markers go.
_INLINE_TAG_RE = re.compile(
    r"</?(?:sub|sup|i|b|em|strong|jats:[a-zA-Z0-9_-]+)\b[^>]*>",
    re.IGNORECASE,
)

# Characters that need backslash-escaping for LaTeX. The list is the
# minimum set that breaks BibTeX / biber compilation when present in
# field values; ``$``, ``{``, ``}``, ``\`` are not common in
# bibliographic metadata and would need a richer escape.
_LATEX_ESCAPES: dict[str, str] = {
    "&": r"\&",
    "%": r"\%",
    "_": r"\_",
    "#": r"\#",
}


def _clean_inline_text(text: str) -> str:
    """Run the metadata-cleanup pipeline used by every renderer.

    Steps (idempotent):

    1. ``html.unescape`` to flip ``&amp;`` → ``&``, ``&lt;`` → ``<``,
       and any double-encoded ``&amp;lt;`` shapes back to literal text.
       Run twice so the double-encoded shape lands as ``<``.
    2. Strip a small whitelist of inline HTML/JATS tags.
    3. Collapse whitespace runs.

    Pure — never raises.
    """
    if not text:
        return ""
    cleaned = html.unescape(html.unescape(text))
    cleaned = _INLINE_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _latex_escape(text: str) -> str:
    """Backslash-escape the LaTeX special chars BibTeX trips on."""
    if not text:
        return ""
    out = text
    for ch, esc in _LATEX_ESCAPES.items():
        out = out.replace(ch, esc)
    return out


# ---------------------------------------------------------------------------
# View aliasing + abstract sanitisation
# ---------------------------------------------------------------------------


# Kwarg ``view=`` accepts the same vocabulary as the id-path form so that
# ``view='cite/bib'`` and ``id='slug/cite/bib'`` resolve identically.
# Without this, an agent that copied an id like ``slug/cite/bib`` from a
# docstring and then split it into id+view ended up with an Unsupported
# error — the asymmetry the MCP critic flagged.
_VIEW_KWARG_ALIASES: dict[str, str] = {
    "bibtex": "bibtex",
    "ris": "ris",
    "endnote": "endnote",
    "abstract": "abstract",
    "toc": "toc",
    "cite/bib": "bibtex",
    "cite/bibtex": "bibtex",
    "cite/ris": "ris",
    "cite/endnote": "endnote",
}


_VIEW_NOOP_ALIASES: frozenset[str] = frozenset({"text", "body", "full"})


def _normalise_view(view: str | None) -> str | None:
    """Canonicalise the ``view`` argument.

    Accepts both bare names (``'bibtex'``) and slash-paths
    (``'cite/bib'``). Returns the canonical bare name. Unknown views
    pass through verbatim so the renderer can produce its own
    ``Unsupported`` error with the supported-options list.

    ``view='text'``, ``'body'``, ``'full'`` are treated as no-ops —
    they map to ``None``. Workers reach for ``view='text'`` as the
    natural way to ask for chunk bytes (``id='slug~13', view='text'``);
    rather than fight that mental model and emit ``Unsupported``,
    accept it as a synonym for "render the addressed scope using the
    default renderer". With a chunk selector this gives the chunk
    text; without one it gives the paper overview.
    """
    if view is None:
        return None
    if view in _VIEW_NOOP_ALIASES:
        return None
    return _VIEW_KWARG_ALIASES.get(view, view)


# JATS-XML namespace tags leak through some publishers' abstract metadata.
# We strip the simple ``<jats:tag>...</jats:tag>`` form rather than running
# a full parser — abstracts are short and the tag set is constrained.
#
# `<jats:title>Abstract</jats:title>` immediately followed by body text
# was rendering as ``AbstractMetal–organic frameworks…`` (heading word
# glued to the next sentence) — the MCP critic's MINOR m1. Drop the
# ``<jats:title>Abstract</jats:title>`` block specifically because the
# view name itself ('abstract') already names the section, and the
# label is never anything else worth keeping.
_JATS_ABSTRACT_TITLE_RE = re.compile(
    r"<jats:title>\s*Abstract\s*</jats:title>", re.IGNORECASE
)


def _strip_jats(text: str) -> str:
    """Strip ``<jats:*>`` and ``</jats:*>`` namespace tags from text.

    Leaves tag *contents* intact, so ``<jats:p>Hi.</jats:p>`` becomes
    ``Hi.``. Idempotent. Whitespace around stripped tags is collapsed
    only where two newlines emerge — we keep paragraph structure.

    Block-level closing tags (``</jats:p>``, ``</jats:title>``) are
    replaced with a single space rather than nothing, so adjacent
    paragraphs/headings don't end up word-glued. The opening tags
    are still empty-substituted because the preceding character is
    typically already whitespace.
    """
    # Drop the redundant `<jats:title>Abstract</jats:title>` outright —
    # see comment on _JATS_ABSTRACT_TITLE_RE.
    cleaned = _JATS_ABSTRACT_TITLE_RE.sub("", text)
    # Closing tags get a space so we don't fuse "Hi.</jats:p><jats:p>Bye"
    # into "Hi.Bye". Opening tags get nothing.
    cleaned = re.sub(r"</jats:[a-zA-Z0-9_-]+\s*[^>]*>", " ", cleaned)
    cleaned = re.sub(r"<jats:[a-zA-Z0-9_-]+\s*[^>]*>", "", cleaned)
    # Collapse the spaces and newlines the strip can leave behind.
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------------------
# Figure-and-caption coalescing
# ---------------------------------------------------------------------------

# Markdown image markers like ``![](path/to.jpeg)``. The path component
# is captured so the placeholder can name the original asset (purely
# informational — the image isn't served).
_IMAGE_MARKER_RE = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")

# Page-anchor span that acatome-extract emits before image blocks
# (``<span id="page-19-0"></span>``). Stripped along with the image so
# the placeholder body is just a one-line marker.
_PAGE_ANCHOR_RE = re.compile(r"<span\s+id=\"page-\d+-\d+\"\s*></span>")

# Caption blocks start with ``**Fig``, ``**Figure``, ``**Scheme``, or
# ``**Table`` — the bold-prefixed legend pattern emitted by Marker /
# acatome-extract. The check is applied to the *first non-empty line*
# of the candidate block.
_CAPTION_LEAD_RE = re.compile(
    r"^\*\*\s*(Fig(?:ure)?|Scheme|Table)\b",
    re.IGNORECASE,
)


def _is_image_only_block(text: str) -> bool:
    """True when the block consists solely of image markers + page anchors.

    A block that's just ``<span id="page-N-M"></span>![](_page_N_*.jpeg)``
    has no readable content for the agent — the relative path resolves
    to nothing the MCP serves, and there's no caption text to quote.
    """
    stripped = _PAGE_ANCHOR_RE.sub("", text)
    stripped = _IMAGE_MARKER_RE.sub("", stripped)
    return stripped.strip() == ""


def _looks_like_caption(text: str) -> bool:
    """True when the block opens with a Fig/Scheme/Table legend lead."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        return bool(_CAPTION_LEAD_RE.match(line))
    return False


def _render_block_body(slug: str, pos: int, text: str) -> str:
    """Replace bare image markers with a structured placeholder.

    The relative URL ``![](_page_19_Figure_1.jpeg)`` resolves to
    nothing — quoting it back is a footgun for any LLM citing the
    figure. Replace each marker with a short ``[figure: slug~N —
    image not served; caption on adjacent block]`` placeholder, and
    strip the page-anchor spans around it.

    The asset path is **not** preserved.  An earlier cut kept it
    "for diagnostics" but a 7B caller reading ``asset: _page_3_
    Figure_3.jpeg`` still treats the string as a real file —
    that's the same footgun the substitution exists to close.
    The MCP critic's April 2026 re-probe pinned this regression.
    """
    if not _IMAGE_MARKER_RE.search(text):
        return text
    cleaned = _PAGE_ANCHOR_RE.sub("", text)

    def _replace(_m: re.Match[str]) -> str:
        return (
            f"[figure: {slug}~{pos} — image not served; "
            f"caption on adjacent block (~{pos + 1})]"
        )

    cleaned = _IMAGE_MARKER_RE.sub(_replace, cleaned)
    # Collapse whitespace-only artefacts left behind by the strip.
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def _scrub_block_text(text: str) -> str:
    """Strip image markers + page anchors from arbitrary block text.

    Companion to :func:`_render_block_body` for code paths that
    don't have a slug/pos in scope (search previews, future digest
    views).  The output never carries a markdown image marker or
    a page-anchor span — anything that would lure an LLM into
    quoting a non-served asset is dropped, replaced by a brief
    ``[figure]`` sentinel.

    Idempotent: running twice yields the same result, because the
    regexes don't match their own replacements.

    The MCP critic's April 2026 re-probe flagged the search
    preview path leaking raw ``![](_page_3_Figure_3.jpeg)``
    markers because :func:`_render_block_body` was only wired
    into ``_render_chunks``.  Centralising the substitution in
    one helper keeps every excerpt path on the same contract.
    """
    if not text:
        return text
    cleaned = _PAGE_ANCHOR_RE.sub("", text)
    cleaned = _IMAGE_MARKER_RE.sub("[figure]", cleaned)
    return cleaned


__all__ = ["PaperHandler"]
