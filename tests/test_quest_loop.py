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
        assert meta["params"]["tier"] == "big"
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
