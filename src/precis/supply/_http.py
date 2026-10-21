"""Redact supplier API query keys in HTTPX's informational request log.

Farnell and Mouser require query credentials; HTTPX logs the request URL at
INFO even on success. Keep an idempotent filter on that logger so concurrent
requests cannot expose keys when logging is enabled. The credential-name rule
and the ``REDACTED`` token are shared with the record-creation scrub
(:mod:`precis.utils.log_scrub`), which runs earlier — one mask shape on
either path. Actual requests are unchanged; errors returned to callers use
only safe stage/status diagnostics.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import replace

import httpx

from precis.supply.base import SupplierError
from precis.utils.secret_scan import CREDENTIAL_MASK, is_credential_param


class _QueryKeys(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            args = []
            for arg in record.args:
                if isinstance(arg, httpx.URL):
                    for name in arg.params:
                        if is_credential_param(name):
                            arg = arg.copy_set_param(name, CREDENTIAL_MASK)
                args.append(arg)
            record.args = tuple(args)
        return True


def protect_request_logs() -> None:
    logger = logging.getLogger("httpx")
    if not any(isinstance(item, _QueryKeys) for item in logger.filters):
        logger.addFilter(_QueryKeys())


def polite_send(
    send: Callable[[], httpx.Response],
    *,
    service: str,
    stage: str,
    detail: Callable[[httpx.Response], str] | None = None,
) -> httpx.Response:
    """Run one supplier request under the shared politeness core.

    Live lookups are interactive, so the budget is bounded (two attempts,
    10 s cap, 5 s limiter wait). Every attempt takes a limiter token and
    honours ``Retry-After``. A 4xx other than 429 comes back as a response
    for the adapter's own status handling. What the core raises is mapped to
    :class:`SupplierError` carrying only the stage, status and the adapter's
    own safe ``detail(response)``, so callers keep their existing handling:
    401/403 keep their status, and an exhausted 429/5xx keeps that status and
    is never a "not found". A failure with no HTTP status (transport error
    after the retry, breaker open, limiter refusal) has ``status=None`` and
    says so, rather than posing as an HTTP status. The core's message can
    carry the request URL (query credentials), so it is never forwarded.
    """
    from precis.utils.http import FATAL_STATUSES, VendorError, policy_for, with_backoff
    from precis.utils.rate_limit import RateLimited

    policy = replace(policy_for(service), attempts=2, cap=10.0, limiter_wait_s=5.0)
    seen = ""

    def guarded() -> httpx.Response:
        nonlocal seen
        resp = send()
        seen = detail(resp) if detail is not None else ""
        if resp.status_code in FATAL_STATUSES:
            # The core logs a refusal's URL and body; for these suppliers
            # both can echo the API key. The safe detail was taken above.
            return httpx.Response(resp.status_code)
        return resp

    try:
        return with_backoff(guarded, service=service, policy=policy)
    except VendorError as exc:
        if exc.status is not None:
            raise SupplierError(stage, exc.status, seen) from None
        text = str(exc)
        if any(w in text for w in ("circuit", "cooling", "limiter", "draining")):
            reason = f"temporarily paused after repeated failures at {stage}"
        else:
            reason = f"transport error at {stage}"
        raise SupplierError(stage, None, reason=reason) from None
    except RateLimited:
        raise SupplierError(
            stage, None, reason=f"rate limited locally at {stage}"
        ) from None
