"""Tests for the quest loop reconciler (:mod:`precis.quest.loop`).

Replaces the old inline-tick allocator worker pass: not "which quest ticks
next" but "does every active quest have a live ``quest_tick`` coordinator
loop, and is a rested one re-armed?". Runs against real PG (the ``store``
fixture) since the guarantee rides ``JobHandler``'s real idem-dedup SQL.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.quest import QuestHandler
from precis.quest import compute as compute_mod
from precis.quest import loop as loop_mod
from precis.store import Store
from precis.store.types import Tag

#: A minimal candidate structure spec — mirrors ``test_quest_compute.py``'s
#: own ``_SPEC``, kept local so this file's fixtures don't cross-import a
#: sibling test module.
_SPEC = {
    "cell": {"a": 8.4, "b": 8.4, "c": 24.0, "pbc": [True, True, False]},
    "ops": [{"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.5]}],
}


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


def _age_status(store: Store, job_id: int, age_sql: str) -> None:
    """Backdate the job's current STATUS tag row's ``created_at`` server-side
    (e.g. ``'40 minutes'``) so the failed-rest cooldown sees an aged failure
    without tests fighting DB/Python tz."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            UPDATE ref_tags rt SET created_at = (now() - (%s)::interval)
              FROM tags t
             WHERE rt.tag_id = t.tag_id AND t.namespace = 'STATUS'
               AND rt.ref_id = %s
            """,
            (age_sql, job_id),
        )
        conn.commit()


def _non_terminal_loop_ids(store: Store, quest_id: int) -> list[int]:
    """Every non-terminal ``quest_tick`` coordinator job for ``quest_id``."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id FROM refs r
             WHERE r.kind = 'job' AND r.retired_at IS NULL
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


def _insert_chunk(store: Store, job_id: int, *, age_sql: str) -> None:
    """Insert one body chunk for ``job_id``, backdated by ``age_sql`` (e.g.
    ``'2 minutes'``) — simulates a coordinator slice's evidence-of-life
    write (gr204309: a job that's still writing chunks must never be
    mistaken for a reboot orphan, lease notwithstanding)."""
    with store.pool.connection() as conn:
        conn.execute(
            """
            INSERT INTO chunks (ref_id, ord, chunk_kind, text, created_at)
            VALUES (%s, 0, 'job_event', 'slice progress', now() - (%s)::interval)
            """,
            (job_id, age_sql),
        )
        conn.commit()


def _backdate_created_at(store: Store, job_id: int, age_sql: str) -> None:
    """Backdate ``refs.created_at`` server-side (e.g. ``'20 minutes'``) — the
    dead-node-pin reap's grace-margin predicate."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - (%s)::interval WHERE ref_id = %s",
            (age_sql, job_id),
        )
        conn.commit()


def _pin_target_node(store: Store, job_id: int, target_node: str) -> None:
    """Stamp ``meta.params.target_node`` on an already-minted loop — mirrors
    a legacy/env/per-quest pin without going through ``ensure_quest_loop``
    (which no longer sets it by default, gr292747)."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{params,target_node}', "
            "to_jsonb(%s::text)) WHERE ref_id = %s",
            (target_node, job_id),
        )
        conn.commit()


def _upsert_host_heartbeat(store: Store, host: str, *, age_minutes: float) -> None:
    """Seed/refresh a ``host_heartbeat`` row (PK on ``host``, so upsert) —
    mirrors ``test_sweeper.py``'s own helper for the identical predicate."""
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) "
            "VALUES (%s, now() - (%s || ' minutes')::interval) "
            "ON CONFLICT (host) DO UPDATE SET ts = excluded.ts",
            (host, age_minutes),
        )
        conn.commit()


def _last_reap_event_payload(store: Store, job_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT payload FROM ref_events
             WHERE ref_id = %s AND event = 'loop-reaped'
             ORDER BY event_id DESC LIMIT 1
            """,
            (job_id,),
        ).fetchone()
    assert row is not None, f"no loop-reaped event for job {job_id}"
    return dict(row[0] or {})


class TestLoopParams:
    """gr292747: :func:`loop_mod._loop_params`'s ``target_node`` resolution —
    unset by default, env-overridable, quest-override wins over env."""

    def test_default_target_node_is_none(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        tier, target_node = loop_mod._loop_params(store, q)
        assert tier == "big"
        assert target_node is None

    def test_env_sets_target_node(self, store: Store, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_NODE", "melchior")
        q = _mk_quest(store, "A striving")
        _tier, target_node = loop_mod._loop_params(store, q)
        assert target_node == "melchior"

    def test_empty_env_reads_as_unset(self, store: Store, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_NODE", "")
        q = _mk_quest(store, "A striving")
        _tier, target_node = loop_mod._loop_params(store, q)
        assert target_node is None

    def test_quest_meta_override_wins_over_env(
        self, store: Store, monkeypatch: Any
    ) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_NODE", "melchior")
        q = _mk_quest(store, "A striving")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (Jsonb({"loop": {"target_node": "caspar"}}), q),
            )
            conn.commit()
        _tier, target_node = loop_mod._loop_params(store, q)
        assert target_node == "caspar"


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
        assert meta["params"]["tier"] == "big"
        # gr292747: unpinned by default — the key is OMITTED, not null, so
        # coordinator._claim_jobs's "no target_node → claimable by any
        # system worker" shape applies.
        assert "target_node" not in meta["params"]

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
                    Jsonb({"loop": {"tier": "frontier", "target_node": "melchior"}}),
                    q,
                ),
            )
            conn.commit()

        job_id, created = loop_mod.ensure_quest_loop(store, q)
        assert created is True
        assert job_id is not None
        meta = _job_meta(store, job_id)
        assert meta["params"]["tier"] == "frontier"
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
            "escalated": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 0,
            "minted": 0,
            "pathways_failed": 0,
            "pathways_redispatched": 0,
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
            "escalated": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 3,
            "minted": 2,
            "pathways_failed": 0,
            "pathways_redispatched": 0,
        }

    def test_enabled_via_env_default(self, store: Store, monkeypatch: Any) -> None:
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ENABLED", "1")
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _store: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _store: [])

        out = loop_mod.reconcile_quest_loops(store)
        assert out == {
            "enabled": True,
            "cooled": 0,
            "escalated": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 0,
            "minted": 0,
            "pathways_failed": 0,
            "pathways_redispatched": 0,
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
            "escalated": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 1,
            "minted": 1,
            "pathways_failed": 0,
            "pathways_redispatched": 0,
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

    def test_lease_expired_but_recent_chunk_is_not_reaped(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr204309: a lease past the grace window is NOT proof of death on
        its own — a chunk written inside the grace window is direct
        evidence the slice is still alive (the exact prod scenario: job
        204379 wrote a chunk 37 minutes after its lease had "expired")."""
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ORPHAN_GRACE_S", "600")
        q = self._make_active_quest(store)
        live_id = _arm_orphan(store, q, lease_sql="now() - interval '1 hour'")
        _insert_chunk(store, live_id, age_sql="1 minute")

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _current_status(store, live_id) == "running"
        assert _non_terminal_loop_ids(store, q) == [live_id]

    def test_lease_expired_and_no_recent_chunk_is_still_reaped(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """A lease-expired-beyond-grace loop with a STALE (outside-grace)
        chunk — or none at all — is still a provable orphan; the event
        payload carries the lease/chunk evidence for observability."""
        monkeypatch.setenv("PRECIS_QUEST_LOOP_ORPHAN_GRACE_S", "600")
        q = self._make_active_quest(store)
        orphan_id = _arm_orphan(store, q, lease_sql="now() - interval '1 hour'")
        _insert_chunk(store, orphan_id, age_sql="2 hours")  # stale, outside grace

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 1
        assert out["minted"] == 1
        assert _current_status(store, orphan_id) == "cancelled"

        payload = _last_reap_event_payload(store, orphan_id)
        assert payload["lease_until"] is not None
        assert payload["last_chunk_at"] is not None

        meta = _job_meta(store, orphan_id)
        assert meta.get("reap_note")

    def test_reap_event_payload_has_null_last_chunk_when_never_written(
        self, store: Store
    ) -> None:
        q = self._make_active_quest(store)
        orphan_id = _arm_orphan(store, q, lease_sql="now() - interval '1 hour'")

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 1
        payload = _last_reap_event_payload(store, orphan_id)
        assert payload["last_chunk_at"] is None
        assert payload["lease_until"] is not None


class TestReapDeadNodePinnedLoop:
    """gr292747: a never-claimed loop pinned to a provably dead node is
    reaped and re-minted in the same pass — division of labor with
    :class:`TestReapOrphanedLoop`'s claimed/lease-expired arm."""

    def _make_active_quest(self, store: Store) -> int:
        q = _mk_quest(store, "A striving")
        _set_status(store, q, "active")
        return q

    def _arm_pinned_orphan(
        self,
        store: Store,
        quest_id: int,
        *,
        target_node: str,
        age_sql: str = "20 minutes",
    ) -> int:
        """Mint a loop (unpinned, per gr292747's default), then pin it to a
        dead node and backdate it past the grace margin — mirrors a
        legacy/env/per-quest pin that never got claimed before its node
        died."""
        job_id, _ = loop_mod.ensure_quest_loop(store, quest_id)
        assert job_id is not None
        _pin_target_node(store, job_id, target_node)
        _backdate_created_at(store, job_id, age_sql)
        return job_id

    def test_dead_node_pinned_loop_is_reaped_and_re_minted(self, store: Store) -> None:
        from uuid import uuid4

        node = f"dead-{uuid4().hex[:8]}"
        q = self._make_active_quest(store)
        orphan_id = self._arm_pinned_orphan(store, q, target_node=node)

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 1
        assert out["minted"] == 1
        assert _current_status(store, orphan_id) == "cancelled"
        tags = {str(t) for t in store.tags_for(orphan_id)}
        assert "reaped:dead-node-pin" in tags
        # replace_prefix must swap the queued STATUS out, not stack a second
        # one next to it — a cancelled+queued job would still look claimable.
        assert sum(t.startswith("STATUS:") for t in tags) == 1
        meta = _job_meta(store, orphan_id)
        assert meta.get("reap_note")
        # A fresh, unpinned loop re-minted in the same pass.
        live = _non_terminal_loop_ids(store, q)
        assert len(live) == 1
        assert live[0] != orphan_id
        assert "target_node" not in _job_meta(store, live[0])["params"]

        payload = _last_reap_event_payload(store, orphan_id)
        assert payload["cause"] == "dead-node-pin"
        assert payload["target_node"] == node

    def test_fresh_host_heartbeat_is_not_reaped(self, store: Store) -> None:
        from uuid import uuid4

        node = f"hb-{uuid4().hex[:8]}"
        q = self._make_active_quest(store)
        _upsert_host_heartbeat(store, node, age_minutes=0.5)
        live_id = self._arm_pinned_orphan(store, q, target_node=node)

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _current_status(store, live_id) == "queued"
        assert _non_terminal_loop_ids(store, q) == [live_id]

    def test_claimed_lease_is_not_this_arms_business(self, store: Store) -> None:
        """A ``lease_until``-non-null loop is a claimed loop, dead or not —
        :func:`loop_mod._reap_orphaned_loop`'s job, never this arm's."""
        from uuid import uuid4

        node = f"leased-{uuid4().hex[:8]}"
        q = self._make_active_quest(store)
        job_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert job_id is not None
        _pin_target_node(store, job_id, node)
        _backdate_created_at(store, job_id, "20 minutes")
        # Claimed and still non-terminal — its (already-expired) lease means
        # the OTHER arm may reap it, but never this one directly: force the
        # other arm's chunk-evidence guard so only the dead-node-pin
        # predicate's ``lease_until IS NULL`` exclusion is under test.
        _set_status(store, job_id, "running")
        _set_lease(store, job_id, "now() - interval '1 hour'")

        assert loop_mod._reap_dead_node_pinned_loop(store, q, grace_s=600) is None

    def test_job_younger_than_grace_is_not_reaped(self, store: Store) -> None:
        from uuid import uuid4

        node = f"young-{uuid4().hex[:8]}"
        q = self._make_active_quest(store)
        live_id = self._arm_pinned_orphan(
            store, q, target_node=node, age_sql="1 minute"
        )

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _non_terminal_loop_ids(store, q) == [live_id]

    def test_unpinned_job_is_not_reaped(self, store: Store) -> None:
        q = self._make_active_quest(store)
        job_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert job_id is not None
        _backdate_created_at(store, job_id, "20 minutes")

        assert loop_mod._reap_dead_node_pinned_loop(store, q, grace_s=600) is None
        out = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert out["reaped"] == 0
        assert out["minted"] == 0
        assert _non_terminal_loop_ids(store, q) == [job_id]


class TestFailedRestBackoff:
    """RC1: a loop that rested ``STATUS:failed`` is held out of the
    re-mint for an escalating cooldown (``BASE * 2^(n-1)`` capped at ``MAX``,
    ``n`` = trailing failed-rest count); ``cancelled`` (reboot) / ``succeeded``
    (dry/punt/RC2) / non-terminal tops are not failed rests and re-mint now."""

    def _active_quest(self, store: Store) -> int:
        q = _mk_quest(store, "A striving")
        _set_status(store, q, "active")
        return q

    def _failed_loop(self, store: Store, quest_id: int, *, age: str) -> int:
        """Mint a loop, rest it ``STATUS:failed``, and backdate that failure."""
        job_id, _ = loop_mod.ensure_quest_loop(store, quest_id)
        assert job_id is not None
        _set_status(store, job_id, "failed")
        _age_status(store, job_id, age)
        return job_id

    def test_recent_failed_rest_is_cooling_down(self, store: Store) -> None:
        q = self._active_quest(store)
        self._failed_loop(store, q, age="1 minute")  # n=1 → 30-min window
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is True
        )

    def test_elapsed_failed_rest_re_mints(self, store: Store) -> None:
        q = self._active_quest(store)
        self._failed_loop(store, q, age="40 minutes")  # > 30-min window at n=1
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_second_consecutive_failure_widens_the_window(self, store: Store) -> None:
        # n=2 → 60-min window: a 40-min-old failure that would have re-minted at
        # n=1 (30-min window) is still cooling once a second failed rest precedes.
        q = self._active_quest(store)
        self._failed_loop(store, q, age="90 minutes")  # older failed rest
        self._failed_loop(store, q, age="40 minutes")  # most-recent failed rest
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is True
        )

    def test_max_caps_the_window(self, store: Store) -> None:
        # The cap bounds the window regardless of n: with a tiny max, a failure
        # older than it re-mints even with a trailing failed run.
        q = self._active_quest(store)
        self._failed_loop(store, q, age="10 minutes")
        self._failed_loop(store, q, age="5 minutes")
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=60)
            is False
        )

    def test_cancelled_top_is_not_a_failed_rest(self, store: Store) -> None:
        # A reboot-orphan reap terminalizes to cancelled → re-mint now, no cooldown.
        q = self._active_quest(store)
        job_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert job_id is not None
        _set_status(store, job_id, "cancelled")
        _age_status(store, job_id, "1 minute")
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_succeeded_top_is_not_a_failed_rest(self, store: Store) -> None:
        # A dry/punt/RC2 rest succeeds → re-mint now, no cooldown.
        q = self._active_quest(store)
        job_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert job_id is not None
        _set_status(store, job_id, "succeeded")
        _age_status(store, job_id, "1 minute")
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_non_terminal_top_is_not_cooling(self, store: Store) -> None:
        # A freshly-minted queued (or live running) loop is not a rest at all.
        q = self._active_quest(store)
        loop_mod.ensure_quest_loop(store, q)  # queued, no terminal STATUS
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_no_loops_yet_is_not_cooling(self, store: Store) -> None:
        q = self._active_quest(store)
        assert (
            loop_mod._failed_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_reconcile_tallies_backoff_and_skips_the_mint(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = self._active_quest(store)
        self._failed_loop(store, q, age="1 minute")
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["backoff"] == 1
        assert out["minted"] == 0
        assert out["ensured"] == 0
        # The re-mint was skipped: the failed loop stays terminal, no fresh loop.
        assert _non_terminal_loop_ids(store, q) == []

    def test_reconcile_re_mints_once_the_cooldown_elapses(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = self._active_quest(store)
        self._failed_loop(store, q, age="40 minutes")  # past the 30-min n=1 window
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["backoff"] == 0
        assert out["minted"] == 1
        assert out["ensured"] == 1
        assert len(_non_terminal_loop_ids(store, q)) == 1


class TestDryRestCooldownAndEscalation:
    """gr170252: a dry rest (``STATUS:succeeded`` + ``rest_reason: "dry"``)
    gets the SAME escalating cooldown as a real failure, instead of the
    "succeeded → re-mint immediately" exemption that let the loop spin
    forever; and a quest whose ``consecutive_dry_rests`` counter crosses
    ``PRECIS_QUEST_DRY_REST_ESCALATE`` is held out of re-minting for a long
    fixed cooldown (review finding #3: NOT forever — a running tick is the
    only thing that can reset the counter, so escalation must stay
    recoverable) until either the counter resets or the cooldown elapses."""

    def _active_quest(self, store: Store) -> int:
        q = _mk_quest(store, "A striving")
        _set_status(store, q, "active")
        return q

    def _dry_rest(self, store: Store, quest_id: int, *, age: str) -> int:
        """Mint a loop, rest it exactly as ``quest_tick``'s dry-tick budget
        does: ``STATUS:succeeded`` + ``meta.rest_reason = "dry"``, backdated."""
        job_id, _ = loop_mod.ensure_quest_loop(store, quest_id)
        assert job_id is not None
        _set_status(store, job_id, "succeeded")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                (Jsonb({"rest_reason": "dry"}), job_id),
            )
            conn.commit()
        _age_status(store, job_id, age)
        return job_id

    # ── (a) dry rest → cooldown applies, no immediate re-mint ─────────

    def test_recent_dry_rest_is_cooling_down(self, store: Store) -> None:
        q = self._active_quest(store)
        self._dry_rest(store, q, age="1 minute")  # n=1 → 30-min window
        assert (
            loop_mod._dry_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is True
        )

    def test_elapsed_dry_rest_re_mints(self, store: Store) -> None:
        q = self._active_quest(store)
        self._dry_rest(store, q, age="40 minutes")  # > 30-min window at n=1
        assert (
            loop_mod._dry_rest_cooldown_active(store, q, base_s=1800, max_s=21600)
            is False
        )

    def test_reconcile_skips_immediate_remint_after_a_dry_rest(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = self._active_quest(store)
        self._dry_rest(store, q, age="1 minute")
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["backoff"] == 1
        assert out["minted"] == 0
        # The dry rest stays terminal — no fresh loop minted this pass.
        assert _non_terminal_loop_ids(store, q) == []

    # ── (d) a productive (or punt/RC2) rest keeps re-minting immediately ──

    def test_productive_rest_without_rest_reason_remints_immediately(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = self._active_quest(store)
        job_id, _ = loop_mod.ensure_quest_loop(store, q)
        assert job_id is not None
        _set_status(store, job_id, "succeeded")  # no rest_reason stamped at all
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["backoff"] == 0
        assert out["minted"] == 1
        assert len(_non_terminal_loop_ids(store, q)) == 1

    # ── (b) 3 consecutive dry rests → alert + re-mint held in a long cooldown
    #        (gr170252 review finding #3: NOT forever — see the elapsed-
    #        cooldown test below) ────────────────────────────────────────

    def test_three_consecutive_dry_rests_escalate_and_suppress_reminting(
        self, store: Store, monkeypatch: Any
    ) -> None:
        from precis.alerts import open_alert_severity
        from precis.workers.job_types import quest_tick as qt

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        q = self._active_quest(store)

        # quest_tick's own counter — 3 consecutive dry rests, backed by a real
        # recent dry-rested loop (the recency source the escalated cooldown
        # keys off, same as _dry_rest_cooldown_active).
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        self._dry_rest(store, q, age="1 minute")
        assert (
            open_alert_severity(
                store, source="quest_tick", fingerprint=f"quest:dry-rest/{q}"
            )
            == "warn"
        )

        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])
        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["escalated"] == 1
        assert out["minted"] == 0
        assert out["ensured"] == 0
        assert out["backoff"] == 0
        assert out["reaped"] == 0
        # Nothing minted at all — not even a queued loop.
        assert _non_terminal_loop_ids(store, q) == []

    def test_escalation_re_mints_once_the_escalated_cooldown_elapses(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """gr170252 review finding #3: escalation is a long cooldown, not a
        permanent lockout — once ``PRECIS_QUEST_DRY_REST_ESCALATE_COOLDOWN_S``
        has elapsed since the most recent dry rest, re-minting resumes even
        though the ``consecutive_dry_rests`` counter itself hasn't reset (only
        a running tick can reset it, so it must be reachable again)."""
        from precis.workers.job_types import quest_tick as qt

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE_COOLDOWN_S", "3600")
        q = self._active_quest(store)

        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        self._dry_rest(store, q, age="2 hours")  # past the 1h escalated cooldown

        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])
        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["escalated"] == 0
        assert out["minted"] == 1
        assert len(_non_terminal_loop_ids(store, q)) == 1

    def test_dry_rest_escalation_active_skip_and_elapsed(self, store: Store) -> None:
        """Unit-level check of :func:`loop_mod._dry_rest_escalation_active`
        itself, mirroring the neighboring cooldown-function tests above."""
        q = self._active_quest(store)
        store.update_ref(q, meta_patch={"consecutive_dry_rests": 3})
        self._dry_rest(store, q, age="1 hour")
        assert (
            loop_mod._dry_rest_escalation_active(
                store, q, threshold=3, cooldown_s=86400
            )
            is True
        )

        # Advance the same recency source past the (default 24h) cooldown —
        # the gate opens even though the counter is untouched.
        self._dry_rest(store, q, age="25 hours")
        assert (
            loop_mod._dry_rest_escalation_active(
                store, q, threshold=3, cooldown_s=86400
            )
            is False
        )

    def test_below_threshold_does_not_escalate(
        self, store: Store, monkeypatch: Any
    ) -> None:
        from precis.workers.job_types import quest_tick as qt

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        q = self._active_quest(store)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)  # only 2 — below threshold

        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])
        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["escalated"] == 0
        assert out["minted"] == 1

    # ── (c) frontier improvement resets the counter, re-mint resumes ──────

    def test_frontier_improvement_resets_the_counter_and_remint_resumes(
        self, store: Store, monkeypatch: Any
    ) -> None:
        from precis.workers.job_types import quest_tick as qt

        monkeypatch.setenv("PRECIS_QUEST_DRY_REST_ESCALATE", "3")
        q = self._active_quest(store)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        qt._register_dry_rest(store, q)
        # 1 hour: past the ordinary dry-rest cooldown's 30-min (n=1) window,
        # so once the counter resets below only the escalated cooldown could
        # still be holding it back — but still well inside the (default 24h)
        # escalated cooldown, so the "blocked" pass below is exercising
        # escalation specifically, not the ordinary sibling cooldown.
        self._dry_rest(store, q, age="1 hour")

        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])
        blocked = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert blocked["escalated"] == 1
        assert blocked["minted"] == 0

        # A real tick's cascade update would reset ticks_since_frontier_improve
        # to 0 on genuine progress — simulate that and let quest_tick's own
        # detector clear the escalation counter.
        store.update_ref(q, meta_patch={"ticks_since_frontier_improve": 0})
        assert qt._frontier_improved_this_tick(store, q) is True
        qt._reset_dry_rest_counter(store, q)

        resumed = loop_mod.reconcile_quest_loops(store, enabled=True)
        assert resumed["escalated"] == 0
        assert resumed["minted"] == 1
        assert len(_non_terminal_loop_ids(store, q)) == 1


# ── Orphaned pathway stub sweep (PART A) ─────────────────────────────────


def _pathway_meta(store: Store, pathway_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (pathway_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


class _PathwayTreeMixin:
    """Shared fan-out-tree builders — candidate -> pathway + T_agg todo ->
    seed todo(s) -> seed job(s) — mirroring
    :func:`precis.quest.compute.dispatch_autocatpath`'s own tree shape
    (``TestStuckSeedFailure`` in ``test_quest_compute.py`` is the sibling
    fixture for the compute-side query over the same tree).

    An autouse fixture applies the ``precis_pathway`` plugin's own
    migration (idempotent) before every test in a subclass, so the
    ``pathway`` kind exists in this test DB — mirrors ``test_pathway_
    plugin.py``'s own ``pathway_store`` fixture; the plugin isn't installed
    as a core dep, so its ``kinds`` row is otherwise absent here. Kept as a
    fixture rather than a per-test call so every ``store``-typed test method
    below is unchanged (just ``store: Store``, no extra fixture param).
    """

    _n = 0

    @pytest.fixture(autouse=True)
    def _seed_pathway_kind(self, store: Store) -> None:
        import precis_pathway

        migrations_dir = Path(precis_pathway.__file__).parent / "migrations"
        with store.pool.connection() as conn:
            for sql_file in sorted(migrations_dir.glob("*.sql")):
                body = sql_file.read_text(encoding="utf-8")
                body = body.replace("BEGIN;", "").replace("COMMIT;", "")
                conn.execute(body)
            conn.commit()

    def _candidate(self, store: Store, qid: int) -> int:
        # A distinct frac per call so content-addressed dedup
        # (``ensure_candidate``) never collapses two calls in the same test
        # onto one structure — each test wants its own independent tree.
        _PathwayTreeMixin._n += 1
        spec = {
            **_SPEC,
            "ops": [
                {
                    "op": "add_atom",
                    "element": "Fe",
                    "frac": [0.0, 0.0, 0.5 + self._n * 0.001],
                }
            ],
        }
        sid = compute_mod.ensure_candidate(
            store, qid, {"name": "Fe", "structure": spec}
        )
        assert sid is not None
        return sid

    def _pathway(
        self, store: Store, sid: int, *, status: str = "computing", tier: str = "neb"
    ) -> int:
        ref = store.insert_ref(
            kind="pathway",
            slug=f"test-pw-{sid}-{status}",
            title="test pathway",
            meta={"status": status, "candidate_ref": sid, "tier": tier},
        )
        return int(ref.id)

    def _agg_todo(self, store: Store, sid: int, pathway_id: int) -> int:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath aggregate",
            meta={
                "executor": "ssh_node",
                "job_type": "autocatpath_aggregate",
                "params": {"pathway_ref_id": pathway_id},
            },
            parent_id=sid,
        )
        return int(ref.id)

    def _seed(
        self,
        store: Store,
        agg_todo_id: int,
        *,
        pathway_id: int,
        job_statuses: list[str],
        job_open_tags: list[str | None] | None = None,
        todo_status: str | None = None,
    ) -> tuple[int, list[int]]:
        seed_todo = store.insert_ref(
            kind="todo",
            slug=None,
            title="autocatpath seed",
            meta={"auto_check": {"type": "child_job_succeeded"}},
            parent_id=agg_todo_id,
        )
        if todo_status:
            store.add_tag(
                seed_todo.id, Tag.closed("STATUS", todo_status), set_by="system"
            )
        open_tags = job_open_tags or [None] * len(job_statuses)
        job_ids = []
        for status, open_tag in zip(job_statuses, open_tags, strict=True):
            job = store.insert_ref(
                kind="job",
                slug=None,
                title="autocatpath_seed",
                meta={
                    "job_type": "autocatpath_seed",
                    # The provenance stamp real seed jobs carry (compute.
                    # dispatch_autocatpath) — the module-level catch-all
                    # (:func:`loop_mod._reconcile_stale_computing_pathways`)
                    # keys its "any live job" check off this.
                    "params": {"pathway_ref_id": pathway_id},
                },
                parent_id=seed_todo.id,
            )
            store.add_tag(job.id, Tag.closed("STATUS", status), set_by="system")
            if open_tag:
                store.add_tag(job.id, Tag.open(open_tag), set_by="system")
            job_ids.append(int(job.id))
        return int(seed_todo.id), job_ids


class TestPathwayJobTreeState(_PathwayTreeMixin):
    """Direct coverage of :func:`loop_mod._pathway_job_tree_state`."""

    def test_no_tree_at_all_is_unknown(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        assert loop_mod._pathway_job_tree_state(store, pw) == ("unknown", None)

    def test_todo_with_no_job_yet_is_in_flight(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        self._agg_todo(store, sid, pw)
        assert loop_mod._pathway_job_tree_state(store, pw)[0] == "in_flight"

    def test_live_seed_job_is_in_flight(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["queued"])
        assert loop_mod._pathway_job_tree_state(store, pw)[0] == "in_flight"

    def test_genuine_failure_without_infra_tag_is_failed(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["failed"])
        state, reason = loop_mod._pathway_job_tree_state(store, pw)
        assert state == "failed"
        assert reason

    def test_cancelled_only_is_wrongful_kill(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])
        assert loop_mod._pathway_job_tree_state(store, pw) == ("wrongful_kill", None)

    def test_infra_tagged_failed_is_wrongful_kill(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(
            store,
            agg,
            pathway_id=pw,
            job_statuses=["failed"],
            job_open_tags=["infra:child-killed"],
        )
        assert loop_mod._pathway_job_tree_state(store, pw) == ("wrongful_kill", None)

    def test_dead_node_reaped_failed_is_wrongful_kill(self, store: Store) -> None:
        """``reaped:dead-node-orphan`` is infra-class in ``_job_bubble.
        INFRA_FAILURE_TAGS`` (gr210536 — it used to be a local widening
        here), so this sweep's wrongful-kill set carries it: an
        ``ssh_node``-executor autocatpath seed job dying with its GPU node
        is exactly as wrongful as a swept claim-orphan."""
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(
            store,
            agg,
            pathway_id=pw,
            job_statuses=["failed"],
            job_open_tags=["reaped:dead-node-orphan"],
        )
        assert loop_mod._pathway_job_tree_state(store, pw) == ("wrongful_kill", None)

    def test_genuine_failure_wins_over_a_sibling_wrongful_kill(
        self, store: Store
    ) -> None:
        """A mixed tree (one seed genuinely failed, another cancelled) reads
        as a real failure — safer than re-dispatching a candidate that
        already produced a genuine content-class error."""
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])
        self._seed(store, agg, pathway_id=pw, job_statuses=["failed"])
        assert loop_mod._pathway_job_tree_state(store, pw)[0] == "failed"

    def test_done_todo_with_stale_failed_job_is_excluded(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(
            store, agg, pathway_id=pw, job_statuses=["failed"], todo_status="done"
        )
        assert loop_mod._pathway_job_tree_state(store, pw) == ("unknown", None)

    def test_succeeded_seeds_with_no_aggregate_job_yet_is_never_actioned(
        self, store: Store
    ) -> None:
        """Every seed done, but the aggregate job hasn't been minted by the
        dispatch worker yet (T_agg carries no job of its own until then,
        per ``dispatch_autocatpath``'s docstring) — a normal transient, not
        an orphan: neither ``failed`` nor ``wrongful_kill`` (the only two
        states :func:`loop_mod._reconcile_orphaned_pathways` acts on). The
        age-gated catch-all is the backstop if the aggregate never arrives."""
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(
            store, agg, pathway_id=pw, job_statuses=["succeeded"], todo_status="done"
        )
        assert loop_mod._pathway_job_tree_state(store, pw)[0] not in (
            "failed",
            "wrongful_kill",
        )


class TestReconcileOrphanedPathways(_PathwayTreeMixin):
    """:func:`loop_mod._reconcile_orphaned_pathways` — the per-active-quest
    step."""

    def test_failed_tree_stamps_the_pathway_failed(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["failed"])

        failed, redispatched = loop_mod._reconcile_orphaned_pathways(store, q, hub=None)

        assert (failed, redispatched) == (1, 0)
        meta = _pathway_meta(store, pw)
        assert meta["status"] == "failed"
        assert meta.get("failed_reason")

    def test_in_flight_tree_is_left_alone(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["running"])

        failed, redispatched = loop_mod._reconcile_orphaned_pathways(store, q, hub=None)

        assert (failed, redispatched) == (0, 0)
        assert _pathway_meta(store, pw)["status"] == "computing"

    def test_wrongful_kill_redispatches_via_dispatch_autocatpath(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = _mk_quest(store, "A striving")
        store.update_ref(q, meta_patch={"reaction_config": {"substrate": "NO"}})
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid, tier="verify")
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])

        calls: list[tuple[int, dict[str, Any], str]] = []

        def _fake_dispatch(
            _store: Any, sid: int, reaction: dict[str, Any], *, hub: Any, tier: str
        ) -> str:
            calls.append((sid, reaction, tier))
            return "autocatpath[fake] dispatched"

        monkeypatch.setattr(loop_mod, "dispatch_autocatpath", _fake_dispatch)

        failed, redispatched = loop_mod._reconcile_orphaned_pathways(store, q, hub=None)

        assert (failed, redispatched) == (0, 1)
        assert calls == [(sid, {"substrate": "NO"}, "verify")]
        # Not stamped by this sweep — a live re-dispatch (or the next
        # harvest) owns moving it forward, not the orphan sweep itself.
        assert _pathway_meta(store, pw)["status"] == "computing"

    def test_wrongful_kill_skipped_without_reaction_config(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = _mk_quest(store, "A striving")  # no reaction_config stamped
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])

        calls: list[int] = []
        monkeypatch.setattr(
            loop_mod, "dispatch_autocatpath", lambda *a, **k: calls.append(1)
        )

        failed, redispatched = loop_mod._reconcile_orphaned_pathways(store, q, hub=None)

        assert (failed, redispatched) == (0, 0)
        assert calls == []
        assert _pathway_meta(store, pw)["status"] == "computing"

    def test_redispatch_is_capped_per_pass(
        self, store: Store, monkeypatch: Any
    ) -> None:
        q = _mk_quest(store, "A striving")
        store.update_ref(q, meta_patch={"reaction_config": {"substrate": "NO"}})
        for _ in range(loop_mod._MAX_PATHWAY_REDISPATCH_PER_PASS + 2):
            sid = self._candidate(store, q)
            pw = self._pathway(store, sid)
            agg = self._agg_todo(store, sid, pw)
            self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])

        calls: list[int] = []

        def _fake_dispatch(_store: Any, sid: int, *_a: Any, **_k: Any) -> str:
            calls.append(sid)
            return "ok"

        monkeypatch.setattr(loop_mod, "dispatch_autocatpath", _fake_dispatch)

        _failed, redispatched = loop_mod._reconcile_orphaned_pathways(
            store, q, hub=None
        )

        assert redispatched == loop_mod._MAX_PATHWAY_REDISPATCH_PER_PASS
        assert len(calls) == loop_mod._MAX_PATHWAY_REDISPATCH_PER_PASS

    def test_wired_into_reconcile_quest_loops_per_active_quest(
        self, store: Store, monkeypatch: Any
    ) -> None:
        """Part A end-to-end: an active quest's dead pathway stub is
        resolved by an ordinary ``reconcile_quest_loops`` pass, and the
        counts surface on the summary dict."""
        q = _mk_quest(store, "A striving")
        _set_status(store, q, "active")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["failed"])
        monkeypatch.setattr(loop_mod, "cool_stalled", lambda _s: [])
        monkeypatch.setattr(loop_mod, "active_quest_ids", lambda _s: [q])

        out = loop_mod.reconcile_quest_loops(store, enabled=True)

        assert out["pathways_failed"] == 1
        assert out["pathways_redispatched"] == 0
        assert _pathway_meta(store, pw)["status"] == "failed"


class TestReconcileStaleComputingPathways(_PathwayTreeMixin):
    """:func:`loop_mod._reconcile_stale_computing_pathways` — the module-
    level, NOT quest-scoped catch-all."""

    def _age_pathway(self, store: Store, pathway_id: int, age_sql: str) -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET updated_at = now() - (%s)::interval WHERE ref_id = %s",
                (age_sql, pathway_id),
            )
            conn.commit()

    def test_stale_with_no_live_job_anywhere_is_stamped_failed(
        self, store: Store
    ) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])
        self._age_pathway(store, pw, "8 days")

        n = loop_mod._reconcile_stale_computing_pathways(store)

        assert n == 1
        meta = _pathway_meta(store, pw)
        assert meta["status"] == "failed"
        assert meta["failed_reason"] == "orphaned (no live compute)"

    def test_stale_with_a_still_running_job_is_left_alone(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["running"])
        self._age_pathway(store, pw, "8 days")

        n = loop_mod._reconcile_stale_computing_pathways(store)

        assert n == 0
        assert _pathway_meta(store, pw)["status"] == "computing"

    def test_not_yet_stale_is_left_alone(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])
        self._age_pathway(store, pw, "1 day")

        n = loop_mod._reconcile_stale_computing_pathways(store)

        assert n == 0
        assert _pathway_meta(store, pw)["status"] == "computing"

    def test_ready_pathway_is_never_touched(self, store: Store) -> None:
        q = _mk_quest(store, "A striving")
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid, status="ready")
        self._age_pathway(store, pw, "30 days")

        n = loop_mod._reconcile_stale_computing_pathways(store)

        assert n == 0
        assert _pathway_meta(store, pw)["status"] == "ready"

    def test_reaches_a_pathway_whose_quest_is_no_longer_active(
        self, store: Store
    ) -> None:
        """The per-quest sweep only ever sees an active quest's candidates
        — this catch-all is what reclaims a stub whose quest has since gone
        dormant/abandoned, independent of ``active_quest_ids``."""
        q = _mk_quest(store, "A striving")
        # deliberately no STATUS:active tag — this quest is not active.
        sid = self._candidate(store, q)
        pw = self._pathway(store, sid)
        agg = self._agg_todo(store, sid, pw)
        self._seed(store, agg, pathway_id=pw, job_statuses=["cancelled"])
        self._age_pathway(store, pw, "8 days")

        n = loop_mod._reconcile_stale_computing_pathways(store)

        assert n == 1
        assert _pathway_meta(store, pw)["status"] == "failed"
