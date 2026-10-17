"""Politeness layer for the two third-party catalog services.

EasyEDA (footprints) and JLCPCB (stock/price/orders) are *someone else's*
servers, and both can blacklist us. Every outbound call in :mod:`precis.pcb`
goes through :func:`with_backoff`. The wrapper itself now lives in
:mod:`precis.utils.http` (gr493222), the one outbound wrapper for every
third-party service; this module re-exports it so ``precis.pcb`` imports
keep working, and keeps the JLC-specific business-envelope check.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.utils.http import (
    BULK_POLICY,
    DEFAULT_POLICY,
    EASYEDA_POLICY,
    FATAL_STATUSES,
    RETRY_STATUSES,
    TRACE_ID_HEADER,
    Policy,
    VendorError,
    VendorUnavailable,
    backoff_delay,
    reset_circuit,
    with_backoff,
)

if TYPE_CHECKING:
    import httpx


def _code_ok(code: Any) -> bool:
    """Lenient success test: 0/200 in any numeric spelling ("200.0",
    "000000") or the words ok/success; anything else is a failure."""
    text = str(code).strip().lower()
    if text in ("ok", "success"):
        return True
    try:
        return int(float(text)) in (0, 200)
    except ValueError:
        return False


def check_envelope(
    service: str, data: dict[str, Any], resp: httpx.Response, path: str
) -> dict[str, Any]:
    """Raise :class:`VendorError` (with code + trace id) when a 200 body
    carries a failed business envelope: ``success`` false, or — with no
    ``success`` field — a ``code`` other than 0/200. A body with neither
    field is passed through (not every endpoint wraps)."""
    success = data.get("success")
    code = data.get("code")
    failed = success is False or (
        success is None and code is not None and not _code_ok(code)
    )
    if failed:
        msg = data.get("message") or data.get("msg") or "no message"
        raise VendorError(
            service,
            f"business failure from {path}: {str(msg)[:200]}",
            status=resp.status_code,
            code=code,
            trace_id=resp.headers.get(TRACE_ID_HEADER),
            url=path,
        )
    return data


__all__ = [
    "BULK_POLICY",
    "DEFAULT_POLICY",
    "EASYEDA_POLICY",
    "FATAL_STATUSES",
    "RETRY_STATUSES",
    "TRACE_ID_HEADER",
    "Policy",
    "VendorError",
    "VendorUnavailable",
    "backoff_delay",
    "check_envelope",
    "reset_circuit",
    "with_backoff",
]
