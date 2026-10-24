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
4. :func:`request_with_deadline` — an overall wall-clock bound on a
   request, which per-phase httpx timeouts cannot express (a trickling
   peer defeats read timeouts indefinitely — gr486823).

It is also the one politeness wrapper for third-party services
(gr493222): :func:`with_backoff` (response-returning sends) and
:func:`call_with_backoff` (raising client libraries) apply a per-service
:class:`Policy` from :data:`POLICIES` — full-jitter backoff on 429/5xx and
transport errors only, Retry-After clamped and parked on the shared
limiter row, one ``rate_limit`` token per attempt, a breaker, and a
drain-aware sleep. :func:`polite_user_agent` is the one honest
User-Agent. :func:`external_retry` and :func:`retry_transient` are shims
over the same core; ``precis.pcb._http`` re-exports it.

Bespoke per-kind error messages and ``next=`` hints stay at the call site —
deliberately tuned per kind, not duplication.

This module does **not** import ``httpx`` at module load — the lazy import
via :func:`require_httpx` keeps module import cheap and degrades a broken
venv into a typed, actionable error instead of an ImportError.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import http.client
import ipaddress
import json
import logging
import math
import random
import threading
import time
import urllib.error
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import TYPE_CHECKING, Any, Protocol
from urllib.parse import urlparse

from tenacity import RetryError

from precis.utils.deadline import DeadlineExceeded, call_with_deadline
from precis.utils.optional_deps import require_optional

if TYPE_CHECKING:
    import httpx

    from precis.utils import outbound_policy

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


def request_with_deadline(
    client: httpx.Client,
    deadline_s: float,
    send: Callable[[], httpx.Response],
    *,
    label: str = "",
) -> httpx.Response:
    """Run ``send()`` (e.g. ``lambda: client.get(url)`` or a ``safe_get``
    call) under an overall wall-clock deadline.

    The client's own ``timeout`` is *per phase* — a peer that trickles a
    byte inside every read window keeps the call alive indefinitely, which
    is how an ``orcid_enrich`` SSL read once held a worker host's loop
    ~15 min (gr486823). On expiry the client is closed (best-effort socket
    kill — see :mod:`precis.utils.deadline` for why a blocked recv may
    outlive it) and ``httpx.ReadTimeout`` is raised, so call sites
    catching ``httpx.HTTPError`` map it to their usual upstream error.
    The client must not be reused after a deadline hit.
    """
    httpx_mod = require_httpx()
    try:
        return call_with_deadline(
            send, deadline_s=deadline_s, abort=client.close, label=label
        )
    except DeadlineExceeded as exc:
        raise httpx_mod.ReadTimeout(str(exc)) from exc


def _unwrap_retry(exc: BaseException) -> BaseException:
    """The real failure inside a ``tenacity.RetryError``.

    The ``semanticscholar`` client wraps every failure its own retry loop
    gave up on in ``RetryError`` — and ``retry=False`` doesn't opt out (it
    just swaps in ``stop_after_attempt(1)``), so a single S2 429 surfaces
    as ``RetryError(ConnectionRefusedError)``, not the
    ``ConnectionRefusedError`` the status table below knows (gr503080 —
    the wrapped 429 read as unretryable and failed on first attempt).
    """
    if not isinstance(exc, RetryError):
        return exc
    attempt = getattr(exc, "last_attempt", None)
    if attempt is None:
        return exc
    inner = attempt.exception()
    if inner is None or inner is exc:
        return exc
    return _unwrap_retry(inner)


def _status_of(exc: BaseException) -> int | None:
    """HTTP status carried by ``exc``, or ``None`` when it has none.

    The ``semanticscholar`` library raises its own types instead of
    exposing the response (its ``ApiRequester`` maps 400/404/500/504 to
    exception classes, 403 to ``PermissionError`` and 429 to
    ``ConnectionRefusedError``); httpx and ``requests`` carry the response
    on the exception. Only the S2 mapping needs the type-based table.
    """
    exc = _unwrap_retry(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code
    response = getattr(exc, "response", None)
    code = getattr(response, "status_code", None)
    if isinstance(code, int):
        return code
    try:
        from semanticscholar import SemanticScholarException as s2x
    except ImportError:  # pragma: no cover - optional dependency
        return None
    if isinstance(exc, s2x.ServerErrorException):
        return 504 if isinstance(exc, s2x.GatewayTimeoutException) else 500
    if isinstance(exc, s2x.ObjectNotFoundException):
        return 404
    if isinstance(exc, s2x.BadQueryParametersException):
        return 400
    if isinstance(exc, PermissionError):
        return 403
    if isinstance(exc, ConnectionRefusedError):
        return 429
    return None


def _headers_of(exc: BaseException) -> Any:
    """Response headers carried by ``exc`` (httpx/requests ``.response``, or
    a stdlib ``urllib.error.HTTPError`` which is its own response)."""
    exc = _unwrap_retry(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return exc.headers
    return getattr(getattr(exc, "response", None), "headers", None)


def _is_transport_error(exc: BaseException) -> bool:
    """Connection/timeout failures in httpx, ``requests`` or the stdlib."""
    httpx = require_httpx()
    if isinstance(
        exc,
        (
            httpx.TransportError,
            ConnectionError,
            TimeoutError,
            http.client.IncompleteRead,
            # A gateway error page (HTML/empty body) makes the S2 library's
            # ``r.json()`` raise before it can map 500/504 to its own type.
            json.JSONDecodeError,
        ),
    ):
        return True
    try:
        from requests import exceptions as rex
    except ImportError:  # pragma: no cover - requests ships with habanero
        return False
    return isinstance(
        exc,
        (
            rex.ConnectionError,
            rex.Timeout,
            rex.ChunkedEncodingError,
            rex.ContentDecodingError,
        ),
    )


def is_retryable_external_error(exc: BaseException) -> bool:
    """True for transport errors and HTTP 429/5xx; everything else (4xx
    client errors, programming errors such as ``TypeError``, unknown
    exceptions) is permanent and must surface immediately."""
    status = _status_of(exc)
    if status is not None:
        return status == 429 or 500 <= status < 600
    return _is_transport_error(_unwrap_retry(exc))


# ── The politeness core ────────────────────────────────────────────────────
#
# One outbound wrapper for third-party services (gr493222), promoted from
# precis.pcb._http, which re-exports it. The rules, for every service:
#
# * Retry only what retrying can fix: 429, 5xx and transport errors. A 4xx
#   that is not 429 is a bug in our request; retrying it is noise.
# * Never retry 401/403. An auth failure retried in a loop looks like an
#   attack from the far end.
# * Full-jitter exponential backoff, so a fleet's retries do not land on
#   one beat.
# * Honour Retry-After (seconds or HTTP-date), clamp the sleep to the
#   policy cap, and park the service's shared limiter row for the hinted
#   time so every host waits, not just this one.
# * Take one token from the shared limiter (precis.utils.rate_limit) per
#   attempt, so pacing and backoff are one call.
# * Trip a per-service breaker rather than hammer an outage.
# * Back off with a drain-aware sleep, and stop retrying once the worker
#   drains.
#
# Sleep, clock and randomness are injected so every policy is unit-testable
# without real time passing.

#: Statuses worth a retry. Everything else is either success or our fault.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses that must *never* be retried, however tempting.
FATAL_STATUSES = frozenset({401, 403})

#: How much of a failing response body to keep for diagnosis. Bounded so a
#: chatty error page can't bloat the exception; never the headers — those
#: can carry auth material (session cookies, signed tokens).
_BODY_PREFIX_LIMIT = 500

#: Length of the refusal body kept in the diagnostics log line.
_REFUSAL_BODY_LOG = 300

#: Longest Retry-After we honour. A longer hint is clamped to this for the
#: limiter park (and to ``Policy.cap`` for an in-request sleep).
RETRY_AFTER_MAX_S = 900.0

#: JLC's response header naming the request, for support tickets.
TRACE_ID_HEADER = "J-Trace-ID"

#: Limiter row seeded on first use for a host with no row of its own
#: (``host:<netloc>``): ``(capacity, refill_per_sec)``, one request a second.
#: An ``llm-api``-class row keeps this same seed — its tokens are unused
#: (the class is rate-less) and the row exists to carry ``parked_until``
#: and the override columns (``outbound_policy`` docstring, D1).
DEFAULT_HOST_ROW = (1, 1.0)


class VendorError(Exception):
    """A third-party call failed in a way we will not retry."""

    def __init__(
        self,
        service: str,
        detail: str,
        *,
        status: int | None = None,
        body: str | None = None,
        url: str | None = None,
        code: str | int | None = None,
        trace_id: str | None = None,
    ):
        self.service = service
        self.status = status
        self.body = body
        self.url = url
        #: The vendor's business ``code`` from a 200 body, when that is
        #: what failed (JLC answers 200 with ``success: false``).
        self.code = code
        #: The vendor's request id (JLC: ``J-Trace-ID``) — what support asks for.
        self.trace_id = trace_id
        message = f"{service}: {detail}"
        if code is not None:
            message += f" code={code}"
        if trace_id:
            message += f" trace_id={trace_id}"
        if url:
            message += f" [{url}]"
        if body:
            message += f" body={body!r}"
        super().__init__(message)


class VendorUnavailable(VendorError):
    """Retries exhausted, the breaker is open, or the worker is draining.

    ``cooldown=True`` marks a refusal that is *ours*, not the vendor's:
    the breaker already stood open at entry, a limiter refused a retry,
    or the worker drained mid-call — no request got a provider answer.
    Fetch workers map it to a deferral-class event (the pass doesn't
    count as an attempt); the default ``False`` raises (``"N attempts
    exhausted"``, ``"circuit opened after …"``) are real failures the
    call observed and answered for.
    """

    def __init__(
        self,
        service: str,
        detail: str,
        *,
        cooldown: bool = False,
        **kw: Any,
    ) -> None:
        self.cooldown = cooldown
        super().__init__(service, detail, **kw)


@dataclass(frozen=True)
class Policy:
    """Backoff shape. Defaults are deliberately unhurried — we are a guest."""

    attempts: int = 5
    base: float = 1.0
    cap: float = 60.0
    #: Consecutive failures that open the breaker; 0 disables the breaker.
    breaker_threshold: int = 8
    #: How long the breaker stays open, in seconds.
    breaker_cooldown: float = 300.0
    #: Minimum spacing between calls to one service from this process.
    min_interval: float = 0.0
    #: Extra random spacing, uniform in ``[0, spacing_jitter]``, added on top
    #: of ``min_interval`` so a fleet's calls don't land on a fixed beat.
    spacing_jitter: float = 0.0
    #: When set, a fatal 401/403 opens the breaker at once for this many
    #: seconds (instead of counting toward ``breaker_threshold``).
    fatal_cooldown: float | None = None
    #: Whether a fatal 401/403 counts toward ``breaker_threshold``. Off for a
    #: service where a 403 is per-record (ORCID's private profiles), so a run
    #: of those must not open the breaker on healthy traffic.
    fatal_counts_toward_breaker: bool = True
    #: Human name for operator-facing messages (defaults to the service key).
    label: str = ""
    #: ``external_rate_limits`` row taken per attempt; ``None`` means no
    #: shared limiter unless the caller passes ``url=`` (then the
    #: ``host:<netloc>`` row).
    provider: str | None = None
    #: Longest wait for a limiter token before ``RateLimited``. The
    #: resolver's code-layer ``max_wait_s`` when ``max_wait_s`` is unset.
    limiter_wait_s: float = 30.0
    #: ``(capacity, refill_per_sec)`` seeded for a ``host:<netloc>`` row this
    #: policy creates (``provider`` unset, ``url=`` passed). The website
    #: politeness default; a rate-less class's row is a state carrier only.
    host_row: tuple[int, float] = DEFAULT_HOST_ROW
    #: Outbound policy class (``scraped-host``/``provider-api``/``llm-api``/
    #: ``llm-local``/``internal``); ``None`` = infer from provider/url —
    #: see :mod:`precis.utils.outbound_policy`.
    cls: str | None = None
    #: Admission-wait override feeding the resolved ``max_wait_s``;
    #: ``None`` inherits (``limiter_wait_s`` then the class default).
    max_wait_s: float | None = None
    #: Per-phase/overall timeout overrides; ``None`` = caller's timeout.
    connect_s: float | None = None
    read_s: float | None = None
    total_s: float | None = None
    #: ``wait``/``defer``/``fail`` override; ``None`` = class default.
    on_refuse: str | None = None
    #: ``resource_slots`` resource to hold for the call; ``None`` = inherit.
    concurrency: str | None = None


DEFAULT_POLICY = Policy()

#: Bulk enumeration walks the same host thousands of times — slower, and it
#: gives up sooner, because a long walk has time to notice an outage.
BULK_POLICY = Policy(attempts=4, base=2.0, cap=120.0, min_interval=0.5)

#: EasyEDA is a CDN-fronted guest service that has 403'd us. One policy for
#: every call site (single fetch and bulk warm alike): >=2 s between calls
#: plus jitter, and a refusal parks us for 30 min instead of probing again.
EASYEDA_POLICY = Policy(
    attempts=5,
    base=2.0,
    cap=60.0,
    min_interval=2.0,
    spacing_jitter=1.0,
    fatal_cooldown=1800.0,
    label="EasyEDA",
)

#: Per-service policies, keyed by the ``service`` name callers pass; each
#: names its limiter row (seeded by migration). A service missing here
#: gets :data:`DEFAULT_POLICY`.
POLICIES: dict[str, Policy] = {
    "easyeda": EASYEDA_POLICY,
    "s2": Policy(base=2.0, provider="s2", label="Semantic Scholar"),
    "crossref": Policy(provider="crossref", label="Crossref"),
    "crossref-list": Policy(base=2.0, provider="crossref-list", label="Crossref"),
    "openalex": Policy(provider="openalex", label="OpenAlex"),
    "openalex-content": Policy(provider="openalex-content", label="OpenAlex"),
    "unpaywall": Policy(provider="unpaywall", label="Unpaywall"),
    "arxiv": Policy(attempts=4, base=3.0, provider="arxiv", label="arXiv"),
    "europepmc": Policy(provider="europepmc", label="Europe PMC"),
    "core": Policy(provider="core", label="CORE"),
    "datacite": Policy(provider="datacite", label="DataCite"),
    "orcid": Policy(provider="orcid", fatal_counts_toward_breaker=False, label="ORCID"),
    "pubchem": Policy(provider="pubchem", label="PubChem"),
    "sec": Policy(provider="sec", label="SEC EDGAR"),
    "google-patents": Policy(
        base=2.0, provider="google-patents", label="Google Patents"
    ),
    "nanopub-registry": Policy(provider="nanopub-registry", label="nanopub registry"),
    "materialsproject": Policy(provider="materialsproject", label="Materials Project"),
    "digikey": Policy(provider="digikey", label="DigiKey"),
    "mouser": Policy(provider="mouser", label="Mouser"),
    "farnell": Policy(provider="farnell", label="Farnell"),
    # Services below take the ``host:<netloc>`` limiter row of the ``url=``
    # the caller passes (auto-seeded, one request a second), so they need no
    # migration. A 4xx other than 401/403/429 comes back as a response.
    "web-fetch": Policy(attempts=3, base=2.0, cap=30.0, label="web fetch"),
    "wikipedia": Policy(attempts=4, base=2.0, label="Wikipedia"),
    "wolfram": Policy(attempts=3, base=2.0, cap=30.0, label="Wolfram Alpha"),
    # A live agent waits on these two: few attempts, short cap. A 429 backs
    # off here and is never raised to the agent as a rate-limit error.
    "perplexity": Policy(attempts=4, base=2.0, cap=30.0, label="Perplexity"),
    # LLM APIs are metered, not scraped (gr507343): the ``llm-api`` class
    # has no token lane — the provider's own 429/Retry-After parks the
    # host row's ``parked_until`` and a shared slot hold caps in-flight
    # concurrency (``outbound_policy`` docstring, D1/D2).
    "openai-tools": Policy(
        attempts=2,
        base=2.0,
        cap=30.0,
        label="OpenAI-style API",
        cls="llm-api",
    ),
    "remarkable": Policy(
        attempts=4,
        base=2.0,
        fatal_counts_toward_breaker=False,
        label="reMarkable cloud",
    ),
    "nanopub-mirror": Policy(attempts=4, base=2.0, label="nanopub mirror"),
    "provenance-rw": Policy(attempts=4, base=2.0, label="Retraction Watch"),
    "secret-probe": Policy(attempts=3, base=2.0, cap=20.0, label="credential probe"),
    "datasheet-pull": Policy(attempts=3, base=2.0, cap=30.0, label="datasheet fetch"),
    "cif-fetch": Policy(attempts=3, base=2.0, cap=30.0, label="CIF fetch"),
    # LCSC product pages + their og:image thumbnails (precis_web.pcb_parts) —
    # a hover-card convenience, so give up fast rather than hold a request.
    "lcsc": Policy(attempts=3, base=2.0, cap=30.0, label="LCSC"),
    "discord": Policy(attempts=3, base=2.0, cap=30.0, label="Discord webhook"),
    # precis doctor's /healthz probes (web port, remote embedder) —
    # operator-configured endpoints: one retry on a short cap so a probe
    # fails fast (<~2 s of backoff) yet rides over a single blip.
    "doctor-probe": Policy(attempts=2, base=0.5, cap=2.0, label="doctor health probe"),
}


def policy_for(service: str) -> Policy:
    """The registered :class:`Policy` for ``service``, else the default."""
    return POLICIES.get(service, DEFAULT_POLICY)


@dataclass
class _Circuit:
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_call_at: float = field(default=0.0)
    #: Cooldown this opening lasts (the policy's, or the longer fatal one).
    cooldown: float = 0.0
    #: Set only by a fatal refusal: wall-clock end of the cooldown, for the
    #: operator-facing message.
    refused_until: datetime | None = None


#: Per-process breaker state, one per service. (Breaker state in the
#: ``external_rate_limits`` row would share it across hosts; not yet.)
_CIRCUITS: dict[str, _Circuit] = {}


def reset_circuit(service: str | None = None) -> None:
    """Clear breaker state — for tests, and for an operator retrying by hand."""
    if service is None:
        _CIRCUITS.clear()
    else:
        _CIRCUITS.pop(service, None)


def _delay(
    attempt: int, policy: Policy, uniform: Callable[[float, float], float]
) -> float:
    """Full-jitter exponential backoff (AWS's formulation): a random wait in
    ``[0, min(cap, base * 2**attempt)]``. Full jitter, not equal jitter,
    because our callers are a small fleet hitting one host — decorrelating
    them matters more than a tight lower bound."""
    return uniform(0.0, min(policy.cap, policy.base * (2**attempt)))


class UniformSource(Protocol):
    """Anything with ``random.Random.uniform``'s shape (tests pass stubs)."""

    def uniform(self, a: float, b: float) -> float: ...


def backoff_delay(
    attempt: int, policy: Policy = DEFAULT_POLICY, rng: UniformSource | None = None
) -> float:
    """Public face of the full-jitter delay, for callers that fail over
    between hosts and so cannot wrap one response in :func:`with_backoff`."""
    return _delay(attempt, policy, (rng or random.Random()).uniform)


def _body_prefix(resp: httpx.Response) -> str | None:
    """Bounded prefix of a failing response body — the shape of the
    complaint without the unbounded blob. Never headers."""
    try:
        text = resp.text
    except Exception:
        return None
    return text[:_BODY_PREFIX_LIMIT] if text else None


def redact_url(url: str | None) -> str | None:
    """``url`` with credential-named query param values masked (``REDACTED``).

    Diagnostic text only — never feed the result back into a request. The
    one name rule is :func:`~precis.utils.secret_scan.is_credential_param`
    (``api_key``/``token``/``key``/``secret``/``sig``/``mailto``/dotted
    envelopes like ``callInfo.apiKey``) and the mask token is
    :data:`~precis.utils.secret_scan.CREDENTIAL_MASK`, so a URL redacted here
    re-scans clean under :func:`mask_secrets` and reads identically to a line
    the record-creation scrub or the DB-write mask rewrote. A URL holding no
    credential param comes back byte-identical; input that does not parse as
    a URL is handed to :func:`mask_secrets` instead of being trusted through
    (gr502250: the OpenAlex content key rides in ``?api_key=``, and a log
    line that composes the request URL must never carry it).
    """
    if url is None:
        return None
    from precis.utils.secret_scan import (
        CREDENTIAL_MASK,
        is_credential_param,
        mask_secrets,
    )

    try:
        parsed = require_httpx().URL(url)
    except Exception:
        return mask_secrets(url, warn=False)
    names = {n for n in parsed.params if is_credential_param(n)}
    if not names:
        return url
    for name in names:
        parsed = parsed.copy_set_param(name, CREDENTIAL_MASK)
    return str(parsed)


def _url_of(resp: httpx.Response) -> str | None:
    """The request URL a failing response belongs to, redacted for
    diagnostics (a bare test-double :class:`httpx.Response` may not have
    one). Query credentials never compose into a log line or an exception
    message — everything that renders a request URL routes through here."""
    try:
        return redact_url(str(resp.request.url))
    except (RuntimeError, AttributeError):
        return None


def retry_after_seconds(
    headers: Any, *, wall: Callable[[], datetime] | None = None
) -> float | None:
    """``Retry-After`` from ``headers`` in seconds, clamped to
    ``[0, RETRY_AFTER_MAX_S]``; ``None`` when absent or unparseable.

    Takes both forms: delay-seconds and an HTTP-date (relative to
    ``wall()``, default now in UTC)."""
    raw = headers.get("Retry-After") if headers is not None else None
    if not raw:
        return None
    raw = str(raw).strip()
    try:
        secs = float(raw)
    except ValueError:
        try:
            when = parsedate_to_datetime(raw)
        except (TypeError, ValueError, IndexError):
            return None
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        current = wall() if wall is not None else datetime.now(UTC)
        secs = (when - current).total_seconds()
    if math.isnan(secs):
        return None
    return min(max(secs, 0.0), RETRY_AFTER_MAX_S)


def _is_handshake_failure(exc: BaseException) -> bool:
    """True for a failure to *establish* the connection (TCP/TLS handshake)
    — never a read timeout, an HTTP status error, or anything after a
    response. Covers httpx and ``requests`` (habanero's transport)."""
    httpx = require_httpx()
    if isinstance(exc, (httpx.ConnectTimeout, httpx.ConnectError)):
        return True
    try:
        from requests import exceptions as rex
    except ImportError:  # pragma: no cover - requests ships with habanero
        return False
    return isinstance(exc, rex.ConnectTimeout)


def _is_connect_failure(exc: BaseException) -> bool:
    """A handshake failure or no pooled connection: the request cannot have
    reached the server, so re-sending cannot duplicate anything."""
    return _is_handshake_failure(exc) or isinstance(exc, require_httpx().PoolTimeout)


def netloc_provider(url: str) -> str | None:
    """The default limiter row key for ``url``'s host: ``host:<netloc>``."""
    netloc = urlparse(url).netloc.lower()
    return f"host:{netloc}" if netloc else None


class _Unset:
    """Sentinel type: ``provider`` not passed."""


_UNSET = _Unset()


def _resolve_provider(
    provider: str | _Unset | None, policy: Policy, url: str | None
) -> tuple[str | None, tuple[int, float] | None]:
    """Limiter row for this call, and the row to seed when it is missing.

    An explicit ``provider`` wins (``None`` turns the limiter off), then
    the policy's row, then ``host:<netloc>`` of ``url`` — the only case
    that seeds a row (``policy.host_row``, :data:`DEFAULT_HOST_ROW` unless
    the policy names a metered API)."""
    if not isinstance(provider, _Unset):
        return provider, None
    if policy.provider is not None:
        return policy.provider, None
    if url:
        host = netloc_provider(url)
        if host is not None:
            return host, policy.host_row
    return None, None


def _default_sleep(seconds: float) -> None:
    """Backoff sleep that wakes early when the worker starts draining, so a
    sleep can't outlive the SIGTERM drain budget (a worker SIGKILLed
    mid-retry leaves an orphaned claim)."""
    from precis.liveness import drain_sleep

    drain_sleep(seconds)


def _draining() -> bool:
    from precis.liveness import drain_requested

    return drain_requested()


def _circuit_for(service: str, policy: Policy) -> _Circuit:
    """The service's shared breaker state; a throwaway one when the policy
    has neither breaker nor spacing (the shims), so per-host shim names
    don't grow :data:`_CIRCUITS`."""
    if policy.breaker_threshold <= 0 and policy.min_interval <= 0:
        return _Circuit()
    return _CIRCUITS.setdefault(service, _Circuit())


def _resolve_effective(
    service: str, policy: Policy, row: str | None, url: str | None
) -> outbound_policy.EffectivePolicy | None:
    """Resolve this call's :class:`EffectivePolicy`
    (:mod:`precis.utils.outbound_policy`): class, token lane, concurrency
    hold and wait
    bound in one cached view. ``None`` on a resolver fault — the caller
    falls back to the row + ``limiter_wait_s`` path exactly as before."""
    try:
        from precis.utils import outbound_policy

        return outbound_policy.resolve_for(service, policy, row, url)
    except Exception:  # pragma: no cover — resolve() itself is fail-open
        log.debug("http: outbound policy resolution failed", exc_info=True)
        return None


def _take_token(
    provider: str | None,
    policy: Policy,
    seed: tuple[int, float] | None,
    effective: outbound_policy.EffectivePolicy | None = None,
) -> None:
    """One limiter token for ``provider``; raises ``RateLimited`` on a
    refusal. Fails open (and logs) when the limiter store is unavailable.
    A resolved rate-less class (``llm-api``/``internal``/``llm-local``)
    takes no token — admission is the ``parked_until`` gate and/or a
    concurrency hold (``outbound_policy`` docstring, D1)."""
    if provider is None:
        return
    wait_s = policy.limiter_wait_s
    if effective is not None:
        if effective.rate is None:
            return
        wait_s = effective.max_wait_s
    from precis.utils import rate_limit

    rate_limit.require(provider, max_wait_s=wait_s, seed_row=seed)


def _park(provider: str | None, seconds: float | None) -> None:
    """Park ``provider``'s shared row for a server's Retry-After."""
    if provider is None or seconds is None or seconds <= 0:
        return
    from precis.utils import rate_limit

    rate_limit.park(provider, seconds)
    try:
        from precis.utils import outbound_policy

        outbound_policy.record_park(provider, seconds)
    except Exception:  # pragma: no cover — write-through is best-effort
        pass


def _enter(
    service: str,
    policy: Policy,
    circuit: _Circuit,
    *,
    now: Callable[[], float],
    sleep: Callable[[float], None],
    uniform: Callable[[float, float], float],
) -> None:
    """Breaker check (raises :class:`VendorUnavailable` while open), then the
    per-process spacing before the first attempt."""
    if circuit.opened_at is not None:
        elapsed = now() - circuit.opened_at
        if elapsed < circuit.cooldown:
            if circuit.refused_until is not None:
                raise VendorUnavailable(
                    service,
                    f"{policy.label or service} refused, cooling down until "
                    f"{circuit.refused_until:%H:%M}Z",
                    cooldown=True,
                )
            raise VendorUnavailable(
                service,
                f"circuit open ({circuit.consecutive_failures} consecutive "
                f"failures); retry in {circuit.cooldown - elapsed:.0f}s",
                cooldown=True,
            )
        # Cooldown elapsed — half-open: let one call through to probe.
        circuit.opened_at = None
        circuit.refused_until = None

    if policy.min_interval > 0.0:
        required = policy.min_interval + uniform(0.0, policy.spacing_jitter)
        gap = now() - circuit.last_call_at
        if gap < required:
            sleep(required - gap)


def with_backoff(
    send: Callable[[], httpx.Response],
    *,
    service: str,
    policy: Policy | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    rng: UniformSource | None = None,
    wall: Callable[[], datetime] = lambda: datetime.now(UTC),
    idempotent: bool = True,
    provider: str | _Unset | None = _UNSET,
    url: str | None = None,
) -> httpx.Response:
    """Call ``send()`` under the politeness rules; return the final response.

    ``send`` must be a zero-arg thunk performing exactly one HTTP request, so
    a retry re-issues it cleanly (build the request inside the thunk, not
    outside). Raises :class:`VendorError` on a fatal status,
    :class:`VendorUnavailable` when retries are exhausted, the breaker is
    open or the worker drains, and ``rate_limit.RateLimited`` when the
    shared limiter refuses. A non-retryable non-fatal status (a 404, say)
    is *returned* — the caller knows what its own 404 means; we do not.

    ``policy`` defaults to :func:`policy_for` of ``service``. The limiter
    row is ``provider`` if passed, else ``policy.provider``, else
    ``host:<netloc>`` of ``url`` (seeded at one request a second).

    ``idempotent=False`` is for POSTs that create something (an order, an
    upload): only connect-phase errors (the request never reached the
    server) and 429 (rejected unprocessed) are retried. A read timeout or a
    5xx may follow a server-side commit, so those raise :class:`VendorError`
    at once rather than risk a duplicate.
    """
    import httpx

    policy = policy or policy_for(service)
    uniform = (rng or random.Random()).uniform
    do_sleep = sleep if sleep is not None else _default_sleep
    row, seed = _resolve_provider(provider, policy, url)
    effective = _resolve_effective(service, policy, row, url)
    circuit = _circuit_for(service, policy)
    _enter(service, policy, circuit, now=now, sleep=do_sleep, uniform=uniform)

    last_detail = "no attempts made"
    last_status: int | None = None
    last_body: str | None = None
    last_url: str | None = None
    #: The transport exception behind the current failure, if any — kept
    #: outside the ``except`` block (which Python unbinds ``exc`` from on
    #: exit) so the eventual raise can chain ``from`` it.
    last_exc: BaseException | None = None
    for attempt in range(policy.attempts):
        if attempt and _draining():
            raise VendorUnavailable(
                service,
                f"worker draining; stopped retrying after {last_detail}",
                status=last_status,
                body=last_body,
                url=last_url,
                cooldown=True,
            ) from last_exc
        try:
            _take_token(row, policy, seed, effective)
        except Exception as refused:
            if not attempt:
                raise
            # A retry the limiter refuses (our own Retry-After park
            # outlasting the wait) ends the call like an exhausted budget.
            raise VendorUnavailable(
                service,
                f"limiter refused the retry after {last_detail}",
                status=last_status,
                body=last_body,
                url=last_url,
                cooldown=True,
            ) from refused
        try:
            resp = send()
        except httpx.HTTPError as exc:
            from precis.utils.secret_scan import mask_secrets

            # A ``raise_for_status`` inside ``send()`` folds the full request
            # URL — query credentials included — into the exception text;
            # mask before it composes into the error/log line.
            exc_text = mask_secrets(str(exc), warn=False)
            if not idempotent and not _is_connect_failure(exc):
                _trip(circuit, policy, now)
                raise VendorError(
                    service,
                    "transport error after the request may have been sent, "
                    "not retried (non-idempotent; check the account for a "
                    f"created order before re-sending): {exc_text}",
                ) from exc
            last_detail = f"transport error: {exc_text}"
            last_status = None
            last_body = None
            last_url = None
            last_exc = exc
            circuit.last_call_at = now()
        else:
            last_exc = None
            circuit.last_call_at = now()
            if resp.status_code in FATAL_STATUSES:
                _log_refusal(service, resp)
                if policy.fatal_cooldown is not None:
                    circuit.consecutive_failures += 1
                    circuit.opened_at = now()
                    circuit.cooldown = policy.fatal_cooldown
                    circuit.refused_until = wall() + timedelta(
                        seconds=policy.fatal_cooldown
                    )
                elif policy.fatal_counts_toward_breaker:
                    _trip(circuit, policy, now)
                raise VendorError(
                    service,
                    f"HTTP {resp.status_code} — auth/permission failure, not retried",
                    status=resp.status_code,
                )
            if resp.status_code not in RETRY_STATUSES:
                circuit.consecutive_failures = 0
                return resp
            last_detail = f"HTTP {resp.status_code}"
            last_status = resp.status_code
            last_body = _body_prefix(resp)
            last_url = _url_of(resp)
            if not idempotent and resp.status_code != 429:
                _trip(circuit, policy, now)
                raise VendorError(
                    service,
                    f"HTTP {resp.status_code}, not retried (non-idempotent; "
                    "the server may have committed — check the account for "
                    "a created order before re-sending)",
                    status=resp.status_code,
                    body=last_body,
                    url=last_url,
                    trace_id=resp.headers.get(TRACE_ID_HEADER),
                )
            hinted = retry_after_seconds(resp.headers, wall=wall)
            _park(row, hinted)
            if hinted is not None and attempt < policy.attempts - 1:
                _trip(circuit, policy, now)
                # Clamp: a LIVE caller must not block for the server's 900 s.
                do_sleep(min(hinted, policy.cap))
                continue

        _trip(circuit, policy, now)
        if circuit.opened_at is not None:
            raise VendorUnavailable(
                service,
                f"circuit opened after {last_detail}",
                status=last_status,
                body=last_body,
                url=last_url,
            ) from last_exc
        if attempt < policy.attempts - 1:
            do_sleep(_delay(attempt, policy, uniform))

    raise VendorUnavailable(
        service,
        f"{policy.attempts} attempts exhausted; last: {last_detail}",
        status=last_status,
        body=last_body,
        url=last_url,
    ) from last_exc


def call_with_backoff[T](
    fn: Callable[[], T],
    *,
    service: str,
    policy: Policy | None = None,
    sleep: Callable[[float], None] | None = None,
    now: Callable[[], float] = time.monotonic,
    uniform: Callable[[float, float], float] = random.uniform,
    wall: Callable[[], datetime] | None = None,
    idempotent: bool = True,
    provider: str | _Unset | None = _UNSET,
    url: str | None = None,
    retry_if: Callable[[BaseException], bool] = is_retryable_external_error,
    delay: Callable[[int], float] | None = None,
    on_error: Callable[[BaseException, int], None] | None = None,
    deadline: float | None = None,
) -> T:
    """:func:`with_backoff` for calls that *raise* on failure (client
    libraries such as ``semanticscholar`` or habanero, ``raise_for_status``
    wrappers) instead of returning a response.

    Retries an exception when ``retry_if`` says so (default: transport
    errors and HTTP 429/5xx, :func:`is_retryable_external_error`); anything
    else propagates on the first attempt. When retries run out, the worker
    drains or the breaker opens, the *original* exception is re-raised, so
    callers' failure handling is unchanged. An exception carrying a response
    with ``Retry-After`` gets that wait (clamped to ``policy.cap``) and
    parks the limiter row. ``delay(attempt)`` replaces the jittered backoff;
    ``on_error(exc, attempt)`` sees every exception (logging, capture).
    ``idempotent=False`` retries only connect-phase errors and 429.

    ``deadline`` is an optional instant on the ``now`` clock past which no
    further attempt is started — the current failure is re-raised instead
    of sleeping and retrying. The attempt already in flight is never
    interrupted, so the bound is "deadline + one request timeout". Used by
    the OA fetcher to cap a leg's retry tail inside a per-stub wall budget.
    """
    policy = policy or policy_for(service)
    do_sleep = sleep if sleep is not None else _default_sleep
    row, seed = _resolve_provider(provider, policy, url)
    effective = _resolve_effective(service, policy, row, url)
    circuit = _circuit_for(service, policy)
    _enter(service, policy, circuit, now=now, sleep=do_sleep, uniform=uniform)
    last_exc: BaseException | None = None
    for attempt in range(policy.attempts):
        try:
            _take_token(row, policy, seed, effective)
        except Exception:
            if last_exc is None:
                raise
            # A retry the limiter refuses ends the call with the original
            # failure, as an exhausted budget would.
            raise last_exc from None
        try:
            result = fn()
        except Exception as exc:
            if on_error is not None:
                on_error(exc, attempt)
            status = _status_of(exc)
            retryable = (
                status not in FATAL_STATUSES
                and retry_if(exc)
                and (idempotent or _is_connect_failure(exc) or status == 429)
            )
            if not retryable:
                raise
            last_exc = exc
            circuit.last_call_at = now()
            _trip(circuit, policy, now)
            hinted = retry_after_seconds(_headers_of(exc), wall=wall)
            _park(row, hinted)
            if (
                attempt + 1 >= policy.attempts
                or circuit.opened_at is not None
                or _draining()
                or (deadline is not None and now() >= deadline)
            ):
                raise
            if hinted is not None:
                wait = min(hinted, policy.cap)
            elif delay is not None:
                wait = delay(attempt)
            else:
                wait = _delay(attempt, policy, uniform)
            do_sleep(wait)
            if _draining():
                raise
            continue
        circuit.last_call_at = now()
        circuit.consecutive_failures = 0
        return result
    raise AssertionError("unreachable")  # pragma: no cover (attempts < 1)


async def async_call_with_backoff[T](
    fn: Callable[[], Awaitable[T]],
    *,
    service: str,
    policy: Policy | None = None,
    provider: str | _Unset | None = _UNSET,
    url: str | None = None,
    retry_if: Callable[[BaseException], bool] = is_retryable_external_error,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    """:func:`call_with_backoff` for coroutines (the secret-status probes).

    Same rules: breaker, limiter token per attempt (taken off the event
    loop), 401/403 never retried, full-jitter backoff, ``Retry-After``
    honoured and parked; the original exception is re-raised when retries
    run out. ``policy.min_interval`` is not applied here."""
    policy = policy or policy_for(service)
    row, seed = _resolve_provider(provider, policy, url)
    effective = _resolve_effective(service, policy, row, url)
    circuit = _circuit_for(service, policy)
    _enter(
        service,
        policy,
        circuit,
        now=time.monotonic,
        sleep=lambda _s: None,
        uniform=random.uniform,
    )
    for attempt in range(policy.attempts):
        await asyncio.to_thread(_take_token, row, policy, seed, effective)
        try:
            result = await fn()
        except Exception as exc:
            status = _status_of(exc)
            if status in FATAL_STATUSES or not retry_if(exc):
                raise
            circuit.last_call_at = time.monotonic()
            _trip(circuit, policy, time.monotonic)
            hinted = retry_after_seconds(_headers_of(exc))
            _park(row, hinted)
            if (
                attempt + 1 >= policy.attempts
                or circuit.opened_at is not None
                or _draining()
            ):
                raise
            wait = (
                min(hinted, policy.cap)
                if hinted is not None
                else _delay(attempt, policy, random.uniform)
            )
            await sleep(wait)
            continue
        circuit.last_call_at = time.monotonic()
        circuit.consecutive_failures = 0
        return result
    raise AssertionError("unreachable")  # pragma: no cover (attempts < 1)


#: Host suffixes that never name a third party.
_INTERNAL_SUFFIXES = (".local", ".lan", ".internal", ".localdomain", ".ts.net")


def is_internal_host(url: str) -> bool:
    """True when ``url``'s host is loopback, private/link-local, a bare
    single-label name or an internal suffix — a cluster- or operator-owned
    endpoint rather than a third party. Decided per call from the URL, so a
    config pointing the same client at a public host is paced, and one
    pointing it at the LAN is not."""
    host = (urlparse(url).hostname or "").lower().rstrip(".")
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return (
            host == "localhost" or "." not in host or host.endswith(_INTERNAL_SUFFIXES)
        )
    return ip.is_loopback or ip.is_private or ip.is_link_local


@dataclass(frozen=True, slots=True)
class _PacedHost:
    suffix: str
    row: str | None
    seed: tuple[int, float] | None
    policy: Policy
    user_agent: str | None
    service: str


_PACED_LOCK = threading.Lock()
_PACED_HOSTS: list[_PacedHost] = []
_PACED_ORIG_SEND: Any = None


def _paced_send(session: Any, request: Any, **kwargs: Any) -> Any:
    """``requests.Session.send`` replacement: one limiter token per outbound
    request to a registered host, a Retry-After on 429/503 parks the row."""
    host = (urlparse(str(request.url)).hostname or "").lower()
    with _PACED_LOCK:
        entry = next(
            (
                h
                for h in reversed(_PACED_HOSTS)
                if host == h.suffix or host.endswith("." + h.suffix)
            ),
            None,
        )
        orig = _PACED_ORIG_SEND
    if entry is None:
        return orig(session, request, **kwargs)
    if entry.user_agent and str(request.headers.get("User-Agent", "")).startswith(
        "python-requests"
    ):
        request.headers["User-Agent"] = entry.user_agent
    _take_token(
        entry.row,
        entry.policy,
        entry.seed,
        _resolve_effective(entry.service, entry.policy, entry.row, str(request.url)),
    )
    resp = orig(session, request, **kwargs)
    if resp.status_code in (429, 503):
        _park(entry.row, retry_after_seconds(resp.headers))
    return resp


def limiter_pacer(service: str, *, policy: Policy | None = None) -> Callable[[], None]:
    """A zero-argument callable taking one limiter token for ``service`` —
    for code that must not import ``precis`` (``precis_dft``) but loops over
    several upstream requests and has to pace each one."""
    policy = policy or policy_for(service)
    row, seed = _resolve_provider(_UNSET, policy, None)

    def pace() -> None:
        _take_token(row, policy, seed, _resolve_effective(service, policy, row, None))

    return pace


@contextlib.contextmanager
def paced_requests(
    host: str,
    *,
    service: str,
    policy: Policy | None = None,
    provider: str | _Unset | None = _UNSET,
    user_agent: str | None = None,
) -> Iterator[None]:
    """Pace every ``requests`` call to ``host`` (and its subdomains) made
    inside the block — for client libraries that issue several requests per
    call and expose no hook (``mp_api``, ``youtube_transcript_api``), where
    :func:`call_with_backoff` can only meter the whole call.

    Each request takes a token from the service's limiter row (the policy's
    ``limiter_wait_s`` bounds the wait; a refusal raises ``RateLimited`` out
    of the library call), and a 429/503 ``Retry-After`` parks the row.
    ``user_agent`` replaces the library's bare ``python-requests`` UA only;
    a library that sets its own keeps it. Requests to other hosts pass
    through untouched. Re-entrant and safe across threads: the patch is
    installed with the first block and removed with the last.
    """
    import requests

    session_cls: Any = requests.Session  # Any: the class attribute is swapped
    policy = policy or policy_for(service)
    url = f"https://{host}/"
    row, seed = _resolve_provider(provider, policy, url)
    entry = _PacedHost(host.lower(), row, seed, policy, user_agent, service)
    global _PACED_ORIG_SEND
    with _PACED_LOCK:
        if not _PACED_HOSTS:
            _PACED_ORIG_SEND = session_cls.send
            session_cls.send = _paced_send
        _PACED_HOSTS.append(entry)
    try:
        yield
    finally:
        with _PACED_LOCK:
            _PACED_HOSTS.remove(entry)
            if not _PACED_HOSTS:
                session_cls.send = _PACED_ORIG_SEND


def polite_safe_get(
    client: httpx.Client,
    url: str,
    *,
    service: str | None = None,
    policy: Policy | None = None,
    **kwargs: Any,
) -> httpx.Response:
    """:func:`safe_get` (SSRF-pinned, redirect-revalidated) under the
    politeness core: a limiter token per attempt on ``host:<netloc>``,
    full-jitter backoff on transport errors and 429/5xx, ``Retry-After``
    parks the row, 401/403 are fatal (:class:`VendorError`).

    ``service`` defaults to the host, so one dead site cannot open the
    breaker for every other site an agent asks for. An :class:`SsrfBlocked`
    propagates untouched (not retried). Other 4xx come back as responses."""
    from precis.utils.safe_fetch import safe_get

    return with_backoff(
        lambda: safe_get(client, url, **kwargs),
        service=service or netloc_provider(url) or "web-fetch",
        policy=policy or POLICIES["web-fetch"],
        url=url,
    )


def _log_refusal(service: str, resp: httpx.Response) -> None:
    """Diagnostics for a 401/403: bounded body prefix and the CDN request id
    (``x-amz-cf-id``, which the operator can quote to the CDN). Read-only —
    nothing here feeds back into the request headers."""
    body = _body_prefix(resp) or ""
    log.warning(
        "%s refused HTTP %s url=%s x-amz-cf-id=%s body=%r",
        service,
        resp.status_code,
        _url_of(resp),
        resp.headers.get("x-amz-cf-id"),
        body[:_REFUSAL_BODY_LOG],
    )


def _trip(circuit: _Circuit, policy: Policy, now: Callable[[], float]) -> None:
    circuit.consecutive_failures += 1
    if (
        policy.breaker_threshold > 0
        and circuit.consecutive_failures >= policy.breaker_threshold
        and circuit.opened_at is None
    ):
        circuit.opened_at = now()
        circuit.cooldown = policy.breaker_cooldown


# ── User-Agent ─────────────────────────────────────────────────────────────

#: Where an operator on the far end can read what we are.
PROJECT_URL = "https://github.com/retospect/precis-mcp"

#: Contact used when no polite-pool email is configured: a real, monitored
#: address (the one SEC EDGAR's fair-access policy already gets).
FALLBACK_CONTACT = "reto@retostamm.com"

_PLACEHOLDER_CONTACT_MARKERS = ("noreply", "no-reply", "example.com", "example.org")
_FORBIDDEN_UA_PREFIXES = (
    "mozilla/",
    "python-requests/",
    "python-httpx/",
    "python-urllib/",
    "curl/",
)


def _real_contact(email: str | None) -> str | None:
    email = (email or "").strip()
    if "@" not in email:
        return None
    lowered = email.lower()
    if any(marker in lowered for marker in _PLACEHOLDER_CONTACT_MARKERS):
        return None
    return email


def polite_user_agent(
    contact: str | None = None, *, product: str = "precis-mcp"
) -> str:
    """The one honest User-Agent for third-party services:
    ``precis-mcp/<version> (+<project url>; mailto:<contact>)``.

    ``contact`` defaults to the ``contact.polite_email`` setting; a missing
    or placeholder address (``noreply``, ``example.com``) is replaced by
    :data:`FALLBACK_CONTACT` rather than sent."""
    if _real_contact(contact) is None:
        try:
            from precis import settings

            contact = settings.get_str("contact.polite_email")
        except Exception:
            log.debug("polite_user_agent: settings lookup failed", exc_info=True)
            contact = None
    email = _real_contact(contact) or FALLBACK_CONTACT
    return check_user_agent(f"{product}/{_version()} (+{PROJECT_URL}; mailto:{email})")


def check_user_agent(ua: str) -> str:
    """Return ``ua`` if it identifies us; raise ``ValueError`` for a
    browser spoof, a bare library default, or a placeholder contact."""
    lowered = ua.strip().lower()
    if not lowered or lowered.startswith(_FORBIDDEN_UA_PREFIXES):
        raise ValueError(f"User-Agent {ua!r} is a spoof or a bare library default")
    if any(marker in lowered for marker in _PLACEHOLDER_CONTACT_MARKERS):
        raise ValueError(f"User-Agent {ua!r} carries a placeholder contact")
    return ua


def _version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("precis-mcp")
    except PackageNotFoundError:  # pragma: no cover - source checkout
        return "0"


# ── Shims (callers unchanged) ──────────────────────────────────────────────


def external_retry(
    *,
    attempts: int = 5,
    wait_min_s: float = 1.0,
    wait_max_s: float = 60.0,
    sleep: Callable[[float], None] | None = None,
    uniform: Callable[[float, float], float] = random.uniform,
) -> Callable[[Callable[..., Any]], Callable[..., Any]]:
    """Decorator form of :func:`call_with_backoff` for outbound external-API
    calls (S2, Crossref, ...): full-jitter exponential backoff, five
    attempts, the original exception re-raised.

    Only transport errors and HTTP 429/5xx are retried
    (:func:`is_retryable_external_error`); a 4xx or a programming error
    propagates on the first attempt. The wait before retry ``n`` is
    ``uniform(0, min(wait_max_s, wait_min_s * 2**n))``. The sleep is
    drain-aware and no retry starts once the worker drains. No breaker and
    no limiter row: callers take their own token today (gr493222 moves them
    onto :func:`call_with_backoff` with a service policy).
    ``sleep``/``uniform`` are test seams.
    """
    policy = Policy(
        attempts=attempts, base=wait_min_s, cap=wait_max_s, breaker_threshold=0
    )

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            return call_with_backoff(
                lambda: fn(*args, **kwargs),
                service=f"external_retry:{fn.__qualname__}",
                policy=policy,
                sleep=sleep,
                uniform=uniform,
                provider=None,
            )

        return wrapper

    return decorate


def retry_transient[T](
    fn: Callable[[], T],
    *,
    host: str = "",
    attempts: int = 2,
    backoff_s: tuple[float, ...] = (3.0,),
    sleep: Callable[[float], None] | None = None,
) -> T:
    """Call ``fn()``, retrying only on a connect-phase failure.

    Retries ``httpx.ConnectTimeout`` / ``ConnectError`` (and ``requests`` ``ConnectTimeout`` for habanero) up to ``attempts``
    total tries with a short fixed backoff (``backoff_s[i]`` before retry
    ``i+1``, last entry reused). Motivation: the fetcher host intermittently
    stalls the TLS handshake to a single ``api.crossref.org`` IP and recovers
    within a minute (gr465931). Anything else — read timeouts, HTTP status
    errors, errors after a response — propagates immediately. ``host`` is
    only for the INFO log line (a URL is reduced to its netloc). A shim
    over :func:`call_with_backoff` with no breaker and no limiter.
    """
    label = (urlparse(host).netloc or host) if "//" in host else host

    def fixed_delay(attempt: int) -> float:
        return backoff_s[min(attempt, len(backoff_s) - 1)] if backoff_s else 0.0

    def on_error(exc: BaseException, attempt: int) -> None:
        if not _is_handshake_failure(exc):
            return
        from precis.utils.stall_capture import maybe_capture

        maybe_capture(exc, host)
        if attempt + 1 < attempts:
            log.info(
                "retry_transient: %s on %s, retrying in %.1fs (attempt %d/%d)",
                type(exc).__name__,
                label or "?",
                fixed_delay(attempt),
                attempt + 2,
                attempts,
            )

    return call_with_backoff(
        fn,
        service=f"retry_transient:{label or '?'}",
        policy=Policy(attempts=attempts, breaker_threshold=0),
        sleep=sleep if sleep is not None else time.sleep,
        provider=None,
        retry_if=_is_handshake_failure,
        delay=fixed_delay,
        on_error=on_error,
    )


__all__ = [
    "BULK_POLICY",
    "DEFAULT_HOST_ROW",
    "DEFAULT_POLICY",
    "DEFAULT_USER_AGENT",
    "EASYEDA_POLICY",
    "FATAL_STATUSES",
    "POLICIES",
    "RETRY_STATUSES",
    "TRACE_ID_HEADER",
    "Policy",
    "VendorError",
    "VendorUnavailable",
    "async_call_with_backoff",
    "backoff_delay",
    "call_with_backoff",
    "check_user_agent",
    "external_retry",
    "http_client",
    "is_internal_host",
    "is_retryable_external_error",
    "netloc_provider",
    "policy_for",
    "polite_safe_get",
    "polite_user_agent",
    "redact_url",
    "request_with_deadline",
    "require_httpx",
    "reset_circuit",
    "retry_after_seconds",
    "retry_transient",
    "with_backoff",
]
