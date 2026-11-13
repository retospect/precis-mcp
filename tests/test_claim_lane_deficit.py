"""Lane deficit (gr595541): share less the fraction of lane-seconds given."""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import _lane_of, lane_deficits

_EX = "test_lane_deficit"
_HOST = "host-a"


def _job(
    store: Store,
    lane: str,
    status: str,
    claimed_ago_s: int,
    ended_ago_s: int = 0,
    host: str = _HOST,
) -> int:
    meta: dict[str, Any] = {
        "job_type": "demo",
        "executor": _EX,
        "params": {},
        "share_key": lane,
        "lease_host": host,
        "last_claimed_at": (
            "2026-01-01T00:00:00+00:00"  # overwritten below via SQL now()
        ),
    }
    ref = store.insert_ref(kind="job", slug=None, title=lane, meta=meta, prio=5)
    store.add_tag(
        ref.id, Tag.closed("STATUS", status), set_by="agent", replace_prefix=True
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{last_claimed_at}',"
            " to_jsonb((now() - %s)::text)) WHERE ref_id = %s",
            (timedelta(seconds=claimed_ago_s), ref.id),
        )
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - %s FROM tags t"
            " WHERE t.tag_id = rt.tag_id AND rt.ref_id = %s"
            " AND t.namespace = 'STATUS'",
            (timedelta(seconds=ended_ago_s), ref.id),
        )
        conn.commit()
    return ref.id


def test_lane_of_prefers_stamped_share_key() -> None:
    assert _lane_of({"share_key": "code", "job_type": "run_slice"}) == "code"
    assert _lane_of({"job_type": "derived_drain", "params": {"pass": "a"}}) == (
        "derived_drain:a"
    )


def test_starved_lane_owes_more_than_the_fed_lane(store: Store) -> None:
    _job(store, "fed", "succeeded", claimed_ago_s=1800, ended_ago_s=300)
    _job(store, "starved", "queued", claimed_ago_s=100, ended_ago_s=90)
    with store.pool.connection() as conn:
        d = lane_deficits(conn, _EX, _HOST, 3600)
        conn.rollback()
    assert d["starved"] > 0 > d["fed"]
    assert sum(d.values()) == pytest.approx(0.0, abs=1e-9)


def test_usage_outside_window_and_other_hosts_is_ignored(store: Store) -> None:
    _job(store, "old", "succeeded", claimed_ago_s=9000, ended_ago_s=8000)
    _job(store, "elsewhere", "succeeded", claimed_ago_s=900, ended_ago_s=60, host="b")
    _job(store, "idle", "queued", claimed_ago_s=10, ended_ago_s=5)
    with store.pool.connection() as conn:
        d = lane_deficits(conn, _EX, _HOST, 3600)
        conn.rollback()
    assert "elsewhere" not in d
    assert d["idle"] > 0
    assert d.get("old", 0.0) <= 0.5
