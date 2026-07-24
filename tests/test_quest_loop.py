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


def _current_status(store: Store, job_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
             WHERE rt.ref_id = %s AND t.namespace = 'STATUS'
             ORDER BY rt.created_at DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    return str(row[0]) if row else None


def _set_lease(store: Store, job_id: int, lease_sql: str) -> None:
    """Stamp ``meta.lease_until`` from a SQL expression evaluated server-side
    (e.g. ``now() - interval '1 hour'``) so tests never fight DB/Python tz."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || "
            f"jsonb_build_object('lease_until', ({lease_sql})::text) "
            "WHERE ref_id = %s",
            (job_id,),
        )
        conn.commit()


def _non_terminal_loop_ids(store: Store, quest_id: int) -> list[int]:
    """Every non-terminal ``quest_tick`` coordinator job for ``quest_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
             WHERE r.kind = 'job' AND r.deleted_at IS NULL
               AND r.meta->>'idem_key' = %s
               AND NOT EXISTS (
                     SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                        AND t.value = ANY(ARRAY['succeeded', 'failed', 'cancelled'])
                   )
             ORDER BY r.ref_id
            """,
            (f"quest_tick:{quest_id}",),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _arm_orphan(store: Store, quest_id: int, *, lease_sql: str) -> int:
    """Mint a loop, then force it to look like a mid-run coordinator slice:
    ``STATUS:running`` with a ``meta.lease_until`` set from ``lease_sql``."""
    job_id, _ = loop_mod.ensure_quest_loop(store, quest_id)
    assert job_id is not None
    _set_status(store, job_id, "running")
    _set_lease(store, job_id, lease_sql)
    return job_id


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
        assert out == {
            "enabled": False,
            "cooled": 0,
            "reaped": 0,
            "ensured": 0,
            "minted": 0,
        }

    def test_enabled_ensures_each_active_quest_and_tallies(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [999])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [1, 2, 3])
        # Keep this a pure test of the tally logic — no real orphan rows exist.
        monkeypatch.setattr(loop_mod, "_reap_orphaned_loop", lambda *_a, **_k: None)

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
        assert out == {
            "enabled": True,
            "cooled": 1,
            "reaped": 0,
            "ensured": 3,
            "minted": 2,
        }

    def test_enabled_via_env_default(self, store: Store, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ENABLED", "1")
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [])

        out = loop_mod.reconcile_quest_loops(store)
        assert out == {
            "enabled": True,
            "cooled": 0,
            "reaped": 0,
            "ensured": 0,
            "minted": 0,
        }

    def test_a_failing_ensure_does_not_stop_the_rest(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [1, 2])
        monkeypatch.setattr(loop_mod, "_reap_orphaned_loop", lambda *_a, **_k: None)

        def _fake_ensure(
            _store: Any, quest_id: int, *, hub: Any = None
        ) -> tuple[int | None, bool]:
            if quest_id == 1:
                return None, False
            return 202, True

        monkeypatch.setattr(loop_mod, "ensure_quest_loop", _fake_ensure)

        out = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert out == {
            "enabled": True,
            "cooled": 0,
            "reaped": 0,
            "ensured": 1,
            "minted": 1,
        }


class TestReapOrphanedLoop:
    """Reboot self-heal: one reconcile pass terminalizes a provably-orphaned
    coordinator loop and re-mints exactly one fresh loop — while a live loop and
    a just-minted queued loop are both left untouched."""

    def _make_active_quest(self, store: Store) -> int:
        q = _mk_quest(store, "A striving")
        _set_status(store, q, "active")
        return q

    def test_orphaned_running_loop_is_reaped_and_re_minted(self, store: Store) -> None:
        q = self._make_active_quest(store)
        # A coordinator slice that died mid-run: STATUS:running, lease long past.
        orphan_id = _arm_orphan(store, q, lease_sql="now() - interval '1 hour'")

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 1
        assert out["minted"] == 1
        assert out["ensured"] == 1
        # The orphan is now terminal (cancelled, not failed)…
        assert _current_status(store, orphan_id) == "cancelled"
        # …and exactly one fresh non-terminal loop exists, different from it.
        live = _non_terminal_loop_ids(store, q)
        assert live != [orphan_id]
        assert len(live) == 1
        assert live[0] != orphan_id

    def test_second_pass_is_idempotent(self, store: Store) -> None:
        q = self._make_active_quest(store)
        _arm_orphan(store, q, lease_sql="now() - interval '1 hour'")

        first = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert first["reaped"] == 1 and first["minted"] == 1
        after_first = _non_terminal_loop_ids(store, q)
        assert len(after_first) == 1

        # The re-minted loop is fresh queued (null lease) → not an orphan; its
        # idem blocks another mint. Nothing to reap or re-mint.
        second = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert second["reaped"] == 0
        assert second["minted"] == 0
        assert _non_terminal_loop_ids(store, q) == after_first

    def test_live_loop_future_lease_is_untouched(self, store: Store) -> None:
        q = self._make_active_quest(store)
        live_id = _arm_orphan(store, q, lease_sql="now() + interval '1 hour'")

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _current_status(store, live_id) == "running"
        assert _non_terminal_loop_ids(store, q) == [live_id]

    def test_just_minted_queued_loop_is_untouched(self, store: Store) -> None:
        q = self._make_active_quest(store)
        job_id, created = loop_mod.ensure_quest_loop(store, q)  # queued, null lease
        assert created is True and job_id is not None

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        # A null-lease queued loop is never an orphan, and its idem blocks a
        # re-mint — so the pass neither reaps nor mints.
        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _non_terminal_loop_ids(store, q) == [job_id]

    def test_expired_within_grace_is_not_reaped(
        self, store: Store, monkeypatch: Any
    ) -> None:
        # A slow-but-live 5-min-lease slice: lease expired only recently, still
        # inside the grace window → must NOT be mistaken for a reboot orphan.
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ORPHAN_GRACE_S", "600")
        q = self._make_active_quest(store)
        live_id = _arm_orphan(store, q, lease_sql="now() - interval '30 seconds'")

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _current_status(store, live_id) == "running"
        assert _non_terminal_loop_ids(store, q) == [live_id]
