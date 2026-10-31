"""The generated nano artifact and its compatibility entry stay reproducible."""

from __future__ import annotations

import ast
from pathlib import Path

from precis.pcb.board_script import classified_batch, emit
from tests.workspaces._script import load_module

ROOT = Path(__file__).resolve().parents[1]


def test_generated_nano_matches_current_writer_and_source() -> None:
    source = ROOT / "tests/fixtures/pcb/nano_usb_pd_testboard.py"
    directory = ROOT / "examples/boards/nano"
    fixture = load_module(source, "nano_fixture").build_design()
    text = emit(
        fixture,
        "nano-usb-pd-test",
        "tests/fixtures/pcb/nano_usb_pd_testboard.py:build_design",
        catalog_cache=directory / "footprints.json",
        catalog_relative_path="footprints.json",
    )
    # The committed artifact is Ruff-formatted after emission.
    assert ast.dump(ast.parse(text)) == ast.dump(
        ast.parse((directory / "main.py").read_text(encoding="utf-8"))
    )
    board = load_module(directory / "main.py", "nano_example").build()
    assert board.to_batch() == classified_batch(fixture)
    assert not board.check().findings
    assert sorted(board.check().not_checked) == sorted(
        fixture["meta"]["synthesized_footprint_refdes"]
    )


def test_old_nano_entry_is_compatible_with_regenerated_example() -> None:
    current = load_module(ROOT / "examples/boards/nano/main.py", "nano_current").build()
    legacy = load_module(
        ROOT / "examples/boards/nano_usb_pd/main.py", "nano_legacy"
    ).build()
    assert legacy.to_batch() == current.to_batch()
    assert legacy.check().summary() == current.check().summary()
