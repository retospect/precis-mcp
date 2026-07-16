"""LLM-catalog reconcile pass — keep model-card facts true + flag drift.

Slice 1 of the ``llm`` catalog (docs/proposals/llm-catalog.md). Reads the live
model feed (OpenRouter ``/api/v1/models`` — 344 models, no key, rich: context
window, per-token price, ``supported_parameters``) and refreshes each seeded
``llm`` card's facts, then flags **drift**: a card whose loopback-proxy offering
names a model the proxy doesn't actually serve (the ``claude-opus-4-8``-not-in-
proxy bug — any opus call *through the litellm proxy* 400s, and nothing noticed).
It is step 1 of the litellm teardown: the facts land in precis now, so when the
proxy dies the catalog already holds them.

Two guards keep it cheap + non-racy, copied from ``paper_reconcile``:

* **Cadence throttle** — an ``llm_reconcile:last_run`` marker in ``app_state``
  gates the pass to once per ``PRECIS_LLM_RECONCILE_REFRESH_HOURS`` (default 24).
* **Single-runner advisory lock** — a **transaction-scoped**
  ``pg_try_advisory_xact_lock`` held for the whole pass, so only one cluster node
  reconciles corpus-wide even if two clear the throttle in the same tick (a
  session lock is unsafe under pgbouncer transaction pooling — see
  ``paper_reconcile`` for the full reasoning).

Both live feeds are **injectable** (``models`` / ``proxy_models``) and both
**degrade to a no-op**: a fetch failure means "unknown", never a false alert. So
in a dev/test box with no network + no proxy the pass refreshes nothing and
raises nothing; a unit test injects the feeds to exercise refresh + drift.

Ships dark: registered default-OFF (``PRECIS_LLM_RECONCILE_ENABLED`` / ``--only
llm_reconcile``), and an empty catalog makes it a single cheap ``app_state`` read.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from typing import Any

import psycopg

from precis.alerts import raise_alert, resolve_stale_alerts
from precis.store import Store
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant, namespaced away from the ingest / paper_reconcile keys.
_LOCK_KEY = 0x6C_6C_6D_72_65_63_00_01 - 2**63  # "llmrec\x00\x01", mapped signed
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "llm_reconcile:last_run"
#: Alert source for the drift / dead-endpoint findings (deduped per fingerprint).
_ALERT_SOURCE = "llm_reconcile:drift"

#: The live model feed. No key needed; SSRF-guarded via safe_get.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

#: Offering transports that route through the loopback / OpenAI-compatible proxy —
#: the ones whose served-model set the drift check consults.
_PROXY_TRANSPORTS: frozenset[str] = frozenset({"litellm", "openai_compat"})


def _refresh_hours() -> float:
    """Minimum gap between reconcile passes.

    ``PRECIS_LLM_RECONCILE_REFRESH_HOURS`` (default 24.0, floor 0.1).
    """
    raw = os.environ.get("PRECIS_LLM_RECONCILE_REFRESH_HOURS")
    if not raw:
        return 24.0
    try:
        return max(0.1, float(raw))
    except ValueError:
        return 24.0


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    last = store.get_setting(_STATE_KEY)
    if not last:
        return True
    try:
        last_ts = datetime.fromisoformat(last)
    except ValueError:
        return True
    return datetime.now(UTC) - last_ts >= timedelta(hours=_refresh_hours())


def _norm_model_key(s: str) -> str:
    """Canonicalise a model id for matching across naming conventions.

    Drops the provider prefix and every non-alphanumeric, lower-cased — so
    ``anthropic/claude-opus-4.8`` (OpenRouter) and ``claude-opus-4-8`` (our
    ``_TIER_MODEL`` default) both fold to ``claudeopus48``. A ``-fast`` variant
    stays distinct (``claudeopus48fast``).
    """
    tail = (s or "").split("/")[-1].lower()
    return "".join(ch for ch in tail if ch.isalnum())


def _price_per_million(raw: Any) -> float | None:
    """OpenRouter prices are per-token USD strings; convert to per-1M USD."""
    if raw in (None, ""):
        return None
    try:
        return round(float(raw) * 1_000_000, 4)
    except (TypeError, ValueError):
        return None


def _facts_from_openrouter(m: dict[str, Any]) -> dict[str, Any]:
    """Extract the reconcile-relevant facts from one OpenRouter model object."""
    top = m.get("top_provider") or {}
    pricing = m.get("pricing") or {}
    return {
        "openrouter_id": m.get("id"),
        "context_length": m.get("context_length") or top.get("context_length"),
        "max_output": top.get("max_completion_tokens"),
        "price_in": _price_per_million(pricing.get("prompt")),
        "price_out": _price_per_million(pricing.get("completion")),
        "supported_parameters": m.get("supported_parameters"),
    }


def fetch_openrouter_models(
    *, timeout: float = 20.0
) -> dict[str, dict[str, Any]] | None:
    """Fetch the OpenRouter model feed, keyed by :func:`_norm_model_key`.

    Best-effort: returns ``None`` on any failure (network, non-200, bad JSON) so
    the caller degrades to "unknown" rather than clobbering good facts.
    """
    import httpx

    from precis.utils.safe_fetch import safe_get

    try:
        with httpx.Client(follow_redirects=False, timeout=timeout) as client:
            resp = safe_get(client, OPENROUTER_MODELS_URL)
            resp.raise_for_status()
            data = resp.json().get("data", [])
    except Exception:  # pragma: no cover — network/parse variance
        log.warning("llm_reconcile: OpenRouter fetch failed", exc_info=True)
        return None
    out: dict[str, dict[str, Any]] = {}
    for m in data:
        key = _norm_model_key(m.get("id") or m.get("canonical_slug") or "")
        if key:
            out.setdefault(key, m)
    return out


def _drift_fingerprint(model_id: str) -> str:
    return f"proxy-missing:{model_id}"


def run_llm_reconcile_pass(
    store: Store,
    *,
    models: dict[str, dict[str, Any]] | None = None,
    proxy_models: set[str] | None = None,
    force: bool = False,
    _fetch: bool = True,
) -> BatchResult:
    """Refresh ``llm`` card facts from the live feed + flag drift, if due.

    ``models`` — the OpenRouter feed (keyed by :func:`_norm_model_key`); fetched
    live when ``None`` and ``_fetch`` is set. ``proxy_models`` — the set of model
    ids the loopback proxy actually serves (normalised keys); ``None`` = unknown
    (drift check skipped, no false alerts). Both are injected by tests.

    ``claimed``/``ok`` count cards refreshed + drift findings raised this pass.
    """
    idle = BatchResult(handler="llm_reconcile", claimed=0, ok=0, failed=0)
    if not store.dsn or (not force and not _due(store)):
        return idle
    dsn = store.dsn

    conn = psycopg.connect(dsn)
    try:
        with conn.transaction():
            row = conn.execute(
                "SELECT pg_try_advisory_xact_lock(%s)", (_LOCK_KEY,)
            ).fetchone()
            if not (row and row[0]):
                return idle  # another node owns the sweep this cycle

            cards = store.list_refs(kind="llm", limit=1000)
            if not cards:
                store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
                return idle

            if models is None and _fetch:
                models = fetch_openrouter_models()

            now_iso = datetime.now(UTC).isoformat()
            refreshed = 0
            live_drift: set[str] = set()

            for card in cards:
                meta = card.meta or {}
                model_id = meta.get("model_id")
                if not model_id:
                    continue
                key = _norm_model_key(model_id)

                # (A) refresh authoritative facts from the live feed.
                if models is not None:
                    m = models.get(key)
                    if m is not None:
                        store.update_ref(
                            card.id,
                            meta_patch={
                                "facts_openrouter": _facts_from_openrouter(m),
                                "reconciled_at": now_iso,
                            },
                        )
                        refreshed += 1

                # (B) drift: a proxy-routed offering for a model the proxy can't
                # serve. Only assert absence when proxy_models is authoritative.
                if proxy_models is not None and _has_proxy_offering(meta):
                    if key not in proxy_models:
                        fp = _drift_fingerprint(model_id)
                        live_drift.add(fp)
                        raise_alert(
                            store,
                            source=_ALERT_SOURCE,
                            fingerprint=fp,
                            title=(
                                f"model {model_id!r} routes through the proxy but "
                                "the proxy does not serve it"
                            ),
                            detail=(
                                f"an offering for {model_id!r} names a "
                                "litellm/openai-compat transport, but that id is "
                                "absent from the proxy's served set — calls 400. "
                                "Fix the offering's model id or expose it on the "
                                "proxy."
                            ),
                            severity="warn",
                            subject_ref_id=card.id,
                        )

            # Clear any drift that has since been fixed (only when the proxy set
            # was authoritative this pass — else we can't know it cleared).
            if proxy_models is not None:
                resolve_stale_alerts(
                    store, source=_ALERT_SOURCE, live_fingerprints=live_drift
                )

            store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
            work = refreshed + len(live_drift)
            if work:
                log.info(
                    "llm_reconcile: refreshed %d card(s), flagged %d drift finding(s)",
                    refreshed,
                    len(live_drift),
                )
            return BatchResult(handler="llm_reconcile", claimed=work, ok=work, failed=0)
    finally:
        conn.close()


def _has_proxy_offering(meta: dict[str, Any]) -> bool:
    """True if any offering routes through the loopback / OpenAI-compat proxy."""
    for o in meta.get("offerings") or []:
        if isinstance(o, dict) and o.get("transport") in _PROXY_TRANSPORTS:
            return True
    return False


__all__ = ["fetch_openrouter_models", "run_llm_reconcile_pass"]
