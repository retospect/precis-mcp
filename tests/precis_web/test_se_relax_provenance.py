"""gr504809 — the se page says whether and how each bound structure was
relaxed, from the relax records only (``precis_web/relax_provenance.py``).

The page half seeds real structures through the handler (an in-request
``clean`` relax stamps ``meta['last_relax']``) and a dispatched-rung run row
through the store, the way ``struct_relax``'s write-back records one."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.handlers.structure import StructureHandler
from precis_se.handler import SeHandler
from precis_web.app import create_app
from precis_web.config import WebConfig
from precis_web.relax_provenance import formula, relax_state
from tests.test_web_se_atomic3d import (
    _SE_MIGRATIONS,
    _apply_migrations,
    _seed_c60_structure,
    _seed_plain_se,
)


def _run(**kw: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": 1,
        "fidelity": "xtb",
        "status": "succeeded",
        "model": "GFN2-xTB",
        "on_version": 2,
        "converged": True,
        "n_steps": 40,
        "max_force": 0.03,
        "provenance": "computed",
        "params": {},
        "method": None,
        "has_geometry": True,
    }
    row.update(kw)
    return row


def test_no_record_says_none_even_when_text_claims_a_relax() -> None:
    s = relax_state({"version": 1, "description": "UFF-relaxed (RDKit)"}, [])
    assert s.state == "none"
    assert s.headline == "no relax recorded"
    assert "UFF" not in s.headline + s.detail
    assert s.tone == "danger"


def test_clean_rung_reads_as_geometry_repair_not_a_relax() -> None:
    meta = {
        "version": 3,
        "last_relax": {"rung": "clean", "converged": True, "n_steps": 55},
    }
    s = relax_state(meta, [_run(id=972, fidelity="clean", on_version=3, model=None)])
    assert s.state == "repaired"
    assert s.headline.startswith("geometry repair only · clean")
    assert "no force field" in s.headline
    assert s.run_id == 972
    assert s.record["rung"] == "clean"


def test_energy_rung_last_relax_reports_method_and_fmax() -> None:
    meta = {
        "version": 2,
        "last_relax": {
            "rung": "xtb",
            "converged": True,
            "n_steps": 40,
            "max_force": 0.03,
            "model": "GFN2-xTB",
        },
    }
    s = relax_state(meta, [_run(id=7, params={"fmax": 0.05})])
    assert s.state == "relaxed"
    assert "xtb (semi-empirical quantum)" in s.headline
    assert "GFN2-xTB" in s.headline
    assert "target fmax 0.05 eV/Å" in s.headline
    assert s.record["fmax_eV_per_A"] == 0.05
    assert "output" in s.detail


def test_unconverged_relax_is_flagged() -> None:
    meta = {
        "version": 2,
        "last_relax": {"rung": "ml", "converged": False, "n_steps": 500},
    }
    s = relax_state(meta, [])
    assert s.state == "unconverged"
    assert "stopped after 500 steps" in s.headline
    assert "snapshot" in s.detail


def test_dispatched_run_on_current_version_is_not_applied() -> None:
    s = relax_state({"version": 2}, [_run(id=11)])
    assert s.state == "run-not-applied"
    assert "run r11" in s.headline
    assert "unrelaxed input" in s.detail


def test_runs_on_older_versions_do_not_count_as_current() -> None:
    s = relax_state(
        {"version": 3}, [_run(id=5, on_version=2), _run(id=6, on_version=1)]
    )
    assert s.state == "none"
    assert "2 earlier relax runs" in s.detail


def test_failed_runs_are_ignored_and_external_rows_are_named() -> None:
    runs = [
        _run(id=1, status="failed"),
        _run(
            id=2,
            fidelity="dft",
            provenance="external",
            method={"functional": "PBE", "dataset": "OC20"},
        ),
    ]
    s = relax_state({"version": 2}, runs)
    assert s.state == "external"
    assert "functional PBE" in s.headline and "dataset OC20" in s.headline


def test_formula_is_hill_ordered() -> None:
    assert formula({"H": 144, "C": 384}) == "C384H144"
    assert formula({"Pd": 2, "H": 1}) == "HPd2"
    assert formula({}) == ""


@pytest.fixture
def web(store, runtime_with_store, tmp_path) -> TestClient:
    _apply_migrations(store, _SE_MIGRATIONS)
    return TestClient(
        create_app(
            runtime=runtime_with_store, web_config=WebConfig(corpus_dir=tmp_path)
        )
    )


def _seed_design(runtime_with_store, slug: str, binds: dict[str, str]) -> None:
    ops: list[dict[str, Any]] = []
    for block, design in binds.items():
        ops.append({"op": "add_block", "name": block, "envelope": "sphere:r5e-9"})
        ops.append({"op": "bind_structure", "block": block, "design": design})
    SeHandler(hub=runtime_with_store.hub).put(id=slug, text=json.dumps({"ops": ops}))


def _row(page: str, block: str) -> str:
    m = re.search(rf'<tr[^>]*data-block="{block}"[^>]*>(.*?)</tr>', page, re.S)
    assert m, f"no structures row for {block}"
    return m.group(0)


def test_se_page_shows_each_bound_structures_relax_state(
    web, store, runtime_with_store
) -> None:
    _seed_c60_structure(runtime_with_store, "relaxprov-plain")
    _seed_c60_structure(runtime_with_store, "relaxprov-clean")
    StructureHandler(hub=runtime_with_store.hub).edit(
        id="relaxprov-clean", ops=[{"op": "relax", "fidelity": "clean"}]
    )
    _seed_c60_structure(runtime_with_store, "relaxprov-xtb")
    ref = store.get_ref(kind="structure", id="relaxprov-xtb")
    run_id = store.structure_record_run(
        ref.id,
        fidelity="xtb",
        on_version=int(ref.meta["version"]),
        converged=True,
        n_steps=12,
        max_disp=0.01,
        energy=-1.0,
        max_force=0.02,
        model="GFN2-xTB",
        final_geometry={"labels": [], "frac": []},
    )
    _seed_design(
        runtime_with_store,
        "relaxprov-design",
        {
            "a_plain": "relaxprov-plain",
            "b_clean": "relaxprov-clean",
            "c_xtb": "relaxprov-xtb",
        },
    )

    page = web.get("/se/relaxprov-design").text
    assert 'id="bt3d-structures"' in page
    assert (
        "3 bound structures: 1 relaxed geometry not applied, "
        "1 geometry repair only, 1 no relax recorded"
    ) in page

    plain = _row(page, "a_plain")
    assert 'data-relax-state="none"' in plain
    assert "no relax recorded" in plain
    assert 'href="/structure/relaxprov-plain"' in plain

    clean = _row(page, "b_clean")
    assert 'data-relax-state="repaired"' in clean
    assert "geometry repair only" in clean

    xtb = _row(page, "c_xtb")
    assert 'data-relax-state="run-not-applied"' in xtb
    assert f'href="/structure/relaxprov-xtb/run/{run_id}"' in xtb
    assert "GFN2-xTB" in xtb


def test_se_page_without_bound_structures_has_no_panel(web, runtime_with_store) -> None:
    _seed_plain_se(runtime_with_store, "relaxprov-none")
    assert 'id="bt3d-structures"' not in web.get("/se/relaxprov-none").text
