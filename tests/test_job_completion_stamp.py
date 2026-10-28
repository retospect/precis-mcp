"""Every job completion path stamps finished_at / wall_s / node, and the
seed compute-dir reaper keeps an unstamped dir (gr496403).

The stamp rides ``store.add_tag`` for a terminal ``STATUS:`` on a job, so
these tests drive each real completion route against the dev DB.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import _common, claude_inproc, ssh_node
from precis.workers.job_types import JobTypeSpec


def _mk_job(store: Store, *, running: bool = True, **meta: Any) -> int:
    started = datetime.now(UTC) - timedelta(seconds=90)
    base: dict[str, Any] = {"executor": "ssh_node", "job_type": "fake"}
    if running:
        base["started_at"] = started.isoformat()
        base["lease_host"] = "node-a"
    base.update(meta)
    ref = store.insert_ref(kind="job", slug=None, title="stamp test", meta=base)
    store.add_tag(
        ref.id,
        Tag.parse_strict("STATUS:running" if running else "STATUS:queued"),
        set_by="agent",
    )
    return int(ref.id)


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _assert_stamped(store: Store, ref_id: int, *, node: str | None = "node-a") -> None:
    meta = _meta(store, ref_id)
    finished = datetime.fromisoformat(meta["finished_at"])
    started = datetime.fromisoformat(meta["started_at"])
    assert finished.tzinfo is not None
    assert finished >= started
    assert 89 <= meta["wall_s"] <= 600
    assert meta["node"] == node


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_set_status_terminal_stamps(store: Store, status: str) -> None:
    ref_id = _mk_job(store)
    _common.set_status(store, ref_id, status)
    _assert_stamped(store, ref_id)


def test_record_failure_stamps(store: Store) -> None:
    ref_id = _mk_job(store)
    _common.record_failure(store, ref_id, "boom", gripe_rollback=None)
    _assert_stamped(store, ref_id)


def test_tag_verb_cancel_stamps(store: Store) -> None:
    """The ``tag(kind='job', add=['STATUS:cancelled'])`` route skips
    ``set_status`` and goes straight to ``add_tag``."""
    ref_id = _mk_job(store)
    store.add_tag(
        ref_id,
        Tag.parse_strict("STATUS:cancelled"),
        set_by="agent",
        replace_prefix=True,
    )
    _assert_stamped(store, ref_id)


def test_timeout_kill_stamps(store: Store) -> None:
    ref_id = _mk_job(store)
    meta = _meta(store, ref_id)
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", meta)
    spec = JobTypeSpec(
        name="fake",
        params_schema={},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset(),
        description="x",
        run=lambda *a, **k: None,
        dispatch=lambda *a, **k: None,
    )
    ssh_node._kill_and_terminalize(
        ctx, {"h": 1}, spec, swept_tag="deadline", summary="wall clock"
    )
    _assert_stamped(store, ref_id)


def test_pinned_target_node_wins_and_never_started_is_zero_wall(
    store: Store,
) -> None:
    ref_id = _mk_job(store, running=False, params={"target_node": "pinned-box"})
    _common.set_status(store, ref_id, "cancelled")
    meta = _meta(store, ref_id)
    assert meta["node"] == "pinned-box"
    assert meta["wall_s"] == 0.0
    assert datetime.fromisoformat(meta["finished_at"]).tzinfo is not None


def test_non_job_ref_not_stamped(store: Store) -> None:
    ref = store.insert_ref(kind="todo", slug=None, title="not a job", meta={})
    store.add_tag(ref.id, Tag.parse_strict("STATUS:failed"), set_by="agent")
    assert "finished_at" not in _meta(store, int(ref.id))


class _Ctx:
    def __init__(self, store: Store, ref_id: int) -> None:
        self.store = store
        self.ref_id = ref_id


@pytest.fixture
def no_autocatpath(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Simulate a host without the catalyst extra (CI): the kill/reap hooks
    must remove the scratch dir without importing ``autocatpath``."""
    import sys
    import tempfile

    # Model the submit's direct-child temp layout without touching global tmp.
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.setitem(sys.modules, "autocatpath", None)
    monkeypatch.delitem(sys.modules, "precis_pathway.runner", raising=False)


def test_reaper_skips_unstamped_and_reaps_stamped(
    store: Store, tmp_path: Path, no_autocatpath: None
) -> None:
    from precis_pathway import seed_job

    scratch = tmp_path / "autocatpath-seed-x"
    scratch.mkdir()
    (scratch / "result.json").write_text("{}", encoding="utf-8")
    handle = {"pid": 1, "dir": str(scratch)}

    ref_id = _mk_job(store)
    ctx = _Ctx(store, ref_id)

    # Running, no stamp yet: the dir must survive the reap attempt.
    seed_job._reap_scratch(ctx, handle, failed=False)
    assert os.path.isdir(scratch)

    # Terminal write lands the stamp; now the reap removes it.
    _common.set_status(store, ref_id, "succeeded")
    seed_job._reap_scratch(ctx, handle, failed=False)
    assert not os.path.exists(scratch)


def test_kill_keeps_dir_until_reap_hook_after_stamp(
    store: Store, tmp_path: Path, no_autocatpath: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis_pathway import seed_job

    scratch = tmp_path / "autocatpath-seed-y"
    scratch.mkdir()
    # A valid process handle with fake process operations: invalid/missing
    # pgids are now refused before kill, independently of stamp ordering.
    handle = {
        "pid": 4242,
        "pgid": 4242,
        "dir": str(scratch),
        "boot_id": "test-boot",
        "start_time": 1234,
    }
    monkeypatch.setattr(ssh_node, "_detached_identity_reason", lambda h: None)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda h: True)
    from precis_pathway import scratch as scratch_module

    def fake_kill(h: Any, *, keep_scratch: bool = False) -> None:
        assert h == handle
        assert keep_scratch
        assert scratch.is_dir()

    monkeypatch.setattr(scratch_module, "kill_seed_partial_detached", fake_kill)

    ref_id = _mk_job(store)
    meta = _meta(store, ref_id)
    ctx = claude_inproc._build_dispatch_context(store, ref_id, "t", meta)
    spec = JobTypeSpec(
        name="fake",
        params_schema={},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset(),
        description="x",
        run=lambda *a, **k: None,
        dispatch=lambda *a, **k: None,
        kill=seed_job._kill,
        reap=seed_job._reap,
    )
    seen: list[bool] = []
    real_reap = seed_job._reap_scratch

    def _spy(c: Any, h: Any, *, failed: bool) -> None:
        seen.append(os.path.isdir(scratch))  # dir still there at reap time
        real_reap(c, h, failed=failed)

    mp = pytest.MonkeyPatch()
    mp.setattr(seed_job, "_reap_scratch", _spy)
    try:
        ssh_node._kill_and_terminalize(
            ctx, handle, spec, swept_tag="deadline", summary="wall clock"
        )
    finally:
        mp.undo()
    assert seen == [True]
    assert not os.path.exists(scratch)
    assert "finished_at" in _meta(store, ref_id)


def test_poison_reap_sees_uncommitted_completion_stamp(
    store: Store, tmp_path: Path, no_autocatpath: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Poison cleanup uses its transaction's stamp, not a stale second read."""
    from precis_pathway import seed_job

    scratch = tmp_path / "autocatpath-seed-poison"
    scratch.mkdir()
    handle = {
        "pid": 4242,
        "pgid": 4242,
        "dir": str(scratch),
        "boot_id": "test-boot",
        "start_time": 1234,
    }
    ref_id = _mk_job(
        store,
        compute_handle=handle,
        params={"target_node": "node-a"},
        attempts=ssh_node._MAX_ATTEMPTS,
    )
    meta = _meta(store, ref_id)
    killed: list[Any] = []
    spec = JobTypeSpec(
        name="fake",
        params_schema={},
        compatible_executors=frozenset({"ssh_node"}),
        requires=frozenset(),
        description="x",
        run=lambda *a, **k: None,
        dispatch=lambda *a, **k: None,
        kill=lambda ctx, h: killed.append(h),
        reap=seed_job._reap,
    )
    monkeypatch.setattr(ssh_node, "get_job_type", lambda name: spec)
    monkeypatch.setattr(ssh_node, "_detached_identity_reason", lambda h: None)
    monkeypatch.setattr(ssh_node, "_detached_group_alive", lambda h: True)

    with store.pool.connection() as conn:
        assert _common.poison_guard(store, conn, ref_id, meta)
        # A fresh connection really cannot see the stamp at this point.
        assert "finished_at" not in _meta(store, ref_id)
        assert ssh_node._reap_detached_job(
            store, conn, ref_id, "poison", meta, node="node-a", require_grace=False
        )
        assert killed == [handle]
        assert not scratch.exists()
        conn.commit()
    assert "finished_at" in _meta(store, ref_id)
