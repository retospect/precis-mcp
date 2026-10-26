"""Batch-mirror a local cathub ``.db`` into ``structure`` refs + references.

Catalysis-Hub's live channels are all credential-gated now, but a cathub
``.db`` file is a self-contained, keyless data package. This is the
"bulk-download-and-mine-local" ingress: a local file -> the shared
``catalysis-hub`` adapter -> ``structure_import`` -> searchable, cited
``structure`` refs the catalyst quest can explore and relax.

The fixture ``cathub_no_pd.db`` is a real (ASE-built) cathub-format file: two
reactions — NO adsorption on Pd(111) and CO adsorption on Cu(111) — each with a
clean slab (``star``), a gas reference (``NOgas``/``COgas``), and the product
adsorbate system (``NOstar``/``COstar``). Slice A1 imports **every member
system** (the reference's ``members`` must resolve in the store): six refs
in all, each run carrying its own DFT total energy, and one
``adsorption_energy`` ``measures`` row on each adsorbate ref. Elements are
all inside the EMT set so the imported geometry relaxes torch-free.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

pytest.importorskip("ase")  # the [import] extra — reader + emt relax need it

from precis.structure.importers import cathub_db
from precis.structure.relax import relax

FIXTURE = str(Path(__file__).parent / "fixtures" / "catalysis" / "cathub_no_pd.db")
_JSON_FIXTURE = Path(__file__).parent / "fixtures" / "catalysis" / "pd111_no.json"


_SLAB_CELL = [[2.7, 0.0, 0.0], [0.0, 2.7, 0.0], [0.0, 0.0, 20.0]]
_SLAB_XY = [[0.0, 0.0, 0.0], [0.9, 0.0, 0.0], [0.0, 0.9, 3.0], [0.9, 0.9, 3.0]]


def _atoms(symbols, positions, cell, pbc, energy):
    from ase import Atoms
    from ase.calculators.singlepoint import SinglePointCalculator

    a = Atoms(symbols, positions=positions, cell=cell, pbc=pbc)
    a.calc = SinglePointCalculator(a, energy=energy)
    return a


def _cathub_tables(conn):
    conn.execute(
        "CREATE TABLE publication (id INTEGER PRIMARY KEY, pub_id TEXT, doi TEXT)"
    )
    conn.execute(
        "CREATE TABLE reaction (id INTEGER PRIMARY KEY, surface_composition TEXT, "
        "facet TEXT, reactants TEXT, products TEXT, reaction_energy REAL, "
        "activation_energy REAL, dft_code TEXT, dft_functional TEXT, pub_id TEXT)"
    )
    conn.execute(
        "CREATE TABLE reaction_system "
        "(name TEXT, energy_correction REAL, ase_id TEXT, id INTEGER)"
    )
    conn.execute(
        "INSERT INTO publication (pub_id, doi) "
        "VALUES ('MamunHighT2019', '10.1021/acscatal.9b02179')"
    )


def _build_shared_slab_db(path: Path) -> dict[str, str]:
    """A minimal cathub ``.db`` where two reactions share the clean slab
    AND the NOgas reference — the layout real Catalysis-Hub exports have.
    Returns the ASE ``unique_id`` of each written system by role."""
    from ase.db import connect as ase_connect

    adb = ase_connect(str(path))
    ids: dict[str, str] = {}
    systems = (
        ("slab", _atoms("Pd4", _SLAB_XY, _SLAB_CELL, [True, True, False], -321.0)),
        ("gas", _atoms("NO", [[0, 0, 0], [0, 0, 1.2]], [15.0] * 3, False, -12.4)),
        (
            "nostar",
            _atoms(
                "Pd4NO",
                [*_SLAB_XY, [1.35, 1.35, 8.0], [1.35, 1.35, 9.2]],
                _SLAB_CELL,
                [True, True, False],
                -334.9,
            ),
        ),
        (
            "no2star",
            _atoms(
                "Pd4NO2",
                [*_SLAB_XY, [1.35, 1.35, 8.0], [1.35, 1.35, 9.2], [0.5, 0.5, 8.6]],
                _SLAB_CELL,
                [True, True, False],
                -336.1,
            ),
        ),
    )
    for key, a in systems:
        row_id = adb.write(a)
        ids[key] = adb.get(id=row_id).unique_id

    conn = sqlite3.connect(str(path))
    try:
        _cathub_tables(conn)
        for rid, products, energy in (
            (1, '{"NOstar": 1}', -1.85),
            (2, '{"NO2star": 1}', -0.90),
        ):
            conn.execute(
                "INSERT INTO reaction (id, surface_composition, facet, reactants, "
                "products, reaction_energy, dft_code, dft_functional, pub_id) "
                "VALUES (?, 'Pd', '111', '{\"star\": 1, \"NOgas\": 1}', ?, ?, "
                "'Quantum ESPRESSO', 'BEEF-vdW', 'MamunHighT2019')",
                (rid, products, energy),
            )
        for rid, name, ase_id in (
            (1, "star", ids["slab"]),
            (1, "NOgas", ids["gas"]),
            (1, "NOstar", ids["nostar"]),
            (2, "star", ids["slab"]),
            (2, "NOgas", ids["gas"]),
            (2, "NO2star", ids["no2star"]),
        ):
            conn.execute(
                "INSERT INTO reaction_system VALUES (?, 0.0, ?, ?)",
                (name, ase_id, rid),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def _build_chain_db(path: Path) -> dict[str, str]:
    """A cathub ``.db`` where an ADSORBATE is shared across reactions —
    the chain chemistry the NRR quest wants::

        R1: star + NOgas -> NOstar          (-1.85 adsorption)
        R2: NOstar + star -> Nstar + Ostar  (+0.40 reaction, +1.20 barrier)
        R3: Nstar + Ostar -> NOstar         (-0.40 reaction)

    ``NOstar`` is R1's adsorbate product, only a member (reactant) of R2,
    and R3's product again; ``Nstar``/``Ostar`` are R2's products and R3's
    reactants; the clean slab is shared by R1 and R2. Returns the ASE
    ``unique_id`` of each written system by role."""
    from ase.db import connect as ase_connect

    adb = ase_connect(str(path))
    ids: dict[str, str] = {}
    systems = (
        ("slab", _atoms("Pd4", _SLAB_XY, _SLAB_CELL, [True, True, False], -321.0)),
        ("gas", _atoms("NO", [[0, 0, 0], [0, 0, 1.2]], [15.0] * 3, False, -12.4)),
        (
            "nostar",
            _atoms(
                "Pd4NO",
                [*_SLAB_XY, [1.35, 1.35, 8.0], [1.35, 1.35, 9.2]],
                _SLAB_CELL,
                [True, True, False],
                -334.9,
            ),
        ),
        (
            "nstar",
            _atoms(
                "Pd4N",
                [*_SLAB_XY, [1.35, 1.35, 8.0]],
                _SLAB_CELL,
                [True, True, False],
                -325.0,
            ),
        ),
        (
            "ostar",
            _atoms(
                "Pd4O",
                [*_SLAB_XY, [0.5, 0.5, 8.2]],
                _SLAB_CELL,
                [True, True, False],
                -323.0,
            ),
        ),
    )
    for key, a in systems:
        row_id = adb.write(a)
        ids[key] = adb.get(id=row_id).unique_id

    conn = sqlite3.connect(str(path))
    try:
        _cathub_tables(conn)
        for rid, reactants, products, energy, barrier in (
            (1, '{"star": 1, "NOgas": 1}', '{"NOstar": 1}', -1.85, None),
            (2, '{"NOstar": 1, "star": 1}', '{"Nstar": 1, "Ostar": 1}', 0.40, 1.20),
            (3, '{"Nstar": 1, "Ostar": 1}', '{"NOstar": 1}', -0.40, None),
        ):
            conn.execute(
                "INSERT INTO reaction (id, surface_composition, facet, reactants, "
                "products, reaction_energy, activation_energy, dft_code, "
                "dft_functional, pub_id) VALUES (?, 'Pd', '111', ?, ?, ?, ?, "
                "'Quantum ESPRESSO', 'BEEF-vdW', 'MamunHighT2019')",
                (rid, reactants, products, energy, barrier),
            )
        for rid, name, ase_id in (
            (1, "star", ids["slab"]),
            (1, "NOgas", ids["gas"]),
            (1, "NOstar", ids["nostar"]),
            (2, "NOstar", ids["nostar"]),
            (2, "star", ids["slab"]),
            (2, "Nstar", ids["nstar"]),
            (2, "Ostar", ids["ostar"]),
            (3, "Nstar", ids["nstar"]),
            (3, "Ostar", ids["ostar"]),
            (3, "NOstar", ids["nostar"]),
        ):
            conn.execute(
                "INSERT INTO reaction_system VALUES (?, 0.0, ?, ?)",
                (name, ase_id, rid),
            )
        conn.commit()
    finally:
        conn.close()
    return ids


def _external_run(store, ref_id):
    runs = [r for r in store.structure_runs(ref_id) if r["fidelity"] == "external"]
    assert len(runs) == 1, f"expected exactly one external run, got {runs}"
    return runs[0]


def _refs_with_measures(store, ref_ids):
    """The adsorbate refs — the ones carrying a reference ``measures`` row."""
    return [r for r in ref_ids if store.measures_for(r)]


def _run_row(store, ref_id):
    with store.pool.connection() as conn:
        return conn.execute(
            "SELECT provenance, energy, method, descriptor FROM struct_runs "
            "WHERE ref_id = %s AND provenance = 'external'",
            (ref_id,),
        ).fetchone()


def test_batch_import_brings_in_every_member_system(store):
    summary = cathub_db.batch_import(store, FIXTURE)
    # two reactions -> 3 member systems each (star, Xgas, Xstar).
    assert summary.configs == 6
    assert summary.imported == 6
    assert summary.reused == 0
    assert summary.skipped == 0
    assert len(summary.ref_ids) == 6

    # each imported ref carries ONE external run with the system's own DFT
    # total energy — never the reaction energy.
    totals = sorted(_external_run(store, r)["energy"] for r in summary.ref_ids)
    assert totals == sorted([-321.0, -12.4, -334.9, -200.0, -14.8, -215.4])


def test_import_records_the_method_fingerprint_and_descriptor(store):
    summary = cathub_db.batch_import(store, FIXTURE)
    (no_pd,) = [
        r
        for r in _refs_with_measures(store, summary.ref_ids)
        if store.measures_for(r)[0]["meta"]["value_eV"] == -1.85
    ]

    provenance, _energy, method, descriptor = _run_row(store, no_pd)
    assert provenance == "external"
    # method keeps only method fields — a pure comparability fingerprint.
    assert method["functional"] == "BEEF-vdW"
    assert method["code"] == "Quantum ESPRESSO"
    assert method["dataset_doi"] == "10.1021/acscatal.9b02179"
    assert "facet" not in method and "reactants" not in method
    # the record's descriptors live in their own payload.
    assert descriptor["facet"] == "111"
    assert descriptor["surface_composition"] == "Pd"
    assert descriptor["products"] == {"NOstar": 1}


def test_references_land_as_measures_rows_whose_members_resolve(store):
    summary = cathub_db.batch_import(store, FIXTURE)
    adsorbates = _refs_with_measures(store, summary.ref_ids)
    assert len(adsorbates) == 2

    by_value = {}
    for ref_id in adsorbates:
        (m,) = store.measures_for(ref_id)
        assert m["direction"] == "output"
        assert m["tier"] == "computed"
        assert m["reported_unit"] == "eV"
        assert m["measurand"] == "adsorption energy"
        assert m["display_unit"] == "eV"
        meta = m["meta"]
        assert meta["quantity"] == "adsorption_energy"
        assert meta["dataset"] == "catalysis-hub"
        assert meta["dataset_doi"] == "10.1021/acscatal.9b02179"
        assert len(meta["members"]) == 3
        # the equation and the record's descriptor ride in meta.
        assert meta["equation"]["products"] == [
            {"name": p, "state": "star", "stoich": 1}
            for p in meta["descriptor"]["products"]
        ]
        assert meta["descriptor"]["facet"] == "111"
        by_value[meta["value_eV"]] = meta

    assert sorted(by_value) == [-1.85, -0.61]
    # every member config_id resolves to one of the imported refs.
    for meta in by_value.values():
        for config_id in meta["members"]:
            resolved = store.find_ref_by_identifier(
                "catalysis-hub", config_id, kind="structure"
            )
            assert resolved in summary.ref_ids


def test_filters_narrow_to_the_quest_slice(store):
    # surface filter: only Pd surfaces -> the NO/Pd reaction's 3 members.
    pd_only = cathub_db.batch_import(store, FIXTURE, surface_contains=["Pd"])
    assert pd_only.configs == 3
    assert pd_only.imported == 3

    # product filter: only NO adsorbates -> same single reaction's members.
    no_only = cathub_db.read_cathub_db(FIXTURE, product_contains=["NO"])
    assert len(no_only) == 3
    assert {r["surfaceComposition"] for r in no_only} == {"Pd"}
    assert {r["name"] for r in no_only} == {"star", "NOgas", "NOstar"}

    # facet filter that matches nothing -> empty.
    assert cathub_db.read_cathub_db(FIXTURE, facet="211") == []


def test_reimport_is_idempotent_no_duplicate_runs_or_measures(store):
    first = cathub_db.batch_import(store, FIXTURE)
    assert first.imported == 6 and first.reused == 0

    second = cathub_db.batch_import(store, FIXTURE)
    assert second.imported == 0
    assert second.reused == 6
    # same refs, and still exactly one external run each (no duplication).
    assert set(second.ref_ids) == set(first.ref_ids)
    for ref_id in second.ref_ids:
        _external_run(store, ref_id)  # asserts exactly one
    # and still exactly one live measure per adsorbate — no duplicate, no
    # spurious supersession.
    for ref_id in _refs_with_measures(store, second.ref_ids):
        assert len(store.measures_for(ref_id)) == 1
        assert len(store.measures_for(ref_id, include_superseded=True)) == 1


def test_changed_reference_value_supersedes_the_measure(store, monkeypatch):
    """An unchanged measure row is left alone on re-import; a changed
    stated value supersedes it — never two live rows for one reference."""
    first = cathub_db.batch_import(store, FIXTURE)

    records = cathub_db.read_cathub_db(FIXTURE)
    (no_star,) = [r for r in records if r["name"] == "NOstar"]
    # the stated energy rides in the record's `producing` entry now — the
    # top-level field is only the unmerged fallback.
    no_star["producing"][0]["reactionEnergy"] = -1.90
    monkeypatch.setattr(cathub_db, "read_cathub_db", lambda *a, **k: records)

    second = cathub_db.batch_import(store, FIXTURE)
    assert second.reused == 6

    (no_pd,) = [
        r
        for r in _refs_with_measures(store, second.ref_ids)
        if store.measures_for(r)[0]["meta"]["value_eV"] == -1.90
    ]
    live = store.measures_for(no_pd)
    assert len(live) == 1
    history = store.measures_for(no_pd, include_superseded=True)
    assert sorted(m["meta"]["value_eV"] for m in history) == [-1.90, -1.85]


def test_record_with_no_total_energy_is_skipped_and_counted(store, monkeypatch):
    # drop the total energy off one member record — it must not import.
    records = cathub_db.read_cathub_db(FIXTURE, surface_contains=["Pd"])
    assert len(records) == 3
    records[0]["system"]["energy"] = None
    monkeypatch.setattr(cathub_db, "read_cathub_db", lambda *a, **k: records)

    summary = cathub_db.batch_import(store, FIXTURE)
    assert summary.configs == 3
    assert summary.skipped == 1
    assert summary.imported == 2
    assert len(summary.ref_ids) == 2


def test_method_key_compares_methods_not_records(store):
    """Same dataset + functional => equal ``_method_key``; a different
    functional => unequal. Descriptors no longer collide two records of one
    method (slice A1)."""
    from precis.handlers.structure import _method_key

    records = cathub_db.read_cathub_db(FIXTURE)
    from precis.structure.importers.catalysis_hub import adapter

    runs = [adapter(r)[1] for r in records]
    keys = {_method_key({"provenance": "external", "method": r.method}) for r in runs}
    # two reactions, one dataset and functional => ONE method key.
    assert len(keys) == 1

    other = adapter({**records[0], "dftFunctional": "RPBE"})[1]
    assert _method_key({"provenance": "external", "method": other.method}) != keys.pop()


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
    (no_pd,) = [
        r
        for r in _refs_with_measures(store, summary.ref_ids)
        if store.measures_for(r)[0]["meta"]["value_eV"] == -1.85
    ]

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


def test_shared_member_systems_import_once_with_clean_descriptor(tmp_path, store):
    """Real cathub .db files share the clean slab and gas references across
    reactions. Each shared ase_id must emit (and so import) ONCE per batch —
    not once per reaction — and a shared system's descriptor carries no
    reaction-specific reactants/products (no last-writer-wins)."""
    db = tmp_path / "shared.db"
    ids = _build_shared_slab_db(db)

    records = cathub_db.read_cathub_db(str(db))
    # 4 distinct systems, not 6 (2 reactions x 3 members).
    assert len(records) == 4
    assert {r["name"] for r in records} == {"star", "NOgas", "NOstar", "NO2star"}

    shared = {r["name"]: r for r in records if r["name"] in ("star", "NOgas")}
    for r in shared.values():
        assert r["reactants"] is None
        assert r["products"] is None

    summary = cathub_db.batch_import(store, str(db))
    assert summary.configs == 4
    assert summary.imported == 4
    assert summary.reused == 0
    assert len(summary.ref_ids) == 4

    # the shared slab is one ref, one external run, descriptor cleared of
    # reaction terms (facet/composition still describe the surface).
    slab_ref = store.find_ref_by_identifier(
        "catalysis-hub", ids["slab"], kind="structure"
    )
    assert slab_ref in summary.ref_ids
    _external_run(store, slab_ref)
    _prov, _energy, _method, descriptor = _run_row(store, slab_ref)
    assert descriptor["reactants"] is None
    assert descriptor["products"] is None
    assert descriptor["facet"] == "111"
    assert descriptor["surface_composition"] == "Pd"

    # each reaction's adsorbate still carries its own adsorption energy,
    # and its members resolve — the shared systems are in the store.
    adsorbates = _refs_with_measures(store, summary.ref_ids)
    assert len(adsorbates) == 2
    for ref_id in adsorbates:
        (m,) = store.measures_for(ref_id)
        assert m["measurand"] == "adsorption energy"
        assert len(m["meta"]["members"]) == 3
        for config_id in m["meta"]["members"]:
            resolved = store.find_ref_by_identifier(
                "catalysis-hub", config_id, kind="structure"
            )
            assert resolved in summary.ref_ids

    # a re-import collapses onto the same four refs — no version churn.
    second = cathub_db.batch_import(store, str(db))
    assert second.imported == 0 and second.reused == 4
    for ref_id in second.ref_ids:
        _external_run(store, ref_id)


def test_limit_counts_reactions_never_splits_members(tmp_path, store):
    """``limit`` applies per reaction — a truncated read never leaves a
    reaction's adsorbate record without its resolvable members."""
    one = cathub_db.read_cathub_db(FIXTURE, limit=1)
    assert {r["name"] for r in one} == {"star", "NOgas", "NOstar"}

    db = tmp_path / "shared.db"
    _build_shared_slab_db(db)
    limited = cathub_db.read_cathub_db(str(db), limit=1)
    # one selected reaction, minus the (not-yet-emitted) shared members is
    # still the full member set — dedup only bites across reactions.
    assert {r["name"] for r in limited} == {"star", "NOgas", "NOstar"}

    summary = cathub_db.batch_import(store, str(db), limit=1)
    assert summary.configs == 3
    assert summary.imported == 3
    (adsorbate,) = _refs_with_measures(store, summary.ref_ids)
    (m,) = store.measures_for(adsorbate)
    for config_id in m["meta"]["members"]:
        resolved = store.find_ref_by_identifier(
            "catalysis-hub", config_id, kind="structure"
        )
        assert resolved in summary.ref_ids


def test_multi_adsorbate_reaction_energy_lands_on_each_product_ref(store):
    """GRIPE_FIX ruling: a reaction with more than one adsorbate product
    does NOT carry an adsorption energy of either — the stated energy is a
    ``reaction_energy`` reference written as a 'reaction energy' measure on
    each product ref (this also exercises the taxon minted on first use:
    dimensioned SI energy, rooted under ``measurand``)."""
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler
    from precis.structure.importers.catalysis_hub import adapter

    # the seeded measurand start node a real database carries (refs are
    # truncated per test, so the 0174 seed row is absent here).
    TaxonHandler(hub=Hub(store=store)).put(
        text="measurand — a quantity that can be measured",
        meta={"start": True, "contract": {"required_keys": ["dimension_kind"]}},
    )

    raw = json.loads(_JSON_FIXTURE.read_text(encoding="utf-8"))
    raw["reactants"] = {"star": 1, "Ngas": 1, "Ogas": 1}
    raw["products"] = {"Nstar": 1, "Ostar": 1}
    raw["member_ids"] = ["slab-uid", "n-star", "o-star", "ngas-uid", "ogas-uid"]
    for name, uid in (("Nstar", "n-star"), ("Ostar", "o-star")):
        row = dict(raw)
        row["name"] = name
        row["system"] = {**raw["system"], "uniqueId": uid}
        scene, run, eid = adapter(row)
        assert [r.quantity for r in run.references] == ["reaction_energy"]
        ref_id = store.structure_import(scene, run, eid)
        (m,) = store.measures_for(ref_id)
        assert m["measurand"] == "reaction energy"
        assert m["meta"]["quantity"] == "reaction_energy"
        assert m["meta"]["value_eV"] == pytest.approx(-1.85)
        # the measurand minted on first use is a rooted, dimensioned taxon.
        taxon = store.get_ref(kind="taxon", id=m["measurand_ref_id"])
        assert taxon.meta["dimension_kind"] == "si"
        assert taxon.meta["si_vector"] == "2,1,-2,0,0,0,0"
        assert taxon.meta["canonical_unit"] == "J"
        assert taxon.meta["display_unit"] == "eV"
        parents = store.taxon_parents(taxon.id)
        assert parents, "measurand taxon must be rooted"
        parent = store.get_ref(kind="taxon", id=parents[0][0])
        assert parent.meta["slug"] == "measurand"


def test_shared_adsorbate_keeps_every_producing_reaction(tmp_path, store):
    """An adsorbate shared across reactions still emits once, but its one
    record merges the references of every selected reaction that produces
    it — ``NOstar``'s -1.85 adsorption energy must not vanish because the
    dissociation reaction also lists the system as a member."""
    from precis.dispatch import Hub
    from precis.handlers.taxon import TaxonHandler
    from precis.structure.importers.catalysis_hub import adapter

    # the seeded measurand start node a real database carries (refs are
    # truncated per test, so the 0174 seed row is absent here).
    TaxonHandler(hub=Hub(store=store)).put(
        text="measurand — a quantity that can be measured",
        meta={"start": True, "contract": {"required_keys": ["dimension_kind"]}},
    )

    db = tmp_path / "chain.db"
    ids = _build_chain_db(db)

    records = cathub_db.read_cathub_db(str(db))
    # 5 distinct systems over 3 reactions — each emits once, and only the
    # shared SLAB loses its reaction terms (a gas used once keeps them).
    assert len(records) == 5
    by_name = {r["name"]: r for r in records}
    assert by_name["star"]["reactants"] is None
    assert by_name["star"]["products"] is None
    assert by_name["NOgas"]["products"] == {"NOstar": 1}

    # NOstar is produced by R1 AND R3: merged record, two reference sets,
    # descriptor terms from a reaction that produces it.
    nostar = by_name["NOstar"]
    assert nostar["products"] == {"NOstar": 1}
    assert len(nostar["producing"]) == 2
    _scene, run, _eid = adapter(nostar)
    assert [(r.quantity, r.value_eV) for r in run.references] == [
        ("adsorption_energy", -1.85),
        ("reaction_energy", -0.40),
    ]

    # Nstar is only a member of R3 — its terms come from R2, the reaction
    # that produces it (with R2's barrier riding along).
    nstar = by_name["Nstar"]
    assert nstar["products"] == {"Nstar": 1, "Ostar": 1}
    assert len(nstar["producing"]) == 1
    _scene, nrun, _eid = adapter(nstar)
    assert [(r.quantity, r.value_eV) for r in nrun.references] == [
        ("reaction_energy", 0.40),
        ("activation_energy", 1.20),
    ]

    summary = cathub_db.batch_import(store, str(db))
    assert summary.configs == 5
    assert summary.imported == 5

    def ref(key):
        return store.find_ref_by_identifier("catalysis-hub", ids[key], kind="structure")

    # every reference lands as its own live measure row.
    nostar_measures = store.measures_for(ref("nostar"))
    assert sorted(
        (m["meta"]["quantity"], m["meta"]["value_eV"]) for m in nostar_measures
    ) == [("adsorption_energy", -1.85), ("reaction_energy", -0.40)]
    assert {m["measurand"] for m in nostar_measures} == {
        "adsorption energy",
        "reaction energy",
    }
    for key in ("nstar", "ostar"):
        measures = store.measures_for(ref(key))
        assert sorted(
            (m["meta"]["quantity"], m["meta"]["value_eV"]) for m in measures
        ) == [("activation_energy", 1.20), ("reaction_energy", 0.40)]

    # each measure's member config_ids resolve to an imported ref.
    for key in ("nostar", "nstar", "ostar"):
        for m in store.measures_for(ref(key)):
            for config_id in m["meta"]["members"]:
                resolved = store.find_ref_by_identifier(
                    "catalysis-hub", config_id, kind="structure"
                )
                assert resolved in summary.ref_ids

    # a re-import collapses cleanly — no extra runs or measure churn.
    second = cathub_db.batch_import(store, str(db))
    assert second.imported == 0 and second.reused == 5
    assert len(store.measures_for(ref("nostar"))) == 2
