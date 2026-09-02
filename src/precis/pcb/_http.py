"""Politeness layer for the two third-party catalog services.

EasyEDA (footprints) and JLCPCB (stock/price/orders) are *someone else's*
servers, and both can blacklist us. Every outbound call in :mod:`precis.pcb`
goes through :func:`with_backoff`, which enforces the same four rules:

* **Retry only what retrying can fix** — 429, 5xx, and transport errors.
  A 4xx that isn't 429 is a bug in our request; retrying it is noise.
* **Never retry 401/403.** An auth failure loops forever and looks exactly
  like an attack from the far end. Fail fast, surface the status.
* **Honour ``Retry-After``** when the server sends one — it is the server
  telling us the answer we would otherwise be guessing.
* **Trip a breaker rather than hammer.** Consecutive failures against one
  service open a circuit for a cooldown window, so a bulk walk stops
  instead of turning an outage into a flood.

Sleep and clock are injected so the whole policy is unit-testable without
real time passing.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import httpx

#: Statuses worth a retry. Everything else is either success or our fault.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

#: Statuses that must *never* be retried, however tempting.
FATAL_STATUSES = frozenset({401, 403})

#: How much of a failing response body to keep for diagnosis. Bounded so a
#: chatty error page can't bloat the exception; never the headers — those
#: can carry auth material (session cookies, signed tokens).
_BODY_PREFIX_LIMIT = 500


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
    ):
        self.service = service
        self.status = status
        self.body = body
        self.url = url
        message = f"{service}: {detail}"
        if url:
            message += f" [{url}]"
        if body:
            message += f" body={body!r}"
        super().__init__(message)


class VendorUnavailable(VendorError):
    """Retries exhausted, or the circuit breaker is open for this service."""


@dataclass(frozen=True)
class Policy:
    """Backoff shape. Defaults are deliberately unhurried — we are a guest."""

    attempts: int = 5
    base: float = 1.0
    cap: float = 60.0
    #: Consecutive failures that open the breaker.
    breaker_threshold: int = 8
    #: How long the breaker stays open, in seconds.
    breaker_cooldown: float = 300.0
    #: Minimum spacing between calls to one service (bulk-walk politeness).
    min_interval: float = 0.0


DEFAULT_POLICY = Policy()

#: Bulk enumeration walks the same host thousands of times — slower, and it
#: gives up sooner, because a long walk has time to notice an outage.
BULK_POLICY = Policy(attempts=4, base=2.0, cap=120.0, min_interval=0.5)


@dataclass
class _Circuit:
    consecutive_failures: int = 0
    opened_at: float | None = None
    last_call_at: float = field(default=0.0)


_CIRCUITS: dict[str, _Circuit] = {}


def reset_circuit(service: str | None = None) -> None:
    """Clear breaker state — for tests, and for an operator retrying by hand."""
    if service is None:
        _CIRCUITS.clear()
    else:
        _CIRCUITS.pop(service, None)


def _delay(attempt: int, policy: Policy, rng: random.Random) -> float:
    """Full-jitter exponential backoff (AWS's formulation): sleep a random
    amount in ``[0, min(cap, base * 2**attempt)]``. Full jitter, not
    equal jitter, because our callers are a small fleet hitting one host —
    decorrelating them matters more than a tight lower bound."""
    ceiling = min(policy.cap, policy.base * (2**attempt))
    return rng.uniform(0.0, ceiling)


def _body_prefix(resp: httpx.Response) -> str | None:
    """Bounded prefix of a failing response body — the shape of the
    complaint (validation error, HTML error page, ...) without the
    unbounded blob. Never headers: those are where auth material lives."""
    try:
        text = resp.text
    except Exception:
        return None
    return text[:_BODY_PREFIX_LIMIT] if text else None


def _url_of(resp: httpx.Response) -> str | None:
    """The request URL a failing response belongs to, if one was attached
    (a bare test-double :class:`httpx.Response` may not have one)."""
    try:
        return str(resp.request.url)
    except RuntimeError:
        return None


def _retry_after(resp: httpx.Response) -> float | None:
    """``Retry-After`` in seconds, if the server sent a sane one."""
    raw = resp.headers.get("Retry-After")
    if not raw:
        return None
    try:
        secs = float(raw.strip())
    except ValueError:
        # The HTTP-date form. We do not parse it: a date we mis-parse is
        # worse than falling back to our own backoff, which is bounded.
        return None
    # Ignore absurd values rather than sleeping for an hour inside a request.
    return secs if 0.0 <= secs <= 900.0 else None


def with_backoff(
    send: Callable[[], httpx.Response],
    *,
    service: str,
    policy: Policy = DEFAULT_POLICY,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], float] = time.monotonic,
    rng: random.Random | None = None,
) -> httpx.Response:
    """Call ``send()`` under the politeness rules; return the final response.

    ``send`` must be a zero-arg thunk performing exactly one HTTP request, so
    a retry re-issues it cleanly (build the request inside the thunk, not
    outside). Raises :class:`VendorError` on a fatal status and
    :class:`VendorUnavailable` when retries are exhausted or the breaker is
    open. A non-retryable non-fatal status (a 404, say) is *returned* — the
    caller knows what its own 404 means; we do not.
    """
    import httpx

    rng = rng or random.Random()
    circuit = _CIRCUITS.setdefault(service, _Circuit())

    if circuit.opened_at is not None:
        elapsed = now() - circuit.opened_at
        if elapsed < policy.breaker_cooldown:
            raise VendorUnavailable(
                service,
                f"circuit open ({circuit.consecutive_failures} consecutive "
                f"failures); retry in {policy.breaker_cooldown - elapsed:.0f}s",
            )
        # Cooldown elapsed — half-open: let one call through to probe.
        circuit.opened_at = None

    if policy.min_interval > 0.0:
        gap = now() - circuit.last_call_at
        if gap < policy.min_interval:
            sleep(policy.min_interval - gap)

    last_detail = "no attempts made"
    last_status: int | None = None
    last_body: str | None = None
    last_url: str | None = None
    #: The transport exception behind the current failure, if any — kept
    #: outside the ``except`` block (which Python unbinds ``exc`` from on
    #: exit) so the eventual raise can chain ``from`` it.
    last_exc: BaseException | None = None
    for attempt in range(policy.attempts):
        try:
            resp = send()
        except httpx.HTTPError as exc:
            last_detail = f"transport error: {exc}"
            last_status = None
            last_body = None
            last_url = None
            last_exc = exc
        else:
            last_exc = None
            circuit.last_call_at = now()
            if resp.status_code in FATAL_STATUSES:
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
            hinted = _retry_after(resp)
            if hinted is not None and attempt < policy.attempts - 1:
                _trip(circuit, policy, now)
                sleep(hinted)
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
            sleep(_delay(attempt, policy, rng))

    raise VendorUnavailable(
        service,
        f"{policy.attempts} attempts exhausted; last: {last_detail}",
        status=last_status,
        body=last_body,
        url=last_url,
    ) from last_exc


def _trip(circuit: _Circuit, policy: Policy, now: Callable[[], float]) -> None:
    circuit.consecutive_failures += 1
    if (
        circuit.consecutive_failures >= policy.breaker_threshold
        and circuit.opened_at is None
    ):
        circuit.opened_at = now()


__all__ = [
    "BULK_POLICY",
    "DEFAULT_POLICY",
    "FATAL_STATUSES",
    "RETRY_STATUSES",
    "Policy",
    "VendorError",
    "VendorUnavailable",
    "reset_circuit",
    "with_backoff",
]
