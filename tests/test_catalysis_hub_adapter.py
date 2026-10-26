"""Catalysis-Hub adapter. Pure unit test — fixture only, no network."""

from __future__ import annotations

import json
from pathlib import Path

from precis.structure.importers import (
    ExternalId,
    ExternalRun,
    Reference,
    get_adapter,
)
from precis.structure.importers.catalysis_hub import _reactions_query, adapter
from precis.structure.scene import FIX_ALL, Scene

FIXTURE = Path(__file__).parent / "fixtures" / "catalysis" / "pd111_no.json"


def _load_raw() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


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


def test_adapter_run_energy_method_and_descriptor() -> None:
    _scene, run, _eid = adapter(_load_raw())

    assert isinstance(run, ExternalRun)
    # energy is always the DFT total — the stated reactionEnergy rides in
    # references, never here (slice A1).
    assert run.energy == -321.456
    assert run.max_force is None  # Catalysis-Hub doesn't expose per-system forces
    assert run.final_geometry is not None
    assert run.final_geometry["numbers"][:3] == [46, 46, 46]
    # method holds the comparability fingerprint only — the record's
    # descriptors moved to `descriptor`.
    assert run.method == {
        "functional": "BEEF-vdW",
        "code": "Quantum ESPRESSO",
        "cutoff_eV": None,
        "kmesh": None,
        "spin": None,
        "dataset_doi": "10.1021/acscatal.9b02179",
    }
    assert run.descriptor == {
        "facet": "111",
        "surface_composition": "Pd",
        "reactants": {"NOgas": 1, "star": 1},
        "products": {"NOstar": 1},
    }
    assert run.provenance == "external"


def test_adapter_typed_reference_on_the_adsorbate_row() -> None:
    _scene, run, _eid = adapter(_load_raw())

    (ref,) = run.references
    assert isinstance(ref, Reference)
    # star + NOgas -> NOstar, stoichiometry 1: a bare adsorption.
    assert ref.quantity == "adsorption_energy"
    assert ref.value_eV == -1.85
    assert ref.equation == {
        "reactants": [
            {"name": "NOgas", "state": "gas", "stoich": 1},
            {"name": "star", "state": "star", "stoich": 1},
        ],
        "products": [{"name": "NOstar", "state": "star", "stoich": 1}],
    }
    assert ref.members == ("PdNO111top-a1b2c3",)


def test_adapter_members_come_from_member_ids() -> None:
    raw = _load_raw()
    raw["member_ids"] = ["slab-uid", "gas-uid", "ads-uid"]
    _scene, run, _eid = adapter(raw)
    (ref,) = run.references
    assert ref.members == ("slab-uid", "gas-uid", "ads-uid")


def test_adapter_member_rows_carry_no_reference() -> None:
    for name in ("star", "NOgas"):
        raw = _load_raw()
        raw["name"] = name
        _scene, run, _eid = adapter(raw)
        assert run.references == ()
        assert run.energy == -321.456


def test_adapter_activation_energy_is_its_own_reference() -> None:
    raw = _load_raw()
    raw["activationEnergy"] = 0.92
    _scene, run, _eid = adapter(raw)
    assert [r.quantity for r in run.references] == [
        "adsorption_energy",
        "activation_energy",
    ]
    assert run.references[1].value_eV == 0.92


def test_adapter_multi_species_reaction_stays_reaction_energy() -> None:
    raw = _load_raw()
    # a two-gas coadsorption: still a product row, but not the bare
    # star + Xgas -> Xstar shape, so the quantity stays reaction_energy.
    raw["reactants"] = {"star": 1, "NOgas": 1, "H2gas": 1}
    raw["products"] = {"NOstar": 1, "H2star": 1}
    _scene, run, _eid = adapter(raw)
    (ref,) = run.references
    assert ref.quantity == "reaction_energy"


def test_adapter_multi_adsorbate_reaction_is_reaction_energy_on_each_product() -> None:
    """GRIPE_FIX ruling: with more than one adsorbate product the stated
    energy is NOT an adsorption energy of either product — it is a
    ``reaction_energy`` reference carried on EACH product row."""
    raw = _load_raw()
    raw["reactants"] = {"star": 1, "Ngas": 1, "Ogas": 1}
    raw["products"] = {"Nstar": 1, "Ostar": 1}
    for name in ("Nstar", "Ostar"):
        _scene, run, _eid = adapter({**raw, "name": name})
        (ref,) = run.references
        assert ref.quantity == "reaction_energy"


def test_adapter_no_total_energy_leaves_energy_none() -> None:
    raw = _load_raw()
    raw["system"]["energy"] = None
    _scene, run, _eid = adapter(raw)
    assert run.energy is None


def test_adapter_external_id_uses_system_unique_id() -> None:
    _scene, _run, eid = adapter(_load_raw())
    assert eid == ExternalId(dataset="catalysis-hub", config_id="PdNO111top-a1b2c3")


def test_catalysis_hub_adapter_is_registered() -> None:
    assert get_adapter("catalysis-hub") is adapter


def test_reactions_query_escapes_filter_values_as_graphql_strings() -> None:
    """A caller-supplied filter is a *value*, never query text — embedded
    via ``json.dumps``, so a quote inside it cannot break out of the string
    literal and smuggle a second filter clause into the query (the raw
    f-string interpolation let it)."""
    q = _reactions_query(first=5, surface_composition='Pd", facet: "111', facet=None)

    # one escaped literal, not two clauses — the whole hostile value stays
    # inside its quotes.
    assert 'surfaceComposition: "Pd\\", facet: \\"111"' in q
    assert q.count("facet:") == 1


def test_reactions_query_plain_filters_render_as_before() -> None:
    q = _reactions_query(first=5, surface_composition="Pd", facet="111")
    assert 'surfaceComposition: "Pd"' in q
    assert 'facet: "111"' in q
    assert "reactions(first: 5," in q
