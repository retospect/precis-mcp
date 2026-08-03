"""§B-2 cycle b — reserve mode + the ``precis jobs kill`` operator backstop.

Three layers: the ``service_config`` reserve pseudo-service (migration
0104's ``expires_at``, ``workers/service_config.py``'s ``set_reserve`` /
``clear_reserve`` / ``reserve_active``), the claim-time gate
(``claim_executor_jobs(..., respect_reserve=True)``), and
``precis jobs kill``'s validate-then-stamp CLI. The executor-side kill
drills (the job_type's ``spec.kill`` actually firing, GPU reclaim) live
in ``test_ssh_node_executor.py`` / ``test_sandbox_run.py`` alongside their
existing deadline-kill siblings.
"""

from __future__ import annotations

import argparse

import pytest

from precis.cli import jobs_admin
from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import claim_executor_jobs
from precis.workers.service_config import (
    ALL_HOSTS,
    clear_reserve,
    list_service_config,
    reserve_active,
    set_reserve,
)

pytestmark = pytest.mark.db

# ---------------------------------------------------------------------------
# service_config reserve helpers
# ---------------------------------------------------------------------------


def test_set_reserve_then_reserve_active_true(store: Store) -> None:
    expires_at = set_reserve(store, "melchior", hours=1.0, actor="test")
    assert expires_at is not None
    with store.pool.connection() as conn:
        assert reserve_active(conn, "melchior") is True
        assert reserve_active(conn, "caspar") is False  # exact-host only


def test_wildcard_reserve_gates_every_host(store: Store) -> None:
    set_reserve(store, ALL_HOSTS, hours=1.0, actor="test")
    with store.pool.connection() as conn:
        assert reserve_active(conn, "melchior") is True
        assert reserve_active(conn, "caspar") is True
        assert reserve_active(conn, "spark") is True


def test_clear_reserve_removes_row(store: Store) -> None:
    set_reserve(store, "melchior", hours=1.0)
    assert clear_reserve(store, "melchior") is True
    assert clear_reserve(store, "melchior") is False  # already gone
    with store.pool.connection() as conn:
        assert reserve_active(conn, "melchior") is False


def test_expired_reserve_is_inert(store: Store) -> None:
    """A row whose expires_at is already in the past behaves as if absent —
    reserve_active's predicate alone is the auto-expiry, no reaper needed."""
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE service_config SET expires_at = now() - interval '1 second' "
            "WHERE host = %s AND service = 'reserve'",
            ("melchior",),
        )
        conn.commit()
        assert reserve_active(conn, "melchior") is False


def test_set_reserve_rejects_out_of_bounds_hours(store: Store) -> None:
    with pytest.raises(ValueError):
        set_reserve(store, "melchior", hours=0)
    with pytest.raises(ValueError):
        set_reserve(store, "melchior", hours=-1)
    with pytest.raises(ValueError):
        set_reserve(store, "melchior", hours=169)


def test_reserve_row_visible_in_list_service_config(store: Store) -> None:
    set_reserve(store, "melchior", hours=2.0, actor="reto")
    rows = {(r["host"], r["service"]): r for r in list_service_config(store)}
    row = rows[("melchior", "reserve")]
    assert row["prio"] == 1
    assert row["expires_at"] is not None


# ---------------------------------------------------------------------------
# claim-time gate
# ---------------------------------------------------------------------------


def _queue_job(store: Store, *, executor: str) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="reserve-gate job",
        meta={"executor": executor, "job_type": "demo", "params": {}},
    )
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return int(ref.id)


def test_active_reserve_blocks_ssh_node_style_claim(store: Store) -> None:
    rid = _queue_job(store, executor="ssh_node")
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="ssh_node",
            limit=10,
            reserve_host_id="melchior",
            respect_reserve=True,
        )
        conn.commit()
    assert rows == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (rid,),
        ).fetchone()
    assert row is not None  # untouched, still queued


def test_active_reserve_blocks_claude_docker_style_claim(store: Store) -> None:
    _queue_job(store, executor="claude_docker")
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="claude_docker",
            limit=10,
            reserve_host_id="melchior",
            respect_reserve=True,
        )
        conn.commit()
    assert rows == []


def test_active_reserve_blocks_job_inproc_style_claim(store: Store) -> None:
    """§F cycle a: ``job_inproc`` passes ``respect_reserve=True`` too — the
    box-heavy bounded lane, same as ``ssh_node``/``claude_docker``."""
    rid = _queue_job(store, executor="job_inproc")
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="job_inproc",
            limit=10,
            reserve_host_id="melchior",
            respect_reserve=True,
        )
        conn.commit()
    assert rows == []
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (rid,),
        ).fetchone()
    assert row is not None  # untouched, still queued


def test_reserve_does_not_gate_a_call_that_opts_out(store: Store) -> None:
    """coordinator / claude_inproc never pass respect_reserve — the light
    cloud lane keeps running even while the box is reserved."""
    _queue_job(store, executor="coordinator")
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="coordinator",
            limit=10,
            reserve_host_id="melchior",
            # respect_reserve defaults to False
        )
        conn.commit()
    assert len(rows) == 1


def test_expired_reserve_row_lets_claims_resume(store: Store) -> None:
    _queue_job(store, executor="ssh_node")
    set_reserve(store, "melchior", hours=1.0)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE service_config SET expires_at = now() - interval '1 second' "
            "WHERE host = %s AND service = 'reserve'",
            ("melchior",),
        )
        conn.commit()
        rows = claim_executor_jobs(
            conn,
            executor="ssh_node",
            limit=10,
            reserve_host_id="melchior",
            respect_reserve=True,
        )
        conn.commit()
    assert len(rows) == 1


def test_exact_host_reserve_does_not_gate_another_host(store: Store) -> None:
    _queue_job(store, executor="ssh_node")
    set_reserve(store, "melchior", hours=1.0)  # reserves melchior, not spark
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="ssh_node",
            limit=10,
            reserve_host_id="spark",
            respect_reserve=True,
        )
        conn.commit()
    assert len(rows) == 1


def test_no_reserve_row_is_byte_identical(store: Store) -> None:
    """The empty-table default-off case: respect_reserve=True with no
    reserve row anywhere claims exactly as if the parameter didn't exist."""
    _queue_job(store, executor="ssh_node")
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="ssh_node",
            limit=10,
            reserve_host_id="melchior",
            respect_reserve=True,
        )
        conn.commit()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# `precis jobs kill` — validate-then-stamp CLI
# ---------------------------------------------------------------------------


def _kill_ns(ref_id: int, *, note: str | None = None) -> argparse.Namespace:
    return argparse.Namespace(ref_id=ref_id, note=note, database_url=None)


def test_kill_refuses_non_running_job(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = store.insert_ref(
        kind="job", slug=None, title="queued job", meta={"executor": "ssh_node"}
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")

    with pytest.raises(SystemExit) as exc:
        jobs_admin._cmd_kill(store, _kill_ns(ref.id))
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "not running" in err

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta ? 'kill_requested' FROM refs WHERE ref_id = %s", (ref.id,)
        ).fetchone()
    assert row is not None
    assert row[0] is False  # no write happened


def test_kill_refuses_non_job_kind(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = store.insert_ref(kind="todo", slug=None, title="not a job", meta={})

    with pytest.raises(SystemExit) as exc:
        jobs_admin._cmd_kill(store, _kill_ns(ref.id))
    assert exc.value.code == 2
    assert "not 'job'" in capsys.readouterr().err


def test_kill_stamps_running_job(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    ref = store.insert_ref(
        kind="job", slug=None, title="running job", meta={"executor": "ssh_node"}
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")

    jobs_admin._cmd_kill(store, _kill_ns(ref.id, note="stuck relax"))
    out = capsys.readouterr().out
    assert "requested kill" in out

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta -> 'kill_requested' FROM refs WHERE ref_id = %s", (ref.id,)
        ).fetchone()
    assert row is not None
    request = row[0]
    assert request["note"] == "stuck relax"
    assert "at" in request and "actor" in request


def test_kill_refuses_unknown_ref(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        jobs_admin._cmd_kill(store, _kill_ns(999_999_999))
    assert exc.value.code == 2
    assert "no such ref" in capsys.readouterr().err
