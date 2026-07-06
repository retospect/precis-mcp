"""``EdgarHandler`` — read-only SEC EDGAR filings kind.

Mirrors ``PatentHandler`` (EPO OPS) but talks to the key-less SEC APIs:

- ``search(q=..., tags=..., scope=..., page_size=...)`` — merged local +
  remote EDGAR full-text hits with ``[local]`` / ``[edgar]`` markers.
- ``get(id=<accession>)`` — fetch-as-ingest. First call hits SEC
  (submissions + primary document), parses into section-labelled
  blocks, stores; subsequent calls render from the local store.
- ``get(id='/recent')`` — recently-ingested list (local).
- ``get(id='cik:320193' | 'ticker:aapl')`` — a company's recent filings
  (remote submissions index; does not ingest).
- ``get(id=<accession>, view='diff')`` — quarter-to-quarter section diff
  against the prior same-form filing for that company.
- ``put(...)`` raises ``Unsupported`` (public record, read-only).

Hidden from the agent boundary unless ``PRECIS_EDGAR_USER_AGENT`` and
``PRECIS_EDGAR_RAW_ROOT`` are set (``KindSpec.requires_env``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound, Unsupported
from precis.handlers._edgar_accession import (
    looks_like_accession,
    parse_accession,
)
from precis.handlers._edgar_client import (
    EdgarClientProto,
    EdgarError,
)
from precis.handlers._edgar_ingest import ingest_filing
from precis.handlers._edgar_parse import (
    EdgarHit,
    parse_fts_response,
    parse_submissions,
)
from precis.handlers._edgar_query import build_fts_params
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store import SEMANTIC_DISTANCE_FLOOR, Ref, Tag
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import (
    SearchHit,
    block_hits_to_search_hits,
    merge_and_render,
)
from precis.utils.text import excerpt as _excerpt

_SUPPORTED_VIEWS: tuple[str, ...] = (
    "biblio",
    "body",
    "toc",
    "diff",
)

_REQUIRED_ENV: tuple[str, ...] = (
    "PRECIS_EDGAR_USER_AGENT",
    "PRECIS_EDGAR_RAW_ROOT",
)

_LIST_PAGE_LIMIT = 50
_DEFAULT_REMOTE_PAGE = 20


class EdgarHandler(Handler):
    """Slug-addressed (accession), read-only SEC filing handler."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="edgar",
        title="SEC Filing",
        description=(
            "Read-only SEC EDGAR filing. Accession-slugged "
            "(e.g. 0000320193-23-000106). Search merges local + EDGAR "
            "full-text; get(id=...) fetches + stores. get(id='cik:320193' "
            "| 'ticker:aapl') lists a company's filings; view='diff' shows "
            "quarter-to-quarter section changes. See precis-edgar-help."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=False,
        is_numeric=False,
        id_required=False,
        corpus_role="evidence",
        role="corpus",
        views=_SUPPORTED_VIEWS,
        requires_env=_REQUIRED_ENV,
    )

    def __init__(
        self,
        *,
        hub: Hub,
        client: EdgarClientProto | None = None,
        raw_root: Path | None = None,
    ) -> None:
        if hub.store is None:
            raise InitError("edgar: store required")
        self.store = hub.store
        self.embedder = hub.embedder
        if client is None or raw_root is None:
            import os

            from precis.handlers._edgar_client import EdgarClient

            ua = os.environ.get("PRECIS_EDGAR_USER_AGENT")
            raw = os.environ.get("PRECIS_EDGAR_RAW_ROOT")
            if not (ua and raw):
                missing = [e for e in _REQUIRED_ENV if not os.environ.get(e)]
                raise InitError("edgar: missing env vars " + ", ".join(missing))
            if client is None:
                client = EdgarClient(user_agent=ua)
            if raw_root is None:
                raw_root = Path(raw).expanduser()
        self.client = client
        self.raw_root = raw_root

    # ── verbs ──────────────────────────────────────────────────────────

    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or id == "/" or id == "/recent":
            return self._render_list()

        if not isinstance(id, str):
            raise BadInput(
                f"edgar id must be a string accession, got {type(id).__name__}",
                next="get(kind='edgar', id='0000320193-23-000106')",
            )

        if id.startswith("cik:"):
            return self._render_company_list(cik=id[4:].strip())
        if id.startswith("ticker:"):
            ticker = id[7:].strip()
            cik = self.client.resolve_ticker(ticker)
            if not cik:
                raise NotFound(
                    f"unknown ticker {ticker!r}",
                    next="get(kind='edgar', id='cik:<number>') or check the symbol",
                )
            return self._render_company_list(cik=cik, ticker=ticker)

        slug, chunk = _parse_edgar_id(id)

        ref = self.store.get_ref(kind="edgar", id=slug)
        if ref is None:
            try:
                result = ingest_filing(
                    slug,
                    store=self.store,
                    client=self.client,
                    embedder=self.embedder,
                    raw_root=self.raw_root,
                )
            except NotFound:
                raise
            except EdgarError as e:
                raise NotFound(
                    f"could not fetch filing {slug!r} from SEC: {e}",
                    next="check PRECIS_EDGAR_USER_AGENT and SEC availability",
                ) from e
            ref = self.store.get_ref(kind="edgar", id=result.slug)
            if ref is None:
                raise NotFound(f"filing {slug!r} ingest succeeded but ref missing")

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
        normalized_tags = Tag.normalize_filter(tags, kind="edgar")
        if source not in ("both", "local", "remote"):
            raise BadInput(
                f"invalid source={source!r} - expected 'both', 'local', or 'remote'",
                next="search(kind='edgar', q='...', source='remote')",
            )

        scope_ref_id: int | None = None
        if scope is not None:
            scope_ref = resolve_live_slug_ref(
                self.store,
                kind="edgar",
                id=scope,
                next_hint="search(kind='edgar', q='...') to find one",
            )
            scope_ref_id = scope_ref.id

        local_hits: list[tuple[Any, Ref, float]] = []
        if source != "remote" or scope_ref_id is not None:
            local_hits = self._search_local(
                q=q,
                scope_ref_id=scope_ref_id,
                tags=normalized_tags,
                page_size=page_size,
                mode=mode,
            )

        remote_hits: list[EdgarHit] = []
        params_used: dict[str, str] | None = None
        if scope_ref_id is None and source != "local":
            try:
                params = build_fts_params(q=q, tags=tags, resolver=self.client)
            except BadInput:
                params = None
            if params is not None:
                params_used = params
                try:
                    resp = self.client.search(
                        params, size=max(page_size, _DEFAULT_REMOTE_PAGE)
                    )
                    remote_hits, _total = parse_fts_response(resp.json)
                except EdgarError:
                    remote_hits = []

        if source == "remote" and remote_hits:
            remote_hits = [
                h
                for h in remote_hits
                if self.store.get_ref(kind="edgar", id=h.accession) is None
            ]

        response = self._render_search_response(
            q=q,
            params=params_used,
            local_hits=local_hits,
            remote_hits=remote_hits,
            page_size=page_size,
        )

        # Accession-shaped query that found nothing → point at the direct
        # fetch, mirroring patent's DOCDB-shape branch.
        if (
            not local_hits
            and not remote_hits
            and q is not None
            and looks_like_accession(q)
        ):
            from precis.utils.next_block import render_next_section

            slug = parse_accession(q).dashed
            trailer = render_next_section(
                [
                    (
                        f"get(kind='edgar', id={slug!r})",
                        "fetch this filing from SEC directly",
                    ),
                ]
            )
            response = Response(
                body=response.body + "\n\n" + trailer, cost=response.cost
            )

        return response

    def put(self, **_kw: Any) -> Response:  # type: ignore[override]
        raise Unsupported(
            "edgar kind is read-only (public record)",
            next=(
                "use get(kind='edgar', id=<accession>) to fetch a filing, "
                "or search(kind='edgar', q='...') to find one"
            ),
        )

    # ── search helpers ─────────────────────────────────────────────────

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
            kind="edgar",
            scope_ref_id=scope_ref_id,
            tags=tags,
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
        )

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
        if not (q and q.strip()):
            return []
        normalized_tags = Tag.normalize_filter(tags, kind="edgar")
        triples = self._search_local(
            q=q,
            scope_ref_id=None,
            tags=normalized_tags,
            page_size=page_size,
            query_vec=query_vec,
            mode=mode,
        )
        return block_hits_to_search_hits(triples, kind="edgar")

    # ── rendering ──────────────────────────────────────────────────────

    def _render_list(self) -> Response:
        refs = self.store.list_refs(kind="edgar", limit=_LIST_PAGE_LIMIT)
        if not refs:
            return Response(
                body=(
                    "no filings ingested yet - try "
                    "`get(kind='edgar', id='ticker:nvda')` to list NVIDIA's recent "
                    "filings, then `get(kind='edgar', id='<accession>')` to read one "
                    "(or `get(kind='edgar', id='0000320193-23-000106')` to fetch a "
                    "filing directly by accession number)"
                )
            )
        lines = [
            f"# {len(refs)} filing{'s' if len(refs) != 1 else ''} (recently ingested)"
        ]
        for r in refs:
            meta = r.meta or {}
            form = meta.get("form") or "?"
            filed = meta.get("filed_date") or "?"
            company = (meta.get("company") or "")[:40]
            slug = r.slug or "?"
            lines.append(f"  {slug:<22}  {form:<8}  {filed:<10}  {company}")
        return Response(body="\n".join(lines))

    def _render_company_list(self, *, cik: str, ticker: str | None = None) -> Response:
        cik_digits = "".join(c for c in cik if c.isdigit())
        if not cik_digits:
            raise BadInput(
                f"invalid CIK {cik!r}",
                next="get(kind='edgar', id='cik:320193')",
            )
        try:
            subs = parse_submissions(self.client.submissions(cik_digits))
        except EdgarError as e:
            raise NotFound(
                f"could not fetch submissions for CIK {cik_digits}: {e}"
            ) from e
        if not subs.filings:
            return Response(body=f"no filings on record for CIK {cik_digits}")

        label = subs.company or (ticker or f"CIK {cik_digits}")
        lines = [f"# {label} — {len(subs.filings)} recent filings"]
        for f in subs.filings[:_LIST_PAGE_LIMIT]:
            when = f.report_date or f.filed_date or "?"
            lines.append(f"  {f.accession:<22}  {f.form:<8}  {when:<10}")
        lines.append("")
        lines.append("_get(kind='edgar', id='<accession>') to ingest + read any row_")
        return Response(body="\n".join(lines))

    def _render_overview(self, ref: Ref) -> Response:
        meta = ref.meta or {}
        handle = handle_registry.format_handle("edgar", ref.id)
        lines = [f"# {handle}", f"_{ref.title}_"]
        bib: list[str] = []
        if meta.get("form"):
            bib.append(f"form: {meta['form']}")
        if meta.get("filed_date"):
            bib.append(f"filed: {meta['filed_date']}")
        if meta.get("period_of_report"):
            bib.append(f"period: {meta['period_of_report']}")
        if bib:
            lines.append(" · ".join(bib))
        items = meta.get("items") or []
        if items:
            lines.append(f"items: {', '.join(str(i) for i in items[:12])}")

        # Leading body excerpt.
        blocks = self.store.list_blocks_for_ref(ref.id, pos_range=(0, 3))
        if blocks:
            lead = " ".join(b.text for b in blocks)
            lines.append("")
            lines.append(_excerpt(lead, limit=500))

        lines.append("")
        lines.append(f"_Source: SEC EDGAR - {_edgar_url(meta)}_")
        return Response(body="\n".join(lines))

    def _render_view(self, ref: Ref, view: str) -> Response:
        meta = ref.meta or {}
        handle = handle_registry.format_handle("edgar", ref.id)

        if view == "biblio":
            return Response(body=_format_biblio(ref, meta))

        if view == "body":
            blocks = self.store.list_blocks_for_ref(ref.id)
            if not blocks:
                return Response(body=f"no body blocks stored for {ref.slug}")
            lines = [f"# {handle} - Body"]
            for b in blocks:
                lines.append(b.text)
                lines.append("")
            return Response(body="\n".join(lines).rstrip())

        if view == "toc":
            return self._render_toc(ref)

        if view == "diff":
            from precis.handlers._edgar_diff import render_diff

            return render_diff(store=self.store, ref=ref)

        raise Unsupported(
            f"unknown view {view!r} for kind='edgar'",
            options=list(_SUPPORTED_VIEWS),
            next=f"see precis-edgar-help - try views: {', '.join(_SUPPORTED_VIEWS)}",
        )

    def _render_toc(self, ref: Ref) -> Response:
        from precis.utils.toc_db import render_from_store

        handle = handle_registry.format_handle("edgar", ref.id)
        body = render_from_store(
            store=self.store,
            ref_id=ref.id,
            handle=handle,
            kind="edgar",
        )
        return Response(body=body)

    def _render_chunks(self, ref: Ref, chunk: tuple[int, int]) -> Response:
        lo, hi = chunk
        handle = handle_registry.format_handle("edgar", ref.id)
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
        params: dict[str, str] | None,
        local_hits: list[tuple[Any, Ref, float]],
        remote_hits: list[EdgarHit],
        page_size: int,
    ) -> Response:
        local_stream: list[SearchHit] = block_hits_to_search_hits(
            local_hits,
            kind="edgar",
            source="local",
            excerpt=200,
            ref_level_dedupe=True,
        )
        remote_stream: list[SearchHit] = [
            _edgar_hit_to_search_hit(h) for h in remote_hits
        ]
        query_desc = q or (params.get("q") if params else None)
        response = merge_and_render(
            [local_stream, remote_stream],
            page_size=page_size,
            query=query_desc,
            header_noun="filing hit",
            mode="priority",
            empty_body=f"no filings match {(query_desc or '(no query)')!r}",
        )
        return response


# ---------------------------------------------------------------------------
# Slug + chunk parsing
# ---------------------------------------------------------------------------

_RANGE_RE = re.compile(r"^(\d+)\.\.(\d+)$")
_CHUNK_RE = re.compile(r"^(\d+)$")


def _parse_edgar_id(raw: str) -> tuple[str, tuple[int, int] | None]:
    """Split ``raw`` into ``(accession-slug, chunk_range)``.

    Recognised forms::

        0000320193-23-000106         → (slug, None)
        0000320193-23-000106~5       → (slug, (5, 5))
        0000320193-23-000106~5..12   → (slug, (5, 12))
    """
    slug_part, _, chunk_part = raw.partition("~")
    slug = parse_accession(slug_part).dashed
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


def _edgar_hit_to_search_hit(hit: EdgarHit) -> SearchHit:
    extras: tuple[str, ...] = ()
    detail = " · ".join(p for p in (hit.form, hit.filed_date or "") if p)
    if detail:
        extras = (detail,)
    title = hit.company or hit.accession
    if hit.form:
        title = f"{title} — {hit.form}"
    return SearchHit(
        score=0.0,
        kind="edgar",
        slug=hit.accession,
        title=title,
        preview="",
        source="edgar",
        extra_lines=extras,
        dedupe_key=f"edgar:{hit.accession}",
    )


def _format_biblio(ref: Ref, meta: dict[str, Any]) -> str:
    handle = handle_registry.format_handle("edgar", ref.id)
    lines = [f"# {handle} - Bibliographic data", f"_{ref.title}_", ""]
    pairs: list[tuple[str, str]] = [
        ("Accession", meta.get("accession") or ref.slug or "?")
    ]
    for label, key in (
        ("Company", "company"),
        ("CIK", "cik"),
        ("Ticker", "ticker"),
        ("Form", "form"),
        ("Filed", "filed_date"),
        ("Period", "period_of_report"),
        ("Primary doc", "primary_doc"),
    ):
        val = meta.get(key)
        if val:
            pairs.append((label, str(val)))
    items = meta.get("items") or []
    if items:
        pairs.append(("Items", ", ".join(str(i) for i in items)))
    width = max(len(k) for k, _ in pairs)
    for k, v in pairs:
        lines.append(f"  {k:<{width}}  {v}")
    lines.append("")
    lines.append(f"_Source: SEC EDGAR - {_edgar_url(meta)}_")
    return "\n".join(lines)


def _edgar_url(meta: dict[str, Any]) -> str:
    """EDGAR filing-archive deep link from meta."""
    cik = str(meta.get("cik") or "").lstrip("0")
    dashless = str(meta.get("accession") or "").replace("-", "")
    if cik and dashless:
        return f"https://www.sec.gov/Archives/edgar/data/{cik}/{dashless}/"
    return "https://www.sec.gov/edgar/search/"


__all__ = ["EdgarHandler"]
