"""``external_retry`` classification and jittered backoff (gr493050). Mocks only."""

from __future__ import annotations

import json
from http.client import IncompleteRead

import httpx
import pytest
import requests
from semanticscholar import SemanticScholarException as s2x

from precis.liveness import request_drain
from precis.utils import http


def _status_error(code: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.example.org/x")
    return httpx.HTTPStatusError(
        "boom", request=req, response=httpx.Response(code, request=req)
    )


def _run(exc: Exception, *, attempts: int = 4) -> tuple[int, list[float]]:
    calls = {"n": 0}
    waits: list[float] = []

    @http.external_retry(
        attempts=attempts,
        wait_min_s=1.0,
        wait_max_s=8.0,
        sleep=waits.append,
        uniform=lambda lo, hi: hi / 2,
    )
    def fn() -> None:
        calls["n"] += 1
        raise exc

    with pytest.raises(type(exc)):
        fn()
    return calls["n"], waits


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(400),
        _status_error(401),
        _status_error(403),
        _status_error(404),
        s2x.BadQueryParametersException("bad"),
        s2x.ObjectNotFoundException("nf"),
        PermissionError("HTTP status 403 Forbidden."),
        TypeError("programming error"),
        ValueError("nope"),
    ],
)
def test_permanent_errors_not_retried(exc: Exception) -> None:
    n, waits = _run(exc)
    assert n == 1
    assert waits == []


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(429),
        _status_error(503),
        ConnectionRefusedError("HTTP status 429 Too Many Requests."),
        s2x.InternalServerErrorException("500"),
        s2x.GatewayTimeoutException("504"),
        httpx.ConnectTimeout("t"),
        httpx.ReadTimeout("t"),
        httpx.ConnectError("reset"),
        json.JSONDecodeError("gateway html", "<html>", 0),
        IncompleteRead(b""),
        requests.exceptions.ChunkedEncodingError("cut"),
        ConnectionResetError("reset"),
        TimeoutError("t"),
    ],
)
def test_transient_errors_retried_with_full_jitter(exc: Exception) -> None:
    n, waits = _run(exc)
    assert n == 4
    # injected uniform returns cap/2: caps are 1, 2, 4 (then max 8)
    assert waits == [0.5, 1.0, 2.0]


def test_wait_is_capped_and_drawn_from_zero_to_cap() -> None:
    seen: list[tuple[float, float]] = []

    def uniform(lo: float, hi: float) -> float:
        seen.append((lo, hi))
        return 0.0

    @http.external_retry(
        attempts=6,
        wait_min_s=1.0,
        wait_max_s=5.0,
        sleep=lambda s: None,
        uniform=uniform,
    )
    def fn() -> None:
        raise httpx.ConnectError("x")

    with pytest.raises(httpx.ConnectError):
        fn()
    # tenacity draws once more on the final attempt before stopping
    assert seen[:5] == [(0.0, 1.0), (0.0, 2.0), (0.0, 4.0), (0.0, 5.0), (0.0, 5.0)]


def test_recovers_after_transient_failure() -> None:
    calls = {"n": 0}

    @http.external_retry(attempts=3, sleep=lambda s: None)
    def fn() -> str:
        calls["n"] += 1
        if calls["n"] < 3:
            raise _status_error(503)
        return "ok"

    assert fn() == "ok"


def test_drain_still_stops_retry_of_transient_error() -> None:
    request_drain()
    n, waits = _run(httpx.ConnectError("x"))
    assert n == 1
    assert waits == []
