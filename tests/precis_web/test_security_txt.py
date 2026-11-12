"""RFC 9116 ``/.well-known/security.txt`` (gr627864).

The public funnel host must serve a valid security.txt or external
scanners flag it (pentest-tools scan TJ1YnNQza7QxWgcN, 2026-11-10). The
route must answer **unauthenticated** — a researcher reporting a
vulnerability has no Basic credentials — and its ``Expires`` must stay
under the RFC's one-year cap forever, which the per-request render
guarantees.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.security_txt import render_security_txt

_EXPIRES_RE = re.compile(r"^Expires: (.+)$", re.MULTILINE)


def _client(cfg: WebConfig | None = None) -> TestClient:
    return TestClient(create_app(web_config=cfg or WebConfig()))


def test_serves_rfc9116_fields() -> None:
    r = _client().get("/.well-known/security.txt")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    body = r.text
    assert "Contact: mailto:reto@retostamm.com" in body
    assert "Preferred-Languages: en" in body
    # Canonical is the request's own absolute URL — behind the funnel the
    # host header is the public name, so deriving it per request keeps the
    # field right on every alias.
    assert "Canonical: http://testserver/.well-known/security.txt" in body


def test_expires_within_one_year() -> None:
    r = _client().get("/.well-known/security.txt")
    m = _EXPIRES_RE.search(r.text)
    assert m is not None
    expires = datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    now = datetime.now(UTC)
    assert expires > now
    assert (expires - now).days <= 365


def test_open_path_with_auth_gate_on() -> None:
    """The Basic gate must not challenge the file — a 401 reads as
    "missing" to a scanner, and reporters have no credentials."""
    client = _client(WebConfig(auth_required=True))
    r = client.get("/.well-known/security.txt")
    assert r.status_code == 200
    assert "www-authenticate" not in r.headers
    # The gate itself is still installed: a normal route challenges.
    guarded = client.get("/drive")
    assert guarded.status_code in (401, 503)


def test_contact_config_override() -> None:
    cfg = WebConfig(
        security_contacts=("mailto:sec@example.org", "https://example.org/security")
    )
    r = _client(cfg).get("/.well-known/security.txt")
    assert "Contact: mailto:sec@example.org" in r.text
    assert "Contact: https://example.org/security" in r.text


def test_expires_recomputed_per_render() -> None:
    """The renewal path: rendering at a later time yields a later Expires,
    so a long-running server never serves a stale file."""
    contacts = ("mailto:a@b.c",)
    canonical = "https://host/.well-known/security.txt"
    jan = render_security_txt(
        contacts=contacts,
        languages="en",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        canonical_url=canonical,
    )
    jun = render_security_txt(
        contacts=contacts,
        languages="en",
        now=datetime(2026, 6, 1, tzinfo=UTC),
        canonical_url=canonical,
    )
    assert "Expires: 2026-12-31T" in jan
    assert "Expires: 2027-05-31T" in jun


def test_no_languages_line_when_unset() -> None:
    body = render_security_txt(
        contacts=("mailto:a@b.c",),
        languages="",
        now=datetime(2026, 1, 1, tzinfo=UTC),
        canonical_url="https://host/.well-known/security.txt",
    )
    assert "Preferred-Languages" not in body
