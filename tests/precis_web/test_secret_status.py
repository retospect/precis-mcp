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
from typing import TYPE_CHECKING, Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis import secrets as vault
from precis_web import secret_status

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


def test_every_probe_group_has_a_registered_probe() -> None:
    groups = {s.probe_group for s in secret_status.KNOWN_SECRETS if s.probe_group}
    assert groups  # sanity: the registry does define probed secrets
    assert groups <= set(secret_status._PROBES.keys())


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


# ── (d) index route rendering ──────────────────────────────────────────────


def test_index_renders_missing_and_verified_dots(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "list_secrets", lambda *, store: [])

    def fake_is_available(name: str, *, store: Any = None) -> bool:
        return name == "ANTHROPIC_API_KEY"

    monkeypatch.setattr(vault, "is_available", fake_is_available)

    async def fake_get_results(
        store: Any, *, max_age_s: float = 900.0, force: bool = False
    ) -> dict[str, secret_status.CheckResult]:
        return {"ANTHROPIC_API_KEY": secret_status.CheckResult("ok", "verified")}

    monkeypatch.setattr(secret_status, "get_results", fake_get_results)
    monkeypatch.setattr(secret_status, "checked_at", lambda: None)

    r = client.get("/secrets")
    assert r.status_code == 200
    # Present + verified -> emerald dot.
    assert "bg-emerald-500" in r.text
    # Every other known secret is absent -> at least one rose dot.
    assert "bg-rose-500" in r.text
    assert "missing" in r.text
    assert "verified" in r.text
    # Cost badges render for known rows — Anthropic's is paid, S2's free.
    assert "paid — usage-billed" in r.text
    assert "bg-amber-50" in r.text
    assert "bg-emerald-50" in r.text


def test_index_renders_amber_for_bad_probe(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "list_secrets", lambda *, store: [])

    def fake_is_available(name: str, *, store: Any = None) -> bool:
        return name == "PERPLEXITY_API_KEY"

    monkeypatch.setattr(vault, "is_available", fake_is_available)

    async def fake_get_results(
        store: Any, *, max_age_s: float = 900.0, force: bool = False
    ) -> dict[str, secret_status.CheckResult]:
        return {
            "PERPLEXITY_API_KEY": secret_status.CheckResult(
                "bad", "HTTP 401 — key rejected"
            )
        }

    monkeypatch.setattr(secret_status, "get_results", fake_get_results)
    monkeypatch.setattr(secret_status, "checked_at", lambda: None)

    r = client.get("/secrets")
    assert r.status_code == 200
    assert "bg-amber-500" in r.text
    # The blurb/get-link surfaces for a not-fully-verified known secret.
    assert "perplexity.ai" in r.text


def test_check_now_forces_refresh_and_redirects(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[bool] = []

    async def fake_get_results(
        store: Any, *, max_age_s: float = 900.0, force: bool = False
    ) -> dict[str, secret_status.CheckResult]:
        calls.append(force)
        return {}

    monkeypatch.setattr(secret_status, "get_results", fake_get_results)

    r = client.post("/secrets/check", follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == "/secrets"
    assert calls == [True]


def test_unknown_vault_extra_present_dot_is_emerald(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import UTC, datetime

    monkeypatch.setattr(
        vault,
        "list_secrets",
        lambda *, store: [
            {
                "name": "SOME_UNLISTED_SECRET",
                "hint": "sk-…abc",
                "updated_at": datetime(2026, 7, 1, tzinfo=UTC),
            }
        ],
    )
    monkeypatch.setattr(vault, "is_available", lambda name, *, store=None: False)

    async def fake_get_results(
        store: Any, *, max_age_s: float = 900.0, force: bool = False
    ) -> dict[str, secret_status.CheckResult]:
        return {}

    monkeypatch.setattr(secret_status, "get_results", fake_get_results)
    monkeypatch.setattr(secret_status, "checked_at", lambda: None)

    r = client.get("/secrets")
    assert r.status_code == 200
    assert "SOME_UNLISTED_SECRET" in r.text
    assert "bg-emerald-500" in r.text
