"""gr503080: lookup_s2 must back off on 429 and fail as a typed deferral.

The ``semanticscholar`` lib maps HTTP 429 to ``ConnectionRefusedError`` and —
even with ``retry=False`` — wraps it in ``tenacity.RetryError`` (its
``retry_with(stop_after_attempt(1))`` stub still routes the raise through
tenacity). Before the unwrap in ``utils.http._status_of``, that wrapped 429
classified as non-retryable, so a single refusal failed the lookup outright
and surfaced at the sweep level as a plain error (16 lost lookups in the
2026-10-21 resolve-metadata run).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from tenacity import Future, RetryError

from precis.ingest import semantic_scholar as s2mod
from precis.utils.http import reset_circuit


@pytest.fixture(autouse=True)
def _clean_s2_state(monkeypatch: pytest.MonkeyPatch) -> None:
    s2mod._cache.clear()
    reset_circuit()
    monkeypatch.setattr("precis.utils.http._take_token", lambda *a, **k: None)


def _retry_wrapped(exc: BaseException) -> RetryError:
    """The lib's real failure shape: the exception tenacity re-raises after
    its (stubbed) retry loop — ``RetryError.last_attempt`` holding it."""
    fut = Future(attempt_number=1)
    fut.set_exception(exc)
    return RetryError(fut)


def _exc_429(*, wrapped: bool = True) -> BaseException:
    exc: BaseException = ConnectionRefusedError("HTTP status 429 Too Many Requests.")
    return _retry_wrapped(exc) if wrapped else exc


class _FlakySch:
    """search_paper: raise each of ``errors`` in order, then return the page."""

    def __init__(self, errors: list[BaseException], items: list[Any]) -> None:
        self.errors = list(errors)
        self.items = items
        self.calls = 0

    def search_paper(self, query: str, limit: int = 3) -> Any:
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return SimpleNamespace(items=self.items)


def _wire(monkeypatch: pytest.MonkeyPatch, fake: _FlakySch) -> list[float]:
    sleeps: list[float] = []
    monkeypatch.setattr(s2mod, "_client", lambda api_key="": fake)
    monkeypatch.setattr("precis.utils.http._default_sleep", lambda s: sleeps.append(s))
    # Keep Retry-After parks off the shared test DB's limiter rows.
    monkeypatch.setattr("precis.utils.rate_limit.park", lambda *a, **k: None)
    return sleeps


@pytest.mark.parametrize("wrapped", [True, False])
def test_lookup_s2_retries_429_then_returns(
    monkeypatch: pytest.MonkeyPatch, wrapped: bool
) -> None:
    """One 429 (wrapped the way the lib really wraps it, or bare) then a 200:
    the lookup must retry — not error on the first refusal."""
    fake = _FlakySch(
        [_exc_429(wrapped=wrapped)], [SimpleNamespace(title="T", paperId="p1")]
    )
    sleeps = _wire(monkeypatch, fake)
    out = s2mod.lookup_s2("Some real paper title")
    assert out is not None and out["title"] == "T"
    assert fake.calls == 2
    assert sleeps  # the retry backed off (full-jitter: duration, not count)


def test_lookup_s2_persistent_429_raises_typed_deferral(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Five refusals exhaust the budget; the caller gets S2RateLimited (a
    "retry next pass" signal), not a tenacity RetryError traceback — and no
    RuntimeError about an event loop, since the whole path sleeps
    synchronously."""
    fake = _FlakySch([_exc_429() for _ in range(10)], [])
    _wire(monkeypatch, fake)
    with pytest.raises(s2mod.S2RateLimited, match="429"):
        s2mod.lookup_s2("Some real paper title")
    assert fake.calls == 5  # the s2 policy's attempt budget


def test_lookup_s2_honours_retry_after(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 429 carrying ``Retry-After: 3`` sleeps that long before the retry —
    the shared wrapper reads the header off the exception's response."""

    class _HttpError(Exception):
        response: Any = None

    exc = _HttpError("HTTP 429")
    exc.response = SimpleNamespace(status_code=429, headers={"Retry-After": "3"})
    fake = _FlakySch([exc], [SimpleNamespace(title="T", paperId="p1")])
    sleeps = _wire(monkeypatch, fake)
    assert s2mod.lookup_s2("Some real paper title") is not None
    assert sleeps == [3.0]


def test_search_s2_papers_wrapped_429_still_fails_fast(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lit-search fast path must stay fast: a lib-wrapped 429 returns []
    after ONE call — no backoff loop stalling the quest tick."""
    fake = _FlakySch([_exc_429() for _ in range(10)], [])
    sleeps = _wire(monkeypatch, fake)
    assert s2mod.search_s2_papers("q") == []
    assert fake.calls == 1
    assert sleeps == []


def test_get_paper_by_id_propagate_429_is_s2_rate_limited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The strict id lookup reports a 429 storm as S2RateLimited (an
    S2ResolverError subtype), not a generic resolver fault."""

    class _Refusing:
        calls = 0

        def get_paper(self, paper_id: str) -> Any:
            type(self).calls += 1
            raise _exc_429()

    monkeypatch.setattr(s2mod, "_client", lambda api_key="": _Refusing())
    monkeypatch.setattr("precis.utils.http._default_sleep", lambda s: None)
    with pytest.raises(s2mod.S2RateLimited):
        s2mod.get_paper_by_id("doi:10.1/x", propagate=True)
