"""Footprint admission + model affinity on the pull (gr595542).

The claim reserves counted tokens only before this slice: a ``gpu:1``
job never proved its model's footprint fits the host's free VRAM, and a
science-lane job never proved its predicted peak RSS fits free memory.
These tests pin the three additions
(:mod:`precis.workers.footprint_admission`, wired in
``executors/_common.claim_executor_jobs``):

* GPU admission — a job naming ``meta.params.model`` whose model has a
  ``model_footprint`` row (0322) reserves ``vram_mb`` = footprint +
  margin, all-or-nothing with its ``gpu`` token; shortfall refuses the
  claim; a missing model row or missing host ``vram_mb`` capacity row
  leaves the token-only claim unchanged (dark start). The reservation
  also needs the per-card ``vram_card_mb`` ceiling, and
  :func:`~precis.workers.footprint_admission.admission_block` refuses
  the claim when the footprint exceeds any single card or the host's
  latest measured free headroom (``host_heartbeat.meta``) can't cover
  the units — the static ledger alone over-admits past untracked
  consumers (td655172). A held reservation stays pending in *full*
  until the reservation itself releases: the measured free is
  aggregate, so no drop can be attributed to the reserved job over an
  untracked consumer, and releasing on drops could free a hold whose
  job had not allocated (td655171 review; replaces the td655172
  drop-evidenced release) — and when
  the beat carries ``vram_free_cards_mb`` the check
  becomes a *pick*: the claim binds one card (``meta.reserved.gpu_card``)
  under that card's headroom — raw free net of the host's
  ``PRECIS_HOST_VRAM_RESERVE_MB`` reserve, which the per-card probe
  leaves raw and the beat's own ``vram_reserve_mb`` stamp carries (the
  measured host's setting — never the claimer's env, which a remote
  ``target_node`` claim would otherwise subtract; td655172 review) — and
  ``PRECIS_GPU_CARD_MAX_JOBS`` cap.
* CPU admission — an ``ssh_node`` job whose exact ``(job_type,
  meta.size)`` rollup key has at least ``PRECIS_FOOTPRINT_MIN_SAMPLES``
  (5) measured runs reserves ``mem_mb`` = p95 × margin; shortfall
  refuses; below the sample floor, or on any other executor, no memory
  reserves. Live free memory (heartbeat meta — *admittable*: available
  less the host's ``PRECIS_HOST_MEM_RESERVE_MB`` OS reserve, the same
  subtraction the static capacity row carries, td655171) can refuse the
  claim even when the ledger has room.
* Model affinity — a successful claim stamps the reservation host's
  last-used model (``app_state`` ``claim.last_model.<host>``), resolved
  through the job's *effective* model (``params.model`` else the
  job_type's ``default_model`` — ``struct_relax`` → ``mace_mp``), so an
  implicit default steers the queue like an explicit one; the re-rank
  prefers queued jobs naming that model among rows the fairness keys
  tied, and the stamp decays after
  ``PRECIS_MODEL_AFFINITY_TTL_MIN`` (30 min).

Helpers mirror ``tests/test_reserve_at_claim.py`` — every advertisement
seeds the matching fresh ``host_heartbeat`` row (``_advertised_by_host``
joins for freshness), and a synthetic executor name isolates each test.
"""

from __future__ import annotations

import math
import time
from typing import Any

import pytest
from psycopg.types.json import Jsonb

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import (
    claim_executor_jobs,
    set_status,
)

HOST = "fp-host"


def _advertise(store: Store, host: str, slots: dict[str, int | None]) -> None:
    """Seed ``resource_slots`` AND a fresh ``host_heartbeat`` row — the
    advertised-map read joins for freshness (gr333274 discipline, same as
    ``tests/test_reserve_at_claim.py``'s ``_advertise``)."""
    store.sync_host_resource_slots(host, slots)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (host,),
        )
        conn.commit()


def _seed_live_free(
    store: Store,
    host: str,
    *,
    vram_free_mb: int | None = None,
    mem_free_mb: int | None = None,
    vram_free_cards_mb: list[int] | None = None,
    vram_reserve_mb: int | None = 0,
) -> None:
    """Stamp the host's latest measured *admittable* free headroom (MiB)
    on its ``host_heartbeat`` meta — what ``admission_block`` cross-checks
    the footprint units against (td655172). The live probe already nets
    the host's reserve into the stamped value (``mem_free_mb`` =
    available − ``PRECIS_HOST_MEM_RESERVE_MB``, td655171), so seeded
    numbers here are post-reserve headroom. ``vram_free_cards_mb`` is the
    RAW per-card list card-bound admission picks from — the pick nets
    ``vram_reserve_mb``, the measured host's own reserve stamped
    alongside the list (td655172 review), off the card under test, so
    seeded numbers here are pre-reserve. The two travel together on a
    real beat, so the stamp defaults to ``0`` whenever the list is
    seeded; explicit ``vram_reserve_mb=None`` seeds the stamp-less
    stale-beat shape. Keys with
    ``None`` are left absent (dark, ledger-only)."""
    meta: dict[str, Any] = {}
    if vram_free_mb is not None:
        meta["vram_free_mb"] = vram_free_mb
    if mem_free_mb is not None:
        meta["mem_free_mb"] = mem_free_mb
    if vram_free_cards_mb is not None:
        meta["vram_free_cards_mb"] = vram_free_cards_mb
        if vram_reserve_mb is not None:
            meta["vram_reserve_mb"] = vram_reserve_mb
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts, meta) VALUES (%s, now(), %s) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts, "
            "meta = COALESCE(host_heartbeat.meta, '{}'::jsonb) || EXCLUDED.meta",
            (host, Jsonb(meta)),
        )
        conn.commit()


def _queue_job(
    store: Store,
    *,
    executor: str,
    job_type: str = "fp_demo",
    requires: dict[str, int] | None = None,
    params: dict[str, Any] | None = None,
    size: dict[str, Any] | None = None,
    prio: int | None = None,
) -> int:
    meta: dict[str, Any] = {
        "job_type": job_type,
        "executor": executor,
        "params": params or {},
    }
    if requires is not None:
        meta["requires"] = requires
    if size is not None:
        meta["size"] = size
    ref = store.insert_ref(kind="job", slug=None, title="fp", meta=meta, prio=prio)
    store.add_tag(
        ref.id, Tag.closed("STATUS", "queued"), set_by="agent", replace_prefix=True
    )
    return ref.id


def _claim(
    store: Store, executor: str, host: str = HOST, *, limit: int = 10
) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = claim_executor_jobs(
            conn, executor=executor, limit=limit, reserve_host_id=host
        )
        conn.commit()
    return rows


def _free(store: Store, host: str, resource: str) -> int | None:
    for s in store.resource_slots_for_host(host):
        if s.resource == resource:
            return s.free
    return None


def _reserved_meta(store: Store, ref_id: int) -> dict[str, Any] | None:
    """The job's full ``meta.reserved`` annotation (slots + claim epoch +
    bound card), or ``None`` when it reserved nothing."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->'reserved' FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return dict(row[0]) if row and row[0] else None


def _reserved_slots(store: Store, ref_id: int) -> dict[str, int] | None:
    reserved = _reserved_meta(store, ref_id)
    return dict(reserved.get("slots") or {}) if reserved else None


def _seed_rss_samples(
    store: Store,
    *,
    job_type: str,
    size: dict[str, Any],
    rss_mb: float,
    n: int,
) -> None:
    """Terminal-less sample refs carrying ``meta.footprint`` — the exact
    shape the ``(job_type, meta.size)`` rollup reads (gr595536)."""
    for _ in range(n):
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="sample",
            meta={
                "job_type": job_type,
                "executor": "fp_demo",
                "params": {},
                "size": size,
                "footprint": {"ru_maxrss_mb": rss_mb, "wall_s": 10.0, "cpu_s": 5.0},
            },
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "succeeded"),
            set_by="agent",
            replace_prefix=True,
        )


def _stamp_last_model(
    store: Store, host: str, model: str, *, age_s: float = 0.0
) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (f"claim.last_model.{host}", model),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, %s) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (f"claim.last_model_at.{host}", str(time.time() - age_s)),
        )
        conn.commit()


def _set_queued_at(store: Store, ref_id: int, ts: Any) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = %s "
            "FROM tags t WHERE t.tag_id = rt.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'STATUS' AND t.value = 'queued'",
            (ts, ref_id),
        )
        conn.commit()


# ── GPU admission ──────────────────────────────────────────────────────

#: The per-card ceiling these tests advertise — one 60 GiB card pair's
#: worth of headroom over the 40 GiB mace_mp footprint.
_CARD_MB = 60000


def test_gpu_claim_reserves_model_vram(store: Store) -> None:
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1, "vram_mb": 40000 + 1024}
    assert _free(store, HOST, "gpu") == 1
    assert _free(store, HOST, "vram_mb") == 120000 - 41024


def test_gpu_claim_refused_when_vram_short(store: Store) -> None:
    """Capacity covers one reservation; a second finds ``free`` short of
    the (unclamped) units and is refused all-or-nothing."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 50000, "vram_card_mb": _CARD_MB})
    first = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    second = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    assert _claim(store, "ssh_node") == []
    # Refusal is all-or-nothing: neither the token nor the VRAM moved.
    assert _free(store, HOST, "gpu") == 1
    assert _free(store, HOST, "vram_mb") == 50000 - 41024
    assert _reserved_slots(store, second) is None


def test_gpu_claim_clamps_oversize_vram_to_capacity(store: Store) -> None:
    """gr595542 review: footprint + margin above the host's advertised
    ``vram_mb`` capacity (total less reserve) must not strand the job —
    the units clamp to capacity so it claims on an empty host and takes
    the whole VRAM row."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 30000, "vram_card_mb": _CARD_MB})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1, "vram_mb": 30000}
    assert _free(store, HOST, "vram_mb") == 0


def test_gpu_claim_refused_when_model_exceeds_any_card(store: Store) -> None:
    """td655172: two 24 GiB cards advertise 48 GiB host-wide, which the
    ledger alone reads as fitting a 40 GiB model — but no single card can
    hold it. The per-card ceiling must refuse the claim."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 48000, "vram_card_mb": 24576})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "gpu") == 2
    assert _free(store, HOST, "vram_mb") == 48000
    assert _reserved_slots(store, job) is None


def test_gpu_claim_without_card_row_falls_back_to_token(store: Store) -> None:
    """No ``vram_card_mb`` row (older heartbeat) → GPU admission dark,
    the token-only claim unchanged."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 48000})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1}
    assert _free(store, HOST, "vram_mb") == 48000


def test_gpu_claim_refused_when_live_vram_free_short(store: Store) -> None:
    """td655172: the ledger row is static, so a consumer that never
    reserved (an LLM server, an interactive session) doesn't shrink it —
    the measured free headroom on the heartbeat must refuse the claim."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=20000)
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "gpu") == 2
    assert _free(store, HOST, "vram_mb") == 120000
    assert _reserved_slots(store, job) is None


def test_gpu_claim_admitted_when_live_vram_free_covers(store: Store) -> None:
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=80000)
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1, "vram_mb": 40000 + 1024}


def test_batch_claims_do_not_overadmit_measured_headroom(store: Store) -> None:
    """td655172: 50 GiB measured free, 120 GiB ledger — two 40 GiB claims
    in one batch must not both pass; the second sees the first as pending."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=50000)
    jobs = [
        _queue_job(
            store,
            executor="ssh_node",
            requires={"gpu": 1},
            params={"model": "mace_mp"},
        )
        for _ in range(2)
    ]

    rows = _claim(store, "ssh_node")

    assert len(rows) == 1
    assert rows[0][0] in jobs
    assert _free(store, HOST, "gpu") == 1
    assert _free(store, HOST, "vram_mb") == 120000 - 41024
    # A later, separate claim pass is held back by the same pending hold.
    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "vram_mb") == 120000 - 41024


def test_fresh_beat_keeps_pending_hold(store: Store) -> None:
    """td655172 review: a beat stamped AFTER the claim must NOT release
    the hold — the job may not have allocated (let alone peaked) when the
    beat measured. Holds never release on drops (td655171), so the
    hold's full units stay pending and the second 40 GiB
    job stays refused against 50 GiB measured free."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=50000)
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert _claim(store, "ssh_node") == []
    # A newer beat still reports the pre-allocation free number, and no
    # drop releases a hold anyway (td655171) — the full units keep
    # counting however stale the claim gets (no timer release).
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE host_heartbeat SET ts = now() + interval '1 hour', "
            "meta = meta || '{\"vram_free_mb\": 50000}'::jsonb WHERE host = %s",
            (HOST,),
        )
        conn.commit()
    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "vram_mb") == 120000 - 41024
    assert _reserved_meta(store, second) is None


def test_untracked_drop_does_not_release_a_hold(store: Store) -> None:
    """td655171 review: the beat's free is aggregate — a drop below the
    claim-time level can't be attributed to the reserved job over an
    untracked consumer, so no drop ever releases a hold. Here an
    untracked consumer eats 52 GiB while the reserved job hasn't
    allocated: the drop-evidenced release this replaces would have freed
    the whole 5024 hold and admitted the second claim into headroom the
    first job still needs (over-admission)."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=60000)
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    # The hold's units are 5024. The beat drops to 8000 — a 52 GiB drop
    # no single 5024 claim can have caused, i.e. provably an untracked
    # consumer. The hold still counts in full: effective free
    # 8000 - 5024 = 2976 < 5024 → refused. Release-on-drop would have
    # zeroed the hold and admitted into real headroom of ~2976.
    _seed_live_free(store, HOST, vram_free_mb=8000)
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, second) is None


def test_bound_hold_stays_pending_on_a_consumption_sized_drop(
    store: Store,
) -> None:
    """td655171 review: a card's measured drop equal to the hold's units
    is indistinguishable from an untracked consumer's — the hold is never
    released on drops, only on the reservation's own refund. Baseline
    12000 → 6976 (exactly the hold's 5024): the second claim reads
    effective free 1952 < 5024 and is refused, where the drop-release
    this replaces would have zeroed the hold and admitted it."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[12000, 4000])
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    reserved = _reserved_meta(store, first)
    assert reserved is not None and reserved["gpu_card"] == 0
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    _seed_live_free(store, HOST, vram_free_cards_mb=[6976, 4000])
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, second) is None


def test_second_claim_shares_a_card_while_a_hold_is_pending(
    store: Store,
) -> None:
    """Pending holds don't block the 1–2-jobs-per-card concurrency the
    gripe wants: card 0's effective free (measured less the held units)
    still covers the second claim, which binds the same card under its
    cap — the hold counting in full until release costs headroom, not
    the slot."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[60000, 4000])
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    reserved = _reserved_meta(store, first)
    assert reserved is not None and reserved["gpu_card"] == 0
    assert "free_at_claim" not in reserved and "at" not in reserved
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    # The beat shows the first job's consumption (60000 → 54976); the
    # hold still counts in full, leaving 49952 effective — enough for
    # the second 5024 claim, which binds the same card under the cap.
    _seed_live_free(store, HOST, vram_free_cards_mb=[54976, 4000])
    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [second]
    reserved2 = _reserved_meta(store, second)
    assert reserved2 is not None and reserved2["gpu_card"] == 0


def test_bound_hold_stays_pending_on_an_incomplete_drop(
    store: Store,
) -> None:
    """The delayed/late-peaking case: a drop smaller than the hold's
    units can't be attributed to the job either — the hold keeps
    counting in full, so the effective free is refused exactly as if
    the job hadn't run."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[6000, 4000])
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    # Baseline 6000, measured 5500: the 500 MiB drop can't be attributed
    # to this job → the full 5024 stays pending → effective
    # 5500 - 5024 = 476 < 5024 → refused.
    _seed_live_free(store, HOST, vram_free_cards_mb=[5500, 4000])
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, second) is None


def test_pending_holds_never_release_on_drops(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two holds on one card: no drop is attributed to either, so both
    count in full while their reservations live — the effective free
    refuses the third claim even though the measured card could read
    as though one job's footprint already showed."""
    monkeypatch.setenv("PRECIS_GPU_CARD_MAX_JOBS", "3")
    store.upsert_model_footprint("mace_mp", 1, source="test")  # 1 + 1024 margin = 1025
    store.upsert_model_footprint("mace_big", 2928, source="test")  # 3952
    _advertise(store, HOST, {"gpu": 3, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[6000, 4000])
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    third = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_big"},
    )
    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [first]
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [second]
    store.add_tag(
        second, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    # Card 0 drops 1025 — exactly the second hold's units, plausibly its
    # consumption but unattributable. BOTH holds count in full:
    # 4975 - 2050 = 2925 < 3952 refuses the third claim (card 1's
    # 3000 is short too); releasing either hold on the drop would
    # admit it.
    _seed_live_free(store, HOST, vram_free_cards_mb=[4975, 3000])
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, third) is None


def test_dark_claim_stays_pending_while_held(store: Store) -> None:
    """A claim admitted while the host's probe was dark counts in full
    like every other hold: pending for its reservation's whole life,
    since nothing releases a hold but the reservation's own refund."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    first = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    store.add_tag(
        first, Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    second = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    # The probe comes up reporting 5024 free. The dark-admitted hold's
    # full units still count → pending 5024 → effective 0 → refused.
    _seed_live_free(store, HOST, vram_free_mb=5024)
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, second) is None


# ── GPU card assignment (td655172) ────────────────────────────────────


def test_gpu_claim_binds_the_freest_card(store: Store) -> None:
    """The per-card list turns the veto into a pick: the claim admits
    onto the card with the most effective free and stamps it on
    ``meta.reserved.gpu_card`` for the dispatch to bind."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[20000, 60000])
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    reserved = _reserved_meta(store, job)
    assert reserved is not None
    assert reserved["gpu_card"] == 1
    assert dict(reserved["slots"]) == {"gpu": 1, "vram_mb": 41024}


def test_second_claim_binds_the_other_card(store: Store) -> None:
    """A pending hold is charged to ITS card: the second claim sees card
    0's effective free already spent and binds card 1; a third has no
    eligible card left."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 3, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[50000, 50000])
    jobs = [
        _queue_job(
            store,
            executor="ssh_node",
            requires={"gpu": 1},
            params={"model": "mace_mp"},
        )
        for _ in range(3)
    ]

    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [jobs[0]]
    store.add_tag(
        jobs[0], Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [jobs[1]]

    first_reserved = _reserved_meta(store, jobs[0])
    second_reserved = _reserved_meta(store, jobs[1])
    assert first_reserved is not None and second_reserved is not None
    assert first_reserved["gpu_card"] == 0
    assert second_reserved["gpu_card"] == 1
    # Both cards now carry a pending 41024 against 50000 free —
    # no card can cover a third claim while the reservations hold.
    store.add_tag(
        jobs[1], Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, jobs[2]) is None


def test_gpu_claim_refused_when_no_single_card_fits(store: Store) -> None:
    """Two cards with 30 GiB free each sum past the 41 GiB need, but no
    card alone can hold the model — the claim must be refused."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[30000, 30000])
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "vram_mb") == 120000
    assert _reserved_meta(store, job) is None


def test_gpu_card_concurrency_cap_spreads_and_refuses(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``PRECIS_GPU_CARD_MAX_JOBS=1``: a card already holding a bound job
    is ineligible even with headroom to spare — the cap spreads claims
    across cards and refuses once every card is full."""
    monkeypatch.setenv("PRECIS_GPU_CARD_MAX_JOBS", "1")
    store.upsert_model_footprint("mace_mp", 4000, source="test")
    _advertise(store, HOST, {"gpu": 3, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_cards_mb=[60000, 60000])
    jobs = [
        _queue_job(
            store,
            executor="ssh_node",
            requires={"gpu": 1},
            params={"model": "mace_mp"},
        )
        for _ in range(3)
    ]

    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [jobs[0]]
    store.add_tag(
        jobs[0], Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    assert [r[0] for r in _claim(store, "ssh_node", limit=1)] == [jobs[1]]

    first_reserved = _reserved_meta(store, jobs[0])
    second_reserved = _reserved_meta(store, jobs[1])
    assert first_reserved is not None and second_reserved is not None
    assert first_reserved["gpu_card"] == 0
    # Card 0 still has 55 GiB effective free, but it's at the job cap.
    assert second_reserved["gpu_card"] == 1
    # Both cards at the cap: veto leaves ledger and reservation untouched.
    store.add_tag(
        jobs[1], Tag.closed("STATUS", "running"), set_by="agent", replace_prefix=True
    )
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, jobs[2]) is None
    assert _free(store, HOST, "vram_mb") == 120000 - 2 * 5024


def test_gpu_claim_without_card_list_stays_unbound(store: Store) -> None:
    """A beat with only the scalar ``vram_free_mb`` keeps the pre-binding
    admission: the claim passes the scalar check and stamps no card."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=80000)
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    reserved = _reserved_meta(store, job)
    assert reserved is not None and "gpu_card" not in reserved


def test_unbound_gpu_hold_counts_against_every_card(store: Store) -> None:
    """A hold with no ``gpu_card`` (admitted before binding existed, or a
    token-only GPU claim) could be on any card — its pending units are
    charged against the card under test."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=50000)
    unbound = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [unbound]
    reserved = _reserved_meta(store, unbound)
    assert reserved is not None and "gpu_card" not in reserved
    # Now the host's beat gains the per-card list, still reporting both
    # cards half free. The unbound 41024 is charged against each card:
    # 50000 - 41024 < 41024 → the bound claim is refused.
    _seed_live_free(store, HOST, vram_free_cards_mb=[50000, 50000])
    bound = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )
    assert _claim(store, "ssh_node") == []
    assert _reserved_meta(store, bound) is None


def test_card_pick_preserves_the_host_vram_reserve(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """td655172 review: the reserve the pick nets is the MEASURED host's,
    stamped on its beat as ``vram_reserve_mb`` — not the claimer's
    ``PRECIS_HOST_VRAM_RESERVE_MB`` env. A claimer (or a ``target_node``
    claim against a remote host) whose env reserves nothing must still
    refuse a card whose raw free covers the units only by spending the
    reservation host's reserve — the claim would otherwise admit into
    headroom an untracked consumer is already eating, skipping the
    reserve-netted scalar check."""
    monkeypatch.setenv("PRECIS_HOST_VRAM_RESERVE_MB", "0")
    store.upsert_model_footprint("mace_mp", 4000, source="test")  # need 5024
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    # An untracked consumer left 6000 raw on card 0. The claimer's env
    # reserves nothing — raw free ≥ the 5024 units would admit — but
    # the beat stamps the measured host's 2048 reserve → effective
    # 3952 < 5024 → refused (admission would have left 976 free,
    # spending 1072 of the host's reserve).
    _seed_live_free(store, HOST, vram_free_cards_mb=[6000, 3000], vram_reserve_mb=2048)
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "vram_mb") == 120000
    assert _reserved_meta(store, job) is None


def test_card_pick_admits_above_the_host_vram_reserve(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reserve costs headroom, not the slot — and it is the beat's
    stamp that applies, not the claimer's env: with the claimer
    reserving MORE than the measured host (9000 vs the stamped 2048),
    the pick still binds the card the host's own reserve leaves
    eligible, and applies that reserve to every card under test."""
    monkeypatch.setenv("PRECIS_HOST_VRAM_RESERVE_MB", "9000")
    store.upsert_model_footprint("mace_mp", 4000, source="test")  # need 5024
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    # Card 0: 7072 - 2048 (the stamped host reserve) = 5024 ≥ 5024 →
    # binds; the claimer's own 9000 would have vetoed both cards. Card
    # 1: 6000 - 2048 = 3952 < 5024 → ineligible even though its raw
    # free would admit.
    _seed_live_free(store, HOST, vram_free_cards_mb=[7072, 6000], vram_reserve_mb=2048)
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    reserved = _reserved_meta(store, job)
    assert reserved is not None and reserved["gpu_card"] == 0


def test_card_list_without_reserve_stamp_stays_unbound(store: Store) -> None:
    """A beat carrying the per-card list but no ``vram_reserve_mb``
    stamp (a heartbeat predating the stamp) can't tell the claim the
    measured host's reserve — guessing from the claimer's env would
    subtract the wrong host's, so the pick stays dark: the claim keeps
    the unbound scalar fallback (reserve-netted at measurement on the
    host) and stamps no ``gpu_card``."""
    store.upsert_model_footprint("mace_mp", 4000, source="test")  # need 5024
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(
        store,
        HOST,
        vram_free_mb=20000,
        vram_free_cards_mb=[60000, 4000],
        vram_reserve_mb=None,
    )
    job = _queue_job(
        store, executor="ssh_node", requires={"gpu": 1}, params={"model": "mace_mp"}
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    reserved = _reserved_meta(store, job)
    assert reserved is not None and "gpu_card" not in reserved
    assert dict(reserved["slots"]) == {"gpu": 1, "vram_mb": 5024}


def test_gpu_claim_without_model_row_reserves_token_only(store: Store) -> None:
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "unlearned_model"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1}
    assert _free(store, HOST, "vram_mb") == 120000


def test_gpu_claim_without_vram_row_falls_back_to_token(store: Store) -> None:
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1}


def test_terminal_refunds_footprint_units(store: Store) -> None:
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    job = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )
    _claim(store, "ssh_node")
    assert _free(store, HOST, "vram_mb") == 120000 - 41024

    set_status(store, job, "succeeded")

    assert _free(store, HOST, "vram_mb") == 120000
    assert _free(store, HOST, "gpu") == 2


# ── CPU admission (science lane) ───────────────────────────────────────

_SIZE = {"name": "atoms", "value": 64}


def test_ssh_node_claim_reserves_predicted_rss(store: Store) -> None:
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 30000})
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    rows = _claim(store, "ssh_node")

    expected = math.ceil(1000.0 * 1.25)
    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"mem_mb": expected}
    assert _free(store, HOST, "mem_mb") == 30000 - expected


def test_ssh_node_claim_refused_when_mem_short(store: Store) -> None:
    """Capacity covers one 1250 MiB reservation; a second finds ``free``
    short of the (unclamped) units and is refused."""
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 2000})
    first = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )
    assert [r[0] for r in _claim(store, "ssh_node")] == [first]
    second = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "mem_mb") == 2000 - 1250
    assert _reserved_slots(store, second) is None


def test_ssh_node_claim_clamps_oversize_mem_to_capacity(store: Store) -> None:
    """gr595542 review: p95 × 1.25 above the host's ``mem_mb`` capacity
    must not strand the job — units clamp to capacity, so it claims on an
    empty host and takes the whole row (a second job then waits)."""
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 1000})
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"mem_mb": 1000}
    assert _free(store, HOST, "mem_mb") == 0

    other = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )
    assert _claim(store, "ssh_node") == []
    assert _reserved_slots(store, other) is None


def test_cpu_admission_dark_below_min_samples(store: Store) -> None:
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=3)
    _advertise(store, HOST, {"mem_mb": 30000})
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) is None


def test_cpu_admission_scoped_to_science_executor(store: Store) -> None:
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 30000})
    job = _queue_job(
        store,
        executor="job_inproc",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    rows = _claim(store, "job_inproc")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) is None
    assert _free(store, HOST, "mem_mb") == 30000


def test_ssh_node_claim_refused_when_live_mem_free_short(store: Store) -> None:
    """td655172: the mem_mb row is a static total; a live free-memory
    measurement below the predicted peak RSS must refuse the claim."""
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 30000})
    _seed_live_free(store, HOST, mem_free_mb=1000)
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "mem_mb") == 30000
    assert _reserved_slots(store, job) is None


def test_ssh_node_claim_admitted_when_live_mem_free_covers(store: Store) -> None:
    _seed_rss_samples(store, job_type="fp_demo", size=_SIZE, rss_mb=1000.0, n=5)
    _advertise(store, HOST, {"mem_mb": 30000})
    _seed_live_free(store, HOST, mem_free_mb=20000)
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="fp_demo",
        params={"atoms": 64},
        size=_SIZE,
    )

    rows = _claim(store, "ssh_node")

    expected = math.ceil(1000.0 * 1.25)
    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"mem_mb": expected}


# ── Model affinity ─────────────────────────────────────────────────────


def _queue_model_pair(store: Store) -> tuple[int, int]:
    """Two same-class gpu jobs with byte-identical queue stamps (a minted
    batch), so ``aged_prio`` ties and affinity is the term that decides;
    ``cold`` is the older ref_id and wins every later tiebreak."""
    from datetime import UTC, datetime, timedelta

    _advertise(store, HOST, {"gpu": 2})
    cold = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_other"},
    )
    warm = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )
    queued_at = datetime.now(UTC) - timedelta(minutes=5)
    _set_queued_at(store, cold, queued_at)
    _set_queued_at(store, warm, queued_at)
    return cold, warm


def test_model_affinity_prefers_warm_model(store: Store) -> None:
    _cold, warm = _queue_model_pair(store)
    _stamp_last_model(store, HOST, "mace_mp")

    rows = _claim(store, "ssh_node", limit=1)

    assert [r[0] for r in rows] == [warm]


def test_model_affinity_ignores_stale_stamp(store: Store) -> None:
    cold, _warm = _queue_model_pair(store)
    _stamp_last_model(store, HOST, "mace_mp", age_s=3600.0)

    rows = _claim(store, "ssh_node", limit=1)

    assert [r[0] for r in rows] == [cold]


def test_claim_stamps_last_model(store: Store) -> None:
    _advertise(store, HOST, {"gpu": 2})
    _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_mp"},
    )

    _claim(store, "ssh_node")

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = %s",
            (f"claim.last_model.{HOST}",),
        ).fetchone()
        at = conn.execute(
            "SELECT value FROM app_state WHERE key = %s",
            (f"claim.last_model_at.{HOST}",),
        ).fetchone()
    assert row is not None and row[0] == "mace_mp"
    assert at is not None and float(at[0]) <= time.time()


# ── Implicit models (td655172) ─────────────────────────────────────────


def test_implicit_model_resolved_for_vram_admission(store: Store) -> None:
    """``struct_relax`` omits ``params.model`` and loads ``mace_mp`` — the
    admission must reserve that model's learned footprint, not sail past
    with a token-only claim."""
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="struct_relax",
        requires={"gpu": 1},
        params={"fidelity": "ml"},
    )

    rows = _claim(store, "ssh_node")

    assert [r[0] for r in rows] == [job]
    assert _reserved_slots(store, job) == {"gpu": 1, "vram_mb": 40000 + 1024}


def test_implicit_model_refused_when_live_vram_free_short(store: Store) -> None:
    store.upsert_model_footprint("mace_mp", 40000, source="test")
    _advertise(store, HOST, {"gpu": 2, "vram_mb": 120000, "vram_card_mb": _CARD_MB})
    _seed_live_free(store, HOST, vram_free_mb=1000)
    job = _queue_job(
        store,
        executor="ssh_node",
        job_type="struct_relax",
        requires={"gpu": 1},
        params={"fidelity": "ml"},
    )

    assert _claim(store, "ssh_node") == []
    assert _free(store, HOST, "gpu") == 2
    assert _reserved_slots(store, job) is None


def test_claim_stamps_implicit_last_model(store: Store) -> None:
    """The stamp records the model the run will ACTUALLY load — the
    resolved default, not the absent params key."""
    _advertise(store, HOST, {"gpu": 2})
    _queue_job(
        store,
        executor="ssh_node",
        job_type="struct_relax",
        requires={"gpu": 1},
        params={"fidelity": "ml"},
    )

    _claim(store, "ssh_node")

    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT value FROM app_state WHERE key = %s",
            (f"claim.last_model.{HOST}",),
        ).fetchone()
    assert row is not None and row[0] == "mace_mp"


def test_model_affinity_prefers_implicit_warm_model(store: Store) -> None:
    """An implicit-default job (struct_relax, no params.model) steers the
    re-rank exactly like an explicit one: with the fairness keys tied and
    the host stamped mace_mp, it claims ahead of the cold explicit job."""
    from datetime import UTC, datetime, timedelta

    _advertise(store, HOST, {"gpu": 2})
    cold = _queue_job(
        store,
        executor="ssh_node",
        requires={"gpu": 1},
        params={"model": "mace_other"},
    )
    warm = _queue_job(
        store,
        executor="ssh_node",
        job_type="struct_relax",
        requires={"gpu": 1},
        params={"fidelity": "ml"},
    )
    queued_at = datetime.now(UTC) - timedelta(minutes=5)
    _set_queued_at(store, cold, queued_at)
    _set_queued_at(store, warm, queued_at)
    _stamp_last_model(store, HOST, "mace_mp")

    rows = _claim(store, "ssh_node", limit=1)

    assert [r[0] for r in rows] == [warm]
