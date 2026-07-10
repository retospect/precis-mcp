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
from precis.store.types import Tag
from precis_web.routes.structure import (
    _latest_proposal,
    _lineage,
    _list_rows,
    _markers,
    _pending_jobs,
    _run_count,
    _run_rows,
    _viewer,
)

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

# Same cell with no relax op, so a design starts with zero recorded runs — used
# to prove a *dispatched* relax is visible (as a pending job) before any run
# lands in the cube.
_SI2_RAW = json.dumps(
    {
        "cell": {"a": 5.43, "b": 5.43, "c": 5.43, "pbc": [True, True, True]},
        "ops": [
            {"op": "add_atom", "element": "Si", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Si", "frac": [0.25, 0.25, 0.25]},
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


def test_run_count_counts_recorded_runs(seeded):
    store, ref = seeded
    # the put's clean run + the injected dft-fast run
    assert _run_count(store, ref.id) == 2


def test_pending_jobs_surfaces_dispatched_relax_and_transitions(store):
    """A dispatched relax (no local backend) is visible as a pending job with
    *no* run-cube row yet — the fix for "the button ran, but the page shows
    nothing". queued→running is reflected; a succeeded job drops out (it now
    lives as a struct_runs row)."""
    h = StructureHandler(hub=Hub(store=store))
    h.put(id="pend_si2", text=_SI2_RAW)
    ref = resolve_live_slug_ref(store, kind="structure", id="pend_si2")
    h.edit(id="pend_si2", ops=[{"op": "relax", "fidelity": "dft"}])

    pending = _pending_jobs(store, ref.id)
    assert len(pending) == 1
    job_id = pending[0]["job_id"]
    assert pending[0]["fidelity"] == "dft"
    assert pending[0]["status"] == "queued"
    assert _run_count(store, ref.id) == 0  # nothing landed yet

    store.add_tag(
        job_id,
        Tag.parse_strict("STATUS:running", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    assert _pending_jobs(store, ref.id)[0]["status"] == "running"

    store.add_tag(
        job_id,
        Tag.parse_strict("STATUS:succeeded", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    assert _pending_jobs(store, ref.id) == []


def test_pending_jobs_shows_a_failed_relax(store):
    h = StructureHandler(hub=Hub(store=store))
    h.put(id="fail_si2", text=_SI2_RAW)
    ref = resolve_live_slug_ref(store, kind="structure", id="fail_si2")
    h.edit(id="fail_si2", ops=[{"op": "relax", "fidelity": "dft"}])
    job_id = _pending_jobs(store, ref.id)[0]["job_id"]
    store.add_tag(
        job_id,
        Tag.parse_strict("STATUS:failed", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )
    pending = _pending_jobs(store, ref.id)
    assert len(pending) == 1 and pending[0]["status"] == "failed"


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

    init = v["initial"]
    # Initial XYZ: two Si atoms, Cartesian header count line == 2.
    assert init["xyz"].splitlines()[0] == "2"
    assert init["xyz"].count("Si ") == 2
    # Per-atom detail: element / label / colour / coordination all present.
    assert {a["element"] for a in init["atoms"]} == {"Si"}
    assert init["atoms"][0]["color"] == "#f0c8a0"  # CPK Si
    assert all("coordination" in a for a in init["atoms"])
    # Authoritative bond graph: the two Si are within the covalent cutoff.
    assert len(init["bonds"]) >= 1
    b = init["bonds"][0]
    assert {b["i"], b["j"]} <= {a["label"] for a in init["atoms"]}
    assert b["length"] > 0
    # Colour legend groups by element + carries labels for hover-highlight.
    assert len(v["legend"]) == 1
    leg = v["legend"][0]
    assert leg["element"] == "Si"
    assert leg["color"] == "#f0c8a0"
    assert leg["count"] == 2
    assert sorted(leg["labels"]) == sorted(a["label"] for a in init["atoms"])

    # Relaxed geometry present + sourced from the injected run.
    relaxed = v["relaxed"]
    assert relaxed is not None
    assert v["relaxed_run_id"] == runs[0]["id"]
    # The relaxed atom moved off the ideal 0.25 site (cartesian ~1.39 Å).
    relaxed_second = relaxed["xyz"].splitlines()[3].split()
    assert relaxed_second[0] == "Si"
    assert float(relaxed_second[1]) == pytest.approx(0.256 * 5.43, abs=1e-3)

    # "What moved": both Si shifted, sorted by displacement, each hover-linkable.
    assert v["moved"], "expected a displacement list for the relaxed geometry"
    assert all(m["delta"] > 0 for m in v["moved"])
    assert v["moved"] == sorted(v["moved"], key=lambda m: m["delta"], reverse=True)
    assert {m["label"] for m in v["moved"]} <= {a["label"] for a in init["atoms"]}


_PD_MARKS = json.dumps(
    {
        "cell": {"a": 10.0, "b": 10.0, "c": 10.0, "pbc": [True, True, False]},
        "ops": [
            {"op": "add_atom", "element": "Pd", "frac": [0.0, 0.0, 0.0]},
            {"op": "add_atom", "element": "Pd", "frac": [0.26, 0.0, 0.0]},
            {
                "op": "eye",
                "name": "active_site",
                "atoms": ["aPd1"],
                "reach": 3.0,
                "for": "reactive Pd",
            },
            {
                "op": "measure",
                "kind": "distance",
                "atoms": ["aPd1", "aPd2"],
                "direction": "target",
                "goal": {"target": 2.5, "tol": 0.05},
                "strength": "soft",
            },
        ],
    }
)


def test_markers_helper_evaluates(store):
    StructureHandler(hub=Hub(store=store)).put(id="sqlm_pd", text=_PD_MARKS)
    ref = resolve_live_slug_ref(store, kind="structure", id="sqlm_pd")
    scene, _ = store.structure_load(ref.id)
    marks = _markers(scene)
    by = {m["label"]: m for m in marks}
    assert by["active_site"]["is_eye"] and by["active_site"]["operands"] == ["aPd1"]
    dist = next(m for m in marks if m["kind"] == "distance")
    assert dist["operands"] == ["aPd1", "aPd2"]
    assert dist["verdict"] == "warn"  # 2.6 vs 2.5±0.05, soft → warn
    assert "2.6" in dist["value"]


def test_lineage_helper_both_directions(store):
    h = StructureHandler(hub=Hub(store=store))
    h.put(id="sqll_parent", text=_PD_MARKS)
    h.derive(
        id="sqll_parent",
        to="sqll_child",
        ops=[{"op": "add_atom", "element": "O", "frac": [0.5, 0.5, 0.5]}],
    )
    parent = resolve_live_slug_ref(store, kind="structure", id="sqll_parent")
    child = resolve_live_slug_ref(store, kind="structure", id="sqll_child")
    assert [c["slug"] for c in _lineage(store, parent.id)["children"]] == ["sqll_child"]
    assert [p["slug"] for p in _lineage(store, child.id)["parents"]] == ["sqll_parent"]


def test_latest_proposal_reads_status_and_result(store):
    StructureHandler(hub=Hub(store=store)).put(id="sqlp_pd", text=_PD_MARKS)
    ref = resolve_live_slug_ref(store, kind="structure", id="sqlp_pd")
    proposal = {
        "ops": [{"op": "vacancy", "atom": "aPd2"}],
        "rationale": "drop one",
        "valid": True,
    }
    with store.tx() as conn:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="structure_propose",
            meta={
                "job_type": "structure_propose",
                "executor": "claude_inproc",
                "params": {
                    "structure_ref_id": ref.id,
                    "slug": "sqlp_pd",
                    "instruction": "drop a Pd",
                },
            },
            conn=conn,
        )
        conn.execute(
            "INSERT INTO chunks (ref_id, set_by, ord, chunk_kind, text, meta) "
            "VALUES (%s,'agent',0,'job_result',%s,'{}')",
            (job.id, json.dumps(proposal)),
        )
    store.add_tag(
        job.id,
        Tag.parse_strict("STATUS:succeeded", kind="job"),
        set_by="agent",
        replace_prefix=True,
    )

    got = _latest_proposal(store, ref.id)
    assert got is not None
    assert got["job_id"] == job.id
    assert got["status"] == "succeeded"
    assert got["proposal"]["ops"][0]["op"] == "vacancy"

    # no job for a different design → None
    StructureHandler(hub=Hub(store=store)).put(id="sqlp_other", text=_PD_MARKS)
    other = resolve_live_slug_ref(store, kind="structure", id="sqlp_other")
    assert _latest_proposal(store, other.id) is None
