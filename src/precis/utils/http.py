"""Shared outbound-HTTP seam — the single boundary every network-reaching
kind (``web``, ``news``, ``wikipedia``, ``semanticscholar``, ``math``,
``perplexity``, ``youtube``, the ORCID ingest client, …) routes through.

:func:`http_client` centralises three things that were previously
open-coded per call site:

1. ``httpx = require_optional("httpx")`` — the lazy import gate + install
   hint.
2. A ``User-Agent`` header.
3. ``follow_redirects=`` — security-relevant: :mod:`precis.utils.safe_fetch`'s
   SSRF guard only works when the client does **not** auto-follow redirects
   (``safe_get`` walks the chain itself, revalidating each hop). A client
   that defaulted to ``follow_redirects=True`` would let an agent-supplied
   URL redirect into a private/loopback/metadata address.

Bespoke per-kind error messages and ``next=`` hints stay at the call site —
deliberately tuned per kind, not duplication.

This module does **not** import ``httpx`` at module load — the lazy import
via :func:`require_httpx` keeps module import cheap and degrades a broken
venv into a typed, actionable error instead of an ImportError.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

from precis.utils.optional_deps import require_optional

if TYPE_CHECKING:
    import httpx
    from tenacity import RetryCallState

log = logging.getLogger(__name__)

#: Default User-Agent for precis outbound requests. Individual callers
#: may override (e.g. the ORCID client appends a contact URL, the web
#: kind honours ``WEB_USER_AGENT``).
DEFAULT_USER_AGENT = "precis-mcp/1.0"


def require_httpx() -> Any:
    """Return the ``httpx`` module or raise the standard install-hint error.

    Thin wrapper around :func:`require_optional` (``httpx`` is a core
    dep; a miss means a broken venv). Callers that need ``httpx`` for
    an ``except httpx.HTTPError`` clause use this; callers that only need
    a client use :func:`http_client`.
    """
    return require_optional("httpx")


def http_client(
    *,
    timeout: float | httpx.Timeout,
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

    Two drain hooks are added so a backoff sleep can't outlive the worker's
    SIGTERM drain budget (a sleep that outlives the drain window gets the
    worker SIGKILLed mid-retry, minting an orphaned claim). The sleep wakes
    early on drain via :func:`precis.liveness.drain_sleep`, and the retry
    predicate refuses the *next* attempt once draining — with
    ``reraise=True`` the in-flight exception then propagates as if the
    retry budget had been exhausted, so callers' failure handling is
    unchanged.
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


def _is_connect_failure(exc: BaseException) -> bool:
    """True for a failure to *establish* the connection (TCP/TLS handshake) —
    never a read timeout, an HTTP status error, or anything after a response.
    Covers httpx and ``requests`` (habanero's transport)."""
    httpx = require_httpx()
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    try:
        from requests import exceptions as rex
    except ImportError:  # pragma: no cover - requests ships with habanero
        return False
    return isinstance(exc, rex.ConnectTimeout)


def retry_transient[T](
    fn: Callable[[], T],
    *,
    host: str = "",
    attempts: int = 2,
    backoff_s: tuple[float, ...] = (3.0,),
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call ``fn()``, retrying only on a connect-phase failure.

    Retries ``httpx.ConnectTimeout`` / ``httpx.ConnectError`` (and
    ``requests`` ``ConnectTimeout`` for habanero) up to ``attempts`` total
    tries with a short fixed backoff (``backoff_s[i]`` before retry ``i+1``,
    last entry reused). Motivation: the fetcher host intermittently stalls
    the TLS handshake to a single ``api.crossref.org`` IP and recovers within
    a minute (gr465931). Anything else — read timeouts, HTTP status errors,
    errors after a response — propagates immediately. ``host`` is only for
    the INFO log line (a URL is reduced to its netloc).
    """
    do_sleep = sleep if sleep is not None else time.sleep
    label = (urlparse(host).netloc or host) if "//" in host else host
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:
            if attempt + 1 >= attempts or not _is_connect_failure(exc):
                raise
            delay = backoff_s[min(attempt, len(backoff_s) - 1)] if backoff_s else 0.0
            log.info(
                "retry_transient: %s on %s, retrying in %.1fs (attempt %d/%d)",
                type(exc).__name__,
                label or "?",
                delay,
                attempt + 2,
                attempts,
            )
            do_sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover (attempts < 1)


__all__ = [
    "DEFAULT_USER_AGENT",
    "external_retry",
    "http_client",
    "require_httpx",
    "retry_transient",
]
