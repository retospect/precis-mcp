"""Real-Postgres companion for ``precis_web/relax_provenance.py``'s raw SQL
(docs/conventions/testing.md, raw-sql-needs-real-pg-test): the
``struct_runs`` read behind the se page's relax panel, run on the test DB."""

from __future__ import annotations

import json

from precis.handlers.structure import StructureHandler
from precis_web.relax_provenance import _runs_by_ref, structure_relax


def _seed(runtime_with_store, slug: str) -> None:
    StructureHandler(hub=runtime_with_store.hub).put(
        id=slug,
        text=json.dumps(
            {
                "cell": {"a": 20.0, "b": 20.0, "c": 20.0, "pbc": [False, False, False]},
                "ops": [
                    {"op": "add_atom", "element": "C", "cart": [10.0, 10.0, 10.0]},
                    {"op": "add_atom", "element": "C", "cart": [11.4, 10.0, 10.0]},
                ],
            }
        ),
    )


def test_runs_by_ref_reads_columns_and_scopes_by_ref(store, runtime_with_store) -> None:
    _seed(runtime_with_store, "rp-sql-a")
    _seed(runtime_with_store, "rp-sql-b")
    a = store.get_ref(kind="structure", id="rp-sql-a")
    b = store.get_ref(kind="structure", id="rp-sql-b")
    run_id = store.structure_record_run(
        a.id,
        fidelity="xtb",
        on_version=int(a.meta["version"]),
        converged=True,
        n_steps=9,
        max_disp=0.01,
        energy=-3.0,
        max_force=0.02,
        model="GFN2-xTB",
        params={"fmax": 0.05},
        final_geometry={"labels": [], "frac": []},
    )
    runs = _runs_by_ref(store, [int(a.id), int(b.id)])
    assert int(b.id) not in runs
    (row,) = runs[int(a.id)]
    assert row["id"] == run_id
    assert row["fidelity"] == "xtb"
    assert row["status"] == "succeeded"
    assert row["provenance"] == "computed"
    assert row["params"] == {"fmax": 0.05}
    assert row["has_geometry"] is True
    assert row["max_force"] == 0.02
    assert _runs_by_ref(store, []) == {}

    state = structure_relax(store, a)
    assert state.state == "run-not-applied"
    assert state.run_id == run_id
    assert structure_relax(store, b).state == "none"
