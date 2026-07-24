"""Batch-mirror a local cathub ``.db`` into ``structure`` refs (ADR 0053 §3).

Catalysis-Hub's live channels are all credential-gated now, but a cathub
``.db`` file is a self-contained, keyless data package. This is the
"bulk-download-and-mine-local" ingress: a local file -> the shared
``catalysis-hub`` adapter -> ``structure_import`` -> searchable, cited
``structure`` refs the catalyst quest can explore and relax.

The fixture ``cathub_no_pd.db`` is a real (ASE-built) cathub-format file: two
reactions — NO adsorption on Pd(111) and CO adsorption on Cu(111) — each with a
clean slab (``star``), a gas reference (``NOgas``/``COgas``), and the product
adsorbate system (``NOstar``/``COstar``). Only the product systems should
import; elements are all inside the EMT set so the imported geometry relaxes
torch-free.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("ase")  # the [import] extra — reader + emt relax need it

from precis.structure.importers import cathub_db
from precis.structure.relax import relax

FIXTURE = str(Path(__file__).parent / "fixtures" / "catalysis" / "cathub_no_pd.db")


def _external_run(store, ref_id):
    runs = [r for r in store.structure_runs(ref_id) if r["fidelity"] == "external"]
    assert len(runs) == 1, f"expected exactly one external run, got {runs}"
    return runs[0]


def _ref_for_energy(store, ref_ids, energy):
    """The imported ref whose external run carries ``energy`` (identifies which
    reaction, since config_ids are opaque ASE uuids)."""
    for ref_id in ref_ids:
        if _external_run(store, ref_id)["energy"] == pytest.approx(energy):
            return ref_id
    raise AssertionError(f"no imported ref with external energy {energy}")


def test_batch_import_brings_in_product_systems_only(store):
    summary = cathub_db.batch_import(store, FIXTURE)
    # two reactions -> two product adsorbate systems (NOstar, COstar); the
    # clean-slab (star) and gas-phase (NOgas/COgas) systems are skipped.
    assert summary.configs == 2
    assert summary.imported == 2
    assert summary.reused == 0
    assert len(summary.ref_ids) == 2

    # each imported ref is an ordinary structure carrying ONE external run whose
    # energy is the reaction (adsorption) energy.
    no_pd = _ref_for_energy(store, summary.ref_ids, -1.85)
    co_cu = _ref_for_energy(store, summary.ref_ids, -0.61)
    assert no_pd != co_cu

    ref = store.get_ref(kind="structure", id=no_pd)
    assert ref is not None


def test_import_records_the_method_fingerprint_and_external_provenance(store):
    summary = cathub_db.batch_import(store, FIXTURE)
    no_pd = _ref_for_energy(store, summary.ref_ids, -1.85)

    with store.pool.connection() as conn:
        provenance, method = conn.execute(
            "SELECT provenance, method FROM struct_runs "
            "WHERE ref_id = %s AND provenance = 'external'",
            (no_pd,),
        ).fetchone()
    assert provenance == "external"
    assert method["functional"] == "BEEF-vdW"
    assert method["code"] == "Quantum ESPRESSO"
    assert method["facet"] == "111"
    assert method["surface_composition"] == "Pd"
    assert method["dataset_doi"] == "10.1021/acscatal.9b02179"


def test_filters_narrow_to_the_quest_slice(store):
    # surface filter: only Pd surfaces -> just the NO/Pd reaction.
    pd_only = cathub_db.batch_import(store, FIXTURE, surface_contains=["Pd"])
    assert pd_only.configs == 1
    assert _external_run(store, pd_only.ref_ids[0])["energy"] == pytest.approx(-1.85)

    # product filter: only NO adsorbates -> same single config.
    no_only = cathub_db.read_cathub_db(FIXTURE, product_contains=["NO"])
    assert len(no_only) == 1
    assert no_only[0]["surfaceComposition"] == "Pd"

    # facet filter that matches nothing -> empty.
    assert cathub_db.read_cathub_db(FIXTURE, facet="211") == []


def test_reimport_is_idempotent_no_duplicate_runs(store):
    first = cathub_db.batch_import(store, FIXTURE)
    assert first.imported == 2 and first.reused == 0

    second = cathub_db.batch_import(store, FIXTURE)
    assert second.imported == 0
    assert second.reused == 2
    # same refs, and still exactly one external run each (no duplication).
    assert set(second.ref_ids) == set(first.ref_ids)
    for ref_id in second.ref_ids:
        _external_run(store, ref_id)  # asserts exactly one


def test_adsorbate_product_guard_is_direction_robust():
    # the adsorbate-on-slab product ('NOstar') is selected; a desorption-
    # direction reaction that lists the clean slab ('star') or a gas species
    # ('NOgas') as a product must NOT be mistaken for the substrate.
    from precis.structure.importers.cathub_db import _is_adsorbate_product

    assert _is_adsorbate_product("NOstar", {"NOstar"})
    assert _is_adsorbate_product("COstar", {"COstar", "star"})
    assert not _is_adsorbate_product("star", {"star", "NOstar"})
    assert not _is_adsorbate_product("NOgas", {"NOgas"})
    assert not _is_adsorbate_product("NOstar", {"star"})  # not a product here


def test_imported_substrate_relaxes_torch_free_emt(store):
    """The quest goal: pull a real DB substrate, then run a potential on it.
    Load the imported NO/Pd geometry back from the DB and relax it with the
    torch-free EMT rung (Pd/N/O are all in the EMT element set)."""
    summary = cathub_db.batch_import(store, FIXTURE)
    no_pd = _ref_for_energy(store, summary.ref_ids, -1.85)

    scene, _handles = store.structure_load(no_pd)
    before = {la: a.frac.copy() for la, a in scene.atoms.items()}

    res = relax(scene, fidelity="emt", steps=200, tol=0.1)
    assert res.rung == "emt"
    assert res.converged
    assert res.energy is not None and res.max_force is not None
    # the free (unfixed) atoms actually moved under the potential.
    import numpy as np

    moved = any(
        not np.allclose(before[la], scene.atoms[la].frac, atol=1e-6)
        for la in scene.atoms
    )
    assert moved
