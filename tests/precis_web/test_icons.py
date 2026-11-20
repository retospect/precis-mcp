"""gr633600: the precis mark on the web host.

``/favicon.ico`` + ``/favicon.svg`` are served unauthenticated
(``auth._OPEN_PATHS``) from the shared :mod:`precis.icons` package, and
``base.html.j2`` declares the SVG so browser tabs carry it too.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")


def test_favicon_ico(client) -> None:
    resp = client.get("/favicon.ico")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/x-icon")
    assert resp.content[:4] == b"\x00\x00\x01\x00"


def test_favicon_svg(client) -> None:
    resp = client.get("/favicon.svg")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("image/svg+xml")
    assert b"#34d399" in resp.content


def test_base_template_declares_the_icon(client) -> None:
    resp = client.get("/drive")
    assert resp.status_code == 200
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg"' in resp.text
    assert '<link rel="alternate icon" href="/favicon.ico"' in resp.text


def test_favicons_are_open_paths_when_auth_is_on() -> None:
    """The connector's icon fetch carries no credential — both paths must
    clear the Basic gate exactly like ``/healthz`` does, while a content
    route still challenges."""
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    app = create_app(
        runtime=SimpleNamespace(),
        web_config=WebConfig(corpus_dir=None, auth_required=True),
    )
    gated = TestClient(app)
    assert gated.get("/favicon.ico").status_code == 200
    assert gated.get("/favicon.svg").status_code == 200
    assert gated.get("/drive").status_code in (401, 503)
