"""``hexfold_relax`` job_type — the H3 relax pipeline as a compute-group
job (docs/backlog/hexfold-nanoreactor-tube.md).

Proved without a cluster, the way ``test_struct_relax_job`` does: a fake
``DispatchContext`` drives the dispatch against a generated structure,
the ``xtb`` rung's calculator is stubbed (tblite is absent on the Mac —
same pattern as ``test_structure_relax_xtb``), and the whole-block
polish runs on the always-available ``emt`` rung.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.workers.executors import ssh_node
from precis.workers.executors._context import DispatchContext
from precis_se import hexfold_relax_job as hexfold_relax
from precis_se.handler import SeHandler


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


def _fake_ctx(store: Any, params: dict[str, Any]) -> tuple[DispatchContext, list]:
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
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _stub_xtb_with_emt(monkeypatch: pytest.MonkeyPatch) -> None:
    from ase.calculators.emt import EMT

    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", lambda **kw: EMT())  # noqa: B010 — ModuleType has no attr
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)


@pytest.mark.slow
def test_dispatch_relaxes_and_stores_strain(store: Any, monkeypatch) -> None:
    """End to end: generate a rail tunnel, dispatch the job with an EMT
    polish + stubbed-xtb fragment pass — the scene gains a version, the
    generated record carries before/after strain, and the compute passes
    land in struct_runs."""
    _stub_xtb_with_emt(monkeypatch)
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


def test_dispatch_without_bookkeeping_fails(store: Any) -> None:
    """A structure not minted with the H3 strain record gets a clean
    params-class failure, not a crash."""
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
    ref = store.get_ref(kind="structure", id="plain")
    ctx, events = _fake_ctx(store, {"structure_ref_id": ref.id, "fragment": "none"})
    hexfold_relax._dispatch(ctx, hexfold_relax.SPEC)
    fails = [p for k, p in events if k == "fail"]
    assert len(fails) == 1
    assert "junctions" in fails[0]["reason"]


def test_dispatch_missing_backend_is_infra(store: Any, monkeypatch) -> None:
    """No tblite on the claiming node → an honest infra failure naming
    the extra, never different physics silently."""
    monkeypatch.setitem(sys.modules, "tblite.ase", None)
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
