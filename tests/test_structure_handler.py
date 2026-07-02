"""StructureHandler end-to-end against a live store (ADR 0043 increment 2).

Exercises the DB round-trip: author via put, read the TOC, probe an atom /
neighbourhood / bonds / the validator, apply an op via edit, and soft-delete.
Uses the same ``store`` fixture every DB-backed handler test uses (auto
``pytest.mark.db``).
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.structure import StructureHandler

_PD = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {"op": "add_bond", "i": "aPd1", "j": "aPd2", "order": 1},
        ],
    }
)


@pytest.fixture
def structure(store):
    return StructureHandler(hub=Hub(store=store))


def test_put_creates_lists_and_round_trips(structure):
    resp = structure.put(id="pd_pair", text=_PD)
    assert "created" in resp.body
    assert "Pd2" in resp.body and "aPd1" in resp.body and "pbc[TTF]" in resp.body
    # listing shows it
    assert "pd_pair" in structure.get().body
    # TOC reloads from the DB
    toc = structure.get(id="pd_pair")
    assert "Pd2" in toc.body and "1 bonds" in toc.body


def test_atom_and_neighborhood_and_bonds_probes(structure):
    structure.put(id="pd_pair", text=_PD)
    atom = structure.get(id="pd_pair", view="atom", args={"atom": "aPd1"})
    assert "aPd2" in atom.body and "2.6" in atom.body
    nb = structure.get(
        id="pd_pair", view="neighborhood", args={"center": "aPd1", "radius": 3.0}
    )
    assert "aPd2" in nb.body
    bonds = structure.get(id="pd_pair", view="bonds")
    assert "aPd1" in bonds.body and "aPd2" in bonds.body


def test_edit_applies_ops_and_persists(structure):
    structure.put(id="pd_pair", text=_PD)
    resp = structure.edit(
        id="pd_pair",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    )
    assert "edited" in resp.body and "aO1" in resp.body
    # persisted: a fresh TOC shows the O
    assert "O1" in structure.get(id="pd_pair").body


def test_validate_view_flags_overlap(structure):
    bad = json.dumps(
        {
            "cell": {"a": 10.0, "b": 10.0, "c": 10.0},
            "ops": [
                {"op": "add_atom", "element": "H", "frac": [0.0, 0.0, 0.0]},
                {"op": "add_atom", "element": "H", "frac": [0.03, 0.0, 0.0]},
            ],
        }
    )
    structure.put(id="clash", text=bad)
    findings = structure.get(id="clash", view="validate")
    assert "atom_overlap" in findings.body


def test_bad_payload_and_missing_atom_raise(structure):
    with pytest.raises(BadInput):
        structure.put(id="nope", text="{not json")
    with pytest.raises(BadInput):
        structure.put(id="nocell", text=json.dumps({"ops": []}))
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(NotFound):
        structure.get(id="pd_pair", view="atom", args={"atom": "aXx9"})


def test_delete_retires(structure):
    structure.put(id="pd_pair", text=_PD)
    out = structure.delete(id="pd_pair")
    assert "retired" in out.body
    # gone from the listing
    assert "pd_pair" not in structure.get().body


def test_search_finds_by_description(structure):
    spec = json.loads(_PD)
    spec["description"] = "a palladium dimer for adsorption screening"
    structure.put(id="pd_pair", text=json.dumps(spec))
    resp = structure.search(q="adsorption screening", mode="lexical")
    assert "pd_pair" in resp.body
    # search_hits feeds the cross-kind merge
    hits = structure.search_hits(q="adsorption screening", mode="lexical")
    assert hits and hits[0].kind == "structure" and hits[0].slug == "pd_pair"


def test_search_requires_q(structure):
    with pytest.raises(BadInput):
        structure.search()


def test_export_views(structure):
    from precis.errors import Unsupported
    from precis.structure import export

    structure.put(id="pd_pair", text=_PD)
    poscar = structure.get(id="pd_pair", view="poscar")
    assert "Direct" in poscar.body and "Pd2" in poscar.body
    xyz = structure.get(id="pd_pair", view="extxyz")
    assert "Lattice=" in xyz.body and "aPd1" in xyz.body and 'pbc="T T F"' in xyz.body
    # CIF is ASE-gated
    if export.ase_available():
        assert "data_" in structure.get(id="pd_pair", view="cif").body.lower()
    else:
        with pytest.raises(Unsupported):
            structure.get(id="pd_pair", view="cif")


def test_relax_clean_via_edit(structure):
    structure.put(id="pd_pair", text=_PD)  # Pd-Pd at 2.6 Å (< covalent 2.78)
    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "clean"}])
    assert "relax[clean]" in resp.body and "converged" in resp.body
    # the relax summary persists on the design and shows on a fresh TOC
    assert "relax[clean]" in structure.get(id="pd_pair").body


def test_relax_ml_rung_dispatches_without_a_todo(structure):
    """An energy rung with no local backend is *derived compute* (ADR 0044):
    it dispatches a struct_relax job parented on the structure itself — no
    todo required — instead of raising. (Pre-0044 this rung raised Unsupported
    demanding a parent todo.)"""
    structure.put(id="pd_pair", text=_PD)
    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "dispatched" in resp.body and "view='runs'" in resp.body


def test_nav_views_line_fragments_pov(structure):
    structure.put(id="pd_pair", text=_PD)  # aPd1 at 0,0,0 ; aPd2 at 0.26,0,0 ; bonded
    # a ray down the x axis through both atoms
    line = structure.get(
        id="pd_pair",
        view="line",
        args={"origin": [0, 0, 0], "direction": [1, 0, 0], "radius": 0.5},
    )
    assert "aPd1" in line.body and "aPd2" in line.body
    # one bonded fragment of size 2
    frags = structure.get(id="pd_pair", view="fragments")
    assert "1 fragment" in frags.body and "Pd2" in frags.body
    # embodiment readout
    pov = structure.get(
        id="pd_pair", view="pov", args={"support": "aPd1", "reach": 3.0}
    )
    assert "i_am=atom" in pov.body and "aPd2" in pov.body


def test_diff_view_compares_two_designs(structure):
    structure.put(id="pd_pair", text=_PD)
    moved = json.loads(_PD)
    moved["ops"][1]["frac"] = [0.40, 0.0, 0.0]  # push aPd2 out
    structure.put(id="pd_moved", text=json.dumps(moved))
    d = structure.get(id="pd_moved", view="diff", args={"other": "pd_pair"})
    assert "RMSD" in d.body and "aPd2" in d.body


def test_clean_relax_records_a_run(structure):
    structure.put(id="pd_pair", text=_PD)
    # no runs before a relax
    assert "no compute runs yet" in structure.get(id="pd_pair", view="runs").body
    structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "clean"}])
    runs = structure.get(id="pd_pair", view="runs")
    assert "1 compute run" in runs.body and "clean" in runs.body
    # clean has no energy — it's undefined, shown as the em-dash, not 0
    assert "—" in runs.body


def test_clean_rung_is_never_cached(structure):
    # clean is instant + pure + energy-free: it records a run but stamps no
    # cache_key (ADR §23.16), so the cube never grows a clean cache entry.
    structure.put(id="pd_pair", text=_PD)
    structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "clean"}])
    ref = structure.store.get_ref(kind="structure", id="pd_pair")
    with structure.store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM struct_runs "
            "WHERE ref_id = %s AND cache_key IS NOT NULL",
            (ref.id,),
        ).fetchone()[0]
    assert n == 0


def test_cache_hit_short_circuits_a_gated_rung(structure):
    """A pre-seeded run-cube entry makes the (otherwise gated) ``ml`` rung a
    zero-compute hit — it returns the cached envelope instead of raising
    Unsupported, proving the cache short-circuits *before* the backend."""
    from precis.structure import cache as relax_cache

    structure.put(id="pd_pair", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair")
    scene, _ = structure.store.structure_load(ref.id)

    # Seed the cube as if an ml relax had already converged on this geometry,
    # relaxing aPd2 from 0.26 → 0.24.
    order = relax_cache.canonical_order(scene)
    key = relax_cache.run_cache_key(
        scene, fidelity="ml", model="mace_mp", params={"steps": 200}
    )
    sha = relax_cache.structure_sha(scene)
    relaxed = structure.store.structure_load(ref.id)[0]
    relaxed.atoms["aPd2"].frac = np.array([0.24, 0.0, 0.0])
    structure.store.structure_record_run(
        ref.id,
        fidelity="ml",
        on_version=structure.store.structure_version(ref.id),
        converged=True,
        n_steps=7,
        max_disp=0.02,
        energy=-3.21,
        max_force=0.04,
        model="mace_mp",
        curve=[0.5, 0.1, 0.04],
        cache_key=key,
        structure_sha=sha,
        final_geometry=relax_cache.serialize_geometry(relaxed, order),
    )

    # ml would normally raise Unsupported (no MACE installed); the cache hits.
    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "relax[ml]" in resp.body and "converged" in resp.body
    # the cached relaxed geometry was written back onto the design.
    reloaded, _ = structure.store.structure_load(ref.id)
    assert round(float(reloaded.atoms["aPd2"].frac[0]), 4) == 0.24


def test_store_cache_round_trip(structure):
    structure.put(id="pd_pair", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair")
    assert structure.store.structure_find_cached_run("nope") is None
    structure.store.structure_record_run(
        ref.id,
        fidelity="ml",
        on_version=1,
        converged=True,
        n_steps=5,
        max_disp=0.01,
        energy=-1.0,
        max_force=0.03,
        model="mace_mp",
        curve=[0.2, 0.03],
        cache_key="k123",
        structure_sha="sha123",
        final_geometry={"frac": [[0.0, 0.0, 0.0]], "lattice": None},
    )
    hit = structure.store.structure_find_cached_run("k123")
    assert hit is not None
    assert hit["converged"] is True and hit["model"] == "mace_mp"
    assert hit["curve"] == [0.2, 0.03]
    assert hit["final_geometry"] == {"frac": [[0.0, 0.0, 0.0]], "lattice": None}


def _child_jobs(store, parent_id: int) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job' "
            "AND deleted_at IS NULL ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_energy_rung_mints_a_struct_relax_job_parented_on_the_structure(
    structure, store
):
    """An energy rung that misses the cache and has no local backend dispatches
    a struct_relax job to the GPU node (ADR 0043 §23.12) — parented on the
    *structure*, not a todo (ADR 0044 compute lane) — carrying the content
    address + staged geometry the run-cube write-back needs."""
    structure.put(id="pd_pair", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair")

    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "dispatched" in resp.body and "view='runs'" in resp.body

    # The job hangs off the structure ref, not any todo.
    jobs = _child_jobs(store, ref.id)
    assert len(jobs) == 1
    meta = jobs[0]
    assert meta["job_type"] == "struct_relax" and meta["executor"] == "ssh_node"
    params = meta["params"]
    assert params["structure_ref_id"] == ref.id
    assert params["cache_key"] and params["structure_sha"]
    assert params["fidelity"] == "ml"
    assert "Pd" in params["poscar"]
    assert set(params["poscar_labels"]) == {"aPd1", "aPd2"}


def test_energy_rung_with_requester_wires_the_wait(structure, store):
    """``requested_by=<todo>`` on the relax op links the todo ``requested`` →
    the job and arms a ``derived_job_succeeded`` auto_check so the intentful
    caller (a planner tick, a human) blocks on the build (ADR 0044)."""
    from precis.dispatch import Hub
    from precis.handlers.todo import TodoHandler
    from tests.conftest import id_of

    structure.put(id="pd_pair", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair")
    todo = TodoHandler(hub=Hub(store=store)).put(text="relax pd_pair on spark")
    todo_id = id_of(todo.body)

    resp = structure.edit(
        id="pd_pair",
        ops=[{"op": "relax", "fidelity": "ml", "requested_by": todo_id}],
    )
    assert "dispatched" in resp.body and f"todo #{todo_id}" in resp.body

    # Job parents on the structure; the todo reaches it via ``requested``.
    jobs = _child_jobs(store, ref.id)
    assert len(jobs) == 1
    links = store.links_for(todo_id, direction="out", relation="requested")
    assert len(links) == 1
    job_row = store.get_ref(kind="job", id=links[0].dst_ref_id)
    assert job_row.meta["job_type"] == "struct_relax"

    # The wait is armed on the requester.
    todo_ref = store.get_ref(kind="todo", id=todo_id)
    assert todo_ref.meta["auto_check"] == {"type": "derived_job_succeeded"}
