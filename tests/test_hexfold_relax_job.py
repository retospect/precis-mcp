"""``hexfold_relax`` job_type — the H3 relax pipeline as a compute-group
job (docs/backlog/hexfold-nanoreactor-tube.md).

Proved without a cluster, the way ``test_struct_relax_job`` does: a fake
``DispatchContext`` drives the dispatch against a generated structure,
the ``xtb`` rung's calculator is stubbed (tblite is absent on the Mac —
same pattern as ``test_structure_relax_xtb``), and the whole-block
polish runs on the always-available ``emt`` rung.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.store.types import Tag
from precis.workers.executors import ssh_node
from precis.workers.executors._common import set_status
from precis.workers.executors._context import DispatchContext
from precis_se import hexfold_relax_job as hexfold_relax
from precis_se.handler import SeHandler
from tests import _child_compute_targets as targets


def test_runner_honours_hexfold_relax_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """The type's host-local cap, not an absent resources field, sizes both."""
    monkeypatch.delenv("PRECIS_DFT_RELAX_TIMEOUT_S", raising=False)
    meta: dict[str, Any] = {"params": {}, "job_type": "hexfold_relax"}
    spec = hexfold_relax.SPEC
    assert ssh_node._wall_bound_seconds(meta) == ssh_node._LEASE_FLOOR_S
    assert ssh_node._wall_bound_seconds(meta, spec) == 14400 + ssh_node._LEASE_MARGIN_S
    assert ssh_node._lease_seconds(meta, spec) == ssh_node._wall_bound_seconds(
        meta, spec
    )
    monkeypatch.setenv("PRECIS_DFT_RELAX_TIMEOUT_S", "18000")
    assert ssh_node._wall_bound_seconds(meta, spec) == 18000 + ssh_node._LEASE_MARGIN_S


def _tunnel_ops() -> dict[str, Any]:
    return {
        "ops": [
            {
                "op": "generate",
                "generator": "hexfold_scene",
                "name": "t",
                "params": {
                    "tube": [4, 3],
                    "features": [
                        {
                            "name": "t",
                            "type": "tunnel-reservoirs",
                            "sheet": [24, 24],
                        }
                    ],
                    "k_tether": 1.0,
                },
            }
        ]
    }


def _structure_ref(store: Any) -> Any:
    ref = store.get_ref(kind="structure", id="hx-tun-t")
    assert ref is not None
    return ref


def _fake_ctx(
    store: Any,
    params: dict[str, Any],
    is_cancel_requested: Callable[[], bool] = lambda: False,
) -> tuple[DispatchContext, list]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=999,
        title="hexfold_relax",
        meta={"params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=is_cancel_requested,
    )
    return ctx, events


@pytest.mark.slow
def test_dispatch_relaxes_and_stores_strain(store: Any, monkeypatch) -> None:
    """End to end: generate a rail tunnel, dispatch the job with an EMT
    polish + stubbed-xtb fragment pass — the scene gains a version, the
    generated record carries before/after strain, and the compute passes
    land in struct_runs. The compute runs in the supervised child, which
    installs the xtb stub itself (a parent monkeypatch cannot reach it)."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_emt_xtb)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    params = {
        "structure_ref_id": ref.id,
        "polish": "emt",
        "fragment": "xtb",
        "buffer_A": 5.0,
        "core_A": 2.0,
        "steps": 30,
    }
    ctx, events = _fake_ctx(store, params)
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    assert not [p for k, p in events if k == "fail"]
    assert ("status", "succeeded") in events

    ref2 = _structure_ref(store)
    assert int(ref2.meta["version"]) == 2
    record = ref2.meta["generated"]["relax"]
    assert record["job_id"] == 999
    assert record["polish"]["rung"] == "emt"
    assert len(record["fragments"]) == 2
    for frag in record["fragments"]:
        assert frag["rung"] == "xtb" and frag["h_caps"] > 0 and frag["frozen"] > 0
    before, after = record["strain"]["before"], record["strain"]["after"]
    assert {f["code"] for f in before} >= {"junction.strain"}
    assert {f["code"] for f in after} >= {"junction.strain"}
    assert len(before) == len(after)

    runs = store.structure_runs(ref.id)
    assert [r["fidelity"] for r in runs][:3] == ["xtb", "xtb", "emt"]
    assert all(r["model"] == "gfn2-xtb" for r in runs[:2])
    assert ref2.meta["last_relax"]["rung"] == "hexfold:emt+xtb"


def test_validate_submit(store: Any) -> None:
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    assert (
        hexfold_relax._validate_submit(store, params={"structure_ref_id": ref.id})
        is None
    )
    assert "structure_ref_id" in (
        hexfold_relax._validate_submit(store, params={}) or ""
    )
    assert "no structure ref" in (
        hexfold_relax._validate_submit(store, params={"structure_ref_id": 424242}) or ""
    )
    assert "polish" in (
        hexfold_relax._validate_submit(
            store, params={"structure_ref_id": ref.id, "polish": "gpaw"}
        )
        or ""
    )


def _plain_structure(store: Any) -> Any:
    """A one-atom structure with no junction bookkeeping."""
    from precis.structure.cell import Cell
    from precis.structure.scene import Atom as StructAtom
    from precis.structure.scene import Scene as StructScene

    store.structure_save(
        slug="plain",
        title="plain",
        scene=StructScene(
            cell=Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)),
            atoms={"aC1": StructAtom("aC1", "C", np.zeros(3))},
        ),
        version=1,
        card_text="plain.",
    )
    return store.get_ref(kind="structure", id="plain")


def test_dispatch_without_bookkeeping_fails(store: Any) -> None:
    """A structure not minted with the H3 strain record gets a clean
    params-class failure, not a crash."""
    ref = _plain_structure(store)
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert "junctions" in fails[0]["reason"]


def test_dispatch_missing_backend_is_infra(store: Any, monkeypatch) -> None:
    """No tblite on the claiming node → an honest infra failure naming
    the extra, never different physics silently."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_no_tblite)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    ctx, events = _fake_ctx(
        store,
        {
            "structure_ref_id": ref.id,
            "polish": "emt",
            "fragment": "xtb",
            "steps": 5,
        },
    )
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "infra"
    assert ("status", "succeeded") not in events


# ── killable compute (gr506395): a fake long-running child ──────────


def _sleeping_child(monkeypatch: pytest.MonkeyPatch, cap: float = 1.0) -> None:
    """Swap the relax for a child that never returns, with a short cap and
    fast supervisor cadence."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.sleep_forever)
    monkeypatch.setattr(hexfold_relax, "_relax_timeout_s", lambda: cap)
    monkeypatch.setattr(hexfold_relax, "_CHILD_POLL_S", 0.1)
    monkeypatch.setattr(hexfold_relax, "_CHILD_GRACE_S", 5.0)


def _gone(pid: int, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def _child_handles(events: list[tuple[str, Any]]) -> list[dict[str, Any]]:
    return [
        p["compute_child"] for k, p in events if k == "meta" and "compute_child" in p
    ]


def test_timeout_kills_the_relax_child(store: Any, monkeypatch) -> None:
    _sleeping_child(monkeypatch)
    ref = _plain_structure(store)
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert fails[0]["failure_class"] == "timeout"
    assert "killed" in fails[0]["reason"]
    assert ("status", "succeeded") not in events
    start, end = _child_handles(events)
    assert start["pid"] == end["pid"]
    assert end["stopped"] == "timeout" and end["kill_verified"] is True
    assert _gone(start["pid"])


def test_cancel_requested_kills_the_relax_child(store: Any, monkeypatch) -> None:
    _sleeping_child(monkeypatch, cap=600)
    checks: list[int] = []

    def _cancel() -> bool:
        checks.append(1)
        return len(checks) > 2

    ref = _plain_structure(store)
    ctx, events = _fake_ctx(
        store, {"structure_ref_id": ref.id, "fragment": "none"}, _cancel
    )
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    assert ("status", "cancelled") in events
    assert not [p for k, p in events if k == "fail"]
    end = _child_handles(events)[-1]
    assert end["stopped"] == "cancel" and end["kill_verified"] is True
    assert _gone(end["pid"])


def test_cpu_fallback_lands_on_meta_as_a_warning(store: Any) -> None:
    ctx, events = _fake_ctx(store, {})
    report = {"device": "cpu", "warning": "MLIP mace_mp runs on CPU although x"}
    hexfold_relax._on_child_event(ctx, "ml_device", report)
    assert ("meta", {"ml_device": report}) in events
    assert any(k == "job_event" and "runs on CPU" in t for k, t in events)


# ── the same through the real ssh_node pass and a DB job row ─────────


def _mk_job(store: Any, structure_ref_id: int) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="hexfold_relax",
        meta={
            "executor": "ssh_node",
            "job_type": "hexfold_relax",
            "params": {"structure_ref_id": structure_ref_id, "fragment": "none"},
        },
    )
    store.add_tag(ref.id, Tag.parse_strict("STATUS:queued"), set_by="agent")
    return int(ref.id)


def _job_state(store: Any, ref_id: int) -> tuple[str | None, dict[str, Any]]:
    with store.pool.connection() as conn:
        status = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref_id,),
        ).fetchone()
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    return (status[0] if status else None), dict(meta[0] or {})


def _once_child_runs(
    store: Any, ref_id: int, act: Callable[[], None]
) -> threading.Thread:
    """Run ``act`` from a side thread once the job's meta names its child."""

    def _wait() -> None:
        end = time.monotonic() + 120
        while time.monotonic() < end:
            if "compute_child" in _job_state(store, ref_id)[1]:
                act()
                return
            time.sleep(0.05)

    thread = threading.Thread(target=_wait, daemon=True)
    thread.start()
    return thread


def _ssh_node_hexfold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_NODE", raising=False)
    monkeypatch.setattr(ssh_node, "get_job_type", lambda _name: hexfold_relax.SPEC)


@pytest.mark.db
def test_ssh_node_timeout_fails_the_job_with_no_live_child(
    store: Any, monkeypatch
) -> None:
    _sleeping_child(monkeypatch)
    _ssh_node_hexfold(monkeypatch)
    rid = _mk_job(store, _plain_structure(store).id)

    ssh_node.run_ssh_node_pass(store, limit=1)

    status, meta = _job_state(store, rid)
    assert status == "failed"
    assert meta["failure_class"] == "timeout"
    child = meta["compute_child"]
    assert child["stopped"] == "timeout" and child["kill_verified"] is True
    assert _gone(int(child["pid"]))


@pytest.mark.db
def test_ssh_node_cancel_requested_cancels_with_no_live_child(
    store: Any, monkeypatch
) -> None:
    _sleeping_child(monkeypatch, cap=600)
    _ssh_node_hexfold(monkeypatch)
    rid = _mk_job(store, _plain_structure(store).id)
    side = _once_child_runs(
        store, rid, lambda: set_status(store, rid, "cancel_requested")
    )

    ssh_node.run_ssh_node_pass(store, limit=1)
    side.join(5)

    status, meta = _job_state(store, rid)
    assert status == "cancelled"
    child = meta["compute_child"]
    assert child["stopped"] == "cancel" and child["kill_verified"] is True
    assert _gone(int(child["pid"]))


@pytest.mark.db
def test_ssh_node_operator_kill_fails_with_no_live_child(
    store: Any, monkeypatch
) -> None:
    _sleeping_child(monkeypatch, cap=600)
    _ssh_node_hexfold(monkeypatch)
    rid = _mk_job(store, _plain_structure(store).id)

    def _kill() -> None:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || "
                """'{"kill_requested": {"note": "test"}}'::jsonb """
                "WHERE ref_id = %s",
                (rid,),
            )
            conn.commit()

    side = _once_child_runs(store, rid, _kill)
    ssh_node.run_ssh_node_pass(store, limit=1)
    side.join(5)

    status, meta = _job_state(store, rid)
    assert status == "failed"
    child = meta["compute_child"]
    assert child["stopped"] == "kill" and child["kill_verified"] is True
    assert _gone(int(child["pid"]))


# ── step checkpoints (gr508493) ────────────────────────────────────


@pytest.fixture(autouse=True)
def _tmp_ckpt_root(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test's checkpoint root is a throwaway dir — never the dev
    host's real ``~/.cache/precis/hexfold-relax``."""
    monkeypatch.setenv("PRECIS_RELAX_CKPT_DIR", str(tmp_path))


def _write_ckpt(
    store: Any,
    positions_scene: Any,
    *,
    job_id: int = 999,
    base_version: int = 1,
    stage: str = "polish",
    fragment: Any = None,
    stage_steps: int = 7,
    polish: Any = None,
    fragments: list | None = None,
) -> tuple[Any, dict[str, Any]]:
    """A real checkpoint file + the small meta pointer that names it —
    written through the same _CkptWriter the child uses."""
    writer = hexfold_relax._CkptWriter(
        str(hexfold_relax._ckpt_dir(job_id)),
        job_id=job_id,
        base_version=base_version,
    )
    writer.write(
        {
            "v": 1,
            "stage": stage,
            "fragment": fragment,
            "stage_steps": stage_steps,
            "curve": [0.9 - 0.05 * i for i in range(stage_steps)],
            "positions": {
                la: [round(float(x), 6) for x in a.frac]
                for la, a in positions_scene.atoms.items()
            },
            "polish": polish,
            "fragments": fragments or [],
            "before": [],
        }
    )
    path = writer.path
    os.close(writer._lock_fd)  # release the flock for the child under test
    ptr = {
        "v": 1,
        "path": str(path),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "stage": stage,
        "fragment": fragment,
        "step": stage_steps,
        "n": 1,
        "base_version": base_version,
        "job_id": job_id,
    }
    return path, ptr


def test_timeout_writes_checkpoint_back_as_version(
    store: Any, monkeypatch, tmp_path
) -> None:
    """A child that checkpointed then stalled: the timeout kill reads
    the file back and saves it as the structure's next version with
    converged=false, the step count, and a resume seed."""
    monkeypatch.setattr(
        hexfold_relax, "_relax_child", targets.emit_checkpoint_then_sleep
    )
    # Cap must outlast child spawn (~seconds on a loaded box) + the
    # first checkpoint write — 1s was beaten by spawn on CI.
    monkeypatch.setattr(hexfold_relax, "_relax_timeout_s", lambda: 8.0)
    monkeypatch.setattr(hexfold_relax, "_CHILD_POLL_S", 0.1)
    monkeypatch.setattr(hexfold_relax, "_CHILD_GRACE_S", 5.0)
    ref = _plain_structure(store)
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and fails[0]["failure_class"] == "timeout"
    assert "version 2" in fails[0]["reason"] and "step 7" in fails[0]["reason"]
    # The meta writes carry only the small pointer — never the geometry.
    ptrs = [
        p["relax_checkpoint"]
        for k, p in events
        if k == "meta" and p.get("relax_checkpoint")
    ]
    assert ptrs and "positions" not in ptrs[-1]
    assert "path" in ptrs[-1] and "sha256" in ptrs[-1]
    # After the write-back the pointer is cleared and the file gone.
    assert any(k == "meta" and p.get("relax_checkpoint") is None for k, p in events)
    assert not (tmp_path / "job-999" / "checkpoint.json").exists()
    assert any(
        k == "job_summary" and "checkpoint written back as version 2" in t
        for k, t in events
    )
    ref2 = store.get_ref(kind="structure", id=ref.id)
    assert int(ref2.meta["version"]) == 2
    record = ref2.meta["generated"]["relax"]
    assert record["interrupted"]["reason"] == "timeout"
    assert record["interrupted"]["steps"] == 7
    assert record["polish"]["converged"] is False
    assert record["resume"]["version"] == 2
    assert ref2.meta["last_relax"]["converged"] is False
    run = store.structure_runs(ref.id)[0]
    assert run["status"] == "interrupted" and run["n_steps"] == 7


def test_cancel_writes_checkpoint_back_as_version(
    store: Any, monkeypatch, tmp_path
) -> None:
    """Cancel gets the same write-back as timeout (review item 7)."""
    monkeypatch.setattr(
        hexfold_relax, "_relax_child", targets.emit_checkpoint_then_sleep
    )
    monkeypatch.setattr(hexfold_relax, "_relax_timeout_s", lambda: 600.0)
    monkeypatch.setattr(hexfold_relax, "_CHILD_POLL_S", 0.1)
    monkeypatch.setattr(hexfold_relax, "_CHILD_GRACE_S", 5.0)
    ref = _plain_structure(store)
    ckpt_file = hexfold_relax._ckpt_dir(999) / "checkpoint.json"
    ctx, events = _fake_ctx(
        store,
        {"structure_ref_id": ref.id, "fragment": "none"},
        # Cancel only once the child has written a checkpoint — an
        # earlier cancel is allowed to produce no write-back.
        is_cancel_requested=lambda: ckpt_file.exists(),
    )
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    assert ("status", "cancelled") in events
    assert not [p for k, p in events if k == "fail"]
    ref2 = store.get_ref(kind="structure", id=ref.id)
    assert int(ref2.meta["version"]) == 2
    record = ref2.meta["generated"]["relax"]
    assert record["interrupted"]["reason"] == "cancel"
    assert record["interrupted"]["steps"] == 7


def test_operator_kill_writes_checkpoint_back_as_version(
    store: Any, monkeypatch, tmp_path
) -> None:
    """An operator kill writes the checkpoint back too, and the job
    failure names both the kill and the saved version."""
    monkeypatch.setattr(
        hexfold_relax, "_relax_child", targets.emit_checkpoint_then_sleep
    )
    monkeypatch.setattr(hexfold_relax, "_relax_timeout_s", lambda: 600.0)
    monkeypatch.setattr(hexfold_relax, "_CHILD_POLL_S", 0.1)
    monkeypatch.setattr(hexfold_relax, "_CHILD_GRACE_S", 5.0)
    ref = _plain_structure(store)
    ckpt_file = hexfold_relax._ckpt_dir(999) / "checkpoint.json"
    monkeypatch.setattr(
        hexfold_relax,
        "job_stop_reason",
        # Kill only once the child has written a checkpoint.
        lambda ctx: "kill" if ckpt_file.exists() else None,
    )
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1 and "killed by operator" in fails[0]["reason"]
    assert "version 2" in fails[0]["reason"]
    ref2 = store.get_ref(kind="structure", id=ref.id)
    assert int(ref2.meta["version"]) == 2
    assert ref2.meta["generated"]["relax"]["interrupted"]["reason"] == "kill"


def test_no_checkpoint_no_writeback(store: Any, monkeypatch, tmp_path) -> None:
    """A stalled child that never checkpointed fails exactly as before —
    no phantom version."""
    _sleeping_child(monkeypatch)
    ref = _plain_structure(store)
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    assert len([p for k, p in events if k == "fail"]) == 1
    assert int(store.get_ref(kind="structure", id=ref.id).meta["version"]) == 1


def test_writeback_uses_latest_version_not_loaded(
    store: Any, monkeypatch, tmp_path
) -> None:
    """on_version < live: the checkpoint saves at live+1 — a stale job
    relaxing an older design never collides with versions saved since
    (review item 3)."""
    ref = _plain_structure(store)
    scene, _ = store.structure_load(ref.id)
    ctx, _events = _fake_ctx(store, {})
    checkpoint = {
        "v": 1,
        "base_version": 1,
        "stage": "polish",
        "fragment": None,
        "stage_steps": 5,
        "curve": [0.5] * 5,
        "positions": {
            la: [round(float(x), 6) for x in a.frac] for la, a in scene.atoms.items()
        },
        "polish": None,
        "fragments": [],
        "before": [],
    }
    kw = dict(
        structure_ref_id=ref.id,
        version_loaded=1,
        polish="emt",
        fragment="none",
        checkpoint=checkpoint,
        reason="timeout",
    )
    assert hexfold_relax._write_checkpoint_version(ctx, **kw) == (2, 5)
    # A second write-back off the same base lands on v3, not v2 again.
    assert hexfold_relax._write_checkpoint_version(ctx, **kw) == (3, 5)
    assert int(store.get_ref(kind="structure", id=ref.id).meta["version"]) == 3


def test_missing_checkpoint_file_starts_fresh(
    store: Any, monkeypatch, tmp_path
) -> None:
    """The pointer names a file that is not on this node — the job
    warns and runs from scratch, never fails (review item 1)."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_emt_xtb)
    ref = _plain_structure(store)
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    ctx.meta["relax_checkpoint"] = {
        "v": 1,
        "path": str(tmp_path / "job-999" / "checkpoint.json"),
        "sha256": "0" * 64,
        "stage": "polish",
        "step": 4,
        "base_version": 1,
        "job_id": 999,
    }
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    # plain structure has no junctions → the child dies on the
    # ValueError (infra class); the point is the resume path yielded
    # None and warned, not that it crashed.
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert not any(
        k == "job_event" and "resuming from checkpoint" in t for k, t in events
    )


@pytest.mark.slow
def test_same_job_checkpoint_resumes(store: Any, monkeypatch, tmp_path) -> None:
    """The reclaim path: meta pointer → node-local file → the job
    resumes — polish is never re-run, fragments continue."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_emt_xtb)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    scene, _ = store.structure_load(ref.id)
    _path, ptr = _write_ckpt(
        store,
        scene,
        stage="fragment",
        fragment="low",
        stage_steps=3,
        polish={
            "rung": "emt",
            "model": "emt",
            "converged": True,
            "n_steps": 17,
            "max_disp": 0.01,
            "energy": -5.0,
            "max_force": 0.01,
            "curve": [0.9] * 17,
        },
    )
    ctx, events = _fake_ctx(
        store,
        {
            "structure_ref_id": ref.id,
            "polish": "emt",
            "fragment": "xtb",
            "buffer_A": 5.0,
            "core_A": 2.0,
            "steps": 8,
        },
    )
    ctx.meta["relax_checkpoint"] = ptr
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)

    assert not [p for k, p in events if k == "fail"]
    assert ("status", "succeeded") in events
    assert any(k == "job_event" and "resuming from checkpoint" in t for k, t in events)
    record = _structure_ref(store).meta["generated"]["relax"]
    # The recorded polish is the checkpoint's envelope — a re-run would
    # report different step counts/energy.
    assert record["polish"]["n_steps"] == 17
    assert record["polish"]["energy"] == -5.0
    assert "interrupted" not in record


@pytest.mark.slow
def test_restep_job_resumes_from_checkpoint_version(
    store: Any, monkeypatch, tmp_path
) -> None:
    """A job minted with on_version=<checkpoint version> resumes from the
    generated.relax resume seed — geometry already saved, no positions."""
    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_emt_xtb)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    ctx, events = _fake_ctx(store, {})
    scene, _ = store.structure_load(ref.id)
    checkpoint = {
        "v": 1,
        "base_version": 1,
        "stage": "fragment",
        "fragment": "low",
        "stage_steps": 2,
        "curve": [0.5, 0.4],
        "positions": {
            la: [round(float(x), 6) for x in a.frac] for la, a in scene.atoms.items()
        },
        "polish": {
            "rung": "emt",
            "model": "emt",
            "converged": True,
            "n_steps": 11,
            "max_disp": 0.02,
            "energy": -7.0,
            "max_force": 0.02,
            "curve": [0.9] * 11,
        },
        "fragments": [],
        "before": [],
    }
    saved = hexfold_relax._write_checkpoint_version(
        ctx,
        structure_ref_id=ref.id,
        version_loaded=1,
        polish="emt",
        fragment="xtb",
        checkpoint=checkpoint,
        reason="timeout",
    )
    assert saved == (2, 13)

    ctx2, events2 = _fake_ctx(
        store,
        {
            "structure_ref_id": ref.id,
            "on_version": 2,
            "polish": "emt",
            "fragment": "xtb",
            "buffer_A": 5.0,
            "core_A": 2.0,
            "steps": 8,
        },
    )
    hexfold_relax._dispatch(ctx2, hexfold_relax.SPEC)
    assert not [p for k, p in events2 if k == "fail"]
    assert any(k == "job_event" and "resuming from checkpoint" in t for k, t in events2)
    record = _structure_ref(store).meta["generated"]["relax"]
    assert record["polish"]["n_steps"] == 11
    assert record["polish"]["energy"] == -7.0


@pytest.mark.slow
def test_reclaim_resumes_same_job(store: Any, monkeypatch, tmp_path) -> None:
    """Kill a REAL child after several fragment steps, then re-dispatch
    the SAME job through its meta pointer (the reclaim path, not a
    re-step): it resumes, the finished polish is never re-run, and the
    final geometry lands within fmax of an uninterrupted run
    (review item C)."""
    from precis.workers.child_compute import ChildStopped, run_in_child

    monkeypatch.setattr(hexfold_relax, "_relax_child", targets.relax_child_emt_xtb)
    monkeypatch.setattr(hexfold_relax, "_CHILD_POLL_S", 0.1)
    monkeypatch.setattr(hexfold_relax, "_CHILD_GRACE_S", 5.0)
    se = SeHandler(hub=Hub(store=store))
    se.put(id="hx-tun", text=json.dumps(_tunnel_ops()))
    ref = _structure_ref(store)
    params = {
        "structure_ref_id": ref.id,
        "polish": "emt",
        "fragment": "xtb",
        "buffer_A": 5.0,
        "core_A": 2.0,
        "steps": 30,
    }
    ckpt_dir = hexfold_relax._ckpt_dir(999)
    ckpt_file = ckpt_dir / "checkpoint.json"
    ckpt = {"dir": str(ckpt_dir), "job_id": 999, "base_version": 1}
    kwargs = {
        "polish": "emt",
        "model": None,
        "fragment": "xtb",
        "buffer_A": 5.0,
        "core_A": 2.0,
        "steps": 30,
    }

    # 1) A real spawned child writes real per-step checkpoint files;
    #    kill it once fragment work is several steps in.
    scene, _handles = store.structure_load(ref.id)

    # Kill once the run is into fragment work — trigger on the pointer
    # EVENT stream, not a file poll (a file read can miss a fast stage
    # boundary). The polish-done boundary emit already has
    # stage='fragment', so the first such event guarantees the polish
    # is a completed stage.
    seen: dict[str, Any] = {}

    def _on_ev(kind: str, payload: Any) -> None:
        if kind == "checkpoint" and payload.get("stage") == "fragment":
            seen["frag"] = True

    with pytest.raises(ChildStopped):
        run_in_child(
            targets.relax_child_emt_xtb,
            (scene, ref.meta["generated"], kwargs, False, None, ckpt),
            label="ckpt-kill",
            timeout_s=600,
            should_stop=lambda: "kill" if seen.get("frag") else None,
            on_event=_on_ev,
            poll_s=0.1,
            grace_s=5.0,
        )
    payload = json.loads(ckpt_file.read_bytes())
    # Killed into fragment work — the polish is a completed stage in
    # the payload, not an in-flight one (it may have just crossed the
    # boundary, so stage_steps can be 0).
    assert payload["stage"] == "fragment"
    prior_polish_steps = int(payload["polish"]["n_steps"])
    assert prior_polish_steps > 0

    # 2) Same-job reclaim: the pointer the dead generation's dispatch
    #    would have left in meta.
    ctx, events = _fake_ctx(store, params)
    ctx.meta["relax_checkpoint"] = {
        "v": 1,
        "path": str(ckpt_file),
        "sha256": hashlib.sha256(ckpt_file.read_bytes()).hexdigest(),
        "stage": payload["stage"],
        "fragment": payload.get("fragment"),
        "step": payload["stage_steps"],
        "n": payload["n"],
        "base_version": 1,
        "job_id": 999,
    }
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    assert not [p for k, p in events if k == "fail"]
    assert ("status", "succeeded") in events
    assert any(k == "job_event" and "resuming from checkpoint" in t for k, t in events)
    record = _structure_ref(store).meta["generated"]["relax"]
    # The polish was skipped — the record is the checkpoint's envelope,
    # not a fresh count.
    assert record["polish"]["n_steps"] == prior_polish_steps
    assert "interrupted" not in record
    resumed_scene, _ = store.structure_load(ref.id)

    # 3) Uninterrupted on a second, identical structure.
    se.put(id="hx-tun2", text=json.dumps(_tunnel_ops()))
    ref3 = store.get_ref(kind="structure", id="hx-tun2-t")
    ctx3, events3 = _fake_ctx(store, {**params, "structure_ref_id": ref3.id})
    hexfold_relax._dispatch(ctx3, hexfold_relax.SPEC)
    assert ("status", "succeeded") in events3
    fresh_scene, _ = store.structure_load(ref3.id)

    assert set(resumed_scene.atoms) == set(fresh_scene.atoms)
    diff = max(
        float(
            np.linalg.norm(
                resumed_scene.cell.frac_to_cart(resumed_scene.atoms[la].frac)
                - fresh_scene.cell.frac_to_cart(fresh_scene.atoms[la].frac)
            )
        )
        for la in resumed_scene.atoms
    )
    # fmax-scale agreement: the resumed FIRE lands in the same basin.
    assert diff < 0.25, f"resumed vs uninterrupted max displacement {diff:.3f}A"

    # 4) gr508967: a job on on_version < live relaxes that version but
    #    records it honestly — runs name the loaded version, the save
    #    lands at live+1.
    before_runs = len(store.structure_runs(ref.id))
    ctx4, events4 = _fake_ctx(
        store,
        {
            "structure_ref_id": ref.id,
            "on_version": 1,
            "polish": "emt",
            "fragment": "none",
            "steps": 10,
        },
    )
    hexfold_relax._dispatch(ctx4, hexfold_relax.SPEC)
    assert ("status", "succeeded") in events4
    ref4 = store.get_ref(kind="structure", id=ref.id)
    # live was 2, loaded 1 — the save appends at 3, never collides at 2.
    assert int(ref4.meta["version"]) == 3
    new_runs = store.structure_runs(ref.id)[
        : len(store.structure_runs(ref.id)) - before_runs
    ]
    assert new_runs and all(r["on_version"] == 1 for r in new_runs)


def test_resume_file_newer_across_stage_boundary(tmp_path: Any) -> None:
    """Pointer at fragment-A step 40 (write n=100), file at fragment-B
    step 2 (write n=140): the file is NEWER though stage_steps went
    backwards across the stage boundary — resume keeps it."""
    ckpt_dir = tmp_path / "job-999"
    ckpt_dir.mkdir()
    path = ckpt_dir / "checkpoint.json"
    payload = {
        "v": 1,
        "n": 140,
        "job_id": 999,
        "base_version": 1,
        "stage": "fragment",
        "fragment": "high",
        "stage_steps": 2,
        "curve": [0.6, 0.55],
        "positions": {"a": [0.1, 0.2, 0.3]},
        "polish": {"rung": "emt", "n_steps": 50},
        "fragments": [{"name": "low", "n_steps": 40}],
        "before": [],
    }
    path.write_bytes(json.dumps(payload).encode("utf-8"))
    ptr = {
        "v": 1,
        "path": str(path),
        "sha256": "stale-pointer-sha",
        "stage": "fragment",
        "fragment": "low",
        "step": 40,
        "n": 100,
        "base_version": 1,
        "job_id": 999,
    }
    out = hexfold_relax._resume_from_file({"relax_checkpoint": ptr}, 1, 999)
    assert out is not None
    assert out["fragment"] == "high" and out["stage_steps"] == 2


def test_reap_leaves_a_reused_pid_alone(store: Any) -> None:
    """A stale compute_child handle naming a live, unrelated pid is NOT
    killed — the identity check catches pid reuse (review item B)."""
    import subprocess

    proc = subprocess.Popen(["sleep", "60"])
    try:
        ctx, events = _fake_ctx(store, {})
        host = os.environ.get("PRECIS_NODE") or socket.gethostname()
        ctx.meta["compute_child"] = {
            "pid": proc.pid,
            # Claims group leadership; sleep's real pgid is ours, so the
            # identity check fails before any signal is sent.
            "pgid": proc.pid,
            "host": host,
            "started_at": datetime.now(UTC).isoformat(),
        }
        assert hexfold_relax._reap_prior_child(ctx) is True
        assert proc.poll() is None  # untouched
        assert any(k == "job_event" and "reused pid" in t for k, t in events)
    finally:
        proc.terminate()
        proc.wait()


def test_ckpt_writer_continues_the_write_counter(tmp_path: Any) -> None:
    """A resumed child seeds its write counter from the checkpoint so
    writes keep counting upward — its first write reads as newer than
    the prior attempt's pointer, never older."""
    writer = hexfold_relax._CkptWriter(
        str(tmp_path / "job-999"), job_id=999, base_version=1, resume_n=100
    )
    writer.write(
        {
            "v": 1,
            "stage": "polish",
            "fragment": None,
            "stage_steps": 1,
            "curve": [0.9],
            "positions": {},
            "polish": None,
            "fragments": [],
            "before": [],
        }
    )
    os.close(writer._lock_fd)
    assert json.loads(writer.path.read_bytes())["n"] == 101
