"""LLM-catalog reconcile pass — keep model-card facts true + flag drift.

Slice 1 of the ``llm`` catalog (``llm-catalog`` (git-only)). Reads the live
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

Step (D), S9: the same pass also refreshes the ``devin`` ``llm_account``
row's ``entitlement`` from Devin's AI Gateway models list (``GET
/v3beta1/ai-gateway/models`` — per-model ``id``/``members`` (model UIDs)
and the account-effective ``price``; a model's mere presence in the
response IS "allowed" for this account, per the endpoint's own
description). A model whose price changed since the last fetch gets
``stale_since`` stamped on its entitlement entry; a model that dropped
out of the response is kept with ``allowed: false`` rather than deleted
(the account may regain it). ``promo_expired`` is set when the account's
``promo_expiry`` column has passed. Account-level, independent of the
``llm`` catalog, so it runs even on an empty catalog; degrades the same
way as the card feeds — no token or a failed fetch leaves the stored
entitlement untouched.

The refresh MERGES into the stored entitlement: it writes only
``models`` / ``fetched_at`` / ``fetched_from`` / ``promo_expired``, so the
curated ``source`` / ``note`` migration 0280 seeds, and any key an owner
adds, survive it. The response is shape-checked: a body that is not
``{"models": [...]}`` leaves the entitlement untouched (a missing list must
never read as "the account may call nothing"), an entry without a string
``id`` or with a non-numeric ``price`` is skipped with a WARNING (its stored
state is carried forward, not flipped to ``allowed: false``), and the whole
leg is fenced so nothing in it can stop the card legs or the throttle stamp.

Runs on default_profiles (every system-worker node, every cycle, subject
to the usual throttle + ``service_config`` run control) — the §L control
cutover (``registry.ServiceSpec.default_profiles``) is what actually
turns the pass on; ``PRECIS_LLM_RECONCILE_ENABLED`` only seeds the
deploy-time ``service_config`` row's starting state.
"""

from __future__ import annotations

import logging
import math
import reprlib
from datetime import UTC, datetime
from typing import Any

import psycopg

from precis.alert_ids import RULES_BY_ID
from precis.alerts import raise_alert, resolve_stale_alerts
from precis.llm_catalog import synced_offerings
from precis.store import Store
from precis.workers import _throttle
from precis.workers.runner import BatchResult

log = logging.getLogger(__name__)

#: Fixed signed-bigint key for the single-runner advisory lock. Arbitrary
#: constant, namespaced away from the ingest / paper_reconcile keys.
_LOCK_KEY = 0x6C_6C_6D_72_65_63_00_01 - 2**63  # "llmrec\x00\x01", mapped signed
#: app_state key holding the ISO-8601 timestamp of the last completed pass.
_STATE_KEY = "llm_reconcile:last_run"
#: Env var + default for the cadence throttle (see :func:`_throttle.due`).
_REFRESH_ENV_VAR = "PRECIS_LLM_RECONCILE_REFRESH_HOURS"
_DEFAULT_REFRESH_HOURS = 24.0
#: Alert source for the drift / dead-endpoint findings (deduped per fingerprint).
_ALERT_SOURCE = RULES_BY_ID["llm_reconcile:drift/proxy-missing:{model_id}"].rule

#: The live model feed. No key needed; SSRF-guarded via safe_get.
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

#: Per-slug endpoint feed: the ~28 bookable provider×quant variants of one model
#: (gripe 162624). ``{slug}`` is the OpenRouter model id (``z-ai/glm-5.2``).
OPENROUTER_ENDPOINTS_URL_TMPL = "https://openrouter.ai/api/v1/models/{slug}/endpoints"

#: Devin's AI Gateway model-price list — "List the models the account can
#: call through the AI Gateway with the price the account pays" (the
#: endpoint's own description). A model family whose members share one
#: price is one entry (``members`` = that family's model UIDs); presence
#: in the response IS "allowed" for the account — there is no separate
#: allow-list field. Bearer-token auth (``DEVIN_API_TOKEN``).
DEVIN_AI_GATEWAY_MODELS_URL = "https://api.devin.ai/v3beta1/ai-gateway/models"

#: The one ``llm_account`` row this pass refreshes entitlement for.
#: Hardcoded, not "every account with an API token": the AI Gateway
#: models endpoint is Devin's own surface (migration 0280's ``devin``
#: row), not a generic per-vendor contract other accounts could answer.
_DEVIN_ACCOUNT_NAME = "devin"

#: What the ``fetched_from`` entitlement key records. Not ``source``: that key
#: is the curated (migration 0280 / owner-edited) provenance of the row and
#: the refresh never writes it.
_DEVIN_FETCHED_FROM = "devin ai-gateway models API"

#: The per-token price fields an entitlement entry carries.
_DEVIN_PRICE_FIELDS = ("input", "output", "cache_write", "cache_read")

#: supported_parameters values that satisfy a "structured output" flag (mirrors
#: the policy's ``_STRUCTURED_PARAMS`` — kept local to avoid the import).
_STRUCTURED_PARAMS: frozenset[str] = frozenset(
    {"structured_outputs", "response_format"}
)

#: Offering transports that route through the loopback / OpenAI-compatible proxy —
#: the ones whose served-model set the drift check consults.
_PROXY_TRANSPORTS: frozenset[str] = frozenset({"local", "openai_compat"})


def _due(store: Store) -> bool:
    """True when the throttle window has elapsed since the last pass."""
    return _throttle.due(store, _STATE_KEY, _REFRESH_ENV_VAR, _DEFAULT_REFRESH_HOURS)


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
    from precis.utils.http import http_client, polite_user_agent, with_backoff
    from precis.utils.safe_fetch import safe_get

    try:
        with http_client(timeout=timeout, user_agent=polite_user_agent()) as client:
            # Politeness core on the host:openrouter.ai row: backoff on
            # 429/5xx honouring Retry-After; exhaustion lands in the
            # best-effort ``None`` below.
            resp = with_backoff(
                lambda: safe_get(client, OPENROUTER_MODELS_URL),
                service="openrouter",
                url=OPENROUTER_MODELS_URL,
            )
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


def _endpoint_from_openrouter(e: dict[str, Any]) -> dict[str, Any]:
    """Distil one OpenRouter endpoint object into a bookable-variant dict
    (:data:`precis.llm_catalog.ENDPOINT_KEYS`). Prices → per-1M USD; the
    ``supported_parameters`` list collapses to the tools/structured booleans a
    requirement filters on. ``status`` (0 = live) + ``uptime_1d`` are the
    availability telemetry a booking consults."""
    pricing = e.get("pricing") or {}
    sp = set(e.get("supported_parameters") or [])
    return {
        "provider": e.get("provider_name"),
        "quant": e.get("quantization"),
        "tag": e.get("tag"),
        "max_input": e.get("context_length") or e.get("max_prompt_tokens"),
        "max_output": e.get("max_completion_tokens"),
        "price_in": _price_per_million(pricing.get("prompt")),
        "price_out": _price_per_million(pricing.get("completion")),
        "tools": "tools" in sp,
        "structured": bool(_STRUCTURED_PARAMS & sp),
        "status": e.get("status"),
        "uptime_1d": e.get("uptime_last_1d"),
    }


def fetch_openrouter_endpoints(
    slug: str, *, timeout: float = 20.0
) -> list[dict[str, Any]] | None:
    """Fetch the bookable endpoints for one OpenRouter slug, distilled.

    Best-effort: ``None`` on any failure so the caller keeps the card's prior
    endpoints rather than blanking them. An empty list (a real "no endpoints")
    is distinct from ``None`` (a fetch failure).
    """
    from precis.utils.http import http_client, polite_user_agent, with_backoff
    from precis.utils.safe_fetch import safe_get

    url = OPENROUTER_ENDPOINTS_URL_TMPL.format(slug=slug)
    try:
        with http_client(timeout=timeout, user_agent=polite_user_agent()) as client:
            resp = with_backoff(
                lambda: safe_get(client, url), service="openrouter", url=url
            )
            resp.raise_for_status()
            data = resp.json().get("data") or {}
    except Exception:  # pragma: no cover — network/parse variance
        log.warning("llm_reconcile: endpoints fetch failed for %s", slug, exc_info=True)
        return None
    eps = data.get("endpoints") or []
    return [_endpoint_from_openrouter(e) for e in eps if e.get("provider_name")]


def _models_from_devin_body(body: Any) -> list[Any] | None:
    """The ``models`` list out of a decoded AI Gateway response, or ``None``
    with one WARNING when the body is not ``{"models": [...]}``.

    A renamed or missing key must read as "unknown", never as ``[]``: an empty
    list means "the account may call nothing" downstream and would flip every
    stored model to ``allowed: false``.
    """
    if isinstance(body, dict) and isinstance(body.get("models"), list):
        return body["models"]
    got = (
        f"an object with keys {sorted(body)[:8]}"
        if isinstance(body, dict)
        else f"a {type(body).__name__}"
    )
    log.warning(
        "llm_reconcile: Devin AI Gateway models response has no 'models' list "
        "(got %s) — entitlement left untouched",
        got,
    )
    return None


def fetch_devin_entitlement(*, timeout: float = 20.0) -> list[Any] | None:
    """Fetch Devin's AI Gateway model-price list, raw (``response['models']``).

    Best-effort: ``None`` when ``DEVIN_API_TOKEN`` is unset, the call fails
    (auth, network, non-200, bad JSON) or the body is not ``{"models": [...]}``
    — the caller leaves the stored entitlement untouched rather than
    clobbering it on a transient outage or a changed response shape.
    """
    from precis import secrets as _secrets
    from precis.utils.http import http_client, polite_user_agent, with_backoff

    token = (_secrets.get_secret("DEVIN_API_TOKEN") or "").strip()
    if not token:
        return None
    try:
        # Same transport as executors/devin_api.py: the token is a CLIENT
        # default header on a client that does not follow redirects, sent with a
        # plain ``client.get``. Not ``safe_get`` — it walks the redirect chain
        # itself and replays per-call headers on every hop, which would hand the
        # bearer token to wherever a 3xx pointed. A 3xx here is a failed fetch
        # (``raise_for_status``), never a second request. The pinning transport
        # still SSRF-classifies the connection.
        with http_client(
            timeout=timeout,
            user_agent=polite_user_agent(),
            headers={"Authorization": f"Bearer {token}"},
        ) as client:
            resp = with_backoff(
                lambda: client.get(DEVIN_AI_GATEWAY_MODELS_URL),
                service="devin",
                url=DEVIN_AI_GATEWAY_MODELS_URL,
            )
            resp.raise_for_status()
            body = resp.json()
    except Exception:  # pragma: no cover — network/parse variance
        log.warning(
            "llm_reconcile: Devin AI Gateway models fetch failed", exc_info=True
        )
        return None
    return _models_from_devin_body(body)


def _price_figure_ok(v: Any) -> bool:
    """One price figure: absent (``None``), a finite number, or a decimal
    string (the gateway may quote per-token USD as strings, the way
    OpenRouter's pricing block does)."""
    if v is None:
        return True
    if isinstance(v, bool) or not isinstance(v, (int, float, str)):
        return False
    try:
        return math.isfinite(float(v))
    except (ValueError, OverflowError):
        return False


def _shape_devin_price(raw: Any) -> Any:
    """One entry's ``price`` as stored: the four known figures (all ``None``
    when the entry carries no price), or a bare number carried as given.
    ``ValueError`` on anything else — a non-numeric figure, or a price that is
    neither a number nor an object."""
    if raw is None:
        raw = {}
    if isinstance(raw, dict):
        shaped = {k: raw.get(k) for k in _DEVIN_PRICE_FIELDS}
        for k, v in shaped.items():
            if not _price_figure_ok(v):
                raise ValueError(f"price.{k} is not a number: {reprlib.repr(v)}")
        return shaped
    if _price_figure_ok(raw):
        return raw
    raise ValueError(f"price is neither a number nor an object: {reprlib.repr(raw)}")


def _devin_entitlement_from_models(
    models: list[Any],
    *,
    now_iso: str,
    prev: dict[str, Any] | None,
) -> tuple[dict[str, Any], list[str]]:
    """Merge the AI Gateway response into the ``llm_account.entitlement``
    jsonb, diffing against ``prev`` (the account's current entitlement) to
    find a changed price. Returns ``(entitlement, stale_model_ids)``.

    The result is a copy of ``prev`` with ``models`` / ``fetched_at`` /
    ``fetched_from`` replaced — every other key (the curated ``source`` and
    ``note`` migration 0280 seeds, anything an owner added) is carried through.

    Keyed by the response's ``id`` (a model family or a single model —
    ``members`` holds that family's model UIDs when it's a family). A model
    present before but absent from this fetch is kept with ``allowed:
    False`` rather than dropped — the account may regain it; deleting would
    also discard its price history for no reason. An entry that fails the
    shape check (not an object, no string ``id``, a non-numeric ``price``) is
    skipped with a WARNING and — unlike an absent model — keeps its stored
    state: a malformed row says nothing about what the account may call.
    """
    entitlement = dict(prev) if isinstance(prev, dict) else {}
    stored = entitlement.get("models")
    prev_models = dict(stored) if isinstance(stored, dict) else {}
    stale: list[str] = []
    out: dict[str, Any] = {}
    skipped: set[str] = set()
    for m in models:
        if not isinstance(m, dict):
            log.warning(
                "llm_reconcile: Devin AI Gateway entry skipped — not an object: %s",
                reprlib.repr(m),
            )
            continue
        mid = m.get("id")
        if not isinstance(mid, str) or not mid:
            log.warning(
                "llm_reconcile: Devin AI Gateway entry skipped — id is not a "
                "non-empty string: %s",
                reprlib.repr(mid),
            )
            continue
        try:
            price = _shape_devin_price(m.get("price"))
        except ValueError as exc:
            skipped.add(mid)
            log.warning(
                "llm_reconcile: Devin AI Gateway entry %r skipped — %s", mid, exc
            )
            continue
        members = m.get("members")
        entry: dict[str, Any] = {
            "name": m.get("name"),
            "members": (
                [u for u in members if isinstance(u, str)]
                if isinstance(members, list)
                else []
            ),
            "price": price,
            "allowed": True,
            "seen_at": now_iso,
        }
        prev_entry = prev_models.get(mid)
        if isinstance(prev_entry, dict) and prev_entry.get("price") != entry["price"]:
            entry["stale_since"] = now_iso
            stale.append(mid)
        out[mid] = entry
    for mid, prev_entry in prev_models.items():
        if mid in out or not isinstance(prev_entry, dict):
            continue
        carried = dict(prev_entry)
        if mid not in skipped:
            carried["allowed"] = False
            carried["seen_at"] = now_iso
        out[mid] = carried
    entitlement["models"] = out
    entitlement["fetched_at"] = now_iso
    entitlement["fetched_from"] = _DEVIN_FETCHED_FROM
    return entitlement, stale


def _reconcile_devin_entitlement(
    conn: Any,
    *,
    devin_models: list[Any] | None,
    now: datetime,
) -> tuple[int, int]:
    """Refresh the ``devin`` ``llm_account`` row's entitlement from the AI
    Gateway feed + check its ``promo_expiry`` column. Returns ``(accounts
    updated, stale price count)`` — ``(0, 0)`` when the feed is ``None``
    (no token / fetch failed: a no-op, never clobbering good data) or the
    seeded row is absent.
    """
    from psycopg.types.json import Jsonb

    if devin_models is None:
        return 0, 0
    row = conn.execute(
        "SELECT entitlement, promo_expiry FROM llm_account WHERE name = %s",
        (_DEVIN_ACCOUNT_NAME,),
    ).fetchone()
    if row is None:
        return 0, 0
    prev_entitlement, promo_expiry = row[0], row[1]
    now_iso = now.isoformat()
    entitlement, stale = _devin_entitlement_from_models(
        devin_models, now_iso=now_iso, prev=prev_entitlement
    )
    # The flag is derived each refresh: merging into the old dict would
    # otherwise keep a ``promo_expired`` set before the expiry was extended.
    if promo_expiry is not None and promo_expiry < now.date():
        entitlement["promo_expired"] = True
    else:
        entitlement.pop("promo_expired", None)
    conn.execute(
        "UPDATE llm_account SET entitlement = %s, updated_at = now() WHERE name = %s",
        (Jsonb(entitlement), _DEVIN_ACCOUNT_NAME),
    )
    return 1, len(stale)


def _refresh_devin_entitlement(
    conn: Any,
    *,
    devin_models: list[Any] | None,
    fetch: bool,
    now: datetime,
) -> tuple[int, int]:
    """The pass's devin leg behind a fence: fetch (when not injected and
    ``fetch`` is set) + :func:`_reconcile_devin_entitlement`. Nothing in here
    escapes — a failure is a WARNING and ``(0, 0)`` with the stored
    entitlement untouched, so it can never stop the card legs or the throttle
    stamp that follow it (a leg that raised every cycle used to be able to
    starve both)."""
    try:
        if devin_models is None and fetch:
            devin_models = fetch_devin_entitlement()
        return _reconcile_devin_entitlement(conn, devin_models=devin_models, now=now)
    except Exception:
        log.warning(
            "llm_reconcile: devin entitlement refresh failed — stored "
            "entitlement left untouched",
            exc_info=True,
        )
        return 0, 0


def _drift_fingerprint(model_id: str) -> str:
    return f"proxy-missing:{model_id}"


def _iter_served_by(meta: dict[str, Any]) -> Any:
    """Yield every ``served_by`` entry declared on a card — both the card-level
    ``meta.served_by`` and each offering's ``served_by`` (§6's example nests it
    under the local-serving offering). Each entry is a dict with at least a
    ``host``; ``max_parallel`` is the slot capacity (default 1)."""
    for e in meta.get("served_by") or []:
        if isinstance(e, dict):
            yield e
    for o in meta.get("offerings") or []:
        if isinstance(o, dict):
            for e in o.get("served_by") or []:
                if isinstance(e, dict):
                    yield e


def llm_served_slots_from_cards(cards: list[Any]) -> dict[tuple[str, str], int]:
    """Build the desired ``(host, "llm:<model_id>") → max_parallel`` map from the
    ``served_by`` declarations across every ``llm`` card (slice 7 / §6). This is
    the *declared* local-serving capacity that seeds ``resource_slots`` — the
    dial the capability-gated inline-LLM passes reserve against. Purely from card
    facts (no network). If the same (host, model) is declared twice the larger
    capacity wins."""
    desired: dict[tuple[str, str], int] = {}
    for card in cards:
        meta = card.meta or {}
        model_id = meta.get("model_id")
        if not model_id:
            continue
        resource = f"llm:{model_id}"
        for entry in _iter_served_by(meta):
            host = entry.get("host")
            if not host:
                continue
            raw = entry.get("max_parallel", 1)
            try:
                cap = max(1, int(raw))
            except (TypeError, ValueError):
                cap = 1
            key = (str(host), resource)
            desired[key] = max(desired.get(key, 0), cap)
    return desired


def run_llm_reconcile_pass(
    store: Store,
    *,
    models: dict[str, dict[str, Any]] | None = None,
    proxy_models: set[str] | None = None,
    endpoints_by_key: dict[str, list[dict[str, Any]]] | None = None,
    devin_models: list[Any] | None = None,
    force: bool = False,
    _fetch: bool = True,
) -> BatchResult:
    """Refresh ``llm`` card facts from the live feed + flag drift, if due.

    ``models`` — the OpenRouter feed (keyed by :func:`_norm_model_key`); fetched
    live when ``None`` and ``_fetch`` is set. ``proxy_models`` — the set of model
    ids the loopback proxy actually serves (normalised keys); ``None`` = unknown
    (drift check skipped, no false alerts). ``endpoints_by_key`` — pre-fetched
    per-slug bookable variants (keyed by :func:`_norm_model_key`) for tests; when
    ``None`` and ``_fetch`` is set, each matched card's endpoints are fetched live
    from ``/models/{slug}/endpoints`` (gripe 162624). ``devin_models`` — the raw
    AI Gateway models list for :func:`_reconcile_devin_entitlement`; fetched live
    via :func:`fetch_devin_entitlement` when ``None`` and ``_fetch`` is set. All
    four are injected by tests and degrade to a no-op on a fetch failure.

    ``claimed``/``ok`` count cards refreshed + drift findings raised + the devin
    entitlement refresh (account + stale-price counts) this pass.
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

            # (S) Seed resource_slots from the catalog's declared local serving
            # (slice 7 / §6): served_by.max_parallel → llm:<model> hard slots.
            # Runs on every locked pass (declared facts, no network) and BEFORE
            # the empty-catalog return so an emptied catalog reaps its stale
            # llm: rows. DARK: nothing reserves these slots until the inline-LLM
            # passes are capability-gated; local-transport routing is untouched.
            served = llm_served_slots_from_cards(cards)
            slots_up, slots_del = store.reconcile_llm_served_slots(served)

            # (D) devin entitlement (S9, td568212): account-level, not
            # card-level — runs even on an empty catalog. Fenced: a failure
            # here is logged and skipped, never a reason to skip the legs
            # below or the throttle stamp.
            accounts_updated, stale_prices = _refresh_devin_entitlement(
                conn, devin_models=devin_models, fetch=_fetch, now=datetime.now(UTC)
            )

            if not cards:
                store.set_setting(_STATE_KEY, datetime.now(UTC).isoformat())
                entitlement_work = accounts_updated + stale_prices
                if not entitlement_work:
                    return idle
                return BatchResult(
                    handler="llm_reconcile",
                    claimed=entitlement_work,
                    ok=entitlement_work,
                    failed=0,
                )

            if models is None and _fetch:
                models = fetch_openrouter_models()

            now_iso = datetime.now(UTC).isoformat()
            refreshed = 0
            endpoints_reconciled = 0
            live_drift: set[str] = set()

            for card in cards:
                meta = card.meta or {}
                model_id = meta.get("model_id")
                if not model_id:
                    continue
                key = _norm_model_key(model_id)

                # (A) refresh authoritative facts from the live feed.
                m = models.get(key) if models is not None else None
                if m is not None:
                    store.update_ref(
                        card.id,
                        meta_patch={
                            "facts_openrouter": _facts_from_openrouter(m),
                            "reconciled_at": now_iso,
                        },
                    )
                    refreshed += 1

                # (A2) refresh the bookable-variant endpoints (gripe 162624): one
                # per provider×quant×window×price, so capability + price become
                # variant-precise. Only for a card present in the live feed (a
                # matched slug); a fetch failure (``None``) keeps the prior
                # endpoints rather than blanking them.
                eps: list[dict[str, Any]] | None = None
                if endpoints_by_key is not None:
                    eps = endpoints_by_key.get(key)
                elif _fetch and m is not None:
                    eps = fetch_openrouter_endpoints(m.get("id") or model_id)
                if eps is not None:
                    store.update_ref(
                        card.id,
                        meta_patch={
                            "endpoints": eps,
                            "endpoints_reconciled_at": now_iso,
                        },
                    )
                    endpoints_reconciled += 1

                # (A3) resync the derived local offering to the card-level
                # served_by (gr502698): a card whose offerings were rewritten
                # cloud-only (a seed, a manual put) gets its ``transport:
                # 'local'`` mirror back on the next pass instead of staying
                # cloud-catalogued while fleet-served.
                synced = synced_offerings(
                    meta.get("offerings"),
                    meta.get("served_by"),
                    prev_served_by=meta.get("served_by"),
                )
                if synced != (meta.get("offerings") or []):
                    store.update_ref(card.id, meta_patch={"offerings": synced})
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
                                "local/openai-compat transport, but that id is "
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
            work = (
                refreshed
                + endpoints_reconciled
                + len(live_drift)
                + slots_up
                + slots_del
                + accounts_updated
                + stale_prices
            )
            if work:
                log.info(
                    "llm_reconcile: refreshed %d card(s), %d endpoint set(s), "
                    "flagged %d drift finding(s), seeded %d llm slot(s) "
                    "(%d reaped), refreshed %d devin account(s) "
                    "(%d price(s) gone stale)",
                    refreshed,
                    endpoints_reconciled,
                    len(live_drift),
                    slots_up,
                    slots_del,
                    accounts_updated,
                    stale_prices,
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


__all__ = [
    "DEVIN_AI_GATEWAY_MODELS_URL",
    "fetch_devin_entitlement",
    "fetch_openrouter_endpoints",
    "fetch_openrouter_models",
    "llm_served_slots_from_cards",
    "run_llm_reconcile_pass",
]
