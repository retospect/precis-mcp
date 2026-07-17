"""Window admission — refuse a doomed (context, model) pairing *loudly*.

Slice 2 of the ``llm`` catalog (docs/proposals/llm-catalog.md). The guardrail
that stops "100k tokens into a 2k window" from silently truncating or 400-ing:
a **pure integer fit-check** run wherever a context is paired with a model, hot
path included. It costs nothing (arithmetic), so it is unconditional — unlike
``select_offering`` (the ranking), which is gated to decision points.

Two layers, both here:

* :func:`admit` — the pure check: ``est_tokens × (1 + headroom) ≤ max_input``.
  ``headroom`` is not a nicety — it is the correctness margin, absorbing the
  ``chars/4`` estimator error *and* reserving output/thinking room. No store, no
  I/O, trivially testable.
* :func:`check_dispatch` — the router hook: look up the model's window from the
  catalog (via the process store, cached), estimate the request's tokens, and
  return a refusal *reason* (or ``None`` to allow). **Ships dark**: no store
  bound, no card, or no known window ⇒ ``None`` (today's behaviour byte-for-
  byte). Reused by the standalone :func:`admit_context` for the context-assembly
  path, which can split/trim *before* forming a doomed request.

Refusal is a returned reason (folded into ``LlmResult.error`` by ``dispatch``,
the same shape as the budget ``gate_tier`` trip), **never a raised exception** —
so a pinned-model worker pass treats it as a failed call and applies its normal
backoff instead of spinning. A pass that wants to page on a *durable* oversize
calls :func:`raise_oversize_alert` (deduped, so a repeat can't flood).

The catalog window is read once into a short-TTL in-process cache, so the hot
path is a dict lookup after the first load (the catalog changes ~daily).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from precis.store import Store
    from precis.utils.llm.router import LlmRequest, Transport

log = logging.getLogger(__name__)

#: The working headroom fraction. 20% covers the ``chars/4`` estimator error +
#: output/thinking reservation. Load-bearing (it is the check's correctness
#: margin) — slice 3 calibrates it empirically from ``llm_call_log`` usage
#: echoes (estimated vs actual tokens) rather than arguing it a priori.
DEFAULT_HEADROOM = 0.2

#: Coarse chars→tokens constant. Precis has no tokenizer on the hot path, and
#: running one per call would defeat "costs nothing"; the estimate is absorbed
#: by :data:`DEFAULT_HEADROOM`. A caller with a real token count passes it.
_CHARS_PER_TOKEN = 4

_ALERT_SOURCE = "admit:oversize"


@dataclass(frozen=True, slots=True)
class Admission:
    """The verdict of a window fit-check."""

    fits: bool
    est_tokens: int
    limit: int
    headroom: float
    reason: str | None = None


def estimate_tokens(chars: int) -> int:
    """Coarse token estimate from a character count (``chars / 4``)."""
    return chars // _CHARS_PER_TOKEN


def admit(
    tokens: int, limit: int | None, *, headroom: float = DEFAULT_HEADROOM
) -> Admission:
    """Pure fit-check: does ``tokens`` (+ headroom) fit ``limit``?

    ``limit=None`` (an unknown window) always *admits* — the catalog can't
    refuse what it doesn't know, so the guardrail degrades to today's behaviour.
    The refusal ``reason`` carries the numbers so the error is actionable
    without forensics.
    """
    if limit is None or limit <= 0:
        return Admission(
            fits=True, est_tokens=tokens, limit=limit or 0, headroom=headroom
        )
    need = int(tokens * (1 + headroom))
    fits = need <= limit
    reason = None
    if not fits:
        reason = (
            f"context too large for the model window: est. {tokens:,} tokens "
            f"×(1+{headroom:.0%} headroom)={need:,} > {limit:,} max input. "
            "Split/trim the context or route to a wider model."
        )
    return Admission(
        fits=fits, est_tokens=tokens, limit=limit, headroom=headroom, reason=reason
    )


# ── catalog window lookup (short-TTL in-process cache) ──────────────────

#: {model_id -> card meta}. Rebuilt lazily past the TTL so the hot path is a
#: dict lookup, not a per-call query. The catalog changes ~daily (reconcile).
_CACHE: dict[str, dict[str, Any]] | None = None
_CACHE_AT: float = 0.0
_CACHE_TTL_S = 60.0


def reset_cache() -> None:
    """Drop the catalog cache (tests + after a known catalog write)."""
    global _CACHE, _CACHE_AT
    _CACHE = None
    _CACHE_AT = 0.0


def _catalog(store: Store) -> dict[str, dict[str, Any]]:
    global _CACHE, _CACHE_AT
    now = time.monotonic()
    if _CACHE is None or now - _CACHE_AT > _CACHE_TTL_S:
        cards = store.list_refs(kind="llm", limit=1000)
        built: dict[str, dict[str, Any]] = {}
        for c in cards:
            meta = c.meta or {}
            mid = meta.get("model_id")
            if mid:
                built[mid] = meta
        _CACHE = built
        _CACHE_AT = now
    return _CACHE


def window_for(meta: dict[str, Any], transport: str | None = None) -> int | None:
    """The max-input token window for a card, most-specific first.

    Prefers an offering matching ``transport`` that declares ``max_input``, then
    any offering's ``max_input``, then the **widest** reconciled endpoint window
    (a booking can pin the wide variant — gripe 162624), then the reconciled
    OpenRouter ``context_length`` (the slice-1 fact). ``None`` when the card
    knows no window — the admit degrades to allow. This is deliberately lenient
    (admit refuses only a *known* too-small window); the per-variant window a
    concrete booking actually gets is enforced at selection time.
    """
    offerings = meta.get("offerings") or []
    if transport is not None:
        for o in offerings:
            if (
                isinstance(o, dict)
                and o.get("transport") == transport
                and o.get("max_input")
            ):
                return int(o["max_input"])
    for o in offerings:
        if isinstance(o, dict) and o.get("max_input"):
            return int(o["max_input"])
    windows = [
        int(e["max_input"])
        for e in (meta.get("endpoints") or [])
        if isinstance(e, dict) and e.get("max_input")
    ]
    if windows:
        return max(windows)
    fo = meta.get("facts_openrouter") or {}
    if fo.get("context_length"):
        return int(fo["context_length"])
    return None


def _request_chars(req: LlmRequest) -> int:
    n = len(req.prompt or "")
    if req.messages:
        n += sum(len(str(m.get("content", ""))) for m in req.messages)
    if isinstance(req.system_prompt, str):
        n += len(req.system_prompt)
    return n


def _window_for_model(store: Store, model_id: str, transport: str | None) -> int | None:
    meta = _catalog(store).get(model_id)
    if meta is None:
        return None
    return window_for(meta, transport)


def check_dispatch(req: LlmRequest, *, model: str, transport: Transport) -> str | None:
    """Router hook: return a refusal *reason* for an oversized pairing, or ``None``.

    Ships dark: no process store, no card for ``model``, or no known window ⇒
    ``None`` (dispatch proceeds exactly as today). The token count is a
    ``chars/4`` estimate over the request's prompt + messages + string system
    prompt.
    """
    from precis.budget import meter

    store = meter.active_store()
    if store is None:
        return None
    try:
        limit = _window_for_model(store, model, transport.value)
    except Exception:  # pragma: no cover — a catalog read must never break dispatch
        log.warning("admit: catalog lookup failed for %s", model, exc_info=True)
        return None
    if limit is None:
        return None
    verdict = admit(estimate_tokens(_request_chars(req)), limit)
    return verdict.reason


def admit_context(
    store: Store,
    *,
    model_id: str,
    text: str,
    transport: str | None = None,
    real_tokens: int | None = None,
) -> Admission:
    """Standalone check for the context-assembly path — call *before* forming a
    request so a caller can split/trim rather than build a doomed blob.

    Pass ``real_tokens`` when a true count is known (an API ``usage`` echo, a
    prior turn); otherwise the ``chars/4`` estimate is used.
    """
    limit = _window_for_model(store, model_id, transport)
    tokens = (
        real_tokens if real_tokens is not None else estimate_tokens(len(text or ""))
    )
    return admit(tokens, limit)


def raise_oversize_alert(
    store: Store, *, model: str, source: str, adm: Admission
) -> None:
    """Raise a deduped ops alert for a *durable* oversize pairing (pass-level).

    Best-effort. Deduped on ``(model, source)`` via ``precis.alerts.raise_alert``,
    so a pass that keeps hitting the same oversize bumps one row's ``seen_count``
    rather than flooding ``ref_events`` — the fetcher/chase backoff discipline.
    A pass calls this when it decides the oversize is terminal / backed-off;
    ``dispatch`` itself only returns the reason (no alert on interactive paths).
    """
    try:
        from precis.alerts import raise_alert

        raise_alert(
            store,
            source=_ALERT_SOURCE,
            fingerprint=f"{model}:{source or '?'}",
            title=(
                f"context exceeds {model} window "
                f"({adm.est_tokens:,} est tokens > {adm.limit:,} max input)"
            ),
            detail=adm.reason or "",
            severity="warn",
        )
    except Exception:  # pragma: no cover — an alert must never break the caller
        log.warning("admit: oversize alert failed for %s", model, exc_info=True)


__all__ = [
    "DEFAULT_HEADROOM",
    "Admission",
    "admit",
    "admit_context",
    "check_dispatch",
    "estimate_tokens",
    "raise_oversize_alert",
    "reset_cache",
    "window_for",
]
