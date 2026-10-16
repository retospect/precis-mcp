"""Fleet-wide pacing seam on the JLC clients: every wire attempt (retries
included) calls ``pace(provider)`` first. Mocks only — no network, no orders.
"""

from __future__ import annotations

import contextlib
import inspect
from collections.abc import Callable, Iterator
from typing import Any

import httpx
import pytest

from precis.pcb import jlc_api, jlc_library, jlc_order
from precis.pcb._http import reset_circuit
from precis.utils import rate_limit

# conftest denies `jlc_library._WebPoster` per test; keep the real class.
_RealWebPoster = jlc_library._WebPoster


@pytest.fixture(autouse=True)
def _reset_breaker() -> Iterator[None]:
    reset_circuit("jlcpcb")
    reset_circuit("jlcpcb-web")
    yield
    reset_circuit("jlcpcb")
    reset_circuit("jlcpcb-web")


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, "test-secret-key")


def _noop_sleep(_s: float) -> None:
    pass


def _ok() -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "data": {"fileKey": "fk"}})


def test_component_info_paces_once_per_send(creds: None) -> None:
    log: list[tuple] = []

    def send(m: str, p: str, h: dict[str, str], b: str) -> httpx.Response:
        log.append(("send",))
        return _ok()

    client = jlc_api.JlcApiClient(
        send=send,
        sleep=_noop_sleep,
        pace=lambda prov: log.append(("pace", prov)),
    )
    client.component_info("C1")
    assert log == [("pace", "jlcpcb"), ("send",)]


def test_retry_paces_every_attempt(creds: None) -> None:
    log: list[tuple] = []
    responses = [httpx.Response(503, text="busy"), _ok()]

    def send(m: str, p: str, h: dict[str, str], b: str) -> httpx.Response:
        log.append(("send",))
        return responses.pop(0)

    client = jlc_api.JlcApiClient(
        send=send,
        sleep=_noop_sleep,
        now=lambda: 0.0,
        pace=lambda prov: log.append(("pace", prov)),
    )
    client.component_info("C1")
    assert log == [("pace", "jlcpcb"), ("send",)] * 2


def _order_client(log: list[tuple]) -> jlc_order.JlcOrderClient:
    def send(m: str, p: str, h: dict[str, str], b: str) -> httpx.Response:
        log.append(("send",))
        return _ok()

    def upload(
        p: str, h: dict[str, str], meta: str, fn: str, blob: bytes
    ) -> httpx.Response:
        log.append(("send",))
        return _ok()

    return jlc_order.JlcOrderClient(
        send=send,
        upload=upload,
        sleep=_noop_sleep,
        pace=lambda prov: log.append(("pace", prov)),
    )


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda c: c.quote({"pcbParam": {}}), id="quote"),
        pytest.param(lambda c: c.upload_gerber("b.zip", b"x"), id="upload_gerber"),
        pytest.param(
            lambda c: c.create_order({"pcbParam": {}}, confirm="add-to-cart"),
            id="create_order",
        ),
    ],
)
def test_order_client_paces_before_send(
    creds: None, call: Callable[[jlc_order.JlcOrderClient], Any]
) -> None:
    log: list[tuple] = []
    call(_order_client(log))
    assert log == [("pace", "jlcpcb"), ("send",)]


def test_web_poster_paces_handshake_and_post(monkeypatch: pytest.MonkeyPatch) -> None:
    from precis.utils import safe_fetch

    log: list[tuple] = []

    def fake_get(client: Any, url: str, **kw: Any) -> httpx.Response:
        log.append(("get",))
        return httpx.Response(
            200,
            headers={"set-cookie": "XSRF-TOKEN=tok; Path=/"},
            request=httpx.Request("GET", url),
        )

    @contextlib.contextmanager
    def fake_stream(
        client: Any, method: str, url: str, **kw: Any
    ) -> Iterator[httpx.Response]:
        log.append(("post",))
        yield httpx.Response(200, json={"code": 200, "data": {}})

    monkeypatch.setattr(safe_fetch, "safe_get", fake_get)
    monkeypatch.setattr(safe_fetch, "safe_stream", fake_stream)
    poster = _RealWebPoster(pace=lambda prov: log.append(("pace", prov)))
    try:
        poster({"currentPage": 1, "pageSize": 1})
    finally:
        poster.close()
    assert log == [
        ("pace", "jlcpcb-web"),
        ("get",),
        ("pace", "jlcpcb-web"),
        ("post",),
    ]


@pytest.mark.parametrize(
    "cls",
    [jlc_api.JlcApiClient, jlc_order.JlcOrderClient, _RealWebPoster],
    ids=lambda c: c.__name__,
)
def test_default_pace_is_rate_limit_acquire(cls: type) -> None:
    default = inspect.signature(cls).parameters["pace"].default
    assert default is rate_limit.acquire
