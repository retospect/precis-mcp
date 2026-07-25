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
from precis.handlers.structure import StructureHandler, guard_energy_comparable

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


@pytest.fixture
def no_local_mlip(monkeypatch):
    """Force the ML relax backend absent so an ``ml`` rung takes the
    dispatch-to-GPU-node path (ADR 0044) deterministically. The gate container
    installs ``[dft-ml]`` (Dockerfile ``uv sync --all-extras``), so without
    this the handler would relax inline instead of minting a struct_relax job —
    which is only correct on a host that *has* the backend, not the data hosts
    these dispatch tests model."""
    import importlib

    # NB: the ``precis.structure`` package re-exports the ``relax`` *function*,
    # shadowing the submodule name — reach the module via importlib.
    relax_mod = importlib.import_module("precis.structure.relax")

    def _no_mlip(model):  # type: ignore[no-untyped-def]
        raise relax_mod.RelaxUnsupported("no MLIP backend (test)")

    monkeypatch.setattr(relax_mod, "_ml_calculator", _no_mlip)


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


def test_externally_stamped_meta_survives_an_edit(structure, store):
    # gr: structure_save's update path used to wholesale-replace refs.meta,
    # silently erasing anything a quest harvest pass stamped (barrier, span,
    # quest_harvested_upto, ...) the next time the design is edited.
    structure.put(id="pd_pair", text=_PD)
    ref = store.get_ref(kind="structure", id="pd_pair")
    store.stamp_ref_meta(ref.id, {"barrier": 0.9, "quest_harvested_upto": 3})
    structure.edit(
        id="pd_pair",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    )
    meta = store.get_ref(kind="structure", id="pd_pair").meta or {}
    assert meta.get("barrier") == 0.9
    assert meta.get("quest_harvested_upto") == 3
    # structure_save's own fields still refresh normally
    assert meta.get("version", 0) >= 1


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


def test_validate_view_flags_too_long_bond(structure):
    bad = json.dumps(
        {
            "cell": {"a": 10.0, "b": 10.0, "c": 10.0},
            "ops": [
                {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
                {"op": "add_atom", "element": "H", "frac": [0.4, 0.0, 0.0]},  # 4 Å
                {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            ],
        }
    )
    structure.put(id="longbond_view", text=bad)
    findings = structure.get(id="longbond_view", view="validate")
    assert "bond_too_long" in findings.body and "aO1" in findings.body


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


def test_relax_ml_rung_dispatches_without_a_todo(structure, no_local_mlip):
    """An energy rung with no local backend is *derived compute* (ADR 0044):
    it dispatches a struct_relax job parented on the structure itself — no
    todo required — instead of raising. (Pre-0044 this rung raised Unsupported
    demanding a parent todo.)"""
    structure.put(id="pd_pair", text=_PD)
    resp = structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "dispatched" in resp.body and "view='runs'" in resp.body


# ── pre-flight gate ahead of cloud dispatch (gripe 51393) ──────────────────


def _child_jobs2(store, parent_id: int) -> list:
    """Local twin of the module-level ``_child_jobs`` helper defined further
    down this file — kept name-distinct so these tests can sit next to the
    dispatch test they extend without a forward reference."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job' "
            "AND deleted_at IS NULL ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_dispatch_rejects_clashing_pair_and_mints_no_job(
    structure, store, no_local_mlip
):
    """The hard-reject gate (gripe 51393) runs before anything is staged for
    the GPU node: a sub-covalent clash raises BadInput naming the atom pair,
    and no struct_relax job is created."""
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
    ref = structure.store.get_ref(kind="structure", id="clash")
    with pytest.raises(BadInput) as exc:
        structure.edit(id="clash", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "aH1" in str(exc.value) and "aH2" in str(exc.value)
    assert _child_jobs2(store, ref.id) == []


def test_dispatch_rejects_over_valent_atom_and_mints_no_job(
    structure, store, no_local_mlip
):
    ops: list[dict] = [{"op": "add_atom", "element": "C", "frac": [0.5, 0.5, 0.5]}]
    for dx, dy, dz in [
        (0.09, 0, 0),
        (-0.09, 0, 0),
        (0, 0.09, 0),
        (0, -0.09, 0),
        (0, 0, 0.09),
    ]:
        ops.append(
            {"op": "add_atom", "element": "H", "frac": [0.5 + dx, 0.5 + dy, 0.5 + dz]}
        )
    structure.put(
        id="overval",
        text=json.dumps({"cell": {"a": 10.0, "b": 10.0, "c": 10.0}, "ops": ops}),
    )
    ref = structure.store.get_ref(kind="structure", id="overval")
    with pytest.raises(BadInput) as exc:
        structure.edit(id="overval", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "aC1" in str(exc.value)
    assert _child_jobs2(store, ref.id) == []


def test_dispatch_rejects_absurd_bond_length_and_mints_no_job(
    structure, store, no_local_mlip
):
    spec = json.dumps(
        {
            "cell": {"a": 10.0, "b": 10.0, "c": 10.0},
            "ops": [
                {"op": "add_atom", "element": "O", "frac": [0.0, 0.0, 0.0]},
                {"op": "add_atom", "element": "H", "frac": [0.4, 0.0, 0.0]},  # 4 Å
                {"op": "add_bond", "i": "aO1", "j": "aH1", "order": 1},
            ],
        }
    )
    structure.put(id="longbond", text=spec)
    ref = structure.store.get_ref(kind="structure", id="longbond")
    with pytest.raises(BadInput) as exc:
        structure.edit(id="longbond", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "aO1" in str(exc.value) and "aH1" in str(exc.value)
    assert _child_jobs2(store, ref.id) == []


def test_dispatch_still_succeeds_for_physical_geometry(structure, store, no_local_mlip):
    """The gate only blocks impossible geometry — an ordinary, physically
    fine design still dispatches exactly as before (no regression)."""
    structure.put(id="pd_pair_ok", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair_ok")
    resp = structure.edit(id="pd_pair_ok", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "dispatched" in resp.body
    assert len(_child_jobs2(store, ref.id)) == 1


def test_dispatch_preflight_cleans_geometry_before_staging(
    structure, store, no_local_mlip
):
    """A mild clash (below the hard-reject floor) is repaired by a local
    ``clean`` pre-relax before the POSCAR is staged for the cloud job (gripe
    51393): the saved design and the dispatched POSCAR both reflect the
    cleaned geometry, not the as-authored 2.6 Å Pd-Pd."""
    from precis.structure import export, probe

    structure.put(id="pd_pair_dirty", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair_dirty")
    structure.edit(id="pd_pair_dirty", ops=[{"op": "relax", "fidelity": "ml"}])

    # write-back: the design itself moved off the as-authored 2.6 Å.
    scene, _ = structure.store.structure_load(ref.id)
    assert probe.distance(scene, "aPd1", "aPd2") > 2.65

    # the staged POSCAR is exported from that same cleaned scene, not the
    # pre-preflight geometry.
    jobs = _child_jobs2(store, ref.id)
    assert len(jobs) == 1
    assert jobs[0]["params"]["poscar"] == export.to_poscar(scene)

    # a run recorded the pre-relax (write-back parity with an ordinary
    # local 'clean' relax — the run-cube isn't silently skipped).
    runs = structure.store.structure_runs(ref.id)
    assert any(r["fidelity"] == "clean" for r in runs)


def test_dispatch_cache_hit_after_preflight_short_circuits_no_job(
    structure, store, no_local_mlip
):
    """A design whose *preflight-cleaned* geometry matches an already-
    completed cached run must be a zero-compute hit — not a fresh cloud
    dispatch. The early cache lookup runs against the as-authored geometry
    and can't see this; the post-preflight re-check must (gripe 51393)."""
    from precis.structure import cache as relax_cache
    from precis.structure import relax as run_relax

    structure.put(id="pd_pair_cached", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair_cached")

    # What the preflight 'clean' pre-relax will produce: relax a copy of the
    # same as-authored geometry the same way _preflight_relax does.
    cleaned_scene, _ = structure.store.structure_load(ref.id)
    run_relax(cleaned_scene, fidelity="clean", steps=200)

    order = relax_cache.canonical_order(cleaned_scene)
    key = relax_cache.run_cache_key(
        cleaned_scene, fidelity="ml", model="mace_mp", params={"steps": 200}
    )
    sha = relax_cache.structure_sha(cleaned_scene)
    structure.store.structure_record_run(
        ref.id,
        fidelity="ml",
        on_version=structure.store.structure_version(ref.id),
        converged=True,
        n_steps=9,
        max_disp=0.01,
        energy=-5.55,
        max_force=0.02,
        model="mace_mp",
        curve=[0.3, 0.05, 0.01],
        cache_key=key,
        structure_sha=sha,
        final_geometry=relax_cache.serialize_geometry(cleaned_scene, order),
    )

    resp = structure.edit(id="pd_pair_cached", ops=[{"op": "relax", "fidelity": "ml"}])
    assert "relax[ml]" in resp.body and "converged" in resp.body
    assert "dispatched" not in resp.body
    assert _child_jobs2(store, ref.id) == []


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


# -- per-atom forces (gripe 161576) ------------------------------------------


def test_migration_0087_columns_exist(structure):
    """The tail migration applied cleanly — struct_runs carries the new
    nullable forces/charges columns (the harness auto-applies migrations at
    fixture setup; this just confirms the columns are queryable)."""
    with structure.store.pool.connection() as conn:
        row = conn.execute("SELECT forces, charges FROM struct_runs LIMIT 0").fetchone()
    assert row is None  # no rows yet — the point is the query didn't error


def test_emt_relax_records_per_atom_forces_shown_in_atom_view(structure):
    """A real 'emt' relax (ASE-EMT, no MACE needed) records a per-atom force
    array of the right length; view='atom' surfaces |F| for one atom, tagged
    'computed' (not 'approx')."""
    pytest.importorskip("ase")
    structure.put(id="pd_pair_emt", text=_PD)
    structure.edit(id="pd_pair_emt", ops=[{"op": "relax", "fidelity": "emt"}])
    ref = structure.store.get_ref(kind="structure", id="pd_pair_emt")
    runs = structure.store.structure_runs(ref.id)
    emt_run = next(r for r in runs if r["fidelity"] == "emt")
    assert emt_run["forces"] is not None
    vectors = emt_run["forces"]["vectors"]
    labels = emt_run["forces"]["labels"]
    assert len(vectors) == 2 and len(labels) == 2  # aPd1, aPd2
    assert set(labels) == {"aPd1", "aPd2"}  # label-paired (gripe 161576 FIX 1)
    assert emt_run["forces"]["approx"] is False
    assert emt_run["forces"]["source"] == "emt"

    atom_view = structure.get(id="pd_pair_emt", view="atom", args={"atom": "aPd1"})
    assert "|F| =" in atom_view.body
    assert "computed" in atom_view.body
    assert "Charges: —" in atom_view.body


def test_view_atom_run_arg_selects_a_specific_run(structure):
    """``run=<id>`` pins the atom-view force readout to one recorded run,
    not just the latest force-bearing one."""
    pytest.importorskip("ase")
    structure.put(id="pd_pair_runs", text=_PD)
    structure.edit(id="pd_pair_runs", ops=[{"op": "relax", "fidelity": "emt"}])
    ref = structure.store.get_ref(kind="structure", id="pd_pair_runs")
    first_run_id = structure.store.structure_runs(ref.id)[0]["id"]

    # a second op-free 'clean' relax records another run (a no-op geometry
    # repair, since emt already converged the pair) so there's a later run
    # to disambiguate against.
    structure.edit(id="pd_pair_runs", ops=[{"op": "relax", "fidelity": "clean"}])

    pinned = structure.get(
        id="pd_pair_runs",
        view="atom",
        args={"atom": "aPd1", "run": first_run_id},
    )
    assert f"[r{first_run_id}]" in pinned.body

    with pytest.raises(NotFound):
        structure.get(
            id="pd_pair_runs",
            view="atom",
            args={"atom": "aPd1", "run": 999999},
        )


def test_clean_relax_on_emt_supported_elements_surfaces_approx_force(structure):
    """A clean-only design on an EMT-supported element set (Pd) surfaces an
    approximate per-atom force, clearly labeled — never confused with a real
    emt/ml relax force."""
    pytest.importorskip("ase")
    structure.put(id="pd_pair_clean", text=_PD)
    structure.edit(id="pd_pair_clean", ops=[{"op": "relax", "fidelity": "clean"}])
    ref = structure.store.get_ref(kind="structure", id="pd_pair_clean")
    runs = structure.store.structure_runs(ref.id)
    clean_run = next(r for r in runs if r["fidelity"] == "clean")
    assert clean_run["forces"] is not None
    assert clean_run["forces"]["approx"] is True
    assert clean_run["forces"]["source"] == "emt"

    atom_view = structure.get(id="pd_pair_clean", view="atom", args={"atom": "aPd1"})
    assert "|F| =" in atom_view.body
    assert "approx" in atom_view.body


def test_clean_relax_outside_emt_coverage_surfaces_no_forces(structure):
    """An element set outside EMT's coverage never gets a fabricated force —
    the clean-run's forces column stays NULL, and view='atom' says so."""
    unsupported = json.dumps(
        {
            "cell": {"a": 10.0, "b": 10.0, "c": 10.0},
            "ops": [
                {"op": "add_atom", "element": "Fe", "frac": [0.0, 0.0, 0.0]},
                {"op": "add_atom", "element": "Fe", "frac": [0.2, 0.0, 0.0]},
            ],
        }
    )
    structure.put(id="fe_pair", text=unsupported)
    structure.edit(id="fe_pair", ops=[{"op": "relax", "fidelity": "clean"}])
    ref = structure.store.get_ref(kind="structure", id="fe_pair")
    runs = structure.store.structure_runs(ref.id)
    clean_run = next(r for r in runs if r["fidelity"] == "clean")
    assert clean_run["forces"] is None

    atom_view = structure.get(id="fe_pair", view="atom", args={"atom": "aFe1"})
    assert "|F|: unavailable" in atom_view.body
    assert "Charges: —" in atom_view.body


def test_forces_join_is_label_stable_not_rank_derived(structure, monkeypatch):
    """gripe 161576 FIX 1 regression: the stored forces join is by LABEL,
    captured together with the vectors at write time — never by re-deriving
    canonical_order (which sorts on fractional position, and so can reorder
    across a relax on a periodic same-element slab) at read time. Proven two
    ways: (1) each atom's displayed |F| matches exactly its own recorded
    vector, never a neighbor's; (2) the read path never even calls
    canonical_order, so an adversarial rank flip can't touch it."""
    pytest.importorskip("ase")
    from precis.structure import cache as relax_cache

    spec = json.dumps(
        {
            "cell": {"a": 10.0, "b": 10.0, "c": 10.0},
            "ops": [
                {"op": "add_atom", "element": "Pd", "frac": [0.10, 0.0, 0.0]},
                {"op": "add_atom", "element": "Pd", "frac": [0.40, 0.0, 0.0]},
                {"op": "add_atom", "element": "Pd", "frac": [0.70, 0.0, 0.0]},
            ],
        }
    )
    structure.put(id="pd_trio", text=spec)
    structure.edit(id="pd_trio", ops=[{"op": "relax", "fidelity": "emt"}])
    ref = structure.store.get_ref(kind="structure", id="pd_trio")
    run = next(
        r for r in structure.store.structure_runs(ref.id) if r["fidelity"] == "emt"
    )
    stored = run["forces"]
    assert stored is not None
    # Ground truth: label -> vector, exactly as recorded (labels/vectors are
    # index-paired, so zip is a faithful reconstruction of the write-time map).
    truth = dict(zip(stored["labels"], stored["vectors"], strict=True))
    assert {"aPd1", "aPd2", "aPd3"} <= set(truth)

    # Poison canonical_order so the read path fails loudly if it's ever
    # called — the fix's whole point is that the forces join no longer
    # depends on it at all.
    def _boom(scene):
        raise AssertionError("canonical_order must not run on the forces read path")

    monkeypatch.setattr(relax_cache, "canonical_order", _boom)

    for label in ("aPd1", "aPd2", "aPd3"):
        resp = structure.get(id="pd_trio", view="atom", args={"atom": label})
        fx, fy, fz = truth[label]
        mag = (fx * fx + fy * fy + fz * fz) ** 0.5
        assert f"{mag:.4f}" in resp.body, f"{label} did not show its own force"


def test_view_atom_no_run_arg_ignores_a_stale_older_version_run(structure):
    """gripe 161576 FIX 2: without an explicit run=, forces must come from a
    run at the design's CURRENT version — a superseded version's forces never
    silently surface as if current. It falls through to the on-demand
    estimate (Pd is EMT-covered) instead of the stale run; the pinned run=
    selector still answers explicitly with that exact (stale) run."""
    pytest.importorskip("ase")
    structure.put(id="pd_pair_stale", text=_PD)
    structure.edit(id="pd_pair_stale", ops=[{"op": "relax", "fidelity": "emt"}])
    ref = structure.store.get_ref(kind="structure", id="pd_pair_stale")
    stale_run_id = structure.store.structure_runs(ref.id)[0]["id"]

    # A further edit bumps the design version with no new force-bearing run
    # — the emt run above is now stale relative to the current version.
    structure.edit(
        id="pd_pair_stale",
        ops=[{"op": "add_atom", "element": "Pd", "frac": [0.6, 0.0, 0.0]}],
    )
    resp = structure.get(id="pd_pair_stale", view="atom", args={"atom": "aPd1"})
    assert f"[r{stale_run_id}]" not in resp.body  # not silently shown as current
    assert "on-demand estimate" in resp.body  # falls through (Pd is EMT-covered)

    pinned = structure.get(
        id="pd_pair_stale",
        view="atom",
        args={"atom": "aPd1", "run": stale_run_id},
    )
    assert f"[r{stale_run_id}]" in pinned.body  # explicit pin still answers


def test_toc_default_view_never_runs_the_live_emt_estimate(structure, monkeypatch):
    """gripe 161576 FIX 3: the default TOC / per-atom list |F| column is a
    cheap DB read only — the live EMT single-point estimate runs ONLY for an
    explicit view='atom', never on a plain get(kind='structure', id=...)."""
    import precis.handlers.structure as structure_mod

    def _boom(scene):
        raise AssertionError("estimate_forces_emt must not run on the default TOC")

    monkeypatch.setattr(structure_mod, "estimate_forces_emt", _boom)
    # Pd is EMT-covered — if the guard failed, this would call _boom and raise.
    structure.put(
        id="pd_pair_toc", text=_PD
    )  # no relax at all — no recorded run either
    resp = structure.get(id="pd_pair_toc")
    assert "—" in resp.body  # the |F| column shows the dash, not a live estimate


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
    assert hit["forces"] is None  # no forces payload was recorded on this run


def test_cache_hit_propagates_forces_onto_the_fresh_run_row(structure):
    """A cache hit still records a fresh struct_runs row (append-only audit,
    §9/§12) — its forces payload rides along from the cached run rather than
    silently going missing."""
    from precis.structure import cache as relax_cache

    structure.put(id="pd_pair_hitforce", text=_PD)
    ref = structure.store.get_ref(kind="structure", id="pd_pair_hitforce")
    scene, _ = structure.store.structure_load(ref.id)

    order = relax_cache.canonical_order(scene)
    key = relax_cache.run_cache_key(
        scene, fidelity="ml", model="mace_mp", params={"steps": 200}
    )
    sha = relax_cache.structure_sha(scene)
    forces_payload = {
        "vectors": [[0.1, 0.0, 0.0], [-0.1, 0.0, 0.0]],
        "labels": ["aPd1", "aPd2"],
        "approx": False,
        "source": "mace_mp",
    }
    structure.store.structure_record_run(
        ref.id,
        fidelity="ml",
        on_version=structure.store.structure_version(ref.id),
        converged=True,
        n_steps=5,
        max_disp=0.01,
        energy=-4.0,
        max_force=0.1,
        model="mace_mp",
        curve=[0.2, 0.05],
        cache_key=key,
        structure_sha=sha,
        final_geometry=relax_cache.serialize_geometry(scene, order),
        forces=forces_payload,
    )

    structure.edit(id="pd_pair_hitforce", ops=[{"op": "relax", "fidelity": "ml"}])
    runs = structure.store.structure_runs(ref.id)
    assert runs[0]["forces"] == forces_payload  # newest = the fresh hit row


def _child_jobs(store, parent_id: int) -> list[dict]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job' "
            "AND deleted_at IS NULL ORDER BY ref_id",
            (parent_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_energy_rung_mints_a_struct_relax_job_parented_on_the_structure(
    structure, store, no_local_mlip
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


def test_relax_cell_mode_rides_the_dispatch_and_keys(structure, store, no_local_mlip):
    """A variable-cell relax carries its ``cell`` mode into the struct_relax job
    params (the GPU container honours it) and folds into the run-cube cache key,
    while an atoms-only relax keeps the historical key shape (no ``cell`` param)
    so the existing cube stays a hit."""
    structure.put(id="slab_a", text=_PD)
    ref_a = structure.store.get_ref(kind="structure", id="slab_a")
    structure.edit(
        id="slab_a", ops=[{"op": "relax", "fidelity": "ml", "cell": "inplane"}]
    )
    params = _child_jobs(store, ref_a.id)[0]["params"]
    assert params["cell"] == "inplane"
    key_inplane = params["cache_key"]

    structure.put(id="slab_b", text=_PD)  # same geometry, atoms-only relax
    ref_b = structure.store.get_ref(kind="structure", id="slab_b")
    structure.edit(id="slab_b", ops=[{"op": "relax", "fidelity": "ml"}])
    params_b = _child_jobs(store, ref_b.id)[0]["params"]
    assert "cell" not in params_b  # historical param shape preserved
    assert key_inplane != params_b["cache_key"]  # cell folds into the address


def test_relax_cell_mode_requires_energy_rung(structure):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput):
        structure.edit(
            id="pd_pair",
            ops=[{"op": "relax", "fidelity": "clean", "cell": "inplane"}],
        )


def test_relax_unknown_cell_mode_is_bad_input(structure):
    structure.put(id="pd_pair", text=_PD)
    with pytest.raises(BadInput):
        structure.edit(
            id="pd_pair", ops=[{"op": "relax", "fidelity": "ml", "cell": "sideways"}]
        )


def test_energy_rung_with_requester_wires_the_wait(structure, store, no_local_mlip):
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


# ── T4: provenance guards (ADR 0053 §4, migration 0084) ────────────────────


def _insert_external_run(
    store,
    ref_id: int,
    *,
    method: dict,
    energy: float = -12.34,
    fidelity: str = "dft-tight",
) -> None:
    """Seed an external-provenance ``struct_runs`` row directly — T3's
    import path (``structure_import``) isn't necessarily landed yet, so the
    guard tests seed via raw SQL the way ``tests/test_struct_runs_method_
    provenance.py`` does."""
    with store.tx() as conn:
        conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, on_version, energy, provenance, method) "
            "VALUES (%s, %s, 1, %s, 'external', %s::jsonb)",
            (ref_id, fidelity, energy, json.dumps(method)),
        )


def test_edit_refuses_an_externally_sourced_design(structure, store):
    structure.put(id="oc20_slab", text=_PD)
    ref = store.get_ref(kind="structure", id="oc20_slab")
    _insert_external_run(store, ref.id, method={"functional": "PBE", "cutoff_eV": 500})
    with pytest.raises(BadInput, match="derive a variant"):
        structure.edit(
            id="oc20_slab",
            ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
        )
    # a purely computed design is unaffected
    structure.put(id="pd_pair", text=_PD)
    resp = structure.edit(
        id="pd_pair",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    )
    assert "edited" in resp.body


def test_put_refuses_overwriting_an_externally_sourced_design(structure, store):
    structure.put(id="oc20_ext", text=_PD)
    ref = store.get_ref(kind="structure", id="oc20_ext")
    _insert_external_run(store, ref.id, method={"functional": "PBE", "cutoff_eV": 500})
    # re-put on the same slug would overwrite the mirror in place — refuse it
    with pytest.raises(BadInput, match="derive a variant"):
        structure.put(id="oc20_ext", text=_PD)
    # a fresh slug is unaffected
    resp = structure.put(id="fresh_slab", text=_PD)
    assert "created" in resp.body


def test_runs_view_labels_provenance_and_method(structure, store):
    structure.put(id="oc20_slab", text=_PD)
    ref = store.get_ref(kind="structure", id="oc20_slab")
    _insert_external_run(store, ref.id, method={"functional": "PBE", "cutoff_eV": 500})
    resp = structure.get(id="oc20_slab", view="runs")
    assert "external" in resp.body
    assert "PBE" in resp.body and "500" in resp.body

    # a computed run shows "computed" and no method fingerprint
    structure.put(id="pd_pair", text=_PD)
    structure.edit(id="pd_pair", ops=[{"op": "relax", "fidelity": "clean"}])
    computed_resp = structure.get(id="pd_pair", view="runs")
    assert "computed" in computed_resp.body
    assert "PBE" not in computed_resp.body


def test_guard_energy_comparable_refuses_a_mismatched_fingerprint():
    external_pbe = {
        "provenance": "external",
        "method": {"functional": "PBE", "cutoff_eV": 500},
    }
    computed_mace = {"provenance": "computed", "model": "mace"}
    with pytest.raises(BadInput, match="category error"):
        guard_energy_comparable(external_pbe, computed_mace)


def test_guard_energy_comparable_allows_a_matching_fingerprint():
    a = {"provenance": "external", "method": {"functional": "PBE", "cutoff_eV": 500}}
    b = {"provenance": "external", "method": {"cutoff_eV": 500, "functional": "PBE"}}
    guard_energy_comparable(a, b)  # no raise

    same_model_a = {"provenance": "computed", "model": "mace"}
    same_model_b = {"provenance": "computed", "model": "mace"}
    guard_energy_comparable(same_model_a, same_model_b)  # no raise
