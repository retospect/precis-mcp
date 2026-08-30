"""Tests for `precis cast schedule` — idempotent recurring-watch install."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from precis.cli.cast import (
    _CARD_FORGE_CRON,
    _reconcile_watch_cron,
    install_cast_watches,
)
from precis.reading.cast_common import CAST_PROFILES
from precis.workers.schedule import validate_schedule
from precis.workers.schedule.seed import ensure_watches_root


def test_install_is_idempotent_and_well_formed(store: Any) -> None:
    ids1 = install_cast_watches(store)
    ids2 = install_cast_watches(store)
    assert ids1 == ids2  # second call creates nothing new

    watches = ensure_watches_root(store)
    for cast, ref_id in zip(("reading", "nidra", "card_forge"), ids1, strict=True):
        expect_cron = (
            CAST_PROFILES[cast].cron if cast in CAST_PROFILES else _CARD_FORGE_CRON
        )
        expect_job = (
            CAST_PROFILES[cast].job_type if cast in CAST_PROFILES else "card_forge"
        )
        ref = store.get_ref(kind="todo", id=ref_id)
        assert ref is not None
        assert ref.parent_id == watches  # lands under the Watches umbrella
        assert ref.meta["schedule"]["cron"] == expect_cron
        assert ref.meta["schedule"]["backfill_missed"] is False
        assert ref.meta["executor"] == "claude_inproc"  # opus compose on melchior
        assert ref.meta["job_type"] == expect_job
        assert ref.meta["cast_watch"] == cast

        tags = {str(t) for t in store.tags_for(ref_id)}
        assert "STATUS:open" in tags


def test_exactly_one_watch_per_cast(store: Any) -> None:
    install_cast_watches(store)
    install_cast_watches(store)
    for cast in ("reading", "nidra", "card_forge"):
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='todo' AND retired_at IS NULL "
                "AND meta->>'cast_watch' = %s",
                (cast,),
            ).fetchone()[0]
        assert n == 1


class TestReconcileWatchCron:
    """A cron edit in ``CAST_PROFILES`` must reach an already-installed
    watch — install is idempotent on the ``cast_watch`` marker, so without
    reconciliation the live schedule would silently drift from the code."""

    def test_drifted_cron_is_patched_to_match(self, store: Any) -> None:
        ids = install_cast_watches(store)
        reading_ref_id = ids[0]
        # Simulate a stale cron left behind by an older profile.
        store.update_ref(
            reading_ref_id,
            meta_patch={"schedule": {"cron": "0 0 * * *", "backfill_missed": True}},
        )

        new_sched = validate_schedule({"cron": CAST_PROFILES["reading"].cron})
        _reconcile_watch_cron(store, reading_ref_id, new_sched, "cast reading")

        ref = store.get_ref(kind="todo", id=reading_ref_id)
        assert ref is not None
        assert ref.meta["schedule"]["cron"] == CAST_PROFILES["reading"].cron
        # Reconcile patches only the cron literal — an operator-set
        # ``backfill_missed`` on the watch is preserved, not clobbered back.
        assert ref.meta["schedule"]["backfill_missed"] is True

    def test_matching_cron_is_a_noop(self, store: Any, monkeypatch: Any) -> None:
        ids = install_cast_watches(store)
        reading_ref_id = ids[0]
        sched = validate_schedule({"cron": CAST_PROFILES["reading"].cron})

        def boom(*a: Any, **k: Any) -> Any:
            raise AssertionError("update_ref must not be called for a matching cron")

        monkeypatch.setattr(store, "update_ref", boom)
        _reconcile_watch_cron(store, reading_ref_id, sched, "cast reading")  # no raise

    def test_install_reconciles_a_drifted_watch_via_profile_change(
        self, store: Any, monkeypatch: Any
    ) -> None:
        """End-to-end: an already-installed watch whose cron drifted from
        the (changed) profile gets fixed on the next ``install_cast_watches``
        call, not just when ``_reconcile_watch_cron`` is called directly."""
        import precis.cli.cast as cast_cli

        ids1 = install_cast_watches(store)
        reading_ref_id = ids1[0]

        drifted = replace(CAST_PROFILES["reading"], cron="0 1 * * *")
        monkeypatch.setitem(cast_cli.CAST_PROFILES, "reading", drifted)

        ids2 = install_cast_watches(store)
        assert ids2 == ids1  # still no new watch minted

        ref = store.get_ref(kind="todo", id=reading_ref_id)
        assert ref is not None
        assert ref.meta["schedule"]["cron"] == "0 1 * * *"
