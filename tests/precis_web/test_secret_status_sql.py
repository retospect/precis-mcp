"""Real-PG test for ``secret_status.last_succeeded_job``'s raw SQL.

The weekly credential report uses the newest succeeded job of a secret's
dependent job types as evidence that an unprobed key still works. The query
joins ``refs`` / ``ref_tags`` / ``tags`` and filters on ``meta->>'job_type'``,
which ``FakeStore`` never parses, so it needs real postgres (``store``).
"""

from __future__ import annotations

from typing import Any

from precis.store import Tag
from precis_web.secret_status import last_succeeded_job


def _job(store: Any, job_type: str, status: str) -> Any:
    ref = store.insert_ref(
        kind="job", slug=None, title=f"{job_type} job", meta={"job_type": job_type}
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    return ref


def test_no_job_types_short_circuits_without_a_query(store: Any) -> None:
    assert last_succeeded_job(store, ()) is None


def test_returns_none_when_no_job_succeeded(store: Any) -> None:
    _job(store, "secret_probe_a", "failed")
    _job(store, "secret_probe_a", "pending")

    assert last_succeeded_job(store, ("secret_probe_a",)) is None


def test_picks_newest_succeeded_job_of_the_requested_types(store: Any) -> None:
    older = _job(store, "secret_probe_b", "succeeded")
    newer = _job(store, "secret_probe_c", "succeeded")
    _job(store, "secret_probe_b", "failed")  # wrong status: ignored
    _job(store, "secret_probe_other", "succeeded")  # wrong type: ignored
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '1 day' WHERE ref_id = %s",
            (older.id,),
        )
        conn.commit()

    hit = last_succeeded_job(store, ("secret_probe_b", "secret_probe_c"))

    assert hit is not None
    assert hit[0] == newer.id
    assert hit[1] is not None
