"""``generated_report_error`` — a bound structure whose stored build report
(``meta.generated.report``) carries ERROR findings must not read clean at
design level (gr464342: view=validate said 0 errors while view=block showed
4 ERROR geom.clash)."""

from __future__ import annotations

from typing import Any

from precis_se.atomic import validate as se_atomic_validate
from precis_se.handler import validate_digest
from precis_se.ops import SeBlock, SeTree


def _tree() -> SeTree:
    tree = SeTree()
    tree.blocks["pillar_foot"] = SeBlock(
        name="pillar_foot",
        envelope="sphere:r2e-10",
        bound_kind="structure",
        bound="d-foot",
    )
    return tree


def _record(n_err: int, n_warn: int) -> dict[str, Any]:
    findings = [
        {
            "severity": "error",
            "code": "geom.clash",
            "where": f"b{i}-t{i}",
            "message": f"{0.54 + i / 10:.2f} A",
        }
        for i in range(n_err)
    ] + [
        {"severity": "warn", "code": "geom.close", "where": f"w{i}", "message": "m"}
        for i in range(n_warn)
    ]
    return {"generator": "x", "report": {"ok": not n_err, "findings": findings}}


def test_stored_error_findings_make_the_design_not_clean() -> None:
    findings = se_atomic_validate.validate_atomic(
        _tree(), generated_records={"d-foot": _record(4, 47)}
    )
    (err,) = [f for f in findings if f.rule == "generated_report_error"]
    assert err.severity == "error"
    assert err.subject == "pillar_foot"
    assert "geom.clash" in err.detail and "x4" in err.detail
    assert "name=pillar_foot" in err.detail
    (warn,) = [f for f in findings if f.rule == "generated_report_warn"]
    assert warn.severity == "warn" and "geom.close x47" in warn.detail
    digest, qualifier = validate_digest(findings)
    assert digest.startswith("validate: 1 error(s), 1 warning(s)") and qualifier


def test_clean_or_missing_report_stays_silent() -> None:
    for rec in ({"generator": "x"}, _record(0, 0), {"report": "junk"}):
        assert (
            se_atomic_validate.validate_atomic(
                _tree(), generated_records={"d-foot": rec}
            )
            == []
        )
