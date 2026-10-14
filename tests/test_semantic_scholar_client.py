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


# ── get_paper_by_id: not-found vs resolver fault (gr483930) ─────────
#
# Regression for the 2026-10-14 outage: every exception used to collapse
# to ``None``, so an S2 outage reported real DOIs as "did not resolve —
# likely hallucinated". ``propagate=True`` narrows None to its true
# meaning — S2 answered "no such paper" — and raises S2ResolverError on
# every other failure. The exception classes below are the ones the
# ``semanticscholar`` lib actually raises for each HTTP outcome:
# ObjectNotFoundException on 404, ConnectionRefusedError on 429,
# InternalServerErrorException / GatewayTimeoutException on 5xx —
# ``ApiRequester.get_data_async``.


class _GetFakeSch:
    """Records ``get_paper`` calls; raises or returns a canned result."""

    def __init__(self, *, result: Any = None, exc: Exception | None = None) -> None:
        self.result = result
        self.exc = exc
        self.calls = 0

    def get_paper(self, paper_id: str) -> Any:
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.result


def _wire_get_fake(monkeypatch: pytest.MonkeyPatch, fake: _GetFakeSch) -> None:
    monkeypatch.setattr(s2mod, "_client", lambda api_key="": fake)
    # Bypass the tenacity wrapper — its retry classification is covered
    # by external_retry's own tests; here we pin the classification of
    # the exception it finally reraises.
    monkeypatch.setattr(
        s2mod,
        "_get_with_retry",
        lambda sch, paper_id: sch.get_paper(paper_id),
    )
    monkeypatch.setattr(s2mod, "acquire_rate_limit", lambda name: None)


def test_get_paper_by_id_propagate_returns_meta_on_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paper = SimpleNamespace(paperId="P1", title="Real Paper")
    fake = _GetFakeSch(result=paper)
    _wire_get_fake(monkeypatch, fake)
    out = s2mod.get_paper_by_id("doi:10.1038/nature10352", propagate=True)
    assert out is not None and out["title"] == "Real Paper"
    assert fake.calls == 1


def test_get_paper_by_id_propagate_not_found_is_none_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A genuine 404 stays a plain None under propagate=True, and IS
    cached — evidence of absence is stable for the TTL."""
    from semanticscholar.SemanticScholarException import ObjectNotFoundException

    fake = _GetFakeSch(exc=ObjectNotFoundException("Paper not found"))
    _wire_get_fake(monkeypatch, fake)
    assert s2mod.get_paper_by_id("doi:10.9999/nope", propagate=True) is None
    assert s2mod.get_paper_by_id("doi:10.9999/nope", propagate=True) is None
    assert fake.calls == 1  # cached miss — no second round trip


def test_get_paper_by_id_propagate_fault_raises_and_is_not_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A resolver fault raises S2ResolverError and is NOT cached — an
    outage must not poison the cache for the TTL after recovery."""
    fake = _GetFakeSch(exc=ConnectionRefusedError("HTTP status 429 Too Many Requests."))
    _wire_get_fake(monkeypatch, fake)
    with pytest.raises(s2mod.S2ResolverError, match="doi:10.1/x"):
        s2mod.get_paper_by_id("doi:10.1/x", propagate=True)
    with pytest.raises(s2mod.S2ResolverError):
        s2mod.get_paper_by_id("doi:10.1/x", propagate=True)
    assert fake.calls == 2  # every attempt hits the client — no cached miss


def test_get_paper_by_id_lenient_fault_still_none_uncached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default propagate=False keeps the graceful-degrade contract for
    the ingest call sites — fault → None — but likewise never caches."""
    from semanticscholar.SemanticScholarException import (
        InternalServerErrorException,
    )

    fake = _GetFakeSch(exc=InternalServerErrorException("boom"))
    _wire_get_fake(monkeypatch, fake)
    assert s2mod.get_paper_by_id("doi:10.1/x") is None
    assert s2mod.get_paper_by_id("doi:10.1/x") is None
    assert fake.calls == 2


def test_get_paper_by_id_empty_paper_counts_as_not_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """S2's odd 200-with-``{}`` body yields a Paper with no paperId —
    an answer of "no such record", not a resolver fault."""
    fake = _GetFakeSch(result=SimpleNamespace(paperId=None))
    _wire_get_fake(monkeypatch, fake)
    assert s2mod.get_paper_by_id("doi:10.1/x", propagate=True) is None
