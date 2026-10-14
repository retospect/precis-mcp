"""``generated_report_error`` — a bound structure whose stored build report
(``meta.generated.report``) carries ERROR findings must not read clean at
design level (gr464342: view=validate said 0 errors while view=block showed
4 ERROR geom.clash)."""

from __future__ import annotations

import json
from typing import Any

import numpy as np
import pytest

from hexfold.report import Finding, Report, Severity
from precis.dispatch import Hub
from precis.store import Store
from precis.structure.cell import Cell
from precis.structure.scene import Atom, Scene
from precis_se.atomic import validate as se_atomic_validate
from precis_se.atomic.generate import generated_record
from precis_se.handler import SeHandler, _render_tree, validate_digest
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


def _hexfold_record() -> dict[str, Any]:
    """Use the producer's serialization, including its uppercase severity names."""
    report = Report(
        tuple(
            Finding("geom.clash", Severity.ERROR, "0.54 A", where=f"b{i}-t{i}")
            for i in range(4)
        )
        + (
            Finding("angle.dev", Severity.WARN, "angle deviation"),
            Finding("geom.summary", Severity.INFO, "geometry checked"),
        )
    )
    return generated_record("hexfold_scene", {"report": report.to_dict()})


@pytest.mark.parametrize(
    "block_names", [("pillar_foot",), ("pillar_foot", "other_foot")]
)
def test_stored_hexfold_report_reaches_design_views(
    store: Store, block_names: tuple[str, ...]
) -> None:
    record = _hexfold_record()
    assert record["report"]["findings"][0]["severity"] == "ERROR"
    scene = Scene(
        cell=Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)),
        atoms={"aC1": Atom("aC1", "C", np.zeros(3))},
    )
    store.structure_save(
        slug="stored-foot",
        title="Stored generated foot",
        scene=scene,
        version=1,
        card_text="Stored generated report regression fixture.",
        meta_extra={"generated": record},
    )
    handler = SeHandler(hub=Hub(store=store))
    ops = []
    for name in block_names:
        ops.extend(
            [
                {"op": "add_block", "name": name, "envelope": "sphere:r2e-10"},
                {
                    "op": "bind_structure",
                    "block": name,
                    "design": "stored-foot",
                    "ports": {},
                },
            ]
        )
    handler.put(id="stored-report-design", text=json.dumps({"ops": ops}))

    validation = handler.get(id="stored-report-design", view="validate").body
    for name in block_names:
        error_line = next(
            line
            for line in validation.splitlines()
            if "generated_report_error" in line and f"block {name!r}" in line
        )
        assert "geom.clash x4" in error_line
        warning_line = next(
            line
            for line in validation.splitlines()
            if "generated_report_warn" in line and f"block {name!r}" in line
        )
        assert "angle.dev x1" in warning_line
    if len(block_names) == 1:
        drc = handler.get(id="stored-report-design", view="drc").body
        assert "generated_report_error" in drc and "geom.clash x4" in drc
        assert "pillar_foot" in drc
    for view in (None, "tree"):
        digest = handler.get(id="stored-report-design", view=view).body
        for name in block_names:
            block_line = next(
                line for line in digest.splitlines() if line.startswith(f"- {name} ")
            )
            # build report labelled as the stored, build-time source; the
            # bound structure's CURRENT validator read sits beside it.
            assert "build report: 4 error(s), 1 warning(s)" in block_line
            assert "validator: 0 error(s), 0 warning(s)" in block_line


@pytest.mark.parametrize(
    ("record", "expected_count"),
    [
        (_record(0, 0), 0),
        (_record(4, 2), 4),
        ({"generator": "x"}, None),
        ({"report": "junk"}, None),
        ({"report": {"findings": "junk"}}, None),
    ],
)
def test_tree_build_report_count_distinguishes_clean_from_missing(
    record: dict[str, Any], expected_count: int | None
) -> None:
    digest = _render_tree(
        _tree(),
        "Stored report",
        "",
        bound_summaries={
            "d-foot": {
                "build": se_atomic_validate.generated_report_rows(record),
                "validator": None,
            }
        },
    )
    if expected_count is None:
        assert "build report:" not in digest
    else:
        assert f"build report: {expected_count} error(s), " in digest
    # An unresolvable bound structure reads "unavailable", never clean.
    assert "validator: unavailable" in digest


def test_tree_line_shows_bound_structure_validator_result() -> None:
    """gr486404 — the bound structure's validator tally rides the block
    line, labelled apart from the stored build report."""
    digest = _render_tree(
        _tree(),
        "Validator line",
        "",
        bound_summaries={
            "d-foot": {
                "build": se_atomic_validate.generated_report_rows(_record(0, 1)),
                "validator": {
                    "errors": 6,
                    "warns": 0,
                    "error_rules": {"over_valence": 4, "atom_overlap": 2},
                    "warn_rules": {},
                },
            }
        },
    )
    block_line = next(
        line for line in digest.splitlines() if line.startswith("- pillar_foot ")
    )
    assert "build report: 0 error(s), 1 warning(s)" in block_line
    assert (
        "validator: 6 error(s), 0 warning(s) (over_valence x4, atom_overlap x2)"
        in block_line
    )


def _overlap_scene() -> Scene:
    """Two H atoms 0.3 Å apart — inside the 0.6×(rH+rH) overlap floor."""
    cell = Cell.from_lengths_angles(10.0, 10.0, 10.0, pbc=(False, False, False))
    scene = Scene(cell=cell)
    scene.atoms["h1"] = Atom("h1", "H", np.zeros(3))
    scene.atoms["h2"] = Atom("h2", "H", np.array([0.03, 0.0, 0.0]))
    return scene


def test_bound_structure_validator_errors_reach_tree_and_validate(
    store: Store,
) -> None:
    """gr486404 — a bound structure that fails the structure validator must
    not read clean off its stored build report alone."""
    store.structure_save(
        slug="overlap-foot",
        title="Overlapping H pair",
        scene=_overlap_scene(),
        version=1,
        card_text="Validator-overlap regression fixture.",
        meta_extra={"generated": _record(0, 1)},
    )
    handler = SeHandler(hub=Hub(store=store))
    handler.put(
        id="validator-dirty-design",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "foot", "envelope": "sphere:r2e-10"},
                    {
                        "op": "bind_structure",
                        "block": "foot",
                        "design": "overlap-foot",
                        "ports": {},
                    },
                ]
            }
        ),
    )
    tree_body = handler.get(id="validator-dirty-design", view="tree").body
    block_line = next(
        line for line in tree_body.splitlines() if line.startswith("- foot ")
    )
    assert "build report: 0 error(s), 1 warning(s)" in block_line
    assert "validator: 1 error(s), 0 warning(s) (atom_overlap x1)" in block_line

    validation = handler.get(id="validator-dirty-design", view="validate").body
    error_line = next(
        line for line in validation.splitlines() if "structure_validator_error" in line
    )
    assert "atom_overlap x1" in error_line and "overlap-foot" in error_line
    digest = handler.get(id="validator-dirty-design", view="drc").body
    assert "validate: 1 error(s)" in digest


def test_bound_structure_clean_validator_reads_zero(store: Store) -> None:
    """A clean bound structure still shows an explicit 0, not silence."""
    scene = Scene(
        cell=Cell.from_lengths_angles(20, 20, 20, pbc=(False, False, False)),
        atoms={"aC1": Atom("aC1", "C", np.zeros(3))},
    )
    store.structure_save(
        slug="clean-foot",
        title="Clean foot",
        scene=scene,
        version=1,
        card_text="Clean validator regression fixture.",
    )
    handler = SeHandler(hub=Hub(store=store))
    handler.put(
        id="validator-clean-design",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "foot", "envelope": "sphere:r2e-10"},
                    {
                        "op": "bind_structure",
                        "block": "foot",
                        "design": "clean-foot",
                        "ports": {},
                    },
                ]
            }
        ),
    )
    tree_body = handler.get(id="validator-clean-design", view="tree").body
    block_line = next(
        line for line in tree_body.splitlines() if line.startswith("- foot ")
    )
    assert "validator: 0 error(s), 0 warning(s)" in block_line
    assert "build report:" not in block_line
    validation = handler.get(id="validator-clean-design", view="validate").body
    assert "structure_validator" not in validation


def test_malformed_rows_do_not_hide_valid_stored_findings() -> None:
    record = _hexfold_record()
    record["report"]["findings"].extend(
        [None, "junk", [], {}, {"severity": None}, {"severity": ["ERROR"]}]
    )
    findings = se_atomic_validate.validate_atomic(
        _tree(), generated_records={"d-foot": record}
    )
    errors = [f for f in findings if f.rule == "generated_report_error"]
    assert len(errors) == 1 and "geom.clash x4" in errors[0].detail
    warnings = [f for f in findings if f.rule == "generated_report_warn"]
    assert len(warnings) == 1 and "angle.dev x1" in warnings[0].detail
    digest = _render_tree(
        _tree(),
        "Stored report",
        "",
        bound_summaries={
            "d-foot": {
                "build": se_atomic_validate.generated_report_rows(record),
                "validator": {
                    "errors": 0,
                    "warns": 0,
                    "error_rules": {},
                    "warn_rules": {},
                },
            }
        },
    )
    assert "build report: 4 error(s), 1 warning(s)" in digest
    assert "validator: 0 error(s), 0 warning(s)" in digest
