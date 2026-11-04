"""Owner isolation, immutable corrections and real row policies for learning."""

from __future__ import annotations

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from threading import Barrier
from types import SimpleNamespace
from typing import Any, cast

import psycopg
import pytest
from psycopg import sql

from precis.errors import BadInput, Conflict, Denied, NotFound
from precis.learning import LearningSettings, LearningStore
from precis.store import Store
from precis.user_context import user_scope
from precis.users import PasswordRecord

pytestmark = pytest.mark.db

NOW = datetime(2026, 11, 4, 0, 30, tzinfo=UTC)
TODAY = NOW.date()


@pytest.fixture
def owners(store: Store) -> tuple[int, int]:
    password = PasswordRecord("unused", "unused", "scrypt-v1")
    first = store.create_web_user(login="alice", abbrev="al", password=password)
    second = store.create_web_user(login="bob", abbrev="bo", password=password)
    return first.id, second.id


def _history(store: Store, owner: int) -> list[tuple[Any, ...]]:
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "SELECT set_config('precis.learning_user_id', %s, true)", (str(owner),)
        )
        return conn.execute(
            "SELECT event_id, study_date, status, minutes, note, supersedes_id "
            "FROM learning_events WHERE user_id = %s ORDER BY event_id",
            (owner,),
        ).fetchall()


def test_two_users_have_separate_defaults_settings_activity_and_events(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    alice, bob = owners
    with user_scope(alice):
        assert learning.settings() == LearningSettings("UTC", 30)
        assert learning.set_settings(
            timezone="Europe/Dublin", typical_session_minutes=None
        ) == LearningSettings("Europe/Dublin", None)
        first = learning.checkin(
            status="studied",
            minutes=1,
            note="Alice's private note",
            idempotency_key="shared-key",
            now=NOW,
        )
    with user_scope(bob):
        assert learning.settings() == LearningSettings("UTC", 30)
        assert learning.activity(days=2, now=NOW)["last_studied"] is None
        second = learning.checkin(status="rest", idempotency_key="shared-key", now=NOW)
        assert learning.get_event(second.id) == second
        with pytest.raises(NotFound) as foreign:
            learning.get_event(first.id)
        with pytest.raises(NotFound) as missing:
            learning.get_event(9_000_000_000)
        assert str(foreign.value) == str(missing.value)
        assert learning.activity(days=1, now=NOW)["days"] == [
            {
                "date": TODAY.isoformat(),
                "status": "rest",
                "minutes": None,
                "source": "self_report",
                "event_id": second.id,
            }
        ]
    with user_scope(alice):
        assert learning.settings().typical_session_minutes is None
        assert learning.get_event(first.id).note == "Alice's private note"
        assert learning.activity(days=1, now=NOW)["days"][0]["event_id"] == first.id


@pytest.mark.parametrize(
    "operation", ["settings", "set_settings", "checkin", "get_event", "activity"]
)
def test_anonymous_is_refused_before_any_database_access(operation: str) -> None:
    class RefusePool:
        def connection(self) -> Any:
            pytest.fail("an anonymous learning call touched the database")

    learning = LearningStore(cast(Store, SimpleNamespace(pool=RefusePool())))
    calls = {
        "settings": lambda: learning.settings(),
        "set_settings": lambda: learning.set_settings(
            timezone="UTC", typical_session_minutes=30
        ),
        "checkin": lambda: learning.checkin(status="studied", idempotency_key="k"),
        "get_event": lambda: learning.get_event(1),
        "activity": lambda: learning.activity(),
    }
    with user_scope(None), pytest.raises(Denied, match="authenticated user"):
        calls[operation]()


def test_every_public_operation_revalidates_disabled_and_deleted_accounts(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        event = learning.checkin(status="studied", idempotency_key="k", now=NOW)
        assert store.set_web_user_disabled("alice", disabled=True)
        calls = [
            lambda: learning.settings(),
            lambda: learning.set_settings(timezone="UTC", typical_session_minutes=None),
            lambda: learning.checkin(status="rest", idempotency_key="next", now=NOW),
            lambda: learning.get_event(event.id),
            lambda: learning.activity(now=NOW),
        ]
        for call in calls:
            with pytest.raises(Denied, match="no longer active"):
                call()
        assert store.delete_web_user("alice")
        with pytest.raises(Denied, match="no longer active"):
            learning.settings()


def test_idempotent_retry_returns_original_even_after_a_correction(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        first = learning.checkin(
            study_date=TODAY,
            status="studied",
            minutes=3,
            note="original",
            idempotency_key="first",
            now=NOW,
        )
        assert (
            learning.checkin(
                study_date=TODAY.isoformat(),
                status="studied",
                minutes=3,
                note="original",
                idempotency_key="first",
                now=NOW,
            )
            == first
        )
        correction = learning.checkin(
            status="rest",
            note="correction",
            idempotency_key="second",
            now=NOW,
        )
        assert correction.id != first.id
        assert (
            learning.checkin(
                study_date=TODAY,
                status="studied",
                minutes=3,
                note="original",
                idempotency_key="first",
                now=NOW,
            )
            == first
        )
        assert learning.get_event(first.id) == first
        activity = learning.activity(days=1, now=NOW)
        assert activity["days"][0]["status"] == "rest"
        assert activity["last_studied"] is None
    assert _history(store, owners[0]) == [
        (first.id, TODAY, "studied", 3, "original", None),
        (correction.id, TODAY, "rest", None, "correction", first.id),
    ]
    assert first.source == correction.source == "self_report"
    assert first.created_at.tzinfo is not None


@pytest.mark.parametrize("omitted_date", [None, ""])
def test_date_omitted_retry_keeps_original_date_across_local_midnight(
    store: Store, owners: tuple[int, int], omitted_date: str | None
) -> None:
    learning = LearningStore(store)
    before = datetime(2026, 11, 4, 7, 59, tzinfo=UTC)
    after = datetime(2026, 11, 4, 8, 1, tzinfo=UTC)
    with user_scope(owners[0]):
        learning.set_settings(
            timezone="America/Los_Angeles", typical_session_minutes=None
        )
        first = learning.checkin(
            study_date=omitted_date,
            status="studied",
            minutes=2,
            note="before midnight",
            idempotency_key="midnight",
            now=before,
        )
        assert first.study_date == date(2026, 11, 3)
        assert (
            learning.checkin(
                study_date=omitted_date,
                status="studied",
                minutes=2,
                note="before midnight",
                idempotency_key="midnight",
                now=after,
            )
            == first
        )
        with pytest.raises(Conflict):
            learning.checkin(
                study_date="2026-11-04",
                status="studied",
                minutes=2,
                note="before midnight",
                idempotency_key="midnight",
                now=after,
            )
    assert len(_history(store, owners[0])) == 1


@pytest.mark.parametrize("explicit_date", ["2026-11-04", date(2026, 11, 4)])
def test_explicit_date_retry_survives_timezone_moving_today_backward(
    store: Store, owners: tuple[int, int], explicit_date: str | date
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        learning.set_settings(timezone="Pacific/Kiritimati", typical_session_minutes=30)
        first = learning.checkin(
            study_date=explicit_date,
            status="studied",
            idempotency_key="moved-zone",
            now=NOW,
        )
        learning.set_settings(
            timezone="America/Los_Angeles", typical_session_minutes=30
        )
        assert learning.activity(days=1, now=NOW)["today"] == "2026-11-03"
        assert (
            learning.checkin(
                study_date=explicit_date,
                status="studied",
                idempotency_key="moved-zone",
                now=NOW,
            )
            == first
        )
        with pytest.raises(Conflict):
            learning.checkin(
                study_date="2026-11-05",
                status="studied",
                idempotency_key="moved-zone",
                now=NOW,
            )
        with pytest.raises(BadInput, match="YYYY-MM-DD"):
            learning.checkin(
                study_date="20261104",
                status="studied",
                idempotency_key="moved-zone",
                now=NOW,
            )
        with pytest.raises(BadInput, match="future"):
            learning.checkin(
                study_date=explicit_date,
                status="studied",
                idempotency_key="new-key",
                now=NOW,
            )
    assert len(_history(store, owners[0])) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"study_date": "2026-11-03"},
        {"status": "rest", "minutes": None},
        {"minutes": 8},
        {"note": "different private note"},
    ],
)
def test_idempotency_conflicts_do_not_leak_or_append_payload(
    store: Store, owners: tuple[int, int], changed: dict[str, Any]
) -> None:
    learning = LearningStore(store)
    payload: dict[str, Any] = dict(
        study_date=TODAY,
        status="studied",
        minutes=4,
        note="original private note",
        idempotency_key="private-key",
        now=NOW,
    )
    with user_scope(owners[0]):
        learning.checkin(**payload)
        with pytest.raises(Conflict) as conflict:
            learning.checkin(**(payload | changed))
        assert "private" not in str(conflict.value)
    assert len(_history(store, owners[0])) == 1


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_checkins_serialize_retries_and_corrections(
    store: Store, owners: tuple[int, int], same_key: bool
) -> None:
    learning = LearningStore(store)
    ready = Barrier(4)

    def append(index: int) -> int:
        with user_scope(owners[0]):
            ready.wait(timeout=10)
            return learning.checkin(
                status="studied",
                minutes=1,
                idempotency_key="same" if same_key else f"key-{index}",
                now=NOW,
            ).id

    with ThreadPoolExecutor(max_workers=4) as executor:
        event_ids = list(executor.map(append, range(4)))
    rows = _history(store, owners[0])
    if same_key:
        assert len(set(event_ids)) == len(rows) == 1
    else:
        assert len(set(event_ids)) == len(rows) == 4
        assert [row[5] for row in rows] == [None, *[row[0] for row in rows[:-1]]]
    with user_scope(owners[0]):
        assert learning.activity(days=1, now=NOW)["days"][0]["event_id"] == rows[-1][0]


def test_same_owner_foreign_key_refuses_cross_user_supersession(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        first = learning.checkin(status="studied", idempotency_key="a", now=NOW)
    with store.pool.connection() as conn, conn.transaction():
        conn.execute(
            "SELECT set_config('precis.learning_user_id', %s, true)", (str(owners[1]),)
        )
        with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
            conn.execute(
                "INSERT INTO learning_events "
                "(user_id, study_date, status, idempotency_key, supersedes_id) "
                "VALUES (%s, %s, 'rest', 'b', %s)",
                (owners[1], TODAY, first.id),
            )
    assert _history(store, owners[1]) == []


@pytest.mark.parametrize("minutes", [None, 0, 1, 1440])
def test_duration_is_optional_guidance_and_never_a_completion_threshold(
    store: Store, owners: tuple[int, int], minutes: int | None
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        learning.set_settings(timezone="UTC", typical_session_minutes=90)
        event = learning.checkin(
            status="studied", minutes=minutes, idempotency_key="short", now=NOW
        )
        day = learning.activity(days=1, now=NOW)["days"][0]
        assert day["status"] == "studied"
        assert day["minutes"] == event.minutes == minutes
        assert learning.activity(days=1, now=NOW)["last_studied"] == TODAY.isoformat()


def test_timezone_midnight_future_refusal_and_reported_date_survive_zone_change(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    before = datetime(2026, 11, 4, 7, 59, tzinfo=UTC)
    after = datetime(2026, 11, 4, 8, 1, tzinfo=UTC)
    with user_scope(owners[0]):
        learning.set_settings(
            timezone="America/Los_Angeles", typical_session_minutes=None
        )
        assert learning.activity(days=1, now=before)["today"] == "2026-11-03"
        assert learning.activity(days=1, now=after)["today"] == "2026-11-04"
        with pytest.raises(BadInput, match="future"):
            learning.checkin(
                study_date="2026-11-04",
                status="studied",
                idempotency_key="future",
                now=before,
            )
        event = learning.checkin(
            study_date="", status="studied", idempotency_key="local", now=before
        )
        assert event.study_date == date(2026, 11, 3)
        learning.set_settings(timezone="Pacific/Kiritimati", typical_session_minutes=15)
        activity = learning.activity(days=2, now=before)
        assert activity["timezone"] == "Pacific/Kiritimati"
        assert activity["today"] == "2026-11-04"
        assert [day["status"] for day in activity["days"]] == ["studied", "unrecorded"]
        assert learning.get_event(event.id).study_date == date(2026, 11, 3)


def test_last_studied_considers_current_reports_beyond_the_display_window(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    old = TODAY - timedelta(days=180)
    with user_scope(owners[0]):
        learning.checkin(
            study_date=old, status="studied", idempotency_key="old", now=NOW
        )
        activity = learning.activity(days=3, now=NOW)
        assert activity["last_studied"] == old.isoformat()
        assert len(activity["days"]) == 3
        assert all(day["status"] == "unrecorded" for day in activity["days"])
        assert [day["date"] for day in activity["days"]] == [
            (TODAY - timedelta(days=offset)).isoformat() for offset in (2, 1, 0)
        ]
        learning.checkin(
            study_date=old, status="rest", idempotency_key="correct", now=NOW
        )
        assert learning.activity(days=3, now=NOW)["last_studied"] is None


@pytest.mark.parametrize(
    "changed",
    [
        {"status": "reviewed"},
        {"status": None},
        {"minutes": True},
        {"minutes": -1},
        {"minutes": 1441},
        {"minutes": "3"},
        {"status": "rest", "minutes": 0},
        {"note": None},
        {"note": "x" * 501},
        {"note": "contains\x00nul"},
        {"idempotency_key": ""},
        {"idempotency_key": " "},
        {"idempotency_key": "x" * 129},
        {"idempotency_key": None},
        {"study_date": "2026-02-30"},
        {"study_date": "20261103"},
        {"study_date": 3},
        {"study_date": datetime(2026, 11, 3, tzinfo=UTC)},
        {"now": datetime(2026, 11, 3)},
        {"now": "2026-11-03"},
    ],
)
def test_invalid_checkin_is_actionable_and_does_not_append(
    store: Store, owners: tuple[int, int], changed: dict[str, Any]
) -> None:
    learning = LearningStore(store)
    payload: dict[str, Any] = dict(status="studied", idempotency_key="k", now=NOW)
    with user_scope(owners[0]), pytest.raises(BadInput) as error:
        learning.checkin(**(payload | changed))
    assert error.value.next
    assert _history(store, owners[0]) == []


@pytest.mark.parametrize(
    "changed",
    [
        {"timezone": "Not/AZone"},
        {"timezone": None},
        {"timezone": "/etc/passwd"},
        {"typical_session_minutes": True},
        {"typical_session_minutes": 0},
        {"typical_session_minutes": 1441},
        {"typical_session_minutes": "30"},
    ],
)
def test_invalid_settings_are_refused_without_changing_saved_preferences(
    store: Store, owners: tuple[int, int], changed: dict[str, Any]
) -> None:
    learning = LearningStore(store)
    with user_scope(owners[0]):
        learning.set_settings(timezone="UTC", typical_session_minutes=None)
        payload: dict[str, Any] = dict(
            timezone="Europe/Dublin", typical_session_minutes=15
        )
        with pytest.raises(BadInput) as error:
            learning.set_settings(**(payload | changed))
        assert error.value.next
        assert learning.settings() == LearningSettings("UTC", None)


@pytest.mark.parametrize("days", [0, 91, True, "3", None])
def test_invalid_activity_window_is_refused(
    store: Store, owners: tuple[int, int], days: Any
) -> None:
    with user_scope(owners[0]), pytest.raises(BadInput, match="1 to 90"):
        LearningStore(store).activity(days=days, now=NOW)


def test_checkins_do_not_create_refs_or_change_existing_mastery(
    store: Store, owners: tuple[int, int]
) -> None:
    from precis.reading.promote import create_concept

    concept = create_concept(store, name="existing concept", definition="unchanged")
    store.update_ref(concept, meta_patch={"mastery": 0.35, "state": "active"})

    def snapshot() -> tuple[Any, ...]:
        with store.pool.connection() as conn:
            return tuple(
                conn.execute(query).fetchall()
                for query in (
                    "SELECT ref_id, meta, retired_at FROM refs ORDER BY ref_id",
                    "SELECT chunk_id, text FROM chunks ORDER BY chunk_id",
                    "SELECT chunk_id, embedder FROM chunk_embeddings ORDER BY chunk_id, embedder",
                )
            )

    before = snapshot()
    with user_scope(owners[0]):
        learning = LearningStore(store)
        learning.checkin(status="studied", minutes=1, idempotency_key="k", now=NOW)
        learning.activity(now=NOW)
    assert snapshot() == before


def test_forced_rls_hides_other_owners_and_refuses_unbound_or_foreign_writes(
    store: Store, owners: tuple[int, int]
) -> None:
    learning = LearningStore(store)
    for owner in owners:
        with user_scope(owner):
            learning.checkin(status="studied", idempotency_key="k", now=NOW)
    role = "learning_probe_" + uuid.uuid4().hex[:12]
    with store.pool.connection() as conn:
        try:
            try:
                conn.execute(
                    sql.SQL("CREATE ROLE {} NOLOGIN NOBYPASSRLS").format(
                        sql.Identifier(role)
                    )
                )
                conn.execute(
                    sql.SQL("GRANT {} TO CURRENT_USER").format(sql.Identifier(role))
                )
            except psycopg.errors.InsufficientPrivilege:
                conn.rollback()
                pytest.skip(
                    "test DB role cannot create and assume a non-BYPASSRLS probe role"
                )
            conn.execute(
                sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(
                    sql.Identifier(role)
                )
            )
            conn.execute(
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE ON learning_events, learning_settings TO {}"
                ).format(sql.Identifier(role))
            )
            conn.execute(
                sql.SQL(
                    "GRANT USAGE ON SEQUENCE learning_events_event_id_seq TO {}"
                ).format(sql.Identifier(role))
            )
            flags = conn.execute(
                "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
                "WHERE oid IN ('learning_events'::regclass, 'learning_settings'::regclass)"
            ).fetchall()
            assert flags == [(True, True), (True, True)]
            conn.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
            assert conn.execute(
                "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
            ).fetchone() == (False, False)
            conn.execute("SELECT set_config('precis.learning_user_id', '', true)")
            assert conn.execute("SELECT user_id FROM learning_events").fetchall() == []
            assert (
                conn.execute("SELECT user_id FROM learning_settings").fetchall() == []
            )
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                conn.transaction(),
            ):
                conn.execute(
                    "INSERT INTO learning_events (user_id, study_date, status, idempotency_key) "
                    "VALUES (%s, %s, 'rest', 'unbound')",
                    (owners[0], TODAY),
                )
            conn.execute(
                "SELECT set_config('precis.learning_user_id', %s, true)",
                (str(owners[0]),),
            )
            assert conn.execute("SELECT user_id FROM learning_events").fetchall() == [
                (owners[0],)
            ]
            assert conn.execute("SELECT user_id FROM learning_settings").fetchall() == [
                (owners[0],)
            ]
            assert (
                conn.execute(
                    "UPDATE learning_settings SET timezone = 'UTC' WHERE user_id = %s RETURNING user_id",
                    (owners[1],),
                ).fetchall()
                == []
            )
            with (
                pytest.raises(psycopg.errors.InsufficientPrivilege),
                conn.transaction(),
            ):
                conn.execute(
                    "INSERT INTO learning_events (user_id, study_date, status, idempotency_key) "
                    "VALUES (%s, %s, 'rest', 'foreign')",
                    (owners[1], TODAY),
                )
        finally:
            # Transactional role/grants disappear with the probe; no fleet roles change.
            conn.rollback()
        cleared = conn.execute(
            "SELECT current_setting('precis.learning_user_id', true)"
        ).fetchone()
        assert cleared is not None and cleared[0] in (None, "")
