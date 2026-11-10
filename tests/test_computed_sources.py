"""Computed-evidence producer adapters (gr564183): the source contract that
maps a cited value to its exact frozen output bytes, selector, unit and
method, and re-derives every pin at verification time.

No engine, no DB: the pathway fixture is the same stored-seed snapshot as
``tests/test_pathway_bundle.py``; the external adapter runs on plain bytes.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from precis.computed_sources import (
    ADAPTER_EXTERNAL_RESULT,
    ADAPTER_PATHWAY_SEED,
    SOURCE_SCHEMA,
    SOURCE_SCHEMA_VERSION,
    build_external_source,
    build_pathway_source,
    canonical_bytes,
    check_frozen_pins,
    contract_fingerprint,
    require_verified,
    validate_contract,
    verify_source,
)
from precis.errors import BadInput
from precis_pathway import bundle as pathway_bundle
from precis_pathway.bundle import build_envelope

_FIXTURE = Path(__file__).parent / "fixtures" / "pathway" / "repro_seed.json"

_PATH = "outputs/partial.json"
_SELECTOR = "states.s1.energy"


@pytest.fixture
def stored() -> dict[str, Any]:
    return json.loads(_FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture
def envelope(stored: dict[str, Any]) -> dict[str, Any]:
    return _build(stored)


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


def _contract(envelope: dict[str, Any], **over: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "envelope": envelope,
        "path": _PATH,
        "selector": _SELECTOR,
        "literal": "-1.25",
        "unit": "eV",
        "method": "mace:small relaxation",
        "conditions": {"temperature": 300},
    }
    args.update(over)
    return build_pathway_source(**args)


# ── build: the happy path ────────────────────────────────────────────────


def test_valid_pathway_fixture_builds_a_contract_that_verifies(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    assert contract["schema"] == SOURCE_SCHEMA
    assert contract["schema_version"] == SOURCE_SCHEMA_VERSION
    producer = contract["producer"]
    assert producer["adapter"] == ADAPTER_PATHWAY_SEED
    assert producer["job_id"] == 7
    assert producer["job_type"] == "autocatpath_seed"
    assert producer["content_key"] == "seed-key"
    assert producer["pathway_ref_id"] == 11
    assert producer["pathway_content_key"] == "pathway-key"
    assert producer["output_id"] == envelope["output_id"]
    assert producer["bundle_id"] == envelope["bundle_id"]
    output = contract["output"]
    entry = next(o for o in envelope["outputs"] if o["name"] == _PATH)
    assert output["sha256"] == entry["sha256"]
    assert output["encoding"] == "json"
    assert output["selector"] == _SELECTOR
    assert contract["value"] == {
        "literal": "-1.25",
        "unit": "eV",
        "number": -1.25,
        "text": None,
    }
    assert contract["method"]["name"] == "mace:small relaxation"
    assert validate_contract(contract) == []
    assert verify_source(contract, envelope=envelope) == []
    assert require_verified(contract, envelope=envelope) is contract
    # the fingerprint is the content address a freezer stores alongside
    assert contract_fingerprint(contract) == contract_fingerprint(
        copy.deepcopy(contract)
    )
    assert len(contract_fingerprint(contract)) == 64


def test_canonical_bytes_match_the_pathway_envelope_spelling() -> None:
    """Digest interop with envelope pins holds by construction — pinned here
    so a future re-spelling in either module fails loudly, not silently
    re-digests every frozen contract."""
    assert canonical_bytes({"b": [1, 2], "a": "é"}) == pathway_bundle.canonical_bytes(
        {"b": [1, 2], "a": "é"}
    )


def test_a_text_value_contract_verifies(envelope: dict[str, Any]) -> None:
    extxyz = "1\nProperties=species:S:1:pos:R:3\nCu 0 0 0\n"
    contract = _contract(
        envelope,
        path="outputs/structures.json",
        selector="s1",
        literal=extxyz,
        unit="extxyz",
        method="structure dump",
    )
    assert contract["value"]["text"] == extxyz
    assert validate_contract(contract) == []
    assert verify_source(contract, envelope=envelope) == []


# ── build: the actionable omissions and disagreements ────────────────────


def test_missing_selector_unit_and_method_refuse_with_the_fix(
    envelope: dict[str, Any],
) -> None:
    for key in ("selector", "unit", "method"):
        with pytest.raises(BadInput) as ei:
            _contract(envelope, **{key: None})
        assert "selector" in (ei.value.next or "") or key in str(ei.value)
        assert ei.value.next, f"{key} refusal must name the fix"
    with pytest.raises(BadInput, match="selector"):
        _contract(envelope, selector="")
    with pytest.raises(BadInput, match="unit"):
        _contract(envelope, unit="  ")
    with pytest.raises(BadInput, match="method"):
        _contract(envelope, method=None)


def test_a_selector_that_does_not_resolve_names_what_is_there(
    envelope: dict[str, Any],
) -> None:
    with pytest.raises(BadInput, match="available keys") as ei:
        _contract(envelope, selector="states.s2.energy")
    assert "s1" in str(ei.value)  # the available key is named
    with pytest.raises(BadInput, match="cannot descend into"):
        _contract(envelope, selector="states.s1.energy.deeper")


def test_an_unrecorded_output_names_the_recorded_ones(
    envelope: dict[str, Any],
) -> None:
    with pytest.raises(BadInput, match="outputs/lattice.json") as ei:
        _contract(envelope, path="outputs/whoops.json")
    assert "recorded outputs" in str(ei.value)


def test_a_literal_that_disagrees_with_the_bytes_refuses(
    envelope: dict[str, Any],
) -> None:
    with pytest.raises(BadInput, match="resolves to -1.25"):
        _contract(envelope, literal="-1.3")
    with pytest.raises(BadInput, match="not the numeric value"):
        _contract(envelope, literal="about one")


def test_building_against_garbage_refuses(envelope: dict[str, Any]) -> None:
    with pytest.raises(BadInput, match="build_envelope"):
        _contract({"schema": "something_else"})
    # a non-JSON (text) output carries no field to select
    textish = copy.deepcopy(envelope)
    for entry in textish["outputs"]:
        if entry["name"] == _PATH:
            entry["encoding"] = "utf-8"
            entry["data"] = "plain text"
    with pytest.raises(BadInput, match="not 'json'"):
        _contract(textish)


# ── verify: moved pins, moved bytes, moved fields ────────────────────────


def test_a_rebuilt_envelope_with_changed_outputs_stales_every_pin(
    stored: dict[str, Any], envelope: dict[str, Any]
) -> None:
    contract = _contract(envelope)
    moved = copy.deepcopy(stored)
    moved["job_meta"]["partial"]["states"]["s1"]["energy"] = -1.3
    issues = verify_source(contract, envelope=_build(moved))
    kinds = {i.kind for i in issues}
    # the run identity stands but the output identity and bytes moved
    assert "changed-source" in kinds
    assert "changed-output" in kinds
    assert "missing-field" not in kinds
    moved_pins = "\n".join(i.message for i in issues if i.kind == "changed-source")
    assert "output_id" in moved_pins and "bundle_id" in moved_pins


def test_a_tampered_inline_entry_is_changed_output(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    tampered = copy.deepcopy(envelope)
    entry = next(o for o in tampered["outputs"] if o["name"] == _PATH)
    entry["data"]["states"]["s1"]["energy"] = -9.9
    issues = verify_source(contract, envelope=tampered)
    assert [i.kind for i in issues] == ["changed-output"]
    assert "digest moved" in issues[0].message


def test_a_selector_removed_from_the_bytes_is_missing_field(
    stored: dict[str, Any], envelope: dict[str, Any]
) -> None:
    contract = _contract(envelope)
    moved = copy.deepcopy(stored)
    del moved["job_meta"]["partial"]["states"]["s1"]["energy"]
    issues = verify_source(contract, envelope=_build(moved))
    by_kind = {i.kind for i in issues}
    assert "changed-source" in by_kind  # output_id moved
    missing = next(i for i in issues if i.kind == "missing-field")
    assert "available keys" in missing.message


def test_a_hand_edited_contract_value_is_caught(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    contract["value"]["number"] = -1.3
    issues = verify_source(contract, envelope=envelope)
    assert [i.kind for i in issues] == ["changed-output"]
    assert "the contract claims" in issues[0].message


def test_restatement_mismatches_refuse_to_adopt(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    assert [
        i.kind for i in verify_source(contract, envelope=envelope, unit="kJ/mol")
    ] == ["unit-mismatch"]
    assert [
        i.kind for i in verify_source(contract, envelope=envelope, method="DFT-PBE")
    ] == ["method-mismatch"]
    assert [
        i.kind
        for i in verify_source(
            contract, envelope=envelope, conditions={"temperature": 400}
        )
    ] == ["method-mismatch"]
    # restating exactly what was frozen is not a mismatch
    assert (
        verify_source(
            contract,
            envelope=envelope,
            unit="eV",
            method="mace:small relaxation",
            conditions={"temperature": 300},
        )
        == []
    )


def test_verification_needs_the_evidence(envelope: dict[str, Any]) -> None:
    contract = _contract(envelope)
    with pytest.raises(BadInput, match="envelope"):
        verify_source(contract)
    external = build_external_source(
        producer="figrecipe",
        run_key="r1",
        data=b'{"x": 1}',
        path="o.json",
        encoding="json",
        selector="x",
        literal="1",
        unit="1",
        method="m",
    )
    with pytest.raises(BadInput, match="bytes"):
        verify_source(external)


def test_malformed_contracts_verify_as_malformed(envelope: dict[str, Any]) -> None:
    contract = _contract(envelope)
    broken = copy.deepcopy(contract)
    broken["output"]["sha256"] = "not-a-digest"
    issues = verify_source(broken, envelope=envelope)
    assert [i.kind for i in issues] == ["malformed-contract"]
    assert validate_contract({"schema": SOURCE_SCHEMA}) != []
    assert validate_contract("nope") == ["not an object"]


def test_require_verified_raises_naming_every_issue(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    tampered = copy.deepcopy(envelope)
    next(o for o in tampered["outputs"] if o["name"] == _PATH)["data"]["states"]["s1"][
        "energy"
    ] = -9.9
    with pytest.raises(BadInput, match="changed-output") as ei:
        require_verified(contract, envelope=tampered)
    assert "re-derive" in (ei.value.next or "")


# ── frozen pins: a signed artifact is never retargeted ───────────────────


def test_frozen_pins_cover_the_contracts_pathway_pin(
    envelope: dict[str, Any],
) -> None:
    contract = _contract(envelope)
    pins = [
        {
            "pathway_ref_id": 11,
            "content_key": "pathway-key",
            "provenance_key": "urn:precis:pathway:pathway-key",
            "evidence_tier": "computed",
        }
    ]
    assert check_frozen_pins(contract, pins) == []
    # a pin set approved over different evidence does not cover this source
    uncovered = check_frozen_pins(contract, [])
    assert [i.kind for i in uncovered] == ["changed-source"]
    assert (
        "never retarget" in uncovered[0].message or "re-approve" in uncovered[0].message
    )
    other = check_frozen_pins(
        contract, [{"pathway_ref_id": 12, "content_key": "other-key"}]
    )
    assert [i.kind for i in other] == ["changed-source"]
    # None (a payload frozen before the pin set existed) uncovers too
    assert [i.kind for i in check_frozen_pins(contract, None)] == ["changed-source"]
    # a malformed contract fails as malformed, not as uncovered
    kinds = [i.kind for i in check_frozen_pins({"producer": {}}, pins)]
    assert kinds and all(k == "malformed-contract" for k in kinds)


def test_external_contracts_carry_no_pathway_pin(
    envelope: dict[str, Any],
) -> None:
    external = build_external_source(
        producer="figrecipe",
        run_key="r1",
        data=b'{"x": 1}',
        path="o.json",
        encoding="json",
        selector="x",
        literal="1",
        unit="1",
        method="m",
    )
    assert external["producer"]["adapter"] == ADAPTER_EXTERNAL_RESULT
    assert check_frozen_pins(external, []) == []


# ── the generic external adapter ─────────────────────────────────────────


def test_external_json_source_builds_and_verifies() -> None:
    data = b'{"barrier": {"eV": 0.85}, "steps": [{"t": 1}, {"t": 2}]}'
    contract = build_external_source(
        producer="figrecipe",
        run_key="run-9",
        data=data,
        path="outputs/result.json",
        encoding="json",
        selector="barrier.eV",
        literal="0.85",
        unit="eV",
        method="nudged elastic band",
    )
    assert contract["output"]["sha256"] == hashlib.sha256(data).hexdigest()
    assert validate_contract(contract) == []
    assert verify_source(contract, data=data) == []
    # a list index selects an entry; a bad index names the range
    second = build_external_source(
        producer="figrecipe",
        run_key="run-9",
        data=data,
        path="outputs/result.json",
        encoding="json",
        selector="steps.1.t",
        literal="2",
        unit="1",
        method="nudged elastic band",
    )
    assert second["value"]["number"] == 2.0
    with pytest.raises(BadInput, match="index 0..1"):
        build_external_source(
            producer="p",
            run_key="r",
            data=data,
            path="o.json",
            encoding="json",
            selector="steps.9.t",
            literal="1",
            unit="1",
            method="m",
        )
    # changed bytes: the digest moved, never silently retargeted
    moved = b'{"barrier": {"eV": 0.9}, "steps": [{"t": 1}, {"t": 2}]}'
    issues = verify_source(contract, data=moved)
    assert [i.kind for i in issues] == ["changed-output"]
    # missing selector/unit/method refuse the same way
    with pytest.raises(BadInput, match="selector"):
        build_external_source(
            producer="p",
            run_key="r",
            data=data,
            path="o.json",
            encoding="json",
            selector=None,
            literal="0.85",
            unit="eV",
            method="m",
        )
    with pytest.raises(BadInput, match="not valid UTF-8 JSON"):
        build_external_source(
            producer="p",
            run_key="r",
            data=b"\xff\xfe",
            path="o.json",
            encoding="json",
            selector="x",
            literal="1",
            unit="1",
            method="m",
        )


def test_external_csv_source_selects_a_column_and_row() -> None:
    data = b"step,barrier\ns1,0.85\ns2,1.10\n"
    contract = build_external_source(
        producer="scitex-stats",
        run_key="run-3",
        data=data,
        path="outputs/steps.csv",
        encoding="csv",
        selector="barrier",
        row=1,
        literal="1.10",
        unit="eV",
        method="loaded ensemble",
        conditions={"replicas": 4},
    )
    assert contract["value"]["number"] == 1.1
    assert validate_contract(contract) == []
    assert verify_source(contract, data=data) == []
    # the default row is the first data row
    first = build_external_source(
        producer="scitex-stats",
        run_key="run-3",
        data=data,
        path="outputs/steps.csv",
        encoding="csv",
        selector="barrier",
        literal="0.85",
        unit="eV",
        method="loaded ensemble",
    )
    assert first["value"]["number"] == 0.85
    # an unknown column names the header
    with pytest.raises(BadInput, match="available columns"):
        build_external_source(
            producer="p",
            run_key="r",
            data=data,
            path="o.csv",
            encoding="csv",
            selector="whoops",
            literal="1",
            unit="eV",
            method="m",
        )
    # a row past the data names the row range
    with pytest.raises(BadInput, match="data row"):
        build_external_source(
            producer="p",
            run_key="r",
            data=data,
            path="o.csv",
            encoding="csv",
            selector="barrier",
            row=9,
            literal="1",
            unit="eV",
            method="m",
        )
    moved = b"step,barrier\ns1,0.90\ns2,1.10\n"
    assert [i.kind for i in verify_source(contract, data=moved)] == ["changed-output"]
