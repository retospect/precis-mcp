"""Tests for the backlog groomer (``workers/backlog_groom.py``).

Covers: minting a dispatchable ``fix_gripe`` todo per open gripe, the
strategic root (find-or-create + reuse), dedup (no re-mint), the
``no-groom`` human opt-out, the cadence throttle, batch_size bounding, and
the end-to-end hand-off — the minted todo is a valid ``dispatch`` candidate
that mints a ``fix_gripe`` job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from precis.store import Store
from precis.store.types import Tag
from precis.workers.backlog_groom import (
    _ROOT_MARKER,
    _STATE_KEY,
    run_backlog_groom_pass,
)
from precis.workers.dispatch import run_dispatch_pass


def _open_gripe(store: Store, title: str) -> int:
    """Insert a live gripe tagged STATUS:open; return its id."""
    ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={})
    store.add_tag(
        ref.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _groomer_todos(store: Store) -> list[dict]:
    """Every live todo minted by the groomer (has meta.params.gripe_id)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, title, meta, parent_id FROM refs
             WHERE kind = 'todo' AND deleted_at IS NULL
               AND meta -> 'params' ? 'gripe_id'
             ORDER BY ref_id
            """
        ).fetchall()
    return [
        {"id": int(r[0]), "title": r[1], "meta": r[2], "parent_id": r[3]} for r in rows
    ]


def _force_due(store: Store) -> None:
    """Reset the cadence marker so the next pass runs."""
    store.set_setting(_STATE_KEY, (datetime.now(UTC) - timedelta(days=1)).isoformat())


# ── minting ──────────────────────────────────────────────────────


def test_mints_dispatchable_todo_for_open_gripe(store: Store) -> None:
    gid = _open_gripe(store, "embedder health signals lie")

    result = run_backlog_groom_pass(store)

    assert result.claimed == 1
    assert result.ok == 1
    assert result.failed == 0

    todos = _groomer_todos(store)
    assert len(todos) == 1
    meta = todos[0]["meta"]
    assert meta["executor"] == "claude_inproc"
    assert meta["job_type"] == "fix_gripe"
    assert meta["params"] == {"gripe_id": gid}
    assert meta["minted_from_gripe"] == gid
    assert f"gr{gid}" in todos[0]["title"]


def test_root_is_strategic_and_reused(store: Store) -> None:
    _open_gripe(store, "gripe one")
    run_backlog_groom_pass(store)

    todo = _groomer_todos(store)[0]
    root_id = todo["parent_id"]
    assert root_id is not None
    # The root carries the marker + the strategic level (so children aren't
    # nursery orphans).
    assert _ROOT_MARKER in store.get_ref(kind="todo", id=root_id).meta
    assert store.has_tag(root_id, "OPEN", "level:strategic")

    # A second groom (after cadence reset) reuses the same root, not a new one.
    _open_gripe(store, "gripe two")
    _force_due(store)
    run_backlog_groom_pass(store)
    roots = {t["parent_id"] for t in _groomer_todos(store)}
    assert roots == {root_id}


# ── dedup ────────────────────────────────────────────────────────


def test_no_remint_for_already_groomed_gripe(store: Store) -> None:
    _open_gripe(store, "already groomed")
    run_backlog_groom_pass(store)
    assert len(_groomer_todos(store)) == 1

    # Force the cadence open and run again: the gripe is already groomed, so
    # no second todo is minted.
    _force_due(store)
    result = run_backlog_groom_pass(store)
    assert result.ok == 0
    assert len(_groomer_todos(store)) == 1


def test_no_remint_even_after_todo_done(store: Store) -> None:
    """Dedup keys on the todo's existence, not its status — a done fix todo
    still suppresses a re-mint (the fix shipped or a human is on it)."""
    _open_gripe(store, "done fix")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )

    _force_due(store)
    run_backlog_groom_pass(store)
    assert len(_groomer_todos(store)) == 1


# ── opt-out ──────────────────────────────────────────────────────


def test_no_groom_tag_opts_out(store: Store) -> None:
    gid = _open_gripe(store, "leave me alone")
    store.add_tag(gid, Tag.open("no-groom"), set_by="agent")

    result = run_backlog_groom_pass(store)
    assert result.claimed == 0
    assert _groomer_todos(store) == []


# ── cadence throttle ─────────────────────────────────────────────


def test_cadence_throttle_idles_second_pass(store: Store) -> None:
    _open_gripe(store, "first")
    run_backlog_groom_pass(store)  # sets the marker

    # Immediately add another open gripe; the throttle should block a second
    # run until the window elapses.
    _open_gripe(store, "second")
    result = run_backlog_groom_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert len(_groomer_todos(store)) == 1


# ── batch bounding ───────────────────────────────────────────────


def test_batch_size_bounds_mints_per_pass(store: Store) -> None:
    for i in range(5):
        _open_gripe(store, f"gripe {i}")

    result = run_backlog_groom_pass(store, batch_size=2)
    assert result.claimed == 2
    assert result.ok == 2
    assert len(_groomer_todos(store)) == 2


# ── end-to-end hand-off ──────────────────────────────────────────


def test_minted_todo_is_a_valid_dispatch_candidate(store: Store) -> None:
    """The groomed todo mints a fix_gripe job on the next dispatch sweep."""
    gid = _open_gripe(store, "dispatchable via groom")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]

    dispatch_result = run_dispatch_pass(store)
    assert dispatch_result.ok >= 1

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND deleted_at IS NULL",
            (todo_id,),
        ).fetchall()
    assert len(rows) == 1
    job_meta = rows[0][0]
    assert job_meta["job_type"] == "fix_gripe"
    assert job_meta["executor"] == "claude_inproc"
    assert job_meta["params"] == {"gripe_id": gid}


def test_idle_when_no_open_gripes(store: Store) -> None:
    result = run_backlog_groom_pass(store)
    assert result.claimed == 0
    assert result.ok == 0
    assert _groomer_todos(store) == []
