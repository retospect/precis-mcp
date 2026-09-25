"""Tests for the backlog groomer (``workers/backlog_groom.py``).

Covers: the ``OPEN:auto-fix`` selection gate (Piece C, 2026-09-18 —
replaced the old every-open-gripe behaviour), the strategic root
(find-or-create + reuse), dedup on a LIVE ``job_type='fix_gripe'`` todo
(scoped so an unrelated todo minter reusing the ``params.gripe_id`` shape
can't starve a re-mint), the ``no-groom`` human opt-out, the cadence
throttle, batch_size bounding, the per-pass mint cap
(``PRECIS_BACKLOG_GROOM_MAX_MINTS``), ``diagnosis_job_id`` threading, and
the end-to-end hand-off — the minted todo is a valid ``dispatch`` candidate
that mints a ``fix_gripe`` job.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.backlog_groom import (
    _MAX_MINTS_ENV_VAR,
    _ROOT_MARKER,
    _STATE_KEY,
    run_backlog_groom_pass,
)
from precis.workers.dispatch import run_dispatch_pass


def _open_gripe(
    store: Store, title: str, *, prio: int | None = None, auto_fix: bool = True
) -> int:
    """Insert a live gripe tagged STATUS:open (and OPEN:auto-fix by
    default — the groomer's selection gate); return its id."""
    ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, prio=prio)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    if auto_fix:
        store.add_tag(ref.id, Tag.open("auto-fix"), set_by="agent")
    return int(ref.id)


def _succeeded_diagnosis_job(store: Store, gripe_id: int) -> int:
    """Insert a succeeded ``diagnose_gripe`` job for ``gripe_id``; return
    its id (feeds :func:`_latest_succeeded_diagnosis_job_id`)."""
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title=f"diagnose_gripe (gripe:{gripe_id})",
        meta={
            "job_type": "diagnose_gripe",
            "executor": "claude_inproc",
            "params": {"gripe_id": gripe_id},
        },
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "succeeded"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def _groomer_todos(store: Store) -> list[dict]:
    """Every live todo minted by the groomer (has meta.params.gripe_id)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT ref_id, title, meta, parent_id, prio FROM refs
             WHERE kind = 'todo' AND retired_at IS NULL
               AND meta -> 'params' ? 'gripe_id'
             ORDER BY ref_id
            """
        ).fetchall()
    return [
        {"id": int(r[0]), "title": r[1], "meta": r[2], "parent_id": r[3], "prio": r[4]}
        for r in rows
    ]


def _force_due(store: Store) -> None:
    """Reset the cadence marker so the next pass runs."""
    store.set_setting(_STATE_KEY, (datetime.now(UTC) - timedelta(days=1)).isoformat())


# ── selection: OPEN:auto-fix gate ───────────────────────────────────


def test_skips_open_gripe_without_auto_fix_tag(store: Store) -> None:
    _open_gripe(store, "no diagnosis yet", auto_fix=False)

    result = run_backlog_groom_pass(store)

    assert result.claimed == 0
    assert result.ok == 0
    assert _groomer_todos(store) == []


def test_mints_dispatchable_todo_for_auto_fix_gripe(store: Store) -> None:
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
    # The root carries the marker + the strategic facet (so children aren't
    # nursery orphans).
    root_ref = store.get_ref(kind="todo", id=root_id)
    assert root_ref is not None
    assert _ROOT_MARKER in root_ref.meta
    assert root_ref.meta.get("rotation_root") is True

    # A second groom (after cadence reset) reuses the same root, not a new one.
    _open_gripe(store, "gripe two")
    _force_due(store)
    run_backlog_groom_pass(store)
    roots = {t["parent_id"] for t in _groomer_todos(store)}
    assert roots == {root_id}


# ── priority inheritance ─────────────────────────────────────────


def test_minted_todo_inherits_gripe_prio(store: Store) -> None:
    """A prioritised gripe hands its prio to the minted fix todo."""
    _open_gripe(store, "high-prio bug", prio=3)
    run_backlog_groom_pass(store)
    assert _groomer_todos(store)[0]["prio"] == 3


def test_minted_todo_defaults_prio_when_gripe_unscored(store: Store) -> None:
    """An unscored gripe (prio NULL) yields the default fix prio, not NULL."""
    _open_gripe(store, "unscored bug")
    run_backlog_groom_pass(store)
    assert _groomer_todos(store)[0]["prio"] == 4


# ── dedup: a live fix todo blocks a re-mint ─────────────────────────


def test_no_remint_while_fix_todo_live(store: Store) -> None:
    _open_gripe(store, "already groomed")
    run_backlog_groom_pass(store)
    assert len(_groomer_todos(store)) == 1

    # Force the cadence open and run again: the fix todo is still live
    # (STATUS:open by default), so no second todo is minted.
    _force_due(store)
    result = run_backlog_groom_pass(store)
    assert result.ok == 0
    assert len(_groomer_todos(store)) == 1


def test_non_fix_todo_with_same_gripe_id_does_not_block_mint(store: Store) -> None:
    """Dedup is scoped to ``job_type='fix_gripe'`` — a differently-typed
    todo that happens to reuse the ``params.gripe_id`` shape (e.g. a future
    minter) must not starve this pass's re-mint."""
    gid = _open_gripe(store, "shadowed by another minter")
    other = store.insert_ref(
        kind="todo",
        slug=None,
        title="unrelated todo, same gripe_id shape",
        meta={"job_type": "some_other_job", "params": {"gripe_id": gid}},
    )
    store.add_tag(
        other.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )

    result = run_backlog_groom_pass(store)

    assert result.ok == 1
    fix_todos = [
        t for t in _groomer_todos(store) if t["meta"].get("job_type") == "fix_gripe"
    ]
    assert len(fix_todos) == 1
    assert fix_todos[0]["meta"]["params"]["gripe_id"] == gid


def test_remint_allowed_once_fix_todo_done(store: Store) -> None:
    """Unlike the old every-gripe groomer, a DONE fix todo no longer blocks
    a re-mint — the fix shipped (or a human closed it), and the gripe only
    reaches selection again if something re-tags it auto-fix."""
    gid = _open_gripe(store, "done fix")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]
    store.add_tag(
        todo_id, Tag.closed("STATUS", "done"), set_by="agent", replace_prefix=True
    )

    _force_due(store)
    result = run_backlog_groom_pass(store)
    assert result.ok == 1
    todos = _groomer_todos(store)
    assert len(todos) == 2
    assert {t["meta"]["params"]["gripe_id"] for t in todos} == {gid}


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


# ── mint cap (PRECIS_BACKLOG_GROOM_MAX_MINTS) ───────────────────────


def test_mint_cap_bounds_mints_and_logs_skipped(
    store: Store, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """4 eligible gripes, cap=3 -> 3 minted, 1 skipped (logged)."""
    monkeypatch.setenv(_MAX_MINTS_ENV_VAR, "3")
    for i in range(4):
        _open_gripe(store, f"gripe {i}")

    with caplog.at_level("INFO"):
        result = run_backlog_groom_pass(store)

    assert result.claimed == 4
    assert result.ok == 3
    assert result.failed == 0
    assert len(_groomer_todos(store)) == 3
    assert any("1 eligible gripe(s) skipped" in rec.message for rec in caplog.records)


def test_mint_cap_defaults_to_three(store: Store) -> None:
    for i in range(5):
        _open_gripe(store, f"gripe {i}")

    result = run_backlog_groom_pass(store)
    assert result.ok == 3
    assert len(_groomer_todos(store)) == 3


# ── diagnosis_job_id threading ───────────────────────────────────


def test_diagnosis_job_id_lands_in_params(store: Store) -> None:
    gid = _open_gripe(store, "diagnosed bug")
    diag_job_id = _succeeded_diagnosis_job(store, gid)

    run_backlog_groom_pass(store)

    todos = _groomer_todos(store)
    assert len(todos) == 1
    assert todos[0]["meta"]["params"] == {
        "gripe_id": gid,
        "diagnosis_job_id": diag_job_id,
    }


def test_no_diagnosis_job_omits_the_param(store: Store) -> None:
    """A gripe hand-tagged auto-fix with no diagnose_gripe job on record
    mints without diagnosis_job_id — fix_gripe falls back to the full brief."""
    gid = _open_gripe(store, "hand-tagged bug")

    run_backlog_groom_pass(store)

    todos = _groomer_todos(store)
    assert todos[0]["meta"]["params"] == {"gripe_id": gid}


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
            "AND retired_at IS NULL",
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


def test_minted_todo_links_to_the_gripe_it_fixes(store: Store) -> None:
    """``claude_inproc._run_fix_gripe`` finds its gripe only through a
    ``rel='fixes'`` link; ``meta.params.gripe_id`` does not stand in for
    it (gr399837)."""
    gid = _open_gripe(store, "needs a fixes edge")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]

    fixes = store.links_for(todo_id, direction="out", relation="fixes")
    assert [link.dst_ref_id for link in fixes] == [gid]


def test_dispatched_job_carries_the_fixes_link(store: Store) -> None:
    """The job row — not just the todo — must carry the edge: the handler
    resolves the gripe from the *job*. Dispatch minted link-less jobs that
    died at event 0 and were re-minted forever by the sweeper."""
    gid = _open_gripe(store, "dispatch must carry the edge")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]

    run_dispatch_pass(store)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL",
            (todo_id,),
        ).fetchone()
    assert row is not None, "dispatch minted no job for the todo"
    job_id = row[0]
    fixes = store.links_for(job_id, direction="out", relation="fixes")
    assert [link.dst_ref_id for link in fixes] == [gid]


def test_dispatch_falls_back_to_params_gripe_id(store: Store) -> None:
    """A todo minted before the groomer wrote the link still dispatches a
    working job — otherwise the already-parked ones stay broken forever."""
    gid = _open_gripe(store, "minted before the link existed")
    run_backlog_groom_pass(store)
    todo_id = _groomer_todos(store)[0]["id"]
    with store.pool.connection() as conn:
        conn.execute(
            "DELETE FROM links WHERE src_ref_id = %s AND relation = 'fixes'",
            (todo_id,),
        )

    run_dispatch_pass(store)

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s "
            "AND retired_at IS NULL",
            (todo_id,),
        ).fetchone()
    assert row is not None, "dispatch minted no job for the todo"
    job_id = row[0]
    fixes = store.links_for(job_id, direction="out", relation="fixes")
    assert [link.dst_ref_id for link in fixes] == [gid]
