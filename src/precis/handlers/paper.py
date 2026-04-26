"""PaperHandler — read scientific papers ingested from .acatome bundles.

Phase 3: read-only via ``get`` / ``search``. Ingest happens out-of-band
via ``Store.ingest_bundle()`` (or the ``precis jobs ingest-bundles``
CLI). ``put`` lands in a later phase when paper edits are scoped.

Slug parsing supports the canonical slug-with-chunk syntax used across
v2:

    wang2020state           — overview
    wang2020state~38        — block at pos=38
    wang2020state~38..42    — block range pos∈[38,42]
    wang2020state/cite/bib  — view shortcut path
    wang2020state/abstract  — view shortcut path
    wang2020state/toc       — view shortcut path
"""

from __future__ import annotations

import re
from typing import Any, ClassVar

from precis.embedder import Embedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import Ref, Store

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
            "per chunk. Ingested from .acatome bundles."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=False,
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
        # because the agent is being explicit in the id.
        effective_view = path_view or view
        if chunk_spec is not None:
            if effective_view is not None:
                raise BadInput(
                    "cannot combine chunk selector (~N) with a view",
                    next=f"get(kind='paper', id={slug!r})  or  pick one of "
                    + ", ".join(_SUPPORTED_VIEWS),
                )
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
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        if q is None or not q.strip():
            raise BadInput(
                "search requires q=",
                next="search(kind='paper', q='your query')",
            )

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

        hits = self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="paper",
            scope_ref_id=scope_ref_id,
            limit=top_k,
        )
        if not hits:
            return Response(body=f"no paper blocks match {q!r}")

        lines = [f"# {len(hits)} block hit{'s' if len(hits) != 1 else ''} for {q!r}"]
        for block, ref, score in hits:
            slug = ref.slug or "???"
            handle = f"{slug}~{block.pos}"
            preview = _excerpt(block.text)
            lines.append(f"\n## {handle}  (score={score:.4f})")
            lines.append(f"_{ref.title}_")
            lines.append(preview)
        return Response(body="\n".join(lines))

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
            lines.append(_excerpt(str(abstract), limit=500))

        lines.append("")
        lines.append("Next:")
        lines.append(f"  get(kind='paper', id='{ref.slug}', view='toc')")
        lines.append(f"  get(kind='paper', id='{ref.slug}~0..5')")
        lines.append(f"  get(kind='paper', id='{ref.slug}', view='bibtex')")
        lines.append(f"  search(kind='paper', q='...', scope='{ref.slug}')")
        return Response(body="\n".join(lines))

    def _render_view(self, ref: Ref, view: str) -> Response:
        if view == "abstract":
            abstract = (ref.meta or {}).get("abstract")
            if not abstract:
                return Response(body=f"no abstract on file for {ref.slug}")
            return Response(body=str(abstract))

        if view == "toc":
            blocks = self.store.list_blocks_for_ref(ref.id)
            if not blocks:
                return Response(body=f"{ref.slug}: no blocks")
            lines = [f"# {ref.slug} — TOC ({len(blocks)} blocks)"]
            for b in blocks:
                lines.append(f"  ~{b.pos:>4}  {_excerpt(b.text, limit=80)}")
            return Response(body="\n".join(lines))

        if view in ("bibtex", "ris", "endnote"):
            return Response(body=_format_citation(ref, style=view))

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
        lines = []
        for b in blocks:
            lines.append(f"# {ref.slug}~{b.pos}")
            lines.append(b.text)
            lines.append("")
        return Response(body="\n".join(lines).rstrip())

    def _render_list_papers(self) -> Response:
        refs = self.store.list_refs(kind="paper", limit=50)
        if not refs:
            return Response(
                body=(
                    "no papers ingested yet — "
                    "use `precis jobs ingest-bundles <dir>` to populate"
                )
            )
        lines = [f"# {len(refs)} paper{'s' if len(refs) != 1 else ''}"]
        for r in refs:
            year = (r.meta or {}).get("year") or ""
            preview = _excerpt(r.title, limit=80)
            yr = f"  ({year})" if year else ""
            lines.append(f"  {r.slug:<30}{yr}  {preview}")
        return Response(body="\n".join(lines))


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

    ``slug`` is mandatory. Exactly one of (chunk_range, view) is set if
    the raw id carries a selector; both None for plain slugs.
    """
    m = _SLUG_RE.match(raw)
    if not m:
        raise BadInput(
            f"invalid paper id: {raw!r}",
            next="paper ids look like 'wang2020state' or 'wang2020state~38'",
        )
    slug, rest = m.group(1), m.group(2)

    if not rest:
        return slug, None, None

    if rest.startswith("~"):
        sel = rest[1:]
        rng = _RANGE_RE.match(sel)
        if rng:
            lo, hi = int(rng.group(1)), int(rng.group(2))
            if lo > hi:
                raise BadInput(
                    f"empty chunk range: {raw!r}",
                    next="ranges run lo..hi inclusive (e.g. '~3..7')",
                )
            return slug, (lo, hi), None
        single = _CHUNK_RE.match(sel)
        if single:
            n = int(single.group(1))
            return slug, (n, n), None
        raise BadInput(
            f"unparseable chunk selector after ~: {sel!r}",
            next="use '~N' for a single block or '~N..M' for a range",
        )

    if rest.startswith("/"):
        parts = tuple(rest[1:].split("/"))
        view = _VIEW_PATH_ALIASES.get(parts)
        if view is None:
            raise BadInput(
                f"unknown view path: {raw!r}",
                options=list(_SUPPORTED_VIEWS),
                next="see precis-paper-help for the supported view paths",
            )
        return slug, None, view

    raise BadInput(
        f"unparseable paper id: {raw!r}",
        next="format: <slug> | <slug>~N | <slug>~N..M | <slug>/<view>",
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


__all__ = ["PaperHandler"]
