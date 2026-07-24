"""Tests for the quest loop reconciler (:mod:`precis.quest.loop`).

Replaces the old inline-tick allocator worker pass: not "which quest ticks
next" but "does every active quest have a live ``quest_tick`` coordinator
loop, and is a rested one re-armed?". Runs against real PG (the ``store``
fixture) since the guarantee rides ``JobHandler``'s real idem-dedup SQL.
"""

from __future__ import annotations

import re
from typing import Any

from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import loop as loop_mod
from precis.store import Store
from precis.store.types import Tag


def _mk_quest(store: Store, text: str) -> int:
    h = QuestHandler(hub=Hub(store=store))
    resp = h.put(text=text)
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, resp.body
    return int(m.group(1))


def _job_meta(store: Store, job_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (job_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _job_executor_and_type(store: Store, job_id: int) -> tuple[str, str]:
    meta = _job_meta(store, job_id)
    return str(meta.get("executor")), str(meta.get("job_type"))


def _set_status(store: Store, job_id: int, status: str) -> None:
    store.add_tag(
        job_id,
        Tag.closed("STATUS", status),
        set_by="agent",
        replace_prefix=True,
    )


class TestEnsureQuestLoop:
    def test_first_call_mints_a_coordinator_loop(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        job_id, created = loop_mod.ensure_quest_loop(store, q)

        assert created is True
        assert job_id is not None
        executor, job_type = _job_executor_and_type(store, job_id)
        assert executor == "coordinator"
        assert job_type == "quest_tick"

        meta = _job_meta(store, job_id)
        assert meta["idem_key"] == f"quest_tick:{q}"
        assert meta["params"]["quest_id"] == q
        assert meta["params"]["tier"] == "local-big"
        assert meta["params"]["target_node"] == "spark"

    def test_second_call_while_non_terminal_does_not_mint_again(
        self, store: Store
    ) -> None:
        q = _mk_quest(store, "A striving")
        first_id, first_created = loop_mod.ensure_quest_loop(store, q)
        second_id, second_created = loop_mod.ensure_quest_loop(store, q)

        assert first_created is True
        assert second_created is False
        assert second_id == first_id

    def test_terminal_loop_is_re_armed_with_a_fresh_job(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        first_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert first_id is not None
        _set_status(store, first_id, "succeeded")

        second_id, second_created = loop_mod.ensure_quest_loop(store, q)

        assert second_created is True
        assert second_id is not None
        assert second_id != first_id

    def test_per_quest_loop_meta_override(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (
                    Jsonb({"loop": {"tier": "cloud-super", "target_node": "melchior"}}),
                    q,
                ),
            )
            conn.commit()

        job_id, created = loop_mod.ensure_quest_loop(store, q)
        assert created is True
        assert job_id is not None
        meta = _job_meta(store, job_id)
        assert meta["params"]["tier"] == "cloud-super"
        assert meta["params"]["target_node"] == "melchior"

    def test_never_raises_on_a_bad_quest_id(self, store: Store) -> None:
        job_id, created = loop_mod.ensure_quest_loop(store, -1)
        assert job_id is None
        assert created is False


class TestReconcileQuestLoops:
    def test_disabled_gate_is_a_no_op(self, store: Store) -> None:
        _mk_quest(store, "A striving")
        out = loop_mod.reconcile_quest_loops(store, enabled=False)
        assert out == {"enabled": False, "cooled": 0, "ensured": 0, "minted": 0}

    def test_enabled_ensures_each_active_quest_and_tallies(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [999])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [1, 2, 3])

        calls: list[int] = []

        def _fake_ensure(
            _store: Any, quest_id: int, *, hub: Any = None
        ) -> tuple[int | None, bool]:
            calls.append(quest_id)
            # quest 2 already has a live loop (not freshly minted).
            return (100 + quest_id, quest_id != 2)

        monkeypatch.setattr(loop_mod, "ensure_quest_loop", _fake_ensure)

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert calls == [1, 2, 3]
        assert out == {"enabled": True, "cooled": 1, "ensured": 3, "minted": 2}

    def test_enabled_via_env_default(self, store: Store, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ENABLED", "1")
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [])

        out = loop_mod.reconcile_quest_loops(store)
        assert out == {"enabled": True, "cooled": 0, "ensured": 0, "minted": 0}

    def test_a_failing_ensure_does_not_stop_the_rest(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [1, 2])

        def _fake_ensure(
            _store: Any, quest_id: int, *, hub: Any = None
        ) -> tuple[int | None, bool]:
            if quest_id == 1:
                return None, False
            return 202, True

        monkeypatch.setattr(loop_mod, "ensure_quest_loop", _fake_ensure)

        out = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert out == {"enabled": True, "cooled": 0, "ensured": 1, "minted": 1}
