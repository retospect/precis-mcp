"""Per-login /eye walk records (gr743231): the route appends, /learning shows.

Every check runs against the isolated PostgreSQL the ``store`` fixture
supplies; owner scoping is asserted by walking as two separate logins.
"""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any, cast

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.errors import BadInput, Denied
from precis.eye_visits import RETENTION_DAYS, EyeVisitStore
from precis.store import Store
from precis.user_context import current_principal, user_scope
from precis.users import hash_password
from precis_web.app import create_app
from precis_web.config import WebConfig

PASSWORD = "password"


def _client(runtime_with_store: Any, *, auth_required: bool = True) -> TestClient:
    return TestClient(
        create_app(
            runtime=runtime_with_store,
            web_config=WebConfig(corpus_dir=None, auth_required=auth_required),
        )
    )


def _auth(login: str) -> dict[str, str]:
    value = base64.b64encode(f"{login}:{PASSWORD}".encode()).decode()
    return {"Authorization": f"Basic {value}"}


@pytest.fixture
def walkers(store: Any) -> tuple[Any, Any]:
    password = hash_password(PASSWORD)
    first = store.create_web_user(login="walk-one", abbrev="wo", password=password)
    second = store.create_web_user(login="walk-two", abbrev="wt", password=password)
    return first, second


def _walk(store: Any, user: Any) -> list[Any]:
    with user_scope(user.id):
        return EyeVisitStore(store).recent()


def _count_rows(store: Any) -> int:
    with store.pool.connection() as conn:
        return int(conn.execute("SELECT count(*) FROM eye_visits").fetchone()[0])


@pytest.mark.db
def test_authenticated_focus_appends_a_walk_step(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    client = _client(runtime_with_store)
    page = client.get(f"/eye/td{todo.id}", headers=_auth(user.login))
    assert page.status_code == 200
    walk = _walk(store, user)
    assert len(walk) == 1
    assert walk[0].handle == f"td{todo.id}"
    assert walk[0].kind == "todo"
    assert walk[0].previous_handle is None
    assert walk[0].visited_at.tzinfo is not None


@pytest.mark.db
def test_steps_chain_the_previous_handle(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    quest = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    client = _client(runtime_with_store)
    client.get(f"/eye/td{todo.id}", headers=_auth(user.login))
    client.get(f"/eye/qu{quest.id}", headers=_auth(user.login))
    walk = _walk(store, user)
    assert [step.handle for step in walk] == [f"qu{quest.id}", f"td{todo.id}"]
    assert walk[0].previous_handle == f"td{todo.id}"
    assert walk[1].previous_handle is None


@pytest.mark.db
def test_each_login_has_an_independent_walk(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    client = _client(runtime_with_store)
    client.get(f"/eye/td{todo.id}", headers=_auth(user.login))
    assert len(_walk(store, user)) == 1
    assert _walk(store, other) == []
    # The second login's own step chains from nothing, not from the
    # first login's handle.
    client.get(f"/eye/td{todo.id}", headers=_auth(other.login))
    other_walk = _walk(store, other)
    assert len(other_walk) == 1 and other_walk[0].previous_handle is None


@pytest.mark.db
def test_auth_off_and_anonymous_write_nothing(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    open_client = _client(runtime_with_store, auth_required=False)
    assert open_client.get(f"/eye/td{todo.id}").status_code == 200
    assert _count_rows(store) == 0
    gated = _client(runtime_with_store)
    assert gated.get(f"/eye/td{todo.id}").status_code == 401
    assert _count_rows(store) == 0
    assert _walk(store, user) == []


@pytest.mark.db
def test_rejection_paths_leave_no_step(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    client = _client(runtime_with_store)
    assert client.get("/eye/not-a-handle", headers=_auth(user.login)).status_code == 404
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    assert (
        client.get(
            f"/eye/td{todo.id}", params={"extent": "bogus"}, headers=_auth(user.login)
        ).status_code
        == 400
    )
    assert _count_rows(store) == 0


@pytest.mark.db
def test_append_prunes_the_retention_window(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    quest = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    client = _client(runtime_with_store)
    client.get(f"/eye/td{todo.id}", headers=_auth(user.login))
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE eye_visits SET visited_at = now() - make_interval(days => %s) "
            "WHERE user_id = %s",
            (RETENTION_DAYS + 1, user.id),
        )
        conn.commit()
    assert _walk(store, user) == []
    client.get(f"/eye/qu{quest.id}", headers=_auth(user.login))
    walk = _walk(store, user)
    assert [step.handle for step in walk] == [f"qu{quest.id}"]
    assert walk[0].previous_handle is None
    assert _count_rows(store) == 1


@pytest.mark.db
def test_learning_shows_the_walk_and_clear_all_empties_it(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, other = walkers
    todo = store.insert_ref(kind="todo", slug=None, title="Ship the ring")
    quest = store.insert_ref(kind="quest", slug=None, title="Grow the mesh")
    client = _client(runtime_with_store)
    client.get(f"/eye/td{todo.id}", headers=_auth(user.login))
    client.get(f"/eye/qu{quest.id}", headers=_auth(user.login))

    page = client.get("/learning", headers=_auth(user.login))
    assert page.status_code == 200
    assert "Your recent walk" in page.text
    assert page.text.index(f"qu{quest.id}") < page.text.index(f"td{todo.id}")
    assert "from " + f"td{todo.id}" in page.text
    assert 'action="/learning/walk/clear"' in page.text

    # The other login's page shows no trace of this walk.
    other_page = client.get("/learning", headers=_auth(other.login))
    assert "Your recent walk" not in other_page.text

    cleared = client.post(
        "/learning/walk/clear", headers=_auth(user.login), follow_redirects=False
    )
    assert cleared.status_code == 303
    assert cleared.headers["location"] == "/learning?saved=walk-cleared"
    assert _walk(store, user) == []
    notice = client.get("/learning?saved=walk-cleared", headers=_auth(user.login))
    assert 'id="walk-heading"' not in notice.text
    assert "Your recent walk was cleared." in notice.text


@pytest.mark.db
def test_skill_eyes_record_the_skill_kind(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    client = _client(runtime_with_store)
    # The skill's existence decides the page status, not whether the walk
    # records: a ``sk:`` focus routes to the skill eye either way.
    client.get("/eye/sk:precis-get-help", headers=_auth(user.login))
    walk = _walk(store, user)
    assert len(walk) == 1
    assert walk[0].handle == "sk:precis-get-help" and walk[0].kind == "skill"


def test_store_refuses_without_a_personal_owner() -> None:
    with pytest.raises(Denied):
        EyeVisitStore(cast(Store, SimpleNamespace())).record(handle="td1", kind="todo")
    assert current_principal() is None


@pytest.mark.db
def test_store_validates_handle_and_kind(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    user, _other = walkers
    with user_scope(user.id):
        with pytest.raises(BadInput):
            EyeVisitStore(store).record(handle="x" * 201, kind="todo")
        with pytest.raises(BadInput):
            EyeVisitStore(store).record(handle="td1", kind="")
    assert current_principal() is None
    assert _count_rows(store) == 0


@pytest.mark.db
def test_not_found_focus_still_records_the_walk_step(
    store: Any, runtime_with_store: Any, walkers: Any
) -> None:
    """A dispatch-error focus still renders the page — and still records."""
    user, _other = walkers
    client = _client(runtime_with_store)
    page = client.get("/eye/td4242424242", headers=_auth(user.login))
    assert page.status_code == 404
    assert "[error:NotFound]" in page.text
    walk = _walk(store, user)
    assert len(walk) == 1 and walk[0].handle == "td4242424242"
