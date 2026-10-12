"""Full synthetic transport examples; not classifier or producer trust tests.

Run this file as a script with --write to refresh the committed wire examples.
Durable production types validate these synthetic fixtures independently of backlog docs.
"""

from __future__ import annotations

import copy
import json
import sys
import types
from decimal import Decimal
from fractions import Fraction
from pathlib import Path
from typing import (
    Any,
    Literal,
    Union,
    get_args,
    get_origin,
    get_type_hints,
    is_typeddict,
)

import pytest

from precis.structure.completion_codec import dumps_wire, loads_wire, to_wire

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/structure/completion-wire.json"
from precis.structure import completion_types as contract


def validate(value: Any, annotation: Any) -> None:
    """Strict keys/types, independent of semantic trust or classifier behavior."""
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any or annotation is object:
        return
    if is_typeddict(annotation):
        assert type(value) is dict
        fields = get_type_hints(annotation)
        assert set(value) == set(fields), (set(value), set(fields))
        for key, field in fields.items():
            validate(value[key], field)
    elif origin in (Union, types.UnionType):
        for choice in args:
            try:
                validate(value, choice)
                return
            except AssertionError:
                pass
        raise AssertionError((value, annotation))
    elif origin is Literal:
        assert any(type(value) is type(item) and value == item for item in args)
    elif origin is list:
        assert type(value) is list
        for item in value:
            validate(item, args[0])
    elif origin is tuple:
        # Wire arrays decode as lists; exact arity and elements still apply.
        assert type(value) in (list, tuple) and len(value) == len(args)
        for item, field in zip(value, args, strict=True):
            validate(item, field)
    elif origin is dict:
        assert type(value) is dict
        for key, item in value.items():
            validate(key, args[0])
            validate(item, args[1])
    else:
        assert type(value) is annotation, (value, annotation)


def blank(annotation: Any) -> Any:
    """Complete skeleton; scenario builders explicitly supply meaningful fields."""
    origin, args = get_origin(annotation), get_args(annotation)
    if annotation is Any or annotation is object:
        return None
    if is_typeddict(annotation):
        return {key: blank(field) for key, field in get_type_hints(annotation).items()}
    if origin in (Union, types.UnionType):
        return None if type(None) in args else blank(args[0])
    if origin is Literal:
        return args[0]
    if origin is list:
        return []
    if origin is tuple:
        return [blank(item) for item in args]
    if origin is dict:
        return {}
    return {str: "synthetic", int: 1, bool: False, float: 0.0, type(None): None}.get(
        annotation
    )


def geometry(version: int = 2) -> dict[str, Any]:
    return {
        "format": 1,
        "ref_id": 9007199254740993,
        "version": version,
        "token": f"synthetic:geometry:v{version}",
    }


def envelope() -> dict[str, Any]:
    item = blank(contract.CompletionEnvelope)
    item.update(
        requested_display={"kind": "current"},
        display_resolution={
            "status": "exact",
            "geometry": geometry(),
            "artifact": None,
            "role": None,
            "reason": None,
        },
        latest_geometry=geometry(),
        shown_geometry=geometry(),
        binding="exact",
        state="unknown",
        reason_codes=["no-trusted-producer", "policy-unavailable"],
        status_token="synthetic:status:unknown",
    )
    item["selected_run"].update(status="not-requested")
    for key in ("run_window", "request_window"):
        item[key].update(limit=64, returned=0, coverage="complete")
    item["earlier_evidence_complete"] = True
    item["next"].update(phase="inspect", reason="No trusted completion evidence")
    return item


def proof() -> dict[str, Any]:
    return {
        "status": "verified",
        "verifier": "SYNTHETIC-TEST-ONLY",
        "verifier_version": "fixture-1",
        "evidence_record_id": "synthetic:proof",
        "bound_receipt_digest": "synthetic:receipt",
    }


def receipt() -> dict[str, Any]:
    item = blank(contract.CompletionReceiptV1)
    item.update(
        request_id=11,
        run_id=12,
        job_id=11,
        policy_id="synthetic:policy",
        policy_digest="synthetic:policy-digest",
    )
    item["input"]["persisted"] = geometry()
    item["output"].update(
        kind="adopted-output",
        persisted=geometry(),
        atom_mapping_digest="synthetic:atom-map",
        mapped_atom_count=1,
        adoption_record_id="synthetic:adoption",
    )
    item["method"].update(
        fidelity="ml",
        model_family="mace_mp",
        model_variant="small",
        checkpoint_sha256="0" * 64,
        backend="synthetic",
        optimizer="FIRE",
        optimizer_settings={"maxstep": Decimal("0.03")},
    )
    item["constraints"].update(
        atoms="free",
        fixed_atom_count=0,
        fixed_axis_count=0,
        atom_mask_digest="synthetic:free",
        additional_constraints=[],
        cell="fixed",
        pbc=[False, False, False],
    )
    item["termination"].update(
        reason="force-converged",
        backend_converged=True,
        steps=300,
        criterion={
            "quantity": "max-atomic-force-norm",
            "units": "eV/angstrom",
            "comparator": "le",
            "target": Decimal("0.05"),
            "scope": "all-atoms-raw",
        },
        initial_energy_eV=Decimal("0.1"),
        final_energy_eV=0.1,
    )
    item["termination"]["forces"].update(
        units="eV/angstrom",
        max_raw=Fraction(1, 20),
        criterion_value=Fraction(1, 20),
        criterion_scope="all-atoms-raw",
        approximate=False,
        output_artifact_digest="synthetic:output-force-binding",
    )
    item["lineage"]["caps"]["steps"] = 300
    item["lineage"]["attempt_usage"]["steps"] = 300
    item["lineage"]["cumulative_usage"]["steps"] = 300
    item["cache"]["kind"] = "fresh"
    return item


def qualified() -> dict[str, Any]:
    item = blank(contract.QualifiedEvidence)
    item.update(
        request_id=11,
        policy_id="synthetic:policy",
        run_id=12,
        role="adopted-output",
        geometry=geometry(),
        level={"fidelity": "ml", "qualification": "converged"},
    )
    parsed = receipt()
    item["constraints"] = parsed["constraints"]
    item["assessment"] = {
        "status": "verified",
        "reason_codes": [],
        "verified": {
            "receipt": parsed,
            "receipt_digest": "synthetic:receipt",
            "producer_proof": proof(),
            "adoption_or_artifact_proof": proof(),
            "consistency_proof": proof(),
        },
    }
    return item


def run_record(evidence: dict[str, Any]) -> dict[str, Any]:
    record = blank(contract.RunRecord)
    record.update(
        ref_id=geometry()["ref_id"],
        run_id=12,
        job_id=11,
        request_id=11,
        on_version=2,
        created_at="2026-10-07T00:00:00Z",
        raw_status="succeeded",
        raw_fidelity="ml",
        raw_model="mace_mp",
        raw_converged=True,
        raw_max_force=Fraction(1, 20),
        raw_n_steps=300,
        receipt_assessment=evidence["assessment"],
    )
    return record


def cases() -> dict[str, Any]:
    values: dict[str, Any] = {"current-unknown": envelope()}
    for role in ("input-request", "adopted-output"):
        item = envelope()
        item.update(
            requested_display={
                "kind": "persisted",
                "role": role,
                "geometry": geometry(),
                "run_id": 12,
            },
            shown_role=role,
        )
        item["display_resolution"]["role"] = role
        item["selected_run"] = {
            "requested_run_id": 12,
            "status": "found",
            "record": run_record(qualified()),
        }
        values[f"same-geometry-{role}"] = item
    for name, version, status in (
        ("stale", 1, "stale"),
        ("historic-unsupported", 0, "unsupported"),
    ):
        item = envelope()
        item.update(
            requested_display={
                "kind": "persisted",
                "role": "input-request",
                "geometry": geometry(version),
                "run_id": None,
            },
            requested_geometry_token=geometry(version)["token"],
            shown_geometry=None,
            binding="stale" if status == "stale" else "historical-unavailable",
            state="unavailable",
            reason_codes=[name],
        )
        item["display_resolution"].update(status=status, geometry=None, reason=name)
        values[name] = item
    item = envelope()
    artifact = blank(contract.ArtifactIdentity)
    artifact.update(artifact_id="synthetic:unadopted", natoms=1)
    item.update(
        requested_display={
            "kind": "artifact",
            "role": "unadopted-artifact",
            "artifact": artifact,
            "run_id": None,
        },
        shown_geometry=None,
        binding="historical-unavailable",
        state="unavailable",
        reason_codes=["artifact-rendering-unsupported"],
    )
    item["display_resolution"].update(
        status="unsupported",
        geometry=None,
        artifact=artifact,
        role="unadopted-artifact",
        reason="no-artifact-loader",
    )
    values["artifact-unsupported"] = item
    item = envelope()
    invalid = qualified()
    invalid["assessment"] = {
        "status": "invalid",
        "verified": None,
        "reason_codes": ["malformed-receipt"],
    }
    item["selected_run"] = {
        "requested_run_id": 12,
        "status": "found",
        "record": run_record(invalid),
    }
    item["reason_codes"] = ["malformed-receipt"]
    values["malformed-receipt"] = item
    item = envelope()
    evidence = qualified()
    item.update(
        state="converged",
        selected_evidence=evidence,
        constraints=evidence["constraints"],
        level=evidence["level"],
        reason_codes=["force-converged-at-final-step", "policy-unavailable"],
    )
    item["selected_run"] = {
        "requested_run_id": 12,
        "status": "found",
        "record": run_record(evidence),
    }
    item["next"].update(
        phase="post-validate", reason="Force pass alone is not finished"
    )
    values["synthetic-final-step-converged"] = item
    capped = copy.deepcopy(item)
    capped.update(state="stopped-unconverged", reason_codes=["step-cap"])
    cap_receipt = capped["selected_evidence"]["assessment"]["verified"]["receipt"]
    cap_receipt["termination"].update(reason="step-cap", backend_converged=False)
    cap_receipt["termination"]["forces"].update(
        max_raw=Decimal("0.1"), criterion_value=Decimal("0.1")
    )
    capped["level"]["qualification"] = "attempted"
    capped["selected_run"]["record"].update(
        raw_converged=False, raw_max_force=Decimal("0.1")
    )
    capped["next"].update(phase="inspect", reason="Cap reached without convergence")
    values["synthetic-capped"] = capped
    item = envelope()
    request = blank(contract.RequestRecord)
    request.update(
        request_id=21,
        job_id=21,
        owner_ref_id=geometry()["ref_id"],
        state="failed",
        created_at="2026-10-07T01:00:00Z",
        failure_reason="synthetic-backend-failure",
    )
    request["input"] = {
        "persisted": geometry(),
        "artifact": None,
        "physical_input_digest": None,
    }
    item.update(
        state="failed",
        selected_request=request,
        request_id=21,
        earlier_qualified_evidence=[qualified()],
        reason_codes=["latest-request-failed"],
    )
    values["failed-with-earlier-evidence"] = item
    return values


def geometry_cases() -> dict[str, Any]:
    values = {}
    for name in ("wrapped-endpoint-proof-missing", "changed-basis-proof-missing"):
        item = blank(contract.MoleculeGeometryEvidence)
        geom = blank(contract.GeometrySnapshot)
        geom.update(
            identity=geometry(),
            source="live-statement",
            lattice=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            pbc=[True, False, False],
        )
        atom = blank(contract.AtomSnapshot)
        atom.update(
            row_id=1,
            label="C1",
            element="C",
            fractional=[Fraction(1, 10), 0, 0],
            fixed_mask=0,
        )
        other = copy.deepcopy(atom)
        other.update(row_id=2, label="C2", fractional=[Fraction(9, 10), 0, 0])
        geom["atoms"] = [atom, other]
        if name == "changed-basis-proof-missing":
            geom["lattice"] = [[0, 1, 0], [1, 0, 0], [0, 0, 1]]
            geom["pbc"] = [False, True, False]
            atom["fractional"] = [0, Fraction(1, 10), 0]
            other["fractional"] = [0, Fraction(9, 10), 0]
        graph = blank(contract.GraphIdentity)
        graph.update(
            geometry=geometry(),
            token=None,
            source="receipt-comparison",
            complete=False,
            comparison_source={
                "persisted": geometry(1),
                "artifact": None,
                "physical_input_digest": None,
            },
            comparison_source_graph_token="synthetic:source-graph",
            atom_mapping_digest="synthetic:atom-map",
            mapped_atom_count=2,
            reason=name,
        )
        item.update(
            geometry=geom,
            geometry_status="available",
            graph=graph,
            completion_status="unavailable",
            reason_codes=[name, "comparison-unavailable"],
        )
        diagnostic = blank(contract.GeometryDiagnostic)
        diagnostic.update(
            diagnostic_id="synthetic:declared-length",
            geometry=geometry(),
            graph=graph,
            family="declared-bond",
            status="unavailable",
            quantity="length",
            units="angstrom",
            reason=name,
        )
        item["diagnostics"] = [diagnostic]
        values[name] = item
    return values


def comparison_context() -> dict[str, Any]:
    source = copy.deepcopy(
        geometry_cases()["wrapped-endpoint-proof-missing"]["geometry"]
    )
    source["identity"] = geometry(1)
    source["source"] = "verified-historic-snapshot"
    source["atoms"][0]["fractional"] = [Fraction(11, 10), 0, 0]
    source["bonds"] = [
        {
            "row_id": 3,
            "i": 1,
            "j": 2,
            "image": [0, 0, 0],
            "order": 1,
            "kind": "pairwise",
            "provenance": "declared",
        }
    ]
    validate(source, contract.GeometrySnapshot)
    return {
        "source_snapshot": source,
        "source_graph_token": "synthetic:source-graph",
        "claimed_atom_map": [[1, 1], [2, 2]],
        "missing_image_proof": "source C1 at 1.1 wraps to target 0.1; target absolute image must change from 0 to 1",
        "missing_basis_proof": "target swaps a/b axes and PBC; atom IDs alone do not prove edge basis correspondence",
        "provenance": "synthetic test inputs, not a trusted historic loader or mapper",
    }


def document() -> dict[str, Any]:
    structures, molecules = cases(), geometry_cases()
    for item in structures.values():
        validate(item, contract.CompletionEnvelope)
        validate(loads_wire(dumps_wire(item)), contract.CompletionEnvelope)
    for item in molecules.values():
        validate(item, contract.MoleculeGeometryEvidence)
        validate(loads_wire(dumps_wire(item)), contract.MoleculeGeometryEvidence)
    return {
        "description": "Complete SYNTHETIC payloads; shape/codec checks only, not classifier or trust proof",
        "completion": to_wire(structures),
        "molecule": to_wire(molecules),
        "comparison_context": to_wire(comparison_context()),
    }


def test_full_fixtures_match_builder_and_required_shapes() -> None:
    assert json.loads(FIXTURE.read_text(encoding="utf-8")) == document()


def test_validator_rejects_missing_extra_bool_identity_and_partial_nested() -> None:
    for mutation in ("missing", "extra", "bool-id", "partial"):
        item = envelope()
        if mutation == "missing":
            del item["finished"]
        elif mutation == "extra":
            item["unexpected"] = None
        elif mutation == "bool-id":
            item["latest_geometry"]["ref_id"] = True
        else:
            del item["next"]["can_submit"]
        with pytest.raises(AssertionError):
            validate(item, contract.CompletionEnvelope)


def test_exact_roundtrip_and_distinct_role_same_geometry() -> None:
    values = cases()
    item = loads_wire(dumps_wire(values["synthetic-final-step-converged"]))
    assert item["latest_geometry"]["ref_id"] == 9007199254740993
    term = item["selected_evidence"]["assessment"]["verified"]["receipt"]["termination"]
    assert term["initial_energy_eV"] == Fraction(1, 10)
    assert term["final_energy_eV"] == Fraction(*(0.1).as_integer_ratio())
    assert term["initial_energy_eV"] != term["final_energy_eV"]
    before, after = (
        values["same-geometry-input-request"],
        values["same-geometry-adopted-output"],
    )
    assert before["shown_geometry"] == after["shown_geometry"]
    assert before["requested_display"] != after["requested_display"]
    assert all(
        not value["finished"] and not value["next"]["can_submit"]
        for value in values.values()
    )


def test_periodic_unavailable_payload_keeps_valid_geometry() -> None:
    source = comparison_context()["source_snapshot"]
    for name, item in geometry_cases().items():
        assert item["geometry_status"] == "available"
        assert item["geometry"]["identity"] == item["graph"]["geometry"]
        assert item["graph"]["comparison_source"]["persisted"] == source["identity"]
        assert item["graph"]["periodic_mapping"] is None
        assert item["graph"]["token"] is None
        assert item["display_bonds"] == []
        assert all(
            d["status"] == "unavailable" and d["value"] is None
            for d in item["diagnostics"]
        )
        if name == "wrapped-endpoint-proof-missing":
            assert (
                source["atoms"][0]["fractional"][0]
                - item["geometry"]["atoms"][0]["fractional"][0]
                == 1
            )
        else:
            assert source["lattice"] != item["geometry"]["lattice"]
            assert source["pbc"] != item["geometry"]["pbc"]


if __name__ == "__main__":
    assert sys.argv[1:] == ["--write"]
    FIXTURE.write_text(
        json.dumps(document(), indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
