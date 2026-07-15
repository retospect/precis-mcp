"""Tests for `precis cast schedule` — idempotent recurring-watch install."""

from __future__ import annotations

from typing import Any

from precis.cli.cast import install_cast_watches
from precis.reading.cast_common import CAST_PROFILES
from precis.workers.schedule.seed import ensure_watches_root


def test_install_is_idempotent_and_well_formed(store: Any) -> None:
    ids1 = install_cast_watches(store)
    ids2 = install_cast_watches(store)
    assert ids1 == ids2  # second call creates nothing new

    watches = ensure_watches_root(store)
    for cast, ref_id in zip(("reading", "nidra"), ids1, strict=True):
        profile = CAST_PROFILES[cast]
        ref = store.get_ref(kind="todo", id=ref_id)
        assert ref is not None
        assert ref.parent_id == watches  # lands under the Watches umbrella
        assert ref.meta["schedule"]["cron"] == profile.cron
        assert ref.meta["schedule"]["backfill_missed"] is False
        assert ref.meta["executor"] == "claude_inproc"  # opus compose on melchior
        assert ref.meta["job_type"] == profile.job_type
        assert ref.meta["cast_watch"] == cast

        tags = {str(t) for t in store.tags_for(ref_id)}
        assert "level:recurring" in tags
        assert "STATUS:open" in tags


def test_exactly_one_watch_per_cast(store: Any) -> None:
    install_cast_watches(store)
    install_cast_watches(store)
    for cast in ("reading", "nidra"):
        with store.pool.connection() as conn:
            n = conn.execute(
                "SELECT count(*) FROM refs WHERE kind='todo' AND deleted_at IS NULL "
                "AND meta->>'cast_watch' = %s",
                (cast,),
            ).fetchone()[0]
        assert n == 1
