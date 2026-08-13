"""Shared outbound-HTTP seam.

Every kind that reaches the network (``web``, ``news``, ``wikipedia``,
``semanticscholar``, ``math``/Wolfram, ``perplexity``, ``youtube``, the
ORCID ingest client, …) previously open-coded the same three things:

1. ``httpx = require_optional("httpx", extra="external")`` — the optional
   dependency gate, with the extra name spelled out by hand each time.
2. A ``User-Agent`` header (variously ``"precis-mcp/1.0"`` or absent).
3. ``follow_redirects=`` — a *security-relevant* default. The SSRF guard
   in :mod:`precis.utils.safe_fetch` only works when the client does
   **not** auto-follow redirects (``safe_get`` walks the chain itself,
   revalidating each hop). A client that silently defaulted to
   ``follow_redirects=True`` would let an agent-supplied URL redirect
   into a private/loopback/metadata address.

:func:`http_client` centralises all three so the extra name, the UA, and
the safe redirect default live in exactly one place. Bespoke per-kind
error messages and ``next=`` hints stay at the call site — they are
deliberately tuned per kind (and asserted in tests), not duplication.

This module does **not** import ``httpx`` at module load — the dep is
optional (``[external]`` extra). Import happens lazily inside the
functions via :func:`require_httpx`, mirroring every existing caller.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.utils.optional_deps import require_optional

if TYPE_CHECKING:
    import httpx
    from tenacity import RetryCallState

#: Default User-Agent for precis outbound requests. Individual callers
#: may override (e.g. the ORCID client appends a contact URL, the web
#: kind honours ``WEB_USER_AGENT``).
DEFAULT_USER_AGENT = "precis-mcp/1.0"

#: The optional-dependency extra that ships ``httpx``.
HTTPX_EXTRA = "external"


def require_httpx() -> Any:
    """Return the ``httpx`` module or raise the standard optional-dep error.

    Thin wrapper around :func:`require_optional` so the ``[external]``
    extra name is spelled in one place. Callers that need ``httpx`` for
    an ``except httpx.HTTPError`` clause use this; callers that only need
    a client use :func:`http_client`.
    """
    return require_optional("httpx", extra=HTTPX_EXTRA)


def http_client(
    *,
    timeout: float,
    headers: dict[str, str] | None = None,
    follow_redirects: bool = False,
    user_agent: str | None = DEFAULT_USER_AGENT,
) -> httpx.Client:
    """Construct an ``httpx.Client`` with precis' shared defaults.

    Args:
        timeout: per-request timeout in seconds (no default — every
            caller already states one; making it explicit keeps that).
        headers: extra headers merged on top of the User-Agent.
        follow_redirects: defaults to ``False``. Leave it False whenever
            the URL is agent-influenced and you route through
            :mod:`precis.utils.safe_fetch`; only set True for fixed,
            trusted API hosts that legitimately redirect.
        user_agent: sent as ``User-Agent``. Pass ``None`` to omit it (or
            to set it yourself via ``headers``).

    Returns:
        An ``httpx.Client`` — use it as a context manager. Its transport
        is :func:`precis.utils.safe_fetch.pinning_transport`, so every
        connection it opens is SSRF-classified and pinned at connect (the
        DNS-rebinding guard applies to *all* fetches through this client,
        not only those wrapped in ``safe_get``/``safe_stream``).
    """
    from precis.utils.safe_fetch import pinning_transport

    httpx = require_httpx()
    merged: dict[str, str] = {}
    if user_agent is not None:
        merged["User-Agent"] = user_agent
    if headers:
        merged.update(headers)
    return httpx.Client(
        timeout=timeout,
        follow_redirects=follow_redirects,
        headers=merged,
        transport=pinning_transport(),
    )


def external_retry(
    *,
    attempts: int = 5,
    wait_min_s: float = 1.0,
    wait_max_s: float = 60.0,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Shared tenacity retry decorator for outbound external-API calls
    (S2, Crossref, ...): exponential backoff, five attempts, reraise.

    Equivalent to the ``wait_exponential(min=1, max=60)`` /
    ``stop_after_attempt(5)`` / ``reraise=True`` decorator that used to be
    hand-copied at every retried call site — with two drain hooks added so
    a backoff sleep can't outlive the worker's SIGTERM drain budget
    (gr204611: a single 60s backoff sleep must not outlive the 60s drain
    window and get the worker SIGKILLed mid-retry, which is what mints the
    orphaned claims the reaper exists to clean up). The sleep wakes early
    on drain via :func:`precis.liveness.drain_sleep`, and the retry
    predicate refuses the *next* attempt once draining — with
    ``reraise=True`` the in-flight exception then propagates exactly as if
    the retry budget had been exhausted, so callers' existing failure
    handling is unchanged.
    """
    import tenacity

    from precis.liveness import drain_requested, drain_sleep

    def _drain_aware_sleep(seconds: float) -> None:
        drain_sleep(seconds)

    class _not_draining(tenacity.retry_base):
        def __call__(self, retry_state: RetryCallState) -> bool:
            return not drain_requested()

    return tenacity.retry(
        wait=tenacity.wait_exponential(min=wait_min_s, max=wait_max_s),
        stop=tenacity.stop_after_attempt(attempts),
        retry=tenacity.retry_if_exception_type(Exception) & _not_draining(),
        reraise=True,
        sleep=_drain_aware_sleep,
    )


__all__ = [
    "DEFAULT_USER_AGENT",
    "HTTPX_EXTRA",
    "external_retry",
    "http_client",
    "require_httpx",
]
