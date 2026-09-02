"""The `/secrets` page's registry of *known* secrets + live-verify probes.

:data:`KNOWN_SECRETS` documents every vault-resolved credential this
codebase actually consumes — name, one-line purpose, where to obtain one,
and (where a provider offers a cheap auth-only check) a ``probe_group``
naming the network probe in :data:`_PROBES` that verifies it.

SECURITY: a probe resolves the plaintext via :func:`precis.secrets.get_secret`
*inside this module only*, and passes it into an outbound request header/body.
The plaintext never leaves this function call — it is never put into a
:class:`CheckResult`, a log line, or an exception message. ``CheckResult.detail``
is always a short human string synthesized from an HTTP status code or
exception *type*, never from response headers/body (Wolfram's body-sniff is
the one exception, and it reads a few hundred bytes locally to test for a
substring — it is never included in ``detail``).

The probe URLs are fixed, hardcoded provider endpoints — never agent- or
user-supplied — so the ``safe_fetch`` SSRF convention
(``src/precis/utils/safe_fetch.py``) does not apply here; a plain
``httpx.AsyncClient`` is fine (see the comment at its construction site).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from precis.secrets import get_secret, is_available

if TYPE_CHECKING:
    from precis.store import Store

# ── registry ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecretSpec:
    """One known secret: what it's for, where to get one, how to verify it."""

    name: str
    purpose: str
    get_url: str | None
    get_blurb: str
    #: Key into :data:`_PROBES`, or ``None`` for a presence-only secret (no
    #: cheap auth-only endpoint, or a probe would cost money/side effects).
    probe_group: str | None = None


KNOWN_SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec(
        name="ANTHROPIC_API_KEY",
        purpose="Anthropic API key for the cloud rungs of the LLM ladder.",
        get_url="https://console.anthropic.com/settings/keys",
        get_blurb="Create a key in the Anthropic console and paste it here.",
        probe_group="anthropic",
    ),
    SecretSpec(
        name="CLAUDE_CODE_OAUTH_TOKEN",
        purpose="Auth for daemon-spawned `claude -p` agent runs.",
        get_url=None,
        get_blurb="Run `claude setup-token` on any machine you're already "
        "logged into, and paste the token it prints here.",
        probe_group=None,
    ),
    SecretSpec(
        name="PERPLEXITY_API_KEY",
        purpose="Backs the perplexity-research / perplexity-reasoning kinds.",
        get_url="https://www.perplexity.ai/settings/api",
        get_blurb="Generate an API key in Perplexity's settings and paste it here.",
        probe_group="perplexity",
    ),
    SecretSpec(
        name="SEMANTIC_SCHOLAR_API_KEY",
        purpose="Paper metadata + citation ingest (Semantic Scholar).",
        get_url="https://www.semanticscholar.org/product/api",
        get_blurb="Request a free API key from Semantic Scholar and paste it here.",
        probe_group="s2",
    ),
    SecretSpec(
        name="EPO_OPS_CLIENT_KEY",
        purpose="Patent kind — EPO Open Patent Services OAuth pair (client key half).",
        get_url="https://developers.epo.org",
        get_blurb="Register an app at the EPO developer portal; it hands you a "
        "client key and secret pair — set both here.",
        probe_group="epo",
    ),
    SecretSpec(
        name="EPO_OPS_CLIENT_SECRET",
        purpose="Patent kind — EPO Open Patent Services OAuth pair (secret half).",
        get_url="https://developers.epo.org",
        get_blurb="Register an app at the EPO developer portal; it hands you a "
        "client key and secret pair — set both here.",
        probe_group="epo",
    ),
    SecretSpec(
        name="ORCID_CLIENT_ID",
        purpose="orcid kind — public-API OAuth pair (client ID half).",
        get_url="https://orcid.org/developer-tools",
        get_blurb="Register a public API application in ORCID's developer "
        "tools; it hands you a client ID and secret — set both here.",
        probe_group="orcid",
    ),
    SecretSpec(
        name="ORCID_CLIENT_SECRET",
        purpose="orcid kind — public-API OAuth pair (secret half).",
        get_url="https://orcid.org/developer-tools",
        get_blurb="Register a public API application in ORCID's developer "
        "tools; it hands you a client ID and secret — set both here.",
        probe_group="orcid",
    ),
    SecretSpec(
        name="WOLFRAM_APP_ID",
        purpose="math kind — Wolfram|Alpha computation.",
        get_url="https://developer.wolframalpha.com",
        get_blurb="Create an AppID in the Wolfram|Alpha developer portal and "
        "paste it here.",
        probe_group="wolfram",
    ),
    SecretSpec(
        name="PRECIS_CORE_API_KEY",
        purpose="CORE open-access fulltext fetch.",
        get_url="https://core.ac.uk/services/api",
        get_blurb="Register for CORE's free-tier API and paste the key here.",
        probe_group="core",
    ),
    SecretSpec(
        name="PRECIS_ELSEVIER_API_KEY",
        purpose="Elsevier TDM article fetch.",
        get_url="https://dev.elsevier.com",
        get_blurb="Register at the Elsevier Developer Portal — fulltext "
        "access needs an institutional TDM entitlement behind the key.",
        probe_group="elsevier",
    ),
    SecretSpec(
        name="PRECIS_OPENALEX_CONTENT_KEY",
        purpose="OpenAlex premium/content key.",
        get_url=None,
        get_blurb="Email support@openalex.org for a premium key — the free "
        "OpenAlex API needs no key at all.",
        probe_group="openalex",
    ),
    SecretSpec(
        name="PRECIS_WILEY_TDM_TOKEN",
        purpose="Wiley TDM PDF fetch.",
        get_url="https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining",
        get_blurb="Request a Wiley TDM client token — verifying it would "
        "mean actually downloading an article, so this entry is "
        "presence-only.",
        probe_group=None,
    ),
    SecretSpec(
        name="PRECIS_LLM_API_KEY",
        purpose="Key for the OSS LLM endpoint PRECIS_LLM_BASE_URL points at.",
        get_url="https://openrouter.ai/keys",
        get_blurb="If using OpenRouter as the OSS backend, generate a key "
        "there and paste it here (see docs/reference/config-variables.md "
        "for the full recipe).",
        probe_group="oss_llm",
    ),
    SecretSpec(
        name="PRECIS_SUMMARIZE_LLM_KEY",
        purpose="Optional override key for the summarize lane's endpoint.",
        get_url=None,
        get_blurb="Only needed when the summarize lane points at a "
        "different endpoint/key than the main LLM ladder.",
        probe_group=None,
    ),
    SecretSpec(
        name="ACATOME_CROSSREF_MAILTO",
        purpose="Crossref polite-pool contact email (not a credential).",
        get_url=None,
        get_blurb="Any reachable contact address works — Crossref just "
        "wants a mailto to rate-limit politely against.",
        probe_group="mailto",
    ),
    SecretSpec(
        name="ASA_DISCORD_TOKEN",
        purpose="Discord bot token for the asa_bot bridge.",
        get_url="https://discord.com/developers/applications",
        get_blurb="Create an application, add a Bot, and Reset Token to "
        "get a fresh token — paste it here.",
        probe_group="discord",
    ),
    SecretSpec(
        name="REMARKABLE_RMAPI_CONFIG",
        purpose="Deployment-wide shared reMarkable fallback (full rmapi config body).",
        get_url=None,
        get_blurb="Prefer per-user pairing on the /account page — this "
        "vault entry is only the shared-device fallback for sends before "
        "a user has paired their own tablet.",
        probe_group=None,
    ),
    SecretSpec(
        name="REMARKABLE_TOKEN",
        purpose="Same shared reMarkable fallback, bare device-token form.",
        get_url="https://my.remarkable.com/device/apps/connect",
        get_blurb="Get a one-time code there and exchange it for a device "
        "token — per-user pairing on /account is still preferred.",
        probe_group=None,
    ),
    SecretSpec(
        name="PRECIS_WEB_PASSWORD_PEPPER",
        purpose="Pepper for web login password hashing.",
        get_url=None,
        get_blurb="Auto-minted into the vault on first `precis users add` — "
        "there's nothing to obtain. Never rotate it: rotation invalidates "
        "every peppered password hash.",
        probe_group=None,
    ),
)


def _group_members() -> dict[str, tuple[str, ...]]:
    groups: dict[str, list[str]] = {}
    for spec in KNOWN_SECRETS:
        if spec.probe_group:
            groups.setdefault(spec.probe_group, []).append(spec.name)
    return {group: tuple(names) for group, names in groups.items()}


#: probe_group -> the KNOWN_SECRETS member names it covers, in registry order.
_PROBE_GROUP_MEMBERS: dict[str, tuple[str, ...]] = _group_members()


# ── results ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CheckResult:
    """Outcome of one probe group. ``detail`` is always safe to render/log —
    never the secret value, a header dump, or response body text."""

    state: str  # "ok" | "bad" | "unknown"
    detail: str


def _classify_status(
    status: int, *, bad_codes: tuple[int, ...] = (401, 403)
) -> CheckResult:
    """2xx -> ok; a listed auth-rejection code -> bad; anything else -> unknown.

    Pure function (no I/O) so the HTTP-status mapping is unit-testable
    without a network call.
    """
    if 200 <= status < 300:
        return CheckResult("ok", "verified")
    if status in bad_codes:
        return CheckResult("bad", f"HTTP {status} — key rejected")
    return CheckResult("unknown", f"HTTP {status} — could not verify")


# ── probes ───────────────────────────────────────────────────────────────
#
# Fixed, hardcoded provider endpoints below — never agent- or user-supplied
# input — so a plain httpx.AsyncClient is the right tool; the safe_fetch
# SSRF guard exists for agent-supplied URLs, which these are not.

ProbeFn = Callable[[httpx.AsyncClient, dict[str, str]], Awaitable[CheckResult]]


async def _probe_anthropic(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.get(
        "https://api.anthropic.com/v1/models",
        headers={
            "x-api-key": values["ANTHROPIC_API_KEY"],
            "anthropic-version": "2023-06-01",
        },
    )
    return _classify_status(r.status_code)


async def _probe_perplexity(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    # A deliberately invalid model/body: Perplexity checks auth before the
    # request body, so a 400 here means the key was ACCEPTED. Costs nothing.
    r = await client.post(
        "https://api.perplexity.ai/chat/completions",
        headers={"Authorization": f"Bearer {values['PERPLEXITY_API_KEY']}"},
        json={"model": "__probe__", "messages": []},
    )
    if r.status_code == 400:
        return CheckResult("ok", "verified")
    if r.status_code == 401:
        return CheckResult("bad", "HTTP 401 — key rejected")
    return CheckResult("unknown", f"HTTP {r.status_code} — could not verify")


async def _probe_s2(client: httpx.AsyncClient, values: dict[str, str]) -> CheckResult:
    r = await client.get(
        "https://api.semanticscholar.org/graph/v1/paper/arXiv:1706.03762",
        params={"fields": "title"},
        headers={"x-api-key": values["SEMANTIC_SCHOLAR_API_KEY"]},
    )
    if r.status_code == 429:
        # Rate limiting doesn't mean the key is bad.
        return CheckResult("unknown", "rate limited — could not verify")
    return _classify_status(r.status_code)


async def _probe_epo(client: httpx.AsyncClient, values: dict[str, str]) -> CheckResult:
    r = await client.post(
        "https://ops.epo.org/3.2/auth/accesstoken",
        auth=(values["EPO_OPS_CLIENT_KEY"], values["EPO_OPS_CLIENT_SECRET"]),
        data={"grant_type": "client_credentials"},
    )
    return _classify_status(r.status_code)


async def _probe_orcid(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.post(
        "https://orcid.org/oauth/token",
        data={
            "client_id": values["ORCID_CLIENT_ID"],
            "client_secret": values["ORCID_CLIENT_SECRET"],
            "grant_type": "client_credentials",
            "scope": "/read-public",
        },
        headers={"Accept": "application/json"},
    )
    return _classify_status(r.status_code)


async def _probe_wolfram(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.get(
        "https://api.wolframalpha.com/v1/result",
        params={"i": "2+2", "appid": values["WOLFRAM_APP_ID"]},
    )
    if r.status_code == 200:
        return CheckResult("ok", "verified")
    if r.status_code == 403:
        return CheckResult("bad", "HTTP 403 — key rejected")
    if r.status_code == 501:
        # Read a small, bounded prefix locally to test for an appid
        # complaint; never put body text into detail.
        body = r.content[:400].decode("utf-8", errors="replace").lower()
        if "appid" in body:
            return CheckResult("bad", "HTTP 501 — key rejected")
        return CheckResult("unknown", "HTTP 501 — could not verify")
    return CheckResult("unknown", f"HTTP {r.status_code} — could not verify")


async def _probe_core(client: httpx.AsyncClient, values: dict[str, str]) -> CheckResult:
    r = await client.get(
        "https://api.core.ac.uk/v3/search/works",
        params={"q": "test", "limit": 1},
        headers={"Authorization": f"Bearer {values['PRECIS_CORE_API_KEY']}"},
    )
    return _classify_status(r.status_code)


async def _probe_elsevier(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.get(
        "https://api.elsevier.com/content/search/sciencedirect",
        params={"query": "test", "count": 1},
        headers={"X-ELS-APIKey": values["PRECIS_ELSEVIER_API_KEY"]},
    )
    return _classify_status(r.status_code, bad_codes=(401, 403))


async def _probe_openalex(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.get(
        "https://api.openalex.org/works",
        params={"per-page": 1, "api_key": values["PRECIS_OPENALEX_CONTENT_KEY"]},
    )
    return _classify_status(r.status_code, bad_codes=(403,))


async def _probe_oss_llm(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    # Same config seam as precis.utils.llm.router._dispatch_openai_compat.
    base_url = os.environ.get("PRECIS_LLM_BASE_URL", "").rstrip("/")
    r = await client.get(
        f"{base_url}/models",
        headers={"Authorization": f"Bearer {values['PRECIS_LLM_API_KEY']}"},
    )
    return _classify_status(r.status_code)


async def _probe_discord(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.get(
        "https://discord.com/api/v10/users/@me",
        headers={"Authorization": f"Bot {values['ASA_DISCORD_TOKEN']}"},
    )
    return _classify_status(r.status_code)


async def _probe_mailto(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    # Local format check only — no network.
    if "@" in values["ACATOME_CROSSREF_MAILTO"]:
        return CheckResult("ok", "verified")
    return CheckResult("bad", "not an email address")


_PROBES: dict[str, ProbeFn] = {
    "anthropic": _probe_anthropic,
    "perplexity": _probe_perplexity,
    "s2": _probe_s2,
    "epo": _probe_epo,
    "orcid": _probe_orcid,
    "wolfram": _probe_wolfram,
    "core": _probe_core,
    "elsevier": _probe_elsevier,
    "openalex": _probe_openalex,
    "oss_llm": _probe_oss_llm,
    "discord": _probe_discord,
    "mailto": _probe_mailto,
}

#: Per-probe timeout — generous enough for a slow provider, short enough
#: that a hung probe doesn't stall the whole page for 15 minutes (the cache
#: TTL papers over the wait afterward).
_PROBE_TIMEOUT_S = 6.0


async def _safe_probe(
    fn: ProbeFn, client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    """Run one probe, turning any exception into an ``unknown`` result.

    Never lets a probe's exception message reach the caller unfiltered —
    only the exception *type name* is used, since ``str(exc)`` on an httpx
    error can echo back request details.
    """
    try:
        return await fn(client, values)
    except httpx.TimeoutException:
        return CheckResult("unknown", "timeout — could not verify")
    except Exception as exc:  # deliberately broad, see docstring above
        return CheckResult("unknown", f"{type(exc).__name__} — could not verify")


async def run_checks(store: Store) -> dict[str, CheckResult]:
    """Probe every KNOWN_SECRETS group that has at least one member present.

    Returns one :class:`CheckResult` per *present* member of a probed group:
    a joint group (epo, orcid) with only one half set reports "bad —
    partner secret missing" for the half that IS set, without a network
    call; a fully-set group runs its probe once and the result covers both
    names. Presence-only specs (``probe_group is None``) and the
    ``oss_llm`` group when ``PRECIS_LLM_BASE_URL`` is unset get no entry —
    the caller treats "known, present, no entry" as unverified-but-present.
    """
    results: dict[str, CheckResult] = {}
    group_order: list[str] = []
    group_names: dict[str, tuple[str, ...]] = {}
    coros: list[Awaitable[CheckResult]] = []

    # Fixed provider endpoints only (see module docstring) — not agent- or
    # user-supplied, so plain httpx is fine here; safe_fetch is for the
    # agent-supplied-URL surface.
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT_S) as client:
        for group, names in _PROBE_GROUP_MEMBERS.items():
            present = [n for n in names if is_available(n, store=store)]
            if not present:
                continue
            if len(present) < len(names):
                for n in present:
                    results[n] = CheckResult("bad", "partner secret missing")
                continue
            if group == "oss_llm" and not os.environ.get("PRECIS_LLM_BASE_URL"):
                # No endpoint configured — nothing to probe against; the key
                # is presence-only until a base URL is set.
                continue
            values = {n: get_secret(n, store=store) or "" for n in names}
            group_order.append(group)
            group_names[group] = names
            coros.append(_safe_probe(_PROBES[group], client, values))

        outcomes = await asyncio.gather(*coros) if coros else []

    for group, outcome in zip(group_order, outcomes, strict=True):
        for n in group_names[group]:
            results[n] = outcome
    return results


# ── cache ────────────────────────────────────────────────────────────────

_last: dict[str, CheckResult] = {}
_checked_at: datetime | None = None
_lock: asyncio.Lock | None = None  # lazily-created; see get_results


def checked_at() -> datetime | None:
    """When :func:`run_checks` last actually ran (``None`` before the first call)."""
    return _checked_at


async def get_results(
    store: Store, *, max_age_s: float = 900.0, force: bool = False
) -> dict[str, CheckResult]:
    """Cached probe results — reruns :func:`run_checks` when stale or forced.

    The lock is created lazily (inside the first async call) rather than at
    import time, so importing this module never requires a running event
    loop.
    """
    global _checked_at, _lock
    if _lock is None:
        _lock = asyncio.Lock()

    now = datetime.now(UTC)
    if (
        not force
        and _checked_at is not None
        and (now - _checked_at).total_seconds() < max_age_s
    ):
        return _last
    async with _lock:
        now = datetime.now(UTC)
        if (
            not force
            and _checked_at is not None
            and (now - _checked_at).total_seconds() < max_age_s
        ):
            return _last
        results = await run_checks(store)
        _last.clear()
        _last.update(results)
        _checked_at = now
        return _last


__all__ = [
    "KNOWN_SECRETS",
    "CheckResult",
    "ProbeFn",
    "SecretSpec",
    "checked_at",
    "get_results",
    "run_checks",
]
