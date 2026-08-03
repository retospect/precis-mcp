"""``job_inproc`` executor (§F cycle a) — pin the shared §H wiring
(epoch-reclaim, poison_guard) onto THIS executor, and the plugin-dispatch
happy path. The underlying epoch/expiry/attempt-cap machinery itself is
already exhaustively covered by ``test_claude_inproc_epoch.py`` — this
file only proves job_inproc wires it the same way, mirroring that file's
shape.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import job_inproc
from precis.workers.job_types import JobTypeSpec

pytestmark = pytest.mark.db


def _spec(*, dispatch: Any, name: str = "fake_embed_batch") -> JobTypeSpec:
    def _run(*_a: Any, **_k: Any) -> str:
        raise NotImplementedError

    return JobTypeSpec(
        name=name,
        params_schema={"type": "object"},
        compatible_executors=frozenset({"job_inproc"}),
        requires=frozenset(),
        description="fake job_type for tests",
        run=_run,
        dispatch=dispatch,
    )


def _status(store: Store, ref_id: int) -> str | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
    return row[0] if row else None


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0] or {})


def _mk_queued_job(store: Store) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="job_inproc test job",
        meta={"executor": "job_inproc", "job_type": "fake_embed_batch", "params": {}},
    )
    store.add_tag(ref.id, Tag.closed("STATUS", "queued"), set_by="agent")
    return int(ref.id)


def _mk_running_job(
    store: Store,
    *,
    lease_offset_s: int,
    attempts: int | None = None,
    lease_boot_id: str | None = None,
    lease_process: str | None = None,
    lease_host: str | None = None,
) -> int:
    meta: dict[str, Any] = {"executor": "job_inproc", "job_type": "fake_embed_batch"}
    if attempts is not None:
        meta["attempts"] = attempts
    if lease_boot_id is not None:
        meta["lease_boot_id"] = lease_boot_id
    if lease_process is not None:
        meta["lease_process"] = lease_process
    if lease_host is not None:
        meta["lease_host"] = lease_host
    ref = store.insert_ref(
        kind="job", slug=None, title="orphaned job_inproc job", meta=meta
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


# ── plugin-dispatch happy path ───────────────────────────────────────────


def test_dispatch_runs_synchronously_and_finalizes_succeeded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ran: list[int] = []

    def _dispatch(ctx: Any, _s: Any) -> None:
        ran.append(ctx.ref_id)
        ctx.append_chunk("job_summary", "embedded 3 chunk(s)")

    monkeypatch.setattr(
        job_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_queued_job(store)

    result = job_inproc.run_job_inproc_pass(store, limit=1)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert ran == [rid]
    assert _status(store, rid) == "succeeded"


def test_unknown_job_type_fails_infra(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_inproc, "get_job_type", lambda name: None)
    rid = _mk_queued_job(store)

    result = job_inproc.run_job_inproc_pass(store, limit=1)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "failed"
    assert _meta(store, rid)["failure_class"] == "infra"


# ── §H boot epoch: reclaim_stale_running wiring ──────────────────────────


def test_epoch_mismatch_reclaims_before_lease_expiry(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A running job whose claiming generation is provably replaced is
    reclaimed on the FIRST claim pass, even with a lease hours away from
    expiry — pins that job_inproc wires reclaim_stale_running=True the
    same way claude_inproc/ssh_node do."""
    monkeypatch.setattr(
        job_inproc,
        "get_job_type",
        lambda name: _spec(dispatch=lambda c, s: c.set_status("succeeded")),
    )
    store.record_heartbeat(
        "melchior-jip-epoch", meta={"boot_ids": {"precis-worker": "new-gen"}}
    )
    rid = _mk_running_job(
        store,
        lease_offset_s=3600,
        lease_boot_id="dead-gen",
        lease_process="precis-worker",
        lease_host="melchior-jip-epoch",
    )

    result = job_inproc.run_job_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    assert _status(store, rid) == "succeeded"
    assert _meta(store, rid)["reclaims"][-1]["why"] == "epoch"


# ── §H piece 3: poison_guard wiring ───────────────────────────────────────


# ── mid-drain lease renewal (renew_own_lease) ────────────────────────────


def test_renew_own_lease_extends_lease_when_still_mine(store: Store) -> None:
    """The happy path: the renewal's own claim identity still matches the
    DB row, so ``lease_until`` is pushed out and the call reports True."""
    rid = _mk_running_job(
        store,
        lease_offset_s=60,
        lease_boot_id="gen-1",
        lease_process="precis-worker",
        lease_host="melchior",
    )
    meta = _meta(store, rid)
    before = meta["lease_until"]

    ok = job_inproc.renew_own_lease(store, rid, meta)

    assert ok is True
    assert "_lease_lost" not in meta
    after = _meta(store, rid)["lease_until"]
    assert after > before


def test_renew_own_lease_false_and_stops_when_lease_stolen(store: Store) -> None:
    """Another worker generation reclaimed the row (its lease identity no
    longer matches what THIS caller's stale ``meta`` remembers) — the
    renewal must report False, must NOT extend ``lease_until``, and must
    stamp the caller's ``meta`` so ``_run_one`` can skip its finalize."""
    rid = _mk_running_job(
        store,
        lease_offset_s=60,
        lease_boot_id="gen-1",
        lease_process="precis-worker",
        lease_host="melchior",
    )
    stale_meta = _meta(store, rid)  # captured BEFORE the steal
    before = stale_meta["lease_until"]
    # A new generation reclaims + re-stamps its own identity.
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_boot_id', 'gen-2') WHERE ref_id = %s",
            (rid,),
        )
        conn.commit()

    ok = job_inproc.renew_own_lease(store, rid, stale_meta)

    assert ok is False
    assert stale_meta.get("_lease_lost") is True
    assert _meta(store, rid)["lease_until"] == before  # untouched


def test_lease_lost_flag_skips_happy_path_finalize(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dispatcher that renews mid-run and loses the lease must NOT be
    finalized SUCCEEDED by this (stale) process — the new owner drives the
    job to its own terminal status."""

    def _dispatch(ctx: Any, _s: Any) -> None:
        # Simulate another generation having stolen the job mid-dispatch.
        ctx.meta["_lease_lost"] = True

    monkeypatch.setattr(
        job_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_queued_job(store)

    result = job_inproc.run_job_inproc_pass(store, limit=1)

    assert result == {"claimed": 1, "ok": 1, "failed": 0}
    # Left at "running" (its state as of the claim) — NOT finalized to
    # succeeded by this stale process; the new owner drives it from here.
    assert _status(store, rid) == "running"


def test_poison_guard_fails_past_max_attempts(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job re-claimed past the shared attempt cap is failed (bubbled),
    never dispatched — pins that job_inproc calls poison_guard on its
    claimed rows the same way claude_inproc/ssh_node do."""
    from precis.workers.executors._common import MAX_ATTEMPTS

    dispatched = {"n": 0}

    def _dispatch(ctx: Any, _s: Any) -> None:
        dispatched["n"] += 1
        ctx.set_status("succeeded")

    monkeypatch.setattr(
        job_inproc, "get_job_type", lambda name: _spec(dispatch=_dispatch)
    )
    rid = _mk_running_job(store, lease_offset_s=-60, attempts=MAX_ATTEMPTS)

    result = job_inproc.run_job_inproc_pass(store, limit=2)

    assert result == {"claimed": 1, "ok": 0, "failed": 1}
    assert _status(store, rid) == "failed"
    assert dispatched["n"] == 0
    assert _meta(store, rid)["failure_class"] == "infra"
