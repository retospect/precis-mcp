"""``structure_import`` write-path tests (ADR 0053 §3/§4/§5, T3).

Exercises the store method directly against the live test DB — the single
idempotent write path all three ADR 0053 ingest modes (on-demand hydrate,
batch mirror, derivative anchor) funnel through.
"""

from __future__ import annotations

import numpy as np
import pytest

from precis.structure.cell import Cell
from precis.structure.importers import ExternalId, ExternalRun
from precis.structure.scene import Atom, Scene


def _pd_scene() -> Scene:
    cell = Cell(np.eye(3) * 10.0, (True, True, True))
    sc = Scene(cell=cell)
    sc.atoms["aPd1"] = Atom(label="aPd1", element="Pd", frac=np.array([0.0, 0.0, 0.0]))
    sc.atoms["aPd2"] = Atom(label="aPd2", element="Pd", frac=np.array([0.26, 0.0, 0.0]))
    return sc


def _run(energy: float = -12.3) -> ExternalRun:
    return ExternalRun(
        energy=energy,
        max_force=0.02,
        final_geometry={"frac": [[0.0, 0.0, 0.0], [0.26, 0.0, 0.0]], "lattice": None},
        method={"functional": "PBE", "cutoff_eV": 500},
    )


def _eid(config_id: str = "cfg-1") -> ExternalId:
    return ExternalId(dataset="catalysis-hub", config_id=config_id)


def test_first_import_creates_design_and_external_run(store):
    ref_id = store.structure_import(_pd_scene(), _run(), _eid())
    ref = store.get_ref(kind="structure", id=ref_id)
    assert ref is not None

    runs = store.structure_runs(ref_id)
    assert len(runs) == 1
    assert runs[0]["fidelity"] == "external"
    assert runs[0]["energy"] == pytest.approx(-12.3)

    # provenance/method aren't part of structure_runs()'s column projection —
    # verify directly against the row.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT provenance, method FROM struct_runs WHERE ref_id = %s",
            (ref_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "external"
    assert row[1] == {"functional": "PBE", "cutoff_eV": 500}


def test_reimport_same_external_id_reuses_ref_no_duplicate_run(store):
    ref_id1 = store.structure_import(_pd_scene(), _run(-12.3), _eid())
    ref_id2 = store.structure_import(_pd_scene(), _run(-12.9), _eid("cfg-1"))

    assert ref_id1 == ref_id2
    runs = store.structure_runs(ref_id1)
    assert len(runs) == 1  # updated in place, never duplicated
    assert runs[0]["energy"] == pytest.approx(-12.9)


def test_distinct_config_id_gets_its_own_design(store):
    ref_id1 = store.structure_import(_pd_scene(), _run(), _eid("cfg-1"))
    ref_id2 = store.structure_import(_pd_scene(), _run(), _eid("cfg-2"))
    assert ref_id1 != ref_id2


def test_computed_run_after_import_leaves_external_row_intact(store):
    ref_id = store.structure_import(_pd_scene(), _run(), _eid())
    store.structure_record_run(
        ref_id,
        fidelity="ml",
        on_version=store.structure_version(ref_id),
        converged=True,
        n_steps=10,
        max_disp=0.01,
        energy=-11.9,
        max_force=0.03,
        model="mace_mp",
    )

    runs = store.structure_runs(ref_id)
    assert len(runs) == 2
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT provenance, energy FROM struct_runs WHERE ref_id = %s ORDER BY id",
            (ref_id,),
        ).fetchall()
    provenance_by_energy = {round(float(r[1]), 3): r[0] for r in rows}
    assert provenance_by_energy == {-12.3: "external", -11.9: "computed"}


def test_compute_cache_never_hits_an_external_row(store):
    """The 0084 partial index excludes provenance='external' — a compute-cache
    probe by ``cache_key`` must never surface an imported row as a hit."""
    ref_id = store.structure_import(_pd_scene(), _run(), _eid())
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT cache_key FROM struct_runs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    cache_key = row[0]
    assert store.structure_find_cached_run(cache_key) is None
