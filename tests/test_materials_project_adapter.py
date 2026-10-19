"""Materials Project adapter, fetch layer and on-demand hydrate. Fixture only, no network."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, Unsupported
from precis.handlers.structure import StructureHandler
from precis.structure.importers import ExternalRun, get_adapter, materials_project
from precis.structure.importers.materials_project import adapter

FIXTURE = Path(__file__).parent / "fixtures" / "catalysis" / "mp2_pd.json"


def _load_raw() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_adapter_is_registered_and_builds_a_periodic_scene() -> None:
    assert get_adapter("materials-project") is adapter
    scene, _run, eid = adapter(_load_raw())

    assert scene.composition() == {"Pd": 1}
    assert tuple(scene.cell.pbc) == (True, True, True)
    assert scene.cell.lattice.shape == (3, 3)
    assert (eid.dataset, eid.config_id) == ("materials-project", "mp-2")


def test_adapter_run_carries_formation_energy_and_surface_block() -> None:
    _scene, run, _eid = adapter(_load_raw())

    assert isinstance(run, ExternalRun)
    assert run.provenance == "external"
    assert run.energy == 0.0
    assert run.max_force is None
    assert run.method["energy_kind"] == "formation_energy_per_atom_eV"
    assert run.method["spacegroup"] == "Fm-3m"
    assert run.method["energy_above_hull_eV"] == 0.0
    surf = run.method["surface"]
    assert surf["weighted_surface_energy_J_m2"] == 1.52
    assert surf["weighted_work_function_eV"] == 5.25
    assert [f["miller_index"] for f in surf["facets"]] == [[1, 1, 1], [1, 0, 0]]
    assert surf["facets"][0]["surface_energy_J_m2"] == 1.41
    assert "structure" not in surf["facets"][0]
    json.dumps(run.method)  # JSONB-serialisable
    assert run.final_geometry is not None
    assert run.final_geometry["symbols"] == ["Pd"]


def test_adapter_without_surface_properties_leaves_the_block_none() -> None:
    raw = _load_raw()
    raw["surface_properties"] = None
    _scene, run, _eid = adapter(raw)
    assert run.method["surface"] is None


def test_adapter_refuses_missing_formation_energy_and_disorder() -> None:
    raw = _load_raw()
    raw["summary"]["formation_energy_per_atom"] = None
    with pytest.raises(ValueError, match="formation_energy_per_atom"):
        adapter(raw)

    raw = copy.deepcopy(_load_raw())
    raw["summary"]["structure"]["sites"][0]["species"] = [
        {"element": "Pd", "occu": 0.5},
        {"element": "Cu", "occu": 0.5},
    ]
    with pytest.raises(ValueError, match="disordered"):
        adapter(raw)


def test_valid_material_id() -> None:
    assert materials_project.valid_material_id("mp-149")
    for bad in ("149", "mp-", "mp-1a", "../mp-1", "mp-1&x=2"):
        assert not materials_project.valid_material_id(bad)


# ── fetch layer: key handling, headers, SSRF-guarded GET ────────────────


class _Server:
    def __init__(self, script: list[tuple[int, Any]]) -> None:
        self.script = script
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        status, body = self.script.pop(0)
        return httpx.Response(status, json=body, request=request)


@pytest.fixture(autouse=True)
def _fresh_circuit() -> Any:
    from precis.utils.http import reset_circuit

    reset_circuit()
    yield
    reset_circuit()


@pytest.fixture
def server(monkeypatch: pytest.MonkeyPatch) -> _Server:
    srv = _Server([])
    real = httpx.Client

    def factory(**kw: Any) -> httpx.Client:
        kw["transport"] = httpx.MockTransport(srv)
        return real(**kw)

    def direct_get(client: httpx.Client, url: str, **kw: Any) -> httpx.Response:
        return client.get(url, **kw)

    monkeypatch.setattr(httpx, "Client", factory)
    monkeypatch.setattr("precis.utils.safe_fetch.safe_get", direct_get)
    monkeypatch.setattr("precis.utils.rate_limit.require", lambda provider, **_kw: None)
    return srv


def test_fetch_without_key_refuses_before_any_request(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(materials_project.MP_KEY_SECRET, raising=False)
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: None)
    with pytest.raises(materials_project.MaterialsProjectKeyMissing):
        materials_project.fetch_record("mp-2")
    assert server.requests == []


def test_fetch_sends_key_header_and_returns_adapter_shape(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _load_raw()
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: "k-secret")
    server.script = [
        (200, {"data": [raw["summary"]]}),
        (200, {"data": [raw["surface_properties"]]}),
    ]
    out = materials_project.fetch_record("mp-2")
    assert out == {k: v for k, v in raw.items() if k != "_note"}
    assert [r.url.path for r in server.requests] == [
        "/materials/summary/",
        "/materials/surface_properties/",
    ]
    assert all(r.headers["x-api-key"] == "k-secret" for r in server.requests)
    assert all(r.url.params["material_ids"] == "mp-2" for r in server.requests)


def test_fetch_unknown_material_and_missing_surface_doc(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: "k")
    server.script = [(200, {"data": []})]
    assert materials_project.fetch_record("mp-999999") is None

    server.script = [(200, {"data": [_load_raw()["summary"]]}), (404, {})]
    out = materials_project.fetch_record("mp-2")
    assert out is not None and out["surface_properties"] is None


def test_fetch_401_is_a_clean_rejection(
    server: _Server, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("precis.secrets.get_secret", lambda *a, **k: "bad-key")
    server.script = [(401, {})]
    with pytest.raises(materials_project.MaterialsProjectAuthRejected) as ei:
        materials_project.fetch_record("mp-2")
    assert "bad-key" not in str(ei.value)


def test_fetch_rejects_a_malformed_id() -> None:
    with pytest.raises(ValueError):
        materials_project.fetch_record("mp-2/../x")


# ── on-demand hydrate through the structure handler ────────────────────


@pytest.fixture
def structure(store: Any) -> StructureHandler:
    return StructureHandler(hub=Hub(store=store))


def test_get_by_id_hydrates_then_hits_the_cache(
    structure: StructureHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[str] = []

    def _fake(material_id: str) -> dict[str, Any]:
        calls.append(material_id)
        return _load_raw()

    monkeypatch.setattr(materials_project, "fetch_record", _fake)
    first = structure.get(id="mp-2", args={"source": "materials-project"})
    assert calls == ["mp-2"]
    assert "hydrated" in first.body and "mp-2" in first.body

    ref = structure.store.get_ref(kind="structure", id="materials-project-mp-2")
    assert ref is not None
    (run,) = structure.store.structure_runs(ref.id)
    assert run["fidelity"] == "external"

    second = structure.get(id="mp-2", args={"source": "materials-project"})
    assert calls == ["mp-2"]  # cache hit, no refetch
    assert "cached" in second.body


def test_get_without_key_refuses_with_next(
    structure: StructureHandler, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _nokey(material_id: str) -> dict[str, Any]:
        raise materials_project.MaterialsProjectKeyMissing("no key")

    monkeypatch.setattr(materials_project, "fetch_record", _nokey)
    with pytest.raises(Unsupported) as ei:
        structure.get(id="mp-2", args={"source": "materials-project"})
    assert materials_project.MP_KEY_SECRET in str(ei.value.next)


def test_get_bad_id_refuses_with_next(structure: StructureHandler) -> None:
    with pytest.raises(BadInput) as ei:
        structure.get(id="Pd", args={"source": "materials-project"})
    assert "mp-" in str(ei.value.next)
