"""Reserve-at-claim — the resource_slots reservation mechanism (slice 6c).

A job that declares ``meta.requires`` reserves those slots on the claiming
host in the claim transaction (the conditional decrement is the lock); the
reservation is stamped on ``meta.reserved`` and refunded at any terminal
transition (executor via ``set_status``, or the sweeper directly). Jobs
without ``meta.requires`` are unaffected — the mechanism is inert until a
job opts in, so this is dark in prod until slice 6d wires real requires.
"""

from __future__ import annotations

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
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (jid,)
        ).fetchone()[0]
    assert "reserved" not in meta


def test_claim_without_requires_unaffected(store: Store) -> None:
    jid = _queue_job(store, executor="ex_res_h")  # no requires
    rows = _claim(store, "ex_res_h", "rh_h")
    assert [r[0] for r in rows] == [jid]
    assert "reserved" not in rows[0][2]


# ── 6d-deferred: capability-rarity claim ordering ────────────────────────


def test_scarcity_ranks_rare_capability_first(store: Store) -> None:
    """A low-prio job needing a RARE capability is claimed ahead of a
    high-prio job needing a common one (§5.3: scarcity → prio → age).

    Uses test-unique resource tokens so the shared test DB's other rows can't
    pollute the per-resource host count that drives the scarcity score.
    """
    store.sync_host_resource_slots("sc_solo", {"scarce_x": 1})  # 1 host — rare
    for h in ("sc_1", "sc_2", "sc_3"):
        store.sync_host_resource_slots(h, {"common_y": 2})  # 3 hosts — common
    _common = _queue_job(store, executor="ex_sc", requires={"common_y": 1}, prio=9)
    rare = _queue_job(store, executor="ex_sc", requires={"scarce_x": 1}, prio=1)
    rows = _claim(store, "ex_sc", "sc_1", limit=1)  # only the top-ranked
    assert [r[0] for r in rows] == [rare]


def test_no_requires_queue_keeps_prio_age_order(store: Store) -> None:
    """With nothing requiring a capability, scarcity is 0 everywhere and the
    claim order collapses to prio DESC, age ASC — byte-identical to pre-6d."""
    lo = _queue_job(store, executor="ex_sc2", prio=2)
    hi = _queue_job(store, executor="ex_sc2", prio=8)
    rows = _claim(store, "ex_sc2", "h1", limit=10)
    assert [r[0] for r in rows] == [hi, lo]


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
