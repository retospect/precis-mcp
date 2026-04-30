"""Perplexity Sonar — three cache-backed kinds (websearch / think / research).

Each subclass picks a different Sonar model + timeout + TTL:

    kind        model                      timeout   TTL       cost
    websearch   sonar                      30s       7 days    ~$0.001/call
    think       sonar-reasoning-pro        120s      30 days   ~$0.005/call
    research    sonar-deep-research        600s      pinned    ~$0.50/call

Cache key is ``<model>:<query>`` so the same query under different
models never collides. The cache row provider is ``'perplexity'`` for
all three (pre-existing in the providers table).

Attribution policy (per Perplexity's Terms of Service): every public/
shared output must disclose AI generation; Standard and Pro tiers are
restricted to personal / non-commercial use. The footer also tells the
agent that Perplexity is *not* a primary source — the inline ``[N]``
citations surfaced in the body are the real sources to verify and cite.

Network failures map to ``Upstream`` with model-specific guidance.
401 → ``Upstream`` (auth) so the agent surfaces "fix your key" rather
than silently giving up.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, ClassVar

from precis.errors import BadInput, NotFound, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.handlers._link_tag_ops import (
    apply_link_ops,
    apply_tag_ops,
    format_link_tag_ack,
    validate_link_args,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.md_parse import parse_markdown
from precis.utils.next_block import render_next_section
from precis.utils.optional_deps import require_optional

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.store import Store

log = logging.getLogger(__name__)

_SONAR_URL = "https://api.perplexity.ai/chat/completions"

# Per https://www.perplexity.ai/hub/legal/terms-of-service: every
# public / shared output must disclose AI use; Standard/Pro tiers are
# restricted to personal / non-commercial. Footer text intentionally
# verbose to defend against silent embedding by downstream agents.
_PERPLEXITY_ATTRIBUTION_TEMPLATE = (
    "Source: Perplexity AI ({model}). Perplexity is **not** a primary "
    "source — the numbered [N] citations in the answer link the actual "
    "sources; verify them before citing in publications. Per Perplexity's "
    "Terms of Service: disclose AI use in any public output; Standard/Pro "
    "tiers are restricted to personal / non-commercial use."
)


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _PerplexityBase(CacheBackedHandler):
    """Common Perplexity Sonar handler. Subclasses pin the model + tier."""

    # ── Sonar model identifier ────────────────────────────────────────
    model: ClassVar[str] = ""

    # ── HTTP timeout per tier (seconds) ───────────────────────────────
    timeout: ClassVar[int] = 30

    # ── per-call cost in USD (best estimate; recorded with cache row) ─
    cost_per_call_usd: ClassVar[float] = 0.0

    # ── inherited from CacheBackedHandler (subclasses override) ──────
    provider: ClassVar[str] = "perplexity"
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "your question"
    # ttl_seconds + attribution + spec set by subclasses.

    def __init__(self, *, store: Store, embedder: Embedder | None = None) -> None:
        super().__init__(store=store)
        # Embedder is optional: API-fetched cache entries today land
        # without vectors (single-block bodies; lexical search suffices).
        # `put(mode='import')` parses pasted reports into many blocks
        # and benefits from per-block vectors so semantic search across
        # imported research works. When no embedder is configured the
        # blocks still land — just without vectors.
        self.embedder = embedder

    # ── canonicalize: trim + include model so kinds don't collide ────

    def _canonical_key(self, query: str) -> str:
        q = (query or "").strip()
        if not q:
            raise BadInput(
                f"{self.spec.kind} requires a non-empty query",
                next=f"get(kind={self.spec.kind!r}, id='your question')",
            )
        # Cache key includes the model so same query under different
        # tiers cache separately.
        return f"{self.model}:{q}"

    def _slug_for(self, key: str) -> str:
        # Strip the "<model>:" prefix added by _canonical_key when
        # deriving a human-readable slug — the model is recorded in
        # cache_state.model already.
        _, _, q = key.partition(":")
        from precis.utils.slug import slug_from_text

        return slug_from_text(q, max_len=60) or "perplexity-query"

    # ── auth + transport ──────────────────────────────────────────────

    @staticmethod
    def _api_key() -> str:
        key = (os.environ.get("PERPLEXITY_API_KEY") or "").strip()
        if not key:
            raise Upstream(
                "PERPLEXITY_API_KEY not set",
                next="export PERPLEXITY_API_KEY=... (https://www.perplexity.ai/settings/api)",
            )
        return key

    def _fetch(self, key: str) -> FetchResult:
        httpx = require_optional("httpx", extra="external")

        # Strip the "<model>:" prefix to get the real prompt.
        _, _, query = key.partition(":")

        api_key = self._api_key()
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [{"role": "user", "content": query}],
            "return_citations": True,
            "web_search_options": {"search_context_size": "high"},
        }
        try:
            with httpx.Client(timeout=float(self.timeout)) as client:
                resp = client.post(
                    _SONAR_URL,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.TimeoutException as exc:
            raise Upstream(
                f"Perplexity {self.model} timed out after {self.timeout}s",
                next=(
                    "try a shorter query, or use kind='research' for "
                    "slow-paced multi-step work"
                ),
            ) from exc
        except httpx.HTTPError as exc:
            raise Upstream(
                f"Perplexity transport error: {exc}",
                next="check connectivity; retry later",
            ) from exc

        if resp.status_code == 401:
            raise Upstream(
                "Perplexity rejected the API key (HTTP 401)",
                next="check PERPLEXITY_API_KEY",
            )
        if resp.status_code == 429:
            raise Upstream(
                "Perplexity rate limit (HTTP 429)",
                next="wait and retry; consider a slower tier (think/research)",
            )
        if resp.status_code >= 400:
            raise Upstream(
                f"Perplexity HTTP {resp.status_code}: "
                f"{resp.text[:200] if resp.text else 'no body'}",
                next="retry later",
            )

        data = resp.json()
        body, citations = _format_perplexity_body(data)
        title = _title_for_query(query)

        return FetchResult(
            title=title,
            body_blocks=[BlockInsert(pos=0, text=body)],
            cost_usd=self.cost_per_call_usd,
            meta={
                "model": self.model,
                "query": query,
                "citation_count": len(citations),
                "citations": citations,
            },
        )

    # ── render: append "Sources:" + Next: trailer ────────────────────

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        resp = super()._render(ref, cache, hit=hit)
        meta = cache.meta or {}
        citations = meta.get("citations") or []
        # The body already includes "Sources:" inline (from the fetch
        # formatter), but on cache hit we surface a Next: trailer that
        # points at any other tier the agent might want to escalate to.
        nav: list[tuple[str, str]] = []
        # Suggest the next tier up, except for research (already top tier).
        if self.spec.kind == "websearch":
            nav.append(
                (
                    f"get(kind='think', id={meta.get('query')!r})",
                    "deeper analytical answer (~$0.005/call)",
                )
            )
        elif self.spec.kind == "think":
            nav.append(
                (
                    f"get(kind='research', id={meta.get('query')!r})",
                    "multi-step deep research (~$0.50/call)",
                )
            )
        if citations:
            nav.append(
                (
                    f"get(kind='web', id={citations[0]!r})",
                    "fetch the first cited source directly",
                )
            )
        body = resp.body + render_next_section(nav)
        return Response(body=body, cost=resp.cost)

    # ── /recent listing — Perplexity-specific; surfaces imported vs
    #    fetched provenance and points at the import path so Pro users
    #    can hydrate the cache at $0 from the web UI.

    def _render_recent(self, *, limit: int = 20) -> Response:
        """Render the most recent refs of this kind, newest first.

        Each row shows slug, title (truncated), provenance
        (``imported`` vs ``fetched``), and the ``updated_at`` date.
        Empty-state message points the agent at ``get`` / ``put``.
        """
        refs = self.store.list_refs(
            kind=self.spec.kind,
            provider=self.provider,
            limit=limit,
        )
        heading = f"# recent {self.spec.kind} refs"
        if not refs:
            body = (
                f"{heading}\n\n"
                f"_(no {self.spec.kind} refs yet.)_\n\n"
                f"Next:\n"
                f"- `get(kind={self.spec.kind!r}, id='<query>')` — "
                f"run a fresh query (paid API)\n"
                f"- `put(kind={self.spec.kind!r}, id='<query>', "
                f"text='<report>', mode='import')` — "
                f"register a pre-generated answer at $0\n"
            )
            return Response(body=body)

        lines: list[str] = [heading, ""]
        for ref in refs:
            source = (ref.meta or {}).get("source") or "fetched"
            day = ref.updated_at.strftime("%Y-%m-%d") if ref.updated_at else "—"
            title = ref.title
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"- `{ref.slug}` — {title}  _({source}, {day})_")
        lines.append("")
        lines.append(
            f"_showing {len(refs)} of at most {limit}. "
            f"Next: get(kind={self.spec.kind!r}, id='<slug>') to read one._"
        )
        return Response(body="\n".join(lines))

    # ── cost trailer: distinguish imported cache entries from fetched ─

    def _cost_str(self, cache, *, hit):  # type: ignore[no-untyped-def]
        """Override: when the cache row was populated by
        ``put(mode='import')`` we want the trailer to say so plainly
        rather than just ``[cost: free]`` — agents can then tell at a
        glance that the body is user-supplied rather than API-cached."""
        if hit and (cache.meta or {}).get("source") == "imported":
            return "[cost: free — imported]"
        return super()._cost_str(cache, hit=hit)

    # ── put: import a pre-generated report as a $0 cache entry ───────

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
        """Import a Perplexity-generated report **or** apply link/tag ops.

        Two distinct modes:

        * ``mode='import'`` — paste a Perplexity-generated report as a
          $0 cache entry. Same shape as before: ``id=`` is the query,
          ``text=`` is the report body, the row gets pinned (no TTL)
          so future ``get(kind=<this>, id=<query>)`` returns it.
        * ``mode is None`` + link/tag kwargs — apply link/tag CRUD to
          an *existing* cache ref. Lets agents cross-link a research
          report to the paper that prompted it, or tag a websearch
          row ``CACHE:pinned`` so the TTL sweep doesn't reap it.

        The two modes are dispatched on whichever signal is present.
        Sending both (``mode='import'`` plus ``link=``) is a misuse
        and rejected up front — link/tag ops belong on a *separate*
        call after the import lands.

        Args:
            id: For ``import``, the original query. For link/tag, the
                resolved cache row's slug (returned in the import
                ack as ``ref %r``).
            text: Import only — the report body.
            mode: ``"import"`` for cache import, omitted for link/tag.
            tags / untags: Closed-prefix tags must use the kind's
                allowed axes (``CACHE`` for cache kinds; see
                ``_KIND_ALLOWED_AXES``). Open tags always allowed.
            link / unlink / rel: Cross-link target spec — same shape
                as on memory/todo/paper.
        """
        # Dispatch: link/tag mode is "any link/tag kwarg is set AND
        # mode is not 'import'". The two surfaces share kwargs (id=,
        # text=) but mean different things in each. The check below
        # is structured so a stray kwarg gives a helpful BadInput
        # rather than silently triggering the wrong code path.
        link_tag_kwargs = (link, unlink, tags, untags, rel)
        any_link_tag_kwarg = any(k is not None for k in link_tag_kwargs)
        if mode is None and any_link_tag_kwarg:
            return self._put_link_tag_ops(
                id=id,
                text=text,
                tags=tags,
                untags=untags,
                link=link,
                unlink=unlink,
                rel=rel,
            )
        if mode == "import" and any_link_tag_kwarg:
            raise BadInput(
                "import mode does not accept link/tag kwargs — split into two calls",
                next=(
                    f"put(kind={self.spec.kind!r}, id=<query>, text=..., "
                    "mode='import') first; THEN "
                    f"put(kind={self.spec.kind!r}, id=<slug>, link=...) "
                    "on the resulting slug"
                ),
            )
        if mode != "import":
            raise BadInput(
                f"{self.spec.kind} accepts mode='import' (cache import) "
                "or link/tag kwargs without mode",
                options=["import", "(omit) for link/tag ops"],
                next=(
                    f"put(kind={self.spec.kind!r}, id='<the query>', "
                    "text='<paste report>', mode='import') OR "
                    f"put(kind={self.spec.kind!r}, id='<slug>', "
                    "link='paper:other')"
                ),
            )
        if not isinstance(id, str) or not id.strip():
            raise BadInput(
                "import requires id= (the original Perplexity query)",
                next=(
                    f"put(kind={self.spec.kind!r}, id='<query>', "
                    "text='...', mode='import')"
                ),
            )
        if not isinstance(text, str) or not text.strip():
            raise BadInput(
                "import requires text= (the report body)",
                next=(
                    f"put(kind={self.spec.kind!r}, id='<query>', "
                    "text='<paste report>', mode='import')"
                ),
            )

        query = id.strip()
        body = text.strip()
        key = self._canonical_key(query)
        request_hash = self._hash(key)

        body_blocks = self._blocks_from_report(body)

        ref, _cache = self.store.put_cache_entry(
            corpus_id=self.store.ensure_corpus(self.corpus_slug),
            kind=self.spec.kind,
            slug=self._slug_for(key),
            title=_title_for_query(query),
            body_blocks=body_blocks,
            provider=self.provider,
            request_hash=request_hash,
            ttl_seconds=None,  # imports are pinned — never expire
            model=self.model,
            cost_usd=0.0,
            ref_meta={"source": "imported"},
            cache_meta={
                "model": self.model,
                "query": query,
                "source": "imported",
                "block_count": len(body_blocks),
            },
        )

        n = len(body_blocks)
        plural = "" if n == 1 else "s"
        msg = (
            f"imported {self.spec.kind} ref {ref.slug!r} "
            f"({n} block{plural}). future "
            f"get(kind={self.spec.kind!r}, id={query!r}) "
            f"will return the imported body for $0."
        )
        return Response(body=msg)

    # ── put: link/tag ops on an existing cache slug ──────────────────

    def _put_link_tag_ops(
        self,
        *,
        id: str | int | None,
        text: str | None,
        tags: list[str] | None,
        untags: list[str] | None,
        link: str | None,
        unlink: str | None,
        rel: str | None,
    ) -> Response:
        """Apply link/unlink/tags/untags to an existing cache row.

        Cache rows are identified by slug here, NOT by query — once
        a row exists it has a stable slug, and that's what the
        cross-link target syntax expects (``research:my-slug``).
        Resolving by query would require re-hashing the canonical
        key and looking up by ``request_hash``; we'd then have no
        path for slugs assigned via direct CLI ingest. Slug
        addressing keeps the link/tag surface uniform with paper.
        """
        if text is not None:
            raise BadInput(
                f"text= is not supported for link/tag ops on {self.spec.kind}",
                next=(
                    "use mode='import' to (re)import the body, or drop "
                    "text= and use link/unlink/tags/untags only"
                ),
            )
        if not isinstance(id, str) or not id.strip():
            raise BadInput(
                f"{self.spec.kind} link/tag ops require id= (the slug)",
                next=(f"put(kind={self.spec.kind!r}, id='<slug>', link='paper:other')"),
            )
        slug = id.strip()
        ref = self.store.get_ref(kind=self.spec.kind, id=slug)
        if ref is None:
            raise NotFound(
                f"{self.spec.kind} slug {slug!r} not found",
                next=(
                    f"get(kind={self.spec.kind!r}, id='<query>') first to "
                    "populate the cache, then link/tag the resulting slug"
                ),
            )

        validate_link_args(link=link, unlink=unlink, rel=rel, kind=self.spec.kind)
        if not any((link, unlink, tags, untags)):
            raise BadInput(
                f"{self.spec.kind} link/tag put requires at least one of "
                "link=, unlink=, tags=, untags=",
                next=(
                    f"put(kind={self.spec.kind!r}, id={slug!r}, "
                    "link='paper:other-slug')"
                ),
            )

        n_links_added, n_links_removed = apply_link_ops(
            self.store, ref.id, link=link, unlink=unlink, rel=rel
        )
        n_tags_added, n_tags_removed = apply_tag_ops(
            self.store,
            self.spec.kind,
            ref.id,
            tags=tags,
            untags=untags,
        )
        return Response(
            body=format_link_tag_ack(
                kind=self.spec.kind,
                ref_label=slug,
                n_links_added=n_links_added,
                n_links_removed=n_links_removed,
                n_tags_added=n_tags_added,
                n_tags_removed=n_tags_removed,
            )
        )

    def _blocks_from_report(self, body: str) -> list[BlockInsert]:
        """Parse a pasted Perplexity report into embedded blocks.

        The body is treated as Markdown — Perplexity's web exports
        already are. Each logical chunk (heading line, paragraph,
        list, table, code fence) becomes one block with a stable,
        content-derived slug.
        """
        md_blocks = parse_markdown(body)
        if not md_blocks:
            # Defensive fallback: no recognisable structure → store the
            # whole text as one paragraph block. Better than dropping.
            return [BlockInsert(pos=0, text=body)]

        embeddings: list[list[float]] | None = None
        if self.embedder is not None:
            embeddings = self.embedder.embed([b.text for b in md_blocks])

        out: list[BlockInsert] = []
        for i, mb in enumerate(md_blocks):
            meta: dict[str, Any] = {
                "kind": mb.kind,
                "line_start": mb.line_start,
                "line_end": mb.line_end,
            }
            if mb.heading_level is not None:
                meta["heading_level"] = mb.heading_level
            if mb.meta:
                meta.update(mb.meta)
            out.append(
                BlockInsert(
                    pos=mb.pos,
                    slug=mb.slug,
                    text=mb.text,
                    embedding=embeddings[i] if embeddings else None,
                    meta=meta,
                )
            )
        return out


# ---------------------------------------------------------------------------
# Concrete subclasses
# ---------------------------------------------------------------------------


class WebsearchHandler(_PerplexityBase):
    """``websearch`` — Sonar (fast factual answers, ~$0.001/call)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="websearch",
        title="Web search (Perplexity Sonar)",
        description=(
            "PAID (~$0.001/call): Perplexity Sonar — fast factual web "
            "search with inline citations (2–5s). Use for definitions, "
            "current events, quick lookups. Also accepts "
            "put(mode='import') to register a free, web-UI-generated "
            "answer as a $0 cache entry for the same query."
        ),
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
    )

    model: ClassVar[str] = "sonar"
    timeout: ClassVar[int] = 30
    cost_per_call_usd: ClassVar[float] = 0.001
    ttl_seconds: ClassVar[int | None] = 7 * 24 * 60 * 60  # 7 days
    attribution: ClassVar[str] = _PERPLEXITY_ATTRIBUTION_TEMPLATE.format(model="sonar")


class ThinkHandler(_PerplexityBase):
    """``think`` — Sonar Reasoning Pro (~$0.005/call, 5–30s)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="think",
        title="Think (Perplexity Sonar Reasoning Pro)",
        description=(
            "PAID (~$0.005/call): Perplexity Sonar Reasoning Pro — "
            "detailed analysis with explicit reasoning (5–30s). Use "
            "for comparisons, nuanced questions, multi-source synthesis. "
            "Also accepts put(mode='import') to register a free, "
            "web-UI-generated answer as a $0 cache entry."
        ),
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
    )

    model: ClassVar[str] = "sonar-reasoning-pro"
    timeout: ClassVar[int] = 120
    cost_per_call_usd: ClassVar[float] = 0.005
    ttl_seconds: ClassVar[int | None] = 30 * 24 * 60 * 60  # 30 days
    attribution: ClassVar[str] = _PERPLEXITY_ATTRIBUTION_TEMPLATE.format(
        model="sonar-reasoning-pro"
    )


class ResearchHandler(_PerplexityBase):
    """``research`` — Sonar Deep Research (~$0.50/call, 2–10 min)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="research",
        title="Deep research (Perplexity Sonar Deep Research)",
        description=(
            "PAID (~$0.50/call, 2–10 MIN): Perplexity Sonar Deep "
            "Research — multi-step investigation with extensive "
            "citation. Use only when the question justifies the wait "
            "and spend. Pro subscribers can run the same query free "
            "in the web UI, then put(mode='import') the result here "
            "to populate the cache at $0."
        ),
        supports_get=True,
        supports_put=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
    )

    model: ClassVar[str] = "sonar-deep-research"
    timeout: ClassVar[int] = 600
    cost_per_call_usd: ClassVar[float] = 0.50
    ttl_seconds: ClassVar[int | None] = None  # pinned — too expensive to expire
    attribution: ClassVar[str] = _PERPLEXITY_ATTRIBUTION_TEMPLATE.format(
        model="sonar-deep-research"
    )


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _format_perplexity_body(data: dict[str, Any]) -> tuple[str, list[str]]:
    """Render the Perplexity response and surface a clean citations list.

    Returns ``(body_text, citations_list)``. The body preserves inline
    ``[N]`` markers exactly as Perplexity returned them, then appends a
    ``Sources:`` block of the underlying URLs. Citations list is also
    returned separately so the cache row can store them as structured
    metadata.
    """
    choices = data.get("choices") or []
    if not choices:
        return "_Perplexity returned no answer (empty `choices`)._", []

    message = choices[0].get("message") or {}
    content = str(message.get("content") or "").strip()
    citations: list[str] = [str(c) for c in (data.get("citations") or [])]

    parts: list[str] = []
    parts.append(content if content else "_(Perplexity returned an empty answer.)_")

    if citations:
        parts.append("")
        parts.append("Sources:")
        for i, url in enumerate(citations, 1):
            parts.append(f"[{i}] {url}")

    return "\n".join(parts), citations


def _title_for_query(query: str) -> str:
    """Short ref title — first 80 chars of the query, single line."""
    one_line = " ".join(query.split())
    return one_line[:80] + ("…" if len(one_line) > 80 else "")
