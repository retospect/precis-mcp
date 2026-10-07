"""``pourbaix_bulk`` job_type — host composition, the no-key config failure,
the MP entry cache, and the verdict write-back.

No network: the MP seams (``MP_VERSION`` / ``MP_FETCH``) are replaced with
hand-built entries. Formation free energies and their sources are in
``tests/precis_dft/test_pourbaix_bulk.py``'s docstring (Wagman et al., NBS
1982); Rh carries only its element reference (0 by definition).
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np
import pytest
from pymatgen.analysis.phase_diagram import PDEntry
from pymatgen.analysis.pourbaix_diagram import IonEntry, PourbaixEntry
from pymatgen.core import Composition
from pymatgen.core.ion import Ion

from precis import secrets as vault
from precis.dispatch import Hub
from precis.handlers.structure import StructureHandler
from precis.store.types import Tag
from precis.structure.cell import Cell
from precis.structure.ops import apply_ops
from precis.structure.scene import Scene
from precis.workers.executors import EXECUTOR_PROVIDES
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import pourbaix_bulk as job
from precis_dft import pourbaix_bulk as engine

_KJ = 1.0 / 96.485

#: fcc(111) Cu, 3×3×4 = 36 atoms (labels aCu1..aCu36); z_frac ≈ 0.62 is the
#: top layer's height in the 10 Å-vacuum cell, 0.70 sits ~2 Å above it.
_SLAB: dict[str, Any] = {
    "op": "slab",
    "element": "Cu",
    "size": [3, 3, 4],
    "fix_layers": 2,
}
_RH_SUBST: dict[str, Any] = {"op": "set_element", "atom": "aCu36", "element": "Rh"}
_H_ADATOM: dict[str, Any] = {
    "op": "add_atom",
    "element": "H",
    "frac": [0.15, 0.15, 0.70],
}
_RH_ADATOM: dict[str, Any] = {
    "op": "add_atom",
    "element": "Rh",
    "frac": [0.55, 0.55, 0.70],
}


@pytest.fixture(autouse=True)
def _extra_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    """The dispatch's install check sees the [pourbaix] extra as present.

    Every test stubs ``MP_VERSION``/``MP_FETCH``, so ``mp_api`` is never
    imported; without this the precis-dev image (pymatgen, no mp-api) fails
    the check before the code under test runs. The check itself is
    ``test_missing_extra_fails_config``, whose own patch overrides this one.
    """
    monkeypatch.setattr(job, "find_spec", lambda name: object())


def _scene(ops: list[dict[str, Any]]) -> Scene:
    scene = Scene(cell=Cell(np.eye(3) * 10.0, (True, True, True)))
    apply_ops(scene, ops)
    return scene


def _solid(formula: str, dgf_kj: float) -> Any:
    # PourbaixEntry documents PDEntry/IonEntry inputs but annotates only
    # ComputedEntry; route through Any.
    entry: Any = PDEntry(Composition(formula), dgf_kj * _KJ)
    return PourbaixEntry(entry)


def _ion(formula: str, dgf_kj: float) -> Any:
    entry: Any = IonEntry(Ion.from_formula(formula), dgf_kj * _KJ)
    return PourbaixEntry(entry)


def _cu_entries() -> list[Any]:
    return [
        _solid("Cu", 0.0),
        _solid("Cu2O", -146.0),
        _solid("CuO", -129.7),
        _ion("Cu[2+]", 65.49),
        _ion("HCuO2[-]", -258.5),
    ]


def _rh_entries() -> list[Any]:
    return [_solid("Rh", 0.0)]


_BY_CHEMSYS = {"Cu": _cu_entries, "Rh": _rh_entries}


# ── registry + spec ───────────────────────────────────────────────────


def test_registered_with_spec_params() -> None:
    spec = get_job_type("pourbaix_bulk")
    assert spec is job.SPEC
    assert "pourbaix_bulk" in known_job_types()
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert spec.requires == frozenset({"has_pourbaix"})
    assert spec.requires <= EXECUTOR_PROVIDES["claude_inproc"]
    assert spec.dispatch is not None
    assert spec.validate_submit is job.validate_submit
    assert set(spec.params_schema["properties"]) == {
        "candidate_ref",
        "point",
        "window",
        "ion_conc_M",
        "stability_tol",
        "grid",
    }
    assert spec.params_schema["required"] == ["candidate_ref", "point"]


def test_validate_submit_checks_nested_params() -> None:
    ok = {
        "candidate_ref": "st205850",
        "point": {"U_RHE": -0.2, "pH": 7},
        "window": {"U_RHE": [-0.4, 0.0], "pH": [7, 10]},
        "ion_conc_M": 1e-6,
        "stability_tol": 0.1,
        "grid": 5,
    }
    assert job.validate_submit(None, params=ok) is None
    bad: list[tuple[dict[str, Any], str]] = [
        ({"point": {"U_RHE": 0}}, "params.point"),
        ({"point": {"U_RHE": True, "pH": 7}}, "params.point"),
        ({"window": {"pH": [7]}}, "params.window.pH"),
        ({"window": {"T": [1, 2]}}, "params.window must be"),
        ({"ion_conc_M": 0}, "ion_conc_M"),
        ({"stability_tol": -0.1}, "stability_tol"),
        ({"grid": 1}, "grid"),
        ({"grid": 5.0}, "grid"),
    ]
    for override, needle in bad:
        err = job.validate_submit(None, params={**ok, **override})
        assert err is not None and needle in err, (override, err)


# ── host composition ──────────────────────────────────────────────────


def test_ops_path_substituted_rh_is_a_dopant() -> None:
    ops = [_SLAB, _RH_SUBST, _H_ADATOM]
    comp = job.host_composition(_scene(ops), ops)
    assert comp.path == "ops"
    assert comp.host == {"Cu": 35, "Rh": 1}
    assert comp.dopants == {"Rh": pytest.approx(1 / 36)}
    assert comp.host_phase == {"Cu": 35}
    assert comp.adsorbates == {"H": 1}
    assert comp.as_dict()["rule"].startswith("ops:")


def test_ops_path_heavy_substitution_stays_host() -> None:
    # 4 of 36 = 11% Ni: over the 10% line, so Ni is part of the host phase.
    subst = [
        {"op": "set_element", "atom": f"aCu{i}", "element": "Ni"}
        for i in (33, 34, 35, 36)
    ]
    ops = [_SLAB, *subst]
    comp = job.host_composition(_scene(ops), ops)
    assert comp.dopants == {}
    assert comp.host_phase == {"Cu": 32, "Ni": 4}


def test_ops_path_adsorbate_group_is_not_host() -> None:
    ops = [_SLAB, {"op": "add_atom", "element": "O", "frac": [0.15, 0.15, 0.70]}]
    comp = job.host_composition(_scene(ops), ops)
    assert comp.host == {"Cu": 36}
    assert comp.adsorbates == {"O": 1}


def test_ops_path_starts_at_last_slab_op() -> None:
    old = {"op": "slab", "element": "Pd", "size": [2, 2, 2]}
    ops = [old, _SLAB]
    comp = job.host_composition(_scene(ops), ops)
    assert comp.host_phase == {"Cu": 36}


def test_scene_path_layers() -> None:
    ops = [_SLAB, _RH_SUBST, _H_ADATOM]
    comp = job.host_composition(_scene(ops), None)
    assert comp.path == "scene"
    # The in-layer Rh sits in the slab's layer stack: host, never a dopant
    # on the scene path.
    assert comp.dopants == {}
    assert comp.host_phase == {"Cu": 35, "Rh": 1}
    assert comp.adsorbates == {"H": 1}
    assert comp.as_dict()["rule"].startswith("scene:")


def test_scene_path_above_top_metal_is_a_dopant() -> None:
    ops = [_SLAB, _RH_ADATOM, _H_ADATOM]
    comp = job.host_composition(_scene(ops), None)
    assert comp.dopants == {"Rh": pytest.approx(1 / 37)}
    assert comp.host_phase == {"Cu": 36}
    assert comp.adsorbates == {"H": 1}


def test_no_slab_op_falls_back_to_scene() -> None:
    ops = [_SLAB, _H_ADATOM]
    comp = job.host_composition(_scene(ops), [_H_ADATOM])
    assert comp.path == "scene"


# ── dispatch ──────────────────────────────────────────────────────────


def _fake_ctx(
    store: Any, params: dict[str, Any], ref_id: int = 1
) -> tuple[DispatchContext, list[tuple[str, Any]]]:
    events: list[tuple[str, Any]] = []
    ctx = DispatchContext(
        store=store,
        ref_id=ref_id,
        title="pourbaix",
        meta={"job_type": "pourbaix_bulk", "params": params},
        set_status=lambda s: events.append(("status", s)),
        append_chunk=lambda k, t: events.append((k, t)),
        set_meta=lambda **kw: events.append(("meta", kw)),
        record_failure=lambda r, **kw: events.append(("fail", {"reason": r, **kw})),
        is_cancel_requested=lambda: False,
    )
    return ctx, events


def _run(ctx: DispatchContext) -> None:
    dispatch = job.SPEC.dispatch
    assert dispatch is not None
    dispatch(ctx, job.SPEC)


def _params(candidate: Any, **extra: Any) -> dict[str, Any]:
    return {"candidate_ref": candidate, "point": {"U_RHE": -0.2, "pH": 7}, **extra}


def test_no_key_fails_config_without_verdict(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(vault, "get_secret", lambda *_a, **_k: None)

    def _no_network(*_a: Any) -> Any:
        raise AssertionError("MP must not be called without a key")

    monkeypatch.setattr(job, "MP_VERSION", _no_network)
    monkeypatch.setattr(job, "MP_FETCH", _no_network)
    ctx, events = _fake_ctx(None, _params(1))
    _run(ctx)
    (fail,) = events
    assert fail[0] == "fail"
    assert fail[1]["failure_class"] == "config"
    assert "PRECIS_MP_API_KEY" in fail[1]["reason"]
    assert not any(kind == "meta" for kind, _ in events)


def test_ion_reference_response_shape_is_pinned_and_paginated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from pathlib import Path

    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/mpcontribs_ion_reference_shape.json"
        ).read_text(encoding="utf-8")
    )
    shape = fixture["shape"]
    canonical = json.dumps(
        shape, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    assert hashlib.sha256(canonical.encode()).hexdigest() == fixture["shape_sha256"]
    assert fixture["source"] == engine.ION_REFERENCE_URL

    class Response:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.payload = payload

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, Any]:
            return self.payload

    calls: list[dict[str, Any]] = []
    row = {
        "identifier": "ion-1",
        "formula": "Cu+",
        "data": {key: None for key in shape["record"]["data_fields"]},
    }

    def fake_get(url: str, **kwargs: Any) -> Response:
        calls.append({"url": url, **kwargs})
        page = kwargs["params"]["page"]
        return Response(
            {
                "data": [row],
                "has_more": page == 1,
                "total_count": 2,
                "total_pages": 2,
            }
        )

    import httpx

    monkeypatch.setattr(httpx, "get", fake_get)
    rows = engine.fetch_ion_reference_data("dummy-key")
    assert len(rows) == 2
    assert [call["params"]["page"] for call in calls] == [1, 2]
    assert all(call["params"]["project"] == "ion_ref_data" for call in calls)
    assert all(call["params"]["_fields"] == "identifier,formula,data" for call in calls)
    assert all(call["headers"] == {"x-api-key": "dummy-key"} for call in calls)


def test_fetch_entries_supplies_rest_records_to_mp_api_builder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # mp-api is the [estimate] extra; the dev image and the host venv carry
    # it only when that extra is installed (see host_pytest_paper_extra).
    mp_client = pytest.importorskip("mp_api.client")

    ion_records = [{"identifier": "ion-1", "formula": "Cu+", "data": {}}]
    marker = object()

    class FakeMPRester:
        def __init__(self, api_key: str) -> None:
            assert api_key == "dummy-key"

        def __enter__(self) -> FakeMPRester:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get_ion_reference_data(self) -> list[dict[str, Any]]:
            return []

        def get_pourbaix_entries(self, chemsys_str: str) -> list[Any]:
            assert chemsys_str == "Cu"
            assert self.get_ion_reference_data() == ion_records
            return [marker]

    monkeypatch.setattr(engine, "fetch_ion_reference_data", lambda _key: ion_records)
    monkeypatch.setattr(mp_client, "MPRester", FakeMPRester)
    assert engine.fetch_entries("dummy-key", "Cu") == [marker]


@pytest.fixture
def structure(store: Any) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


@pytest.fixture
def mp_stub(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Key present, MP replaced by hand-built entries; records each fetch."""
    fetched: list[str] = []
    monkeypatch.setattr(vault, "get_secret", lambda *_a, **_k: "test-key")
    monkeypatch.setattr(job, "MP_VERSION", lambda _key: "2099.01.01-test")

    def _fetch(_key: str, chemsys: str) -> list[Any]:
        fetched.append(chemsys)
        return _BY_CHEMSYS[chemsys]()

    monkeypatch.setattr(job, "MP_FETCH", _fetch)
    return fetched


def _put(structure: StructureHandler, slug: str, ops: list[dict[str, Any]]) -> Any:
    structure.put(id=slug, text=json.dumps({"ops": ops}))
    ref = structure.store.get_ref(kind="structure", id=slug)
    assert ref is not None
    return ref


def _meta(events: list[tuple[str, Any]]) -> dict[str, Any]:
    metas = [kw for kind, kw in events if kind == "meta"]
    assert len(metas) == 1, events
    return metas[0]


def test_dispatch_writes_verdict_and_cache(
    structure: StructureHandler, mp_stub: list[str]
) -> None:
    ref = _put(structure, "cu_rh_pbx", [_SLAB, _RH_SUBST, _H_ADATOM])
    window = {"U_RHE": [-0.2, 0.6], "pH": [1, 7]}
    ctx, events = _fake_ctx(structure.store, _params(ref.id, window=window, grid=3))
    _run(ctx)
    assert not any(kind == "fail" for kind, _ in events), events
    meta = _meta(events)
    v = meta["verdict"]
    assert v["verdict"] == "stable"
    assert v["worst_in_window"] == "dissolved"
    assert v["dissolved_everywhere"] is False
    assert v["candidate_ref_id"] == ref.id
    assert v["composition"]["path"] == "ops"
    assert v["basis"]["composition_path"] == "ops"
    assert v["basis"]["mp_version"] == "2099.01.01-test"
    assert v["basis"]["entries_source"] == {"Cu": "mp", "Rh": "mp"}
    assert v["source"] == "Materials Project 2099.01.01-test, CC-BY 4.0"
    assert [d["element"] for d in v["dopants"]] == ["Rh"]
    assert v["flags"] == ["pourbaix:dopant-unassessed"]
    assert set(meta[job.CACHE_META_KEY]) == {
        "Cu@2099.01.01-test",
        "Rh@2099.01.01-test",
    }
    assert mp_stub == ["Cu", "Rh"]
    summaries = [t for kind, t in events if kind == "job_summary"]
    assert summaries == [v["text"]]
    # The verdict round-trips through the jsonb meta column.
    json.dumps(v)


def test_dispatch_by_handle(structure: StructureHandler, mp_stub: list[str]) -> None:
    from precis.utils import handle_registry

    ref = _put(structure, "cu_pbx_handle", [_SLAB])
    handle = handle_registry.format_handle("structure", ref.id)
    ctx, events = _fake_ctx(structure.store, _params(handle))
    _run(ctx)
    assert _meta(events)["verdict"]["candidate_ref_id"] == ref.id


def _seed_cache_job(store: Any, status: str, key: str) -> int:
    ref = store.insert_ref(
        kind="job",
        slug=None,
        title="earlier pourbaix job",
        meta={
            "job_type": "pourbaix_bulk",
            job.CACHE_META_KEY: {key: engine.entries_to_json(_cu_entries())},
        },
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent")
    return int(ref.id)


def test_cache_reused_from_newest_succeeded_job(
    structure: StructureHandler, mp_stub: list[str]
) -> None:
    store = structure.store
    key = job.cache_key("Cu", "2099.01.01-test")
    good = _seed_cache_job(store, "succeeded", key)
    _seed_cache_job(store, "failed", key)  # newer, but not succeeded
    hit = job.find_cached_entries(store, key)
    assert hit is not None and hit[0] == good
    assert job.find_cached_entries(store, job.cache_key("Cu", "other")) is None

    ref = _put(structure, "cu_pbx_cached", [_SLAB])
    ctx, events = _fake_ctx(store, _params(ref.id))
    _run(ctx)
    meta = _meta(events)
    assert mp_stub == []  # no fetch: the cached entries served
    assert meta["verdict"]["basis"]["entries_source"] == {"Cu": f"cache:job {good}"}
    assert job.CACHE_META_KEY not in meta  # only fresh fetches are re-cached
    assert meta["verdict"]["verdict"] == "stable"


def test_four_element_host_fails_input(
    structure: StructureHandler, mp_stub: list[str]
) -> None:
    # 3 of 36 each (8%) would be dopants; 4 each (11%) stays host → Cu-Ni-Pd-Pt.
    subst = []
    labels = iter(range(25, 37))
    for el in ("Ni", "Pd", "Pt"):
        for _ in range(4):
            subst.append(
                {"op": "set_element", "atom": f"aCu{next(labels)}", "element": el}
            )
    ref = _put(structure, "cu_quaternary_pbx", [_SLAB, *subst])
    ctx, events = _fake_ctx(structure.store, _params(ref.id))
    _run(ctx)
    (fail,) = [kw for kind, kw in events if kind == "fail"]
    assert fail["failure_class"] == "input"
    assert "Cu-Ni-Pd-Pt" in fail["reason"]
    assert mp_stub == []
    assert not any(kind == "meta" for kind, _ in events)


def test_missing_candidate_fails_input(
    structure: StructureHandler, mp_stub: list[str]
) -> None:
    ctx, events = _fake_ctx(structure.store, _params("no-such-structure"))
    _run(ctx)
    (fail,) = [kw for kind, kw in events if kind == "fail"]
    assert fail["failure_class"] == "input"


def test_mp_failure_is_infra(
    structure: StructureHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(vault, "get_secret", lambda *_a, **_k: "test-key")

    def _down(_key: str) -> str:
        raise ConnectionError("MP unreachable")

    monkeypatch.setattr(job, "MP_VERSION", _down)
    ref = _put(structure, "cu_pbx_mpdown", [_SLAB])
    ctx, events = _fake_ctx(structure.store, _params(ref.id))
    _run(ctx)
    (fail,) = [kw for kind, kw in events if kind == "fail"]
    assert fail["failure_class"] == "infra"
    assert not any(kind == "meta" for kind, _ in events)


def test_malformed_params_fail_input() -> None:
    ctx, events = _fake_ctx(None, {"candidate_ref": 1, "point": {"U_RHE": 0.0}})
    _run(ctx)
    (fail,) = events
    assert fail[1]["failure_class"] == "input"
    assert "params.point" in fail[1]["reason"]


def test_missing_extra_fails_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        job, "find_spec", lambda name: None if name == "httpx" else object()
    )
    ctx, events = _fake_ctx(None, _params(1))
    _run(ctx)
    (fail,) = events
    assert fail[1]["failure_class"] == "config"
    assert "httpx" in fail[1]["reason"]
    assert "[pourbaix] extra" in fail[1]["reason"]
