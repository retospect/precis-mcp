"""Stored seed evidence: byte integrity, immutable selection and honest gaps.

These tests need neither the engine nor a database. Existing engine-backed
code/lock/weights refusal coverage remains in test_pathway_revalidate.py.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from precis_pathway import manifest as vm
from precis_pathway.bundle import EnvelopeRefused, build_envelope, canonical_bytes

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "repro_seed.json"


@pytest.fixture
def stored() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


def _build(stored: dict[str, Any]) -> dict[str, Any]:
    return build_envelope(
        job_id=stored["job_id"],
        job_meta=stored["job_meta"],
        pathway_id=stored["pathway_id"],
        pathway_meta=stored["pathway_meta"],
        read_artifact=lambda entry: stored["artifact_text"][entry["name"]].encode(
            "utf-8"
        ),
    )


def _canonical(value: Any) -> bytes:
    """Independent spelling of the public serialization contract."""
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _no_lookup(*args: Any, **kwargs: Any) -> Any:
    raise AssertionError("export must not infer execution provenance from its host")


def test_stored_envelope_has_stable_bytes_and_separate_hashes(
    stored: dict[str, Any],
) -> None:
    first = _build(stored)
    assert canonical_bytes(first) == canonical_bytes(_build(copy.deepcopy(stored)))
    assert first["schema"] == "precis_pathway_seed_envelope"
    assert first["schema_version"] == 1
    payload = {key: value for key, value in first.items() if key != "bundle_id"}
    assert first["bundle_id"] == hashlib.sha256(_canonical(payload)).hexdigest()
    output_payload = {
        "inline": first["outputs"],
        "artifacts": [
            {key: value for key, value in row.items() if key != "availability"}
            for row in first["artifacts"]
        ],
    }
    assert first["output_id"] == hashlib.sha256(_canonical(output_payload)).hexdigest()
    for record in first["inputs"] + first["outputs"]:
        raw = (
            record["data"].encode("utf-8")
            if record["encoding"] == "utf-8"
            else _canonical(record["data"])
        )
        assert record["sha256"] == hashlib.sha256(raw).hexdigest()
        assert record["bytes"] == len(raw)
    assert {row["name"] for row in first["outputs"]} == {
        "outputs/partial.json",
        "outputs/lattice.json",
        "outputs/structures.json",
        "outputs/bands.json",
        "outputs/ts.json",
    }
    assert canonical_bytes({"β": "é", "a": 1}) == '{"a":1,"β":"é"}'.encode()


def test_export_is_detached_and_does_not_mutate_stored_snapshots(
    stored: dict[str, Any],
) -> None:
    before = copy.deepcopy(stored)
    envelope = _build(stored)
    assert stored == before
    frozen = canonical_bytes(envelope)
    stored["job_meta"]["params"]["config"]["search"]["seeds"].append(4)
    stored["job_meta"]["partial"]["states"].clear()
    stored["pathway_meta"]["validation_manifest"]["platform"]["arch"] = "changed"
    assert canonical_bytes(envelope) == frozen


def test_d10_is_context_not_seed_execution_proof(
    stored: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in (
        "build",
        "engine_manifest",
        "precis_sha",
        "lock_sha256",
        "weights_sha256_for",
    ):
        monkeypatch.setattr(vm, name, _no_lookup)
    envelope = _build(stored)
    context = envelope["pathway_context"]
    assert (
        context["validation_manifest"] == stored["pathway_meta"]["validation_manifest"]
    )
    assert context["manifest_id"] == stored["pathway_meta"]["manifest_id"]
    assert context["execution_binding"].startswith("unproven:")
    assert envelope["run"]["content_key"] == "seed-key"
    assert context["content_key"] == "pathway-key"
    assert envelope["provenance"]["weights_sha256"] == "a" * 64
    for key in (
        "code",
        "lock",
        "wheel",
        "image",
        "platform",
        "units",
        "effective_config",
    ):
        assert envelope["provenance"][key] is None
        assert f"provenance/{key}" in envelope["missing"]
    assert "later_mutable_result" not in canonical_bytes(envelope).decode("utf-8")


def test_output_changes_do_not_redefine_d10(stored: dict[str, Any]) -> None:
    first = _build(stored)
    stored["job_meta"]["partial"]["states"]["s1"]["energy"] = -1.5
    changed = _build(stored)
    assert changed["output_id"] != first["output_id"]
    assert changed["bundle_id"] != first["bundle_id"]
    assert (
        changed["pathway_context"]["manifest_id"]
        == first["pathway_context"]["manifest_id"]
    )


def test_context_changes_leave_output_identity_alone(stored: dict[str, Any]) -> None:
    first = _build(stored)
    stored["pathway_meta"]["validation_manifest"]["platform"]["arch"] = "aarch64"
    changed = _build(stored)
    assert changed["output_id"] == first["output_id"]
    assert changed["bundle_id"] != first["bundle_id"]
    assert (
        changed["pathway_context"]["manifest_id"]
        == first["pathway_context"]["manifest_id"]
    )


def test_external_inventory_order_does_not_change_envelope(
    stored: dict[str, Any],
) -> None:
    first = canonical_bytes(_build(stored))
    stored["job_meta"]["artifacts"].reverse()
    assert canonical_bytes(_build(stored)) == first


@pytest.fixture
def artifact_roots(
    stored: dict[str, Any], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path]:
    root, spool = tmp_path / "root", tmp_path / "spool"
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_ROOT", str(root))
    monkeypatch.setenv("PRECIS_JOB_ARTIFACT_SPOOL", str(spool))
    for index, entry in enumerate(stored["job_meta"]["artifacts"]):
        base = root if index == 0 else spool
        target = base / entry["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(stored["artifact_text"][entry["name"]].encode("utf-8"))
    return root, spool


def _store_build(stored: dict[str, Any]) -> dict[str, Any]:
    return build_envelope(
        job_id=stored["job_id"],
        job_meta=stored["job_meta"],
        pathway_id=stored["pathway_id"],
        pathway_meta=stored["pathway_meta"],
    )


def test_existing_artifact_store_reads_root_and_spool(
    stored: dict[str, Any], artifact_roots: tuple[Path, Path]
) -> None:
    envelope = _store_build(stored)
    assert envelope == _build(stored)
    assert [row["availability"] for row in envelope["artifacts"]] == [
        "verified",
        "verified",
    ]


def test_changed_stored_bytes_refuse(
    stored: dict[str, Any], artifact_roots: tuple[Path, Path]
) -> None:
    root, _ = artifact_roots
    (root / stored["job_meta"]["artifacts"][0]["path"]).write_bytes(b"abd")
    with pytest.raises(EnvelopeRefused, match="sha256 mismatch"):
        _store_build(stored)


def test_recorded_size_disagreement_refuses(stored: dict[str, Any]) -> None:
    stored["job_meta"]["artifacts"][0]["bytes"] = 4
    with pytest.raises(EnvelopeRefused, match="byte count mismatch"):
        _build(stored)


def test_unavailable_file_retains_pin_and_output_identity(
    stored: dict[str, Any], artifact_roots: tuple[Path, Path]
) -> None:
    before = _store_build(stored)
    root, _ = artifact_roots
    entry = stored["job_meta"]["artifacts"][0]
    (root / entry["path"]).unlink()
    after = _store_build(stored)
    assert after["artifacts"][0]["availability"] == "unavailable"
    assert after["artifacts"][0]["sha256"] == entry["sha256"]
    assert f"artifacts/{entry['path']}/data" in after["missing"]
    assert after["output_id"] == before["output_id"]
    assert after["bundle_id"] != before["bundle_id"]


@pytest.mark.parametrize("missing_pin", ["sha256", "bytes"])
def test_unpinned_file_is_never_read_to_invent_provenance(
    stored: dict[str, Any], missing_pin: str
) -> None:
    entry = stored["job_meta"]["artifacts"][0]
    entry.pop(missing_pin)
    stored["job_meta"]["artifacts"] = [entry]
    envelope = build_envelope(
        job_id=stored["job_id"],
        job_meta=stored["job_meta"],
        pathway_id=stored["pathway_id"],
        pathway_meta=stored["pathway_meta"],
        read_artifact=_no_lookup,
    )
    assert envelope["artifacts"][0]["availability"] == "unverified"
    assert envelope["artifacts"][0][missing_pin] is None
    assert f"artifacts/{entry['path']}/{missing_pin}" in envelope["missing"]


def test_absent_provenance_stays_explicit_and_empty_outputs_survive(
    stored: dict[str, Any],
) -> None:
    stored["pathway_meta"] = {}
    for key in ("weights_sha256", "artifacts", "lattice", "model"):
        stored["job_meta"].pop(key)
    stored["job_meta"]["params"].pop("slab_extxyz")
    envelope = _build(stored)
    for key in (
        "pathway_context/manifest_id",
        "pathway_context/validation_manifest",
        "pathway_context/content_key",
        "provenance/weights_sha256",
        "artifacts",
        "outputs/lattice.json",
        "run/model",
        "inputs/slab.extxyz",
    ):
        assert key in envelope["missing"]
    assert envelope["artifacts"] == []
    assert envelope["provenance"]["weights_sha256"] is None
    assert (
        next(row for row in envelope["outputs"] if row["name"] == "outputs/ts.json")[
            "data"
        ]
        == {}
    )
    assert "outputs/ts.json" not in envelope["missing"]


@pytest.mark.parametrize("key", ["seed", "model_index", "content_key"])
def test_contradictory_requested_and_observed_selection_refuses(
    stored: dict[str, Any], key: str
) -> None:
    stored["job_meta"][key] = "contradiction"
    with pytest.raises(EnvelopeRefused, match=key):
        _build(stored)


@pytest.mark.parametrize("pointer", ["pathway_ref", "pathway_ref_id"])
def test_contradictory_pathway_pointer_refuses(
    stored: dict[str, Any], pointer: str
) -> None:
    owner = (
        stored["job_meta"] if pointer == "pathway_ref" else stored["job_meta"]["params"]
    )
    owner[pointer] = 12
    with pytest.raises(EnvelopeRefused, match="pathway_ref mismatch"):
        _build(stored)


def test_missing_pathway_pointer_is_not_guessed(stored: dict[str, Any]) -> None:
    stored["job_meta"].pop("pathway_ref")
    stored["job_meta"]["params"].pop("pathway_ref_id")
    with pytest.raises(EnvelopeRefused, match="pathway_ref mismatch or missing"):
        _build(stored)


def test_other_producer_and_conflicting_manifest_refuse(stored: dict[str, Any]) -> None:
    stored["job_meta"]["job_type"] = "autocatpath_aggregate"
    with pytest.raises(EnvelopeRefused, match="requires an autocatpath_seed"):
        _build(stored)
    stored["job_meta"]["job_type"] = "autocatpath_seed"
    stored["pathway_meta"]["manifest_id"] = "f" * 64
    with pytest.raises(EnvelopeRefused, match="manifest_id disagrees"):
        _build(stored)


@pytest.mark.parametrize(
    "path",
    [
        "../band.extxyz",
        "/band.extxyz",
        "job-artifacts/8/band.extxyz",
        "job-artifacts/7/../band.extxyz",
        "job-artifacts/7/..",
        "job-artifacts/7//band.extxyz",
        "job-artifacts/7/band\\other.extxyz",
    ],
)
def test_artifact_paths_cannot_escape_selected_job(
    stored: dict[str, Any], path: str
) -> None:
    stored["job_meta"]["artifacts"][0]["path"] = path
    with pytest.raises(EnvelopeRefused, match="path must stay"):
        _build(stored)


def test_nonfinite_stored_result_refuses_serialization(stored: dict[str, Any]) -> None:
    stored["job_meta"]["partial"]["states"]["s1"]["energy"] = float("nan")
    with pytest.raises(ValueError, match="Out of range float"):
        _build(stored)
