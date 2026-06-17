"""``math`` kind — Wolfram Alpha query wrapper.

Subclasses :class:`CacheBackedHandler` so the cache-flow plumbing
(hash → lookup → freshness → fetch-on-miss → attribution footer →
cost trailer) is shared with `youtube` and `web`. This module owns
only the Wolfram-specific bits:

- Canonicalization (lowercase, whitespace collapse) so case/whitespace
  variants of the same query share a cache row.
- HTTP call (custom-rolled, see comment in ``_run_query`` for why we
  don't use ``wolframalpha.Client.query``).
- Pod → markdown formatter ported from the v1 handler.
- Wolfram's mandatory attribution text + per-query deep-link.

Gating: requires ``WOLFRAM_APP_ID`` env var (declared on KindSpec so
the dispatcher hides the kind when the key is missing). Requires the
``[external]`` optional dep group for the ``wolframalpha`` package.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
from typing import Any, ClassVar
from urllib.parse import quote_plus

from precis.errors import Upstream
from precis.handlers._cache_base import CacheBackedHandler, FetchResult
from precis.protocol import KindSpec
from precis.store.types import BlockInsert

log = logging.getLogger(__name__)


# Wolfram Alpha Terms of Use require attribution on every redistributed
# result. See https://www.wolframalpha.com/termsofuse — every result
# must carry a "Computed by Wolfram|Alpha" label and, for API users,
# a link back to the specific query page.
_WOLFRAM_QUERY_URL_TPL = "https://www.wolframalpha.com/input?i={q}"

# The static portion of the attribution. The base class adds a leading
# "— " and renders it as the response footer; the per-query deep-link
# is appended in ``_render`` since it depends on the original query.
_WOLFRAM_BASE_ATTRIBUTION = (
    "Computed by Wolfram|Alpha. Results © Wolfram Alpha LLC; "
    "attribution required under Wolfram's Terms of Use."
)


def _today_iso() -> str:
    """Today's date in ISO-8601 (UTC). Factored out for test seam."""
    return _dt.datetime.now(_dt.UTC).date().isoformat()


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class MathHandler(CacheBackedHandler):
    """``math`` — compute via Wolfram Alpha. Cache-pinned (results are
    deterministic for a fixed query)."""

    spec: ClassVar[KindSpec] = KindSpec(
        kind="math",
        title="Math (Wolfram Alpha)",
        description=(
            "Compute via Wolfram|Alpha - population/orbital/conversion "
            "facts, calculus, linear algebra, world data. Pass a "
            "natural-language or mathematical expression as `id` or `q`."
        ),
        supports_get=True,
        supports_search=True,
        supports_search_hits=True,
        is_numeric=False,
        id_required=True,
        requires_env=("WOLFRAM_APP_ID",),
    )

    provider: ClassVar[str] = "wolfram"
    # Wolfram results are deterministic for a fixed query — pin them.
    # The agent can force a refresh by deleting the ref (phase 5+).
    ttl_seconds: ClassVar[int | None] = None
    attribution: ClassVar[str] = _WOLFRAM_BASE_ATTRIBUTION
    corpus_slug: ClassVar[str] = "default"
    example_query: ClassVar[str] = "population of Ireland"

    # Per-call cost is tier-dependent for paid Wolfram plans; the free
    # tier is rate-limited but free. We surface a conservative
    # placeholder so cost trailers exist; tier-aware accounting can
    # land later.
    _COST_PER_CALL: ClassVar[float] = 0.002

    # ── canonicalization & cache key ──────────────────────────────────

    def _recover_key(self, ref, cache):  # type: ignore[no-untyped-def]
        """Return the input query stored in cache meta.

        Lets ``mode='refresh'`` work when the caller addressed by
        slug. Math doesn't carry a ``WATCH`` axis (Wolfram results
        don't drift), but manual refresh by slug stays useful for
        retried calls. (gripe:3681 phase 4.)
        """
        return (cache.meta or {}).get("input_query")

    def _canonical_key(self, query: str) -> str:
        """Lowercase + collapse internal whitespace.

        Wolfram itself is case-insensitive and whitespace-tolerant, so
        callers asking the "same" question shouldn't pay twice.
        """
        return " ".join(query.lower().split())

    # ── upstream call ─────────────────────────────────────────────────

    def _fetch(self, key: str) -> FetchResult:
        app_id = os.environ.get("WOLFRAM_APP_ID", "").strip()
        if not app_id:
            # Defense in depth — KindSpec.requires_env already gates this
            # at the registry level, but if a test or operator forces
            # the handler in without the env var, fail clearly.
            raise Upstream(
                "WOLFRAM_APP_ID environment variable is not set",
                next="export WOLFRAM_APP_ID=...; see https://products.wolframalpha.com/api",
            )

        try:
            doc = _run_query(app_id, key)
        except Upstream:
            raise
        except Exception as exc:
            raise Upstream(
                f"Wolfram Alpha API error: {exc}",
                next="retry, or use kind='calc' for offline arithmetic",
            ) from exc

        body = _format_doc(doc, key)
        return FetchResult(
            title=key,
            body_blocks=[BlockInsert(pos=0, text=body)],
            model="wolfram-alpha-v2",
            cost_usd=self._COST_PER_CALL,
            meta={"input_query": key},
        )

    # ── response polish — append per-query deep-link to attribution ───

    def _render(self, ref, cache, *, hit):  # type: ignore[no-untyped-def]
        """Override to inject Wolfram's per-query deep-link in the footer."""
        # Reuse the base body assembly but rewrite the footer line.
        resp = super()._render(ref, cache, hit=hit)
        # The base appended `— Computed by Wolfram|Alpha. ...`. Add the
        # deep-link + accessed-date line below it.
        url = _WOLFRAM_QUERY_URL_TPL.format(q=quote_plus(ref.title))
        deep_link = (
            f"  Verify: {url}\n"
            f'  Cite:   Wolfram|Alpha, WolframAlpha["{ref.title}"] '
            f"(accessed {_today_iso()})."
        )
        body = resp.body + "\n" + deep_link
        from precis.response import Response  # local import to avoid cycle

        return Response(body=body, cost=resp.cost)


# ---------------------------------------------------------------------------
# HTTP + parsing — synchronous, hand-rolled to bypass two upstream bugs
# ---------------------------------------------------------------------------


def _run_query(app_id: str, expression: str) -> Any:
    """Fetch + parse a Wolfram Alpha query directly via httpx.

    The :pypi:`wolframalpha` client (v5.x) has two bugs that prevent its
    ``Client.query`` from working inside our serial-stdio MCP runtime:

    1. ``Client.query`` wraps ``aquery`` with ``asyncio.run``, which
       raises ``RuntimeError`` when called from a running event loop.
       FastMCP runs handlers via ``anyio.from_thread`` with the loop
       still active.
    2. ``aquery`` asserts the response Content-Type is exactly
       ``'text/xml;charset=utf-8'`` (no space), but the real Wolfram
       API returns ``'text/xml; charset=utf-8'``.

    We sidestep both by making the GET ourselves and reusing only the
    library's XML postprocessor (``Document.make``) for output parity
    with v1's wolfravant-mcp formatter.
    """
    import httpx
    import multidict
    import xmltodict
    from wolframalpha import Document

    # Wolfram's per-query ``totaltimeout`` defaults to 20s. Broader
    # queries (e.g. "distance of the planets from the sun") routinely
    # exceed it and come back as ``<queryresult timedout="" numpods="0"/>``.
    # Push it to 55s; httpx read budget slightly above so a server-side
    # timeout still surfaces as a parsed ``<queryresult>`` rather than
    # a transport-level ``ReadTimeout``.
    params = multidict.MultiDict(
        appid=app_id,
        input=expression,
        totaltimeout="55",
    )
    url = "https://api.wolframalpha.com/v2/query"
    with httpx.Client(timeout=60.0) as http:
        resp = http.get(url, params=params)
    if resp.status_code != 200:
        raise Upstream(
            f"Wolfram Alpha HTTP {resp.status_code}: {resp.text[:200]}",
            next="retry; check WOLFRAM_APP_ID validity at https://account.wolfram.com",
        )
    doc = xmltodict.parse(resp.content, postprocessor=Document.make)
    return doc["queryresult"]


# ---------------------------------------------------------------------------
# Formatting — ported from v1 with light edits for the v2 response shape
# ---------------------------------------------------------------------------


def _format_doc(res: Any, query: str) -> str:
    """Flatten Wolfram's pods into a markdown body.

    Doesn't include attribution — the base class appends that as a
    footer. Returns the body text; sections are h2 ``## Title`` blocks.

    Handles three failure shapes from the Wolfram API:

    - timeout (``timedout=""``, no ``success`` attribute)
    - explicit failure (``success="false"``), with optional
      ``didyoumeans`` suggestions
    - success but no displayable plaintext in any pod
    """
    timed_out = getattr(res, "timedout", None)
    success = getattr(res, "success", None)

    if success is None:
        if timed_out is not None:
            return (
                f"Wolfram Alpha timed out internally for: {query!r}.\n"
                "Try a more specific query."
            )
        return f"Wolfram Alpha returned no success status for: {query!r}."

    if not success:
        tips: list[str] = []
        dym = getattr(res, "didyoumeans", None)
        if dym:
            if isinstance(dym, dict):
                dym = [dym]
            suggestions = [d.get("#text", str(d)) for d in dym]
            tips.append(f"Did you mean: {', '.join(suggestions)}")
        hint = (" " + " ".join(tips)) if tips else ""
        return f"Query failed for: {query!r}.{hint}"

    sections: list[str] = []
    for pod in res.pods:
        title = pod.get("@title") or pod.get("title") or "Result"
        # xmltodict collapses single-child elements to a dict (not a
        # one-element list), so ``pod['subpod']`` is a dict when the pod
        # has exactly one subpod and a list when it has many. Normalise.
        raw_subpods = pod.get("subpod", [])
        if isinstance(raw_subpods, dict):
            subpods: list[Any] = [raw_subpods]
        elif isinstance(raw_subpods, list):
            subpods = raw_subpods
        else:
            subpods = []
        texts: list[str] = []
        for sub in subpods:
            if isinstance(sub, str):
                continue
            plaintext = sub.get("plaintext")
            if plaintext:
                texts.append(plaintext)
        if texts:
            sections.append(f"## {title}\n" + "\n".join(texts))

    if not sections:
        return f"Query succeeded but returned no displayable text (for: {query!r})."

    return "\n\n".join(sections)
