"""Schematic sheet provenance survives the pure EasyEDA component importer."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.pcb import epro

_FIXTURE = Path(__file__).parent / "fixtures" / "pcb_epro_tiny" / "board.epru"


def _sheet(
    uuid: str | None, refdes: list[str], title: str | None = None
) -> epro.EproDocument:
    records = []
    if title is not None:
        records.append(epro.EproRecord("META", "meta", {"title": title}))
    for index, designator in enumerate(refdes):
        records.extend(
            [
                epro.EproRecord("COMPONENT", f"c{index}", {"partId": "U.1"}),
                epro.EproRecord(
                    "ATTR",
                    f"designator-{index}",
                    {"parentId": f"c{index}", "key": "Designator", "value": designator},
                ),
            ]
        )
    return epro.EproDocument("SCH_PAGE", uuid, records)


def _extract(*sheets: epro.EproDocument) -> tuple[dict[str, dict[str, Any]], list[str]]:
    documents = epro.split_documents(_FIXTURE.read_text(encoding="utf-8"))
    project = epro.EproProject(
        "project title is not a sheet title", "3.2.149", documents + list(sheets)
    )
    board = project.pcb()
    _, frame = epro.board_outline(board)
    parts, _, warnings = epro.extract_components(project, board, frame, {})
    return {part["refdes"]: part for part in parts}, warnings


def test_import_retains_sheet_uuid_and_actual_title() -> None:
    parts, warnings = _extract(_sheet("sheet-power-uuid", ["U1", "R1"], "Power"))
    for refdes in ("U1", "R1"):
        assert parts[refdes]["part_meta"]["source_sheet"] == {
            "id": "sheet-power-uuid",
            "name": "Power",
        }
    assert not any("multiple schematic sheets" in warning for warning in warnings)


@pytest.mark.parametrize("title", [None, "", "   "])
def test_untitled_sheet_uses_its_uuid(title: str | None) -> None:
    parts, _ = _extract(_sheet("untitled-uuid", ["U1"], title))
    assert parts["U1"]["part_meta"]["source_sheet"] == {
        "id": "untitled-uuid",
        "name": "untitled-uuid",
    }
    assert "source_sheet" not in parts["R1"].get("part_meta", {})


def test_board_only_import_has_no_invented_hierarchy() -> None:
    parts, _ = _extract()
    assert parts
    assert all(
        "source_sheet" not in part.get("part_meta", {}) for part in parts.values()
    )


def test_ambiguous_designator_does_not_choose_a_sheet() -> None:
    parts, warnings = _extract(
        _sheet("power-uuid", ["U1", "R1"], "Power"),
        _sheet("logic-uuid", ["U1"], "Logic"),
    )
    assert "source_sheet" not in parts["U1"].get("part_meta", {})
    assert parts["R1"]["part_meta"]["source_sheet"]["id"] == "power-uuid"
    assert any(
        "U1" in warning and "multiple schematic sheets" in warning
        for warning in warnings
    )


def test_equal_sheet_titles_keep_distinct_identity() -> None:
    parts, _ = _extract(
        _sheet("first-uuid", ["U1"], "Circuit"),
        _sheet("second-uuid", ["R1"], "Circuit"),
    )
    assert parts["U1"]["part_meta"]["source_sheet"] == {
        "id": "first-uuid",
        "name": "Circuit",
    }
    assert parts["R1"]["part_meta"]["source_sheet"] == {
        "id": "second-uuid",
        "name": "Circuit",
    }


def test_repeated_symbol_units_in_one_sheet_are_not_ambiguous() -> None:
    parts, warnings = _extract(_sheet("logic-uuid", ["U1", "U1"], "Logic"))
    assert parts["U1"]["part_meta"]["source_sheet"]["id"] == "logic-uuid"
    assert not any("multiple schematic sheets" in warning for warning in warnings)


def test_sheet_without_uuid_does_not_invent_identity() -> None:
    parts, _ = _extract(_sheet(None, ["U1"], "Logic"))
    assert "source_sheet" not in parts["U1"].get("part_meta", {})


def test_sheet_metadata_preserves_existing_component_data() -> None:
    baseline, _ = _extract()
    parts, _ = _extract(_sheet("logic-uuid", ["U1"], "Logic"))
    actual = parts["U1"]
    actual["part_meta"].pop("source_sheet")
    if not actual["part_meta"] and "part_meta" not in baseline["U1"]:
        actual.pop("part_meta")
    assert parts == baseline
