"""The ``xtb`` relax rung (GFN2-xTB via tblite's ASE calculator) — the
in-product relaxer of record for hexfold blocks (gr472846: GFN2-xTB for
now; MACE optional). The pillar.hx neck — a (6,0) tube fused k=0 onto a
C60 − hexagon ball, open rim H-terminated — is the 2026-10-11 reference
relaxer run (hexfold-corner/s4_xtb.py): six seam C–C bonds at ~1.403 Å
after a whole-molecule FIRE relax.

Only the real-backend geometry test needs the wheel itself (PyPI has no
linux-aarch64 wheel, so it skips there unless the wheelhouse build is
installed); the recording / refusal /
render tests stub or block the import and run on every platform.
"""

from __future__ import annotations

import json
import sys
import types
from typing import Any

import numpy as np
import pytest
from ase.calculators.calculator import Calculator, all_changes

from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.structure.cell import Cell
from precis.structure.relax import RelaxUnsupported, relax
from precis.structure.scene import Atom as StructAtom
from precis.structure.scene import Scene as StructScene
from precis_se.atomic.generate import generated_cell
from precis_se.handler import SeHandler

#: The pillar.hx neck — (6,0) tube fused k=0 onto the C60 − hexagon ball,
#: open rim H-terminated (the 2026-10-11 reference recipe's neck case at
#: k=0; k=0..3 all converged to the same six 1.403 Å seam bonds).
_NECK_SPEC = (
    "hexfold 0.2\n"
    "origin t\n"
    "t: tube(6,0, len=5)\n"
    "c: fullerene(C60) - hexagon@(0,0,A)\n"
    "t.out --fuse k=0--> c.hole\n"
    "terminate: t.in = H\n"
)


def _neck() -> tuple[StructScene, list[str], np.ndarray, np.ndarray]:
    """The pillar-neck stick seed as a Scene, plus the atom labels / bond
    index pairs / per-atom instance tags the assertions need."""
    from hexfold.build import build
    from hexfold.stick import stick

    net = build(_NECK_SPEC, strict=False)
    assert not net.report.errors()
    pos = np.asarray(stick(net), dtype=float)
    scene = StructScene(cell=generated_cell(pos))
    labels: list[str] = []
    for i, a in enumerate(net.atoms):
        label = scene.next_label(a.element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(pos[i], dtype=float)))
        scene.atoms[label] = StructAtom(label=label, element=a.element, frac=frac)
        labels.append(label)
    return (
        scene,
        labels,
        np.array([(i, j) for i, j, *_ in net.bonds]),
        np.array([a.instance for a in net.atoms]),
    )


def _cart(scene: StructScene, labels: list[str]) -> np.ndarray:
    return np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])


def _dimer() -> StructScene:
    """A two-atom non-periodic molecule — all the rung needs to reach its
    backend gates; the neck is only worth building where geometry is
    asserted."""
    scene = StructScene(
        cell=Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False))
    )
    scene.atoms["aC1"] = StructAtom("aC1", "C", np.array([0.5, 0.5, 0.5]))
    scene.atoms["aC2"] = StructAtom("aC2", "C", np.array([0.57, 0.5, 0.5]))
    return scene


@pytest.mark.slow
def test_xtb_relaxes_the_pillar_neck() -> None:
    """GFN2-xTB on the fused tube→C60 neck: converges from the stick seed,
    the six seam C–C bonds land at 1.40 ± 0.02 Å (the 2026-10-11
    reference), and no non-bonded pair falls under 2.0 Å."""
    pytest.importorskip("tblite")
    scene, labels, bonds, inst = _neck()
    res = relax(scene, fidelity="xtb", steps=1500)
    assert res.rung == "xtb" and res.model == "gfn2-xtb"
    assert res.converged is True
    assert res.curve and res.curve[-1] <= 0.05 + 1e-4
    out = _cart(scene, labels)
    i, j = bonds[:, 0], bonds[:, 1]
    seam = ((inst[i] == "t") & (inst[j] == "c")) | ((inst[i] == "c") & (inst[j] == "t"))
    assert int(seam.sum()) == 6
    bl = np.linalg.norm(out[i] - out[j], axis=1)
    assert np.allclose(bl[seam], 1.40, atol=0.02)
    bonded = {tuple(sorted(p)) for p in bonds.tolist()}
    dmin = min(
        float(np.linalg.norm(out[a] - out[b]))
        for a in range(len(out))
        for b in range(a + 1, len(out))
        if (a, b) not in bonded
    )
    assert dmin > 2.0


def test_xtb_unsupported_without_tblite(monkeypatch: pytest.MonkeyPatch) -> None:
    """A host without the tblite wheel (PyPI has no linux-aarch64 one) raises
    RelaxUnsupported naming the platform limit — never a bare
    ImportError, and the rung is refused rather than computed on
    different physics."""
    monkeypatch.setitem(sys.modules, "tblite.ase", None)
    with pytest.raises(RelaxUnsupported, match="linux-aarch64"):
        relax(_dimer(), fidelity="xtb")


class _StuckTBLite(Calculator):
    """A TBLite stand-in whose forces never fall under fmax — the
    optimiser runs to the step cap, so ``converged`` comes back False
    (a step-capped run is *attempted*, not converged)."""

    implemented_properties = ["energy", "forces", "free_energy"]

    def __init__(self, atoms=None, **kwargs: Any):
        super().__init__(atoms=atoms)

    def calculate(
        self,
        atoms=None,
        properties=("energy",),
        system_changes=all_changes,
    ) -> None:
        super().calculate(atoms, properties, system_changes)
        assert self.atoms is not None
        self.results = {
            "energy": -1.0,
            "free_energy": -1.0,
            "forces": np.full((len(self.atoms), 3), 0.1),
        }


_MOLECULE = json.dumps(
    {
        "cell": {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [False, False, False]},
        "ops": [
            {"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]},
            {"op": "add_atom", "element": "C", "frac": [0.57, 0.5, 0.5]},
            {"op": "add_atom", "element": "H", "frac": [0.43, 0.5, 0.5]},
        ],
    }
)


def _stub_tblite(monkeypatch: pytest.MonkeyPatch, calc: type) -> None:
    """Substitute the TBLite constructor the rung imports — injecting a
    fake ``tblite.ase`` module so the stub lands whether or not the real
    wheel is installed on this platform."""
    fake = types.ModuleType("tblite.ase")
    setattr(fake, "TBLite", calc)  # noqa: B010 — a ModuleType has no TBLite attr
    monkeypatch.setitem(sys.modules, "tblite.ase", fake)


def test_xtb_run_records_rung_model_and_nonconverged(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Through the handler the xtb rung records like every other energy
    rung: struct_runs row + last_relax on the ref + runs view — with the
    optimiser's own honest verdict (non-converged at the step cap)."""
    _stub_tblite(monkeypatch, _StuckTBLite)
    structure = StructureHandler(hub=Hub(store=store))
    structure.put(id="mol", text=_MOLECULE)
    resp = structure.edit(
        id="mol", ops=[{"op": "relax", "fidelity": "xtb", "steps": 3}]
    )
    assert "relax[xtb]" in resp.body

    ref = store.get_ref(kind="structure", id="mol")
    lr = ref.meta["last_relax"]
    assert lr["rung"] == "xtb" and lr["model"] == "gfn2-xtb"
    assert lr["converged"] is False
    run = store.structure_runs(ref.id)[0]
    assert run["fidelity"] == "xtb" and run["model"] == "gfn2-xtb"
    assert run["converged"] is False
    runs_view = structure.get(id="mol", view="runs").body
    assert "xtb" in runs_view and "gfn2-xtb" in runs_view


def test_xtb_foreign_model_rejected(store: Any) -> None:
    """The xtb rung has exactly one backend — naming a different model is
    a BadInput, not a silently relabeled run."""
    from precis.errors import BadInput

    structure = StructureHandler(hub=Hub(store=store))
    structure.put(id="mol", text=_MOLECULE)
    with pytest.raises(BadInput, match="gfn2-xtb"):
        structure.edit(
            id="mol",
            ops=[{"op": "relax", "fidelity": "xtb", "model": "mace_mp"}],
        )


def test_xtb_cell_relax_refused() -> None:
    """No variable-cell on this rung — the same RelaxUnsupported shape
    emt raises, before any tblite work."""
    with pytest.raises(RelaxUnsupported, match="variable-cell"):
        relax(_dimer(), fidelity="xtb", cell="full")


def test_handler_refuses_xtb_cell_before_dispatch(store: Any) -> None:
    """Through the handler a variable-cell xtb relax is a BadInput up front:
    relax()'s RelaxUnsupported is the cloud-dispatch trigger, and a
    struct_relax job carrying cell= for an atoms-only rung would only fail
    as infra on the node after queueing."""
    from precis.errors import BadInput

    structure = StructureHandler(hub=Hub(store=store))
    structure.put(id="mol", text=_MOLECULE)
    with pytest.raises(BadInput, match="atoms only"):
        structure.edit(
            id="mol",
            ops=[{"op": "relax", "fidelity": "xtb", "cell": "full"}],
        )


def test_handler_dispatches_xtb_without_local_tblite(
    store: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host without tblite (the linux-aarch64 MCP container) mints a
    struct_relax job pinned into the compute group, whose worker venv
    carries the wheelhouse tblite wheel (gr488907) — the same path as an
    ``ml`` rung, with the model pinned to gfn2-xtb."""
    monkeypatch.setitem(sys.modules, "tblite.ase", None)
    monkeypatch.setenv("PRECIS_MLIP_NODES", "alpha,beta")
    structure = StructureHandler(hub=Hub(store=store))
    structure.put(id="mol", text=_MOLECULE)
    resp = structure.edit(id="mol", ops=[{"op": "relax", "fidelity": "xtb"}])
    assert "dispatched" in resp.body
    ref = store.get_ref(kind="structure", id="mol")
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job' "
            "AND retired_at IS NULL ORDER BY ref_id",
            (ref.id,),
        ).fetchall()
    assert len(rows) == 1
    params = rows[0][0]["params"]
    assert params["fidelity"] == "xtb" and params["model"] == "gfn2-xtb"
    assert params["target_node"] in {"alpha", "beta"}
    assert "cell" not in params


def test_tblite_present_wherever_it_is_promised() -> None:
    """Fails, never skips, when tblite should be importable and is not: an
    ``[estimate]`` venv on a platform the lock's marker covers (macOS,
    x86_64 — CI's all-extras venv), or any host that sets
    PRECIS_REQUIRE_TBLITE=1 (a compute host carrying the wheelhouse wheel).
    The geometry test above skips without tblite; this one keeps a lost
    wheel from going unnoticed."""
    import importlib.util
    import os
    import platform

    promised = os.environ.get("PRECIS_REQUIRE_TBLITE") == "1" or (
        importlib.util.find_spec("pymatgen") is not None
        and importlib.util.find_spec("mendeleev") is not None
        and (sys.platform == "darwin" or platform.machine() == "x86_64")
    )
    if not promised:
        pytest.skip(
            "no tblite promised here: no [estimate] extra, or linux-aarch64 "
            "without PRECIS_REQUIRE_TBLITE=1"
        )
    from tblite.ase import TBLite  # noqa: F401 — the import is the assertion


def test_se_summary_reports_live_xtb_rung(store: Any) -> None:
    """A generated hexfold block's desc froze ``fidelity=stick`` at mint
    time; after an xtb relax on the bound structure the block view and
    the design summary line report the live rung instead — the stored
    generator record itself is untouched."""
    store.structure_save(
        slug="hx-xtb-struct",
        title="pillar neck",
        scene=StructScene(
            cell=Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)),
            atoms={"aC1": StructAtom("aC1", "C", np.zeros(3))},
        ),
        version=1,
        card_text="xtb relax summary test.",
        relax_summary={
            "rung": "xtb",
            "converged": True,
            "n_steps": 33,
            "max_disp": 0.5,
            "energy": -12.3,
            "max_force": 0.03,
            "model": "gfn2-xtb",
        },
    )
    se = SeHandler(hub=Hub(store=store))
    se.put(
        id="hx-xtb-design",
        text=json.dumps(
            {
                "ops": [
                    {
                        "op": "add_block",
                        "name": "post",
                        "envelope": "sphere:r2e-10",
                        "desc": (
                            "hexfold 0.3.6 spec (66 atoms); fidelity=stick "
                            "seed=cylinder; coordinates derived, not part of "
                            "the format"
                        ),
                    },
                    {
                        "op": "bind_structure",
                        "block": "post",
                        "design": "hx-xtb-struct",
                        "ports": {},
                    },
                ]
            }
        ),
    )
    block = se.get(id="hx-xtb-design", view="block", args={"name": "post"}).body
    assert "fidelity=xtb, converged" in block and "max_force" in block
    assert "fidelity=stick" not in block
    tree = se.get(id="hx-xtb-design", view="tree").body
    assert "fidelity=xtb, converged" in tree and "fidelity=stick" not in tree
