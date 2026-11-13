"""``web`` kind — fetch and cache the readable content of a URL.

``get(kind='web', id=URL)`` fetches the URL, extracts the main
content with `trafilatura`, and caches the markdown text. An
``application/pdf`` response (or a body with ``%PDF`` magic) takes
the fitz text path the ingest pipeline's fallback uses instead
(gr510701) — reports published as PDFs land their text rather than
the "no readable content" stub.

A GitHub/GitLab **repo-root URL** takes the archive path instead of
the page path (gr608719): :mod:`precis.handlers._web_repo` resolves
the head commit through the forge API, streams the ``.tar.gz``
through ``safe_stream`` under a size cap, and holds the
audit-relevant files (licences, READMEs, packaging manifests,
``CITATION.cff``) as one chunk each — ``get(id='web:<id>~<path>')``
reads one file — plus a ``~tree`` chunk and ``view='tree'`` listing
every path. The resolved commit sha is recorded in cache meta.
Bookmark mode (durable, user-curated refs) and Wayback-archive
integration remain deferred — they need `put` support and a richer
ref schema.

Cache key: the canonical URL. Different URL forms of the same page
(scheme case, default ports, tracking params, trailing slashes,
fragments on non-SPA hosts) collapse onto a single cache row.

Free (no auth, just bandwidth). 7-day TTL — pages mutate often enough
that long-lived caching produces stale answers.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import replace
from typing import Any, ClassVar
from urllib.parse import urlsplit

from precis.errors import BadInput, NotFound, Unsupported, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    _format_cache_footer,
)
from precis.handlers._web_repo import (
    REPO_BLOCK_META,
    RepoTarget,
    build_repo_blocks,
    download_archive,
    parse_repo_url,
    resolve_commit,
)
from precis.handlers._web_repo import (
    content_sha256 as _repo_sha256,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import ChunkInsert
from precis.utils.http import http_client, require_httpx
from precis.utils.optional_deps import require_optional
from precis.utils.url import canonical_url, host_of, is_http_url, slug_from_url

log = logging.getLogger(__name__)


_WEB_BASE_ATTRIBUTION = (
    "Source: web page; content © its publisher. Fetched and extracted "
    "with trafilatura (HTML) or PyMuPDF (PDF). Verify quotes against "
    "the original; quote sparingly."
)

#: Same non-alnum collapse ``slug_from_url`` applies — reused here for
#: the ``?ref=`` suffix on repo snapshot slugs.
_SLUG_STRIP_RE = re.compile(r"[^a-z0-9]+")


# Honest UA (``polite_user_agent``); an operator can override via the
# `WEB_USER_AGENT` env var.


class WebHandler(CacheBackedHandler):
    """``web`` — fetch+extract a URL. Page-fetch mode only in phase 4."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="web",
        title="Web (page fetch)",
        description=(
            "Fetch a web page and return its readable content as markdown. "
            "A GitHub/GitLab repo-root URL instead snapshots the source "
            "archive: LICENSE/README/packaging files land as chunks "
            "(get(id='web:<id>~<path>') reads one file, view='tree' lists "
            "every path) with the resolved commit sha in meta. Cached for "
            "7 days; tracking params and fragments collapse onto a single "
            "cache row. Fetched pages are block-parsed and embedded so "
            "search(kind='web', q=...) lands hits inside page content. "
            "Tag to bookmark (e.g. add=['bookmark']) and link to memory / "
            "paper refs for cross-referencing."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=True,
        # gr533135: a long extracted page over the context result budget
        # refuses whole-body — the base's ``slug~N`` / ``slug~N..M``
        # range read is the targeted read the refusal's next= names.
        supports_chunk_selectors=True,
    )

    provider: ClassVar[str] = "web"
    ttl_seconds: ClassVar[int | None] = 7 * 24 * 60 * 60  # 7 days
    attribution: ClassVar[str] = _WEB_BASE_ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "https://example.com/article"

    # ── canonicalization & cache key ──────────────────────────────────

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        """The canonical URL is the cache key. Reject non-http(s) input."""
        try:
            url = canonical_url(query)
        except ValueError as exc:
            raise BadInput(
                f"not a valid URL: {query!r}",
                next="get(kind='web', id='https://example.com/article')",
            ) from exc
        if not is_http_url(url):
            raise BadInput(
                f"web kind requires http(s) URL; got: {query!r}",
                next="get(kind='web', id='https://example.com/article')",
            )
        return url

    def _slug_for(self, key: str) -> str:
        """Use the readable slug derived from the canonical URL.

        Two distinct pages on the same host with different paths get
        distinct slugs deterministically — no hash suffix needed for
        most cases. Truncated to 60 chars; collisions (rare for typical
        bookmarks) cascade-replace the older entry per
        ``put_cache_entry``'s upsert behaviour.

        ``slug_from_url`` ignores the query, so any URL whose content
        lives in its query string — the query-parameter APIs (NCBI
        idconv, Crossref, OpenAlex) and every ``?id=``/``?page=`` page
        — used to collapse onto the bare path's slug, where the second
        distinct URL tripped ``_guard_slug_collision`` and could never
        be fetched (gr663681). Any canonical URL carrying a query
        therefore gets a ``-q-<sha256(query)[:8]>`` tail: same query →
        same slug (request-hash dedup still applies), different query →
        a different row. Tracking-param-only URLs canonicalise to an
        empty query and keep the clean slug.

        Repo URLs get a richer suffix — ``-ref-<ref>-<hash>`` for the
        documented pin, the same ``-q-<hash>`` for anything else — so
        distinct pins keep distinct rows. The pin hash covers the full
        unsanitised ref because the readable form collapses (``v1.0``
        and ``v1-0`` both slug to ``v1-0``): without it the second pin
        would still land on the first snapshot's row.
        """
        slug = slug_from_url(key)
        query = urlsplit(key).query
        if not query:
            return slug or "web-fetch"
        target = parse_repo_url(key)
        if target is not None and target.ref is not None:
            ref_slug = _SLUG_STRIP_RE.sub("-", target.ref.lower()).strip("-")
            digest = hashlib.sha256(target.ref.encode("utf-8")).hexdigest()[:8]
            return f"{slug}-ref-{ref_slug[:24] or 'x'}-{digest}"
        digest = hashlib.sha256(query.encode("utf-8")).hexdigest()[:8]
        return f"{slug}-q-{digest}" if slug else f"web-fetch-q-{digest}"

    def _recover_key(self, ref, cache):
        """Return the canonical URL stored in cache meta.

        Lets ``mode='refresh'`` work when the caller addressed by
        slug (e.g. the maintenance driver iterating ``WATCH:daily``
        web bookmarks). (gripe:3681 phase 4.)
        """
        return (cache.meta or {}).get("url")

    def _guard_slug_collision(self, key, ref, cache):
        """Refuse to overwrite a row whose stored URL is a different URL.

        Same failure shape perplexity guards (gr517283): the request
        hash missed but the derived slug matched a live row, and the
        base ``get`` would refresh that row in place — silently
        replacing one snapshot's body with another's. The
        ``-ref-<x>-<hash>`` / ``-q-<hash>`` tails separate ``?ref=``
        pins and query-string variants, so a slug match on a
        different canonical URL is a genuine collision
        (60-char slug truncation, a hash-tail match) rather than the
        routine case. Entries written before ``url`` was tracked in
        meta cannot be compared and keep the in-place refresh.
        """
        stored = (cache.meta or {}).get("url")
        if not stored or stored == key:
            return
        raise BadInput(
            f"{self.spec.kind}: {key!r} slugs to {ref.slug!r}, which "
            f"already holds {stored!r} (ref {ref.id}). Refusing to "
            "overwrite it.",
            next=(
                f"get(kind={self.spec.kind!r}, id={ref.slug!r}, "
                "view='body') reads the existing snapshot; "
                f"get(kind={self.spec.kind!r}, id={ref.slug!r}, "
                "mode='refresh') re-fetches that URL in place",
            ),
        )

    # ── verb override: view='tree' ────────────────────────────────────

    def get(
        self,
        *,
        id: str | int | None = None,
        q: str | None = None,
        view: str | None = None,
        tags: list[str] | None = None,
        untags: list[str] | None = None,
        mode: str | None = None,
        ttl_days: int | None = None,
        refresh: bool = False,
        no_fetch: bool = False,
        literal: bool = False,
        **kw: Any,
    ) -> Response:
        """``view='tree'`` — the file listing of a cached repo snapshot.

        Read-only like ``view='body'``: it never fetches — a repo that
        isn't cached yet gets a NotFound naming the fetch call. Every
        other spelling routes to the cache-backed base get.
        """
        if view == "tree":
            return self._repo_tree_view(id=id, q=q, tags=tags, untags=untags)
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
            **kw,
        )

    # ── upstream fetch + extract ──────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        target = parse_repo_url(key)
        if target is not None:
            repo = self._fetch_repo(key, target)
            if repo is not None:
                return repo
            # The forge API 404'd the project lookup: not a repo (or a
            # private one) — the ordinary page fetch gets its honest
            # shot at the URL (a GitLab group page, a forge 404 page).

        httpx = require_httpx()

        import os

        from precis.utils.http import VendorError, polite_safe_get, polite_user_agent
        from precis.utils.rate_limit import RateLimited
        from precis.utils.safe_fetch import SsrfBlocked

        ua = os.environ.get("WEB_USER_AGENT") or polite_user_agent()

        try:
            # follow_redirects=False (http_client's default) — safe_get
            # walks the redirect chain itself, revalidating each Location
            # against the SSRF blocklist. A True default here would let an
            # agent-supplied URL chain into 169.254.169.254 / 127.0.0.1 /
            # RFC1918.
            with http_client(
                timeout=30.0,
                headers={"Accept": "text/html,*/*;q=0.8"},
                user_agent=ua,
            ) as client:
                resp = polite_safe_get(client, key)
        except SsrfBlocked as exc:
            raise Upstream(
                f"fetch refused for {key}: {exc}",
                next=(
                    "the URL (or a redirect target) resolves to a "
                    "non-public address; use a public-internet URL"
                ),
            ) from exc
        except (httpx.HTTPError, VendorError, RateLimited) as exc:
            raise Upstream(
                f"fetch failed for {key}: {exc}",
                next="check the URL is reachable; retry later",
            ) from exc

        if resp.status_code >= 400:
            raise Upstream(
                f"HTTP {resp.status_code} for {key}",
                next="check the URL or wait for the site to recover",
            )

        # What is pinned: the sha256 of the RAW response bytes the server
        # sent (``resp.content``, after transport decoding of gzip/deflate
        # but before charset decoding or trafilatura extraction) — not of
        # the extracted markdown. The nanopub web anchor
        # (``urn:sha256:<this>``) therefore identifies the page as served;
        # a rewritten extractor does not fake drift, a changed page does.
        content_sha256 = hashlib.sha256(resp.content).hexdigest()
        if _looks_like_pdf(resp):
            return self._fetch_pdf(key, resp, content_sha256=content_sha256)

        html = resp.text
        # A missing ``trafilatura`` is a "feature unavailable on this
        # deployment" condition, not a network/upstream failure — raise
        # ``Unsupported`` (not ``Upstream``) so the rendered error is
        # correctly typed while still carrying an actionable reinstall
        # hint. (gripe #39241; core dep since the 2026-08-16 promotion.)
        # Gated here, after the PDF branch, so a trafilatura-less
        # deployment can still extract PDFs.
        trafilatura = require_optional("trafilatura", error_cls=Unsupported)
        # Trafilatura's `extract` returns markdown when configured; we
        # prefer markdown so blocks read naturally and link-grep works.
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_images=False,
            favor_recall=False,
        )

        title = _extract_title(html) or host_of(key) or key

        if not extracted or not extracted.strip():
            # Trafilatura sometimes returns empty for low-text pages
            # (login walls, JS shells). Fall back to a stub note rather
            # than caching nothing.
            body_text = (
                f"(no readable content extracted from {key} - "
                f"page may require JS, login, or have non-article shape)"
            )
        else:
            body_text = extracted.strip()

        # Block-parse + embed via the shared cache-base helper —
        # this is what makes ``search(kind='web', q=...)`` land
        # lexical + semantic hits inside cached pages. Previously
        # we stored one un-embedded monolithic block, which meant
        # a user's bookmark corpus was opaque to both the block-
        # level fused search and to ``random`` (which only draws
        # from ``embedding IS NOT NULL`` rows). Paragraph-level
        # blocks also surface more useful previews in search
        # results and in the random-pick response body.
        return FetchResult(
            title=title,
            body_blocks=self._blocks_from_report(body_text),
            cost_usd=None,  # bandwidth only
            content_sha256=content_sha256,
            meta={
                "url": key,
                "host": host_of(key),
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "extracted_chars": len(body_text),
            },
        )

    def _fetch_pdf(self, key: str, resp, *, content_sha256: str) -> FetchResult:
        """PDF branch — extract text via the ingest pipeline's fitz path.

        Reached only after :func:`_looks_like_pdf` matched, so ``resp``
        is a 2xx whose body is a PDF. A scanned/image-only PDF yields no
        text (the fitz path doesn't OCR): cache an annotated stub naming
        the OCR-capable paper-ingest door rather than re-downloading the
        same bytes on every call inside the TTL.
        """
        # ``fitz`` (PyMuPDF) ships in the ``paper`` extra, so gate like
        # trafilatura above: missing dep → ``Unsupported`` with an
        # install hint, not a bare ImportError.
        require_optional("fitz", extra="paper", error_cls=Unsupported)
        from precis.ingest.marker import extract_pdf_text

        try:
            body_text, pdf_title = extract_pdf_text(resp.content)
        except Exception as exc:
            raise Upstream(
                f"could not extract text from the PDF at {key}",
                next=(
                    "mint a stub then queue the pull: put(kind='paper', "
                    "title='<title>') then put(kind='paper', id='<slug>', "
                    f"args={{'pdf_url': '{key}'}})"
                ),
            ) from exc

        if not body_text:
            body_text = (
                f"(scanned/image-only PDF at {key} — no extractable text "
                "layer; for the OCR-capable ingest door: put(kind='paper', "
                "title='<title>') then put(kind='paper', id='<slug>', "
                f"args={{'pdf_url': '{key}'}}))"
            )

        return FetchResult(
            title=pdf_title or slug_from_url(key) or host_of(key) or key,
            body_blocks=self._blocks_from_report(body_text),
            cost_usd=None,  # bandwidth only
            content_sha256=content_sha256,
            meta={
                "url": key,
                "host": host_of(key),
                "status_code": resp.status_code,
                "content_type": resp.headers.get("content-type", ""),
                "extractor": "pymupdf",
                "extracted_chars": len(body_text),
            },
        )

    # ── forge repo snapshots (gr608719) ───────────────────────────────

    def _fetch_repo(self, key: str, target: RepoTarget) -> FetchResult | None:
        """Archive-snapshot path for a forge repo-root URL.

        ``None`` when the project lookup 404s — the caller then runs
        the ordinary page fetch, which is the honest outcome for a URL
        that is not actually a public repository (a GitLab group page,
        a forge's own 404).
        """
        httpx = require_httpx()

        import os

        from precis.utils.http import (
            VendorError,
            call_with_backoff,
            polite_user_agent,
        )
        from precis.utils.rate_limit import RateLimited
        from precis.utils.safe_fetch import SsrfBlocked

        ua = os.environ.get("WEB_USER_AGENT") or polite_user_agent()
        try:
            # Same redirect discipline as the page path: the client
            # defaults to follow_redirects=False and safe_stream /
            # polite_safe_get revalidate every hop. The archive can be
            # tens of MB, so the timeout is looser than the page's.
            with http_client(
                timeout=60.0,
                headers={"Accept": "application/json,*/*;q=0.8"},
                user_agent=ua,
            ) as client:
                resolved = resolve_commit(client, target)
                if resolved is None:
                    return None
                # resolve_commit's API reads are polite (polite_safe_get);
                # the archive stream takes its own limiter token + backoff.
                tarball = call_with_backoff(
                    lambda: download_archive(client, resolved.archive_url),
                    service="web-fetch",
                    url=resolved.archive_url,
                )
        except SsrfBlocked as exc:
            raise Upstream(
                f"fetch refused for {key}: {exc}",
                next=(
                    "the URL (or a redirect target) resolves to a "
                    "non-public address; use a public-internet URL"
                ),
            ) from exc
        except (httpx.HTTPError, VendorError, RateLimited) as exc:
            raise Upstream(
                f"fetch failed for {key}: {exc}",
                next="check the URL is reachable; retry later",
            ) from exc

        blocks, listing, held_paths = build_repo_blocks(tarball, embedder=self.embedder)
        held_lines = "\n".join(f"  ~{p}" for p in held_paths) or "  (none)"
        summary = (
            f"Source repository snapshot — {target.forge} {target.project}\n"
            f"{resolved.ref_label} · commit {resolved.sha}\n"
            f"{len(listing)} files · {len(held_paths)} held as chunks · "
            f"archive {len(tarball)} bytes\n\n"
            "Held files — read one with get(kind='web', "
            "id='web:<id>~<path>'):\n"
            f"{held_lines}\n\n"
            "Every path + size: get(kind='web', id='web:<id>', view='tree') "
            "or id='web:<id>~tree'"
        )
        blocks.insert(0, ChunkInsert(ord=0, text=summary, meta={REPO_BLOCK_META: True}))

        return FetchResult(
            title=f"{target.project} @ {resolved.sha[:12]}",
            body_blocks=blocks,
            cost_usd=None,  # bandwidth only
            content_sha256=_repo_sha256(tarball),
            meta={
                "url": key,
                "host": host_of(key),
                "extractor": "repo-tarball",
                "forge": target.forge,
                "repo": target.project,
                "ref": resolved.ref_label,
                "commit_sha": resolved.sha,
                "archive_url": resolved.archive_url,
                "archive_bytes": len(tarball),
                "files_total": len(listing),
                "files_held": held_paths,
                "extracted_chars": sum(len(b.text or "") for b in blocks),
            },
        )

    def _resolve_repo_entry(
        self, id: str | int | None, q: str | None
    ) -> tuple[Any, Any] | None:
        """``(Ref, CacheEntry)`` for a ``view='tree'`` address.

        Same spellings a section read takes: the URL (canonicalised to
        a request hash), the advertised slug, or the bare numeric ref
        id. No network — this is a cache lookup only.
        """
        ident = id if isinstance(id, str) and id.strip() else q
        if isinstance(id, int) and not isinstance(id, bool):
            return self.store.get_cache_entry_by_ref_id(kind=self.spec.kind, ref_id=id)
        if not isinstance(ident, str) or not ident.strip():
            return None
        ident = ident.strip()
        try:
            key = self._canonical_key(ident)
        except BadInput:
            key = None
        if key is not None:
            entry = self.store.get_cache_entry(
                provider=self.provider, request_hash=self._hash(key)
            )
            if entry is not None:
                return entry
        entry = self.store.get_cache_entry_by_slug(kind=self.spec.kind, slug=ident)
        if entry is not None:
            return entry
        if ident.isdigit():
            return self.store.get_cache_entry_by_ref_id(
                kind=self.spec.kind, ref_id=int(ident)
            )
        return None

    def _repo_tree_view(
        self,
        *,
        id: str | int | None,
        q: str | None,
        tags: list[str] | None,
        untags: list[str] | None,
    ) -> Response:
        """``view='tree'`` — render the ``~tree`` chunk of a repo snapshot."""
        entry = self._resolve_repo_entry(id, q)
        if entry is None:
            raise NotFound(
                f"web {(id if isinstance(id, str) else q)!r} is not cached yet",
                next=(
                    "get(kind='web', id=<repo-url>) fetches the snapshot "
                    "first, then view='tree' lists it"
                ),
            )
        ref, cache = entry
        if (cache.meta or {}).get("extractor") != "repo-tarball":
            raise BadInput(
                f"view='tree' applies to a fetched repository snapshot — "
                f"{ref.slug!r} is a page fetch",
                next="get(kind='web', id=<github-or-gitlab repo url>)",
            )
        self._apply_tag_ops_if_any(ref.id, tags, untags)
        return self._render_named_section(
            ref, ref.slug or str(ref.id), "tree", tags=None, untags=None, view=None
        )

    # ── block layout overrides for repo snapshots ─────────────────────

    def _split_body_blocks(self, blocks: list[Any]) -> list[Any]:
        """Keep repo-snapshot chunks whole (gr608719).

        A ``~<path>`` section read resolves against ONE chunk — letting
        the base splitter dice a 200 KB LICENSE into 800-char slices
        would make the read return only the first slice. Blocks the
        repo fetch marked ``repo_block`` pass through with their ord
        rewritten to position; ordinary page bodies keep the base
        split-for-embedding behaviour.
        """
        if any((b.meta or {}).get(REPO_BLOCK_META) for b in blocks):
            return [replace(b, ord=i) for i, b in enumerate(blocks)]
        return super()._split_body_blocks(blocks)

    def _render_named_section(
        self,
        ref: Any,
        slug: str,
        section: str,
        *,
        tags: list[str] | None,
        untags: list[str] | None,
        view: str | None,
    ) -> Response:
        """``~<path>`` — exact slug first, then a case-folded match.

        Repo file chunks carry their real path as the slug
        (``LICENSE``, ``README.md``); an agent asking for ``~license``
        or ``~License`` should land the same file. The folded pass
        only runs when the exact lookup misses, then the base's
        handles-listing refusal covers everything else.
        """
        block = self.store.chunks.get_chunk(ref.id, slug=section) if section else None
        if block is None and section:
            folded = section.strip().lower()
            for b in self.store.chunks.list_chunks_for_ref(ref.id):
                if (b.slug or "").lower() == folded:
                    block = b
                    break
        if block is not None:
            return self._render_chunk_selector(
                slug, block.ord, block.ord, tags=tags, untags=untags, view=view
            )
        return super()._render_named_section(
            ref, slug, section, tags=tags, untags=untags, view=view
        )

    # ── render: append source URL + fetched-at line ───────────────────

    def _render(self, ref, cache, *, hit):
        resp = super()._render(ref, cache, hit=hit)
        url = (cache.meta or {}).get("url") or ""
        fetched = cache.fetched_at.date().isoformat() if cache.fetched_at else "?"
        # Surface cache age + freshness state. The MCP critic flagged
        # the absence of the ``(web cache · age Nd · CACHE:fresh)``
        # annotation that ``precis-cache`` documents — a caller had
        # no way to tell a cached vs fresh response, and so no way
        # to decide whether to force a refetch. Using
        # :func:`_format_cache_footer` keeps the wording consistent
        # across every cache-backed kind. (Critic MINOR #11.)
        cache_state = _format_cache_footer(cache)
        deep_link = f"  Source: {url}\n  Fetched: {fetched}\n  Cache:  {cache_state}"
        return Response(
            body=resp.body + "\n" + deep_link,
            cost=resp.cost,
            result_budget_hint=resp.result_budget_hint,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


#: ``%PDF`` may follow a few bytes of junk; the spec allows 1024 (same
#: window ``datasheet_pull`` uses for its magic check).
_PDF_MAGIC_WINDOW = 1024


def _looks_like_pdf(resp) -> bool:
    """``True`` when the response is a PDF: ``application/pdf`` content
    type, or ``%PDF`` magic in the first bytes (servers that answer
    ``application/octet-stream`` or a wrong ``text/*``)."""
    ctype = (resp.headers.get("content-type") or "").split(";", 1)[0].strip()
    if ctype.lower() == "application/pdf":
        return True
    return b"%PDF" in resp.content[:_PDF_MAGIC_WINDOW]


def _extract_title(html: str) -> str | None:
    """Cheap <title> grab. Trafilatura also exposes title metadata,
    but a single regex avoids a second pass through the HTML."""
    import re

    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return None
    title = re.sub(r"\s+", " ", m.group(1)).strip()
    return title or None
