"""GitHub REST readers authenticate with the factory token.

``factory_git.github_api_token`` resolves ``GH_TOKEN`` from the env, else
the vault's factory token; every in-process api.github.com reader
(``pr_class`` for ``pr_poll`` and ``release_poll``, ``ci_verdict_poll``,
``upstream_watch``) sends it, so the workers stop sharing GitHub's
unauthenticated 60/h for one IP. A rate-limit 403 names its quota headers,
never the token or the IP. No test reaches the network or the vault.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from precis.plans import pr_class
from precis.utils import http as _http
from precis.workers import factory_git as fg
from precis.workers import upstream_watch as uw
from precis.workers.job_types import ci_verdict_poll as cvp
from precis.workers.job_types import release_poll as rp

REPO = "o/r"
#: Built at runtime so the repo's credential scan sees no token literal.
VAULT_TOKEN = "vault-" + "tok-123"


@pytest.fixture(autouse=True)
def _clean(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv(fg.SECRET_NAME, raising=False)
    monkeypatch.setattr(fg, "_unauth_logged", False)
    _http.reset_circuit()


@pytest.fixture
def vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fg, "factory_git_token", lambda: VAULT_TOKEN)


@pytest.fixture
def no_vault(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fg, "factory_git_token", lambda: None)


def _ok(data: Any) -> Any:
    return SimpleNamespace(status_code=200, json=lambda: data)


def _capture(monkeypatch: pytest.MonkeyPatch, module: Any, data: Any) -> list[dict]:
    """Patch ``module._http_get``; record the headers each call sent."""
    seen: list[dict] = []

    def get(url: str, params: dict[str, Any], headers: dict[str, str]) -> Any:
        seen.append(dict(headers))
        return _ok(data)

    monkeypatch.setattr(module, "_http_get", get)
    return seen


def _read_pr_poll(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen = _capture(monkeypatch, pr_class, [])
    assert pr_class.fetch_open_pulls(REPO) == []
    return seen


def _read_release_poll(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen = _capture(monkeypatch, pr_class, [])
    url = f"{rp._API.format(repo=REPO)}/git/matching-refs/heads/release/"
    assert rp._matching_refs(url, rp._HEAD_RE, 10) == {}
    return seen


def _read_ci_verdict_poll(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    seen = _capture(monkeypatch, cvp, {"workflow_runs": []})
    assert cvp.fetch_runs(REPO, "a" * 40) == []
    return seen


def _read_upstream_watch(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    return [uw._default_github()._headers()]


READERS = {
    "pr_poll": _read_pr_poll,
    "release_poll": _read_release_poll,
    "ci_verdict_poll": _read_ci_verdict_poll,
    "upstream_watch": _read_upstream_watch,
}


# ── the resolver ───────────────────────────────────────────────────


def test_env_gh_token_wins_over_the_vault(
    vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GH_TOKEN", " env-tok ")
    assert fg.github_api_token() == "env-tok"


def test_vault_token_when_env_lacks_it(vault: None) -> None:
    assert fg.github_api_token() == VAULT_TOKEN


def test_neither_is_none_and_logs_once(
    no_vault: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.INFO, logger=fg.__name__):
        assert fg.github_api_token() is None
        assert fg.github_api_token() is None
    lines = [r for r in caplog.records if "unauthenticated" in r.getMessage()]
    assert len(lines) == 1
    assert lines[0].levelno == logging.INFO


def test_a_raising_vault_reads_as_no_token(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom() -> str:
        raise RuntimeError("vault down")

    monkeypatch.setattr(fg, "factory_git_token", boom)
    assert fg.github_api_token() is None
    assert "Authorization" not in fg.github_api_headers()


# ── every reader ───────────────────────────────────────────────────


@pytest.mark.parametrize("reader", sorted(READERS))
def test_reader_sends_the_vault_token(
    reader: str, vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = READERS[reader](monkeypatch)
    assert seen
    assert all(h["Authorization"] == f"Bearer {VAULT_TOKEN}" for h in seen)


@pytest.mark.parametrize("reader", sorted(READERS))
def test_reader_without_a_token_sends_no_header(
    reader: str, no_vault: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = READERS[reader](monkeypatch)
    assert seen
    assert all("Authorization" not in h for h in seen)


@pytest.mark.parametrize("reader", sorted(READERS))
def test_reader_never_logs_the_token(
    reader: str,
    vault: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        READERS[reader](monkeypatch)
    assert VAULT_TOKEN not in caplog.text


# ── a rate-limit 403 reads clearly ─────────────────────────────────


#: An address from a documentation range, assembled at runtime.
_IP = ".".join(["203", "0", "113", "7"])
_RESET = 1_794_000_000


def _rate_limited(url: str) -> httpx.Response:
    return httpx.Response(
        403,
        headers={
            "X-RateLimit-Limit": "60",
            "X-RateLimit-Remaining": "0",
            "X-RateLimit-Reset": str(_RESET),
        },
        text=f'{{"message":"API rate limit exceeded for {_IP}."}}',
        request=httpx.Request("GET", url),
    )


@pytest.fixture
def rate_limited(monkeypatch: pytest.MonkeyPatch, vault: None) -> None:
    """The real ``with_backoff`` around a 403 rate-limit answer; the
    limiter is stubbed so no store is consulted."""
    monkeypatch.setattr(_http, "_take_token", lambda *a, **k: None)
    monkeypatch.setattr(
        pr_class, "_send", lambda url, params, headers: _rate_limited(url)
    )


def test_rate_limit_403_names_its_quota_not_the_ip(
    rate_limited: None, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(pr_class.GitHubUnreadable) as excinfo:
            pr_class.fetch_open_pulls(REPO)
    msg = str(excinfo.value)
    assert "rate limit exceeded" in msg
    assert "remaining=0" in msg and "limit=60" in msg
    assert "reset=2026-11-06T21:20:00Z" in msg
    refusal = [
        r.getMessage() for r in caplog.records if "refused HTTP" in r.getMessage()
    ]
    assert refusal and "remaining=0" in refusal[0] and "reset=" in refusal[0]
    for text in (msg, caplog.text):
        assert _IP not in text
        assert VAULT_TOKEN not in text


def test_plain_403_keeps_the_auth_line() -> None:
    def send() -> httpx.Response:
        return httpx.Response(
            403,
            headers={"X-RateLimit-Remaining": "4999"},
            text='{"message":"Resource not accessible by integration"}',
        )

    with pytest.raises(_http.VendorError) as excinfo:
        _http.with_backoff(
            send,
            service="github-plain-403",
            provider=None,
            sleep=lambda s: None,
        )
    assert "auth/permission failure" in str(excinfo.value)
    assert "rate limit" not in str(excinfo.value)
