"""Pure eligibility prerequisite for step retry; no dispatch or seed allocation.

Only convergence failures explicitly blocking a quantity are selected. A
selection is not permission to run: the pinned engine still executes whole
networks, and durable caps/partial replacement remain separate work.
"""

from __future__ import annotations

from typing import Any


def retryable_convergence_blockers(results: dict[str, Any]) -> list[dict[str, Any]]:
    """Return cited fatal convergence records, preserving their full identity.

    Schema 1/2 ids are opaque (state-at-step and attempt suffixes included)
    and may repeat across models. Never manufacture a target from an error
    string or state-only record. Endpoint failures veto the selection even
    when state-only: assigning a basin mismatch to a step needs diagnosis.
    This does not mutate ``results``; returned records belong to the input.
    """
    schema = results.get("trust_schema")
    if type(schema) is not int or schema not in (1, 2):
        return []
    trust = results.get("trust")
    summary = results.get("trust_summary")
    if not isinstance(trust, list) or not isinstance(summary, dict):
        return []
    records = [r for r in trust if isinstance(r, dict)]
    for record in records:
        if record.get("verdict") != "fail" or record.get("severity") != "fatal":
            continue
        evidence = record.get("evidence")
        if record.get("check") in ("endpoint_identity", "endpoint_agreement") or (
            isinstance(evidence, dict) and evidence.get("reason") == "endpoint-mismatch"
        ):
            return []

    cited: set[str] = set()
    for quantity in ("barrier", "selectivity"):
        verdict = summary.get(quantity)
        if not isinstance(verdict, dict) or verdict.get("available") is not False:
            continue
        blockers = verdict.get("blocked_by")
        if not isinstance(blockers, list):
            continue
        for blocker in blockers:
            reasons = blocker.get("reasons") if isinstance(blocker, dict) else [blocker]
            if isinstance(reasons, list):
                cited.update(reason for reason in reasons if isinstance(reason, str))

    return [
        record
        for record in records
        if isinstance(record.get("id"), str)
        and record["id"] in cited
        and record.get("check") in ("neb_convergence", "relax_convergence")
        and record.get("verdict") == "fail"
        and record.get("severity") == "fatal"
        and isinstance(record.get("step"), str)
        and record["step"].strip()
        and type(record.get("seed")) is int
        and record["seed"] >= 0
    ]
