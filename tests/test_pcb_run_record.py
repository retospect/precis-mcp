"""design-rerun-pull S1 (gr512900): terminal ``pcb_place`` jobs carry a
``meta.run_record``; ``_enqueue_op`` looks the resubmit's digest up
against terminal records and serves the stored result verbatim — no
anneal minted — or replays a refused run's verdict as a refusal.

Drives the REAL path end to end: ``put(args={'op':'place'})`` mints the
job, ``run_job_inproc_pass`` drains it, and a second ``put`` either
replays or enqueues — the fake-ctx tests in ``tests/workers/
test_pcb_place.py`` cover the anneal internals.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.job import JobHandler
from precis.handlers.pcb import PcbHandler
from precis.pcb import run_record
from precis.store import Store
from precis.workers.executors.job_inproc import run_job_inproc_pass
from precis.workers.job_types import pcb_place
from precis.workers.sweeper import _gc_transcripts
from tests._pcb_outline import closed_outline

pytestmark = pytest.mark.db

_CROSSED: dict[str, Any] = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [
        {"name": "N1", "class": "signal"},
        {"name": "N2", "class": "signal"},
    ],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
    "features": [closed_outline()],
}


def _handler(store: Store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _seed(store: Store, slug: str, args: dict[str, Any]) -> int:
    _handler(store).put(id=slug, args=args)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def _poses(store: Store, ref_id: int) -> dict[str, tuple[Any, ...]]:
    return {
        i["refdes"]: (i["x"], i["y"], i["rot"])
        for i in store.pcb_graph(ref_id)["instances"]
    }


def _n_jobs(store: Store, ref_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'job' AND parent_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _job_meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'job' "
            "AND parent_id = %s ORDER BY ref_id",
            (ref_id,),
        ).fetchall()
    assert len(rows) == 1
    return {"ref_id": int(rows[0][0]), **(rows[0][1] or {})}


def _place(store: Store, slug: str, ref_id: int, **extra: Any) -> str:
    resp = _handler(store).put(
        id=slug, args={"op": "place", "iters": 2000, "seed": 1, **extra}
    )
    return resp.body


def _run_once(store: Store, slug: str, ref_id: int) -> str:
    """put + drain; returns the enqueue body."""
    body = _place(store, slug, ref_id)
    assert run_job_inproc_pass(store, limit=1)["claimed"] == 1
    return body


def test_committed_run_records_pose_map_and_digests(store: Store) -> None:
    ref_id = _seed(store, "rr-rec", _CROSSED)
    _run_once(store, "rr-rec", ref_id)
    meta = _job_meta(store, ref_id)
    rec = meta.get("run_record")
    assert rec is not None
    assert rec["schema"] == run_record.RUN_RECORD_SCHEMA
    assert rec["code_version"] == run_record.RUN_RECORD_CODE_VERSION
    assert rec["outcome"] == "committed"
    assert rec["op"] == "place" and rec["placer"] == "anneal"
    assert rec["input_digest"] and rec["result_digest"]
    assert rec["input_digest"] != rec["result_digest"]
    written = _poses(store, ref_id)
    assert {r: (p["x"], p["y"], p["rot"]) for r, p in rec["poses"].items()} == written
    assert all(p["side"] in ("top", "bottom") for p in rec["poses"].values())


def test_unchanged_resubmit_replays_no_new_job(store: Store) -> None:
    ref_id = _seed(store, "rr-hit", _CROSSED)
    _run_once(store, "rr-hit", ref_id)
    job_id = _job_meta(store, ref_id)["ref_id"]
    placed = _poses(store, ref_id)
    body = _place(store, "rr-hit", ref_id)
    assert f"replayed run {job_id}" in body
    assert _n_jobs(store, ref_id) == 1, "a resubmit must mint no second anneal"
    assert _poses(store, ref_id) == placed, "replay must be bit-identical"


def test_rewound_board_hits_input_digest_and_reapplies(store: Store) -> None:
    """The stored-and-recovered case: poses rolled back to the pre-run
    state resubmit as a NEW digest — the input side of the record — and
    the stored result is written back verbatim."""
    ref_id = _seed(store, "rr-rewind", _CROSSED)
    before = _poses(store, ref_id)
    _run_once(store, "rr-rewind", ref_id)
    placed = _poses(store, ref_id)
    assert placed != before
    store.pcb_set_pose(ref_id, before)
    body = _place(store, "rr-rewind", ref_id)
    assert "replayed run" in body
    assert _n_jobs(store, ref_id) == 1
    assert _poses(store, ref_id) == placed


def test_unreadable_record_is_a_miss_not_a_failure(store: Store) -> None:
    ref_id = _seed(store, "rr-corrupt", _CROSSED)
    _run_once(store, "rr-corrupt", ref_id)
    job_id = _job_meta(store, ref_id)["ref_id"]
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, '{run_record}', %s::jsonb) "
            "WHERE ref_id = %s",
            ('{"schema": "precis.pcb-run/999", "code_version": 99}', job_id),
        )
        conn.commit()
    body = _place(store, "rr-corrupt", ref_id)
    assert "replayed run" not in body
    assert _n_jobs(store, ref_id) == 2, (
        "an unreadable record must cost the hit, not the run"
    )


def test_placer_code_version_mismatch_is_a_miss(store: Store) -> None:
    """The placer's OWN code version rides the record (the anneal's
    ``ANNEAL_CODE_VERSION``, topo's ``CODE_VERSION``) — a stored run made
    by different placer code is skipped, never replayed."""
    ref_id = _seed(store, "rr-placer-ver", _CROSSED)
    _run_once(store, "rr-placer-ver", ref_id)
    meta = _job_meta(store, ref_id)
    rec = meta["run_record"]
    assert rec["placer_code_version"] == run_record.placer_code_version("anneal")
    job_id = meta["ref_id"]
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = jsonb_set(meta, "
            "'{run_record,placer_code_version}', '0') WHERE ref_id = %s",
            (job_id,),
        )
        conn.commit()
    body = _place(store, "rr-placer-ver", ref_id)
    assert "replayed run" not in body
    assert _n_jobs(store, ref_id) == 2, (
        "a placer-version mismatch must cost the hit, not the run"
    )


def test_footprint_swap_is_a_miss(store: Store) -> None:
    """``content_hash`` does not cover part/footprint/value, so the
    record keys on a footprint digest alongside it: swapping a footprint
    on the same refdes must NOT replay the old run's poses."""
    ref_id = _seed(store, "rr-fp", _CROSSED)
    _run_once(store, "rr-fp", ref_id)
    rec = _job_meta(store, ref_id)["run_record"]
    assert rec["footprint_digest"] == run_record.footprint_fingerprint(
        store.pcb_graph(ref_id)
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_components c SET footprint = 'OTHER-0805' "
            "FROM pcb_instances i "
            "WHERE i.component_id = c.component_id AND c.ref_id = %s "
            "  AND i.refdes = 'A' AND i.retired_at IS NULL "
            "  AND c.retired_at IS NULL",
            (ref_id,),
        )
        conn.commit()
    body = _place(store, "rr-fp", ref_id)
    assert "replayed run" not in body
    assert _n_jobs(store, ref_id) == 2, (
        "a footprint swap must cost the hit, not replay the old poses"
    )


def test_refused_run_replays_as_a_refusal(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    ref_id = _seed(store, "rr-refused", _CROSSED)
    real_optimize = pcb_place.optimize

    def _collapse(ir: Any, config: Any) -> Any:
        result = real_optimize(ir, config)
        ir.inst_x[:] = ir.inst_x[0]
        ir.inst_y[:] = ir.inst_y[0]
        return result

    monkeypatch.setattr(pcb_place, "optimize", _collapse)
    _run_once(store, "rr-refused", ref_id)
    rec = _job_meta(store, ref_id)["run_record"]
    assert rec["outcome"] == "refused" and rec["result_digest"] is None
    before = _poses(store, ref_id)
    with pytest.raises(BadInput, match="replaying the verdict"):
        _place(store, "rr-refused", ref_id)
    assert _n_jobs(store, ref_id) == 1, "a refusal never mints a retry"
    assert _poses(store, ref_id) == before, "a refusal writes no poses"


def test_placement_only_flag_and_replay_text(store: Store) -> None:
    """A run with no route stage (the anneal always; topo without
    ``route=True``) records placement-only — replay says so."""
    ref_id = _seed(store, "rr-ponly", _CROSSED)
    _run_once(store, "rr-ponly", ref_id)
    rec = _job_meta(store, ref_id)["run_record"]
    assert rec["placement_only"] is True
    body = _place(store, "rr-ponly", ref_id)
    assert "placement replayed; route not replayed" in body


def test_topo_route_failure_is_not_a_hit(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A topo run submitted with ``route=True`` is recorded ONLY once
    its route stage succeeds — a raised route fails the job but leaves
    no replayable record, so the resubmit mints a fresh run."""

    def _boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("route boom")

    monkeypatch.setattr(pcb_place, "_topo_route", _boom)
    ref_id = _seed(store, "rr-rfail", _CROSSED)
    _place(store, "rr-rfail", ref_id, placer="topo", route=True, iters=50)
    assert run_job_inproc_pass(store, limit=1)["claimed"] == 1
    meta = _job_meta(store, ref_id)
    assert meta.get("run_record") is None, (
        "a route-failed run must not be recorded as a hit"
    )
    _place(store, "rr-rfail", ref_id, placer="topo", route=True, iters=50)
    assert _n_jobs(store, ref_id) == 2


def test_topo_route_success_records_after_route(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``route=True`` run whose route stage succeeds lands the record
    — flagged NOT placement-only, so replay names the routed copper it
    did not restore rather than the placement-only caveat."""

    def _ok(*_a: Any, **_k: Any) -> Any:
        return "topo-route: ok\n", {"nets": 2}

    monkeypatch.setattr(pcb_place, "_topo_route", _ok)
    ref_id = _seed(store, "rr-rok", _CROSSED)
    _place(store, "rr-rok", ref_id, placer="topo", route=True, iters=50)
    assert run_job_inproc_pass(store, limit=1)["claimed"] == 1
    rec = _job_meta(store, ref_id)["run_record"]
    assert rec["placement_only"] is False
    body = _place(store, "rr-rok", ref_id, placer="topo", route=True, iters=50)
    assert "replayed run" in body
    assert "placement replayed; routed copper not restored, re-run route" in body
    assert _n_jobs(store, ref_id) == 1


def test_run_record_job_is_delete_and_gc_proof(store: Store) -> None:
    ref_id = _seed(store, "rr-gc", _CROSSED)
    _run_once(store, "rr-gc", ref_id)
    meta = _job_meta(store, ref_id)
    job_id = meta["ref_id"]
    with pytest.raises(BadInput, match="run record"):
        JobHandler(hub=Hub(store=store)).delete(id=job_id)
    # The transcript GC strips only its own key families — run_record
    # must ride through untouched (backdate the row past the window).
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET meta = meta || %s::jsonb, "
            "created_at = now() - interval '400 days' WHERE ref_id = %s",
            ('{"transcript": {"x": 1}, "assembled_context": {"y": 2}}', job_id),
        )
        conn.commit()
    assert _gc_transcripts(store) >= 1
    after = _job_meta(store, ref_id)
    assert "transcript" not in after and "assembled_context" not in after
    assert after["run_record"]["schema"] == run_record.RUN_RECORD_SCHEMA
