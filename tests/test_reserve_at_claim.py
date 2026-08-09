"""Reserve-at-claim — the resource_slots reservation mechanism (slice 6c).

A job that declares ``meta.requires`` reserves those slots on the claiming
host in the claim transaction (the conditional decrement is the lock); the
reservation is stamped on ``meta.reserved`` and refunded at any terminal
transition (executor via ``set_status``, or the sweeper directly). Jobs
without ``meta.requires`` are unaffected — the mechanism is inert until a
job opts in, so this is dark in prod until slice 6d wires real requires.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.store import Store
from precis.store._resource_slots_ops import (
    release_resource_slots,
    reserve_resource_slots,
)
from precis.store.types import Tag
from precis.workers.executors._common import (
    claim_executor_jobs,
    release_job_reservation,
    set_status,
)


@pytest.fixture
def _autocatpath_seed_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject ``autocatpath_seed`` into the job_types registry for the test —
    same pattern as ``test_quest_compute.py``'s ``_autocatpath_seed_job_types``:
    the dev container's installed ``entry_points.txt`` is a build-time
    snapshot, so a pyproject entry-point isn't live without a reinstall.
    ``monkeypatch.setitem`` auto-reverts, so this doesn't leak into other
    test modules."""
    pytest.importorskip("autocatpath")
    from precis.workers import job_types as jt
    from precis_pathway import seed_job

    monkeypatch.setitem(jt._REGISTRY, "autocatpath_seed", seed_job.SPEC)


def _queue_job(
    store: Store,
    *,
    executor: str,
    requires: dict[str, int] | None = None,
    prio: int | None = None,
) -> int:
    meta: dict[str, object] = {
        "job_type": "demo",
        "executor": executor,
        "params": {},
    }
    if requires is not None:
        meta["requires"] = requires
    ref = store.insert_ref(kind="job", slug=None, title="j", meta=meta, prio=prio)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return ref.id


def _claim(
    store: Store,
    executor: str,
    host: str,
    *,
    node: str | None = None,
    limit: int = 10,
):
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor=executor,
            limit=limit,
            node=node,
            reserve_host_id=host,
        )
        conn.commit()
    return rows


def _free(store: Store, host: str, resource: str) -> int | None:
    for s in store.resource_slots_for_host(host):
        if s.resource == resource:
            return s.free
    return None


def _age_job(store: Store, ref_id: int, minutes: float) -> None:
    """Backdate a job's ``refs.created_at`` so it reads as queued
    ``minutes`` ago — used to exercise the ``llm:`` affinity grace window
    (:data:`~precis.workers.executors._common.LLM_AFFINITY_GRACE_MIN`)
    without actually sleeping in the test."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - (%s || ' minutes')::interval "
            "WHERE ref_id = %s",
            (minutes, ref_id),
        )
        conn.commit()


# ── low-level reserve / release ──────────────────────────────────────────


def test_reserve_decrements_free(store: Store) -> None:
    store.sync_host_resource_slots("rh_a", {"gpu": 2})
    with store.pool.connection() as conn:
        assert reserve_resource_slots(conn, "rh_a", {"gpu": 1}) is True
        conn.commit()
    assert _free(store, "rh_a", "gpu") == 1


def test_reserve_refuses_past_zero(store: Store) -> None:
    store.sync_host_resource_slots("rh_b", {"gpu": 1})
    with store.pool.connection() as conn:
        assert reserve_resource_slots(conn, "rh_b", {"gpu": 1}) is True
        assert reserve_resource_slots(conn, "rh_b", {"gpu": 1}) is False
        conn.commit()
    assert _free(store, "rh_b", "gpu") == 0  # never went negative


def test_reserve_all_or_nothing_refunds_partial(store: Store) -> None:
    """gpu reserves, podman (no row) fails → gpu is refunded, overall False."""
    store.sync_host_resource_slots("rh_c", {"gpu": 1})
    with store.pool.connection() as conn:
        ok = reserve_resource_slots(conn, "rh_c", {"gpu": 1, "podman": 1})
        conn.commit()
    assert ok is False
    assert _free(store, "rh_c", "gpu") == 1  # partial reservation rolled back


def test_reserve_missing_resource_fails(store: Store) -> None:
    store.sync_host_resource_slots("rh_d", {"gpu": 1})
    with store.pool.connection() as conn:
        assert reserve_resource_slots(conn, "rh_d", {"tts": 1}) is False
        conn.commit()


def test_release_caps_at_capacity(store: Store) -> None:
    store.sync_host_resource_slots("rh_e", {"gpu": 1})
    with store.pool.connection() as conn:
        # release without a prior reserve must not inflate free past capacity
        release_resource_slots(conn, "rh_e", {"gpu": 5})
        conn.commit()
    assert _free(store, "rh_e", "gpu") == 1


# ── claim integration ────────────────────────────────────────────────────


def test_claim_reserves_and_stamps(store: Store) -> None:
    store.sync_host_resource_slots("rh_f", {"gpu": 1})
    jid = _queue_job(store, executor="ex_res_f", requires={"gpu": 1})
    rows = _claim(store, "ex_res_f", "rh_f")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"] == {"host": "rh_f", "slots": {"gpu": 1}}
    assert _free(store, "rh_f", "gpu") == 0


def test_claim_skips_when_no_free_slot(store: Store) -> None:
    store.sync_host_resource_slots("rh_g", {"gpu": 1})
    with store.pool.connection() as conn:
        reserve_resource_slots(conn, "rh_g", {"gpu": 1})  # pre-exhaust
        conn.commit()
    jid = _queue_job(store, executor="ex_res_g", requires={"gpu": 1})
    rows = _claim(store, "ex_res_g", "rh_g")
    assert rows == []  # unreservable here → not claimed
    # the job stays queued (claim never stamped it)
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]


def test_claim_without_requires_unaffected(store: Store) -> None:
    jid = _queue_job(store, executor="ex_res_h")  # no requires
    rows = _claim(store, "ex_res_h", "rh_h")
    assert [r[0] for r in rows] == [jid]
    assert "reserved" not in rows[0][2]


# ── 6d-deferred: capability-rarity claim ordering ────────────────────────


def test_scarcity_ranks_rare_capability_first(store: Store) -> None:
    """A less-urgent (high-number-prio) job needing a RARE capability is
    claimed ahead of a more-urgent (low-number-prio) job needing a common
    one (§5.3: scarcity → prio → age) — proving scarcity is the FIRST sort
    key, ranking above prio (0014: lower prio == more urgent) rather than
    merely riding along with it.

    Uses test-unique resource tokens so the shared test DB's other rows can't
    pollute the per-resource host count that drives the scarcity score.
    """
    store.sync_host_resource_slots("sc_solo", {"scarce_x": 1})  # 1 host — rare
    for h in ("sc_1", "sc_2", "sc_3"):
        store.sync_host_resource_slots(h, {"common_y": 2})  # 3 hosts — common
    _common = _queue_job(store, executor="ex_sc", requires={"common_y": 1}, prio=1)
    rare = _queue_job(store, executor="ex_sc", requires={"scarce_x": 1}, prio=9)
    rows = _claim(store, "ex_sc", "sc_1", limit=1)  # only the top-ranked
    assert [r[0] for r in rows] == [rare]


def test_no_requires_queue_keeps_prio_age_order(store: Store) -> None:
    """With nothing requiring a capability, scarcity is 0 everywhere and the
    claim order collapses to prio ASC (0014: lower == more urgent), age ASC —
    byte-identical to pre-6d."""
    lo = _queue_job(store, executor="ex_sc2", prio=2)
    hi = _queue_job(store, executor="ex_sc2", prio=8)
    rows = _claim(store, "ex_sc2", "h1", limit=10)
    assert [r[0] for r in rows] == [lo, hi]


# ── 6d-deferred: soft memory-pressure veto ───────────────────────────────


def test_mem_pressure_vetoes_heavy_job(store: Store) -> None:
    store.sync_host_resource_slots("hp", {"gpu": 1})
    store.sync_soft_signal("hp", "mem", 0, 2)  # under pressure
    _queue_job(store, executor="ex_v", requires={"gpu": 1})
    rows = _claim(store, "ex_v", "hp", limit=10)  # res_host = hp
    assert rows == []  # heavy job vetoed on the pressured host


def test_mem_ok_allows_heavy_job(store: Store) -> None:
    store.sync_host_resource_slots("hq", {"gpu": 1})
    store.sync_soft_signal("hq", "mem", 2, 2)  # plenty
    jid = _queue_job(store, executor="ex_v2", requires={"gpu": 1})
    rows = _claim(store, "ex_v2", "hq", limit=10)
    assert [r[0] for r in rows] == [jid]


def test_mem_pressure_does_not_veto_commodity(store: Store) -> None:
    store.sync_soft_signal("hr", "mem", 0, 2)  # pressured
    jid = _queue_job(store, executor="ex_v3")  # no requires → not heavy
    rows = _claim(store, "ex_v3", "hr", limit=10)
    assert [r[0] for r in rows] == [jid]


# ── release at terminal ──────────────────────────────────────────────────


def test_release_job_reservation_refunds_and_is_idempotent(store: Store) -> None:
    store.sync_host_resource_slots("rh_i", {"gpu": 1})
    jid = _queue_job(store, executor="ex_res_i", requires={"gpu": 1})
    _claim(store, "ex_res_i", "rh_i")
    assert _free(store, "rh_i", "gpu") == 0
    with store.pool.connection() as conn:
        release_job_reservation(conn, jid)
        conn.commit()
    assert _free(store, "rh_i", "gpu") == 1
    # meta.reserved cleared → a second release is a no-op (no inflation)
    with store.pool.connection() as conn:
        release_job_reservation(conn, jid)
        conn.commit()
    assert _free(store, "rh_i", "gpu") == 1


def test_set_status_terminal_refunds(store: Store) -> None:
    store.sync_host_resource_slots("rh_j", {"gpu": 1})
    jid = _queue_job(store, executor="ex_res_j", requires={"gpu": 1})
    _claim(store, "ex_res_j", "rh_j")
    assert _free(store, "rh_j", "gpu") == 0
    with store.pool.connection() as conn:
        set_status(store, jid, "failed", conn=conn)
        conn.commit()
    assert _free(store, "rh_j", "gpu") == 1


def test_set_status_nonterminal_does_not_refund(store: Store) -> None:
    store.sync_host_resource_slots("rh_k", {"gpu": 1})
    jid = _queue_job(store, executor="ex_res_k", requires={"gpu": 1})
    _claim(store, "ex_res_k", "rh_k")
    with store.pool.connection() as conn:
        set_status(store, jid, "running", conn=conn)
        conn.commit()
    assert _free(store, "rh_k", "gpu") == 0  # still held while running


def test_epoch_reclaim_refunds_stale_reservation_before_re_reserving(
    store: Store,
) -> None:
    """§H piece 2 acceptance: a STATUS:running row epoch-reclaimed (the
    holder was provably replaced, even with `lease_until` still in the
    future) has its stale `meta.reserved` slot refunded before this claim
    re-reserves — same crash-recovery semantics as an expiry reclaim, no
    leak (the slot doesn't come back free-and-unclaimed) and no double-
    reserve (the capacity-1 host isn't driven negative)."""
    host = "rh_epoch_refund"
    store.sync_host_resource_slots(host, {"gpu": 1})
    store.record_heartbeat(host, meta={"boot_ids": {"ex_res_epoch": "new-gen"}})

    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="orphaned reserved job",
        meta={
            "job_type": "demo",
            "executor": "ex_res_epoch",
            "params": {},
            "requires": {"gpu": 1},
            "reserved": {"host": host, "slots": {"gpu": 1}},
            "lease_boot_id": "dead-gen",
            "lease_process": "ex_res_epoch",
            "lease_host": host,
        },
    )
    jid = int(ref.id)
    store.add_tag(ref.id, Tag.closed("STATUS", "running"), set_by="agent")
    with store.pool.connection() as conn:
        # A capacity-1 host with the slot already held by the prior
        # (dead) generation's reservation — free=0, mirroring what the
        # original claim actually left behind.
        conn.execute(
            "UPDATE resource_slots SET free = 0 WHERE host = %s AND resource = 'gpu'",
            (host,),
        )
        conn.execute(
            "UPDATE refs SET meta = meta || jsonb_build_object("
            "  'lease_until', (now() + interval '1 hour')::text"  # far future
            ") WHERE ref_id = %s",
            (jid,),
        )
        conn.commit()
    assert _free(store, host, "gpu") == 0  # sanity: pre-exhausted

    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn,
            executor="ex_res_epoch",
            limit=10,
            reserve_host_id=host,
            reclaim_stale_running=True,
        )
        conn.commit()

    assert [r[0] for r in rows] == [jid]  # reclaimed via the epoch arm
    assert rows[0][2]["reserved"] == {"host": host, "slots": {"gpu": 1}}
    # Refunded then re-reserved — net free still 0 (held, not leaked/doubled).
    assert _free(store, host, "gpu") == 0


# ── §F cycle a acceptance: free=0 two-claim race ──────────────────────────


def test_free_zero_two_claim_race_exactly_one_wins(store: Store) -> None:
    """Two concurrent claims racing for a capacity-1 resource (the shape
    ``job_inproc``'s ``embed_batch`` uses against the ``embedder`` slot) —
    the all-or-nothing conditional decrement inside ``claim_executor_jobs``
    IS the lock: exactly one of the two queued jobs claims the slot, the
    other stays queued. Driven with a REAL concurrent race (two threads,
    two pool connections), not sequential same-connection calls, so the
    row-lock on the shared ``resource_slots`` row is actually contended."""
    from concurrent.futures import ThreadPoolExecutor

    store.sync_host_resource_slots("race_host", {"embedder": 1})
    j1 = _queue_job(store, executor="ex_race", requires={"embedder": 1})
    j2 = _queue_job(store, executor="ex_race", requires={"embedder": 1})

    def _do_claim() -> list[tuple[int, str, dict[str, object]]]:
        return _claim(store, "ex_race", "race_host", limit=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(_do_claim) for _ in range(2)]
        results = [f.result() for f in futures]

    claimed_ids = [r[0][0] for r in results if r]
    assert len(claimed_ids) == 1  # exactly one claim across both racers
    assert claimed_ids[0] in (j1, j2)
    assert _free(store, "race_host", "embedder") == 0  # the winner holds it

    # The loser's job is untouched — still queued, no reservation stamped.
    loser = j2 if claimed_ids[0] == j1 else j1
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (loser,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]


# ── requires derivation + target_node host + self-gating (slice 6d) ───────


def test_effective_requires_derivation() -> None:
    from precis.workers.executors._common import effective_requires

    # derived from the job_type's ServiceSpec (requires={"gpu"})
    assert effective_requires({"job_type": "struct_relax"}) == {"gpu": 1}
    assert effective_requires({"job_type": "fold"}) == {"gpu": 1}
    # unknown job_type → nothing
    assert effective_requires({"job_type": "demo"}) == {}
    assert effective_requires({}) == {}
    # an explicit meta.requires overrides derivation
    assert effective_requires({"job_type": "struct_relax", "requires": {"tts": 2}}) == {
        "tts": 2
    }


def _queue_typed_job(
    store: Store,
    *,
    executor: str,
    job_type: str,
    target_node: str | None = None,
    requires: dict[str, int] | None = None,
) -> int:
    params: dict[str, object] = {}
    if target_node is not None:
        params["target_node"] = target_node
    meta: dict[str, object] = {
        "job_type": job_type,
        "executor": executor,
        "params": params,
    }
    if requires is not None:
        meta["requires"] = requires
    ref = store.insert_ref(kind="job", slug=None, title=job_type, meta=meta)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return ref.id


def test_derived_requires_reserved_on_target_node(store: Store) -> None:
    """A struct_relax job reserves gpu on its target_node, not the claimer."""
    store.sync_host_resource_slots("spark_d", {"gpu": 1})
    jid = _queue_typed_job(
        store, executor="ex_d1", job_type="struct_relax", target_node="spark_d"
    )
    # The node gate admits a spark_d-pinned job to a spark_d worker; the
    # reservation lands on target_node (spark_d), NOT the reserve_host_id
    # identity — proving res_host = target_node takes precedence.
    rows = _claim(store, "ex_d1", "melchior_d", node="spark_d")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"] == {"host": "spark_d", "slots": {"gpu": 1}}
    assert _free(store, "spark_d", "gpu") == 0
    assert _free(store, "melchior_d", "gpu") is None  # reserve_host_id untouched


def test_self_gating_falls_back_when_capability_unadvertised(store: Store) -> None:
    """target_node hasn't advertised gpu yet → claim, don't stall, don't reserve."""
    jid = _queue_typed_job(
        store, executor="ex_d2", job_type="struct_relax", target_node="spark_e"
    )
    rows = _claim(store, "ex_d2", "melchior_e", node="spark_e")
    assert [r[0] for r in rows] == [jid]  # claimed via the pin — no stall
    assert "reserved" not in rows[0][2]  # nothing reserved (self-gated off)


# ── llm:-requirement is a TIME-BOUNDED claim affinity (unlike the gpu
# soft-drop above) ──────────────────────────────────────────────────────
#
# Non-``llm:`` requires (``gpu`` etc.) keep the soft self-gate:
# ``test_self_gating_falls_back_when_capability_unadvertised`` and
# ``test_autocatpath_seed_on_gpu_less_host_claims_unthrottled`` already pin
# that an unadvertised non-``llm:`` requirement is dropped and the job still
# claims. An ``llm:`` requirement instead prefers a host that serves the
# model: an unadvertised ``llm:`` slot skips the claim (the row stays
# queued, retried until a serving host claims it) *while the job is still
# within* ``LLM_AFFINITY_GRACE_MIN`` of being queued — but past that grace
# window, the claim falls back to proceeding host-agnostic and unreserved
# for that slot (gr-incident: a model advertised only on a host that runs
# no executor for this job type stranded the job forever under the old
# hard veto).


def test_llm_requirement_claimed_on_host_that_advertises_it(store: Store) -> None:
    store.sync_host_resource_slots("host_llm_a", {"llm:qwen-x": 1})
    jid = _queue_job(store, executor="ex_llm_a", requires={"llm:qwen-x": 1})
    rows = _claim(store, "ex_llm_a", "host_llm_a")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"] == {"host": "host_llm_a", "slots": {"llm:qwen-x": 1}}
    assert _free(store, "host_llm_a", "llm:qwen-x") == 0


def test_llm_requirement_not_claimed_on_host_that_does_not_advertise_it(
    store: Store,
) -> None:
    """No ``llm:qwen-x`` row on this host at all, job freshly queued (well
    within the grace window) — the claim is skipped (affinity preference),
    unlike an unadvertised ``gpu`` requirement which would still claim,
    unreserved."""
    jid = _queue_job(store, executor="ex_llm_b", requires={"llm:qwen-x": 1})
    rows = _claim(store, "ex_llm_b", "host_llm_b_no_advert")
    assert rows == []  # not claimed — stays queued
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]
    # confirm it's genuinely still queued (claimable), not silently dropped —
    # a second claim pass after advertising the resource picks it up.
    store.sync_host_resource_slots("host_llm_b_no_advert", {"llm:qwen-x": 1})
    rows2 = _claim(store, "ex_llm_b", "host_llm_b_no_advert")
    assert [r[0] for r in rows2] == [jid]


def test_llm_requirement_fallback_claims_host_agnostic_past_grace(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """Regression (prod incident): a job whose ``llm:`` requirement is
    advertised only on a host that never runs this executor (e.g. a
    system-profile-only host) must not stall forever — past
    ``LLM_AFFINITY_GRACE_MIN`` it claims host-agnostic and unreserved for
    that slot, since the model is reachable via the LLM router regardless
    of co-location."""
    from precis.workers.executors._common import LLM_AFFINITY_GRACE_MIN

    jid = _queue_job(store, executor="ex_llm_e", requires={"llm:qwen-x": 1})
    _age_job(store, jid, LLM_AFFINITY_GRACE_MIN + 1)
    with caplog.at_level("INFO", logger="precis.workers.executors._common"):
        rows = _claim(store, "ex_llm_e", "host_llm_e_no_advert")
    assert [r[0] for r in rows] == [jid]
    assert "reserved" not in rows[0][2]  # nothing to reserve — slot unadvertised
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]
    assert any("llm-affinity fallback" in rec.getMessage() for rec in caplog.records)


def test_llm_requirement_still_gated_within_grace(store: Store) -> None:
    """Same unserved-model setup, but the job is younger than the grace
    window — affinity still wins, claim is skipped (not claimed)."""
    from precis.workers.executors._common import LLM_AFFINITY_GRACE_MIN

    jid = _queue_job(store, executor="ex_llm_f", requires={"llm:qwen-x": 1})
    _age_job(store, jid, max(LLM_AFFINITY_GRACE_MIN - 1, 0))
    rows = _claim(store, "ex_llm_f", "host_llm_f_no_advert")
    assert rows == []
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]
    assert meta_row[0].get("lease_host") is None  # not claimed at all


def test_llm_requirement_claimed_with_reservation_regardless_of_age(
    store: Store,
) -> None:
    """When the claiming host DOES advertise the model, it claims with a
    reservation exactly as before — even for a job old enough that the
    affinity fallback would otherwise kick in, since the fallback path is
    only reached when the slot is unadvertised."""
    from precis.workers.executors._common import LLM_AFFINITY_GRACE_MIN

    store.sync_host_resource_slots("host_llm_g", {"llm:qwen-x": 1})
    jid = _queue_job(store, executor="ex_llm_g", requires={"llm:qwen-x": 1})
    _age_job(store, jid, LLM_AFFINITY_GRACE_MIN + 30)
    rows = _claim(store, "ex_llm_g", "host_llm_g")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"] == {"host": "host_llm_g", "slots": {"llm:qwen-x": 1}}
    assert _free(store, "host_llm_g", "llm:qwen-x") == 0


def test_llm_requirement_target_node_claimable_when_node_advertises(
    store: Store,
) -> None:
    """target_node=H + requires llm:X, H advertises llm:X → claimable on H
    (reservation lands on the target_node, not the claiming host identity)."""
    store.sync_host_resource_slots("spark_llm", {"llm:qwen-x": 1})
    jid = _queue_typed_job(
        store,
        executor="ex_llm_c",
        job_type="demo",
        target_node="spark_llm",
        requires={"llm:qwen-x": 1},
    )
    rows = _claim(store, "ex_llm_c", "melchior_llm", node="spark_llm")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"] == {"host": "spark_llm", "slots": {"llm:qwen-x": 1}}
    assert _free(store, "spark_llm", "llm:qwen-x") == 0


def test_llm_requirement_target_node_skipped_when_node_does_not_advertise(
    store: Store,
) -> None:
    """target_node=H + requires llm:X, H does NOT advertise llm:X → skipped,
    not claimed unreserved."""
    jid = _queue_typed_job(
        store,
        executor="ex_llm_d",
        job_type="demo",
        target_node="spark_llm_bare",
        requires={"llm:qwen-x": 1},
    )
    rows = _claim(store, "ex_llm_d", "melchior_llm_d", node="spark_llm_bare")
    assert rows == []
    with store.pool.connection() as conn:
        meta_row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()
    assert meta_row is not None
    assert "reserved" not in meta_row[0]


# ── gr192371/gr197264: autocatpath_seed gpu reservation ───────────────────


def test_autocatpath_seed_serializes_on_single_gpu_slot(
    store: Store, hub: Any, _autocatpath_seed_registered: None
) -> None:
    """The load-bearing regression test: an ``autocatpath_seed`` job minted
    through the real ``JobHandler.put(..., requires={"gpu": 1})`` (not
    ``store.insert_ref`` — a future regression in ``put()``'s signature/
    wiring must be caught here too) reserves the target node's counted
    ``gpu`` slot at claim, so two seeds pinned to a single-GPU host
    serialize one-at-a-time instead of running concurrently."""
    from precis.handlers.job import JobHandler
    from precis.handlers.todo import TodoHandler

    store.sync_host_resource_slots("spark", {"gpu": 1})
    todo_out = TodoHandler(hub=hub).put(text="autocatpath aggregate")
    todo_id = todo_out.ref_id
    assert todo_id is not None

    def _mint(seed: int) -> int:
        out = JobHandler(hub=hub).put(
            job_type="autocatpath_seed",
            executor="ssh_node",
            parent_id=todo_id,
            idem_key=f"autocatpath_seed:test:{seed}",
            requires={"gpu": 1},
            params={
                "config": {"name": "demo"},
                "seed": seed,
                "model_index": 0,
                "content_key": f"ck{seed}",
                "target_node": "spark",
            },
        )
        assert out.ref_id is not None
        return int(out.ref_id)

    seed_a = _mint(1)
    seed_b = _mint(2)

    # First claim pass: exactly one seed claims the single gpu slot.
    rows1 = _claim(store, "ssh_node", "spark", node="spark")
    assert [r[0] for r in rows1] == [seed_a]
    assert rows1[0][2]["reserved"] == {"host": "spark", "slots": {"gpu": 1}}
    assert _free(store, "spark", "gpu") == 0

    # Second claim pass: seed_b cannot claim while seed_a holds the slot.
    rows2 = _claim(store, "ssh_node", "spark", node="spark")
    assert rows2 == []

    # Free the slot by driving seed_a to a terminal status.
    set_status(store, seed_a, "succeeded")
    assert _free(store, "spark", "gpu") == 1

    # Third claim pass: seed_b now claims the freed slot.
    rows3 = _claim(store, "ssh_node", "spark", node="spark")
    assert [r[0] for r in rows3] == [seed_b]
    assert _free(store, "spark", "gpu") == 0


def test_autocatpath_seed_on_gpu_less_host_claims_unthrottled(
    store: Store, hub: Any, _autocatpath_seed_registered: None
) -> None:
    """Companion negative test pinning the self-gating fallback (root-cause
    #4, ``_common.py``'s ``reservable`` fall-through): a seed minted with
    ``requires={"gpu": 1}`` but pinned to a host that has NO ``gpu`` row in
    ``resource_slots`` still claims (doesn't stall forever) and runs with
    ``meta.reserved`` absent — so nobody later "fixes" this fall-through
    into a stall."""
    from precis.handlers.job import JobHandler
    from precis.handlers.todo import TodoHandler

    # NOTE: no store.sync_host_resource_slots(...) for "spark_nogpu" — the
    # host has never advertised a gpu row.
    todo_out = TodoHandler(hub=hub).put(text="autocatpath aggregate (no gpu host)")
    todo_id = todo_out.ref_id
    assert todo_id is not None

    out = JobHandler(hub=hub).put(
        job_type="autocatpath_seed",
        executor="ssh_node",
        parent_id=todo_id,
        idem_key="autocatpath_seed:test:nogpu",
        requires={"gpu": 1},
        params={
            "config": {"name": "demo"},
            "seed": 1,
            "model_index": 0,
            "content_key": "ck_nogpu",
            "target_node": "spark_nogpu",
        },
    )
    seed_id = out.ref_id
    assert seed_id is not None

    rows = _claim(store, "ssh_node", "spark_nogpu", node="spark_nogpu")
    assert [r[0] for r in rows] == [seed_id]  # claimed via the pin, no stall
    assert "reserved" not in rows[0][2]


def test_explicit_requires_overrides_job_type_derivation(store: Store) -> None:
    store.sync_host_resource_slots("host_d3", {"podman": 1})
    jid = _queue_typed_job(
        store,
        executor="ex_d3",
        job_type="struct_relax",
        requires={"podman": 1},
    )
    rows = _claim(store, "ex_d3", "host_d3")
    assert [r[0] for r in rows] == [jid]
    assert rows[0][2]["reserved"]["slots"] == {"podman": 1}  # explicit wins
    assert _free(store, "host_d3", "podman") == 0
