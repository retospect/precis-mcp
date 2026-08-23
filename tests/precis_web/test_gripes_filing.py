"""``POST /gripes`` with the auth gate on — does the filed text name the filer?

The shared ``client`` fixture in ``conftest`` runs with ``auth_required``
off, so ``current_user`` is ``None`` there and the route can only record
"HTTP auth disabled". The whole point of the attribution line is the
*signed-in* case, and that identity comes from the middleware parking a
``WebUser`` on the request scope — a test that stubbed that out would
pass while the wiring was broken. So this module drives the real gate,
the way ``test_account.py`` does.
"""

from __future__ import annotations

import base64
import os

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.users import PasswordRecord, WebUser, hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime, FakeStore

PASSWORD = "correct-horse"


class AuthFakeStore(FakeStore):
    """The route's read surface plus the ``web_users`` rows the gate needs."""

    def __init__(self, login: str = "reto") -> None:
        super().__init__()
        self.user = WebUser(
            id=1,
            login=login,
            abbrev="rs",
            full_name="Reto Stamm",
            email="reto@example.com",
            disabled_at=None,
            last_login_at=None,
            created_at=None,
            updated_at=None,
        )
        self.record = hash_password(PASSWORD, pepper=None)

    def count_web_users(self, *, enabled_only: bool = True) -> int:
        return 1

    def get_web_user(self, login: str) -> WebUser | None:
        return self.user if login == self.user.login else None

    def get_web_user_credentials(
        self, login: str
    ) -> tuple[WebUser, PasswordRecord] | None:
        if login != self.user.login:
            return None
        return self.user, self.record

    def touch_web_user_login(self, login: str) -> None:
        return None


def _auth(login: str = "reto") -> dict[str, str]:
    raw = base64.b64encode(f"{login}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {raw}"}


@pytest.fixture(autouse=True)
def _no_ambient_pepper(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """Match the hash above: no pepper from the environment or the vault."""
    monkeypatch.delenv("PRECIS_WEB_PASSWORD_PEPPER", raising=False)
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    from precis import secrets as vault_mod

    monkeypatch.setattr(
        vault_mod,
        "get_secret",
        lambda name, *, store=None, default=None: os.environ.get(name) or default,
    )


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(AuthFakeStore())


@pytest.fixture
def client(runtime: FakeRuntime, tmp_path) -> TestClient:
    cfg = WebConfig(corpus_dir=tmp_path, auth_required=True)
    return TestClient(create_app(runtime=runtime, web_config=cfg))


def test_filed_text_names_the_signed_in_user(runtime, client) -> None:
    resp = client.post(
        "/gripes",
        data={"text": "the slug error suggests nothing"},
        headers={**_auth(), "Origin": "http://testserver"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/gripes"
    verb, args = runtime.calls[-1]
    assert verb == "put"
    assert args["kind"] == "gripe"
    assert args["text"] == (
        "the slug error suggests nothing\n\n— filed by reto via the /gripes web form"
    )


def test_the_form_shows_who_it_will_attribute_to(client) -> None:
    resp = client.get("/gripes", headers=_auth())
    assert resp.status_code == 200
    assert "Filed as" in resp.text
    assert ">reto<" in resp.text


def test_filing_needs_credentials(runtime, client) -> None:
    """No gate pass, no gripe — the middleware refuses before the route."""
    resp = client.post("/gripes", data={"text": "friction"}, follow_redirects=False)
    assert resp.status_code == 401
    assert runtime.calls == []
