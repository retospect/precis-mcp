"""M3 (gr494831): library exceptions cross the dispatcher as ``Upstream`` +
retry hint, never as ``Internal`` or raw library text.

One test per row of ``runtime.dispatch._boundary_rows``; a new row without
a case here fails ``test_every_row_has_a_case``.
"""

from __future__ import annotations

import email.message
import urllib.error

import anthropic
import httpx
import openai
import psycopg
import psycopg.errors as pge
import psycopg_pool
import pytest
import requests.exceptions

from precis.errors import Upstream
from precis.runtime import PrecisRuntime
from precis.runtime import dispatch as d

_SECRET = "host=10.9.8.7 password=hunter2 SELECT secret_col FROM t"
_REQ = httpx.Request("GET", "https://example.invalid/x")

#: row class -> factory building a representative exception whose text
#: carries something that must not reach the agent.
_CASES: dict[type[BaseException], BaseException] = {
    pge.LockNotAvailable: pge.LockNotAvailable(_SECRET),
    psycopg_pool.PoolTimeout: psycopg_pool.PoolTimeout(_SECRET),
    pge.QueryCanceled: pge.QueryCanceled(_SECRET),
    pge.DeadlockDetected: pge.DeadlockDetected(_SECRET),
    pge.SerializationFailure: pge.SerializationFailure(_SECRET),
    psycopg.OperationalError: psycopg.OperationalError(_SECRET),
    httpx.TimeoutException: httpx.ConnectTimeout(_SECRET, request=_REQ),
    httpx.HTTPStatusError: httpx.HTTPStatusError(
        _SECRET, request=_REQ, response=httpx.Response(503, request=_REQ)
    ),
    httpx.TransportError: httpx.ConnectError(_SECRET, request=_REQ),
    urllib.error.URLError: urllib.error.URLError(_SECRET),
    requests.exceptions.Timeout: requests.exceptions.ReadTimeout(_SECRET),
    requests.exceptions.ConnectionError: requests.exceptions.ConnectionError(_SECRET),
    openai.APITimeoutError: openai.APITimeoutError(_REQ),
    openai.APIConnectionError: openai.APIConnectionError(message=_SECRET, request=_REQ),
    anthropic.APITimeoutError: anthropic.APITimeoutError(_REQ),
    anthropic.APIConnectionError: anthropic.APIConnectionError(
        message=_SECRET, request=_REQ
    ),
    TimeoutError: TimeoutError(_SECRET),
    ConnectionError: ConnectionError(_SECRET),
}


@pytest.fixture(autouse=True)
def _no_retry_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Transient faults on a get are retried; skip the backoff sleeps."""
    monkeypatch.setattr(d, "_retry_sleep", lambda _attempt: None)


def test_every_row_has_a_case() -> None:
    assert {cls for cls, _c, _n in d._boundary_rows()} == set(_CASES)


@pytest.mark.parametrize("cls", list(_CASES), ids=lambda c: c.__name__)
def test_row_maps_to_upstream_with_retry_hint(cls: type[BaseException]) -> None:
    mapped = d.map_boundary_exception(_CASES[cls])
    assert isinstance(mapped, Upstream)
    assert mapped.next and "retry" in str(mapped.next)
    assert "hunter2" not in mapped.cause and "10.9.8.7" not in mapped.cause


def _http_error(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://example.invalid/x",
        code,
        f"HTTP {code} {_SECRET}",
        email.message.Message(),
        None,
    )


def test_http_error_4xx_is_non_retry_not_unreachable() -> None:
    # HTTPError subclasses URLError — without the status-code branch it
    # maps to "could not be reached" + retry hint.
    mapped = d.map_boundary_exception(_http_error(404))
    assert isinstance(mapped, Upstream)
    assert "rejected" in mapped.cause and "404" in mapped.cause
    assert "could not be reached" not in mapped.cause
    assert "retry the same call once" not in str(mapped.next)
    assert "hunter2" not in mapped.cause


def test_http_error_5xx_maps_to_upstream_with_retry() -> None:
    mapped = d.map_boundary_exception(_http_error(503))
    assert isinstance(mapped, Upstream)
    assert "503" in mapped.cause and "retry" in str(mapped.next)


def test_http_error_429_maps_to_rate_limited() -> None:
    from precis.errors import RateLimited

    mapped = d.map_boundary_exception(_http_error(429))
    assert isinstance(mapped, RateLimited)
    assert "429" in mapped.cause and "retry" in str(mapped.next)


def test_wrapped_http_error_maps_through_cause_chain() -> None:
    try:
        try:
            raise _http_error(404)
        except urllib.error.URLError as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        mapped = d.map_boundary_exception(outer)
    assert isinstance(mapped, Upstream)
    assert "rejected" in mapped.cause
    assert "retry the same call once" not in str(mapped.next)


def test_unmapped_exception_stays_none() -> None:
    assert d.map_boundary_exception(KeyError("x")) is None


def test_wrapped_fault_maps_through_cause_chain() -> None:
    try:
        try:
            raise pge.LockNotAvailable(_SECRET)
        except pge.LockNotAvailable as inner:
            raise RuntimeError("wrapper") from inner
    except RuntimeError as outer:
        assert isinstance(d.map_boundary_exception(outer), Upstream)


def _raise_via_calc(runtime: PrecisRuntime, exc: BaseException) -> str:
    calc = runtime.hub.handler_for("calc")
    assert calc is not None
    original = calc.get

    def boom(**_kw: object) -> None:
        raise exc

    calc.get = boom
    try:
        return runtime.dispatch("get", {"kind": "calc", "id": "1+1"})
    finally:
        calc.get = original


@pytest.mark.parametrize("cls", list(_CASES), ids=lambda c: c.__name__)
def test_dispatch_renders_upstream_not_internal(
    runtime: PrecisRuntime, cls: type[BaseException]
) -> None:
    out = _raise_via_calc(runtime, _CASES[cls])
    assert "[error:Upstream]" in out
    assert "[error:Internal]" not in out
    assert "next:" in out and "retry" in out
    assert "hunter2" not in out and "10.9.8.7" not in out


def test_dispatch_internal_names_a_next_step(runtime: PrecisRuntime) -> None:
    out = _raise_via_calc(runtime, KeyError("zz"))
    assert "[error:Internal]" in out and "KeyError" in out
    assert "next:" in out and "gripe" in out


def test_handler_upstream_does_not_leak_library_text(
    runtime: PrecisRuntime,
) -> None:
    try:
        raise OSError("[Errno 61] Connection refused (_ssl.c:993) hunter2")
    except OSError as lib:
        err = Upstream(f"chem: no source answered: {lib}")
        err.__cause__ = lib
    out = _raise_via_calc(runtime, err)
    assert "[error:Upstream]" in out
    assert "hunter2" not in out and "_ssl.c" not in out and "Errno" not in out
    assert "OSError" in out and "next:" in out


def test_scrub_ignores_other_classes() -> None:
    from precis.errors import BadInput

    e = BadInput("keep '_ssl.c:993' text")
    d.scrub_upstream(e)
    assert e.cause == "keep '_ssl.c:993' text" and e.next is None


def test_context_only_chain_is_not_mapped() -> None:
    # A handler bug raised while handling a transient fault is still a bug.
    try:
        try:
            raise pge.LockNotAvailable(_SECRET)
        except pge.LockNotAvailable:
            raise KeyError("recovery bug")  # noqa: B904 - implicit context on purpose
    except KeyError as outer:
        assert d.map_boundary_exception(outer) is None


def test_upstream_without_library_cause_gets_no_retry_hint() -> None:
    e = Upstream("API key not configured")
    d.scrub_upstream(e)
    assert "retry" not in str(e.next) and "gripe" in str(e.next)


def test_scrub_leaves_short_cause_text_alone() -> None:
    try:
        raise OSError("host")
    except OSError as lib:
        e = Upstream("host lookup failed for the host")
        e.__cause__ = lib
    d.scrub_upstream(e)
    assert e.cause == "host lookup failed for the host"
