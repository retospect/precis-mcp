"""``precis_web.errors`` — the exception → status mapping, on a bare app
so the three tiers are pinned independently of any route: ``NotFound`` is
a 404, every other ``PrecisError`` a 400, anything else a 500."""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from precis.errors import BadInput, NotFound, PrecisError
from precis_web.errors import register_error_handlers


@pytest.fixture
def client() -> TestClient:
    app = FastAPI()
    register_error_handlers(app)

    @app.get("/not-found")
    def _not_found() -> None:
        raise NotFound("ref id=42 not found")

    @app.get("/bad-input")
    def _bad_input() -> None:
        raise BadInput("needs a slug")

    @app.get("/precis-error")
    def _precis_error() -> None:
        raise PrecisError("handler failed")

    @app.get("/boom")
    def _boom() -> None:
        raise RuntimeError("secret detail")

    return TestClient(app, raise_server_exceptions=False)


def test_not_found_is_404_with_its_message(client: TestClient) -> None:
    resp = client.get("/not-found")
    assert resp.status_code == 404
    assert "Not found (404)" in resp.text
    assert "ref id=42 not found" in resp.text


def test_other_precis_errors_stay_400(client: TestClient) -> None:
    for path in ("/bad-input", "/precis-error"):
        resp = client.get(path)
        assert resp.status_code == 400, path
        assert "Request error (400)" in resp.text


def test_unhandled_is_500_with_type_only(client: TestClient) -> None:
    resp = client.get("/boom")
    assert resp.status_code == 500
    assert "RuntimeError" in resp.text
    assert "secret detail" not in resp.text
