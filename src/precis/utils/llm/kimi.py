"""Kimi Code subscription adapter (gr543829) — **ships dark**.

Direct OpenAI-compatible HTTP access to the Kimi Code membership, API-key
auth (``KIMI_CODE_API_KEY``), no binary — the one sanctioned direct route:
Kimi's docs issue API keys so members can use their benefits in
third-party development tools (``kimi.com/code/docs``). Facts below were
re-read from the live docs on 2026-11-01 (Overview, Membership Benefits,
Community Guidelines, Error Reference).

Conservative rules, per those docs:

* **Real identity** — "Tampering with the client identifier (User-Agent)
  is considered a violation and may result in suspension of membership
  benefits." Every hosted wire already sends
  :func:`~precis.utils.http.polite_user_agent` (``precis-mcp/<ver>
  (+<url>; mailto:<contact>)``) via
  :func:`precis.utils.llm.openai_tools._post_open`; the adapter never
  overrides it and never impersonates the Kimi CLI.
* **Key auth only** — ``api_key_ref: KIMI_CODE_API_KEY`` resolved at call
  time; never the CLI's OAuth login (the docs scope that to official
  clients).
* **Autonomous use sanctioned; key auth only** — the account owner's
  2026-11-07 ruling (me573727, td573681 comment 4) lifted the 2026-11-01
  interactive-only guard (gr543829): a lit kimi rung now serves
  worker/job-origin calls like any other subscription profile — the
  ``ops`` allowlist, bulk guard and window cap still gate
  (:func:`~precis.utils.llm.router._subscription_refusal`), and the
  shipped profile carries ``ops`` absent (no op gate). The Community
  Guidelines limit subscriptions to *interactive* use
  ("scripted batch execution or data annotation pipelines … goes beyond
  normal use") and send product-integration/enterprise traffic to the
  pay-as-you-go Kimi Platform — the ruling accepts that on the owner's
  account; bulk drains stay refused regardless. The unattended
  ``kimi_tree`` slice lane runs the CLI headless on the membership key —
  key auth only, the CLI's own User-Agent and defaults untouched.
* **Quota** — a rolling 5-hour rate window plus a monthly total (new
  plans; legacy plans keep a 7-day refresh), shared across every device
  and API key. Account-side limits surface as HTTP 429 with documented
  wording (:func:`classify_failure`); the concurrent-request limit is a
  *risk-control* signal, not quota.

**Ships dark**: the profile carries ``"gate": "llm_kimi"`` — the
``service_config`` service an operator lights (``prio > 0``, the
``citer_watch`` gate shape). Until it is lit the rung is dropped by the
host-availability check and refused outright on a pinned dispatch, so
writing the profile and key alone never sends a request. Enabling is an
operator act: store ``KIMI_CODE_API_KEY`` (the secrets page entry landed
in e83bdaefc), write the ``llm.profiles`` entry
(:func:`default_profile`), add a chain rung naming the profile, then
``precis service prio '*' llm_kimi 1``. Once lit the profile serves:
``ops`` is absent (no op gate) and worker-origin calls are admitted —
the bulk guard and window cap are the remaining checks.

Wire differences from OpenRouter (:func:`wire_routing`): the Kimi
endpoint takes a flat ``reasoning_effort`` field (levels
``low``/``high``/``max``), not OpenRouter's ``reasoning:{}`` block, and
``k3.1-pro`` rejects thinking-disabled requests outright — so the SMALL
``reasoning.enabled: false`` default is never sent on this wire; with
nothing sent the model uses its own default (``max`` for k3.1-pro).

Probes: the ``KIMI_CODE_API_KEY`` probe is manual-only — the operator's
"Check now" on /secrets (and ``precis secret check``), never the
periodic job while the lane is disabled; storing or changing the key
does not probe.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
from dataclasses import replace as _replace
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.error import HTTPError

from precis.errors import BadInput
from precis.utils.http import retry_after_seconds
from precis.utils.llm.failure import Classified

log = logging.getLogger(__name__)

#: The ``adapter`` value a ``llm.profiles`` entry sets to get this wire.
ADAPTER = "kimi"

#: The ``service_config`` service an operator lights (``prio > 0``) to
#: arm the adapter — the dark gate. A profile carries ``"gate"`` naming
#: it; the router checks it in the host-availability pass and again at
#: dispatch, so a dark profile can never send a request.
SERVICE_NAME = "llm_kimi"

#: The profile name :func:`default_profile` recommends — a chain rung's
#: ``"profile"`` key and the ``llm.profiles`` entry name.
PROFILE_NAME = "kimi"

#: Vault/env secret the membership key resolves from (the secrets-page
#: entry landed in e83bdaefc).
API_KEY_REF = "KIMI_CODE_API_KEY"

#: The documented OpenAI-compatible bases (chat completions + responses).
#: Overseas is the default — the account's region decides; an operator
#: overrides via the profile's ``base_url``.
BASE_URL_OVERSEAS = "https://api.kimi.ai/coding/v1"
BASE_URL_CHINA = "https://api.kimi.com/coding/v1"
DEFAULT_BASE_URL = BASE_URL_OVERSEAS

#: The only hosts ``KIMI_CODE_API_KEY`` may be Bearer'd to — the two
#: documented membership endpoints, a code constant, not config: an
#: ``llm.profiles`` write repointing ``base_url`` must never redirect
#: the key to a host the code does not name (gr586613).
#: :func:`base_url` feeds the ``kimi_tree`` launch env and the secrets
#: probe, both of which Bearer-auth whatever it resolves to, and the
#: router enforces it in
#: :func:`~precis.utils.llm.router._profile_base_url_refusal` before the
#: key resolves; widening is a reviewed land, never a DB write.
BASE_URL_HOSTS: frozenset[str] = frozenset({"api.kimi.ai", "api.kimi.com"})

#: The model ids the Kimi Code endpoint accepts (docs, 2026-11-01 — six
#: ids; the gripe named the first four). ``kimi-for-coding`` is the K2.8
#: Preview coding model; the ``-highspeed`` variants bill ~2× quota for
#: ~2–3× output speed and gate on the top membership tier.
MODELS: tuple[str, ...] = (
    "kimi-for-coding",
    "k3.1-pro",
    "k3",
    "k3-256k",
    "k3.1-pro-highspeed",
    "kimi-for-coding-highspeed",
)

#: Models that reject thinking-disabled requests (docs: k3.1-pro —
#: "Thinking is required, with `max` as the default"). The highspeed
#: sibling is included conservatively: same K3.1 Pro series, docs do not
#: carve it out. The kimi wire never emits a thinking-disable anyway —
#: this set is informational/for the catalog.
THINKING_REQUIRED_MODELS: frozenset[str] = frozenset({"k3.1-pro", "k3.1-pro-highspeed"})

#: The ``req.source`` ops the shipped profile allowlists — **absent**
#: since the 2026-11-07 ruling (me573727, td573681 comment 4) lifted the
#: 2026-11-01 interactive-only guard (gr543829): the default profile
#: carries ``ops: None`` — no op allowlist at all. An operator may still
#: write an explicit ``ops`` list to narrow what the rung serves; the
#: bulk guard and window cap gate regardless
#: (:func:`~precis.utils.llm.router._subscription_refusal`).
DEFAULT_OPS: tuple[str, ...] | None = None

#: Our ``effort`` vocabulary → Kimi's ``low``/``high``/``max``: the same
#: three-step ordinal scale, different labels (``low→low``,
#: ``medium→high``, ``high→max``).
_EFFORT_MAP: dict[str, str] = {"low": "low", "medium": "high", "high": "max"}

#: Retry horizons for kimi quota wording: the rolling 5-hour window
#: resolves within the window; weekly/monthly/balance wording is a longer
#: wait — a 24 h park re-checks cheaply and the chain falls through
#: meanwhile. A ``Retry-After`` header always wins.
_HORIZON_5H = timedelta(hours=5)
_HORIZON_LONG = timedelta(hours=24)
_HORIZON_RATE = timedelta(minutes=15)
_HORIZON_QUOTA_DEFAULT = timedelta(hours=2)

#: Account-side quota wording (Error Reference, 2026-11-01): every one of
#: these arrives as HTTP 429 with "account usage issues" semantics — the
#: membership window/balance is spent, the rung pauses and falls through.
_QUOTA_WORDING = re.compile(
    r"usage limit|monthly quota|credit balance|extra usage", re.IGNORECASE
)
#: Long-window wording within the quota family.
_LONG_QUOTA_WORDING = re.compile(
    r"weekly|7-day|monthly|billing cycle|credit balance|extra usage",
    re.IGNORECASE,
)
#: Transient/risk-control 429s — *not* quota: "too many requests" and
#: "engine overloaded" recover on a short wait; "concurrent request
#: limit" is risk-control (the guidelines' enforcement signal), unrelated
#: to remaining quota.
_RATE_WORDING = re.compile(
    r"concurrent request limit|too many requests|overloaded", re.IGNORECASE
)


def default_profile() -> dict[str, Any]:
    """The canonical ``llm.profiles`` entry for the Kimi Code adapter.

    JSON-able; an operator writes it under
    ``{"<PROFILE_NAME>": default_profile()}`` (or adapts the
    ``base_url``/``cap`` for the account region and tier) — the values
    are config, not constants the wire reads directly. ``ops`` ships
    absent (``None`` — no op gate) since the 2026-11-07 ruling
    (gr543829): once lit the profile serves whatever the bulk guard and
    window cap admit.
    """
    return {
        "base_url": DEFAULT_BASE_URL,
        "api_key_ref": API_KEY_REF,
        "kind": "subscription",
        "adapter": ADAPTER,
        "gate": SERVICE_NAME,
        "ops": DEFAULT_OPS,
    }


def _profile_base_url() -> str | None:
    """The ``kimi`` profile's configured ``base_url`` (stripped), or
    ``None`` when unset or not a non-empty string."""
    from precis.utils.llm import live_config

    profiles = live_config.profiles_override() or {}
    raw = profiles.get(PROFILE_NAME)
    url = raw.get("base_url") if isinstance(raw, dict) else None
    if isinstance(url, str) and url.strip():
        return url.strip().rstrip("/")
    return None


def _base_url_refusal(url: str) -> str | None:
    """Why ``url`` must not receive the membership key, or ``None`` —
    ``https`` only, an exact :data:`BASE_URL_HOSTS` member (hostname is
    compared lowercased with a trailing dot stripped), no userinfo, no
    port but 443. Never returns the raw URL: a userinfo override could
    carry a password."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port  # a malformed port raises here
    except ValueError:
        return "unparseable URL"
    host = (parts.hostname or "").rstrip(".")
    if parts.scheme != "https":
        return "scheme is not https"
    if parts.username is not None or parts.password is not None:
        return f"userinfo on host {host or '<none>'!r}"
    if port not in (None, 443):
        return f"port {port} on host {host!r}"
    if host not in BASE_URL_HOSTS:
        return f"host {host or '<none>'!r} is not an allowed endpoint"
    return None


def _base_url_message(why: str) -> str:
    return (
        f"llm.profiles 'kimi' 'base_url' refused: {why}; the key is sent "
        f"only to hosts {', '.join(sorted(BASE_URL_HOSTS))}"
    )


def base_url_refusal() -> str | None:
    """The refusal message for the configured ``base_url`` override —
    the same text :func:`base_url` raises — or ``None`` when it resolves
    an allowed host. The secrets probe reports this instead of sending a
    request (a refused override must be visible, never silently
    skipped)."""
    override = _profile_base_url()
    if override is None:
        return None
    why = _base_url_refusal(override)
    return _base_url_message(why) if why is not None else None


def base_url() -> str:
    """The membership endpoint's base URL — the ``kimi`` profile's
    ``base_url`` (``llm.profiles``) when set, else
    :data:`BASE_URL_OVERSEAS`. The account's region decides (China is
    :data:`BASE_URL_CHINA`). An override is honoured only when it names
    a :data:`BASE_URL_HOSTS` member over plain https — the launch env
    and the probe Bearer-auth whatever this returns, so anything else
    raises :class:`~precis.errors.BadInput` naming the setting and the
    allowlist rather than silently fall back to overseas. The
    ``kimi_tree`` launch env
    (:func:`precis.workers.job_types.run_slice._kimi_launch_env`) and
    the secrets probe (:func:`precis.secret_status._probe_kimi`) share
    this resolver so both hit the same host.
    """
    override = _profile_base_url()
    if override is None:
        return BASE_URL_OVERSEAS
    why = _base_url_refusal(override)
    if why is not None:
        raise BadInput(
            _base_url_message(why),
            next=(
                "set llm.profiles 'kimi' 'base_url' to an allowed host "
                f"({', '.join(sorted(BASE_URL_HOSTS))}) or drop the override "
                f"(default {BASE_URL_OVERSEAS}); the allowlist widens only by "
                "a reviewed land"
            ),
        )
    return override


def service_gate_lit(name: str) -> bool:
    """True iff a ``service_config`` row lights ``name`` (``prio > 0``).

    The generic gate check behind a profile's ``gate`` field — the
    ``citer_watch`` shape routed through the process-bound store
    (:func:`precis.budget.meter.active_store`, lazy like
    :mod:`precis.utils.llm.live_config`). Never raises: no store, a
    missing table or a connection blip all read as **dark** — an
    unreadable gate must stop requests, not arm them.
    """
    try:
        from precis.budget import meter

        store = meter.active_store()
        if store is None:
            return False
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM service_config WHERE service = %s AND prio > 0 LIMIT 1",
                (name,),
            ).fetchone()
        return row is not None
    except Exception:
        log.warning(
            "kimi: service_config gate lookup for %r failed", name, exc_info=True
        )
        return False


def enabled() -> bool:
    """Whether the ``llm_kimi`` service gate is lit."""
    return service_gate_lit(SERVICE_NAME)


def wire_routing(
    model: str, *, effort: str | None = None, thinking: bool | None = None
) -> dict[str, Any]:
    """The ``extra_body`` for a Kimi-wire call — replaces
    :func:`~precis.utils.llm.router.openrouter_routing` on this endpoint.

    The OpenAI-compatible Kimi endpoint takes a flat ``reasoning_effort``
    (``low``/``high``/``max`` — mapped from our vocabulary by
    :data:`_EFFORT_MAP`), and k3.1-pro *rejects* thinking-disabled
    requests with no documented disable flag for the rest — so the
    SMALL-tier ``reasoning.enabled: false`` default is **never** emitted
    here: ``thinking=False`` resolves to "send nothing" and the model
    uses its own default (``max`` on k3.1-pro). Empty dict means the bare
    completion body goes out unchanged.
    """
    if effort and effort in _EFFORT_MAP:
        return {"reasoning_effort": _EFFORT_MAP[effort]}
    return {}


def classify_failure(
    text: str, *, exc: BaseException | None = None, now: datetime | None = None
) -> Classified | None:
    """Kimi error wording → a failure class, or ``None`` to keep the
    generic classification.

    Only the cases where the Kimi wire means something different from the
    generic table get a class:

    * quota wording (``5-hour``/``weekly``/``monthly usage limit``,
      ``credit balance``, ``extra usage``) → ``quota``. Generic
      classification already lands most of these on ``quota`` via the
      ``usage limit`` lead-in; ``credit balance``/``extra usage`` would
      land on ``budget`` — here they are the *subscription balance*, so
      ``quota`` is the truer class.
    * transient/risk-control wording (``concurrent request limit``,
      ``too many requests``, ``overloaded``) → ``rate`` — stays rate; a
      concurrent-limit hit is the guidelines' enforcement signal, not a
      quota reset.
    * any other HTTP 429 → ``quota`` (per the gripe's fallback rule: on
      this wire 429s are account-side limits unless the wording says
      transient — the Error Reference documents no other 429 shape).
    """
    now = now if now is not None else datetime.now(UTC)
    t = text or ""
    # A live HTTPError or the folded "…status code: 429…" text the
    # tools-wire error carries — same signal either way.
    is_429 = (isinstance(exc, HTTPError) and exc.code == 429) or bool(
        re.search(r"\b429\b", t)
    )
    if not t and not is_429:
        return None
    secs = (
        retry_after_seconds(exc.headers, wall=lambda: now)
        if isinstance(exc, HTTPError) and exc.code in (429, 503)
        else None
    )
    if _QUOTA_WORDING.search(t):
        horizon = (_LONG_QUOTA_WORDING.search(t) and _HORIZON_LONG) or _HORIZON_5H
        return Classified(
            "quota",
            now + timedelta(seconds=secs) if secs is not None else now + horizon,
        )
    if _RATE_WORDING.search(t):
        return Classified(
            "rate",
            now + timedelta(seconds=secs) if secs is not None else now + _HORIZON_RATE,
        )
    if is_429:
        return Classified(
            "quota",
            now + timedelta(seconds=secs)
            if secs is not None
            else now + _HORIZON_QUOTA_DEFAULT,
        )
    return None


def adjust_failure(result: Any, *, exc: BaseException | None = None) -> Any:
    """Reclassify a kimi-wire failure into provider truth (duck-typed on
    :class:`~precis.utils.llm.router.LlmResult` — no router import).

    A ``quota`` hit additionally marks the result ``paused`` +
    ``quota_exhausted`` — the acceptance shape (an exhausted plan pauses
    the rung and falls through). Everything else keeps the generic
    classification's ``paused``/``retry_at``.
    """
    if getattr(result, "error", None) is None:
        return result
    found = classify_failure(result.error, exc=exc)
    if found is None:
        return result
    patch: dict[str, Any] = {
        "reason_class": found.reason_class,
        "retry_at": found.retry_at,
    }
    if found.reason_class == "quota":
        patch["paused"] = True
        patch["quota_exhausted"] = True
    return _replace(result, **patch)


#: ``(model_id, tier_floor, max_input, prose)`` card seeds — facts from
#: the docs' model table (2026-11-01). ``max_input`` is the documented
#: ceiling (tier gates apply: 1M unlocks at the top membership tier;
#: 256K is the default window); ``None`` where the docs give no figure.
_CARD_SPECS: tuple[tuple[str, str, int | None, str], ...] = (
    (
        "kimi-for-coding",
        "big",
        1_048_576,
        "Kimi for Coding (Moonshot K2.8 Preview) — the subscription coding "
        "model on the Kimi Code membership endpoint; low/high/max reasoning "
        "effort, up to 1M context. Coding and agent ops only.",
    ),
    (
        "k3.1-pro",
        "frontier",
        1_048_576,
        "K3.1 Pro (Moonshot) — flagship 3T-class agentic coding model; "
        "thinking REQUIRED (max default — thinking-disabled requests are "
        "rejected); up to 1M context at the top membership tier.",
    ),
    (
        "k3",
        "frontier",
        1_048_576,
        "K3 (Moonshot) — flagship reasoning model, low/high/max effort; "
        "best for large-codebase analysis, multi-file refactoring and long "
        "documents; up to 1M (tier-gated).",
    ),
    (
        "k3-256k",
        "big",
        262_144,
        "K3 256K (Moonshot) — same results as k3 within its window at ~half "
        "the quota; everyday coding — Q&A, completion, small-file edits. "
        "No video input.",
    ),
    (
        "k3.1-pro-highspeed",
        "frontier",
        1_048_576,
        "K3.1 Pro Highspeed (Moonshot) — ~2-3x the output speed of k3.1-pro "
        "at ~2x the quota usage; top membership tier; up to 1M.",
    ),
    (
        "kimi-for-coding-highspeed",
        "big",
        None,
        "Kimi for Coding Highspeed (Moonshot K2.7 Code) — the fast coding "
        "variant on the membership endpoint; top membership tier.",
    ),
)


def seed_cards(store: Any) -> list[tuple[str, int, bool]]:
    """Mint (or refresh) an ``llm`` catalog card per Kimi Code model —
    the subscription-billing operating point the picker can reason about.

    One ``openai_compat`` offering per card carrying
    ``billing='subscription'`` (a flat membership plan — no per-token
    price, deliberately no ``price_in``/``price_out``) and ``endpoint``
    ``'kimi-code'`` so the operating point names where it runs.
    Idempotent via ``upsert_card`` on ``model_id``. Returns
    ``[(model_id, ref_id, created)]``.
    """
    from precis import llm_catalog

    results: list[tuple[str, int, bool]] = []
    for model_id, tier_floor, max_input, prose in _CARD_SPECS:
        offering: dict[str, Any] = {
            "effort": "medium",
            "transport": "openai_compat",
            "endpoint": "kimi-code",
            "billing": "subscription",
        }
        if max_input is not None:
            offering["max_input"] = max_input
        ref_id, created = llm_catalog.upsert_card(
            store,
            model_id=model_id,
            text=prose,
            tier_floor=tier_floor,
            offerings=[offering],
            params={"vendor": "moonshot", "service": "kimi-code"},
            provenance={"source": "seed-kimi", "band": "vendor-docs-2026-11-01"},
        )
        results.append((model_id, ref_id, created))
    return results


__all__ = [
    "ADAPTER",
    "API_KEY_REF",
    "BASE_URL_CHINA",
    "BASE_URL_HOSTS",
    "BASE_URL_OVERSEAS",
    "DEFAULT_BASE_URL",
    "DEFAULT_OPS",
    "MODELS",
    "PROFILE_NAME",
    "SERVICE_NAME",
    "THINKING_REQUIRED_MODELS",
    "adjust_failure",
    "base_url",
    "base_url_refusal",
    "classify_failure",
    "default_profile",
    "enabled",
    "seed_cards",
    "service_gate_lit",
    "wire_routing",
]
