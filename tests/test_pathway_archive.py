"""The paper-first repro bundle (gr564186): a stored seed run exported as a
deterministic zip — MANIFEST.sha256, RO-Crate metadata, frozen inputs/
outputs/artifacts/env pins, recorded checks and an offline evidence index —
inspectable and integrity-checked without a database, engine or compute.

The persisted ``repro_case.json`` snapshot is the recorded November case;
``repro_case_bundle.zip`` is its packaged archive, committed so an offline
reader can verify integrity without building anything.
"""

from __future__ import annotations

import copy
import hashlib
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest

from precis_pathway.archive import (
    ArchiveRefused,
    build_archive,
    plan_archive,
)
from precis_pathway.verification import verify_archive

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "repro_case.json"
_BUNDLED_ZIP = Path(__file__).parent / "fixtures" / "pathway" / "repro_case_bundle.zip"


@pytest.fixture
def stored() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _reader(stored: dict[str, Any]) -> Any:
    return lambda entry: stored["artifact_text"][entry["name"]].encode("utf-8")


def _build(stored: dict[str, Any], out_dir: Path, **overrides: Any) -> Any:
    kw: dict[str, Any] = {
        "job_id": stored["job_id"],
        "job_meta": stored["job_meta"],
        "pathway_id": stored["pathway_id"],
        "pathway_meta": stored["pathway_meta"],
        "out_dir": out_dir,
        "slug": stored["slug"],
        "title": stored["title"],
        "frozen": stored["frozen"],
        "claims": stored["claims"],
        "read_artifact": _reader(stored),
    }
    kw.update(overrides)
    return build_archive(**kw)


def _members(archive: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(archive) as zf:
        return {i.filename: zf.read(i.filename) for i in zf.infolist()}


def _text(members: dict[str, bytes], root: str, rel: str) -> str:
    return members[f"{root}{rel}"].decode("utf-8")


def test_persisted_november_case_verifies_offline() -> None:
    """The packaged fixture integrity-checks without DB, engine or compute."""
    rows = verify_archive(_BUNDLED_ZIP)
    assert rows, "verify_archive produced no checks"
    assert all(row["status"] == "passed" for row in rows), rows


def test_rebuild_matches_the_packaged_case(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    assert res.archive.read_bytes() == _BUNDLED_ZIP.read_bytes()


def test_identical_inputs_produce_identical_archive_bytes(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    first = _build(stored, tmp_path)
    second = _build(copy.deepcopy(stored), tmp_path)
    assert first.archive.read_bytes() == second.archive.read_bytes()
    assert first.bundle_id == second.bundle_id


def test_bundle_id_is_the_manifest_hash_not_a_member(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    manifest = _text(members, res.root, "MANIFEST.sha256")
    assert res.bundle_id == hashlib.sha256(manifest.encode()).hexdigest()
    assert all(name.startswith(res.root) for name in members)
    # the id itself must not ride inside the archive it identifies
    assert res.bundle_id not in b"".join(members.values()).decode(
        "utf-8", errors="ignore"
    )


def test_manifest_lists_every_other_member(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    listed = {
        line.split("  ", 1)[1]
        for line in _text(members, res.root, "MANIFEST.sha256").splitlines()
    }
    names = {n.removeprefix(res.root) for n in members}
    assert listed == names - {"MANIFEST.sha256"}
    # platform.json is checksummed even though D10 excludes it from manifest_id
    assert "env/platform.json" in listed


def test_tampering_fails_before_any_compute(
    tmp_path: Path, stored: dict[str, Any]
) -> None:
    res = _build(stored, tmp_path)
    data = bytearray(res.archive.read_bytes())
    with zipfile.ZipFile(res.archive) as zf:
        info = next(
            i for i in zf.infolist() if i.filename.endswith("outputs/partial.json")
        )
        # This fixture member is nonempty, so the former one-byte read's
        # tell() - 1 was zero. ZipFile.read returns bytes without text IO.
        assert zf.read(info)
        data_start = info.header_offset
    data[data_start] ^= 0xFF
    tampered = tmp_path / "tampered.zip"
    tampered.write_bytes(bytes(data))
    rows = verify_archive(tampered)
    failed = [r["check"] for r in rows if r["status"] == "failed"]
    assert any(c.endswith("outputs/partial.json") for c in failed), failed


def test_offline_index_resolves_locally(stored: dict[str, Any], tmp_path: Path) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    page = _text(members, res.root, "index.html")
    assert "http://" not in page and "https://" not in page
    assert "src=" not in page  # no external assets at all
    for href in ("outputs/partial.json", "envelope.json", "checks/evidence.json"):
        assert f"href='{href}'" in page or f'href="{href}"' in page


def test_evidence_index_reader_path(stored: dict[str, Any], tmp_path: Path) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    index = json.loads(_text(members, res.root, "evidence-index.json"))
    assert index["schema"] == "precis_repro_evidence_index"
    assert index["claims"][0]["value"] == -1.25  # claim → output field resolved
    assert index["chain"]["envelope"] == "envelope.json"
    assert index["chain"]["checks"] == "checks/evidence.json"
    assert "inputs/config.json" in index["chain"]["inputs"]
    values = {
        (v["file"], v["pointer"]): v.get("value") for v in index["values"] if v["file"]
    }
    assert values[("outputs/partial.json", "/states/s1/energy")] == -1.25


def test_crate_metadata_is_standard_and_files_hashed(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    crate = json.loads(_text(members, res.root, "ro-crate-metadata.json"))
    assert crate["@context"] == "https://w3id.org/ro/crate/1.3/context"
    graph = {e["@id"]: e for e in crate["@graph"]}
    root = graph["./"]
    assert root["conformsTo"] == {"@id": "https://w3id.org/ro/wfrun/process/0.6"}
    assert "datePublished" not in root  # wall-clock stamps are not inputs
    action = graph["#seed-run-7"]
    assert action["@type"] == "CreateAction"
    assert action["startTime"] == "2026-11-03T10:14:02Z"  # captured stamp rides
    part = graph["outputs/partial.json"]
    assert (
        part["sha256"]
        == hashlib.sha256(members[f"{res.root}outputs/partial.json"]).hexdigest()
    )


def test_private_weights_and_missing_wheel_disclosed_not_replayable(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path)
    members = _members(res.archive)
    weights = json.loads(_text(members, res.root, "env/weights.json"))
    assert weights["weights_sha256"] == "a" * 64
    assert weights["bundled"] is False
    missing = json.loads(_text(members, res.root, "provenance/missing.json"))["missing"]
    assert "env/wheels/" in missing and "viewer/" in missing
    checks = json.loads(_text(members, res.root, "checks/evidence.json"))["checks"]
    replay = {c["check"]: c["status"] for c in checks if c["scope"] == "replay"}
    assert replay["catpath wheel"] == "unavailable"
    assert replay["re-execution"] == "not-run"


def test_unavailable_artifact_stays_explicit(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    stored["artifact_text"].pop("ts.extxyz")

    def missing_reader(entry: dict[str, Any]) -> bytes:
        if entry["name"] == "ts.extxyz":
            raise FileNotFoundError(entry["name"])
        return stored["artifact_text"][entry["name"]].encode("utf-8")

    res = _build(stored, tmp_path, read_artifact=missing_reader)
    members = _members(res.archive)
    assert f"{res.root}artifacts/job-artifacts/7/ts.extxyz" not in members
    envelope = json.loads(_text(members, res.root, "envelope.json"))
    assert "artifacts/job-artifacts/7/ts.extxyz/data" in envelope["missing"]


def test_publication_gate_refuses_staged_secrets(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    stored["job_meta"]["params"]["config"]["search"]["note"] = (
        "run on spark at 100.126.0.1"  # secret-gate: allow — planted sample
    )
    with pytest.raises(ArchiveRefused, match="publication gate"):
        _build(stored, tmp_path)


def test_unresolvable_claim_pin_refuses(stored: dict[str, Any], tmp_path: Path) -> None:
    stored["claims"] = [
        {"id": "bad", "file": "outputs/partial.json", "pointer": "/nope"}
    ]
    with pytest.raises(ArchiveRefused, match="does not resolve"):
        _build(stored, tmp_path)


def test_unfrozen_build_is_marked_preliminary(
    stored: dict[str, Any], tmp_path: Path
) -> None:
    res = _build(stored, tmp_path, frozen=False)
    assert res.preliminary is True
    members = _members(res.archive)
    index = json.loads(_text(members, res.root, "evidence-index.json"))
    assert index["preliminary"] is True
    crate = json.loads(_text(members, res.root, "ro-crate-metadata.json"))
    root = next(e for e in crate["@graph"] if e["@id"] == "./")
    assert root["creativeWorkStatus"] == "preliminary"
    assert "PRELIMINARY" in _text(members, res.root, "README.md")


def test_plan_preview_matches_the_export(stored: dict[str, Any]) -> None:
    plan = plan_archive(
        job_id=stored["job_id"],
        job_meta=stored["job_meta"],
        pathway_id=stored["pathway_id"],
        pathway_meta=stored["pathway_meta"],
        slug=stored["slug"],
        title=stored["title"],
        frozen=stored["frozen"],
        read_artifact=_reader(stored),
    )
    assert plan["schema"] == "precis_repro_bundle"
    paths = {f["path"] for f in plan["files"]}
    assert {"index.html", "MANIFEST.sha256", "ro-crate-metadata.json"} <= paths
    assert plan["gate_findings"] == {}
    assert plan["preliminary"] is False


def test_missing_rocrate_writer_refuses_with_next(
    stored: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    real_import = builtins.__import__

    def blocked(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("rocrate"):
            raise ImportError(name)
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(ArchiveRefused, match="repro"):
        _build(stored, tmp_path)
