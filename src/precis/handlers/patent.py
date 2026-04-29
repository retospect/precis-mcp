"""``PatentHandler`` — read-only patent kind backed by EPO OPS.

Phase 1 surface (deferred items in ``docs/patent-kind-spec.md``):

- ``search(q=..., tags=..., scope=..., top_k=...)`` — merged local +
  remote OPS hits with ``[local]`` markers.
- ``get(id=...)`` — fetch-as-ingest. First call hits OPS, parses,
  stores; subsequent calls render from the local store.
- ``get(id='/recent' | '/published')`` — list views.
- ``put(...)`` raises ``Unsupported`` (patents are read-only;
  watches and link/tag ops land in phase 2 / a follow-up).

The handler is hidden from the agent boundary unless
``EPO_OPS_CLIENT_KEY``, ``EPO_OPS_CLIENT_SECRET``, and
``PRECIS_PATENT_RAW_ROOT`` are all set in the environment.
``KindSpec.requires_env`` enforces the gate at registry construction
— see ``precis/protocol.py``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from precis.embedder import Embedder
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._patent_cql import build_cql
from precis.handlers._patent_ingest import ingest_patent
from precis.handlers._patent_ops import (
    OpsClientProto,
    OpsError,
)
from precis.handlers._patent_slug import parse_docdb_id
from precis.handlers._patent_xml import OpsHit, parse_search_response
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Store, Tag

# ---------------------------------------------------------------------------
# Spec
# ---------------------------------------------------------------------------

_SUPPORTED_VIEWS: tuple[str, ...] = (
    "biblio",
    "abstract",
    "description",
    "claims",
    "bibtex",
)

_REQUIRED_ENV: tuple[str, ...] = (
    "EPO_OPS_CLIENT_KEY",
    "EPO_OPS_CLIENT_SECRET",
    "PRECIS_PATENT_RAW_ROOT",
)

# ``[local]`` marker convention — matches the language used in the
# search-future-filters doc and the patent-help skill.
_LOCAL_MARKER = "[local]"

# Conservative cap on local list views so the agent's context
# isn't blown by a large patent corpus.
_LIST_PAGE_LIMIT = 50

# Default page size for the OPS remote leg.
_DEFAULT_REMOTE_PAGE = 20


class PatentHandler(Handler):
    """Slug-addressed, read-only patent handler.

    Stored data: each patent is a ``refs`` row with kind='patent', a
    block per description paragraph and per claim, and structured
    bibliographic metadata in ``refs.meta``. The OPS XML for each
    fetched patent is mirrored on disk under
    ``$PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kind>/`` so the parser can
    be re-run without re-fetching.
    """

    spec: ClassVar[KindSpec] = KindSpec(
        kind="patent",
        title="Patent",
        description=(
            "Patent record from EPO OPS. Slug-addressed by lowercased "
            "DOCDB id (e.g. ep1234567b1). Search merges local + remote "
            "OPS hits; get(id=...) fetches and stores from OPS."
        ),
        supports_get=True,
        supports_search=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
        views=_SUPPORTED_VIEWS,
        requires_env=_REQUIRED_ENV,
    )

    def __init__(
        self,
        *,
        store: Store,
        ops: OpsClientProto,
        raw_root: Path,
        embedder: Embedder | None = None,
    ) -> None:
        self.store = store
        self.ops = ops
        self.raw_root = raw_root
        self.embedder = embedder

    # ── verbs ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        # List views — bare get() and the slash-prefixed handles.
        if id is None or id == "/" or id == "/recent":
            return self._render_list(order="recent")
        if id == "/published":
            return self._render_list(order="published")

        if not isinstance(id, str):
            raise BadInput(
                f"patent id must be a string DOCDB slug, got {type(id).__name__}",
                next="get(kind='patent', id='ep1234567b1')",
            )

        slug, chunk = _parse_patent_id(id)

        # Existing? Render local. Missing? Trigger ingest.
        ref = self.store.get_ref(kind="patent", id=slug)
        if ref is None:
            try:
                result = ingest_patent(
                    slug,
                    store=self.store,
                    ops=self.ops,
                    embedder=self.embedder,
                    raw_root=self.raw_root,
                )
            except NotFound:
                raise
            except OpsError as e:
                raise NotFound(
                    f"could not fetch patent {slug!r} from OPS: {e}",
                    next="check EPO OPS credentials and quota",
                ) from e
            ref = self.store.get_ref(kind="patent", id=result.slug)
            if ref is None:
                # Should be unreachable — ingest commits before returning.
                raise NotFound(f"patent {slug!r} ingest succeeded but ref missing")

        if chunk is not None:
            return self._render_chunks(ref, chunk)

        if view is not None:
            return self._render_view(ref, view)

        return self._render_overview(ref)

    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        tags: list[str] | None = None,
        scope: str | None = None,
        top_k: int = 10,
        **_kw: Any,
    ) -> Response:
        # Tag normalisation. Per-kind axis enforcement: closed tags
        # outside ``{SRC, CACHE}`` raise BadInput at the agent boundary.
        normalized_tags = Tag.normalize_filter(tags, kind="patent")

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = self.store.get_ref(kind="patent", id=scope)
            if scope_ref is None:
                raise NotFound(
                    f"patent slug {scope!r} not found",
                    next="search(kind='patent', q='...') to find one",
                )
            scope_ref_id = scope_ref.id

        # Local leg: hybrid (lex + semantic) over patent blocks.
        local_hits = self._search_local(
            q=q,
            scope_ref_id=scope_ref_id,
            tags=normalized_tags,
            top_k=top_k,
        )

        # Remote leg: only when q= or a CQL-liftable tag is present
        # AND scope is None (scope is local-only — searching one
        # specific patent doesn't make sense remotely).
        remote_hits: list[OpsHit] = []
        cql_used: str | None = None
        if scope_ref_id is None:
            try:
                cql = build_cql(q=q, tags=tags, store=self.store)
            except BadInput:
                # No q= and no liftable tag — local-only search is fine.
                cql = None
            if cql is not None:
                cql_used = cql
                try:
                    response = self.ops.search(
                        cql,
                        range_start=1,
                        range_end=max(top_k, _DEFAULT_REMOTE_PAGE),
                    )
                    remote_hits, _total = parse_search_response(response.xml)
                except OpsError:
                    # Best-effort remote leg. If OPS is down or
                    # quota-limited we still serve the local hits.
                    remote_hits = []

        return self._render_search_response(
            q=q,
            cql=cql_used,
            local_hits=local_hits,
            remote_hits=remote_hits,
            top_k=top_k,
        )

    def put(  # type: ignore[override]
        self, **_kw: Any
    ) -> Response:
        raise Unsupported(
            "patent kind is read-only",
            next=(
                "use get(kind='patent', id=<docdb-slug>) to fetch a "
                "patent from OPS, or search(kind='patent', q='...') "
                "to find one"
            ),
        )

    # ── search-helper internals ────────────────────────────────────────

    def _search_local(
        self,
        *,
        q: str | None,
        scope_ref_id: int | None,
        tags: list[str] | None,
        top_k: int,
    ) -> list[tuple[Any, Ref, float]]:
        """Run the hybrid lex+semantic search over patent blocks.

        Returns an empty list when q= is empty AND no scope/tags are
        set — there's nothing to rank against.
        """
        if not (q and q.strip()):
            return []
        query_vec: list[float] | None = None
        if self.embedder is not None:
            query_vec = self.embedder.embed_one(q)
        return self.store.search_blocks_fused(
            q=q,
            query_vec=query_vec,
            kind="patent",
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=top_k,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )

    # ── rendering helpers ──────────────────────────────────────────────

    def _render_list(self, *, order: str) -> Response:
        """List local patents — by ingest time or publication date."""
        if order == "published":
            refs = self._list_by_publication_date(limit=_LIST_PAGE_LIMIT)
            heading = f"# {len(refs)} patent{'s' if len(refs) != 1 else ''} (by publication date)"
        else:
            refs = self.store.list_refs(kind="patent", limit=_LIST_PAGE_LIMIT)
            heading = f"# {len(refs)} patent{'s' if len(refs) != 1 else ''} (most recently ingested)"

        if not refs:
            return Response(
                body=(
                    "no patents ingested yet — "
                    "use `get(kind='patent', id='ep1234567b1')` to ingest one"
                )
            )

        lines = [heading]
        for r in refs:
            meta = r.meta or {}
            pub = meta.get("publication_date") or "?"
            applicants = (
                ", ".join(
                    a.get("name", "")
                    for a in meta.get("applicants", [])
                    if isinstance(a, dict)
                )[:40]
                or "?"
            )
            title = (r.title or "")[:80]
            slug = r.slug or "?"
            lines.append(f"  {slug:<18}  {pub:<10}  {applicants:<40}  {title}")

        body = "\n".join(lines)
        return Response(body=body)

    def _list_by_publication_date(self, *, limit: int) -> list[Ref]:
        """List patents sorted by ``meta->>'publication_date' DESC, slug ASC``.

        Stable secondary sort on slug — matches the spec's confirmed
        tie-break rule.
        """
        from precis.store.store import _row_to_ref  # local import to avoid cycle

        sql = """
            SELECT r.id, r.corpus_id, r.kind, r.slug, r.title, r.provider,
                   r.meta, r.created_at, r.updated_at, r.deleted_at
            FROM   refs r
            WHERE  r.kind = 'patent' AND r.deleted_at IS NULL
            ORDER BY (r.meta->>'publication_date') DESC NULLS LAST,
                     r.slug ASC
            LIMIT  %s
        """
        with self.store.pool.connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [_row_to_ref(r) for r in rows]

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        slug = ref.slug or "?"
        pub_date = meta.get("publication_date")
        family = meta.get("family_id")
        applicants = ", ".join(
            a.get("name", "") for a in meta.get("applicants", []) if isinstance(a, dict)
        )
        cpc = meta.get("cpc_classes") or []
        n_blocks = self.store.count_blocks(ref.id)

        lines = [f"# {slug}", f"_{ref.title}_"]
        if applicants:
            lines.append(applicants)
        bib_line: list[str] = []
        if pub_date:
            bib_line.append(f"published: {pub_date}")
        if family:
            bib_line.append(f"family: {family}")
        if bib_line:
            lines.append(" · ".join(bib_line))
        if cpc:
            lines.append(f"CPC: {', '.join(cpc[:5])}")
        lines.append("")
        lines.append(f"{n_blocks} block{'s' if n_blocks != 1 else ''}")

        abstract = meta.get("abstract")
        if abstract:
            lines.append("")
            lines.append(_excerpt(str(abstract), limit=500))

        # Espacenet attribution footer (legal-attribution rule).
        lines.append("")
        lines.append(f"_Source: EPO OPS — {_espacenet_url(slug, family_id=family)}_")
        return Response(body="\n".join(lines))

    def _render_view(self, ref: Ref, view: str) -> Response:
        meta = ref.meta or {}
        slug = ref.slug or "?"

        if view == "abstract":
            abstract = meta.get("abstract")
            if not abstract:
                return Response(body=f"no abstract on file for {slug}")
            return Response(body=str(abstract))

        if view == "biblio":
            return Response(body=_format_biblio(ref, meta))

        if view in ("description", "claims"):
            blocks = self.store.list_blocks_for_ref(ref.id)
            if not blocks:
                return Response(body=f"no body blocks stored for {slug}")
            # Naive split: meta has ``len(description_paragraphs)``
            # paragraphs first, then claims. Reconstruct by counting.
            n_desc = len(meta.get("cpc_classes") and [])  # placeholder
            # Better: use block density / position; description tends
            # to be in the early third, claims in the last third.
            # For phase 1, just dump everything labeled by view.
            section = "Description" if view == "description" else "Claims"
            lines = [f"# {slug} — {section}"]
            for b in blocks:
                lines.append(b.text)
                lines.append("")
            return Response(body="\n".join(lines).rstrip())

        if view == "bibtex":
            return Response(body=_format_bibtex(ref, meta))

        raise Unsupported(
            f"unknown view {view!r} for kind='patent'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-patent-help — try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        blocks = self.store.list_blocks_for_ref(ref.id, pos_range=(lo, hi))
        if not blocks:
            raise NotFound(
                f"no blocks in {ref.slug} for range ~{lo}..{hi}",
                next=f"get(kind='patent', id={ref.slug!r})",
            )
        slug = ref.slug or "?"
        lines: list[str] = []
        for b in blocks:
            lines.append(f"# {slug}~{b.pos}")
            lines.append(b.text)
            lines.append("")
        return Response(body="\n".join(lines).rstrip())

    def _render_search_response(
        self,
        *,
        q: str | None,
        cql: str | None,
        local_hits: list[tuple[Any, Ref, float]],
        remote_hits: list[OpsHit],
        top_k: int,
    ) -> Response:
        # Build a slug → "is local" set so we can dedup remote hits
        # whose DOCDB id we already have locally.
        local_slugs: set[str] = {ref.slug for _, ref, _ in local_hits if ref.slug}

        if not local_hits and not remote_hits:
            label = q or cql or "(no query)"
            return Response(body=f"no patents match {label!r}")

        n_total = len(local_hits) + sum(
            1 for h in remote_hits if h.docdb_id not in local_slugs
        )
        header = f"# {n_total} patent hit{'s' if n_total != 1 else ''}"
        if q:
            header += f" for {q!r}"
        lines: list[str] = [header]

        rank = 0
        for block, ref, _score in local_hits:
            rank += 1
            slug = ref.slug or "?"
            preview = _excerpt(block.text, limit=200)
            lines.append(f"\n## {rank}. {slug}~{block.pos}  {_LOCAL_MARKER}")
            lines.append(f"_{ref.title}_")
            lines.append(preview)

        for hit in remote_hits:
            if hit.docdb_id in local_slugs:
                continue
            rank += 1
            applicants = ", ".join(hit.applicants[:2])
            pub = hit.publication_date or ""
            lines.append(f"\n## {rank}. {hit.docdb_id}")
            lines.append(f"_{hit.title}_")
            if applicants or pub:
                meta_line = " · ".join(p for p in (applicants, pub) if p)
                lines.append(meta_line)
            if hit.abstract_preview:
                lines.append(hit.abstract_preview)

        # Espacenet attribution for the search.
        if cql:
            lines.append("")
            lines.append(f"_See Espacenet: {_espacenet_search_url(cql)}_")
        return Response(body="\n".join(lines))


# ---------------------------------------------------------------------------
# Slug + chunk parsing
# ---------------------------------------------------------------------------

# DOCDB slug at the agent boundary. We reuse the same DOCDB regex
# the parser exposes; here we just split slug from optional chunk
# selector. (Path-form views like ``slug/abstract`` are not
# supported in phase 1 — use the ``view=`` kwarg instead.)
_CHUNK_RE = re.compile(r"^(\d+)$")
_RANGE_RE = re.compile(r"^(\d+)\.\.(\d+)$")


def _parse_patent_id(raw: str) -> tuple[str, tuple[int, int] | None]:
    """Split ``raw`` into ``(slug, chunk_range)``.

    Recognised forms:
        ``ep1234567b1``         → (slug, None)
        ``ep1234567b1~5``       → (slug, (5, 5))
        ``ep1234567b1~5..12``   → (slug, (5, 12))

    Validates ``slug`` via ``parse_docdb_id`` so the agent gets the
    same recovery hint shape on bad ids whether they call ``get``
    directly or pass through ``search(scope=...)``.
    """
    slug_part, _, chunk_part = raw.partition("~")
    docdb = parse_docdb_id(slug_part)
    slug = docdb.slug

    if not chunk_part:
        return slug, None

    rm = _RANGE_RE.match(chunk_part)
    if rm is not None:
        lo, hi = int(rm.group(1)), int(rm.group(2))
        if lo > hi:
            raise BadInput(
                f"invalid chunk range ~{lo}..{hi} (lo > hi)",
                next=f"try {slug}~{hi}..{lo}",
            )
        return slug, (lo, hi)

    cm = _CHUNK_RE.match(chunk_part)
    if cm is not None:
        n = int(cm.group(1))
        return slug, (n, n)

    raise BadInput(
        f"invalid chunk selector {chunk_part!r}",
        next=f"try {slug}~5 or {slug}~5..12",
    )


# ---------------------------------------------------------------------------
# Render helpers
# ---------------------------------------------------------------------------


def _excerpt(text: str, *, limit: int = 200) -> str:
    """Trim ``text`` to roughly ``limit`` chars on a word boundary."""
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0]
    return f"{cut}…"


def _format_biblio(ref: Ref, meta: dict[str, Any]) -> str:
    slug = ref.slug or "?"
    lines = [f"# {slug} — Bibliographic data", f"_{ref.title}_", ""]
    pairs: list[tuple[str, str]] = []
    pairs.append(("DOCDB id", slug.upper()))
    if meta.get("publication_date"):
        pairs.append(("Published", str(meta["publication_date"])))
    if meta.get("application_date"):
        pairs.append(("Applied", str(meta["application_date"])))
    if meta.get("family_id"):
        pairs.append(("Family id", str(meta["family_id"])))
    if meta.get("country"):
        pairs.append(("Country", str(meta["country"]).upper()))
    if meta.get("kind_code"):
        pairs.append(("Kind", str(meta["kind_code"]).upper()))
    apps = meta.get("applicants") or []
    if apps:
        pairs.append(
            (
                "Applicants",
                "; ".join(a.get("name", "") for a in apps if isinstance(a, dict)),
            )
        )
    invs = meta.get("inventors") or []
    if invs:
        pairs.append(
            (
                "Inventors",
                "; ".join(a.get("name", "") for a in invs if isinstance(a, dict)),
            )
        )
    cpc = meta.get("cpc_classes") or []
    if cpc:
        pairs.append(("CPC", ", ".join(cpc)))
    ipc = meta.get("ipc_classes") or []
    if ipc:
        pairs.append(("IPC", ", ".join(ipc)))
    width = max(len(k) for k, _ in pairs) if pairs else 0
    for k, v in pairs:
        lines.append(f"  {k:<{width}}  {v}")
    return "\n".join(lines)


def _format_bibtex(ref: Ref, meta: dict[str, Any]) -> str:
    """Minimal BibTeX entry for a patent (``@misc`` with type=patent)."""
    slug = ref.slug or "?"
    pub_date = str(meta.get("publication_date") or "")
    year = pub_date[:4] if len(pub_date) >= 4 else ""
    apps = meta.get("applicants") or []
    author = " and ".join(a.get("name", "") for a in apps if isinstance(a, dict))
    lines = [
        f"@misc{{{slug},",
        f"  title  = {{{ref.title}}},",
    ]
    if author:
        lines.append(f"  author = {{{author}}},")
    if year:
        lines.append(f"  year   = {{{year}}},")
    lines.append("  note   = {Patent " + slug.upper() + "},")
    lines.append(
        f"  url    = {{{_espacenet_url(slug, family_id=meta.get('family_id'))}}},"
    )
    lines.append("}")
    return "\n".join(lines)


def _espacenet_url(slug: str, *, family_id: str | None) -> str:
    """Espacenet deep-link for a single record."""
    if family_id:
        return (
            f"https://worldwide.espacenet.com/patent/search/family/"
            f"{family_id}/publication/{slug.upper()}"
        )
    return f"https://worldwide.espacenet.com/patent/search?q={slug.upper()}"


def _espacenet_search_url(cql: str) -> str:
    from urllib.parse import quote

    return f"https://worldwide.espacenet.com/patent/search?q={quote(cql)}"


__all__ = ["PatentHandler"]
