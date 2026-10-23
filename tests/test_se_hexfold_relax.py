"""H3 relax path for an assembled tunnel — the strain bookkeeping
persisted on the block, the fragment cutter, and ``relax_block``
re-scoring ``tunnel_strain`` on the relaxed atoms (cheap whole-block
polish + junction fragments).

The fragment pass rides the ``xtb`` rung; tests stub ``tblite.ase`` the
way ``test_structure_relax_xtb`` does (tblite is absent on the Mac), so
the same code path runs everywhere with ASE-EMT standing in for GFN2.
"""

from __future__ import annotations

import sys
import types
from typing import Any

import numpy as np
import pytest

from precis.structure import Atom as StructAtom
from precis.structure import Bond as StructBond
from precis.structure import Scene as StructScene
from precis_se.atomic.generate import generated_cell
from precis_se.atomic.generators.hexfold_scene import build_hexfold_scene
from precis_se.atomic.relax_block import (
    cut_junction_fragment,
    relax_block,
    strain_bookkeeping,
)

TUBE: dict[str, Any] = {"rail": [10, 3], "rails": 2, "rows": 2, "pores": []}
ARMCHAIR = [4, 3]


def _params(tube: Any = ARMCHAIR, sheet: list[int] | None = None) -> dict[str, Any]:
    return {
        "tube": tube if isinstance(tube, dict) else list(tube),
        "features": [
            {"name": "t", "type": "tunnel-reservoirs", "sheet": sheet or [24, 24]}
        ],
        "k_tether": 1.0,
    }


def _scene_of(block: Any) -> StructScene:
    scene = StructScene(cell=generated_cell(block.coords))
    labels: list[str] = []
    for i, (element, cart) in enumerate(zip(block.elements, block.coords, strict=True)):
        label = scene.next_label(element)
        scene.atoms[label] = StructAtom(
            label=label,
            element=element,
            frac=scene.cell.wrap(
                scene.cell.cart_to_frac(np.asarray(cart, dtype=float))
            ),
            hybridization=(block.hybridizations[i] if block.hybridizations else None),
        )
        labels.append(label)
    for i, j, order, kind in block.bonds:
        scene.bonds.append(StructBond(i=labels[i], j=labels[j], order=order, kind=kind))
    return scene


def _stub_xtb_with_emt(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``xtb`` rung's calculator replaced by ASE-EMT — same relax
    path, no tblite needed (the wheel is absent on the Mac)."""
    from ase.calculators.emt import EMT

    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", lambda **kw: EMT())  # noqa: B010 — ModuleType has no attr
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)


def test_bookkeeping_persisted_on_the_block() -> None:
    block = build_hexfold_scene(_params(tube=TUBE, sheet=[30, 30]))
    junctions = block.topology["junctions"]
    assert [j["name"] for j in junctions] == ["low", "high"]
    n = len(block.coords)
    for j in junctions:
        assert j["atoms"] and j["fused"] and j["faces"]
        assert all(0 <= a < n for a in j["atoms"])
        assert all(0 <= a < n and 0 <= b < n for a, b in j["fused"])
    # a rail tunnel keeps its seam chains; an armchair one omits the key
    assert [list(s) for s in block.topology["seam_atoms"]]
    assert block.topology["sigma_A"] > 1.0
    armchair = build_hexfold_scene(_params())
    assert "junctions" in armchair.topology
    assert "seam_atoms" not in armchair.topology


def test_strain_bookkeeping_round_trip() -> None:
    block = build_hexfold_scene(_params())
    bookkeeping = strain_bookkeeping(block.topology)
    assert bookkeeping is not None
    zones, seams, sigma = bookkeeping
    assert [z.name for z in zones] == ["low", "high"]
    assert seams == [] and sigma > 1.0
    assert strain_bookkeeping({"generated": {}}) is None
    assert strain_bookkeeping({}) is None


def test_fragment_cut_caps_and_freezes() -> None:
    block = build_hexfold_scene(_params())
    bookkeeping = strain_bookkeeping(block.topology)
    assert bookkeeping is not None
    zones, _seams, _sigma = bookkeeping
    coords = np.asarray(block.coords)
    elements = np.asarray(block.elements)
    frag = cut_junction_fragment(
        coords, elements, block.bonds, zones[0].atoms, "low", buffer_A=6.0, core_A=2.0
    )
    assert set(zones[0].atoms) <= set(frag.members) <= set(range(len(coords)))
    assert frag.movable < set(frag.members)  # a frozen buffer shell exists
    assert frag.caps  # severed boundary bonds are H-capped
    for host_row, pos in frag.caps:
        assert 0 <= host_row < len(frag.members)
        assert 0.8 < float(np.linalg.norm(pos - coords[frag.members[host_row]])) < 1.4
    for i, j, _o, _k in frag.bonds:
        assert 0 <= i < len(frag.members) and 0 <= j < len(frag.members)


@pytest.mark.slow
def test_relax_block_polish_and_fragment_passes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """EMT polish + a stubbed-xtb fragment pass: the scene's coords move,
    the strain findings re-score before and after, and each junction
    reports its fragment's shape."""
    _stub_xtb_with_emt(monkeypatch)
    block = build_hexfold_scene(_params())
    scene = _scene_of(block)
    before_coords = np.array(
        [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()]
    )
    report = relax_block(
        scene,
        block.topology,
        polish="emt",
        fragment="xtb",
        buffer_A=5.0,
        core_A=2.0,
        steps=30,
    )
    assert report.polish.rung == "emt"
    assert len(report.fragments) == 2
    for frag in report.fragments:
        assert frag.result.rung == "xtb"
        assert frag.h_caps > 0 and frag.frozen > 0
    assert len(report.before) == len(report.after)
    assert {f["code"] for f in report.before} >= {"junction.strain"}
    assert {f["code"] for f in report.after} >= {"junction.strain"}
    after_coords = np.array(
        [scene.cell.frac_to_cart(a.frac) for a in scene.atoms.values()]
    )
    assert float(np.linalg.norm(after_coords - before_coords, axis=1).max()) > 0.0


def test_relax_block_refuses_a_record_without_junctions() -> None:
    block = build_hexfold_scene(_params())
    scene = _scene_of(block)
    with pytest.raises(ValueError, match="junctions"):
        relax_block(scene, {"generated": {}}, polish="emt", fragment="none")
    with pytest.raises(ValueError, match="polish"):
        relax_block(scene, block.topology, polish="gpaw")


@pytest.mark.slow
def test_relax_block_emits_step_checkpoints(monkeypatch: pytest.MonkeyPatch) -> None:
    """gr508493: every optimiser step overwrites one checkpoint slot —
    positions, the in-flight stage's step count, and done-state."""
    _stub_xtb_with_emt(monkeypatch)
    block = build_hexfold_scene(_params())
    scene = _scene_of(block)
    ckpts: list[dict[str, Any]] = []
    relax_block(
        scene,
        block.topology,
        polish="emt",
        fragment="xtb",
        buffer_A=5.0,
        core_A=2.0,
        steps=8,
        on_checkpoint=ckpts.append,
    )
    assert ckpts and all(c["v"] == 1 for c in ckpts)
    stages = [c["stage"] for c in ckpts]
    assert stages[0] == "polish" and stages[-1] == "strain"
    assert "fragment" in stages
    polish_steps = [c for c in ckpts if c["stage"] == "polish"]
    assert any(c["stage_steps"] > 0 for c in polish_steps)
    last = ckpts[-1]
    assert len(last["positions"]) == len(scene.atoms)
    assert len(last["fragments"]) == 2
    assert last["polish"]["rung"] == "emt"
    for la, atom in scene.atoms.items():
        assert last["positions"][la] == pytest.approx(list(atom.frac), abs=1e-6)
    # Review item 2 — a stage's steps are counted only while that stage
    # is in flight: the emits right after the polish ends and right
    # after each fragment ends carry stage_steps=0 and name the NEXT
    # stage, so a stop there never double-counts a recorded stage.
    boundary = [c for c in ckpts if c["stage"] == "fragment" and c["fragment"] is None]
    # One polish-done marker + one marker per completed fragment.
    assert len(boundary) == 3
    assert all(c["stage_steps"] == 0 for c in boundary)
    assert boundary[0]["polish"]["rung"] == "emt"
    assert boundary[0]["fragments"] == []
    assert len(boundary[1]["fragments"]) == 1
    assert len(boundary[2]["fragments"]) == 2


@pytest.mark.slow
def test_relax_block_resumes_from_checkpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """A REAL interrupted run's last checkpoint continues (review item
    7): the run is killed mid-fragment, and the resume skips completed
    stages, continues the in-flight one on its remaining budget, and
    reports the whole stage cost."""
    import precis_se.atomic.relax_block as relax_block_mod
    from precis.structure.relax import relax as real_relax

    _stub_xtb_with_emt(monkeypatch)
    block = build_hexfold_scene(_params())
    scene = _scene_of(block)
    ckpts: list[dict[str, Any]] = []

    class _Killed(Exception):
        pass

    def _collect_then_die(c: dict[str, Any]) -> None:
        ckpts.append(c)
        if c["stage"] == "fragment" and c["fragment"] and c["stage_steps"] >= 2:
            raise _Killed

    with pytest.raises(_Killed):
        relax_block(
            scene,
            block.topology,
            polish="emt",
            fragment="xtb",
            buffer_A=5.0,
            core_A=2.0,
            steps=8,
            on_checkpoint=_collect_then_die,
        )
    # The last checkpoint the dying run emitted: polish done, first
    # fragment in flight at its stage_steps.
    ck = ckpts[-1]
    assert ck["stage"] == "fragment" and ck["fragment"]
    assert ck["polish"]["rung"] == "emt"
    assert ck["fragments"] == []
    prior_steps = int(ck["stage_steps"])
    assert prior_steps >= 2
    in_flight = str(ck["fragment"])
    prior_positions = dict(ck["positions"])

    scene2 = _scene_of(block)
    calls: list[dict[str, Any]] = []
    start_positions: dict[str, np.ndarray] = {}

    def _spy(scene_arg: Any, **kw: Any) -> Any:
        if not start_positions:
            start_positions.update(
                {la: np.asarray(a.frac).copy() for la, a in scene2.atoms.items()}
            )
        calls.append(kw)
        return real_relax(scene_arg, **kw)

    monkeypatch.setattr(relax_block_mod, "relax", _spy)
    ckpts2: list[dict[str, Any]] = []
    report2 = relax_block_mod.relax_block(
        scene2,
        block.topology,
        polish="emt",
        fragment="xtb",
        buffer_A=5.0,
        core_A=2.0,
        steps=8,
        on_checkpoint=ckpts2.append,
        resume=ck,
    )
    # Polish and no fragment before the in-flight one were re-run.
    assert [c["fidelity"] for c in calls] == ["xtb", "xtb"]
    assert calls[0]["steps"] == 8 - prior_steps
    assert calls[0]["on_step"] is not None
    # The scene the first relax call saw carries the checkpointed
    # positions — the resume overlaid them before any compute ran.
    assert all(
        np.allclose(np.asarray(pos), start_positions[la], atol=1e-6)
        for la, pos in prior_positions.items()
        if la in start_positions
    )
    # The reconstructed polish keeps its recorded envelope; fragments
    # carry prior + new steps in their totals.
    assert report2.polish.n_steps == int(ck["polish"]["n_steps"])
    assert report2.polish.energy == ck["polish"]["energy"]
    low = next(f for f in report2.fragments if f.name == in_flight)
    assert low.result.n_steps >= prior_steps
    assert len(report2.fragments) == 2
    last = ckpts2[-1]
    assert last["stage"] == "strain" and len(last["fragments"]) == 2
