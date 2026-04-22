"""MathHandler — Wolfram Alpha query wrapper (Phase 4).

Ported from ``wolfravant-mcp``.  Wraps the official ``wolframalpha``
PyPI client (v2 Full Results API).

Gating:

- ``wolframalpha`` must be importable (part of the ``[external]`` extra).
  When absent, registry registration is skipped at startup via the usual
  ``ImportError`` catch in ``_register_builtins``.
- ``WOLFRAM_APP_ID`` env var must be set.  The handler declares this on
  its ``KindSpec.requires`` so :func:`visible_kinds` hides the ``math``
  kind from the agent enum when the key is absent (§6.2, §13).

Cost hint is static — Wolfram Alpha charges by tier rather than per
call, so we surface a rough per-call estimate.  If a tier-aware cost
tracker lands later (§19), it can override this via ``cost_of``.

Dispatch:

- ``read(path=<query>)`` — path is the natural-language or mathematical
  expression; Wolfram returns structured pods which we flatten into
  markdown sections.  Additional handler params (selector, view, depth,
  …) are ignored for this stateless kind.
"""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import quote_plus

from precis.protocol import ErrorCode, Handler, PrecisError

log = logging.getLogger(__name__)


# Wolfram Alpha Terms of Use require attribution.  See
# https://www.wolframalpha.com/termsofuse — every redistributed result
# must carry a "Computed by Wolfram|Alpha" label and, for API users,
# a link back to the specific query page.
_WOLFRAM_QUERY_URL = "https://www.wolframalpha.com/input?i={q}"
_WOLFRAM_ATTRIBUTION = (
    "---\n"
    "_Computed by [Wolfram|Alpha]({url}). Results © Wolfram Alpha LLC; "
    "attribution is required under Wolfram's Terms of Use. For academic "
    'citation: `Wolfram|Alpha, WolframAlpha["{query}"] (accessed [date]).`_'
)


class MathHandler(Handler):
    """Handler for the ``math:`` scheme — compute via Wolfram Alpha.

    Agent usage::

        get(id='math:integrate sin(x)cos(x)')
        get(type='math', id='derivative of x^3 + 2x')
        search(query='population of Ireland', type='math')
    """

    scheme = "math"
    writable = False
    views = set()  # no sub-views; a math query is atomic

    def __init__(self) -> None:
        self._client: Any = None  # wolframalpha.Client | None, lazy

    # ---- Client initialisation (lazy) -------------------------------

    def _get_client(self) -> Any:
        """Return the Wolfram Alpha client, building it on first use.

        Raises :class:`PrecisError` with ``KIND_UNAVAILABLE`` when either
        the package or the env var is missing, so the agent sees a
        unified error rather than a stacktrace.
        """
        if self._client is not None:
            return self._client
        try:
            import wolframalpha
        except ImportError as exc:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "wolframalpha package not installed. "
                "Install with: pip install precis-mcp[external]",
            ) from exc
        app_id = os.environ.get("WOLFRAM_APP_ID", "").strip()
        if not app_id:
            raise PrecisError(
                ErrorCode.KIND_UNAVAILABLE,
                "WOLFRAM_APP_ID environment variable is not set. "
                "Get a free App ID from https://products.wolframalpha.com/api.",
            )
        self._client = wolframalpha.Client(app_id)
        return self._client

    # ---- Core read --------------------------------------------------

    def read(
        self,
        path: str,
        selector: str | None,
        view: str | None,
        subview: str | None,
        query: str,
        summarize: bool,
        depth: int,
        page: int,
    ) -> str:
        """Query Wolfram Alpha and return formatted result text.

        ``path`` is the primary query.  ``query`` (the ``search``-style
        filter parameter) is treated as a fallback for callers that
        happen to route through the search tool with an empty path.
        """
        expression = (path or query or "").strip()
        if not expression:
            raise PrecisError(
                ErrorCode.PARAM_INVALID,
                "empty math query",
            )
        client = self._get_client()
        try:
            res = client.query(expression)
        except Exception as exc:
            raise PrecisError(
                ErrorCode.UPSTREAM_ERROR,
                f"Wolfram Alpha API error: {exc}",
            ) from exc
        return _format_result(res, expression)


# ---------------------------------------------------------------------------
# Formatting — ported verbatim from wolfravant-mcp for output parity
# ---------------------------------------------------------------------------


def _attribution(query: str) -> str:
    """Build the mandatory Wolfram Alpha attribution footer.

    The URL deep-links to the exact query so the user (or reviewer) can
    verify the result, which is the form Wolfram recommends for API
    redistribution per
    https://products.wolframalpha.com/api/commercial-termsofuse.
    """
    url = _WOLFRAM_QUERY_URL.format(q=quote_plus(query))
    return _WOLFRAM_ATTRIBUTION.format(url=url, query=query)


def _format_result(res: Any, query: str) -> str:
    """Flatten Wolfram pods into readable markdown with attribution.

    Preserves the section shape from ``wolfravant-mcp/server.py`` so
    agent prompts that were tuned for that server transfer cleanly.
    Every return path ends with the mandatory attribution footer (see
    :func:`_attribution`).
    """
    if not res.success:
        tips: list[str] = []
        dym = getattr(res, "didyoumeans", None)
        if dym:
            if isinstance(dym, dict):
                dym = [dym]
            suggestions = [d.get("#text", str(d)) for d in dym]
            tips.append(f"Did you mean: {', '.join(suggestions)}")
        hint = (" " + " ".join(tips)) if tips else ""
        body = f"Query failed for: {query!r}.{hint}"
        return f"{body}\n\n{_attribution(query)}"

    sections: list[str] = []
    for pod in res.pods:
        title = pod.get("@title", "Result")
        texts: list[str] = []
        for sub in pod.get("subpod", []):
            if isinstance(sub, str):
                continue
            plaintext = sub.get("plaintext")
            if plaintext:
                texts.append(plaintext)
        if texts:
            sections.append(f"## {title}\n" + "\n".join(texts))

    if not sections:
        body = f"Query succeeded but returned no displayable text (for: {query!r})."
        return f"{body}\n\n{_attribution(query)}"

    return "\n\n".join(sections) + "\n\n" + _attribution(query)
