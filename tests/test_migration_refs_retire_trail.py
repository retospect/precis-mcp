"""Migration 0229: soft-deleting a ref leaves a ``ref_events`` trail.

gr503453 — on 2026-08-11 nine job refs were soft-deleted in one
transaction with zero ``ref_events`` rows, actor unrecoverable, because
the trail depended on each door remembering ``append_event`` (and a raw
psql UPDATE has no door at all). The migration installs an
``AFTER UPDATE OF retired_at`` row trigger on ``refs`` that appends
``('db', 'retired')`` / ``('db', 'restored')`` itself, suppressed only
when this transaction already logged an event for that ref.
"""

from __future__ import annotations

import psycopg
import pytest

from precis.store import Store
from tests.conftest import _active_dsn

pytestmark = pytest.mark.db


def _events(dsn: str, ref_id: int) -> list[dict]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        rows = conn.execute(
            "SELECT source, event, payload FROM ref_events "
            "WHERE ref_id = %s ORDER BY event_id",
            (ref_id,),
        ).fetchall()
    return [{"source": r[0], "event": r[1], "payload": r[2]} for r in rows]


def test_raw_retire_update_writes_a_db_trail_row(store: Store) -> None:
    """The 2026-08-11 shape: a bare ``UPDATE refs SET retired_at=now()``
    — no handler, no ``append_event`` — still lands exactly one
    ``ref_events`` row, ``source='db'``, naming the kind and the
    connecting role."""
    ref = store.insert_ref(kind="job", slug=None, title="trail test job")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = %s",
            (ref.id,),
        )
    (row,) = _events(_active_dsn(), ref.id)
    assert row["source"] == "db"
    assert row["event"] == "retired"
    assert row["payload"]["kind"] == "job"
    assert "db_user" in row["payload"]


def test_handler_own_event_in_same_tx_suppresses_the_trigger(
    store: Store,
) -> None:
    """A door that already wrote its own trail keeps exactly that row —
    ``now()`` is transaction-stable, so an ``append_event`` earlier in
    the same tx makes the trigger's row a duplicate, and the ``NOT
    EXISTS`` guard drops it."""
    ref = store.insert_ref(kind="job", slug=None, title="handled retire")
    with psycopg.connect(_active_dsn()) as conn:
        store.append_event(
            ref.id,
            conn=conn,
            source="quest-seed-recovery",
            event="retired",
            payload={"why": "seed superseded"},
        )
        store.retire_ref(ref.id, conn=conn)
        conn.commit()
    (row,) = _events(_active_dsn(), ref.id)
    assert row["source"] == "quest-seed-recovery"
    assert row["event"] == "retired"
    assert row["payload"] == {"why": "seed superseded"}


def test_restore_writes_a_restored_row(store: Store) -> None:
    """The inverse transition leaves ``('db', 'restored')`` — the trail
    covers both directions of the flag, on a non-job kind (the trigger
    is kind-agnostic by design)."""
    ref = store.insert_ref(kind="todo", slug=None, title="restorable")
    store.retire_ref(ref.id)
    store.restore_ref(ref.id)
    events = _events(_active_dsn(), ref.id)
    assert [e["event"] for e in events] == ["retired", "restored"]
    assert all(e["source"] == "db" for e in events)


def test_one_update_retiring_many_refs_trails_each(store: Store) -> None:
    """A single multi-row UPDATE — the actual incident shape — writes one
    trail row PER row, not one per statement."""
    refs = [
        store.insert_ref(kind="job", slug=None, title=f"bulk {i}") for i in range(3)
    ]
    ids = [r.id for r in refs]
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = ANY(%s)",
            (ids,),
        )
    for rid in ids:
        (row,) = _events(_active_dsn(), rid)
        assert row["event"] == "retired" and row["source"] == "db"


def test_unrelated_update_writes_nothing(store: Store) -> None:
    """``UPDATE OF retired_at`` does not fire when the column isn't in
    the SET list — ordinary edits produce no trail noise."""
    ref = store.insert_ref(kind="job", slug=None, title="retitle me")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        conn.execute(
            "UPDATE refs SET title = %s WHERE ref_id = %s",
            ("retitled", ref.id),
        )
    assert _events(_active_dsn(), ref.id) == []
