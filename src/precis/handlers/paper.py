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

import re
from typing import Any, ClassVar

from precis.embedder import Embedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    validate_link_args,
)
from precis.handlers._paper_toc import build_toc, filter_toc_to_range, render_toc
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Store, Tag
from precis.utils.next_block import render_next_section
from precis.utils.search_header import format_search_headline

# ---------------------------------------------------------------------------
# Public spec
# ---------------------------------------------------------------------------

_SUPPORTED_VIEWS = ("bibtex", "ris", "endnote", "abstract", "toc")


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
            "per chunk. Ingested from .acatome bundles. Put accepts "
            "link/unlink/tags only — paper bodies are import-only."
        ),
        supports_get=True,
        supports_search=True,
        # Phase-8: cross-linking. ``supports_put=True`` does NOT mean
        # paper bodies become writable — the put handler restricts to
        # link/unlink/tags/untags + rel and rejects ``text=``/``mode=``
        # for content mutation. The motivating use case is letting
        # papers cross-cite each other and carry CACHE: tags without
        # going through a memory ref as a hop.
        supports_put=True,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
    )

    def __init__(self, *, store: Store, embedder: Embedder | None = None) -> None:
        self.store = store
        self.embedder = embedder

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
        slug, chunk_spec, path_view = _parse_paper_id(str(id))

        ref = self.store.get_ref(kind="paper", id=slug)
        if ref is None:
            raise NotFound(
                f"paper slug {slug!r} not found",
                next="search(kind='paper', q='your query') to find existing",
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
            raise BadInput(
                f"cannot combine chunk selector (~N..M) with view={effective_view!r}",
                next=f"get(kind='paper', id={slug!r}~{chunk_spec[0]}..{chunk_spec[1]}/toc)",
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
            scope_ref = self.store.get_ref(kind="paper", id=scope)
            if scope_ref is None:
                raise NotFound(
                    f"paper slug {scope!r} not found",
                    next="search(kind='paper', q='...') to find one",
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
            return Response(
                body=(
                    f"no paper blocks match {q!r}\n"
                    "next: try a broader phrase, drop low-info tokens, or "
                    "search a different kind via get(kind='skill', id='precis-help')"
                )
            )

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

        # Hits arrive sorted by RRF fused rank — best first. We do *not*
        # surface the raw fused number because RRF is rank-based by
        # construction (1/(k+rank_lex) + 1/(k+rank_sem)); the absolute
        # score doesn't reflect query strength, and a misleading
        # 0.0164/0.0161/0.0159 staircase is the same for every query.
        # Position in this list is the only honest relevance signal,
        # so we render position only.
        lines = [
            format_search_headline(
                n_returned=len(hits),
                total=total,
                noun="block hit",
                query=q,
            )
        ]
        for i, (block, ref, _score) in enumerate(hits, 1):
            slug = ref.slug or "???"
            handle = f"{slug}~{block.pos}"
            preview = _excerpt(block.text)
            lines.append(f"\n## {i}. {handle}")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

    # -- put: link/tag CRUD only (no body mutation) --------------------------

    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        text: str | None = None,
        mode: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        link: str | None = None,
        unlink: str | None = None,
        rel: str | None = None,
        **_kw: Any,
    ) -> Response:
        """Apply link/tag operations to an existing paper ref.

        Papers are *body-immutable*: the canonical content arrives via
        ``.acatome`` bundle ingest and shouldn't be edited from the
        agent. But cross-linking (paper → paper citations, memory →
        paper references) and tag classification (``SRC:primary``,
        ``CACHE:pinned``) are first-class workflows. This put surface
        accepts exactly the link/tag kwargs and rejects everything
        else with a sharp error.

        Accepted kwargs:

        * ``id`` — paper slug. Required. Chunk selectors (``~N``,
          ``~A..B``) and path views (``/toc``, ``/cite/bib``) are
          rejected here — link/tag ops are ref-level.
        * ``link`` / ``unlink`` — ``kind:identifier[~pos]`` target.
        * ``rel`` — relation slug; defaults to ``related-to`` when
          omitted on link/unlink.
        * ``tags`` / ``untags`` — closed-prefix tags must use one of
          the paper-allowed axes (``SRC``, ``CACHE``); ``STATUS:``
          and ``PRIO:`` are rejected at validation. Open tags are
          always allowed.

        Rejected kwargs:

        * ``text=`` — paper bodies aren't writable. Use the bundle
          ingest CLI to land a new paper.
        * ``mode=`` — there's no mutation mode here. Append/replace/
          delete are nonsensical against an immutable body.
        """
        if text is not None:
            raise BadInput(
                "paper bodies are not writable from put",
                next=(
                    "ingest a paper via `precis jobs ingest-paper "
                    "<bundle.acatome>`; for citations use "
                    "put(kind='paper', id=<slug>, link='paper:other-slug', rel='cites')"
                ),
            )
        if mode is not None:
            raise BadInput(
                f"mode={mode!r} not supported for kind='paper'",
                next=(
                    "paper put accepts only link/unlink/tags/untags — "
                    "no body modes. Drop the mode= kwarg."
                ),
            )
        if id is None:
            raise BadInput(
                "paper put requires id= (the paper slug)",
                next=(
                    "put(kind='paper', id='<slug>', link='paper:other', rel='cites') "
                    "— find the slug via search(kind='paper', q='...')"
                ),
            )

        # Reject chunk selectors and path views — link/tag ops live at
        # the ref level. Re-using ``_parse_paper_id`` here means a
        # caller passing ``slug~46`` or ``slug/cite/bib`` gets the
        # specific "ref-level only" error rather than a generic
        # NotFound on the parsed slug.
        slug, chunk_spec, path_view = _parse_paper_id(str(id))
        if chunk_spec is not None or path_view is not None:
            raise BadInput(
                "paper put operates at ref level — drop the chunk "
                "selector / path view from id=",
                next=f"put(kind='paper', id={slug!r}, link=...)",
            )

        ref = self.store.get_ref(kind="paper", id=slug)
        if ref is None:
            raise NotFound(
                f"paper slug {slug!r} not found",
                next="search(kind='paper', q='...') to find existing slugs",
            )

        validate_link_args(link=link, unlink=unlink, rel=rel, kind="paper")
        if not any((link, unlink, tags, untags)):
            raise BadInput(
                "paper put requires at least one of link=, unlink=, tags=, untags=",
                next=(
                    f"put(kind='paper', id={slug!r}, "
                    "link='paper:other-slug', rel='cites')"
                ),
            )

        n_links_added, n_links_removed = apply_link_ops(
            self.store, ref.id, link=link, unlink=unlink, rel=rel
        )
        n_tags_added, n_tags_removed = apply_tag_ops(
            self.store, "paper", ref.id, tags=tags, untags=untags
        )
        return Response(
            body=format_link_tag_ack(
                kind="paper",
                ref_label=slug,
                n_links_added=n_links_added,
                n_links_removed=n_links_removed,
                n_tags_added=n_tags_added,
                n_tags_removed=n_tags_removed,
            )
        )

    # -- rendering helpers ---------------------------------------------------

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        doi = meta.get("doi")
        year = meta.get("year")
        authors_raw = meta.get("authors")
        authors = _format_authors(authors_raw)
        journal = meta.get("journal") or ""
        n_blocks = self.store.count_blocks(ref.id)

        lines = [f"# {ref.slug}", f"_{ref.title}_"]
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
                return Response(body=f"no abstract on file for {ref.slug}")
            # Strip JATS XML namespace tags (<jats:title>, <jats:p>, …)
            # that some publishers leave in the metadata. The MCP critic
            # found these leaking through verbatim, forcing every caller
            # to do client-side cleanup.
            return Response(body=_strip_jats(str(abstract)))

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
                    f"view='toc', then read the legend block "
                    "(e.g. 'Figure 3. …' on a ~N block). See the "
                    "'Figures' section of precis-paper-help."
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
        lines: list[str] = []
        for b in blocks:
            lines.append(f"# {ref.slug}~{b.pos}")
            lines.append(b.text)
            lines.append("")

        # Next: trailer — adjacent ranges + parent toc + citation.
        # Use the actual block count so the hint never points off the end.
        # Degenerate single-block ranges render as ``~N`` rather than
        # ``~N..N``: the MCP critic flagged ``~77..77`` as training the
        # wrong call shape — agents who saw a "range" hint then
        # extrapolated ``~5..5`` for unrelated singletons later. The
        # canonical single-block form is ``~N``. (Critic MINOR m6.)
        total = self.store.count_blocks(ref.id)
        nav: list[tuple[str, str]] = []
        if hi + 1 < total:
            nxt_lo = hi + 1
            nxt_hi = min(total - 1, hi + (hi - lo + 1))
            sel = f"~{nxt_lo}" if nxt_lo == nxt_hi else f"~{nxt_lo}..{nxt_hi}"
            nav.append(
                (
                    f"get(kind='paper', id='{ref.slug}{sel}')",
                    "next chunk" if nxt_lo == nxt_hi else "next chunk range",
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
            preview = _excerpt(r.title, limit=80)
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
_RANGE_RE = re.compile(r"^(\d+)\.\.(\d+)$")
_CHUNK_RE = re.compile(r"^(\d+)$")

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
    names = _author_names(raw)
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
    meta = ref.meta or {}
    slug = ref.slug or "???"
    title = ref.title
    authors = _author_names(meta.get("authors"))
    journal = str(meta.get("journal") or "")
    year = meta.get("year")
    doi = str(meta.get("doi") or "")

    if style == "bibtex":
        lines = [f"@article{{{slug},"]
        if title:
            lines.append(f"  title = {{{title}}},")
        if authors:
            lines.append(f"  author = {{{' and '.join(authors)}}},")
        if year:
            lines.append(f"  year = {{{year}}},")
        if journal:
            lines.append(f"  journal = {{{journal}}},")
        if doi:
            lines.append(f"  doi = {{{doi}}},")
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


def _excerpt(text: str, *, limit: int = 280) -> str:
    text = " ".join(text.split())  # collapse whitespace
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


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


def _normalise_view(view: str | None) -> str | None:
    """Canonicalise the ``view`` argument.

    Accepts both bare names (``'bibtex'``) and slash-paths
    (``'cite/bib'``). Returns the canonical bare name. Unknown views
    pass through verbatim so the renderer can produce its own
    ``Unsupported`` error with the supported-options list.
    """
    if view is None:
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


__all__ = ["PaperHandler"]
