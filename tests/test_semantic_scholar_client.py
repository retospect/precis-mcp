"""gr459597: one keyed, non-nested-retry S2 client, a TTL cache, 429 fail-fast."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.ingest import semantic_scholar as s2mod


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    s2mod._cache.clear()


class _FakeSch:
    calls = 0

    def __init__(
        self, *, items: list[Any] | None = None, exc: Exception | None = None
    ) -> None:
        self.items = items or []
        self.exc = exc

    def search_paper(self, query: str, limit: int = 3) -> Any:
        type(self).calls += 1
        if self.exc:
            raise self.exc
        return SimpleNamespace(items=self.items)


def test_client_uses_vault_key_and_disables_lib_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Any] = {}

    class _Cls:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    monkeypatch.setattr(s2mod, "SemanticScholar", _Cls)
    monkeypatch.setattr("precis.secrets.get_secret", lambda name: "vault-key")
    s2mod._client("")
    assert seen == {"api_key": "vault-key", "retry": False}
    s2mod._client("explicit")
    assert seen["api_key"] == "explicit"


def test_client_keyless_when_no_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    class _Cls:
        def __init__(self, **kw: Any) -> None:
            seen.update(kw)

    monkeypatch.setattr(s2mod, "SemanticScholar", _Cls)
    monkeypatch.setattr("precis.secrets.get_secret", lambda name: None)
    s2mod._client("")
    assert seen == {"api_key": None, "retry": False}


def test_search_cache_second_query_no_second_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSch.calls = 0
    fake = _FakeSch(items=[SimpleNamespace(title="T", paperId="p1")])
    monkeypatch.setattr(s2mod, "_client", lambda api_key="": fake)
    monkeypatch.setattr(s2mod, "acquire_rate_limit", lambda name: None)
    a = s2mod.search_s2_papers("Foo  Bar", limit=2)
    b = s2mod.search_s2_papers("foo bar", limit=2)
    assert a == b and a[0]["title"] == "T"
    assert _FakeSch.calls == 1


def test_search_429_returns_empty_fast_and_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _FakeSch.calls = 0
    fake = _FakeSch(exc=ConnectionRefusedError("429"))
    monkeypatch.setattr(s2mod, "_client", lambda api_key="": fake)
    monkeypatch.setattr(s2mod, "acquire_rate_limit", lambda name: None)
    assert s2mod.search_s2_papers("q") == []
    assert _FakeSch.calls == 1  # no backoff loop
    assert not s2mod._cache


def test_hub_refine_probe_s2_rate_limit_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.workers import hub_refine

    def _boom(*a: Any, **k: Any) -> list[dict[str, Any]]:
        raise ConnectionRefusedError("429")

    monkeypatch.setattr(s2mod, "search_s2_papers", _boom)
    assert hub_refine._probe_s2("claim") == []
