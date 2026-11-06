"""Tests for the `/secrets` registry + verify-probe layer.

No live network calls anywhere here: (a)/(b) exercise pure data/logic with
no I/O; (c) drives ``run_checks`` against monkeypatched
``is_available``/``get_secret``/probe callables; (d) drives the route with
``secret_status.get_results`` monkeypatched to a canned coroutine. The probe
HTTP functions themselves (``_probe_anthropic`` et al.) are never invoked
with a real ``httpx.AsyncClient`` in this file.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from html import unescape
from typing import TYPE_CHECKING, Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis import secret_status
from precis import secrets as vault

if TYPE_CHECKING:
    from precis.store import Store

#: A store-shaped stand-in for run_checks's ``store`` param in the (c) tests
#: below — those tests monkeypatch is_available/get_secret directly, so the
#: store itself is never actually touched; it only needs to type-check.
_FAKE_STORE = cast("Store", object())

# ── (a) registry hygiene ───────────────────────────────────────────────────


def test_registry_names_are_unique() -> None:
    names = [s.name for s in secret_status.KNOWN_SECRETS]
    assert len(names) == len(set(names))


def test_every_spec_has_purpose_and_blurb() -> None:
    for spec in secret_status.KNOWN_SECRETS:
        assert spec.purpose.strip()
        assert spec.get_blurb.strip()


def test_every_spec_has_cost_note() -> None:
    # The template colors the badge by prefix: "free…" green, else amber.
    for spec in secret_status.KNOWN_SECRETS:
        assert spec.cost.strip()


def test_mp_api_key_is_listed_presence_only() -> None:
    # The pourbaix_bulk job's key (precis.workers.job_types.pourbaix_bulk
    # .MP_API_KEY_SECRET) must show on /secrets so it can be provisioned.
    from precis.workers.job_types.pourbaix_bulk import MP_API_KEY_SECRET

    (spec,) = [s for s in secret_status.KNOWN_SECRETS if s.name == MP_API_KEY_SECRET]
    assert spec.probe_group is None
    assert "pourbaix_bulk" in spec.purpose


def test_web_base_url_is_listed_presence_only() -> None:
    # The file-links origin (precis.file_links.WEB_BASE_URL_SECRET) must
    # show on /secrets so the operator can provision it — unregistered, an
    # unset secret has no write row and its absence surfaces only as
    # path-only links (gr524008). Non-optional: on a deployment serving
    # file links an absent origin is a misconfiguration, not a normal one.
    from precis.file_links import WEB_BASE_URL_SECRET

    (spec,) = [s for s in secret_status.KNOWN_SECRETS if s.name == WEB_BASE_URL_SECRET]
    assert spec.probe_group is None
    assert not spec.optional


def test_every_probe_group_has_a_registered_probe() -> None:
    groups = {s.probe_group for s in secret_status.KNOWN_SECRETS if s.probe_group}
    assert groups  # sanity: the registry does define probed secrets
    assert groups <= set(secret_status._PROBES.keys())


@pytest.mark.parametrize(
    "name, group",
    [("GROQ_API_KEY", "groq"), ("CEREBRAS_API_KEY", "cerebras")],
)
def test_groq_cerebras_specs_are_optional_free_and_probed(
    name: str, group: str
) -> None:
    (spec,) = [s for s in secret_status.KNOWN_SECRETS if s.name == name]
    assert spec.optional
    assert spec.cost.startswith("free")
    assert spec.probe_group == group


# ── (b) result-classification helper ───────────────────────────────────────


@pytest.mark.parametrize(
    "status, expected_state",
    [
        (200, "ok"),
        (201, "ok"),
        (299, "ok"),
        (401, "bad"),
        (403, "bad"),
        (404, "unknown"),
        (500, "unknown"),
        (429, "unknown"),
    ],
)
def test_classify_status_default_bad_codes(status: int, expected_state: str) -> None:
    result = secret_status._classify_status(status)
    assert result.state == expected_state


def test_classify_status_custom_bad_codes() -> None:
    # openalex only treats 403 as bad; a 401 falls to unknown.
    assert secret_status._classify_status(403, bad_codes=(403,)).state == "bad"
    assert secret_status._classify_status(401, bad_codes=(403,)).state == "unknown"
    assert secret_status._classify_status(200, bad_codes=(403,)).state == "ok"


def test_classify_status_detail_never_echoes_status_specific_body() -> None:
    # detail is always a short, synthesized string — never anything from a
    # response body/header.
    result = secret_status._classify_status(401)
    assert "HTTP 401" in result.detail
    assert result.detail == "HTTP 401 — key rejected"


# ── (c) run_checks with monkeypatched probes ───────────────────────────────


def _patch_presence(monkeypatch: pytest.MonkeyPatch, present: dict[str, str]) -> None:
    """Make ``secret_status.is_available``/``get_secret`` report exactly the
    given {name: value} map as present, everything else absent — no store,
    no vault, no env/file lookups."""

    def fake_is_available(name: str, *, store: Any = None) -> bool:
        return name in present

    def fake_get_secret(name: str, *, store: Any = None, default: Any = None) -> Any:
        return present.get(name, default)

    monkeypatch.setattr(secret_status, "is_available", fake_is_available)
    monkeypatch.setattr(secret_status, "get_secret", fake_get_secret)


def test_run_checks_joint_group_fanout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_presence(
        monkeypatch,
        {"EPO_OPS_CLIENT_KEY": "k", "EPO_OPS_CLIENT_SECRET": "s"},
    )
    calls: list[dict[str, str]] = []

    async def fake_epo_probe(
        client: Any, values: dict[str, str]
    ) -> secret_status.CheckResult:
        calls.append(values)
        return secret_status.CheckResult("ok", "verified")

    monkeypatch.setitem(secret_status._PROBES, "epo", fake_epo_probe)

    results = asyncio.run(secret_status.run_checks(store=_FAKE_STORE))

    assert results == {
        "EPO_OPS_CLIENT_KEY": secret_status.CheckResult("ok", "verified"),
        "EPO_OPS_CLIENT_SECRET": secret_status.CheckResult("ok", "verified"),
    }
    # One probe call covers both halves of the pair.
    assert len(calls) == 1
    assert calls[0] == {"EPO_OPS_CLIENT_KEY": "k", "EPO_OPS_CLIENT_SECRET": "s"}


def test_run_checks_missing_partner(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_presence(monkeypatch, {"EPO_OPS_CLIENT_KEY": "k"})
    called = False

    async def fake_epo_probe(
        client: Any, values: dict[str, str]
    ) -> secret_status.CheckResult:
        nonlocal called
        called = True
        return secret_status.CheckResult("ok", "verified")

    monkeypatch.setitem(secret_status._PROBES, "epo", fake_epo_probe)

    results = asyncio.run(secret_status.run_checks(store=_FAKE_STORE))

    assert results == {
        "EPO_OPS_CLIENT_KEY": secret_status.CheckResult("bad", "partner secret missing")
    }
    assert "EPO_OPS_CLIENT_SECRET" not in results
    assert called is False  # no network call for an incomplete pair


def test_run_checks_absent_secret_is_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_presence(monkeypatch, {})

    results = asyncio.run(secret_status.run_checks(store=_FAKE_STORE))

    assert results == {}


def test_llm_subscription_secrets_are_never_resolved_or_probed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = {"KIMI_CODE_API_KEY", "OPENAI_API_KEY", "DEVIN_API_TOKEN"}
    monkeypatch.setattr(
        secret_status, "is_available", lambda name, *, store: name in names
    )

    def no_resolve(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("presence-only secrets must not be decrypted")

    async def no_probe(*args: Any, **kwargs: Any) -> secret_status.CheckResult:
        raise AssertionError("presence-only secrets must not be probed")

    monkeypatch.setattr(secret_status, "get_secret", no_resolve)
    monkeypatch.setattr(secret_status, "_probe_group", no_probe)

    assert asyncio.run(secret_status.run_checks(store=_FAKE_STORE)) == {}


def test_run_checks_single_member_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_presence(monkeypatch, {"ANTHROPIC_API_KEY": "sk-x"})

    async def fake_probe(
        client: Any, values: dict[str, str]
    ) -> secret_status.CheckResult:
        assert values == {"ANTHROPIC_API_KEY": "sk-x"}
        return secret_status.CheckResult("bad", "HTTP 401 — key rejected")

    monkeypatch.setitem(secret_status._PROBES, "anthropic", fake_probe)

    results = asyncio.run(secret_status.run_checks(store=_FAKE_STORE))

    assert results == {
        "ANTHROPIC_API_KEY": secret_status.CheckResult("bad", "HTTP 401 — key rejected")
    }


def test_run_checks_groq_single_member_group(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_presence(monkeypatch, {"GROQ_API_KEY": "synthetic"})

    async def fake_probe(
        client: Any, values: dict[str, str]
    ) -> secret_status.CheckResult:
        assert values == {"GROQ_API_KEY": "synthetic"}
        return secret_status.CheckResult("ok", "verified")

    monkeypatch.setitem(secret_status._PROBES, "groq", fake_probe)

    results = asyncio.run(secret_status.run_checks(store=_FAKE_STORE))

    assert results == {"GROQ_API_KEY": secret_status.CheckResult("ok", "verified")}


# ── (d) index route rendering ──────────────────────────────────────────────

_NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)


def _snapshot(
    entries: dict[str, secret_status.StatusEntry],
) -> secret_status.StatusSnapshot:
    return secret_status.StatusSnapshot(_NOW, entries)


def _entry(state: str, detail: str, check: str = "probe") -> secret_status.StatusEntry:
    return secret_status.StatusEntry(state, detail, check, _NOW)


def _patch_page(
    monkeypatch: pytest.MonkeyPatch,
    *,
    present_names: set[str],
    snap: secret_status.StatusSnapshot,
) -> None:
    """The route's three inputs canned: vault listing empty, presence per
    ``present_names``, stored status as given — and the job-query helper
    quieted."""
    monkeypatch.setattr(vault, "list_secrets", lambda *, store: [])
    monkeypatch.setattr(
        vault,
        "present",
        lambda name, *, vault_names=None, store=None: name in present_names,
    )
    monkeypatch.setattr(secret_status, "stored_status", lambda store: snap)
    monkeypatch.setattr(
        "precis.workers.secret_check.pending_status_check", lambda store: False
    )


@pytest.mark.parametrize(
    "name, get_url, probe_group, instructions",
    [
        (
            "OPENROUTER_API_KEY",
            "https://openrouter.ai/settings/keys",
            None,
            ("Create a key",),
        ),
        (
            "DEEPINFRA_API_KEY",
            "https://deepinfra.com/dash/api_keys",
            None,
            ("Create an API key",),
        ),
        (
            "GROQ_API_KEY",
            "https://console.groq.com/keys",
            "groq",
            ("zero data retention",),
        ),
        (
            "CEREBRAS_API_KEY",
            "https://cloud.cerebras.ai/",
            "cerebras",
            ("data-retention and training",),
        ),
        (
            "KIMI_CODE_API_KEY",
            "https://www.kimi.com/coding/docs/en/",
            None,
            (
                "Kimi Code Console",
                "up to 5",
                "real User-Agent",
                "never impersonate",
                "key auth only",
                "CLI OAuth",
                "Interactive calls only",
                "never autonomous worker/job ops",
                "product integration",
                "Membership Benefits",
                "Community Guidelines",
                "before enabling",
                "retention and training",
            ),
        ),
        (
            "OPENAI_API_KEY",
            "https://platform.openai.com/api-keys",
            None,
            (
                "Optional OpenAI Platform API key",
                "host-login entries",
                "codex login",
                "each host",
                "never extract tokens",
                "never copy an auth file",
            ),
        ),
        (
            "DEVIN_API_TOKEN",
            "https://docs.devin.ai/api-reference/overview",
            None,
            (
                "first confirm the plan includes the Devin API",
                "service user",
                "cog_",
                "personal access token",
                "never buy overage",
            ),
        ),
    ],
)
def test_llm_secret_entries_render_with_instructions(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    get_url: str,
    probe_group: str | None,
    instructions: tuple[str, ...],
) -> None:
    _patch_page(monkeypatch, present_names=set(), snap=_snapshot({}))

    response = client.get("/secrets")

    assert response.status_code == 200
    page = unescape(response.text)
    (spec,) = [s for s in secret_status.KNOWN_SECRETS if s.name == name]
    assert spec.probe_group == probe_group
    assert spec.optional
    assert spec.get_url == get_url
    assert f'name="name" value="{name}"' in page
    assert f'href="{get_url}"' in page
    assert spec.get_blurb in page
    assert spec.purpose in page
    row = page.split(f'value="{name}"', 1)[0].rsplit("<tr>", 1)[1]
    badge = (
        "bg-success-subtle text-success-strong"
        if spec.cost.startswith("free")
        else "bg-warning-subtle text-warning-strong"
    )
    assert badge in row
    for instruction in instructions:
        assert instruction in spec.get_blurb


def test_index_renders_missing_and_verified_dots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_page(
        monkeypatch,
        present_names={"ANTHROPIC_API_KEY"},
        snap=_snapshot({"ANTHROPIC_API_KEY": _entry("ok", "verified")}),
    )

    r = client.get("/secrets")
    assert r.status_code == 200
    # Present + verified -> emerald dot.
    assert "bg-success-solid-soft" in r.text
    # Every other known secret is absent -> at least one rose dot.
    assert "bg-danger-solid-soft" in r.text
    assert "missing" in r.text
    assert "verified" in r.text
    # The stored per-secret check line renders as "checked <ago>: <state>".
    assert "checked" in r.text and ": ok" in r.text
    # Cost badges render for known rows — Anthropic's is paid, S2's free.
    assert "paid — usage-billed" in r.text
    assert "bg-warning-subtle" in r.text
    assert "bg-success-subtle" in r.text


def test_index_makes_no_outbound_call(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The render path must never probe — a raising run_checks proves it."""
    _patch_page(monkeypatch, present_names=set(), snap=_snapshot({}))

    def boom(_store: Any) -> dict[str, secret_status.CheckResult]:
        raise AssertionError("run_checks called from the page")

    monkeypatch.setattr(secret_status, "run_checks", boom)

    r = client.get("/secrets")
    assert r.status_code == 200


def test_index_renders_amber_for_bad_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_page(
        monkeypatch,
        present_names={"PERPLEXITY_API_KEY"},
        snap=_snapshot(
            {"PERPLEXITY_API_KEY": _entry("invalid", "HTTP 401 — key rejected")}
        ),
    )

    r = client.get("/secrets")
    assert r.status_code == 200
    assert "bg-warning-solid-soft" in r.text
    assert ": invalid" in r.text
    # The blurb/get-link surfaces for a not-fully-verified known secret.
    assert "perplexity.ai" in r.text


def test_check_now_mints_a_job_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mints: list[dict[str, Any]] = []

    def fake_mint(store: Any, *, origin: str, title: str) -> int:
        mints.append({"origin": origin, "title": title})
        return 42

    monkeypatch.setattr("precis.workers.secret_check.mint_status_job", fake_mint)

    r = client.post("/secrets/check", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/secrets"
    assert len(mints) == 1
    assert mints[0]["origin"] == "human"


def test_unknown_vault_extra_present_dot_is_emerald(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        vault,
        "list_secrets",
        lambda *, store: [
            {
                "name": "SOME_UNLISTED_SECRET",
                "hint": "sk-…abc",
                "updated_at": _NOW,
            }
        ],
    )
    monkeypatch.setattr(
        vault, "present", lambda name, *, vault_names=None, store=None: False
    )
    monkeypatch.setattr(secret_status, "stored_status", lambda store: _snapshot({}))
    monkeypatch.setattr(
        "precis.workers.secret_check.pending_status_check", lambda store: False
    )

    r = client.get("/secrets")
    assert r.status_code == 200
    assert "SOME_UNLISTED_SECRET" in r.text
    assert "bg-success-solid-soft" in r.text


def test_elsevier_probe_uses_article_retrieval_not_search() -> None:
    import httpx

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await secret_status._probe_elsevier(
                client, {"PRECIS_ELSEVIER_API_KEY": "synthetic"}
            )

    assert asyncio.run(run()).state == "ok"
    assert "/content/article/doi/10.1016/" in str(requests[0].url)
    assert "/search/" not in str(requests[0].url)


def test_core_probe_429_is_rate_limited_with_code() -> None:
    import httpx

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(429))
        ) as client:
            return await secret_status._probe_core(
                client, {"PRECIS_CORE_API_KEY": "synthetic"}
            )

    result = asyncio.run(run())
    assert "429" in result.detail
    assert "rate limited" in result.detail


@pytest.mark.parametrize(
    "probe_name, secret_name, url",
    [
        ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1/models"),
        ("cerebras", "CEREBRAS_API_KEY", "https://api.cerebras.ai/v1/models"),
    ],
    ids=["groq", "cerebras"],
)
def test_groq_cerebras_probes_get_models_with_bearer(
    probe_name: str, secret_name: str, url: str
) -> None:
    import httpx

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            return await secret_status._PROBES[probe_name](
                client, {secret_name: "synthetic"}
            )

    assert asyncio.run(run()).state == "ok"
    assert len(requests) == 1
    assert requests[0].method == "GET"
    assert str(requests[0].url) == url
    assert requests[0].headers["Authorization"] == "Bearer synthetic"


@pytest.mark.parametrize(
    "probe_name, secret_name",
    [("groq", "GROQ_API_KEY"), ("cerebras", "CEREBRAS_API_KEY")],
    ids=["groq", "cerebras"],
)
@pytest.mark.parametrize(
    "status, expected_state",
    [(401, "bad"), (403, "bad"), (429, "unknown"), (500, "unknown")],
)
def test_groq_cerebras_probe_status_mapping(
    probe_name: str, secret_name: str, status: int, expected_state: str
) -> None:
    import httpx

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(status))
        ) as client:
            return await secret_status._PROBES[probe_name](
                client, {secret_name: "synthetic"}
            )

    assert asyncio.run(run()).state == expected_state
