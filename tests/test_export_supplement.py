"""External supplements preserve stored evidence and name absent fields."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zipfile import ZipFile

import numpy as np
import pytest

from precis.errors import BadInput
from precis.export import audience, supplement
from precis.export.supplement import (
    SupplementEntry,
    collect_entry,
    guard_entry,
    write_supplement,
)
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis.utils import handle_registry


@pytest.fixture(autouse=True)
def pathway_handles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The installed plugin set must not change what this fixture addresses."""
    monkeypatch.setattr(handle_registry, "_plugins_loaded", True)
    monkeypatch.setattr(handle_registry, "_plugin_kind_codes", {"pathway": "pw"})
    monkeypatch.setattr(handle_registry, "_plugin_chunk_codes", {})


class _Chunks:
    def live_body_text_for_ref(self, ref_id: int) -> list[str]:
        return ["Stored supporting observation."]


class _Store:
    def __init__(self, refs: dict[int, Any], scene: Scene) -> None:
        self.refs = refs
        self.scene = scene
        self.chunks = _Chunks()

    def get_ref(self, *, kind: str, id: Any) -> Any:
        ref = self.refs.get(int(id))
        return ref if ref is not None and ref.kind == kind else None

    def structure_load(self, ref_id: int) -> tuple[Scene, dict[str, int]]:
        return self.scene, {}


def _store() -> _Store:
    meta: dict[str, Any] = {
        "candidate_ref": 12,
        "tier": "neb",
        "config": {
            "substrate": "NO",
            "target": "NH3",
            "mlip": {"backend": "mace", "model": "medium"},
            "search": {"seeds": [3, 7]},
            "electrochemistry": {"U_vs_RHE": 0.0},
        },
        "config_snapshot_yaml": "substrate: NO\ntarget: NH3\n",
        "results": {
            "substrate": "NO",
            "target": "NH3",
            "pathway": ["NO*", "NH3*"],
            "kinetics_v2": {"tof_chain": 4.5, "manifest_id": "fixture-manifest"},
        },
        "graph": {
            "nodes": [
                {"id": "NO*", "rel_energy": 0.0},
                {"id": "NH3*", "rel_energy": -0.2},
            ],
            "links": [{"source": "NO*", "target": "NH3*", "barrier": 0.4}],
        },
        "structure_refs": {"NO*": 12},
        "manifest_id": "fixture-manifest",
        "validation_manifest": {
            "manifest_id": "fixture-manifest",
            "code": {"precis_sha": "a" * 40, "autocatpath_sha": None},
            "lock": {
                "lock_source": "uv.lock",
                "uv_lock_sha256": "b" * 64,
                "mlip": [
                    {"backend": "mace", "model": "medium", "weights_sha256": "c" * 64}
                ],
            },
            "seeds": {"search_seeds": [3, 7]},
            "inputs": {"config_snapshot_sha256": "d" * 64},
        },
    }
    scene = Scene(cell=Cell(np.eye(3) * 5.0))
    scene.atoms["aPt1"] = Atom("aPt1", "Pt", np.zeros(3))
    return _Store(
        {
            11: SimpleNamespace(id=11, kind="pathway", title="NO to NH3", meta=meta),
            12: SimpleNamespace(id=12, kind="structure", title="Pt catalyst", meta={}),
            13: SimpleNamespace(
                id=13, kind="gripe", title="Stored observation", meta={}
            ),
        },
        scene,
    )


def test_pathway_zip_carries_stored_artifacts_and_absent_stamp(tmp_path: Path) -> None:
    store = _store()
    entry = collect_entry(store, "pw11", "S1.1")
    path = tmp_path / "supplement.zip"
    write_supplement([entry], path)
    with ZipFile(path) as archive:
        names = set(archive.namelist())
        assert {
            "index.json",
            "S1.1/entry.json",
            "S1.1/manifest.json",
            "S1.1/config.json",
            "S1.1/config.yaml",
            "S1.1/results.json",
            "S1.1/kinetics.json",
            "S1.1/structures/001.extxyz",
            "S1.1/structures/candidate.extxyz",
            "S1.1/profile.png",
        } <= names
        record = json.loads(archive.read("S1.1/entry.json"))
        assert record["stamps"]["autocatpath_sha"] == "absent"
        assert record["stamps"]["precis_sha"] == "a" * 40
        assert record["stamps"]["inputs"]["slab_extxyz_sha256"] == "absent"
        assert record["stamps"]["weights"][0]["weights_sha256"] == "c" * 64
        assert (
            json.loads(archive.read("S1.1/results.json"))
            == store.refs[11].meta["results"]
        )
        assert (
            archive.read("S1.1/config.yaml").decode()
            == store.refs[11].meta["config_snapshot_yaml"]
        )
        assert archive.read("S1.1/profile.png").startswith(b"\x89PNG\r\n\x1a\n")
        assert "Pt" in archive.read("S1.1/structures/001.extxyz").decode()
        assert "NO → NH3" in entry.description
        assert "candidate st12 (Pt catalyst)" in entry.description
        assert "potential mace:medium" in entry.description
        assert "U vs RHE 0.0" in entry.description
        assert "seeds [3, 7]; tier neb" in entry.description


def test_missing_pathway_fields_do_not_fail_export(tmp_path: Path) -> None:
    store = _store()
    store.refs[11].meta = {}
    entry = collect_entry(store, "pw11", "S1.2")
    record = json.loads(entry.files["entry.json"])
    assert record["stamps"]["manifest_id"] == "absent"
    assert record["stamps"]["seeds"] == "absent"
    assert record["artifacts"] == {
        "validation_manifest": "absent",
        "config": "absent",
        "results": "absent",
        "graph": "absent",
        "config_snapshot": "absent",
        "kinetics": "absent",
        "structures": "absent",
        "profile": "absent",
    }
    assert "candidate absent" in entry.description
    write_supplement([entry], tmp_path / "supplement.zip")


@pytest.mark.parametrize("voltage", [-0.2, 0.0])
def test_pathway_voltage_prefers_environment(voltage: float) -> None:
    store = _store()
    config = store.refs[11].meta["config"]
    config["environment"] = {
        "electrochemical": {"provider": "che", "U_vs_RHE": voltage}
    }
    config["electrochemistry"] = {"U_vs_RHE": 0.8}
    config["conditions"] = {"potential_U": 1.0}
    entry = collect_entry(store, "pw11", "S1.1")
    assert f"U vs RHE {voltage};" in entry.description


def test_pathway_description_prefers_models_over_model() -> None:
    store = _store()
    store.refs[11].meta["config"]["mlip"] = {
        "backend": "mace",
        "model": "medium",
        "models": ["small", "large"],
    }
    entry = collect_entry(store, "pw11", "S1.1")
    assert "potential mace:['small', 'large'];" in entry.description
    assert "medium" not in entry.description


def test_stored_seed_geometry_wins_over_mutable_structure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.dispatch import Hub
    from precis_pathway import ingest
    from precis_pathway.handler import PathwayHandler

    frozen = "1\nProperties=species:S:1:pos:R:3\nCu 1 2 3\n"
    store = _store()
    requested: list[str] = []

    def sibling(hub: Hub, kind: str) -> Any:
        requested.append(kind)
        assert hub.store is store
        # Exercise the real adapter without requiring the optional engine.
        handler = object.__new__(PathwayHandler)
        handler.hub = hub
        return handler

    monkeypatch.setattr(Hub, "sibling", sibling)
    monkeypatch.setattr(
        ingest, "seed_geometry", lambda store, ref_id: ({"NO*": frozen}, "key")
    )
    entry = collect_entry(store, "pw11", "S1.1")
    assert requested == ["pathway"]
    assert entry.files["structures/001.extxyz"].decode() == frozen
    record = json.loads(entry.files["entry.json"])
    assert record["artifacts"]["structures"][0]["source"] == "stored seed geometry"


def test_seed_geometry_falls_back_when_plugin_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.dispatch import Hub

    def missing(hub: Hub, kind: str) -> Any:
        raise KeyError(kind)

    monkeypatch.setattr(Hub, "sibling", missing)
    entry = collect_entry(_store(), "pw11", "S1.1")
    assert "Pt" in entry.files["structures/001.extxyz"].decode()
    record = json.loads(entry.files["entry.json"])
    assert record["artifacts"]["structures"][0]["source"] == "current linked structure"


@pytest.mark.parametrize("payload", ["credential", "private URL"])
@pytest.mark.parametrize(
    ("field", "member"),
    [
        ("config", "config.json"),
        ("results", "results.json"),
        ("validation_manifest", "manifest.json"),
        ("graph", "graph.json"),
        ("config_snapshot_yaml", "config.yaml"),
    ],
)
def test_collected_pathway_artifacts_refuse_sensitive_content(
    field: str, member: str, payload: str
) -> None:
    value = (
        "sk-" + "ant-" + "fakefixture12345678"
        if payload == "credential"
        else "https://precis.example.test"
    )
    store = _store()
    store.refs[11].meta[field] = (
        value if field == "config_snapshot_yaml" else {"nested": [{"note": value}]}
    )
    with pytest.raises(BadInput) as exc:
        collect_entry(store, "pw11", "S1.1", base_url="https://precis.example.test")
    message = str(exc.value)
    assert payload in message
    assert "S1.1" in message and "pw11" in message and member in message
    if member.endswith(".json"):
        assert "nested[0].note" in message
    assert value not in message


def test_external_structure_description_credential_is_refused() -> None:
    value = "sk-" + "ant-" + "fakefixture12345678"
    store = _store()
    store.refs[12].meta["description"] = value
    state = audience.Audience(store, "external", "https://precis.example.test")
    state.source_chunks["st12"] = ["dc42"]
    with pytest.raises(BadInput) as exc:
        state.reference("st12", None)
    message = str(exc.value)
    assert "S2.1" in message and "st12" in message
    assert "record.json.description" in message
    assert "dc42" in message
    assert value not in message
    assert not state.entries


@pytest.mark.parametrize("member", ["body.txt", "structure.extxyz", "extra.csv"])
@pytest.mark.parametrize("payload", ["credential", "private URL"])
def test_collection_guards_every_text_member(
    monkeypatch: pytest.MonkeyPatch, member: str, payload: str
) -> None:
    value = (
        "sk-" + "ant-" + "fakefixture12345678"
        if payload == "credential"
        else "https://precis.example.test"
    )

    def files(store: Any, ref: Any, files: dict[str, bytes]) -> dict[str, Any]:
        files[member] = value.encode()
        return {}

    monkeypatch.setattr(supplement, "_pathway_files", files)
    with pytest.raises(BadInput) as exc:
        collect_entry(_store(), "pw11", "S1.1", base_url="https://precis.example.test")
    assert member in str(exc.value)
    assert value not in str(exc.value)


@pytest.mark.parametrize("location", ["description", "key", "escaped", "password"])
def test_guard_entry_reports_fields_without_disclosing_secrets(location: str) -> None:
    value = "sk-" + "ant-" + "fakefixture12345678"
    description = value if location == "description" else "Safe description"
    record = {value: "safe"} if location == "key" else {"note": value}
    if location == "password":
        value = "FakeFixture" + "Password123!"
        record = {"password": value}
    data = json.dumps(record)
    if location == "escaped":
        data = data.replace("s", "\\u0073")
    entry = SupplementEntry(
        "S4.1", "gr13", "gripe", description, {"record.json": data.encode()}
    )
    with pytest.raises(BadInput) as exc:
        guard_entry(entry, "")
    message = str(exc.value)
    assert "credential" in message and "S4.1" in message and "gr13" in message
    assert value not in message
    if location == "description":
        assert "description" in message
    elif location == "key":
        assert "key 0" in message
    else:
        assert f"record.json.{next(iter(record))}" in message


@pytest.mark.parametrize("structure_id", [None, "missing", {}, []])
def test_unusable_structure_refs_are_reported_absent(structure_id: Any) -> None:
    store = _store()
    store.refs[11].meta["structure_refs"] = {"NO*": structure_id}
    store.refs[11].meta["candidate_ref"] = structure_id
    entry = collect_entry(store, "pw11", "S1.1")
    record = json.loads(entry.files["entry.json"])
    assert record["artifacts"]["structures"][0]["file"] == "absent"
    assert not any(name.endswith(".extxyz") for name in entry.files)


def test_generic_and_missing_records_remain_self_describing(tmp_path: Path) -> None:
    store = _store()
    entry = collect_entry(store, "gr13", "S4.1")
    missing = collect_entry(store, "gr14", "S4.2")
    assert entry.description == "Stored observation"
    assert entry.files["body.txt"] == b"Stored supporting observation."
    assert json.loads(missing.files["entry.json"])["record"] == "absent"
    write_supplement([entry, missing], tmp_path / "supplement.zip")
    with ZipFile(tmp_path / "supplement.zip") as archive:
        index = json.loads(archive.read("index.json"))
        assert [row["number"] for row in index] == ["S4.1", "S4.2"]


def test_body_export_reads_chunk_store_and_preserves_paragraphs(tmp_path: Path) -> None:
    class Chunks(_Chunks):
        def live_body_text_for_ref(self, ref_id: int) -> list[str]:
            assert ref_id == 13
            return ["First stored observation.", "Second stored observation."]

    store = _store()
    store.chunks = Chunks()
    entry = collect_entry(store, "gr13", "S4.1")
    path = tmp_path / "supplement.zip"
    write_supplement([entry], path)
    with ZipFile(path) as archive:
        record = json.loads(archive.read("S4.1/entry.json"))
        assert record["body"] == "body.txt"
        assert archive.read("S4.1/body.txt").decode("utf-8") == (
            "First stored observation.\n\nSecond stored observation."
        )
