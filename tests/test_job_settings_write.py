"""``put(kind='job', id='/settings')`` agent flag write, revert and
history (gr635487)."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from typing import Any

import psycopg
import pytest

from precis import settings as psettings
from precis.dispatch import Hub
from precis.errors import BadInput, Upstream
from precis.handlers.job import JobHandler
from precis.store import Store

KEY = "inbound_chase.enabled"


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


@pytest.fixture(autouse=True)
def _clean(store: Store) -> Iterator[None]:
    def wipe() -> None:
        with store.pool.connection() as conn:
            conn.execute("DELETE FROM app_settings WHERE key = %s", (KEY,))
            conn.execute("DELETE FROM app_settings_audit WHERE key = %s", (KEY,))
            conn.commit()
        psettings.invalidate(KEY)

    wipe()
    yield
    wipe()


def _args(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "key": KEY,
        "value": True,
        "reason": "watch spend",
        "motivating_ref": "td489406",
        "confirmed_by_user": True,
        "user_said": "yes, turn chase on",
    }
    base.update(over)
    return base


def test_put_settings_requires_confirmation_and_allowlist(
    jobs: JobHandler, store: Store
) -> None:
    with pytest.raises(BadInput):
        jobs.put(id="/settings", **_args(user_said=""))
    for invalid in (False, 1, "true", "yes"):
        with pytest.raises(BadInput, match="explicit"):
            jobs.put(id="/settings", **_args(confirmed_by_user=invalid))
    with pytest.raises(BadInput, match="motivating_ref"):
        jobs.put(id="/settings", **_args(motivating_ref=" "))
    with pytest.raises(BadInput, match="motivating_ref"):
        jobs.put(id="/settings", **_args(revert=True, motivating_ref=None))
    with pytest.raises(BadInput):
        jobs.put(id="/settings", **_args(key="llm.local_first.enabled"))
    jobs.put(id="/settings", **_args())
    assert psettings.get_bool(KEY, store=store) is True
    rows = psettings.audit_history(KEY, store=store)
    assert len(rows) == 1
    assert rows[0]["new"] == "true" and rows[0]["motivating_ref"] == "td489406"
    assert rows[0]["confirmed_quote"] == "yes, turn chase on"


def test_revert_restores_previous_value_and_history_lists_changes(
    jobs: JobHandler, store: Store
) -> None:
    jobs.put(id="/settings", **_args())
    jobs.put(id="/settings", **_args(value=False))
    assert psettings.get_bool(KEY, store=store) is False
    jobs.put(id="/settings", **_args(revert=True, value=None))
    assert psettings.get_bool(KEY, store=store) is True
    body = jobs.get(id="/settings", history=KEY).body
    assert body.count("- #") == 3
    assert "false -> true" in body
    latest, *_ = psettings.audit_history(KEY, store=store)
    for field in ("actor", "reason", "motivating_ref", "confirmed_quote"):
        assert latest[field]
        assert latest[field] not in body


def test_settings_view_shows_effective_flag_and_limits_history(
    jobs: JobHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("PRECIS_INBOUND_CHASE_ENABLED", raising=False)
    assert f"{KEY}: false (default)" in jobs.get(id="/settings").body
    monkeypatch.setenv("PRECIS_INBOUND_CHASE_ENABLED", "0")
    assert f"{KEY}: false (env)" in jobs.get(id="/settings").body
    jobs.put(id="/settings", **_args())
    assert f"{KEY}: true (db)" in jobs.get(id="/settings").body
    with pytest.raises(BadInput, match="agent-settable"):
        jobs.get(id="/settings", history="deploy.fleet")
    jobs.put(id="/settings", **_args(revert=True, value=None))
    assert f"{KEY}: false (env)" in jobs.get(id="/settings").body


def test_missing_audit_table_rolls_back_flag_write(
    jobs: JobHandler, store: Store
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "ALTER TABLE app_settings_audit RENAME TO unavailable_settings_audit"
        )
    try:
        with pytest.raises(Upstream, match="audited") as failure:
            jobs.put(id="/settings", **_args())
        assert isinstance(failure.value.__cause__, psycopg.errors.UndefinedTable)
        with store.pool.connection() as conn:
            assert (
                conn.execute(
                    "SELECT value FROM app_settings WHERE key = %s", (KEY,)
                ).fetchone()
                is None
            )
    finally:
        with store.pool.connection() as conn:
            conn.execute(
                "ALTER TABLE unavailable_settings_audit RENAME TO app_settings_audit"
            )


def test_concurrent_initial_writes_record_each_predecessor(
    jobs: JobHandler, store: Store
) -> None:
    start = Barrier(2)

    def write(value: bool) -> None:
        start.wait(timeout=10)
        jobs.put(id="/settings", **_args(value=value))

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(write, (True, False)))

    latest, first = psettings.audit_history(KEY, store=store)
    assert first["old"] is None
    assert latest["old"] == first["new"]
    assert latest["new"] != first["new"]
