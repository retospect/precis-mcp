"""Pure completion derivation from coherent, verifier-owned read records.

Legacy database JSON is never a VerifiedReceipt: the read service has no
trusted producer in v1. Synthetic verifier records exercise the scientific
contract without introducing a producer, policy authority, writer or dispatch.
Request state, evidence and effective policy remain separate. Every next phase
has can_submit=False. No clock, database, model or network calls live here.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from decimal import Decimal
from fractions import Fraction
from typing import Any, cast

from .completion_codec import CodecError, canonical_number
from .completion_identity import (
    InvalidGeometry,
    exact_token,
    identity,
    integer,
    stored_graph_token,
)
from .completion_types import (
    CompletionEnvelope,
    CompletionSnapshot,
    DisplaySelector,
    EffectivePolicy,
    SeCompletionEnvelope,
    SeCompletionSnapshot,
    VerifiedReceipt,
)
from .completion_validation import valid_record


def _next(
    phase: str = "inspect", reason: str = "Inspect evidence and applicable policy"
) -> dict[str, Any]:
    return {
        "phase": phase,
        "fidelity": None,
        "reason": reason,
        "can_submit": False,
        "resources": None,
    }


def _safe_status(value: Any) -> Any:
    # Encode untrusted mapping contents as ordered pairs so reserved wire tag
    # keys cannot masquerade as numeric transport. No wall clock or repr(obj).
    if isinstance(value, Mapping):
        return {
            "mapping": [
                [str(k), _safe_status(v)]
                for k, v in sorted(value.items(), key=lambda pair: str(pair[0]))
            ]
        }
    if isinstance(value, (list, tuple)):
        return [_safe_status(v) for v in value]
    if type(value) in (float, Decimal, Fraction):
        try:
            return canonical_number(value)
        except CodecError:
            return {"invalid_numeric": str(value)}
    if value is None or type(value) in (str, bool, int):
        return value
    return {"unsupported_type": type(value).__name__}


def _seal(envelope: dict[str, Any], snapshot: Mapping[str, Any]) -> CompletionEnvelope:
    envelope["status_token"] = exact_token(
        "structure.completion-status",
        _safe_status(
            {
                "envelope": {k: v for k, v in envelope.items() if k != "status_token"},
                "runs": snapshot.get("runs"),
                "requests": snapshot.get("requests"),
                "selected_run": snapshot.get("selected_run"),
            }
        ),
    )
    return cast(CompletionEnvelope, envelope)


def validate_selector(value: Any) -> DisplaySelector:
    if not isinstance(value, dict):
        raise InvalidGeometry("display selector must be an object")
    kind = value.get("kind")
    if kind == "current" and set(value) == {"kind"}:
        return cast(DisplaySelector, dict(value))
    if kind == "persisted" and set(value) == {"kind", "role", "geometry", "run_id"}:
        if value["role"] not in ("input-request", "adopted-output"):
            raise InvalidGeometry("unsupported persisted role")
        identity(value["geometry"])
    elif kind == "artifact" and set(value) == {"kind", "role", "artifact", "run_id"}:
        if value["role"] != "unadopted-artifact":
            raise InvalidGeometry("unsupported artifact role")
        artifact = value["artifact"]
        required = {
            "artifact_id",
            "snapshot_codec",
            "snapshot_digest",
            "atom_identity_digest",
            "graph_digest",
            "cell_digest",
            "natoms",
        }
        if not isinstance(artifact, dict) or set(artifact) != required:
            raise InvalidGeometry("malformed artifact identity")
        integer(artifact["natoms"], minimum=0)
        if any(
            type(artifact[k]) is not str or not artifact[k]
            for k in required - {"natoms", "graph_digest"}
        ):
            raise InvalidGeometry("missing artifact identity field")
        if artifact["graph_digest"] is not None and not isinstance(
            artifact["graph_digest"], str
        ):
            raise InvalidGeometry("invalid artifact graph identity")
    else:
        raise InvalidGeometry("unknown display selector or extra fields")
    if value["run_id"] is not None:
        integer(value["run_id"], minimum=1)
    return cast(DisplaySelector, copy.deepcopy(value))


def _proof(value: Any, digest: str | None = None) -> bool:
    return (
        isinstance(value, dict)
        and value.get("status") == "verified"
        and all(
            isinstance(value.get(k), str) and value[k]
            for k in (
                "verifier",
                "verifier_version",
                "evidence_record_id",
                "bound_receipt_digest",
            )
        )
        and (digest is None or value["bound_receipt_digest"] == digest)
    )


def _constraint_known(c: Mapping[str, Any]) -> bool:
    if c.get("atoms") not in ("free", "constrained") or c.get("cell") not in (
        "fixed",
        "inplane",
        "full",
    ):
        return False
    if not isinstance(c.get("additional_constraints"), list) or not isinstance(
        c.get("atom_mask_digest"), str
    ):
        return False
    if (
        not isinstance(c.get("pbc"), (list, tuple))
        or len(c["pbc"]) != 3
        or any(type(v) is not bool for v in c["pbc"])
    ):
        return False
    for key in ("fixed_atom_count", "fixed_axis_count"):
        if type(c.get(key)) is not int or c[key] < 0:
            return False
    return c["atoms"] != "free" or (
        c["fixed_atom_count"] == c["fixed_axis_count"] == 0
        and not c["additional_constraints"]
    )


def _termination(receipt: Mapping[str, Any]) -> str | None:
    term, constraints = receipt.get("termination"), receipt.get("constraints")
    if (
        not isinstance(term, dict)
        or not isinstance(constraints, dict)
        or not _constraint_known(constraints)
    ):
        return None
    if type(term.get("steps")) is not int or term["steps"] < 0:
        return None
    reason, converged = term.get("reason"), term.get("backend_converged")
    if reason in ("step-cap", "time-cap", "resource-limit", "cancelled"):
        return "stopped-unconverged" if converged is False else None
    if reason == "backend-failure":
        return "failed" if converged is False else None
    if reason != "force-converged" or converged is not True:
        return None
    criterion, forces = term.get("criterion"), term.get("forces")
    if not isinstance(criterion, dict) or not isinstance(forces, dict):
        return None
    if (
        criterion.get("quantity") != "max-atomic-force-norm"
        or criterion.get("units") != "eV/angstrom"
        or forces.get("units") != "eV/angstrom"
    ):
        return None
    scope = criterion.get("scope")
    if (
        scope not in ("all-atoms-raw", "constraint-projected")
        or scope != forces.get("criterion_scope")
        or forces.get("approximate") is not False
    ):
        return None
    if constraints["atoms"] == "free" and scope != "all-atoms-raw":
        return None
    if (
        not isinstance(forces.get("output_artifact_digest"), str)
        or not forces["output_artifact_digest"]
    ):
        return None
    target = canonical_number(criterion.get("target"))
    observed = canonical_number(forces.get("criterion_value"))
    actual = canonical_number(
        forces.get("max_raw" if scope == "all-atoms-raw" else "max_projected")
    )
    if target <= 0 or observed < 0 or actual != observed:
        return None
    comparison = criterion.get("comparator")
    if not (
        (comparison == "lt" and observed < target)
        or (comparison == "le" and observed <= target)
    ):
        return None
    # Cell optimization has additional criteria, not inferred from atomic force.
    if constraints["cell"] != "fixed":
        return None  # no reviewed cell-convergence verifier in v1
    return "converged"


def _evidence(
    run: Mapping[str, Any], shown: Any, role: str | None, ref_id: int
) -> tuple[dict[str, Any], str] | None:
    assessment = run.get("receipt_assessment")
    if (
        not isinstance(assessment, dict)
        or assessment.get("status") != "verified"
        or run.get("ref_id") != ref_id
    ):
        return None
    verified = assessment.get("verified")
    if not isinstance(verified, dict) or not valid_record(verified, VerifiedReceipt):
        return None
    digest = verified["receipt_digest"]
    if not all(
        _proof(verified.get(key), digest)
        for key in ("producer_proof", "adoption_or_artifact_proof", "consistency_proof")
    ):
        return None
    receipt = verified.get("receipt")
    if (
        not isinstance(receipt, dict)
        or type(receipt.get("format")) is not int
        or receipt["format"] != 1
        or receipt.get("run_id") != run.get("run_id")
    ):
        return None
    for key in ("job_id", "request_id"):
        if receipt.get(key) != run.get(key):
            return None
    output, inp = receipt.get("output"), receipt.get("input")
    if not isinstance(output, dict) or not isinstance(inp, dict):
        return None
    output_match = (
        output.get("kind") == "adopted-output" and output.get("persisted") == shown
    )
    input_match = inp.get("persisted") == shown
    # A coincident input/output needs an explicit role; no favorable inference.
    if role is None:
        if output_match and not input_match:
            role = "adopted-output"
        elif input_match and not output_match:
            role = "input-request"
        else:
            return None
    state: str | None
    if role == "input-request":
        if not input_match or receipt.get("method", {}).get("fidelity") != "stick":
            return None  # output forces never qualify input geometry
        state = "preview-only"
    elif role == "adopted-output" and output_match:
        if (
            not output.get("adoption_record_id")
            or not output.get("atom_mapping_digest")
            or type(output.get("mapped_atom_count")) is not int
            or output["mapped_atom_count"] <= 0
        ):
            return None
        state = _termination(receipt)
        if state is None:
            return None
        term = receipt["termination"]
        if (
            run.get("raw_converged") is not None
            and run["raw_converged"] != term["backend_converged"]
        ):
            return None
        if run.get("raw_n_steps") is not None and run["raw_n_steps"] != term["steps"]:
            return None
        if run.get("raw_status") == "failed" and state != "failed":
            return None
        if (
            run.get("raw_max_force") is not None
            and term["forces"].get("criterion_value") is not None
        ):
            if canonical_number(run["raw_max_force"]) != canonical_number(
                term["forces"]["criterion_value"]
            ):
                return None
        caps = receipt.get("lineage", {}).get("caps", {})
        if caps.get("steps") is not None and term["steps"] > integer(
            caps["steps"], minimum=0
        ):
            return None
    else:
        return None
    cache = receipt.get("cache")
    if not isinstance(cache, dict) or cache.get("kind") not in ("fresh", "cache-reuse"):
        return None
    if cache["kind"] == "cache-reuse":
        origin = cache.get("source")
        if (
            not isinstance(origin, dict)
            or cache.get("source_lookup")
            not in ("self-contained", "coherent-direct-read")
            or not _proof(cache.get("mapping_proof"))
            or not cache.get("reuse_mapping_digest")
        ):
            return None
        if type(cache.get("mapped_atom_count")) is not int or cache[
            "mapped_atom_count"
        ] != output.get("mapped_atom_count"):
            return None
        if not all(
            _proof(origin.get(k), origin.get("receipt_digest"))
            for k in ("producer_proof", "output_proof")
        ):
            return None
        if any(
            origin.get(k) != receipt.get(k)
            for k in ("method", "constraints", "termination")
        ):
            return None
        if (
            integer(origin["ref_id"], minimum=1) < 1
            or integer(origin["run_id"], minimum=1) < 1
        ):
            return None
        source_input, source_output = origin["input"], origin["output"]
        if not (source_input["persisted"] or source_input["artifact"]):
            return None
        if source_output["kind"] == "adopted-output":
            persisted = source_output["persisted"]
            if not persisted or identity(persisted)["ref_id"] != origin["ref_id"]:
                return None
        elif source_output["kind"] == "unadopted-artifact":
            if not source_output["artifact"]:
                return None
        else:
            return None
    qualification = "converged" if state == "converged" else "attempted"
    return {
        "request_id": receipt.get("request_id"),
        "policy_id": receipt.get("policy_id"),
        "run_id": run["run_id"],
        "role": role,
        "geometry": shown,
        "artifact": None,
        "assessment": copy.deepcopy(assessment),
        "level": {
            "fidelity": receipt.get("method", {}).get("fidelity"),
            "qualification": qualification,
        },
        "constraints": copy.deepcopy(receipt["constraints"]),
        "post_checks": copy.deepcopy(receipt.get("post_checks")),
    }, state


def _post_validated(
    evidence: Mapping[str, Any], policy: Mapping[str, Any], graph_token: str
) -> bool:
    receipt = evidence["assessment"]["verified"]["receipt"]
    if policy.get("request_id") is not None and policy["request_id"] != receipt.get(
        "request_id"
    ):
        return False
    checks = evidence.get("post_checks")
    if (
        not isinstance(checks, dict)
        or checks.get("format") != 1
        or checks.get("state") != "complete"
        or checks.get("outcome") != "pass"
    ):
        return False
    if (
        checks.get("geometry") != evidence["geometry"]
        or checks.get("artifact") is not None
        or checks.get("graph_digest") != graph_token
        or not checks.get("suite_version")
    ):
        return False
    if checks.get("policy_id") != policy.get("policy_id") or checks.get(
        "policy_digest"
    ) != policy.get("policy_digest"):
        return False
    if receipt.get("policy_id") != policy.get("policy_id") or receipt.get(
        "policy_digest"
    ) != policy.get("policy_digest"):
        return False
    if (
        receipt.get("method") != policy.get("method")
        or receipt.get("constraints") != policy.get("constraints")
        or receipt.get("termination", {}).get("criterion") != policy.get("criterion")
    ):
        return False
    required = policy.get("required_post_checks")
    if (
        not isinstance(required, list)
        or not required
        or checks.get("required") != required
    ):
        return False
    results = checks.get("results")
    if not isinstance(results, list) or len(results) != len(required):
        return False
    if len({r.get("check_id") for r in required}) != len(required):
        return False
    return all(
        sum(
            result.get("requirement") == requirement
            and result.get("outcome") == "pass"
            and bool(result.get("evidence_id"))
            for result in results
        )
        == 1
        for requirement in required
    )


def completion_envelope(
    snapshot: CompletionSnapshot,
    *,
    shown_geometry_token: str | None = None,
    requested_display: DisplaySelector | None = None,
    selected_run_id: int | None = None,
    policy: EffectivePolicy | None = None,
) -> CompletionEnvelope:
    """Derive deterministic evidence without compute, writes or implicit roles."""
    latest = snapshot["geometry"]["identity"] if snapshot["geometry"] else None
    resolution: dict[str, Any] = copy.deepcopy(dict(snapshot["display_resolution"]))
    e: dict[str, Any] = {
        "format": 1,
        "requested_display": copy.deepcopy(snapshot["requested_display"]),
        "display_resolution": resolution,
        "requested_geometry_token": shown_geometry_token,
        "latest_geometry": latest,
        "shown_geometry": resolution["geometry"],
        "shown_role": resolution["role"],
        "binding": "exact",
        "state": "unknown",
        "reason_codes": [],
        "request_id": None,
        "policy_id": None,
        "selected_request": None,
        "selected_run": copy.deepcopy(snapshot["selected_run"]),
        "effective_policy": None,
        "selected_evidence": None,
        "earlier_qualified_evidence": [],
        "constraints": None,
        "level": None,
        "post_checks": None,
        "finished": False,
        "next": _next(),
        "run_window": copy.deepcopy(snapshot["run_window"]),
        "request_window": copy.deepcopy(snapshot["request_window"]),
        "earlier_evidence_complete": all(
            window["coverage"] == "complete" and not window["truncated"]
            for window in (snapshot["run_window"], snapshot["request_window"])
        ),
        "status_token": "",
    }

    def unavailable(code: str, binding: str = "unknown") -> CompletionEnvelope:
        e.update(
            binding=binding,
            state="unknown",
            shown_geometry=None,
            shown_role=None,
            selected_evidence=None,
            level=None,
            constraints=None,
            post_checks=None,
            finished=False,
        )
        e["reason_codes"].append(code)
        e["next"] = _next(
            reason="Reload exact geometry or resolve the explicit selection"
        )
        return _seal(e, snapshot)

    try:
        requested = validate_selector(
            requested_display
            if requested_display is not None
            else snapshot["requested_display"]
        )
    except (InvalidGeometry, CodecError, TypeError, KeyError):
        resolution.update(status="invalid", geometry=None, reason="invalid-selector")
        return unavailable("invalid-selector")
    e["requested_display"] = requested
    if requested != snapshot["requested_display"] or (
        selected_run_id is not None
        and selected_run_id != snapshot["selected_run"]["requested_run_id"]
    ):
        resolution.update(
            status="conflict", geometry=None, reason="selection-capture-mismatch"
        )
        return unavailable("selection-capture-mismatch")
    if snapshot["snapshot_consistency"] != "single-statement":
        return unavailable("incoherent-snapshot")
    if resolution["status"] != "exact":
        binding = (
            "historical-unavailable"
            if resolution["status"] == "unsupported"
            else "stale"
            if resolution["status"] == "stale"
            else "unknown"
        )
        return unavailable(resolution["reason"] or "selection-unavailable", binding)
    shown = resolution["geometry"]
    if shown is None:
        return unavailable(snapshot["geometry_reason"] or "geometry-unavailable")
    if (
        (
            requested["kind"] == "current"
            and (shown != latest or resolution["role"] is not None)
        )
        or (
            requested["kind"] == "persisted"
            and (
                shown != requested["geometry"]
                or resolution["role"] != requested["role"]
            )
        )
        or requested["kind"] == "artifact"
    ):
        resolution.update(
            status="conflict", geometry=None, reason="display-resolution-mismatch"
        )
        return unavailable("display-resolution-mismatch")
    if (
        requested.get("run_id") is not None
        and requested.get("run_id") != snapshot["selected_run"]["requested_run_id"]
    ):
        resolution.update(
            status="conflict", geometry=None, reason="display-run-mismatch"
        )
        return unavailable("display-run-mismatch")
    if shown_geometry_token is not None and shown_geometry_token != shown["token"]:
        resolution.update(status="stale", geometry=None, reason="geometry-changed")
        return unavailable("geometry-changed", "stale")
    if requested["kind"] == "current":
        captured = snapshot["geometry"]
    else:
        captured = snapshot["selected_geometry"] or snapshot["geometry"]
    if (
        not captured
        or captured["identity"] != shown
        or (shown != latest and captured["source"] != "verified-historic-snapshot")
    ):
        return unavailable("historic-snapshot-unavailable", "historical-unavailable")
    if snapshot["selected_run"]["status"] not in ("not-requested", "found"):
        return unavailable("selected-run-" + snapshot["selected_run"]["status"])
    if (
        policy is not None
        and valid_record(policy, EffectivePolicy)
        and _proof(policy.get("authority_proof"))
    ):
        e["effective_policy"] = copy.deepcopy(policy)
        e["policy_id"] = policy["policy_id"]
    else:
        policy = None
        e["reason_codes"].append("no-effective-policy")
    runs = list(snapshot["runs"])
    selected = snapshot["selected_run"]["record"]
    if selected and not any(r["run_id"] == selected["run_id"] for r in runs):
        runs.append(selected)
    runs.sort(
        key=lambda run: (run.get("created_at") or "", run["run_id"]), reverse=True
    )
    qualified: list[tuple[dict[str, Any], str]] = []
    for run in runs:
        try:
            result = _evidence(run, shown, e["shown_role"], snapshot["ref_id"])
        except (
            CodecError,
            InvalidGeometry,
            ValueError,
            KeyError,
            TypeError,
            AttributeError,
        ):
            result = None
        if result is not None:
            evidence, _state = result
            receipt = evidence["assessment"]["verified"]["receipt"]
            if list(evidence["constraints"].get("pbc") or []) != list(
                captured["pbc"]
            ) or (
                evidence["role"] == "adopted-output"
                and receipt["output"]["mapped_atom_count"] != len(captured["atoms"])
            ):
                result = None
        if result is not None:
            qualified.append(result)
        elif (
            e["selected_run"]["record"] is not None
            and e["selected_run"]["record"]["run_id"] == run["run_id"]
        ):
            # A verifier record may be valid for another role/history, but is
            # unavailable for this selection. Never return malformed verified
            # content as a qualified selected-record shape.
            e["selected_run"]["record"]["receipt_assessment"] = {
                "status": "unavailable",
                "verified": None,
                "reason_codes": ["receipt-not-qualified-for-selection"],
            }
    requests = sorted(
        (
            r
            for r in snapshot["requests"]
            if r["owner_ref_id"] == snapshot["ref_id"]
            and r["input"]
            and r["input"]["persisted"] == shown
            and (
                policy is None
                or (
                    r["policy_id"] == policy["policy_id"]
                    and r["policy_digest"] == policy["policy_digest"]
                )
            )
        ),
        key=lambda r: (r["created_at"] or "", r["request_id"]),
        reverse=True,
    )
    request = (
        next(
            (r for r in requests if policy and policy["request_id"] == r["request_id"]),
            None,
        )
        if policy and policy["request_id"] is not None
        else (requests[0] if requests else None)
    )
    if selected is not None:
        request = next(
            (r for r in requests if r["request_id"] == selected["request_id"]), None
        )
        qualified_selected = next(
            (q for q in qualified if q[0]["run_id"] == selected["run_id"]), None
        )
    elif policy is not None and policy["request_id"] is not None and request is None:
        qualified_selected = None
        e["reason_codes"].append("policy-request-not-captured")
    elif request is not None:
        qualified_selected = next(
            (q for q in qualified if q[0]["request_id"] == request["request_id"]), None
        )
    elif (
        snapshot["request_window"]["coverage"] != "complete"
        or snapshot["request_window"]["truncated"]
    ):
        # A newer relevant request may be outside the captured window. Keep
        # older evidence visible, but do not promote it to current selection.
        qualified_selected = None
        e["reason_codes"].append("request-selection-incomplete")
    else:
        qualified_selected = next(
            (
                q
                for q in qualified
                if policy is None or q[0]["policy_id"] == policy["policy_id"]
            ),
            None,
        )
    if request is not None:
        e.update(
            selected_request=copy.deepcopy(request), request_id=request["request_id"]
        )
        if request["state"] in ("queued", "running", "failed", "cancelled"):
            e["state"] = (
                "stopped-unconverged"
                if request["state"] == "cancelled"
                else request["state"]
            )
            e["reason_codes"].append("current-request-" + request["state"])
    e["earlier_qualified_evidence"] = [
        q[0] for q in qualified if q is not qualified_selected
    ]
    if qualified_selected is not None:
        evidence, evidence_state = qualified_selected
        e.update(
            selected_evidence=evidence,
            constraints=evidence["constraints"],
            level=evidence["level"],
            post_checks=evidence["post_checks"],
        )
        if e["state"] == "unknown":
            e["state"] = evidence_state
        if (
            policy is not None
            and evidence_state == "converged"
            and e["state"] == "converged"
        ):
            e["finished"] = _post_validated(
                evidence, policy, stored_graph_token(captured)
            )
            if e["finished"]:
                evidence["level"]["qualification"] = "post-validated"
    elif runs:
        e["reason_codes"].append("no-qualified-exact-receipt")
    if policy is not None:
        holds = policy["holds"]
        if policy["state"] == "unavailable":
            e.update(state="unavailable", finished=False)
        elif policy["state"] == "held" or holds:
            e["state"] = "held"
            if any(h["blocks_completion"] for h in holds):
                e["finished"] = False
        if policy["state"] == "unknown":
            e["finished"] = False
    if e["state"] in ("queued", "running"):
        e["next"] = _next(
            "wait", "Wait for the existing request; reading never submits work"
        )
    elif e["state"] == "held":
        e["next"] = _next("resolve-hold", "Resolve the recorded policy hold")
    elif e["state"] == "converged" and not e["finished"]:
        e["next"] = _next(
            "post-validate",
            "Force evidence does not replace complete policy-bound post-checks",
        )
    return _seal(e, snapshot)


def se_completion_envelope(
    snapshot: SeCompletionSnapshot, *, shown_design_token: str | None = None
) -> SeCompletionEnvelope:
    """All captured blocks participate; physical assembly is never qualified."""
    design, latest = snapshot["design"], snapshot["latest_design"]
    exact = (
        design is not None
        and snapshot["complete_inventory"]
        and snapshot["snapshot_consistency"]
        in ("single-statement", "verified-historic-snapshot")
        and (shown_design_token is None or shown_design_token == design["token"])
    )
    stale = bool(
        shown_design_token is not None
        and design
        and shown_design_token != design["token"]
    )
    counts: dict[str, Any] = {
        "all_blocks": len(snapshot["blocks"]),
        "atomistic_blocks": 0,
        "non_atomistic_blocks": 0,
        "unknown_applicability_blocks": 0,
        "unknown_binding_blocks": 0,
        "atomistic_blocks_finished": 0,
        "states": {},
    }
    blocks = []
    seen: set[int] = set()
    for block in snapshot["blocks"]:
        reasons = []
        applicability = block["applicability"]
        counts[
            {
                "atomistic": "atomistic_blocks",
                "non-atomistic": "non_atomistic_blocks",
                "unknown": "unknown_applicability_blocks",
            }[applicability]
        ] += 1
        nested = block["completion"]
        valid = (
            exact
            and applicability == "atomistic"
            and block["block_uid"] not in seen
            and block["binding_status"] == "exact"
            and block["binding_token"] is not None
            and block["geometry"] is not None
            and block["resolved_structure_id"] == block["geometry"]["ref_id"]
            and nested is not None
            and nested["binding"] == "exact"
            and nested["shown_geometry"] == block["geometry"]
        )
        seen.add(block["block_uid"])
        if applicability == "non-atomistic":
            state = "not-applicable"
        elif not valid:
            state = "unknown"
            counts["unknown_binding_blocks"] += 1
            reasons.append("unverified-block-binding")
        else:
            assert nested is not None
            state = nested["state"]
            if nested["finished"]:
                counts["atomistic_blocks_finished"] += 1
        counts["states"][state] = counts["states"].get(state, 0) + 1
        blocks.append(
            {
                "block_uid": block["block_uid"],
                "name": block["name"],
                "applicability": applicability,
                "binding_status": block["binding_status"]
                if valid or applicability == "non-atomistic"
                else "unknown",
                "resolved_structure_id": block["resolved_structure_id"],
                "binding_token": block["binding_token"],
                "geometry": block["geometry"],
                "completion": copy.deepcopy(nested) if valid else None,
                "reason_codes": reasons,
            }
        )
    all_finished = bool(
        exact
        and counts["atomistic_blocks"] > 0
        and counts["unknown_applicability_blocks"] == 0
        and counts["unknown_binding_blocks"] == 0
        and counts["atomistic_blocks_finished"] == counts["atomistic_blocks"]
    )
    states = set(counts["states"])
    state = (
        "unknown"
        if not exact
        else "not-applicable"
        if not blocks or states == {"not-applicable"}
        else next(iter(states))
        if len(states) == 1
        else "mixed"
    )
    result: dict[str, Any] = {
        "format": 1,
        "requested_design_token": shown_design_token,
        "latest_design": latest,
        "shown_design": design if exact else None,
        "binding": "stale" if stale else "exact" if exact else "unknown",
        "state": state,
        "reason_codes": list(snapshot["reason_codes"])
        + (["design-changed"] if stale else []),
        "scope": "per-block-only",
        "complete_inventory": snapshot["complete_inventory"],
        "counts": counts,
        "blocks": blocks,
        "all_atomistic_blocks_finished": all_finished,
        "physical_assembly_assessed": False,
        "finished": False,
        "next": _next(
            reason="Independent blocks do not establish physical assembly convergence"
        ),
        "status_token": "",
    }
    result["status_token"] = exact_token(
        "se.completion-status",
        _safe_status({k: v for k, v in result.items() if k != "status_token"}),
    )
    return cast(SeCompletionEnvelope, result)


def completion_digest(envelope: Mapping[str, Any]) -> str:
    """Short human view of the same result, never an independent classifier."""
    role = envelope.get("shown_role") or "current"
    return (
        f"Completion: {envelope['state']} ({role}); finished={str(envelope['finished']).lower()}\n"
        f"Next: {envelope['next']['phase']} — {envelope['next']['reason']}\n"
        f"Submission enabled: false\nStatus token: {envelope['status_token']}"
    )
