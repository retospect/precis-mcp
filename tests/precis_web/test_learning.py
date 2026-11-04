"""Learning forms exercise the real owner-scoped ledger, never a production DB.

The ordinary ``store`` fixture supplies isolated PostgreSQL. Stateless denial
checks use a minimal fake so they prove no learning read needs to happen first.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi import FastAPI
from fastapi.testclient import TestClient

from precis.learning import LearningStore
from precis.user_context import current_principal, user_scope
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.routes.learning import LearningPrivacyMiddleware

PASSWORD = "learning-password"  # secret-gate: allow — synthetic test password


def _client(store: Any, *, auth_required: bool = True) -> TestClient:
    return TestClient(
        create_app(
            runtime=SimpleNamespace(store=store),
            web_config=WebConfig(corpus_dir=None, auth_required=auth_required),
        )
    )


def _auth(login: str = "learning-one") -> dict[str, str]:
    value = base64.b64encode(f"{login}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


class _Forms(HTMLParser):
    def __init__(self, html: str) -> None:
        super().__init__()
        self.forms: list[dict[str, str]] = []
        self._current: dict[str, str] | None = None
        self.feed(html)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "form":
            self._current = {"action": values.get("action") or ""}
            self.forms.append(self._current)
        elif tag in {"input", "textarea"} and self._current is not None:
            name = values.get("name")
            if name:
                self._current[name] = values.get("value") or ""

    def handle_endtag(self, tag: str) -> None:
        if tag == "form":
            self._current = None


def _forms(html: str) -> list[dict[str, str]]:
    return [
        form for form in _Forms(html).forms if form["action"] == "/learning/checkin"
    ]


@pytest.fixture
def learning_users(store: Any) -> tuple[Any, Any]:
    password = hash_password(PASSWORD)
    first = store.create_web_user(login="learning-one", abbrev="lo", password=password)
    second = store.create_web_user(login="learning-two", abbrev="lt", password=password)
    return first, second


@pytest.mark.parametrize(
    "path", ["/learning", "/learning/checkin", "/learning/settings"]
)
def test_auth_off_refuses_without_a_personal_owner(path: str) -> None:
    client = _client(SimpleNamespace(), auth_required=False)
    response = client.get(path) if path == "/learning" else client.post(path, data={})
    assert response.status_code == 503
    assert "PRECIS_WEB_AUTH" in response.text
    assert response.headers["cache-control"] == "no-store"
    assert current_principal() is None


def test_gate_denial_is_also_private() -> None:
    store = SimpleNamespace(count_web_users=lambda **kwargs: 1)
    response = _client(store).get("/learning")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_unexpected_failure_stays_private(caplog: pytest.LogCaptureFixture) -> None:
    app = FastAPI()
    app.add_middleware(LearningPrivacyMiddleware)

    @app.get("/learning")
    def failing_page() -> None:
        raise RuntimeError("private study note")

    response = TestClient(app).get("/learning")
    assert response.status_code == 500
    assert response.headers["cache-control"] == "no-store"
    assert "private study note" not in response.text
    assert "private study note" not in caplog.text
    assert "RuntimeError" in caplog.text


@pytest.mark.db
def test_empty_calendar_and_independent_forms(store: Any, learning_users: Any) -> None:
    client = _client(store)
    response = client.get("/learning", headers=_auth())
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.text.count('role="listitem"') == 84
    assert "No study recorded yet" in response.text
    assert "engagement, not mastery" in response.text
    assert "/anki/known" not in response.text
    studied, rest = _forms(response.text)
    assert studied["status"] == "studied" and rest["status"] == "rest"
    assert "minutes" in studied and "minutes" not in rest
    assert studied["idempotency_key"] != rest["idempotency_key"]
    refreshed = _forms(client.get("/learning", headers=_auth()).text)
    assert refreshed[0]["idempotency_key"] != studied["idempotency_key"]


@pytest.mark.db
def test_checkin_escapes_notes_and_ignores_forged_owner(
    store: Any, learning_users: Any
) -> None:
    first, second = learning_users
    client = _client(store)
    form = _forms(client.get("/learning", headers=_auth()).text)[0]
    form.pop("action")
    note = '<script>alert("private")</script>'
    form.update(minutes="1", note=note, owner=str(second.id), user_id=str(second.id))
    response = client.post(
        "/learning/checkin", data=form, headers=_auth(), follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    with user_scope(first.id):
        day = LearningStore(store).activity(days=1)["days"][0]
        event = LearningStore(store).get_event(day["event_id"])
    assert day["status"] == "studied" and day["minutes"] == 1
    assert event.note == note
    detail = client.get(f"/learning?event={event.id}", headers=_auth())
    assert detail.status_code == 200
    assert detail.headers["cache-control"] == "no-store"
    assert "&lt;script&gt;" in detail.text and note not in detail.text
    assert "Source: self-report" in detail.text
    with user_scope(second.id):
        assert (
            LearningStore(store).activity(days=1)["days"][0]["status"] == "unrecorded"
        )
    assert current_principal() is None


@pytest.mark.db
def test_rest_form_preserves_history_without_study_minutes(
    store: Any, learning_users: Any
) -> None:
    first, _ = learning_users
    client = _client(store)
    studied, rest = _forms(client.get("/learning", headers=_auth()).text)
    studied.pop("action")
    studied["minutes"] = "2"
    assert (
        client.post(
            "/learning/checkin", data=studied, headers=_auth(), follow_redirects=False
        ).status_code
        == 303
    )
    with user_scope(first.id):
        original = LearningStore(store).activity(days=1)["days"][0]["event_id"]
    rest.pop("action")
    response = client.post(
        "/learning/checkin", data=rest, headers=_auth(), follow_redirects=False
    )
    assert response.status_code == 303
    with user_scope(first.id):
        day = LearningStore(store).activity(days=1)["days"][0]
        assert day["status"] == "rest" and day["minutes"] is None
        assert day["event_id"] != original
        assert LearningStore(store).get_event(original).minutes == 2


@pytest.mark.db
def test_foreign_and_missing_event_have_identical_refusal(
    store: Any, learning_users: Any
) -> None:
    _, second = learning_users
    with user_scope(second.id):
        foreign = LearningStore(store).checkin(
            study_date=None,
            status="studied",
            minutes=3,
            note="other owner's private note",
            idempotency_key="foreign-event",
        )
    client = _client(store)
    foreign_response = client.get(f"/learning?event={foreign.id}", headers=_auth())
    missing_response = client.get(
        "/learning?event=9223372036854775807", headers=_auth()
    )
    assert foreign_response.status_code == missing_response.status_code == 404
    assert foreign_response.text == missing_response.text
    assert "other owner's private note" not in foreign_response.text
    assert foreign_response.headers["cache-control"] == "no-store"


@pytest.mark.db
def test_settings_save_clear_and_separate_owners(
    store: Any, learning_users: Any
) -> None:
    first, second = learning_users
    client = _client(store)
    response = client.post(
        "/learning/settings",
        data={
            "timezone": "Europe/Dublin",
            "typical_session_minutes": "15",
            "owner": str(second.id),
        },
        headers=_auth(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["cache-control"] == "no-store"
    with user_scope(first.id):
        settings = LearningStore(store).settings()
        assert settings.timezone == "Europe/Dublin"
        assert settings.typical_session_minutes == 15
    with user_scope(second.id):
        settings = LearningStore(store).settings()
        assert settings.timezone == "UTC" and settings.typical_session_minutes == 30
    response = client.post(
        "/learning/settings",
        data={"timezone": "UTC", "typical_session_minutes": ""},
        headers=_auth(),
        follow_redirects=False,
    )
    assert response.status_code == 303
    with user_scope(first.id):
        assert LearningStore(store).settings().typical_session_minutes is None
    page = client.get("/learning", headers=_auth())
    assert 'value="" aria-describedby="typical-help"' in page.text
    assert "never a quota" in page.text


@pytest.mark.db
@pytest.mark.parametrize(
    "fields",
    [
        {"minutes": "many"},
        {"minutes": "-1"},
        {"status": "mastered"},
        {"status": "rest", "minutes": "30"},
        {"note": "n" * 501},
        {"date": "not-a-date"},
        {"date": (datetime.now(UTC).date() + timedelta(days=2)).isoformat()},
        {"idempotency_key": ""},
    ],
)
def test_bad_checkin_never_creates_activity(
    store: Any, learning_users: Any, fields: dict[str, str]
) -> None:
    first, _ = learning_users
    data = {"status": "studied", "idempotency_key": "bad-checkin", **fields}
    response = _client(store).post("/learning/checkin", data=data, headers=_auth())
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"
    with user_scope(first.id):
        assert (
            LearningStore(store).activity(days=1)["days"][0]["status"] == "unrecorded"
        )
    assert current_principal() is None


@pytest.mark.db
@pytest.mark.parametrize(
    "fields",
    [
        {"timezone": "Invented/Place", "typical_session_minutes": "30"},
        {"timezone": "UTC", "typical_session_minutes": "many"},
        {"timezone": "UTC", "typical_session_minutes": "-1"},
    ],
)
def test_invalid_settings_are_private_refusals(
    store: Any, learning_users: Any, fields: dict[str, str]
) -> None:
    response = _client(store).post("/learning/settings", data=fields, headers=_auth())
    assert response.status_code == 400
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.db
def test_idempotent_form_retry_and_conflicting_reuse(
    store: Any, learning_users: Any
) -> None:
    first, _ = learning_users
    client = _client(store)
    data = {"status": "studied", "minutes": "1", "idempotency_key": "retry"}
    for _ in range(2):
        assert (
            client.post(
                "/learning/checkin", data=data, headers=_auth(), follow_redirects=False
            ).status_code
            == 303
        )
    response = client.post(
        "/learning/checkin", data={**data, "minutes": "2"}, headers=_auth()
    )
    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    with user_scope(first.id):
        assert LearningStore(store).activity(days=1)["days"][0]["minutes"] == 1


@pytest.mark.db
def test_cross_origin_post_is_refused_before_a_checkin(
    store: Any, learning_users: Any
) -> None:
    first, _ = learning_users
    response = _client(store).post(
        "/learning/checkin",
        data={"status": "studied", "idempotency_key": "cross-origin"},
        headers={**_auth(), "Origin": "https://other.example"},
    )
    assert response.status_code == 403
    assert response.headers["cache-control"] == "no-store"
    with user_scope(first.id):
        assert (
            LearningStore(store).activity(days=1)["days"][0]["status"] == "unrecorded"
        )


@pytest.mark.db
def test_disabled_account_cannot_reuse_a_form(store: Any, learning_users: Any) -> None:
    first, _ = learning_users
    client = _client(store)
    data = _forms(client.get("/learning", headers=_auth()).text)[0]
    data.pop("action")
    store.set_web_user_disabled(first.login, disabled=True)
    response = client.post("/learning/checkin", data=data, headers=_auth())
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"
