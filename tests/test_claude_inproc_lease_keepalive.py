"""claude_inproc — mid-run lease keepalive + superseded-run safety (gr369528)."""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc

pytestmark = pytest.mark.db


def _mk_running(store: Store, *, lease_offset_s: int = 60) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="long claude_inproc job",
        meta={
            "executor": "claude_inproc",
            "job_type": "x",
            "lease_boot_id": "gen-1",
            "lease_process": "precis-worker",
            "lease_host": "melchior",
        },
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:running"), set_by="agent")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + make_interval(secs => %s))::text"
            ") WHERE ref_id = %s",
            (lease_offset_s, int(ref.id)),
        )
        conn.commit()
    return int(ref.id)


def _meta(store: Store, rid: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT meta FROM refs WHERE ref_id=%s", (rid,)).fetchone()
    assert row is not None
    return dict(row[0])


def _status(store: Store, rid: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (rid,),
        ).fetchone()
    return row[0] if row else None


def _steal(store: Store, rid: int) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_boot_id', 'gen-2') WHERE ref_id = %s",
            (rid,),
        )
        conn.commit()


def test_renew_lease_extends_when_mine(store: Store) -> None:
    rid = _mk_running(store)
    meta = _meta(store, rid)
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", meta)
    assert ctx.renew_lease is not None
    assert ctx.renew_lease() is True
    assert "_lease_lost" not in meta
    with store.pool.connection() as conn:
        secs = conn.execute(
            "SELECT extract(epoch FROM (meta->>'lease_until')::timestamptz - now())"
            " FROM refs WHERE ref_id=%s",
            (rid,),
        ).fetchone()
    # lease was 60s; renewed to ~90 min
    assert secs is not None and float(secs[0]) > 80 * 60


def test_renew_lease_false_and_flags_when_stolen(store: Store) -> None:
    rid = _mk_running(store)
    meta = _meta(store, rid)
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", meta)
    _steal(store, rid)
    assert ctx.renew_lease is not None
    assert ctx.renew_lease() is False
    assert meta["_lease_lost"] is True


def test_superseded_run_exception_does_not_mark_failed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _mk_running(store)
    meta = _meta(store, rid)
    meta["_lease_lost"] = True
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", meta)

    def boom(_c: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(claude_inproc, "_run_one", boom)
    assert claude_inproc._run_job_safe(ctx) is False
    assert _status(store, rid) == "running"


def test_live_run_exception_still_marks_failed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    rid = _mk_running(store)
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", _meta(store, rid))

    def boom(_c: Any) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(claude_inproc, "_run_one", boom)
    assert claude_inproc._run_job_safe(ctx) is False
    assert _status(store, rid) == "failed"


def test_lease_lost_skips_finalize(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.job_types import JobTypeSpec

    def _dispatch(ctx: Any, _s: Any) -> None:
        ctx.meta["_lease_lost"] = True

    spec = JobTypeSpec(
        name="x",
        params_schema={"type": "object"},
        compatible_executors=frozenset({"claude_inproc"}),
        requires=frozenset(),
        description="fake",
        run=lambda *_a, **_k: "",
        dispatch=_dispatch,
    )
    monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
    rid = _mk_running(store)
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", _meta(store, rid))
    claude_inproc._run_one(ctx)
    assert _status(store, rid) == "running"


def test_append_chunk_retries_on_ord_collision(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from psycopg.errors import UniqueViolation

    rid = _mk_running(store)
    ctx = claude_inproc._build_dispatch_context(store, rid, "t", _meta(store, rid))
    real = claude_inproc._append_chunk
    calls = {"n": 0}

    def flaky(*a: Any, **k: Any) -> None:
        calls["n"] += 1
        if calls["n"] == 1:
            raise UniqueViolation("chunks_ref_id_ord_key")
        real(*a, **k)

    monkeypatch.setattr(claude_inproc, "_append_chunk", flaky)
    ctx.append_chunk("job_event", "hello")
    assert calls["n"] == 2
    texts = [c.text for c in store.chunks.list_chunks_for_ref(rid)]
    assert "hello" in texts
