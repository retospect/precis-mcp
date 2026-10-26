"""Store-level tests for migration 0250 and the ``struct_neb_*`` ops
(gr512126, slice 3): ``struct_neb_runs``/``struct_neb_images`` CRUD, the
``None``-means-untouched update contract, the succeeded-only unique index
on ``cache_key``, the queued/running inflight dedupe probe (joined to the
job's live STATUS so a dead job's row stops counting), the zombie-row
reaper, and the rolling-24 h quest + UTC-day non-quest mint counts.

Runs against real PG (the ``store`` fixture applies all migrations, so
these tests also pin that 0250 actually applies on an empty DB).
"""

from __future__ import annotations

import psycopg
import pytest

from precis.store import Store
from precis.store.types import Tag


def _structure(store: Store, slug: str) -> int:
    # insert_ref refuses a slug a live ref holds — reuse the existing one.
    hit = store.get_ref(kind="structure", id=slug)
    if hit is not None:
        return int(hit.id)
    return int(store.insert_ref(kind="structure", slug=slug, title=slug).id)


def _record(
    store: Store,
    *,
    cache_key: str = "k1",
    status: str = "queued",
    params: dict | None = None,
    is_slug: str = "is-a",
    fs_slug: str = "fs-a",
) -> int:
    return store.struct_neb_record_run(
        is_ref_id=_structure(store, is_slug),
        fs_ref_id=_structure(store, fs_slug),
        fidelity="ml",
        model="mace",
        params=params,
        cache_key=cache_key,
        job_ref_id=None,
        status=status,
    )


def _get(store: Store, run_id: int) -> dict:
    row = store.struct_neb_get(run_id)
    assert row is not None, run_id
    return row


def _inflight(store: Store, cache_key: str) -> dict:
    row = store.struct_neb_inflight(cache_key)
    assert row is not None, cache_key
    return row


def _job(store: Store, status: str) -> int:
    """A ``kind='job'`` ref tagged ``STATUS:<status>`` — the join partner
    ``struct_neb_inflight``/``struct_neb_reap_dead`` read a row's liveness
    off."""
    job = store.insert_ref(kind="job", slug=None, title="neb job", meta={})
    store.add_tag(
        job.id,
        Tag.closed("STATUS", status),
        set_by="system",
        replace_prefix=True,
    )
    return int(job.id)


def test_migration_0250_tables_and_succeeded_index_exist(store: Store) -> None:
    with store.pool.connection() as conn:
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_name IN ('struct_neb_runs', 'struct_neb_images')"
            ).fetchall()
        }
        assert tables == {"struct_neb_runs", "struct_neb_images"}
        idx = conn.execute(
            "SELECT indexdef FROM pg_indexes "
            "WHERE indexname = 'struct_neb_runs_key_succeeded'"
        ).fetchone()
    assert idx is not None
    assert "succeeded" in idx[0]  # partial: priced address only


def test_record_run_inserts_queued_row_with_null_scalars(store: Store) -> None:
    is_id, fs_id = _structure(store, "is-a"), _structure(store, "fs-a")
    is_run = store.structure_record_run(
        is_id,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=1,
        max_disp=0.0,
        energy=-1.0,
    )
    run_id = store.struct_neb_record_run(
        is_ref_id=is_id,
        fs_ref_id=fs_id,
        is_run_id=is_run,
        fidelity="ml",
        model="mace",
        params={"images": 7},
        cache_key="k1",
        job_ref_id=None,
    )
    row = _get(store, run_id)
    assert row is not None
    assert row["status"] == "queued"
    assert row["is_ref_id"] == is_id and row["fs_ref_id"] == fs_id
    assert row["is_run_id"] == is_run and row["fs_run_id"] is None
    assert row["cache_key"] == "k1"
    assert row["params"] == {"images": 7}
    for col in ("barrier_fwd_ev", "barrier_rev_ev", "delta_e_ev", "ts_index"):
        assert row[col] is None, col
    assert row["converged"] is False and row["trusted"] is False
    assert row["trust"] == []


def test_update_run_none_leaves_columns_untouched(store: Store) -> None:
    run_id = _record(store)
    store.struct_neb_update_run(run_id, status="running", wall_s=3.5)
    row = _get(store, run_id)
    assert row["status"] == "running" and row["wall_s"] == 3.5
    assert row["barrier_fwd_ev"] is None

    ts_id = _structure(store, "ts-a")
    store.struct_neb_update_run(
        run_id,
        status="succeeded",
        converged=True,
        barrier_fwd_ev=0.9,
        barrier_rev_ev=1.4,
        delta_e_ev=-0.5,
        ts_index=4,
        ts_fmax=0.04,
        ts_ref_id=ts_id,
        trust=[{"check": "saddle", "verdict": "pass"}],
        trusted=True,
    )
    row = _get(store, run_id)
    assert row["barrier_fwd_ev"] == 0.9 and row["ts_ref_id"] == ts_id
    assert row["trust"] == [{"check": "saddle", "verdict": "pass"}]

    # A later status-only flip must not null out the priced scalars.
    store.struct_neb_update_run(run_id, status="failed")
    row = _get(store, run_id)
    assert row["status"] == "failed"
    assert row["barrier_fwd_ev"] == 0.9 and row["ts_index"] == 4
    assert row["ts_ref_id"] == ts_id and row["trusted"] is True


def test_unique_index_allows_one_succeeded_row_per_cache_key(
    store: Store,
) -> None:
    first = _record(store, cache_key="dup")
    store.struct_neb_update_run(first, status="succeeded")
    second = _record(store, cache_key="dup")
    with pytest.raises(psycopg.errors.UniqueViolation):
        store.struct_neb_update_run(second, status="succeeded")

    # The index guards only the priced address: failed/queued dupes are fine.
    third = _record(store, cache_key="dup")
    store.struct_neb_update_run(third, status="failed")
    store.struct_neb_update_run(second, status="failed")


def test_inflight_tracks_queued_and_running_only(store: Store) -> None:
    assert store.struct_neb_inflight("k2") is None
    run_id = _record(store, cache_key="k2")
    assert _inflight(store, "k2")["id"] == run_id

    store.struct_neb_update_run(run_id, status="running")
    assert _inflight(store, "k2")["id"] == run_id

    store.struct_neb_update_run(run_id, status="failed")
    assert store.struct_neb_inflight("k2") is None

    newer = _record(store, cache_key="k2")
    assert _inflight(store, "k2")["id"] == newer


def test_cached_serves_only_succeeded_runs(store: Store) -> None:
    run_id = _record(store, cache_key="k3")
    assert store.struct_neb_cached("k3") is None
    store.struct_neb_update_run(run_id, status="succeeded", barrier_fwd_ev=1.1)
    hit = store.struct_neb_cached("k3")
    assert hit is not None and hit["id"] == run_id
    assert hit["barrier_fwd_ev"] == 1.1


def test_images_roundtrip_and_rewrite_replaces(store: Store) -> None:
    run_id = _record(store)
    band = [
        {
            "idx": i,
            "energy_ev": -10.0 + i * 0.1,
            "max_force": 0.05,
            "positions": {"frac": [[0.0, 0.0, 0.3]], "lattice": [[1, 0, 0]]},
        }
        for i in range(3)
    ]
    assert store.struct_neb_record_images(run_id, band) == 3
    rows = store.struct_neb_images(run_id)
    assert [r["idx"] for r in rows] == [0, 1, 2]
    assert rows[1]["energy_ev"] == -9.9
    assert rows[0]["positions"]["frac"] == [[0.0, 0.0, 0.3]]

    # The job may retry: a rewrite replaces rather than appending.
    assert store.struct_neb_record_images(run_id, band[:1]) == 1
    assert [r["idx"] for r in store.struct_neb_images(run_id)] == [0]


def test_runs_for_matches_is_fs_and_ts_sides(store: Store) -> None:
    run_id = _record(store, is_slug="is-x", fs_slug="fs-x")
    ts_id = _structure(store, "ts-x")
    store.struct_neb_update_run(run_id, status="succeeded", ts_ref_id=ts_id)
    row = _get(store, run_id)

    for side in (row["is_ref_id"], row["fs_ref_id"], ts_id):
        hits = store.struct_neb_runs_for(side)
        assert [r["id"] for r in hits] == [run_id], side

    bystander = _structure(store, "unrelated")
    assert store.struct_neb_runs_for(bystander) == []


def test_quest_mints_24h_counts_stamp_any_status(store: Store) -> None:
    quest_id = int(store.insert_ref(kind="quest", slug=None, title="quest").id)
    params = {"quest_ref_id": str(quest_id)}
    a = _record(store, cache_key="cap-a", params=params)
    _record(store, cache_key="cap-b", params=params)
    store.struct_neb_update_run(a, status="failed")  # a spent mint still counts
    _record(store, cache_key="cap-c")  # unstamped: not this quest's

    assert store.struct_neb_quest_mints_24h(quest_id) == 2

    other = int(store.insert_ref(kind="quest", slug=None, title="other").id)
    assert store.struct_neb_quest_mints_24h(other) == 0


def test_record_run_stamps_minted_versions_and_engine(store: Store) -> None:
    """``is_version``/``fs_version``/``engine`` round-trip — the band view
    loads the endpoints at these versions and the stale-render reads the
    token."""
    run_id = store.struct_neb_record_run(
        is_ref_id=_structure(store, "is-v"),
        fs_ref_id=_structure(store, "fs-v"),
        fidelity="ml",
        model="mace",
        params=None,
        cache_key="kv",
        job_ref_id=None,
        is_version=3,
        fs_version=7,
        engine="ac-9",
    )
    row = _get(store, run_id)
    assert row["is_version"] == 3 and row["fs_version"] == 7
    assert row["engine"] == "ac-9"


def test_inflight_joins_the_job_status(store: Store) -> None:
    """A queued/running row is in flight only while its job is
    non-terminal — a worker kill leaves the row lying 'running', and the
    join is what stops it blocking the edge forever."""
    run_id = _record(store, cache_key="kj")
    store.struct_neb_update_run(run_id, status="running")
    # job_ref_id NULL = the mint transaction still owns the row → live.
    assert _inflight(store, "kj")["id"] == run_id

    store.struct_neb_update_run(run_id, job_ref_id=_job(store, "failed"))
    assert store.struct_neb_inflight("kj") is None
    for status in ("succeeded", "cancelled"):
        store.struct_neb_update_run(
            run_id, job_ref_id=_job(store, status), status="queued"
        )
        assert store.struct_neb_inflight("kj") is None, status

    store.struct_neb_update_run(run_id, job_ref_id=_job(store, "running"))
    assert _inflight(store, "kj")["id"] == run_id


def test_reap_dead_fails_only_zombie_rows(store: Store) -> None:
    """``struct_neb_reap_dead`` fails queued/running rows whose job went
    terminal, and only those — a live job's row and a mint-owned
    (job_ref_id NULL) row stay."""
    zombie = _record(store, cache_key="kr")
    store.struct_neb_update_run(zombie, status="running")
    store.struct_neb_update_run(zombie, job_ref_id=_job(store, "failed"))
    live = _record(store, cache_key="kr")
    store.struct_neb_update_run(live, job_ref_id=_job(store, "running"))
    mint_owned = _record(store, cache_key="kr")  # job_ref_id stays NULL
    terminal = _record(store, cache_key="kr", status="succeeded")
    store.struct_neb_update_run(terminal, job_ref_id=_job(store, "failed"))

    assert store.struct_neb_reap_dead("kr") == [zombie]
    assert _get(store, zombie)["status"] == "failed"
    assert _get(store, live)["status"] == "queued"
    assert _get(store, mint_owned)["status"] == "queued"
    assert _get(store, terminal)["status"] == "succeeded"
    # Idempotent and scoped to the key.
    assert store.struct_neb_reap_dead("kr") == []
    assert store.struct_neb_reap_dead("no-such-key") == []


def test_nonquest_mints_today_counts_unstamped_only(store: Store) -> None:
    """The global UTC-day cap's count: rows no quest can be charged for —
    an explicit ``null`` stamp counts the same as an absent one (a todo
    serving no quest lands here too)."""
    assert store.struct_neb_nonquest_mints_today() == 0
    _record(store, cache_key="nq-a")
    _record(store, cache_key="nq-b", params={"quest_ref_id": None})
    quest = int(store.insert_ref(kind="quest", slug=None, title="q").id)
    _record(store, cache_key="nq-c", params={"quest_ref_id": str(quest)})

    assert store.struct_neb_nonquest_mints_today() == 2


def test_cap_counts_exclude_dedupe_collapsed_rows(store: Store) -> None:
    """gr518100: a mint that collapsed onto a live job's idem_key spent
    no compute — its dedupe-failed row must not draw down either daily
    cap. A genuinely-failed run still counts (it burned the budget)."""
    dedupe_trust = [
        {
            "id": "precis-dedupe",
            "check": "dedupe",
            "verdict": "not-run",
            "severity": "info",
            "seed": 0,
            "step": None,
            "evidence": {"reason": "collapsed onto in-flight job jo9"},
        }
    ]
    quest = int(store.insert_ref(kind="quest", slug=None, title="q").id)
    q_stamp = {"quest_ref_id": str(quest)}

    collapsed_q = _record(store, cache_key="dd-qa", params=q_stamp)
    store.struct_neb_update_run(collapsed_q, status="failed", trust=dedupe_trust)
    collapsed_nq = _record(store, cache_key="dd-nqa")
    store.struct_neb_update_run(collapsed_nq, status="failed", trust=dedupe_trust)
    assert store.struct_neb_quest_mints_24h(quest) == 0
    assert store.struct_neb_nonquest_mints_today() == 0

    spent_q = _record(store, cache_key="dd-qb", params=q_stamp)
    store.struct_neb_update_run(spent_q, status="failed")
    spent_nq = _record(store, cache_key="dd-nqb")
    store.struct_neb_update_run(spent_nq, status="failed")
    assert store.struct_neb_quest_mints_24h(quest) == 1
    assert store.struct_neb_nonquest_mints_today() == 1


def test_stamp_harvest_watermark_is_monotonic(store: Store) -> None:
    """neb-s6 note C: ``quest_neb_harvested_upto`` only moves forward
    (GREATEST in SQL) — an overlapping harvest's stale upto landing last
    must not rewind the bookmark and re-read runs already journaled. The
    per-key edge merge and ``extra`` keys still land on every stamp."""
    is_id = _structure(store, "is-wm")

    store.struct_neb_stamp_harvest(
        is_id,
        {"a:b": {"barrier": 0.4}},
        harvested_upto=7,
        extra={"barrier": 0.4},
    )
    store.struct_neb_stamp_harvest(
        is_id,
        {"c:d": {"barrier": 0.9}},
        harvested_upto=3,
        extra={"barrier": 0.9},
    )

    ref = store.get_ref(kind="structure", id=is_id)
    assert ref is not None and ref.meta is not None
    assert ref.meta["quest_neb_harvested_upto"] == 7
    # Later stamps still merge their edges and extra keys.
    assert set(ref.meta["neb_edges"]) == {"a:b", "c:d"}
    assert ref.meta["barrier"] == 0.9
