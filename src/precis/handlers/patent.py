"""``PatentHandler`` — read-only patent kind backed by EPO OPS.

Phase 1 surface (deferred items in ``docs/user-facing/patent-kind-spec.md``):

- ``search(q=..., tags=..., scope=..., page_size=...)`` — merged local +
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

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._patent_cql import build_cql
from precis.handlers._patent_ingest import (
    AWAITING_FULLTEXT_TAG,
    FULLTEXT_UNAVAILABLE_TAG,
    ingest_patent,
)
from precis.handlers._patent_ops import (
    OpsClientProto,
    OpsError,
    OpsHttpError,
)
from precis.handlers._patent_slug import looks_like_docdb, parse_docdb_id
from precis.handlers._patent_xml import OpsHit, parse_search_response
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Tag
from precis.store._mappers import _REFS_COLS_ALIASED, _row_to_ref
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    merge_and_render,
)
from precis.utils.text import excerpt as _excerpt

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

# EPO credentials resolve through the secrets vault (ADR 0055); the raw-root is
# a filesystem path that stays a plain env var.
_REQUIRED_SECRETS: tuple[str, ...] = (
    "EPO_OPS_CLIENT_KEY",
    "EPO_OPS_CLIENT_SECRET",
)
_REQUIRED_ENV: tuple[str, ...] = ("PRECIS_PATENT_RAW_ROOT",)

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
        supports_search_hits=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
        # A patent is citable evidence in the document family.
        corpus_role="evidence",
        role="corpus",
        views=_SUPPORTED_VIEWS,
        requires_env=_REQUIRED_ENV,
        requires_secret=_REQUIRED_SECRETS,
    )

    def __init__(
        self,
        *,
        hub: Hub,
        ops: OpsClientProto | None = None,
        raw_root: Path | None = None,
    ) -> None:
        if hub.store is None:
            raise InitError("patent: store required")
        self.store = hub.store
        self.embedder = hub.embedder
        # Production path: read the env trio that this handler
        # declares in :data:`_REQUIRED_ENV`. The kind_gate has
        # already enforced presence before we land here, but a
        # defensive raise prevents silent drift between the gate's
        # requires_env tuple and what __init__ actually consumes.
        # Test path: callers pass explicit ``ops=`` / ``raw_root=``
        # so a fake OPS client can stand in for the network.
        if ops is None or raw_root is None:
            import os

            from precis import secrets as _secrets
            from precis.handlers._patent_ops import OpsClient

            key = _secrets.get_secret("EPO_OPS_CLIENT_KEY")
            secret = _secrets.get_secret("EPO_OPS_CLIENT_SECRET")
            raw = os.environ.get("PRECIS_PATENT_RAW_ROOT")
            if not (key and secret and raw):
                missing = [e for e in _REQUIRED_ENV if not os.environ.get(e)] + [
                    s for s in _REQUIRED_SECRETS if not _secrets.is_available(s)
                ]
                raise InitError("patent: missing " + ", ".join(missing))
            if ops is None:
                ops = OpsClient(
                    key=key,
                    secret=secret,
                    user_agent=os.environ.get("EPO_OPS_USER_AGENT"),
                )
            if raw_root is None:
                raw_root = Path(raw).expanduser()
        self.ops = ops
        self.raw_root = raw_root

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
        page_size: int = 10,
        source: str = "both",
        mode: str | None = None,
        **_kw: Any,
    ) -> Response:
        # Tag normalisation. Per-kind axis enforcement: closed tags
        # outside ``{SRC, CACHE}`` raise BadInput at the agent boundary.
        normalized_tags = Tag.normalize_filter(tags, kind="patent")

        # ``source=`` picks which leg(s) run. ``'remote'`` additionally
        # filters out hits whose DOCDB id is already in the local
        # store, so the agent sees only patents it hasn't fetched yet
        # — the natural "prior-art sweep" mode. See
        # ``docs/user-facing/search-future-filters.md`` §7.
        if source not in ("both", "local", "remote"):
            raise BadInput(
                f"invalid source={source!r} - expected 'both', 'local', or 'remote'",
                next="search(kind='patent', q='...', source='remote')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="patent",
                id=scope,
                next_hint="search(kind='patent', q='...') to find one",
            )
            scope_ref_id = scope_ref.id

        # Local leg: hybrid (lex + semantic) over patent blocks.
        # Skipped when the caller asked for remote-only; scope= implies
        # local-only (searching one specific patent remotely makes no
        # sense) so the local leg always runs when scope is set.
        local_hits: list[tuple[Any, Ref, float]] = []
        if source != "remote" or scope_ref_id is not None:
            local_hits = self._search_local(
                q=q,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                page_size=page_size,
                mode=mode,
            )

        # Remote leg: only when q= or a CQL-liftable tag is present
        # AND scope is None (scope is local-only — searching one
        # specific patent doesn't make sense remotely) AND the caller
        # didn't ask for local-only.
        remote_hits: list[OpsHit] = []
        cql_used: str | None = None
        if scope_ref_id is None and source != "local":
            try:
                cql = build_cql(q=q, tags=tags, store=self.store)
            except BadInput:
                # No q= and no liftable tag — local-only search is fine.
                cql = None
            if cql is not None:
                cql_used = cql
                try:
                    ops_response = self.ops.search(
                        cql,
                        range_start=1,
                        range_end=max(page_size, _DEFAULT_REMOTE_PAGE),
                    )
                    remote_hits, _total = parse_search_response(ops_response.xml)
                except OpsHttpError as e:
                    # HTTP 400 = OPS rejected the CQL itself (syntax,
                    # unknown field, malformed value). This is *caller*
                    # error — silently empty-ing the remote leg made
                    # the user see "no patents match" when the truth was
                    # "your query is invalid". Surface the upstream
                    # complaint so they can fix the query. Other HTTP
                    # failures (5xx, 403 quota, network) stay best-
                    # effort below.
                    if e.status == 400:
                        raise BadInput(
                            f"OPS rejected the CQL query "
                            f"{cql!r}: {e.body_preview[:300]}",
                            next=(
                                "bare phrases auto-promote to "
                                '(ti="..." OR ab="..."); for explicit '
                                "CQL use field=value with OPS field names "
                                "(ti / ab / pa / pact / cpc / ipc / pd / "
                                "famn). Reference: "
                                "https://worldwide.espacenet.com/help. "
                                "Skill: get(kind='skill', id='precis-patent-help')"
                            ),
                        ) from e
                    remote_hits = []
                except OpsError:
                    # Network/auth/quota/5xx — keep best-effort behaviour.
                    remote_hits = []

        # source='remote' — dedupe against local so the caller sees
        # only patents they haven't fetched yet. One point lookup per
        # remote hit; the OPS page size caps this at a few dozen.
        if source == "remote" and remote_hits:
            remote_hits = [
                h
                for h in remote_hits
                if self.store.get_ref(kind="patent", id=h.docdb_id) is None
            ]

        response = self._render_search_response(
            q=q,
            cql=cql_used,
            local_hits=local_hits,
            remote_hits=remote_hits,
            page_size=page_size,
        )

        # DOCDB-shaped query that found nothing → mirror paper's
        # DOI-shape branch (paper.py:397-426): the caller is hunting
        # for a specific publication, not running a topic search.
        # Route them at the finding-chase + OPS fetch pipeline so the
        # next step is a single action, not 3-5 keyword retries.
        if not local_hits and not remote_hits and q is not None and looks_like_docdb(q):
            from precis.utils.next_block import render_next_section

            docdb = re.sub(r"[\s.]", "", q.lower())
            trailer = render_next_section(
                [
                    (
                        f"get(kind='patent', id={docdb!r})",
                        "fetch this patent from OPS directly",
                    ),
                    (
                        "put(kind='finding', title='<short claim>', "
                        f"body='<claim + setup>', cited_in='patent:{docdb}', "
                        "scope={'...': '...'})",
                        "register as a chase target if OPS doesn't have it",
                    ),
                ]
            )
            response = Response(
                body=response.body + "\n\n" + trailer,
                cost=response.cost,
            )

        return response

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
        page_size: int,
        query_vec: list[float] | None = None,
        mode: str | None = None,
    ) -> list[tuple[Any, Ref, float]]:
        """Run the mode-dispatched lex/semantic search over patent blocks.

        Returns an empty list when q= is empty AND no scope/tags are
        set — there's nothing to rank against.

        ``query_vec=`` may be pre-supplied by the runtime cross-kind
        dispatcher to avoid an embed_one(q) per kind in the fan-out.
        ``mode='lexical'`` forces the keyword leg (no embed).
        """
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        return self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="patent",
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=page_size,
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
                    "no patents ingested yet - "
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
        """List patents sorted by ``meta->>'publication_date' DESC, cite_key ASC``.

        Stable secondary sort on cite_key — matches the spec's
        confirmed tie-break rule. v2 has no ``refs.slug`` column; the
        cite_key handle comes from ``ref_identifiers`` (ADR 0008), so
        the ORDER BY plumbs through that lookup.
        """
        sql = f"""
            SELECT {_REFS_COLS_ALIASED}
            FROM   refs r
            WHERE  r.kind = 'patent' AND r.deleted_at IS NULL
            ORDER BY (r.meta->>'publication_date') DESC NULLS LAST,
                     (SELECT min(id_value) FROM ref_identifiers
                       WHERE ref_id = r.ref_id AND id_kind = 'cite_key') ASC
            LIMIT  %s
        """
        with self.store.pool.connection() as conn:
            rows = conn.execute(sql, (limit,)).fetchall()
        return [_row_to_ref(r) for r in rows]

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        slug = ref.slug or "?"
        handle = handle_registry.format_handle("patent", ref.id)
        pub_date = meta.get("publication_date")
        family = meta.get("family_id")
        applicants = ", ".join(
            a.get("name", "") for a in meta.get("applicants", []) if isinstance(a, dict)
        )
        cpc = meta.get("cpc_classes") or []

        lines = [f"# {handle}", f"_{ref.title}_"]
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
        # Full-text status. Silent for fully-ingested patents —
        # agents discover the ``~N`` chunk range via views. Surfaces
        # one sentence only when OPS didn't serve description or
        # claims at ingest time, so the agent knows what happened
        # and whether to expect an auto-retry.
        open_tags = {
            t.value for t in self.store.tags_for(ref.id) if t.namespace == "open"
        }
        if FULLTEXT_UNAVAILABLE_TAG in open_tags:
            lines.append("")
            lines.append(
                "_full text unavailable from OPS - "
                "searchable by abstract + biblio only_"
            )
        elif AWAITING_FULLTEXT_TAG in open_tags:
            retry_at = meta.get("fulltext_retry_at") or ""
            when = retry_at[:10] if retry_at else "soon"
            lines.append("")
            lines.append(
                f"_full text not yet indexed by OPS - queued for auto-retry on {when}_"
            )

        abstract = meta.get("abstract")
        if abstract:
            lines.append("")
            lines.append(_excerpt(str(abstract), limit=500))

        # Espacenet attribution footer (legal-attribution rule).
        lines.append("")
        lines.append(f"_Source: EPO OPS - {_espacenet_url(slug, family_id=family)}_")
        return Response(body="\n".join(lines))

    def _render_view(self, ref: Ref, view: str) -> Response:
        meta = ref.meta or {}
        slug = ref.slug or "?"
        handle = handle_registry.format_handle("patent", ref.id)

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
            # Phase 1: dump every body block under whichever section
            # the caller asked for. A future cut should split
            # description vs claims by block density / position
            # (description in the early third, claims in the last);
            # for now the pure dump matches what the ingester labels.
            section = "Description" if view == "description" else "Claims"
            lines = [f"# {handle} - {section}"]
            for b in blocks:
                lines.append(b.text)
                lines.append("")
            return Response(body="\n".join(lines).rstrip())

        if view == "bibtex":
            return Response(body=_format_bibtex(ref, meta))

        raise Unsupported(
            f"unknown view {view!r} for kind='patent'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-patent-help - try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        handle = handle_registry.format_handle("patent", ref.id)
        blocks = self.store.list_blocks_for_ref(ref.id, pos_range=(lo, hi))
        if not blocks:
            raise NotFound(
                f"no blocks in {ref.slug} for range ~{lo}..{hi}",
                next=f"get(id={handle!r})",
            )
        slug = ref.slug or "?"
        lines: list[str] = []
        for b in blocks:
            lines.append(
                f"# {handle_registry.try_format(ref.kind, b.id, chunk=True) or f'{slug}~{b.pos}'}"
            )
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
        page_size: int,
    ) -> Response:
        """Merge local + remote hits into one rendered response.

        Delegates to :func:`merge_and_render` so the rank-dedupe-
        label-render pipeline is shared with cross-kind search.
        Local hits keep priority (mode='priority'); remote hits
        whose DOCDB id is already present locally drop via the
        ``dedupe_key`` field rather than the previous bespoke
        ``local_slugs`` set.
        """
        # ``ref_level_dedupe=True`` collapses the local stream's
        # dedup identity to the DOCDB slug (one identity per
        # patent ref, not per block) so the OPS remote stream's
        # ``dedupe_key='patent:<docdb>'`` matches and remote rows
        # for already-local patents drop. Block-level dedup would
        # never collide with the ref-keyed remote stream.
        local_stream: list[SearchHit] = block_hits_to_search_hits(
            local_hits,
            kind="patent",
            source="local",
            excerpt=200,
            ref_level_dedupe=True,
        )
        remote_stream: list[SearchHit] = [
            _ops_hit_to_search_hit(h) for h in remote_hits
        ]

        response = merge_and_render(
            [local_stream, remote_stream],
            page_size=page_size,
            query=q or cql,
            header_noun="patent hit",
            mode="priority",
            empty_body=f"no patents match {(q or cql or '(no query)')!r}",
        )

        # Espacenet attribution for the search itself.
        if cql:
            response = Response(
                body=response.body
                + f"\n\n_See Espacenet: {_espacenet_search_url(cql)}_",
                cost=response.cost,
            )
        return response

    # ── search_hits: structured form for cross-kind merge ──────────

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        tags: list[str] | None = None,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        """Local-only block-level search returned as ``SearchHit``s.

        Cross-kind merge intentionally skips the OPS remote leg —
        upstream calls cost money and shouldn't fire on every
        cross-kind search. Operators who want OPS hits run the
        single-kind ``search(kind='patent', q=...)`` directly.

        ``query_vec=`` may be pre-supplied by the runtime cross-kind
        dispatcher (computed once for all kinds).
        """
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind="patent")
        triples = self._search_local(
            q=q,
            scope_ref_id=None,
            tags=normalized_tags,
            page_size=page_size,
            query_vec=query_vec,
            mode=mode,
        )
        return block_hits_to_search_hits(triples, kind="patent")


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


def _ops_hit_to_search_hit(hit: OpsHit) -> SearchHit:
    """Adapt an OPS remote-search hit into a ``SearchHit``.

    ``source='ops'`` so the renderer marks remote rows with
    ``[ops]`` (mirroring the local rows' ``[local]`` marker).
    The DOCDB id becomes both the ``slug`` (for the citation
    handle) and the ``dedupe_key`` (so a remote hit that's
    already in the local store drops in priority-merge mode).
    """
    applicants = ", ".join(hit.applicants[:2])
    pub = hit.publication_date or ""
    extras: tuple[str, ...] = ()
    if applicants or pub:
        extras = (" · ".join(p for p in (applicants, pub) if p),)
    return SearchHit(
        # OPS doesn't return a relevance score, so use a sentinel
        # zero — the rank position within the remote stream is the
        # only ranking signal merge_and_render uses anyway in
        # priority mode.
        score=0.0,
        kind="patent",
        slug=hit.docdb_id,
        title=hit.title,
        preview=hit.abstract_preview or "",
        source="ops",
        extra_lines=extras,
        dedupe_key=f"patent:{hit.docdb_id}",
    )


def _format_biblio(ref: Ref, meta: dict[str, Any]) -> str:
    slug = ref.slug or "?"
    handle = handle_registry.format_handle("patent", ref.id)
    lines = [f"# {handle} - Bibliographic data", f"_{ref.title}_", ""]
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
