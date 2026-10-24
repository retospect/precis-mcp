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
substring — it is never included in ``detail``). Probe traffic runs inside
:func:`_quiet_probe_logs`: httpx logs the full request URL at INFO and
httpcore logs headers at DEBUG/TRACE, and the OpenAlex/Wolfram probes carry
the key in the query string — the key goes on the wire, never into a log
record.

Who runs the probes: **only the background checker**, never the web page.
:func:`record_status` (driven by the ``secret_status`` job_type — cadence-
minted, or minted on demand by the page's "Check now" POST — and by the
weekly ``secret_check`` alert pass) runs :func:`run_checks` plus a
no-decrypt presence check and persists one :class:`StatusEntry` per secret
in ``app_state``. The ``/secrets`` render path reads only that stored
status (:func:`stored_status`) — it never probes, never decrypts. Infra-
class secrets (``svc/infra/*``, db-own's vault class rule) are checked for
presence only and are never probed, here or anywhere.

:func:`weekly_report` / ``precis secret check`` run the same probes outside the
web page for ``/whatneedsdoing`` (gr496305): one state per registry secret
(ok, failed, unprobed, missing), plus the last succeeded job for an unprobed
secret that jobs depend on. It adds no endpoints beyond :data:`_PROBES`.

The probe URLs are fixed, hardcoded provider endpoints — never agent- or
user-supplied — so the ``safe_fetch`` SSRF convention
(``src/precis/utils/safe_fetch.py``) does not apply here; a plain
``httpx.AsyncClient`` is fine (see the comment at its construction site).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import httpx

from precis.secrets import (
    get_secret,
    is_available,
    is_infra_secret,
    list_secrets,
    present,
)
from precis.utils.http import (
    POLICIES,
    RETRY_STATUSES,
    async_call_with_backoff,
    polite_user_agent,
)
from precis.utils.timeutil import as_utc

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

# ── registry ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SecretSpec:
    """One known secret: what it's for, where to get one, how to verify it."""

    name: str
    purpose: str
    get_url: str | None
    get_blurb: str
    #: Short cost note rendered as a badge — labels starting with "free"
    #: render green, everything else amber (money involved somewhere).
    cost: str = "free"
    #: Key into :data:`_PROBES`, or ``None`` for a presence-only secret (no
    #: cheap auth-only endpoint, or a probe would cost money/side effects).
    probe_group: str | None = None
    #: ``meta->>'job_type'`` values of jobs that cannot succeed without this
    #: secret. The weekly check shows the last succeeded one for a secret no
    #: probe covers, as evidence that it still works.
    used_by_job_types: tuple[str, ...] = ()
    #: Absent is a normal configuration, so the weekly check reports it as
    #: missing without asking anyone to provision it.
    optional: bool = False


KNOWN_SECRETS: tuple[SecretSpec, ...] = (
    SecretSpec(
        name="ANTHROPIC_API_KEY",
        purpose="Anthropic API key for the cloud rungs of the LLM ladder.",
        get_url="https://console.anthropic.com/settings/keys",
        get_blurb="Create a key in the Anthropic console and paste it here.",
        cost="paid — usage-billed",
        probe_group="anthropic",
    ),
    SecretSpec(
        name="CLAUDE_CODE_OAUTH_TOKEN",
        purpose="Auth for daemon-spawned `claude -p` agent runs.",
        get_url="https://code.claude.com/docs/en/cli-reference",
        get_blurb="Run `claude setup-token` on any machine you're already "
        "logged into, and paste the token it prints here.",
        cost="included with a Claude subscription",
        probe_group=None,
    ),
    SecretSpec(
        name="PERPLEXITY_API_KEY",
        purpose="Backs the perplexity-research / perplexity-reasoning kinds.",
        get_url="https://www.perplexity.ai/settings/api",
        get_blurb="Generate an API key in Perplexity's settings and paste it here.",
        cost="paid — usage-billed",
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
        cost="free tier",
        probe_group="epo",
    ),
    SecretSpec(
        name="EPO_OPS_CLIENT_SECRET",
        purpose="Patent kind — EPO Open Patent Services OAuth pair (secret half).",
        get_url="https://developers.epo.org",
        get_blurb="Register an app at the EPO developer portal; it hands you a "
        "client key and secret pair — set both here.",
        cost="free tier",
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
        name="PRECIS_DIGIKEY_CLIENT_ID",
        purpose="component kind — Digi-Key Product Information v4 OAuth pair "
        "(client ID half); live stock for `view='stock'`.",
        get_url="https://developer.digikey.com",
        get_blurb="Register a **Production** app (the sandbox returns canned "
        "data) with the Product Information API enabled; it hands you a "
        "Client ID and Client Secret — set both here.",
        cost="free tier",
        probe_group="digikey",
    ),
    SecretSpec(
        name="PRECIS_DIGIKEY_CLIENT_SECRET",
        purpose="component kind — Digi-Key Product Information v4 OAuth pair "
        "(secret half); live stock for `view='stock'`.",
        get_url="https://developer.digikey.com",
        get_blurb="Register a **Production** app (the sandbox returns canned "
        "data) with the Product Information API enabled; it hands you a "
        "Client ID and Client Secret — set both here.",
        cost="free tier",
        probe_group="digikey",
    ),
    SecretSpec(
        name="PRECIS_FARNELL_API_KEY",
        purpose="component stock — Farnell/element14 warehouse inventory; live read only.",
        get_url="https://partner.element14.com/",
        get_blurb="Create a Partner Portal account and request a Product Search API key. "
        "Ireland store is the default; no contract-pricing credentials needed.",
        cost="free courtesy allowance — confirm ongoing quota",
        # No auth-only endpoint: don't spend a search quota on page rendering.
        probe_group=None,
    ),
    SecretSpec(
        name="PRECIS_MOUSER_API_KEY",
        purpose="component stock — Mouser Search API; live attributed read only.",
        get_url="https://www.mouser.com/en/api-search/",
        get_blurb="Create/verify MyMouser, request Search API access describing your "
        "application, and paste the approved key emailed to you (not an Order API key).",
        cost="free approved Search API — 1000 calls/day",
        probe_group=None,
    ),
    SecretSpec(
        name="WOLFRAM_APP_ID",
        purpose="math kind — Wolfram|Alpha computation.",
        get_url="https://developer.wolframalpha.com",
        get_blurb="Create an AppID in the Wolfram|Alpha developer portal and "
        "paste it here.",
        cost="free tier (non-commercial)",
        probe_group="wolfram",
    ),
    SecretSpec(
        name="PRECIS_MP_API_KEY",
        purpose="Materials Project: Pourbaix entries for the pourbaix_bulk job, "
        "bulk-crystal lookups for structure source='materials-project', and "
        "QMOF CIFs from MPContribs when no QMOF bundle index is configured.",
        get_url="https://next-gen.materialsproject.org/api",
        get_blurb="Log in to the Materials Project; the API page shows your "
        "key — paste it here.",
        cost="free",
        probe_group=None,
        used_by_job_types=("pourbaix_bulk",),
    ),
    SecretSpec(
        name="PRECIS_CATHUB_API_KEY",
        purpose="Catalysis-Hub GraphQL key for structure source='catalysis-hub'; "
        "optional until the server demands it.",
        get_url="https://www.catalysis-hub.org",
        get_blurb="Request SUNCAT credentials from the Catalysis-Hub maintainers "
        "and paste the API key here.",
        cost="free — credentials issued by SUNCAT",
        probe_group=None,
    ),
    SecretSpec(
        name="PRECIS_CORE_API_KEY",
        purpose="CORE open-access fulltext fetch.",
        get_url="https://core.ac.uk/services/api",
        get_blurb="Register for CORE's free-tier API and paste the key here.",
        cost="free tier",
        probe_group="core",
    ),
    SecretSpec(
        name="PRECIS_ELSEVIER_API_KEY",
        purpose="Elsevier TDM article fetch.",
        get_url="https://dev.elsevier.com",
        get_blurb="Register at the Elsevier Developer Portal — fulltext "
        "access needs an institutional TDM entitlement behind the key.",
        cost="free key — needs a paid institutional subscription",
        probe_group="elsevier",
    ),
    SecretSpec(
        name="PRECIS_OPENALEX_CONTENT_KEY",
        purpose="OpenAlex premium/content key.",
        get_url="mailto:support@openalex.org",
        get_blurb="Email support@openalex.org for a premium key — the free "
        "OpenAlex API needs no key at all.",
        cost="paid — premium subscription",
        probe_group="openalex",
    ),
    SecretSpec(
        name="PRECIS_ICAO_API_KEY",
        purpose="Morning briefing weather — ICAO API Data Service NOTAM feed "
        "for the configured airports; missing key shows 'no key configured' "
        "and the brief carries on without the NOTAM block.",
        get_url="https://applications.icao.int/dataservices/default.aspx",
        get_blurb="Request a key for the ICAO API Data Service "
        "(icaoapi@icao.int) and paste it here.",
        cost="free key — registration required",
        probe_group=None,
        optional=True,
    ),
    SecretSpec(
        name="PRECIS_WILEY_TDM_TOKEN",
        purpose="Wiley TDM PDF fetch.",
        get_url="https://onlinelibrary.wiley.com/library-info/resources/text-and-datamining",
        get_blurb="Request a Wiley TDM client token — verifying it would "
        "mean actually downloading an article, so this entry is "
        "presence-only.",
        cost="free token — needs an institutional subscription",
        probe_group=None,
    ),
    SecretSpec(
        name="PRECIS_LLM_API_KEY",
        purpose="Key for the OSS LLM endpoint PRECIS_LLM_BASE_URL points at.",
        get_url="https://openrouter.ai/keys",
        get_blurb="If using OpenRouter as the OSS backend, generate a key "
        "there and paste it here (see docs/reference/config-variables.md "
        "for the full recipe).",
        cost="paid — usage-billed",
        probe_group="oss_llm",
    ),
    SecretSpec(
        name="PRECIS_SUMMARIZE_LLM_KEY",
        purpose="Optional override key for the summarize lane's endpoint.",
        get_url=None,
        get_blurb="Only needed when the summarize lane points at a "
        "different endpoint/key than the main LLM ladder.",
        cost="depends on the endpoint",
        probe_group=None,
        optional=True,
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
        name="PRECIS_WEB_PASSWORD_PEPPER",
        purpose="Pepper for web login password hashing.",
        get_url=None,
        get_blurb="Auto-minted into the vault on first `precis users add` — "
        "there's nothing to obtain. Never rotate it: rotation invalidates "
        "every peppered password hash.",
        probe_group=None,
    ),
    SecretSpec(
        name="JLCPCB_APP_ID",
        purpose="pcb kind — JLCPCB Open API triple (app ID); live stock/price.",
        get_url=None,
        get_blurb="Create an app in the JLCPCB Open API console, grant it the "
        "Components scope, and set the app ID, access key and secret key here. "
        "Requests are HMAC-signed per call, so there is no auth-only "
        "endpoint to probe.",
        probe_group=None,
    ),
    SecretSpec(
        name="JLCPCB_ACCESS_KEY",
        purpose="pcb kind — JLCPCB Open API triple (access key).",
        get_url=None,
        get_blurb="Same app as JLCPCB_APP_ID in the JLCPCB Open API console.",
        probe_group=None,
    ),
    SecretSpec(
        name="JLCPCB_SECRET_KEY",
        purpose="pcb kind — JLCPCB Open API triple (HMAC signing secret).",
        get_url=None,
        get_blurb="Same app as JLCPCB_APP_ID in the JLCPCB Open API console.",
        probe_group=None,
    ),
    SecretSpec(
        name="NANOPUB_BOT_PRIVATE_KEY",
        purpose="Nanopub signing key (base64 PKCS#8) for the non-attesting bot identity.",
        get_url=None,
        get_blurb="Generate with `precis nanopub keygen` and store it with "
        "`precis secret set`. Not an external service credential.",
        probe_group=None,
    ),
    SecretSpec(
        name="NANOPUB_ATTESTING_PRIVATE_KEY",
        purpose="Nanopub signing key (base64 PKCS#8) for the attesting reviewer.",
        get_url=None,
        get_blurb="Generate with `precis nanopub keygen` and store it with "
        "`precis secret set`. Not an external service credential.",
        probe_group=None,
    ),
    SecretSpec(
        name="NANOPUB_ATTESTING_ORCID",
        purpose="ORCID URI the attesting nanopub key is registered to (an "
        "identifier, not a credential).",
        get_url=None,
        get_blurb="The reviewer's ORCID iD as `https://orcid.org/...`; it must "
        "match the key's registry allowlist entry.",
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


async def _raise_retryable(response: httpx.Response) -> None:
    """Response hook: a 429/5xx raises so the politeness core backs off and
    retries; a 401/403 passes through to ``_classify_status`` (never retried)."""
    if response.status_code in RETRY_STATUSES:
        response.raise_for_status()


def _probe_client() -> httpx.AsyncClient:
    """The probe client: honest UA, retryable statuses raised for
    :func:`async_call_with_backoff` (see ``_safe_probe``)."""
    return httpx.AsyncClient(
        timeout=_PROBE_TIMEOUT_S,
        headers={"User-Agent": polite_user_agent()},
        event_hooks={"response": [_raise_retryable]},
    )


_QUIET_LOGGERS = ("httpx", "httpcore")
_quiet_lock = threading.Lock()
_quiet_depth = 0
_quiet_prior: list[int] = []


@contextlib.contextmanager
def _quiet_probe_logs() -> Iterator[None]:
    """Raise the httpx/httpcore loggers to WARNING for the probe call.

    httpx logs the full request URL at INFO (``HTTP Request: GET …``) and
    httpcore logs request headers at DEBUG/TRACE; the OpenAlex ``api_key``
    and Wolfram ``appid`` probes carry the key in the query string, and a
    transport exception's repr can echo the URL too. The key belongs on the
    wire, never in a log record — and this cadence now runs weekly
    unattended, so "someone notices the INFO line" is not a control.
    Refcounted so two concurrent batches (a checker job and the
    ``secret_check`` cadence in one process) can't restore the level while
    the other is still probing.
    """
    global _quiet_depth
    loggers = [logging.getLogger(n) for n in _QUIET_LOGGERS]
    with _quiet_lock:
        _quiet_depth += 1
        if _quiet_depth == 1:
            _quiet_prior[:] = [lg.level for lg in loggers]
            for lg in loggers:
                lg.setLevel(logging.WARNING)
    try:
        yield
    finally:
        with _quiet_lock:
            _quiet_depth -= 1
            if _quiet_depth == 0:
                for lg, level in zip(loggers, _quiet_prior, strict=True):
                    lg.setLevel(level)
                _quiet_prior.clear()


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


async def _probe_digikey(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    r = await client.post(
        "https://api.digikey.com/v1/oauth2/token",
        data={
            "client_id": values["PRECIS_DIGIKEY_CLIENT_ID"],
            "client_secret": values["PRECIS_DIGIKEY_CLIENT_SECRET"],
            "grant_type": "client_credentials",
        },
    )
    # invalid_client comes back as either 400 or 401 depending on which half
    # of the pair is wrong.
    return _classify_status(r.status_code, bad_codes=(400, 401, 403))


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
    if r.status_code == 429:
        return CheckResult("unknown", "HTTP 429 — rate limited")
    return _classify_status(r.status_code)


async def _probe_elsevier(
    client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    """Probe Article Retrieval auth; Search requires a separate entitlement.

    Metadata for a fixed OA article avoids mistaking unavailable full-text
    entitlement for a rejected key. Response content is never persisted.
    """
    r = await client.get(
        "https://api.elsevier.com/content/article/doi/10.1016/j.heliyon.2019.e03087",
        params={"view": "META_ABS"},
        headers={
            "X-ELS-APIKey": values["PRECIS_ELSEVIER_API_KEY"],
            "Accept": "application/json",
        },
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
    "digikey": _probe_digikey,
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

#: At most this many probes in flight at once, so opening the page (or a
#: force-refresh) does not hit a dozen vendors in the same instant.
_MAX_CONCURRENT_PROBES = 3

#: A probe target is not re-probed within this many seconds — a second pass
#: in the same burst (the weekly alert pass persisting status after its
#: report, a Verify click landing beside a cadence fire) re-serves the last
#: result instead of hammering the vendor. A changed credential bypasses it.
_MIN_PROBE_INTERVAL_S = 30.0

# Seam for tests: the per-target interval reads this, never ``time`` directly.
_clock: Callable[[], float] = time.monotonic

#: group -> (monotonic time of the last real probe, credential fingerprint,
#: result). The fingerprint is a process-local hash; secrets are never stored.
_probed: dict[str, tuple[float, int, CheckResult]] = {}


_slots: asyncio.Semaphore | None = None
_slots_loop: asyncio.AbstractEventLoop | None = None


def _probe_slots() -> asyncio.Semaphore:
    """The shared concurrency bound, (re)created per running event loop so
    importing never needs a loop and a new loop never inherits a stale one."""
    global _slots, _slots_loop
    loop = asyncio.get_running_loop()
    if _slots is None or _slots_loop is not loop:
        _slots = asyncio.Semaphore(_MAX_CONCURRENT_PROBES)
        _slots_loop = loop
    return _slots


async def _probe_group(
    group: str, client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    """:func:`_safe_probe` with the per-target minimum interval: a target
    probed less than :data:`_MIN_PROBE_INTERVAL_S` ago with the same
    credentials re-serves its last result."""
    fn = _PROBES[group]
    fingerprint = hash((fn, tuple(sorted(values.items()))))
    prior = _probed.get(group)
    now = _clock()
    if (
        prior is not None
        and prior[1] == fingerprint
        and 0 <= now - prior[0] < _MIN_PROBE_INTERVAL_S
    ):
        return prior[2]
    result = await _safe_probe(fn, client, values)
    _probed[group] = (_clock(), fingerprint, result)
    return result


async def _safe_probe(
    fn: ProbeFn, client: httpx.AsyncClient, values: dict[str, str]
) -> CheckResult:
    """Run one probe, turning any exception into an ``unknown`` result.

    Never lets a probe's exception message reach the caller unfiltered —
    only the exception *type name* is used, since ``str(exc)`` on an httpx
    error can echo back request details.
    """
    try:
        async with _probe_slots():
            # Per-probe breaker and backoff; no shared limiter row (the
            # per-target interval floor above paces probes).
            return await async_call_with_backoff(
                lambda: fn(client, values),
                service=f"probe:{fn.__name__}",
                policy=POLICIES["secret-probe"],
                provider=None,
            )
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
    # agent-supplied-URL surface. The log suppression keeps probe URLs and
    # headers (key-bearing for openalex/wolfram) out of INFO/DEBUG records.
    with _quiet_probe_logs():
        async with _probe_client() as client:
            for group, names in _PROBE_GROUP_MEMBERS.items():
                # Infra class is presence-only by policy (db-own's vault
                # class rule): no live call, not even through a shared group.
                if any(is_infra_secret(n) for n in names):
                    continue
                members = [n for n in names if is_available(n, store=store)]
                if not members:
                    continue
                if len(members) < len(names):
                    for n in members:
                        results[n] = CheckResult("bad", "partner secret missing")
                    continue
                if group == "oss_llm" and not os.environ.get("PRECIS_LLM_BASE_URL"):
                    # No endpoint configured — nothing to probe against; the
                    # key is presence-only until a base URL is set.
                    continue
                values = {n: get_secret(n, store=store) or "" for n in names}
                group_order.append(group)
                group_names[group] = names
                coros.append(_probe_group(group, client, values))

            outcomes = await asyncio.gather(*coros) if coros else []

    for group, outcome in zip(group_order, outcomes, strict=True):
        for n in group_names[group]:
            results[n] = outcome
    return results


# ── stored status ────────────────────────────────────────────────────────
#
# The checker job's durable output and the /secrets page's only read:
# ``app_state["secret_check:status"]`` holds one JSON blob — a small map
# name -> {state, detail, check, checked_at}. ``app_state`` (not a new
# column) because the vault's functions-only surface can't carry it, and
# this is exactly app_state's remit — cross-process bookkeeping rows that
# don't belong on a ref. States: ``ok`` / ``invalid`` / ``unknown`` (a real
# probe ran), ``missing`` / ``present`` (presence-only — every infra-class
# and probe_group-less secret). A secret VALUE is never part of the blob —
# same rule as CheckResult.detail.

_STATUS_KEY = "secret_check:status"

#: CheckResult.state -> stored StatusEntry.state for a probe that ran.
_STORED_STATES = {"ok": "ok", "bad": "invalid", "unknown": "unknown"}


@dataclass(frozen=True)
class StatusEntry:
    """One secret's last recorded check, as persisted in ``app_state``.

    ``state`` is ``ok`` | ``invalid`` | ``unknown`` | ``missing`` |
    ``present``; ``check`` is ``probe`` when a live probe ran or
    ``presence`` when the check was presence-only (infra class, or no
    auth-only endpoint exists — the *only* check allowed for those).
    ``detail`` is a short human string — never a secret value."""

    state: str
    detail: str
    check: str
    checked_at: datetime | None


@dataclass(frozen=True)
class StatusSnapshot:
    """The stored status blob: ``checked_at`` = when it was written."""

    checked_at: datetime | None
    entries: dict[str, StatusEntry]


def _status_entry(
    spec: SecretSpec,
    is_present: bool,
    result: CheckResult | None,
    now: datetime,
) -> StatusEntry:
    """One registry secret's stored row: probe outcome when a probe ran,
    presence otherwise. Live presence beats a stale record — the page
    re-derives presence on render, so ``missing`` here only feeds the
    stored blob's completeness."""
    if is_infra_secret(spec.name):
        return StatusEntry(
            "present" if is_present else "missing",
            "infra class — presence-only by policy, never probed",
            "presence",
            now,
        )
    if result is not None:
        # A probe outcome implies present — run_checks only probes members
        # it resolved; keep the probe verdict even if the presence read
        # disagrees (e.g. a deleted-mid-pass edge).
        return StatusEntry(_STORED_STATES[result.state], result.detail, "probe", now)
    if not is_present:
        detail = "optional, not set" if spec.optional else "not configured"
        return StatusEntry("missing", detail, "presence", now)
    if spec.probe_group is None:
        return StatusEntry(
            "present", "presence only — no auth-only probe", "presence", now
        )
    return StatusEntry("present", "presence only — probe did not run", "presence", now)


def _build_status(
    store: Store, results: dict[str, CheckResult], now: datetime
) -> StatusSnapshot:
    """Presence + probe outcomes -> the per-secret status map. Presence is
    :func:`precis.secrets.present` — no decrypt, no audit row — so the
    checker only reveals what a probe itself needs."""
    vault_rows = list_secrets(store=store)
    vault_names = {str(r["name"]) for r in vault_rows}
    entries: dict[str, StatusEntry] = {}
    for spec in KNOWN_SECRETS:
        entries[spec.name] = _status_entry(
            spec,
            present(spec.name, vault_names=vault_names),
            results.get(spec.name),
            now,
        )
    registry = {s.name for s in KNOWN_SECRETS}
    for name in sorted(vault_names - registry):
        if ":" in name:
            continue  # per-user credentials live on /account, not this page
        entries[name] = StatusEntry(
            "present",
            (
                "infra class — presence-only by policy, never probed"
                if is_infra_secret(name)
                else "presence only — no check registered"
            ),
            "presence",
            now,
        )
    return StatusSnapshot(now, entries)


def _dump_status(snap: StatusSnapshot) -> str:
    return json.dumps(
        {
            "version": 1,
            "written_at": snap.checked_at.isoformat() if snap.checked_at else None,
            "secrets": {
                name: {
                    "state": e.state,
                    "detail": e.detail,
                    "check": e.check,
                    "checked_at": e.checked_at.isoformat() if e.checked_at else None,
                }
                for name, e in snap.entries.items()
            },
        }
    )


def record_status(
    store: Store,
    *,
    results: dict[str, CheckResult] | None = None,
    now: datetime | None = None,
) -> StatusSnapshot:
    """The checker job's work: probe, presence-check, persist — the one
    writer of the /secrets page's status. Pass ``results`` when the caller
    already ran :func:`run_checks` (the weekly alert pass) so its probes
    aren't run twice."""
    if results is None:
        results = asyncio.run(run_checks(store))
    snap = _build_status(store, results, now or datetime.now(UTC))
    store.set_setting(_STATUS_KEY, _dump_status(snap))
    return snap


def stored_status(store: Store) -> StatusSnapshot:
    """Read the checker job's persisted status — the /secrets page's only
    status read. Empty snapshot when the blob is absent or unreadable; the
    page then shows "not checked yet" rather than probing."""
    try:
        raw = store.get_setting(_STATUS_KEY)
    except Exception:
        log.debug("secret_status: stored status read failed", exc_info=True)
        return StatusSnapshot(None, {})
    if not raw:
        return StatusSnapshot(None, {})
    try:
        blob = json.loads(raw)
        secrets = blob["secrets"]
        entries = {
            name: StatusEntry(
                str(e["state"]),
                str(e["detail"]),
                str(e.get("check", "presence")),
                as_utc(e["checked_at"]),
            )
            for name, e in secrets.items()
        }
        return StatusSnapshot(as_utc(blob.get("written_at")), entries)
    except Exception:
        log.warning(
            "secret_status: stored status blob is unreadable — "
            "the next checker job rewrites it"
        )
        return StatusSnapshot(None, {})


# ── weekly report ────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ReportRow:
    """One registry secret as the weekly check reports it. ``state`` is
    ``ok`` | ``failed`` | ``unprobed`` | ``missing``; ``detail`` is a
    :class:`CheckResult` detail or a fixed phrase, never a secret value."""

    name: str
    state: str
    detail: str
    get_url: str | None
    optional: bool = False
    job_types: tuple[str, ...] = ()
    #: ``(job id, finished_at)`` of the newest succeeded job of a type in
    #: ``job_types`` — only looked up for ``unprobed`` rows.
    last_job: tuple[int, datetime] | None = None


def classify(spec: SecretSpec, present: bool, result: CheckResult | None) -> ReportRow:
    """Pure mapping from presence + probe outcome to a report row.

    An inconclusive probe (timeout, rate limit, 5xx) is ``unprobed``, not
    ``failed``: the key was not shown to be bad, so nobody is asked to
    rotate it.
    """
    state, detail = "ok", "verified"
    if not present:
        state, detail = "missing", "optional, not set" if spec.optional else "not set"
    elif result is None:
        state, detail = "unprobed", "no cheap auth-only check"
    elif result.state == "bad":
        state, detail = "failed", result.detail
    elif result.state != "ok":
        state, detail = "unprobed", f"probe inconclusive: {result.detail}"
    return ReportRow(
        spec.name, state, detail, spec.get_url, spec.optional, spec.used_by_job_types
    )


def last_succeeded_job(
    store: Store, job_types: tuple[str, ...]
) -> tuple[int, datetime] | None:
    """Newest live job of one of ``job_types`` tagged ``STATUS:succeeded``."""
    if not job_types:
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT r.ref_id, r.updated_at
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'job' AND r.retired_at IS NULL
               AND r.meta->>'job_type' = ANY(%s)
               AND t.namespace = 'STATUS' AND t.value = 'succeeded'
             ORDER BY r.updated_at DESC
             LIMIT 1
            """,
            (list(job_types),),
        ).fetchone()
    return (int(row[0]), row[1]) if row else None


async def weekly_report(
    store: Store, *, results: dict[str, CheckResult] | None = None
) -> list[ReportRow]:
    """Probe every probe-able registry secret once and report all of them.

    Calls :func:`run_checks` directly (the fixed :data:`_PROBES` endpoints, no
    page cache) — or takes the caller's already-run ``results`` (the weekly
    alert pass probes once for both its report and the status record) — and,
    for each ``unprobed`` secret that jobs depend on, adds the last succeeded
    job as evidence the key still works.
    """
    if results is None:
        results = await run_checks(store)
    rows: list[ReportRow] = []
    for spec in KNOWN_SECRETS:
        row = classify(
            spec, is_available(spec.name, store=store), results.get(spec.name)
        )
        if row.state == "unprobed":
            row = replace(row, last_job=last_succeeded_job(store, row.job_types))
        rows.append(row)
    return rows


def format_report(rows: list[ReportRow]) -> str:
    """Plain-text report, one line per secret, grouped by state. Failed and
    missing (non-optional) rows carry the provider's key URL; an unprobed
    row with job dependencies names its last succeeded job."""
    order = ("failed", "missing", "unprobed", "ok")
    width = max((len(r.name) for r in rows), default=0)
    lines = [
        "secrets: " + " ".join(f"{sum(r.state == s for r in rows)} {s}" for s in order)
    ]
    for state in order:
        for r in (r for r in rows if r.state == state):
            line = f"{state:<8} {r.name:<{width}}  {r.detail}"
            if state in ("failed", "missing") and not r.optional and r.get_url:
                line += f"  — get a key: {r.get_url}"
            if state == "unprobed" and r.last_job is not None:
                job_id, at = r.last_job
                line += (
                    f"  — last succeeded job: get(kind='job', id={job_id}) "
                    f"at {at.astimezone(UTC):%Y-%m-%dT%H:%MZ}"
                )
            elif state == "unprobed" and r.job_types:
                line += f"  — no succeeded {'/'.join(r.job_types)} job on record"
            elif state == "unprobed":
                line += "  — no job records its use"
            lines.append(line)
    return "\n".join(lines)


__all__ = [
    "KNOWN_SECRETS",
    "CheckResult",
    "ProbeFn",
    "ReportRow",
    "SecretSpec",
    "StatusEntry",
    "StatusSnapshot",
    "classify",
    "format_report",
    "last_succeeded_job",
    "record_status",
    "run_checks",
    "stored_status",
    "weekly_report",
]
