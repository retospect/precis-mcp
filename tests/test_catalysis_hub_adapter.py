"""Catalysis-Hub adapter (ADR 0053 §2, T5). Pure unit test — fixture only, no network."""

from __future__ import annotations

import json
from pathlib import Path

from precis.structure.importers import ExternalId, ExternalRun, get_adapter
from precis.structure.importers.catalysis_hub import adapter
from precis.structure.scene import FIX_ALL, Scene

FIXTURE = Path(__file__).parent / "fixtures" / "catalysis" / "pd111_no.json"


def _load_raw() -> dict:
    return json.loads(FIXTURE.read_text())


def test_adapter_builds_scene_with_right_atoms_and_cell() -> None:
    scene, _run, _eid = adapter(_load_raw())

    assert isinstance(scene, Scene)
    assert len(scene.atoms) == 10
    assert scene.composition() == {"Pd": 8, "N": 1, "O": 1}
    # cell — the InputFile's 3x3 lattice, pbc True/True/False (a slab).
    assert scene.cell.lattice.shape == (3, 3)
    assert tuple(scene.cell.pbc) == (True, True, False)
    assert scene.cell.lattice[2][2] == 20.0


def test_adapter_honours_fixatoms_constraint() -> None:
    scene, _run, _eid = adapter(_load_raw())

    fixed = [a for a in scene.atoms.values() if a.fixed == FIX_ALL]
    free = [a for a in scene.atoms.values() if a.fixed == 0]
    assert len(fixed) == 4  # the bottom Pd layer (indices 0-3)
    assert all(a.element == "Pd" for a in fixed)
    assert len(free) == 6  # top Pd layer + the NO adsorbate


def test_adapter_run_energy_and_method() -> None:
    _scene, run, _eid = adapter(_load_raw())

    assert isinstance(run, ExternalRun)
    # reactionEnergy wins over the raw system total when both are present.
    assert run.energy == -1.85
    assert run.max_force is None  # Catalysis-Hub doesn't expose per-system forces
    assert run.final_geometry is not None
    assert run.final_geometry["numbers"][:3] == [46, 46, 46]
    assert run.method["functional"] == "BEEF-vdW"
    assert run.method["code"] == "Quantum ESPRESSO"
    assert run.method["facet"] == "111"
    assert run.method["surface_composition"] == "Pd"
    assert run.method["dataset_doi"] == "10.1021/acscatal.9b02179"
    assert run.method["reactants"] == {"NOgas": 1, "star": 1}
    assert run.method["products"] == {"NOstar": 1}
    assert run.provenance == "external"


def test_adapter_falls_back_to_system_energy_when_no_reaction_energy() -> None:
    raw = _load_raw()
    raw["reactionEnergy"] = None
    _scene, run, _eid = adapter(raw)
    assert run.energy == -321.456


def test_adapter_external_id_uses_system_unique_id() -> None:
    _scene, _run, eid = adapter(_load_raw())
    assert eid == ExternalId(dataset="catalysis-hub", config_id="PdNO111top-a1b2c3")


def test_catalysis_hub_adapter_is_registered() -> None:
    assert get_adapter("catalysis-hub") is adapter
