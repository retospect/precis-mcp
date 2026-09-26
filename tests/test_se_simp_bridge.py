"""``realize(strategy='simp')`` — the se → structsolve.simp → cad field-leaf
bridge (docs/backlog/structural-solution-space.md "Slice 4 bridge", round
A). The op validates and enqueues an ``se_simp`` job; the job body
(:func:`precis_se.simp_bridge.run_simp`) is run in-process here.

The fixture is the acceptance paragraph's cantilever: a 24 x 12 x 12 mm
box, clamped on its ``x-`` face, a downward load on its ``x+`` face,
solved at a 2 mm pitch (12 x 6 x 6 = 432 elements — a couple of seconds).

Every design slug is unique per test (the shared test-DB rule).
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub, _try
from precis.errors import BadInput
from precis.store import Store
from precis.workers import job_types as jt
from precis_se import persist, simp_bridge, simp_job
from precis_se import printing as se_printing
from precis_se.handler import SeHandler
from precis_se.ops import SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"

_PITCH = 0.002
_ENVELOPE = "box:w0.024d0.012h0.012"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            c.execute(body.replace("BEGIN;", "").replace("COMMIT;", ""))
    # `_try` constructs + registers abilities, so the job handler sees `se`
    # as a can_own_jobs kind (the route/pathway plugin tests' pattern; a
    # bare SeHandler(hub=hub) is invisible to `hub.kinds`)
    h = _try(SeHandler, hub=hub)
    assert h is not None
    return h


@pytest.fixture
def register_se_simp() -> Any:
    """Inject the ``se_simp`` job_type into the registry (no entry-point
    discovery at test time — the ``se_propose_atomic`` test's pattern)."""
    jt._REGISTRY[simp_bridge.JOB_TYPE] = simp_job.SPEC
    yield
    jt._REGISTRY.pop(simp_bridge.JOB_TYPE, None)


def _cantilever_ops(*, load: bool = True, fixed: bool = True) -> list[dict[str, Any]]:
    ops: list[dict[str, Any]] = [
        {"op": "add_block", "name": "beam", "envelope": _ENVELOPE},
    ]
    load_op: dict[str, Any] = {"op": "set_load", "block": "beam"}
    if load:
        load_op["force"] = [0.0, 0.0, -10.0]
    if fixed:
        load_op["fixed"] = True
    if load or fixed:
        ops.append(load_op)
    return ops


def _tree(ops: list[dict[str, Any]]) -> SeTree:
    tree = SeTree()
    apply_ops(tree, ops)
    return tree


def _simp_op(**overrides: Any) -> dict[str, Any]:
    op: dict[str, Any] = {
        "op": "realize",
        "strategy": "simp",
        "block": "beam",
        "mode": "fdm/pla",
        "pitch": _PITCH,
        "volfrac": 0.4,
        "load_at": "x+",
        "fixed_at": "x-",
        "max_iter": 25,
    }
    op.update(overrides)
    return op


def _load(handler: SeHandler, slug: str) -> SeTree:
    ref = handler.store.get_ref(kind="se", id=slug)
    assert ref is not None
    return persist.load_tree(handler.store, ref.id)


def _job_params(store: Store, parent_id: int) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE kind = 'job' AND parent_id = %s "
            "ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [dict((r[0] or {}).get("params") or {}) for r in rows]


# ---------------------------------------------------------------------------
# the acceptance cantilever: op → job → realized block → view='print'
# ---------------------------------------------------------------------------


@pytest.mark.slow  # 47s in the 2026-09-26 gate profile
def test_cantilever_op_enqueues_and_the_job_realizes_a_field_leaf_design(
    handler: SeHandler, store: Store, register_se_simp: Any
) -> None:
    handler.put(id="simp-cant", text=json.dumps({"ops": _cantilever_ops()}))
    resp = handler.edit(id="simp-cant", ops=[_simp_op()])
    # the op validated, echoed its choices, and enqueued — nothing solved yet
    assert "queued a se_simp job" in resp.body
    assert "build_dir 'z+' (default: largest face" in resp.body
    assert "12x6x6 grid" in resp.body
    assert "enqueued" in resp.body
    tree = _load(handler, "simp-cant")
    assert tree.blocks["beam"].bound_kind is None
    assert tree.blocks["beam"].mode is None
    ref = store.get_ref(kind="se", id="simp-cant")
    assert ref is not None
    params = _job_params(store, ref.id)
    assert len(params) == 1
    assert params[0]["se_ref_id"] == ref.id and params[0]["block"] == "beam"
    assert params[0]["build_dir"] == "z+" and params[0]["build_dir_source"] == (
        "largest-face"
    )
    # a re-submit of the same problem collapses onto the in-flight job
    again = handler.edit(id="simp-cant", ops=[_simp_op()])
    assert "existing job" in again.body or "idem_key=" in again.body
    assert len(_job_params(store, ref.id)) == 1

    # the job body, in-process
    request = simp_bridge.SimpRequest.from_params(params[0])
    outcome = simp_bridge.run_simp(store, ref.id, request)
    assert outcome.cad_slug == "simp-cant-beam"
    assert "bound to new cad design 'simp-cant-beam'" in outcome.echo

    tree = _load(handler, "simp-cant")
    beam = tree.blocks["beam"]
    assert beam.bound_kind == "cad" and beam.bound == "simp-cant-beam"
    assert beam.mode == "fdm/pla"
    assert beam.build_frame == {
        "down": [0.0, 0.0, -1.0],
        "origin": "simp",
        "build_dir": "z+",
    }
    cad_ref = store.get_ref(kind="cad", id="simp-cant-beam")
    assert cad_ref is not None
    spec, _handles = store.cad_load(cad_ref.id)
    assert len(spec.nodes) == 1 and spec.nodes[0].op == "add"
    assert spec.nodes[0].config.startswith("field:")
    sha = spec.nodes[0].config[len("field:") :]
    header, fld = store.get_field(sha)
    assert header["provenance"]["source"] == "se_simp"
    assert header["provenance"]["build_dir"] == "z+"
    assert fld.pitch == pytest.approx(_PITCH)
    # the run summary, on the se ref's meta under one key
    ref = store.get_ref(kind="se", id="simp-cant")
    assert ref is not None
    summary = ref.meta["simp"]["last"]
    assert summary["cad"] == "simp-cant-beam" and summary["field_sha"] == sha
    assert summary["grid"] == [12, 6, 6] and summary["active_elements"] == 432
    assert summary["engine"] == "simp/1" and len(summary["inputs_sha"]) == 64
    assert summary["compliance_last"] < summary["compliance_first"]
    assert summary["volume_fraction"] == pytest.approx(0.4, abs=0.05)
    assert summary["overhang_violation_count"] == 0
    assert summary["overhang_violations_field"] == 0
    assert summary["passive_elements"] > 0 and summary["load_nodes"] == 49
    assert summary["support_nodes"] == 49
    assert any("never a hard DRC" in n for n in summary["notes"])
    assert summary["ran_at"].endswith("+00:00")
    assert len(ref.meta["simp"]["runs"]) == 1
    # lineage: the cad design is derived-from the se design
    links = store.links_for(ref.id, direction="in", relation="derived-from")
    assert [lk.src_ref_id for lk in links] == [cad_ref.id]

    # view='print' verifies the declared frame, skips the search, and has
    # zero overhang findings
    report = se_printing.report_for(tree, "beam", cad_store_reader=store)
    assert report is not None and report.printed is not None
    assert report.candidates == []
    assert report.search_skipped is not None
    assert "build_dir 'z+' was baked in" in report.search_skipped
    assert "0 unsupported voxel(s)" in report.search_skipped
    assert report.chosen_down is not None
    assert np.allclose(report.chosen_down, [0.0, 0.0, -1.0])
    assert [f.rule for f in report.findings if f.rule == "overhang"] == []
    body = handler.get(id="simp-cant", view="print", args={"block": "beam"}).body
    assert "build frame (simp)" in body
    assert "orientation search skipped" in body
    summary_body = handler.get(id="simp-cant", view="print").body
    assert "orientation search skipped" in summary_body


def test_bind_lands_on_a_fresh_tree_and_refuses_when_the_inputs_changed(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The solve runs for minutes on a snapshot; the bind must neither
    clobber an edit made meanwhile (retire-all/reinsert-all would) nor
    bind a field solved for a block that has since changed."""
    handler.put(id="simp-race", text=json.dumps({"ops": _cantilever_ops()}))
    ref = store.get_ref(kind="se", id="simp-race")
    assert ref is not None
    snapshot = _load(handler, "simp-race")
    _e, request = simp_bridge.prepare_simp(snapshot, _simp_op())
    solve = simp_bridge.solve_simp(snapshot, request)

    # (a) an unrelated edit between snapshot and bind survives the bind
    handler.edit(
        id="simp-race",
        ops=[{"op": "add_block", "name": "stand", "envelope": "box:w0.01d0.01h0.01"}],
    )
    outcome = simp_bridge.realize_simp(store, ref.id, request, solve)
    tree = _load(handler, "simp-race")
    assert set(tree.blocks) == {"beam", "stand"}
    assert tree.blocks["beam"].bound == outcome.cad_slug == "simp-race-beam"

    # (b) an edit to the solve's inputs makes the bind refuse, bind nothing,
    # retire the minted design and leave a discoverable failure marker
    snapshot = _load(handler, "simp-race")
    _e, request2 = simp_bridge.prepare_simp(snapshot, _simp_op(volfrac=0.3))
    solve2 = simp_bridge.solve_simp(snapshot, request2)
    handler.edit(
        id="simp-race",
        ops=[{"op": "set_load", "block": "beam", "force": [0.0, 0.0, -20.0]}],
    )
    with pytest.raises(simp_bridge.SimpStale, match="design changed while solving"):
        simp_bridge.realize_simp(store, ref.id, request2, solve2)
    tree = _load(handler, "simp-race")
    assert tree.blocks["beam"].bound == "simp-race-beam"  # unchanged
    assert tree.blocks["beam"].objectives["force"] == [0.0, 0.0, -20.0]  # kept
    assert store.get_ref(kind="cad", id="simp-race-beam-2") is None  # no orphan
    ref = store.get_ref(kind="se", id="simp-race")
    assert ref is not None
    assert ref.meta["simp"]["last"]["cad"] == "simp-race-beam"
    assert "failed" not in ref.meta["simp"]  # the pre-mint check caught it
    assert len(ref.meta["simp"]["runs"]) == 1

    # (c) the same, past the pre-mint check: the design changes after the
    # cad design was minted — the locked check refuses, the design is
    # retired and meta.simp.failed names it
    snapshot = _load(handler, "simp-race")
    _e, request3 = simp_bridge.prepare_simp(snapshot, _simp_op(volfrac=0.3))
    solve3 = simp_bridge.solve_simp(snapshot, request3)
    real_slug = simp_bridge._unique_cad_slug

    def _racing_slug(st: Store, base: str) -> tuple[str, str | None]:
        # runs after the pre-mint check and before the lock is taken — the
        # window a concurrent edit can still slip into
        handler.edit(
            id="simp-race",
            ops=[{"op": "set_load", "block": "beam", "force": [0.0, 0.0, -30.0]}],
        )
        return real_slug(st, base)

    monkeypatch.setattr(simp_bridge, "_unique_cad_slug", _racing_slug)
    with pytest.raises(simp_bridge.SimpStale, match="at bind time"):
        simp_bridge.realize_simp(store, ref.id, request3, solve3)
    monkeypatch.setattr(simp_bridge, "_unique_cad_slug", real_slug)
    assert store.get_ref(kind="cad", id="simp-race-beam-2") is None  # retired
    assert store.get_ref(kind="cad", id="simp-race-beam-2", include_deleted=True)
    ref = store.get_ref(kind="se", id="simp-race")
    assert ref is not None
    failed = ref.meta["simp"]["failed"]
    assert failed["cad"] == "simp-race-beam-2" and failed["retired"] is True
    assert "design changed while solving" in failed["reason"]
    assert ref.meta["simp"]["last"]["cad"] == "simp-race-beam"
    assert _load(handler, "simp-race").blocks["beam"].bound == "simp-race-beam"


def test_tree_mutation_holds_the_per_ref_advisory_lock(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="simp-lock", text=json.dumps({"ops": _cantilever_ops()}))
    ref = store.get_ref(kind="se", id="simp-lock")
    assert ref is not None

    def _try_lock() -> bool:
        with store.pool.connection() as other:
            row = other.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (persist.TREE_LOCK_NAMESPACE, ref.id),
            ).fetchone()
            assert row is not None
            return bool(row[0])

    assert _try_lock() is True
    with persist.tree_mutation(store, ref.id):
        assert _try_lock() is False  # held by the mutation's transaction
    assert _try_lock() is True  # released with the commit


def test_idem_key_follows_a_moved_load_port(
    handler: SeHandler, store: Store, register_se_simp: Any
) -> None:
    ops = [
        *_cantilever_ops(),
        {
            "op": "add_port",
            "block": "beam",
            "name": "tip",
            "pose": [0.024, 0.006, 0.006],
        },
    ]
    handler.put(id="simp-idem", text=json.dumps({"ops": ops}))
    ref = store.get_ref(kind="se", id="simp-idem")
    assert ref is not None
    tree = _load(handler, "simp-idem")
    _e, req = simp_bridge.prepare_simp(tree, _simp_op(load_at="tip"))
    before = simp_bridge.inputs_sha(tree, req)
    handler.edit(id="simp-idem", ops=[_simp_op(load_at="tip")])
    assert len(_job_params(store, ref.id)) == 1
    # the same problem again dedupes onto the in-flight job
    handler.edit(id="simp-idem", ops=[_simp_op(load_at="tip")])
    assert len(_job_params(store, ref.id)) == 1
    # move the port the load sits on: a different problem, a fresh job
    handler.edit(
        id="simp-idem",
        ops=[
            {
                "op": "set_port_pose",
                "block": "beam",
                "name": "tip",
                "pose": [0.024, 0.006, 0.010],
            }
        ],
    )
    tree = _load(handler, "simp-idem")
    assert simp_bridge.inputs_sha(tree, req) != before
    handler.edit(id="simp-idem", ops=[_simp_op(load_at="tip")])
    assert len(_job_params(store, ref.id)) == 2
    # a vanished block hashes to nothing (the bind-time "no longer exists")
    assert simp_bridge.inputs_sha(tree, replace(req, block="nope")) is None


def test_enqueue_failure_is_reported_not_raised(
    handler: SeHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """job.put fails (injected — whether ``se_simp`` is registered depends
    on the venv, so the refusal cannot be left to the registry). The tree
    edits in the same call must still land, and the response must say the
    solve was NOT queued."""
    handler.put(id="simp-noq", text=json.dumps({"ops": _cantilever_ops()}))
    job_handler = handler.hub.sibling("job")

    def _refuse(**_: Any) -> None:
        raise BadInput("job_type 'se_simp' refused (injected)")

    monkeypatch.setattr(job_handler, "put", _refuse)
    resp = handler.edit(
        id="simp-noq",
        ops=[
            {"op": "add_block", "name": "extra", "envelope": "box:w0.01d0.01h0.01"},
            _simp_op(),
        ],
    )
    assert "simp solve NOT queued for 'beam'" in resp.body
    assert "tree edits in this call were saved" in resp.body
    tree = _load(handler, "simp-noq")
    assert "extra" in tree.blocks
    assert tree.blocks["beam"].bound_kind is None


def test_re_realize_mints_a_sibling_and_leaves_the_first_in_place(
    handler: SeHandler, store: Store
) -> None:
    handler.put(id="simp-twice", text=json.dumps({"ops": _cantilever_ops()}))
    ref = store.get_ref(kind="se", id="simp-twice")
    assert ref is not None
    tree = _load(handler, "simp-twice")
    _echo, request = simp_bridge.prepare_simp(tree, _simp_op())
    first = simp_bridge.run_simp(store, ref.id, request)
    assert first.cad_slug == "simp-twice-beam"
    # the second realize goes through the op again: a cad-bound block is
    # accepted (a component-bound one would not be), and the job mints a
    # sibling with the collision suffix
    tree = _load(handler, "simp-twice")
    _echo, request2 = simp_bridge.prepare_simp(tree, _simp_op(volfrac=0.3))
    second = simp_bridge.run_simp(store, ref.id, request2)
    assert second.cad_slug == "simp-twice-beam-2"
    assert "previous realization 'simp-twice-beam' left in place" in second.echo
    assert "was already taken" in second.echo

    tree = _load(handler, "simp-twice")
    assert tree.blocks["beam"].bound == "simp-twice-beam-2"
    old = store.get_ref(kind="cad", id="simp-twice-beam")
    new = store.get_ref(kind="cad", id="simp-twice-beam-2")
    assert old is not None and new is not None
    old_spec, _h = store.cad_load(old.id)
    assert old_spec.nodes[0].config.startswith("field:")  # untouched
    ref = store.get_ref(kind="se", id="simp-twice")
    assert ref is not None
    runs = ref.meta["simp"]["runs"]
    assert [r["cad"] for r in runs] == ["simp-twice-beam", "simp-twice-beam-2"]
    assert runs[1]["previous_cad"] == "simp-twice-beam"
    assert ref.meta["simp"]["last"]["volfrac"] == pytest.approx(0.3)
    links = store.links_for(ref.id, direction="in", relation="derived-from")
    assert sorted(lk.src_ref_id for lk in links) == sorted([old.id, new.id])


# ---------------------------------------------------------------------------
# refusals (all at op time, against the in-memory tree)
# ---------------------------------------------------------------------------


def test_refuses_a_block_without_loads_or_supports(handler: SeHandler) -> None:
    handler.put(
        id="simp-noload",
        text=json.dumps({"ops": _cantilever_ops(load=False, fixed=False)}),
    )
    with pytest.raises(BadInput, match="no load .* and no support") as exc:
        handler.edit(id="simp-noload", ops=[_simp_op()])
    assert "set_load(block='beam', force=" in str(exc.value)
    handler.put(id="simp-nofix", text=json.dumps({"ops": _cantilever_ops(fixed=False)}))
    with pytest.raises(BadInput, match="declares no support"):
        handler.edit(id="simp-nofix", ops=[_simp_op()])
    # nothing was enqueued or bound
    assert _load(handler, "simp-noload").blocks["beam"].bound_kind is None


def test_refuses_without_pitch_when_the_house_figure_is_null() -> None:
    tree = _tree(_cantilever_ops())
    op = _simp_op()
    del op["pitch"]
    with pytest.raises(simp_bridge.SimpBridgeError, match="simp_pitch") as exc:
        simp_bridge.prepare_simp(tree, op)
    assert "pass pitch=" in str(exc.value)
    # a per-block override (millimetres, the capability's unit) fills the
    # gap through the ordinary resolver — no default in code
    apply_ops(
        tree,
        [
            {"op": "set_mode", "block": "beam", "mode": "fdm/pla"},
            {
                "op": "set_process_override",
                "block": "beam",
                "field": "simp_pitch",
                "value": 3.0,
            },
        ],
    )
    _echo, request = simp_bridge.prepare_simp(tree, op)
    assert request.pitch == pytest.approx(0.003)


@pytest.mark.parametrize("bad", [None, 0, 1, 1.5, -0.2, "lots"])
def test_volfrac_validation(bad: Any) -> None:
    tree = _tree(_cantilever_ops())
    op = _simp_op()
    if bad is None:
        del op["volfrac"]
    else:
        op["volfrac"] = bad
    with pytest.raises(simp_bridge.SimpBridgeError, match="volfrac"):
        simp_bridge.prepare_simp(tree, op)


def test_token_and_option_refusals() -> None:
    tree = _tree(_cantilever_ops())
    with pytest.raises(simp_bridge.SimpBridgeError, match="build_dir must be one of"):
        simp_bridge.prepare_simp(tree, _simp_op(build_dir="up"))
    with pytest.raises(simp_bridge.SimpBridgeError, match="neither an axis face"):
        simp_bridge.prepare_simp(tree, _simp_op(load_at="tip"))
    with pytest.raises(simp_bridge.SimpBridgeError, match="needs fixed_at="):
        simp_bridge.prepare_simp(tree, _simp_op(fixed_at=None))
    with pytest.raises(simp_bridge.SimpBridgeError, match="both name 'x\\+'"):
        simp_bridge.prepare_simp(tree, _simp_op(fixed_at="x+"))
    with pytest.raises(simp_bridge.SimpBridgeError, match="round= is open\\+close"):
        simp_bridge.prepare_simp(tree, _simp_op(round=0.002, open=0.002))
    with pytest.raises(simp_bridge.SimpBridgeError, match="below half the pitch"):
        simp_bridge.prepare_simp(tree, _simp_op(close=0.0001))
    with pytest.raises(
        simp_bridge.SimpBridgeError, match="max_iter must lie in 1..300"
    ):
        simp_bridge.prepare_simp(tree, _simp_op(max_iter=1000))
    with pytest.raises(simp_bridge.SimpBridgeError, match="above the 500000 budget"):
        simp_bridge.prepare_simp(tree, _simp_op(pitch=1e-5))
    with pytest.raises(simp_bridge.SimpBridgeError, match="no envelope"):
        simp_bridge.prepare_simp(
            _tree([{"op": "add_block", "name": "beam"}]), _simp_op()
        )
    # a port without a pose is refused, never guessed
    apply_ops(tree, [{"op": "add_port", "block": "beam", "name": "tip"}])
    with pytest.raises(simp_bridge.SimpBridgeError, match="has no pose"):
        simp_bridge.prepare_simp(tree, _simp_op(load_at="tip"))
    # a load along an axis the support leaves free is a mechanism
    apply_ops(tree, [{"op": "set_load", "block": "beam", "fixed": ["x", "y"]}])
    with pytest.raises(simp_bridge.SimpBridgeError, match="fixed in xy only"):
        simp_bridge.prepare_simp(tree, _simp_op())


def test_unknown_strategy_and_component_bound_block_are_refused(
    handler: SeHandler,
) -> None:
    handler.put(id="simp-strat", text=json.dumps({"ops": _cantilever_ops()}))
    with pytest.raises(BadInput, match="unknown strategy 'magic'"):
        handler.edit(id="simp-strat", ops=[_simp_op(strategy="magic")])
    handler.edit(
        id="simp-strat",
        ops=[
            {
                "op": "set_binding",
                "block": "beam",
                "kind": "component",
                "design": "some-bought-thing",
            }
        ],
    )
    with pytest.raises(BadInput, match="bound to a component"):
        handler.edit(id="simp-strat", ops=[_simp_op()])


def test_default_strategy_is_the_analytic_seed(handler: SeHandler) -> None:
    """``strategy`` absent → the envelope seed, byte-for-byte the old op."""
    handler.put(id="simp-default", text=json.dumps({"ops": _cantilever_ops()}))
    resp = handler.edit(
        id="simp-default", ops=[{"op": "realize", "block": "beam", "mode": "fdm/pla"}]
    )
    assert "bound to new cad design 'simp-default-beam'" in resp.body
    assert "se_simp" not in resp.body
    tree = _load(handler, "simp-default")
    assert tree.blocks["beam"].bound == "simp-default-beam"
    assert tree.blocks["beam"].build_frame is None


# ---------------------------------------------------------------------------
# the pure solve: morphology, build-direction permutation, port placement
# ---------------------------------------------------------------------------


def _volume(fld: Any) -> float:
    return float(np.count_nonzero(np.asarray(fld.grid) <= 0.0)) * fld.pitch**3


def test_morphology_moves_the_volume_the_expected_way() -> None:
    tree = _tree(_cantilever_ops())
    # volfrac 0.6 so the coarse field has something thicker than 3 voxels
    # left after the open below (at 0.4 a 0.75-pitch open erases the whole
    # body — which is then a refusal, tested separately)
    _e, plain = simp_bridge.prepare_simp(tree, _simp_op(volfrac=0.6))
    _e, closed = simp_bridge.prepare_simp(tree, _simp_op(volfrac=0.6, close=_PITCH))
    _e, opened = simp_bridge.prepare_simp(
        tree, _simp_op(volfrac=0.6, open=0.75 * _PITCH)
    )
    base = simp_bridge.solve_simp(tree, plain)
    v0 = _volume(base.field)
    assert base.summary["morphology"] == {}
    res_c = simp_bridge.solve_simp(tree, closed)
    res_o = simp_bridge.solve_simp(tree, opened)
    # closing fills necks/concave steps: never less material
    assert _volume(res_c.field) > v0
    assert res_c.summary["morphology"]["close_m"] == pytest.approx(_PITCH)
    assert (
        res_c.summary["morphology"]["volume_after_m3"]
        > (res_c.summary["morphology"]["volume_before_m3"])
    )
    # opening erases anything thinner than 2r: never more material, and
    # what vanished is reported, never silent
    assert 0.0 < _volume(res_o.field) < v0
    assert res_o.findings and "vanished under open" in res_o.findings[0]
    assert res_o.summary["morphology_findings"] == res_o.findings
    assert (
        res_o.summary["morphology"]["volume_after_m3"]
        < (res_o.summary["morphology"]["volume_before_m3"])
    )
    # the same solve underneath (the density is deterministic)
    assert res_c.summary["compliance_last"] == pytest.approx(
        base.summary["compliance_last"]
    )
    # an open that erases the whole body is a refusal, not an empty design
    _e, gone = simp_bridge.prepare_simp(tree, _simp_op(open=0.75 * _PITCH))
    with pytest.raises(simp_bridge.SimpBridgeError, match="erased the whole body"):
        simp_bridge.solve_simp(tree, gone)


@pytest.mark.parametrize("build_dir", ["x+", "x-", "y+", "y-", "z-"])
def test_any_build_direction_is_self_supporting_in_its_own_frame(
    build_dir: str,
) -> None:
    tree = _tree(_cantilever_ops())
    _e, req = simp_bridge.prepare_simp(tree, _simp_op(build_dir=build_dir, max_iter=8))
    assert req.build_dir_source == "given"
    solve = simp_bridge.solve_simp(tree, req)
    density = solve.result.density
    # the engine solved in its own +z frame; the field comes back in the
    # block frame with the block's grid shape
    assert solve.field.shape == tuple(n + 4 for n in (12, 6, 6))
    assert solve.summary["grid"] == [12, 6, 6]
    assert solve.summary["overhang_violation_count"] == 0
    assert solve.summary["overhang_violations_field"] == 0
    frame = simp_bridge._BuildFrame.for_dir(build_dir)
    # round trip of the permutation is exact
    back = frame.backward_array(density)
    assert np.array_equal(frame.forward_array(back), density)
    axis = "xyz".index(build_dir[0])
    expected_down = [0.0, 0.0, 0.0]
    expected_down[axis] = 1.0 if build_dir[1] == "-" else -1.0
    assert frame.down_local() == expected_down


def test_port_placed_load_uses_the_nearest_active_element() -> None:
    tree = _tree(
        [
            *_cantilever_ops(),
            {
                "op": "add_port",
                "block": "beam",
                "name": "tip",
                "pose": [0.024, 0.006, 0.006],
            },
        ]
    )
    _e, req = simp_bridge.prepare_simp(tree, _simp_op(load_at="tip", max_iter=5))
    solve = simp_bridge.solve_simp(tree, req)
    assert solve.summary["load_nodes"] == 8
    assert solve.summary["load_at"] == "tip"
    assert solve.summary["support_nodes"] == 49
    # the summary carries the local-frame force it actually applied
    assert solve.summary["force_local_n"] == [0.0, 0.0, -10.0]


def test_request_round_trips_through_job_params() -> None:
    tree = _tree(_cantilever_ops())
    _e, req = simp_bridge.prepare_simp(tree, _simp_op(close=0.002, build_dir="y+"))
    params = req.to_params()
    assert set(params) <= set(simp_job.PARAMS_SCHEMA["properties"])
    assert simp_bridge.SimpRequest.from_params(params) == req
    assert simp_job.SPEC.name == "se_simp"
    assert simp_job.SPEC.compatible_executors == frozenset({"job_inproc"})
