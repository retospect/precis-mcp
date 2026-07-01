"""Real-PG regression tests for the /structure route's raw SQL.

The ``test_routes.py`` suite runs against the web ``FakeStore``, which does
*not* parse SQL — so the structure route's list/run queries (a correlated
``ref_identifiers`` slug lookup + the ``struct_runs`` cache columns the MCP
``view='runs'`` omits) are only exercised here, against the live ``store``
fixture. See CLAUDE.md "psycopg % LIKE / fake-store gap".
"""

from __future__ import annotations

import json

import pytest

from precis.dispatch import Hub
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.handlers.structure import StructureHandler
from precis_web.routes.structure import _list_rows, _run_rows, _viewer

_SI2 = json.dumps(
    {
        "cell": {"a": 5.43, "b": 5.43, "c": 5.43, "pbc": [True, True, True]},
        "ops": [
            {"op": "add_atom", "element": "Si", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Si", "frac": [0.25, 0.25, 0.25]},
            {"op": "relax", "fidelity": "clean"},  # rung-0 local, no dispatch
        ],
    }
)


@pytest.fixture
def seeded(store):
    """A Si2 design + one injected succeeded dft-fast run carrying the
    §23.16 cache columns (cache_key + final_geometry) the viewer reads."""
    StructureHandler(hub=Hub(store=store)).put(id="sql_si2", text=_SI2)
    ref = resolve_live_slug_ref(store, kind="structure", id="sql_si2")
    store.structure_record_run(
        ref.id,
        fidelity="dft-fast",
        on_version=1,
        converged=True,
        n_steps=3,
        max_disp=0.01,
        energy=-4.8531,
        max_force=0.0203,
        model="gpaw-rpbe",
        status="succeeded",
        cache_key="deadbeefcafef00d0011223344556677",
        structure_sha="abc123def4567890",
        final_geometry={
            "frac": [[0.004, 0.004, 0.004], [0.256, 0.256, 0.256]],
            "lattice": [[5.43, 0, 0], [0, 5.43, 0], [0, 0, 5.43]],
        },
    )
    return store, ref


def test_list_rows_slug_atoms_runs_energy(seeded):
    store, _ref = seeded
    rows = [r for r in _list_rows(store) if r["slug"] == "sql_si2"]
    assert len(rows) == 1
    row = rows[0]
    assert row["n_atoms"] == 2
    # Two runs: the rung-0 ``clean`` op from the put + the injected dft-fast.
    assert row["n_runs"] == 2
    # ``last_energy`` skips the energy-free clean run for the dft-fast one.
    assert row["last_energy"] == pytest.approx(-4.8531, abs=1e-4)
    assert row["last_fidelity"] == "dft-fast"


def test_run_rows_expose_cache_columns(seeded):
    store, ref = seeded
    runs = _run_rows(store, ref.id)
    # newest-first: the injected dft-fast run leads the energy-free clean run.
    run = runs[0]
    assert run["fidelity"] == "dft-fast"
    assert run["status"] == "succeeded"
    assert run["converged"] is True
    assert run["cache_key"] == "deadbeefcafef00d0011223344556677"
    assert run["structure_sha"] == "abc123def4567890"
    assert run["final_geometry"]["frac"][1] == [0.256, 0.256, 0.256]


def test_viewer_builds_initial_and_relaxed_xyz(seeded):
    store, ref = seeded
    runs = _run_rows(store, ref.id)
    v = _viewer(store, ref, runs)
    assert v["n_atoms"] == 2
    # Initial XYZ: two Si atoms, Cartesian header count line == 2.
    assert v["initial_xyz"].splitlines()[0] == "2"
    assert v["initial_xyz"].count("Si ") == 2
    # Relaxed XYZ present + sourced from the injected run.
    assert v["relaxed_xyz"] is not None
    assert v["relaxed_run_id"] == runs[0]["id"]
    # The relaxed atom moved off the ideal 0.25 site (cartesian ~1.39 Å).
    relaxed_second = v["relaxed_xyz"].splitlines()[3].split()
    assert relaxed_second[0] == "Si"
    assert float(relaxed_second[1]) == pytest.approx(0.256 * 5.43, abs=1e-3)
