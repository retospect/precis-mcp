"""Per-repo lease derived from live job rows (``workers/repo_lease``)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers import repo_lease as rl
from precis.workers._mint import mint_background_job
from precis.workers.repo_lease import LeaseRow, decide

REPO = "acme/widgets"
NOW = 1_000_000.0


def _lease(repo: str = REPO, kind: str = "land") -> dict[str, Any]:
    return {"repo": repo, "kind": kind, "at": "2026-01-01T00:00:00+00:00"}


def _holds(jid: int, **kw: Any) -> LeaseRow:
    return LeaseRow(job_id=jid, terminal=False, lease=_lease(), **kw)


def _wants(jid: int) -> LeaseRow:
    return LeaseRow(
        job_id=jid, terminal=False, want={"repo": REPO, "at": "2026-01-01T00:00:00Z"}
    )


def test_land_blocked_by_live_land_holder() -> None:
    """A second land must wait for the live holder, naming it; else lands
    of one repo run concurrently again."""
    d = decide([_holds(1)], REPO, job_id=2, kind="land", now=NOW)
    assert d.blocker == {"job_id": 1, "kind": "land", "reason": "held"}


def test_land_blocked_by_wanting_release() -> None:
    """A waiting release outranks new lands; if this fails a stream of lands
    starves the release forever."""
    d = decide([_wants(5)], REPO, job_id=2, kind="land", now=NOW)
    assert d.blocker is not None and d.blocker["reason"] == "wanted"
    assert d.blocker["job_id"] == 5


def test_release_not_blocked_by_wanting_release() -> None:
    """Two releases waiting must not deadlock on each other's want flag."""
    d = decide([_wants(5)], REPO, job_id=6, kind="release", now=NOW)
    assert d.blocker is None


def test_release_blocked_by_land_holder() -> None:
    """A release must not start while a land is mid-flight."""
    d = decide([_holds(1)], REPO, job_id=6, kind="release", now=NOW)
    assert d.blocker == {"job_id": 1, "kind": "land", "reason": "held"}


def test_terminal_holder_and_wanter_are_ignored() -> None:
    """A terminal job frees the lease with no cleanup; if it still counted a
    failed job would freeze the repo."""
    rows = [
        LeaseRow(job_id=1, terminal=True, lease=_lease()),
        LeaseRow(job_id=2, terminal=True, want={"repo": REPO}),
    ]
    assert decide(rows, REPO, job_id=3, kind="land", now=NOW).blocker is None


def test_other_repo_does_not_block() -> None:
    """Leases are per repo; a holder of another repo must not block this one."""
    row = LeaseRow(job_id=1, terminal=False, lease=_lease("other/repo"))
    assert decide([row], REPO, job_id=2, kind="land", now=NOW).blocker is None


def test_stale_holder_ignored_and_flagged() -> None:
    """A holder dead past its deadline must not freeze the repo, and must be
    reported; a holder inside the grace window still counts."""
    past = NOW - rl.STALE_AFTER_S - 1
    d = decide([_holds(1, deadline=past)], REPO, job_id=2, kind="land", now=NOW)
    assert d.blocker is None and d.stale == [1] and d.holder is None
    fresh = NOW - rl.STALE_AFTER_S + 1
    d = decide([_holds(1, deadline=fresh)], REPO, job_id=2, kind="land", now=NOW)
    assert d.blocker is not None and d.stale == []


def test_own_lease_is_idempotent() -> None:
    """Re-acquiring a held lease returns no blocker, even with a waiting
    release around; else a retried land would refuse itself."""
    d = decide([_holds(2), _wants(5)], REPO, job_id=2, kind="land", now=NOW)
    assert d.blocker is None and d.holder is not None and d.holder["job_id"] == 2


# -- DB -------------------------------------------------------------------


def _job(store: Store, jt: str = "land_slice", status: str = "running") -> int:
    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type=jt,
            executor="ssh_node",
            title="t",
            status=status,
        )
    return int(ref.id)


def _meta(store: Store, jid: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (jid,)).fetchone()
    assert row is not None
    return dict(row[0])


def _terminal(store: Store, jid: int, status: str = "succeeded") -> None:
    store.add_tag(
        jid, Tag.closed("STATUS", status), set_by="system", replace_prefix=True
    )


def _alert_count(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'alert' AND alert_source = 'repo_lease'"
        ).fetchone()
    assert row is not None
    return int(row[0])


@pytest.mark.db
def test_acquire_stamps_meta_and_refuses_second_job(store: Store) -> None:
    """The lease is the job's meta; a second land must be refused naming the
    holder."""
    a, b = _job(store), _job(store)
    assert rl.acquire(store, REPO, job_id=a, kind="land") is None
    lease = _meta(store, a)[rl.LEASE_KEY]
    assert lease["repo"] == REPO and lease["kind"] == "land" and lease["at"]
    assert rl.acquire(store, REPO, job_id=a, kind="land") is None  # idempotent
    assert rl.acquire(store, REPO, job_id=b, kind="land") == {
        "job_id": a,
        "kind": "land",
        "reason": "held",
    }
    assert rl.LEASE_KEY not in _meta(store, b)
    h = rl.holder(store, REPO)
    assert h is not None and h["job_id"] == a and h["kind"] == "land"


@pytest.mark.db
def test_release_frees_the_lease(store: Store) -> None:
    """An explicit release must let the next job in, and be idempotent."""
    a, b = _job(store), _job(store)
    rl.acquire(store, REPO, job_id=a, kind="land")
    rl.release(store, REPO, job_id=a)
    rl.release(store, REPO, job_id=a)
    assert rl.LEASE_KEY not in _meta(store, a)
    assert rl.holder(store, REPO) is None
    assert rl.acquire(store, REPO, job_id=b, kind="land") is None


@pytest.mark.db
def test_want_blocks_lands_and_release_acquire_clears_it(store: Store) -> None:
    """A waiting release must stop new lands (reason wanted) yet acquire
    itself, clearing its want flag."""
    rel = _job(store, "repo_release")
    land = _job(store)
    rl.want(store, REPO, job_id=rel)
    rl.want(store, REPO, job_id=rel)  # idempotent
    assert _meta(store, rel)[rl.WANT_KEY]["repo"] == REPO
    assert rl.acquire(store, REPO, job_id=land, kind="land") == {
        "job_id": rel,
        "kind": "release",
        "reason": "wanted",
    }
    assert rl.acquire(store, REPO, job_id=rel, kind="release") is None
    meta = _meta(store, rel)
    assert rl.WANT_KEY not in meta and meta[rl.LEASE_KEY]["kind"] == "release"
    got = rl.acquire(store, REPO, job_id=land, kind="land")
    assert got is not None and got["reason"] == "held"


@pytest.mark.db
def test_terminal_holder_frees_the_lease(store: Store) -> None:
    """Every terminal path frees the lease with no explicit release."""
    a, b = _job(store), _job(store)
    rl.acquire(store, REPO, job_id=a, kind="land")
    _terminal(store, a, "failed")
    assert rl.holder(store, REPO) is None
    assert rl.acquire(store, REPO, job_id=b, kind="land") is None


@pytest.mark.db
def test_stale_holder_is_skipped_and_alerted_once(store: Store) -> None:
    """A dead holder past its deadline must not freeze the repo; the alert
    fires once per (repo, job) however often the lease is asked."""
    a, b = _job(store), _job(store)
    rl.acquire(store, REPO, job_id=a, kind="land")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object('deadline', %s::float8)"
            " WHERE ref_id = %s",
            (time.time() - rl.STALE_AFTER_S - 60, a),
        )
    assert rl.holder(store, REPO) is None
    assert rl.holder(store, REPO) is None
    assert _alert_count(store) == 1
    assert rl.acquire(store, REPO, job_id=b, kind="land") is None
    assert _alert_count(store) == 1
