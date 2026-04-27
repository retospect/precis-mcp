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

from precis.errors import BadInput, Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.response import Response
from precis.store.types import BlockInsert
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
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
    # ttl_seconds + attribution + spec set by subclasses.

    def __init__(self, *, store: Store) -> None:
        super().__init__(store=store)

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
        try:
            import httpx
        except ImportError as exc:  # pragma: no cover — guarded at registry
            raise Upstream(
                "httpx is not installed",
                next="pip install 'precis-mcp[external]'",
            ) from exc

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
            "current events, quick lookups."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        requires_env=("PERPLEXITY_API_KEY",),
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
            "for comparisons, nuanced questions, multi-source synthesis."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        requires_env=("PERPLEXITY_API_KEY",),
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
            "and spend."
        ),
        supports_get=True,
        is_numeric=False,
        id_required=True,
        requires_env=("PERPLEXITY_API_KEY",),
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
