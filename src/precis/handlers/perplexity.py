"""Perplexity Sonar — three cache-backed kinds (websearch / perplexity-reasoning / perplexity-research).

Each subclass picks a different Sonar model + timeout + TTL:

    kind                    model                      timeout   TTL       cost
    websearch               sonar                      30s       7 days    ~$0.001/call
    perplexity-reasoning    sonar-reasoning-pro        120s      30 days   ~$0.005/call
    perplexity-research     sonar-deep-research        600s      pinned    ~$0.50/call

Cache key is ``<model>:<query>`` so the same query under different
models never collides. The cache row provider is ``'perplexity'`` for
all three (pre-existing in the providers table).

Search block handles are rejected as queries before a paid fetch: their
truncated report prefix can collide with an existing report's slug.
The literal escape and explicit imports remain available.

New refs mint ``<slug>-<sha8>`` slugs: the query slugified at 60
chars plus an 8-hex sha256 tail of the canonical ``<model>:<query>``
key, so templated queries sharing a lead-in no longer collide
(gr517283 — the design-tree pilot hit refusals on shared 60-char
prefixes). Slugs minted before the suffix stay valid: reads resolve a
stored slug verbatim (or by ``request_hash``), and re-importing a
query cached under a legacy slug refreshes that row in place — its
old slug survives. A slug lookup that lands on a live row whose
stored ``query`` differs is still refused
(``_guard_slug_collision``) on the ``get`` miss path and
``put(mode='import')``; with the hash tail that refusal now means a
genuine same-slug collision, not a shared prefix.

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

import hashlib
import logging
import re
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, ClassVar

from precis._result_budget import (
    _cap_outline,
    _fit_head,
    body_bytes,
    result_context_cap,
    size_cell,
)
from precis.errors import BadInput, ResultBudgetExceeded, Upstream
from precis.handlers._cache_base import (
    CacheBackedHandler,
    FetchResult,
    dedupe_overlap_join,
)
from precis.protocol import KindSpec
from precis.response import Response
from precis.utils.http import (
    VendorError,
    VendorUnavailable,
    http_client,
    polite_user_agent,
    require_httpx,
    with_backoff,
)
from precis.utils.next_block import render_next_section
from precis.utils.slug import slug_from_text

log = logging.getLogger(__name__)

_SONAR_URL = "https://api.perplexity.ai/chat/completions"

# A query that is nothing but digits reads as a ref-id an agent meant to
# retrieve, not a web-search query — see ``_canonical_key``'s guard.
_BARE_REFID_RE = re.compile(r"^\d+$")
_BLOCK_HANDLE_RE = re.compile(r"~[^~]*-[0-9a-fA-F]+$")

# Per https://www.perplexity.ai/hub/legal/terms-of-service: every
# public / shared output must disclose AI use; Standard/Pro tiers are
# restricted to personal / non-commercial. Footer text intentionally
# verbose to defend against silent embedding by downstream agents.
_PERPLEXITY_ATTRIBUTION_TEMPLATE = (
    "Source: Perplexity AI ({model}). Perplexity is **not** a primary "
    "source - the numbered [N] citations in the answer link the actual "
    "sources; verify them before citing in publications. Per Perplexity's "
    "Terms of Service: disclose AI use in any public output; Standard/Pro "
    "tiers are restricted to personal / non-commercial use."
)


# ---------------------------------------------------------------------------
# Return-shaping args (gr540306)
# ---------------------------------------------------------------------------
#
# ``args={'no_sources': True}`` / ``args={'max_bytes': N}`` bound what a
# whole-body render RETURNS — the cached row keeps the Sources block and
# every byte either way. Sonar reports no pre-call result size, so these
# two are the only size bounds a caller has.
#
# Per-call state, threaded from the public ``get`` to ``_render``
# without an instance attribute — the same idiom as
# ``youtube._LANG_PREF``: handler objects are shared across FastMCP's
# worker-thread pool, so per-request state can't sit on ``self``.
# ``(drop_sources, max_bytes)``; the default shapes nothing.
_RETURN_OPTS: ContextVar[tuple[bool, int | None]] = ContextVar(
    "precis.perplexity._return_opts", default=(False, None)
)

#: The fetch formatter appends the numbered URL list under a literal
#: ``Sources:`` lead-in as the LAST body element
#: (:func:`_format_perplexity_body`). Rendered, the chunk re-join may
#: put blank lines between the numbered rows.
_SOURCES_TAIL_RE = re.compile(
    r"\n+Sources:\s*\n(?:\s*\[\d+\][^\n]*(?:\n|$))+",
)

#: What may follow the Sources block when it is the formatter's tail:
#: the footer rows, the ``render_next_section`` header, or a ``{`` JSON
#: tail. Recognises the header; emitting it stays in utils/next_block.py
#: (the ratchet in tests/test_next_literal_ratchet.py counts the literal).
_TAIL_AFTER_SOURCES_RE = re.compile(r"- |Next\s*:|\{")


def _drop_sources_block(body: str) -> str:
    """Remove the trailing ``Sources:\\n[n] url`` block from a rendered
    body — the ``args={'no_sources': True}`` shaping (gr540306).

    The strip fires only when the block is the last body element: the
    text right after it must be the render footer (``- <attribution>``,
    a ``- cite as`` line, a ``Next:`` table) or end-of-body. A
    ``Sources:`` lead-in mid-answer followed by more prose is left
    alone — that would be real content, not the formatter's tail.
    """
    m = _SOURCES_TAIL_RE.search(body)
    if m is None:
        return body
    tail = body[m.end() :].lstrip()
    if tail and not _TAIL_AFTER_SOURCES_RE.match(tail):
        return body
    return body[: m.start()] + "\n\n" + tail


def _first_unshown_ord(blocks: list[Any], shown_body_bytes: int) -> int | None:
    """``ord`` of the first chunk not wholly inside the shown head —
    where a continuation range read must start so nothing is skipped.

    ``blocks`` are the ref's stored chunks; cumulative sizes run
    through the same :func:`dedupe_overlap_join` the reader render
    uses, so the seam math matches what actually printed. ``None``
    when the shown bytes cover every chunk (nothing left to page to).
    """
    running = ""
    for b in blocks:
        running = dedupe_overlap_join([running, b.text or ""])
        if len(running.encode("utf-8")) > shown_body_bytes:
            return b.ord
    return None


def _coerce_max_bytes(value: int | None, *, kind: str) -> int | None:
    """Validate ``args={'max_bytes': N}`` — a positive int or ``None``.

    Rejected before the paid fetch so a typo'd cap never bills. A
    ``bool`` is an ``int`` subclass — ``max_bytes=True`` reads as a
    caller typo, not a 1-byte cap.
    """
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BadInput(
            f"{kind}: args['max_bytes'] must be a positive int (got {value!r})",
            next=(f"get(kind={kind!r}, id='<query>', args={{'max_bytes': 4096}})"),
        )
    return value


# ---------------------------------------------------------------------------
# Per-kind tier configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _SonarTier:
    """The knobs that differ across the three Sonar kinds.

    Config-dataclass parameterization — mirrors
    ``diagram/handler.py::DiagramHandler.LANG``: three
    ClassVar-only subclasses that overrode zero methods collapsed
    into one shared base (``_PerplexityBase``) whose behavior is
    parameterized by a single ``TIER: ClassVar[_SonarTier]`` per
    concrete subclass, instead of five separate ClassVars each.
    ``spec`` stays a direct per-subclass ``ClassVar[KindSpec]``
    (not folded in here) because boot-time registration
    (``dispatch._try``) reads ``cls.spec`` via ``getattr`` *before*
    constructing the handler, to gate kind-enablement without
    importing/instantiating it — that needs one distinct class per
    kind, exactly as ``FigureHandler``/``MermaidHandler`` each keep
    their own ``spec`` alongside a shared ``LANG``.
    """

    model: str
    """Sonar model identifier sent to the API."""

    timeout: int
    """HTTP timeout for this tier, in seconds."""

    cost_per_call_usd: float
    """Per-call cost estimate in USD (best estimate; recorded with the
    cache row, and used as the pre-fetch budget-gate estimate)."""

    ttl_seconds: int | None
    """Default cache freshness window; ``None`` pins forever."""

    attribution: str
    """Legal-attribution footer text rendered on every response."""


# ---------------------------------------------------------------------------
# Shared base
# ---------------------------------------------------------------------------


class _PerplexityBase(CacheBackedHandler):
    """Common Perplexity Sonar handler. Subclasses pin the model + tier."""

    #: Set by each concrete subclass — the model/timeout/cost/TTL/
    #: attribution bundle for that tier.
    TIER: ClassVar[_SonarTier]

    #: Unlike the other fetch caches, the agent-authored query here is sent to
    #: a third party: keep the secret gate on.
    stores_opaque_text: ClassVar[bool] = False

    #: A ``~``-carrying id is ALWAYS a cache-section address on the paid
    #: tiers — never a fresh billable query (gr542060 — a pasted search
    #: block handle used to mint a new ~$0.02–$0.50 row).
    tilde_id_is_address: ClassVar[bool] = True

    # ── inherited from CacheBackedHandler (subclasses override) ──────
    provider: ClassVar[str] = "perplexity"
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "your question"
    # spec set by subclasses (see _SonarTier docstring for why it
    # stays separate from TIER).
    #
    # __init__, _resolve_cache_slug, tag, link, search, search_hits,
    # and _blocks_from_report are all inherited from
    # :class:`CacheBackedHandler` — the web-bookmark patch promoted
    # them out of here so other cache-backed kinds (web) can share
    # the bookmarking / cross-linking surface.

    # ── proxy the three fields CacheBackedHandler's shared machinery
    #    reads generically (``self.ttl_seconds`` / ``self.attribution`` in
    #    get()/_render(), ``self.cost_per_call_usd`` in the budget-gate
    #    estimate) through to TIER, so the base class's cross-kind
    #    contract (also used by web/youtube/math) is untouched.

    @property
    def ttl_seconds(self) -> int | None:  # type: ignore[override]
        return self.TIER.ttl_seconds

    @property
    def attribution(self) -> str:  # type: ignore[override]
        return self.TIER.attribution

    @property
    def cost_per_call_usd(self) -> float:  # type: ignore[override]
        return self.TIER.cost_per_call_usd

    def _expected_cost_usd(self) -> float:
        """Pre-fetch estimate from *observed* billing, not the constant.

        gr521139: the hand-typed TIER figure ran >5x under a real
        sonar-deep-research call ($0.50 documented vs $2.71 billed —
        long prompts blow the flat estimate out). When the cache holds
        billed rows for this tier's model, the estimate is the p90 of
        the most recent observed ``cost_usd`` values — the pre-call
        figure the budget gate shows then tracks reality instead of a
        stale constant. The TIER constant is the no-data fallback only.
        """
        stats = self.store.observed_cost_stats(
            provider=self.provider, model=self.TIER.model
        )
        if stats is not None:
            _n, _median, p90 = stats
            return p90
        return self.TIER.cost_per_call_usd

    # ── canonicalize: trim + include model so kinds don't collide ────

    def _canonical_key(self, query: str, *, literal: bool = False) -> str:
        q = (query or "").strip()
        if not q:
            raise BadInput(
                f"{self.spec.kind} requires a non-empty query",
                next=f"get(kind={self.spec.kind!r}, id='your question')",
            )
        # A bare integer is almost never a real web-search query — it's a
        # ref-id an agent tried to *retrieve* (a gripe number, a prior
        # websearch ref) misrouted through a query-addressed kind, which
        # keys on TEXT. Left alone it fires a paid Sonar call and caches
        # junk ("171 is a composite number"). Reject with a retrieval
        # hint. Because base ``get`` catches this BadInput and falls back
        # to a slug lookup, a ref whose slug *is* that number is still
        # served for free; only an un-cached bare number reaches the
        # agent as the hint. ``literal=True`` (agent passed
        # ``args={'literal': True}``, or the import path) opts back in to
        # a verbatim search. (The 171157 incident.)
        if not literal and _BARE_REFID_RE.match(q):
            raise BadInput(
                f"{self.spec.kind}: {q!r} is a bare number — it reads as a "
                f"ref-id, not a web-search query. {self.spec.kind} is keyed "
                f"by query TEXT, so this would fire a paid Sonar call for the "
                f'literal number and cache junk (e.g. "{q} is a composite '
                f'number").',
                next=(
                    f"to RETRIEVE an existing ref by that number, it is a "
                    f"ref-id — get(id='<kind>:{q}') with the right kind (e.g. "
                    f"gripe:{q}, todo:{q}), or search(q='…') to find it",
                    "to REALLY web-search the literal number, pass "
                    "args={'literal': True}",
                ),
            )
        if not literal and _BLOCK_HANDLE_RE.search(q):
            report_slug = q.split("~", 1)[0]
            raise BadInput(
                f"{self.spec.kind}: {q!r} reads as a search block handle, "
                "not a query. Refusing a fresh paid Sonar call.",
                next=(
                    f"get(kind={self.spec.kind!r}, id={report_slug!r}, "
                    "args={'no_fetch': True}) to retrieve the cached report; "
                    "if absent, find the original query",
                    "to search this text literally, pass args={'literal': True}",
                ),
            )
        # Cache key includes the model so same query under different
        # tiers cache separately.
        return f"{self.TIER.model}:{q}"

    def _coerce_query(self, id: str | int | None, q: str | None) -> str:
        # The literal incident shape is ``get(kind='websearch', id=171157)``
        # with an *unquoted* int (the MCP ``get`` schema accepts
        # ``id: str | int``). The base coercer only reads ``str`` ids, so an
        # int id would be rejected with the generic "requires a query"
        # message *before* the bare-number guard in ``_canonical_key`` runs.
        # Stringify it here so it flows into the guard and gets the
        # actionable ref-id hint instead. Perplexity-local: other cache
        # kinds keep the base behaviour (a bare int is not a URL / video-id /
        # Wolfram query worth firing).
        if isinstance(id, int) and not isinstance(id, bool):
            if not (isinstance(q, str) and q.strip()):
                return str(id)
        return super()._coerce_query(id, q)

    def _slug_for(self, key: str) -> str:
        """``<slugified prefix>-<sha8>`` — readable slug plus a hash tail.

        Strip the ``<model>:`` prefix added by ``_canonical_key`` (the
        model is recorded in ``cache_state.model``), slugify the query
        at 60 chars for readability, then append 8 hex of the sha256 of
        the full canonical key so two queries sharing the prefix get
        distinct slugs (gr517283). Deterministic — the same key always
        re-derives the same slug, which is what the slug-lookup refresh
        path in base ``get`` depends on. Rows minted before 2026-10-25
        keep their un-suffixed slug; reads resolve the *stored* slug
        verbatim, so nothing about them changes.
        """
        _, _, q = key.partition(":")
        prefix = slug_from_text(q, max_len=60) or "perplexity-query"
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
        return f"{prefix}-{digest}"

    def _guard_slug_collision(self, key, ref, cache):
        """Refuse to overwrite a row whose stored query is a different query.

        New slugs carry an 8-hex sha256 tail of the canonical key
        (``_slug_for``), so a slug match on a different query is a
        genuine collision — a legacy row whose pre-suffix slug happens
        to equal the new form, or a hash-tail match — not the routine
        60-char prefix collision the bare slug produced (gr517283). The
        hash misses and base ``get`` would still refresh the existing
        row in place with the new query's body (a ~$0.50 report was
        lost this way, 2026-09-13). Compare the stored full query,
        whitespace-collapsed, and refuse when it differs. Rows written
        before ``query`` was tracked in meta cannot be compared and
        keep the in-place refresh.
        """
        stored = (cache.meta or {}).get("query")
        if not stored:
            return
        _, _, incoming = key.partition(":")
        if _title_for_query(str(stored)) == _title_for_query(incoming):
            return
        raise BadInput(
            f"{self.spec.kind}: {incoming!r} slugs to {ref.slug!r}, which "
            f"already holds a different report: {str(stored)!r} (ref "
            f"{ref.id}). Refusing to overwrite it.",
            next=(
                f"get(kind={self.spec.kind!r}, id={ref.slug!r}, "
                "args={'no_fetch': True}) to read the existing report",
                "rephrase the query — the slug carries a hash of the full "
                "query text, so a same-slug match on different text is a "
                "real collision",
            ),
        )

    def _recover_key(self, ref, cache):
        """Reconstruct ``<model>:<query>`` from cached meta.

        Cache meta stores the original ``query`` and ``model`` so a
        slug-only refresh (typically from the maintenance driver
        iterating ``WATCH:weekly`` research notes) can re-fetch
        without the caller having to remember the original prompt.
        Falls back to the handler's pinned ``self.TIER.model`` if meta
        was written before the model was tracked.
        (gripe:3681 phase 4.)
        """
        meta = cache.meta or {}
        query = meta.get("query")
        if not query:
            return None
        model = meta.get("model") or self.TIER.model
        return f"{model}:{query}"

    # ── get: return-shaping args ─────────────────────────────────────

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
        no_sources: bool = False,
        max_bytes: int | None = None,
        **kw: Any,
    ) -> Response:
        """Base ``get`` plus two return-shaping args (gr540306).

        ``args={'no_sources': True}`` drops the ``Sources:`` URL block
        from the RETURNED text — the cached row keeps it, so a later
        plain get still shows it. ``args={'max_bytes': N}`` bounds the
        returned body to N bytes (head + chunk outline + a ``next:``
        range read for the rest). Neither changes what is paid for or
        cached: the Sonar call, the stored body and the citation meta
        are identical with or without them. The pair is published to
        ``_render`` through ``_RETURN_OPTS`` (see the module section
        for why a ContextVar). ``max_bytes`` is validated before the
        call so a bad value never reaches the paid fetch.

        The base kwargs are re-declared and forwarded explicitly —
        the dispatch extras gate validates ``args=`` keys against the
        resolved signature's own params, so ``args={'no_fetch': True}``
        (an advertised call) must appear here or it would refuse.
        """
        cap = _coerce_max_bytes(max_bytes, kind=self.spec.kind)
        token = _RETURN_OPTS.set((bool(no_sources), cap))
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
                **kw,
            )
        finally:
            _RETURN_OPTS.reset(token)

    # ── auth + transport ──────────────────────────────────────────────

    @staticmethod
    def _api_key() -> str:
        # Resolves env → vault.reveal → ~/.secrets/pw file (secrets vault,
        # The secrets vault). Env-override-wins keeps behaviour identical until the
        # value is moved into the vault and pulled from the environment.
        from precis import secrets as _secrets

        key = (_secrets.get_secret("PERPLEXITY_API_KEY") or "").strip()
        if not key:
            raise Upstream(
                "PERPLEXITY_API_KEY not set",
                next="export PERPLEXITY_API_KEY=... (https://www.perplexity.ai/settings/api)",
            )
        return key

    def _fetch(self, key: str) -> FetchResult:
        httpx = require_httpx()

        # Strip the "<model>:" prefix to get the real prompt.
        _, _, query = key.partition(":")

        api_key = self._api_key()
        payload: dict[str, Any] = {
            "model": self.TIER.model,
            "messages": [{"role": "user", "content": query}],
            "return_citations": True,
            "web_search_options": {"search_context_size": "high"},
        }
        # Politeness core: a 429 backs off (Retry-After parks the row) and is
        # retried before it can reach the agent. A paid call is not re-sent
        # after a read timeout or 5xx (idempotent=False: the provider may
        # already have billed it), so only connect failures and 429 retry.
        try:
            with http_client(
                timeout=float(self.TIER.timeout), user_agent=polite_user_agent()
            ) as client:
                resp = with_backoff(
                    lambda: client.post(
                        _SONAR_URL,
                        headers={
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        },
                        json=payload,
                    ),
                    service="perplexity",
                    url=_SONAR_URL,
                    idempotent=False,
                )
        except VendorUnavailable as exc:
            raise Upstream(
                f"Perplexity rate limit or outage persisted after retries: {exc}",
                next="wait a minute and retry; consider a slower tier "
                "(perplexity-reasoning/perplexity-research)",
            ) from exc
        except VendorError as exc:
            if exc.status == 401 or exc.status == 403:
                raise Upstream(
                    f"Perplexity rejected the API key (HTTP {exc.status})",
                    next="check PERPLEXITY_API_KEY",
                ) from exc
            if isinstance(exc.__cause__, httpx.TimeoutException):
                raise Upstream(
                    f"Perplexity {self.TIER.model} timed out after "
                    f"{self.TIER.timeout}s",
                    next=(
                        "try a shorter query, or use kind='perplexity-research' "
                        "for slow-paced multi-step work"
                    ),
                ) from exc
            raise Upstream(
                f"Perplexity transport error: {exc}",
                next="check connectivity; retry later",
            ) from exc
        except httpx.TimeoutException as exc:
            raise Upstream(
                f"Perplexity {self.TIER.model} timed out after {self.TIER.timeout}s",
                next=(
                    "try a shorter query, or use kind='perplexity-research' for "
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
                next="wait and retry; consider a slower tier (perplexity-reasoning/perplexity-research)",
            )
        if resp.status_code >= 400:
            raise Upstream(
                f"Perplexity HTTP {resp.status_code}: "
                f"{resp.text[:200] if resp.text else 'no body'}",
                next="retry later",
            )

        try:
            data = resp.json()
        except Exception as exc:  # non-JSON 2xx body → clean error, not 500
            raise Upstream(
                f"Perplexity returned non-JSON (HTTP {resp.status_code}): {exc}",
                next="retry later",
            ) from exc
        body, citations = _format_perplexity_body(data)
        title = _title_for_query(query)
        # Prefer the provider's *actual* reported cost (newer Sonar returns a
        # ``usage.cost`` block) over the flat TIER estimate, so the budget
        # tote sums real spend. Fall back to the estimate when absent.
        usage = data.get("usage") or {}
        real_cost = _cost_from_usage(usage)
        cost_usd = real_cost if real_cost is not None else self.TIER.cost_per_call_usd

        # Block-parse + embed via the shared ingestion pipeline so
        # ``search(kind='perplexity-reasoning', q=...)`` can find content inside the
        # fetched report — same shape ``put(mode='import')`` uses.
        # Marginal embedding cost (5–20 calls) is negligible against
        # the API latency (5–30s for perplexity-reasoning, 2–10 min for
        # perplexity-research) and the per-call dollar cost.
        body_blocks = self._blocks_from_report(body)

        return FetchResult(
            title=title,
            body_blocks=body_blocks,
            cost_usd=cost_usd,
            meta={
                "model": self.TIER.model,
                "query": query,
                "citation_count": len(citations),
                "citations": citations,
                "usage": usage or None,
                "cost_estimated": real_cost is None,
            },
        )

    # ── render: append "Sources:" + Next: trailer ────────────────────

    def _render(self, ref, cache, *, hit):
        resp = super()._render(ref, cache, hit=hit)
        meta = cache.meta or {}
        citations = meta.get("citations") or []
        # The body already includes "Sources:" inline (from the fetch
        # formatter), but on cache hit we surface a Next: trailer that
        # points at any other tier the agent might want to escalate to.
        nav: list[tuple[str, str]] = []
        # Suggest the next tier up, except for perplexity-research (already
        # top tier). Guarded like the citations hint below: an unguarded
        # ``meta.get('query')`` renders a valid-but-wrong ``id=None`` literal
        # for any cache row written before ``query`` was tracked in meta —
        # it parses fine and only fails at dispatch.
        query = meta.get("query")
        if self.spec.kind == "websearch" and query:
            nav.append(
                (
                    f"get(kind='perplexity-reasoning', id={query!r})",
                    "deeper analytical answer (~$0.005/call)",
                )
            )
        elif self.spec.kind == "perplexity-reasoning" and query:
            nav.append(
                (
                    f"get(kind='perplexity-research', id={query!r})",
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
        # Return shaping (gr540306). ``no_sources`` drops the Sources
        # block from the RETURNED text only — the stored row keeps it,
        # so a later plain get still shows it. ``max_bytes`` bounds
        # this body to the caller's number without touching what was
        # paid for or stored. Sonar reports no pre-call result size,
        # so the pair is the only size bound a caller has — the
        # over-cap next= names them for the same reason.
        drop_sources, max_bytes = _RETURN_OPTS.get()
        if drop_sources:
            body = _drop_sources_block(body)
        # The base render's targeted read: a multi-chunk report over
        # the context budget refuses whole-body, and the recovery is
        # this kind's own chunk-range read (``id='<slug>~0..3'``),
        # not a hardcoded search on another tier (gr534654). A
        # single-chunk answer can't page — narrowing the question is
        # still the recovery there.
        result_budget_hint = (
            resp.result_budget_hint
            or f"search(kind={self.spec.kind!r}, "
            "q='<a narrower question>', page_size=1) requests a "
            "focused answer"
        )
        result_budget_hint += (
            "; args={'max_bytes': N} bounds the return, "
            "args={'no_sources': True} drops the Sources block"
        )
        if max_bytes is not None:
            body = self._cap_return_bytes(
                body,
                ref=ref,
                cap=max_bytes,
                fallback_hint=result_budget_hint,
            )
        return Response(
            body=body,
            cost=resp.cost,
            result_budget_hint=result_budget_hint,
        )

    def _continuation_hint(self, slug: str, cut_ord: int, last_ord: int) -> str:
        """The range read that returns what a ``max_bytes`` head cut off.

        Names the inclusive ``~<cut>..<last>`` range (``~<cut>`` when it
        lands on the last chunk) — the same ``slug~N``/``~lo..hi``
        grammar the over-cap recovery advertises, continued from where
        the head ran out instead of restarting at ``~0``.
        """
        sel = f"~{cut_ord}" if cut_ord == last_ord else f"~{cut_ord}..{last_ord}"
        return (
            f"get(kind={self.spec.kind!r}, id='{slug}{sel}') — the rest of the answer"
        )

    def _cap_return_bytes(
        self,
        body: str,
        *,
        ref,
        cap: int,
        fallback_hint: str,
    ) -> str:
        """Bound the rendered body to ``cap`` bytes — the
        ``args={'max_bytes': N}`` shaping (gr540306).

        Same shape the result-budget fitter produces — a whole-line
        head, an ``outline:`` of the stored chunks, and a ``next:``
        line naming the range read that returns the rest — but
        bounded by the caller's number instead of an env cap.
        ``cap`` is also clamped to :func:`result_context_cap` so an
        env cap set tighter than the caller's still binds.
        """
        bound = min(cap, result_context_cap())
        if body_bytes(body) <= bound:
            return body
        size = body_bytes(body)
        blocks = self.store.chunks.list_chunks_for_ref(ref.id)
        slug = ref.slug or str(ref.id)
        # Byte offset where the joined chunk text starts inside the
        # rendered body — the head bytes that map onto chunks. When the
        # joined text isn't in the render (a withheld body) the
        # continuation can't be computed — the fallback hint stands
        # alone rather than paging a body that isn't being shown.
        joined = dedupe_overlap_join([b.text or "" for b in blocks])
        start = body.find(joined) if joined else -1
        cuttable = start >= 0 and bool(blocks)
        prefix_bytes = len(body[:start].encode("utf-8")) if cuttable else 0

        # Chunk-outline map, bounded at half the cap like the fitter's —
        # a map bigger than the head it describes stops paying.
        outline_block = ""
        if len(blocks) > 1:
            from precis.format import render_agent_table

            rows = _cap_outline(
                render_agent_table(
                    [
                        {
                            "block": f"{slug}~{b.ord}",
                            "size": size_cell(body_bytes(b.text or "")),
                        }
                        for b in blocks
                    ],
                    schema=["block", "size"],
                )
            )
            kept: list[str] = []
            used = 0
            for ln in rows.split("\n"):
                cost = len(ln.encode("utf-8")) + 3
                if used + cost > bound // 2:
                    break
                kept.append(f"  {ln}")
                used += cost
            if kept:
                outline_block = "\n\noutline:\n" + "\n".join(kept)

        marker = (
            f"\n\n… truncated: showing {{shown}} of {size} bytes; cap max_bytes={cap}"
        )
        # Iterate the trailer: the ``next:`` call depends on the cut
        # chunk, which depends on the head, which depends on the
        # trailer width. Widths settle in a couple of passes; the
        # stable-tail check breaks the loop and a fourth pass bounds
        # the worst case (same fixed-point scheme the fitter uses).
        cut: int | None = None
        shown = 0
        head, tail = "", ""
        for _ in range(4):
            nxt = (
                self._continuation_hint(slug, cut, blocks[-1].ord)
                if cut is not None and cuttable
                else fallback_hint
            )
            new_tail = marker.format(shown=shown) + outline_block + f"\nnext: {nxt}"
            if new_tail == tail:
                break
            tail = new_tail
            head, _dropped = _fit_head(body, max(0, bound - body_bytes(tail)))
            shown = body_bytes(head)
            cut = (
                _first_unshown_ord(blocks, max(0, shown - prefix_bytes))
                if cuttable
                else None
            )
        if not shown or body_bytes(head + tail) > bound:
            raise ResultBudgetExceeded(
                f"get: result is {size} bytes and even a head will not "
                f"fit under max_bytes={cap}",
                next=fallback_hint,
            )
        return head + tail

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
            body = f"{heading}\n\n_(no {self.spec.kind} refs yet.)_\n"
            body += render_next_section(
                [
                    (
                        f"get(kind={self.spec.kind!r}, id='<query>')",
                        f"run a fresh query (paid API, ~${self.TIER.cost_per_call_usd:.4f}/call)",
                    ),
                    (
                        f"put(kind={self.spec.kind!r}, id='<query>', "
                        "text='<report>', mode='import')",
                        "register a pre-generated answer at $0",
                    ),
                ]
            )
            return Response(body=body)

        lines: list[str] = [heading, ""]
        for ref in refs:
            source = (ref.meta or {}).get("source") or "fetched"
            day = ref.updated_at.strftime("%Y-%m-%d") if ref.updated_at else "-"
            title = ref.title
            if len(title) > 80:
                title = title[:77] + "..."
            lines.append(f"- `{ref.slug}` - {title}  _({source}, {day})_")
        lines.append("")
        # Only claim "of at most N" when the page was actually capped
        # (count == limit, so more may exist). At less than the cap the
        # pool is exhausted, so "of at most N" implies a truncation that
        # didn't happen — show the plain count instead.
        if len(refs) >= limit:
            count_phrase = f"showing {len(refs)} of at most {limit}"
        else:
            count_phrase = f"showing {len(refs)} of {len(refs)}"
        lines.append(f"_{count_phrase}._")
        body = "\n".join(lines)
        # ``no_fetch=True`` matters here: a listed slug pasted into a bare
        # ``get(id=<slug>)`` MISSES the request-hash cache (the slug isn't
        # the canonical ``<model>:<query>`` key) and falls through to a
        # fresh, paid Sonar call — up to ~$0.50 and minutes on this tier.
        body += render_next_section(
            [
                (
                    f"get(kind={self.spec.kind!r}, id='<slug>', no_fetch=True)",
                    "read one listed row (never triggers a fresh, billable fetch)",
                ),
            ]
        )
        return Response(body=body)

    # ── cost trailer: distinguish imported cache entries from fetched ─

    def _cost_str(self, cache, *, hit):
        """Override: when the cache row was populated by
        ``put(mode='import')`` we want the trailer to say so plainly
        rather than just ``[cost: free]`` — agents can then tell at a
        glance that the body is user-supplied rather than API-cached."""
        if hit and (cache.meta or {}).get("source") == "imported":
            return "[cost: free - imported]"
        return super()._cost_str(cache, hit=hit)

    # ── put: import a pre-generated report as a $0 cache entry ───────

    def put(
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
        """Import a Perplexity-generated report as a $0 cache entry.

        Only ``mode='import'`` is accepted. Cross-linking and tagging
        a cache row move to the dedicated ``tag`` / ``link`` verbs
        per the seven-verb surface (D6).

        Args:
            id:   The original Perplexity query (used as the cache key).
            text: The report body (Markdown, parsed into blocks).
            mode: Must be ``'import'``. Other modes / no mode rejects.
        """
        # Reject every non-import shape with a sharp pointer at the
        # right verb. tag=, untag=, link=, unlink=, rel= used to be
        # accepted on this kind for cross-linking; they now live on
        # the dedicated tag/link verbs.
        if tags is not None or untags is not None:
            raise BadInput(
                f"tags=/untags= are not accepted on put for kind={self.spec.kind!r}",
                next=(
                    f"import first with put(mode='import'), then "
                    f"tag(kind={self.spec.kind!r}, id='<slug>', add=[...])"
                ),
            )
        if link is not None or unlink is not None or rel is not None:
            raise BadInput(
                f"link=/unlink=/rel= are not accepted on put for kind={self.spec.kind!r}",
                next=(
                    f"import first with put(mode='import'), then "
                    f"link(kind={self.spec.kind!r}, id='<slug>', target=...)"
                ),
            )
        if mode != "import":
            raise BadInput(
                f"{self.spec.kind} put accepts only mode='import'",
                options=["import"],
                next=(
                    f"put(kind={self.spec.kind!r}, id='<the query>', "
                    "text='<paste report>', mode='import')"
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
        # Import is an explicit, user-supplied cache write — bypass the
        # bare-ref-id retrieval guard so a legitimately numeric query can
        # still be hydrated at $0.
        key = self._canonical_key(query, literal=True)
        request_hash = self._hash(key)
        slug = self._slug_for(key)
        body_blocks = self._blocks_from_report(body)

        # The same canonical query may already be cached under a legacy
        # un-suffixed slug (pre-hash-tail rows, gr517283): the slug lookup
        # below can't see it, and the UNIQUE(provider, request_hash)
        # constraint would turn a second insert into an IntegrityError.
        # Refresh that row in place instead — its old slug keeps
        # resolving and any tags/links survive (delete+insert would
        # destroy them).
        same_query = self.store.get_cache_entry(
            provider=self.provider, request_hash=request_hash
        )
        if same_query is not None:
            ref, _cache = same_query
            self.store.stamp_ref_meta(ref.id, {"source": "imported"})
            ref, _cache = self.store.update_cache_entry(
                ref_id=ref.id,
                title=_title_for_query(query),
                body_blocks=body_blocks,
                ttl_seconds=None,  # imports are pinned — never expire
                model=self.TIER.model,
                cost_usd=0.0,
                cache_meta={
                    "model": self.TIER.model,
                    "query": query,
                    "source": "imported",
                    "block_count": len(body_blocks),
                },
            )
            n = len(body_blocks)
            plural = "" if n == 1 else "s"
            return Response(
                body=(
                    f"re-imported {self.spec.kind} ref {ref.slug!r} "
                    f"({n} block{plural}) — refreshed the existing row in "
                    f"place; its slug still resolves. future "
                    f"{_readback_hint(self.spec.kind, query, ref.slug or slug)} "
                    f"will return the imported body for $0."
                )
            )

        # ``put_cache_entry`` replaces the live row under this slug; refuse
        # when that row holds a different query's report (same guard as
        # the ``get`` miss path).
        existing = self.store.get_cache_entry_by_slug(kind=self.spec.kind, slug=slug)
        if existing is not None:
            self._guard_slug_collision(key, *existing)

        ref, _cache = self.store.put_cache_entry(
            kind=self.spec.kind,
            slug=slug,
            title=_title_for_query(query),
            body_blocks=body_blocks,
            provider=self.provider,
            request_hash=request_hash,
            ttl_seconds=None,  # imports are pinned — never expire
            model=self.TIER.model,
            cost_usd=0.0,
            ref_meta={"source": "imported"},
            cache_meta={
                "model": self.TIER.model,
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
            f"{_readback_hint(self.spec.kind, query, ref.slug or slug)} "
            f"will return the imported body for $0."
        )
        return Response(body=msg)

    # ── seven-verb surface ─────────────────────────────────────────
    #
    # _resolve_cache_slug, tag, link, search, search_hits, and
    # _blocks_from_report are inherited from
    # :class:`CacheBackedHandler`. See the base for the docstrings
    # and the web-bookmark patch commit for the reason the
    # cut-over happened.


# ---------------------------------------------------------------------------
# Concrete subclasses
# ---------------------------------------------------------------------------


class WebsearchHandler(_PerplexityBase):
    """``websearch`` — Sonar (fast factual answers, ~$0.001/call)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="websearch",
        title="Web search (Perplexity Sonar)",
        description=(
            "PAID (~$0.001/call): Perplexity Sonar - fast factual web "
            "search with inline citations (2–5s). Use for definitions, "
            "current events, quick lookups. Also accepts "
            "put(mode='import') to register a free, web-UI-generated "
            "answer as a $0 cache entry for the same query."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
        views=("body",),
        # gr533135 / gr534654: a long report over the context result
        # budget refuses whole-body reads — the base's ``slug~N..M``
        # chunk-range read is the targeted recovery the refusal names.
        supports_chunk_selectors=True,
    )

    TIER: ClassVar[_SonarTier] = _SonarTier(
        model="sonar",
        timeout=30,
        cost_per_call_usd=0.001,
        ttl_seconds=7 * 24 * 60 * 60,  # 7 days
        attribution=_PERPLEXITY_ATTRIBUTION_TEMPLATE.format(model="sonar"),
    )


class ThinkHandler(_PerplexityBase):
    """``perplexity-reasoning`` — Sonar Reasoning Pro (~$0.005/call, 5–30s)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="perplexity-reasoning",
        title="Perplexity reasoning (Sonar Reasoning Pro)",
        description=(
            "PAID (~$0.005/call): Perplexity Sonar Reasoning Pro - "
            "detailed analysis with explicit reasoning (5–30s). Use "
            "for comparisons, nuanced questions, multi-source synthesis. "
            "Also accepts put(mode='import') to register a free, "
            "web-UI-generated answer as a $0 cache entry."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
        views=("body",),
        supports_chunk_selectors=True,
    )

    # Cache forever by default. The pollution failure mode (a wrong
    # answer staying around) is bounded: citations come from real papers
    # (the chase + finding pipeline), not from perplexity, so a wrong
    # cached answer can be linked-out as wrong via a memory or deleted.
    # Agents can still override per-call via ``ttl_days=`` or force a
    # re-fetch via ``refresh=True``.
    TIER: ClassVar[_SonarTier] = _SonarTier(
        model="sonar-reasoning-pro",
        timeout=120,
        cost_per_call_usd=0.005,
        ttl_seconds=None,
        attribution=_PERPLEXITY_ATTRIBUTION_TEMPLATE.format(
            model="sonar-reasoning-pro"
        ),
    )


class ResearchHandler(_PerplexityBase):
    """``perplexity-research`` — Sonar Deep Research (~$0.50/call, 2–10 min)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="perplexity-research",
        title="Perplexity deep research (Sonar Deep Research)",
        description=(
            "PAID (~$0.50/call, 2–10 MIN): Perplexity Sonar Deep "
            "Research - multi-step investigation with extensive "
            "citation. Use only when the question justifies the wait "
            "and spend. Pro subscribers can run the same query free "
            "in the web UI, then put(mode='import') the result here "
            "to populate the cache at $0."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        supports_put=True,
        supports_tag=True,
        supports_link=True,
        is_numeric=False,
        id_required=False,
        modes=("import",),
        views=("body",),
        supports_chunk_selectors=True,
    )

    TIER: ClassVar[_SonarTier] = _SonarTier(
        model="sonar-deep-research",
        timeout=600,
        cost_per_call_usd=0.50,
        ttl_seconds=None,  # pinned — too expensive to expire
        attribution=_PERPLEXITY_ATTRIBUTION_TEMPLATE.format(
            model="sonar-deep-research"
        ),
    )


# ---------------------------------------------------------------------------
# Response formatting
# ---------------------------------------------------------------------------


def _cost_from_usage(usage: dict[str, Any]) -> float | None:
    """Pull the provider's actual USD cost from a Sonar ``usage`` block.

    Newer Sonar responses carry ``usage.cost.total_cost`` (a float, USD). A
    top-level ``usage.total_cost`` is also accepted. Returns ``None`` when no
    cost figure is present, so the caller falls back to the TIER estimate.
    """
    if not isinstance(usage, dict):
        return None
    cost_obj = usage.get("cost")
    candidate: Any = None
    if isinstance(cost_obj, dict):
        candidate = cost_obj.get("total_cost")
    elif isinstance(cost_obj, (int, float)):
        candidate = cost_obj
    if candidate is None:
        candidate = usage.get("total_cost")
    if isinstance(candidate, (int, float)):
        return float(candidate)
    return None


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
    # Sonar-reasoning-pro (``perplexity-reasoning``) interleaves a
    # ``<think>…</think>`` scratch block inside the answer that
    # leaks the model's internal reasoning trace.  Callers should
    # see the conclusion, not the scratch — strip the block at
    # render time.  The conclusion follows the closing tag.
    # ``websearch`` / ``perplexity-research`` responses never carry
    # the tag, so the strip is a no-op there.  (MCP critic MINOR-C
    # — perplexity-reasoning kind leaks <think>…</think> reasoning trace.)
    content = _strip_reasoning_trace(content)
    sources = _citation_sources(data)
    citations = [url for _n, url in sources]

    parts: list[str] = []
    parts.append(content if content else "_(Perplexity returned an empty answer.)_")

    if sources:
        parts.append("")
        parts.append("Sources:")
        for n, url in sources:
            parts.append(f"[{n}] {url}")

    return "\n".join(parts), citations


def _citation_sources(data: dict[str, Any]) -> list[tuple[int, str]]:
    """The numbered source list the inline ``[N]`` markers index.

    ``search_results`` is the canonical, id-spaced source list — when an
    entry carries an ``id`` that IS the marker number, and array order
    is the id order. The legacy ``citations`` array can arrive
    deduplicated or reordered relative to it, which skews ``[N]`` off
    its source (gr542060 — a ``[36]`` marker in a deep-research answer
    landed on an unrelated URL). Prefer ``search_results``; fall back
    to ``citations`` — 1-indexed by position, the documented legacy
    mapping — for responses that predate the field.
    """
    numbered: list[tuple[int, str]] = []
    for i, r in enumerate(data.get("search_results") or [], 1):
        url: Any = r.get("url") if isinstance(r, dict) else r
        if not url:
            continue
        rid = r.get("id") if isinstance(r, dict) else None
        numbered.append((rid if isinstance(rid, int) else i, str(url)))
    if numbered:
        return numbered
    return [(i, str(c)) for i, c in enumerate(data.get("citations") or [], 1)]


def _title_for_query(query: str) -> str:
    """Ref title — the full query on a single line.

    Storage keeps the *whole* query so the web detail page can show it
    verbatim (a truncated title is lossy: the original question is the
    point of a search ref). Whitespace is collapsed so a multi-line
    paste renders as one logical line, but nothing is dropped.
    Truncation is a *display* concern — Drive/list rows lean on
    ``item_view.display_title`` and the agent-facing ``/recent`` listing
    caps at 80 chars — never a storage one.
    """
    return " ".join(query.split())


def _readback_hint(kind: str, query: str, slug: str) -> str:
    """The ``get`` call an import ack advertises for reading the row back.

    ``id=<query>`` is the natural form — the request-hash lookup hits
    the imported row for $0. But a query the input guards refuse (a bare
    number reads as a misrouted ref-id, ``x~…-deadbeef`` reads as a
    search block handle) never reaches that lookup, so for those the ack
    names the slug + ``no_fetch=True`` form, which always resolves.
    """
    if _BARE_REFID_RE.match(query) or _BLOCK_HANDLE_RE.search(query):
        return f"get(kind={kind!r}, id={slug!r}, no_fetch=True)"
    return f"get(kind={kind!r}, id={query!r})"


_REASONING_TAG_RE = re.compile(
    r"<think\b[^>]*>.*?</think>\s*",
    flags=re.DOTALL | re.IGNORECASE,
)
_ORPHAN_CLOSE_THINK_RE = re.compile(r"\s*</think>\s*", flags=re.IGNORECASE)
_ORPHAN_OPEN_THINK_RE = re.compile(
    r"<think\b[^>]*>.*\Z",
    flags=re.DOTALL | re.IGNORECASE,
)


def _strip_reasoning_trace(content: str) -> str:
    """Remove any ``<think>…</think>`` blocks from a Perplexity body.

    Sonar-reasoning-pro wraps its internal reasoning in a literal
    ``<think>…</think>`` block before the final answer; small-model
    callers misread the scratch trace as the conclusion and quote
    it back.  The block is deterministic and single-top-level, so
    a regex strip is safe.  No-op when the tag is absent.

    Three forms are scrubbed:

    1. **Paired** ``<think>…</think>`` — the canonical wrapper.
    2. **Orphan closing tag** ``</think>`` with no matching opener
       (observed in the corpus 2026-05-02 — likely from a streaming
       upstream that truncated the opener). Drop everything before
       it on the assumption that the conclusion lives after it,
       falling back to deletion of just the tag if the prefix is
       implausibly long (≥ 90 % of the body).
    3. **Orphan opening tag** ``<think>`` with no matching closer
       (truncation in the other direction). Drop from the tag to
       end-of-string — better to surface a possibly-empty answer
       than to leak the trace.

    Whitespace adjacent to the strip site is swallowed so the
    answer heading lands cleanly. (MCP critic MINOR-C 2026-05-02
    — orphan closing tags leaked into the corpus.)
    """
    lower = content.lower()
    if "<think" not in lower and "</think>" not in lower:
        return content
    cleaned = _REASONING_TAG_RE.sub("", content, count=1)
    # Orphan closing tag: keep the suffix, drop everything up to
    # and including the tag — that's the conclusion that came
    # AFTER the leaked trace. Guard against absurd truncation
    # ratios (where the prefix is ≥ 90 % of the body) by falling
    # back to a simple in-place delete.
    if "</think>" in cleaned.lower():
        m = _ORPHAN_CLOSE_THINK_RE.search(cleaned)
        if m is not None:
            prefix_len = m.start()
            suffix = cleaned[m.end() :]
            if prefix_len <= 0.9 * len(cleaned) and suffix.strip():
                cleaned = suffix
            else:
                cleaned = _ORPHAN_CLOSE_THINK_RE.sub(" ", cleaned)
    # Orphan opening tag: drop from the tag to end-of-string. The
    # conclusion never followed in the corpus we've seen, but
    # callers must not see the trace.
    cleaned = _ORPHAN_OPEN_THINK_RE.sub("", cleaned)
    return cleaned.lstrip()
