"""pcb-workspace-code slice 4, tier-2 CI goldens
(docs/backlog/pcb-workspace-code.md, Slices row 4 + §Goldens):

- ``sanity`` is HARD: ``pcb.local.place`` + ``pcb.local.drc`` must reach
  0 DRC errors on every run.
- ``nano_usb_pd`` and ``esp32c3`` are MEASURED: the placement metrics
  (crossings, pads-only DRC error count) are compared against
  ``data/pcb_tier2_goldens.json`` and any drift is printed naming the
  board — the same "golden drift — regenerate" escape as tier-1, never
  a silent floor. An ``optimize`` change that moves a board's anneal
  trips this test with the delta in the message.

**Routing is retired** (gr515107, Reto 2026-10-25: the vector router was
deleted, tag ``vroute-final``; ``pcb.local.route`` now refuses). The
routed/failed/vias golden rows died with it — what remains measures the
anneal and the pads-only DRC readback over the placed board.

Regenerate the measured rows after an intended engine change (then read
the diff)::

    uv run python -c "import sys, json; sys.path.insert(0, 'tests/workspaces'); \\
      import test_pcb_tier2 as t; t.GOLDEN.write_text(json.dumps({n: \\
      t._measure(n) for n in t.MEASURED}, indent=1, sort_keys=True) + '\\n', \\
      encoding='utf-8')"
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import precis_geom.pcb as pcb

ROOT = Path(__file__).resolve().parents[2]
BOARDS = ROOT / "examples" / "boards"
GOLDEN = Path(__file__).resolve().parent / "data" / "pcb_tier2_goldens.json"

#: Boards whose numbers are measured, not floored.
MEASURED = ("nano_usb_pd", "esp32c3")


def _load(path: Path, name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _example(name: str) -> pcb.Board:
    return _load(BOARDS / name / "main.py", f"example_{name}").build()


def _place(name: str) -> Any:
    board = _example(name)
    return pcb.local.place(board)


def _measure(name: str) -> dict[str, Any]:
    placement = _place(name)
    findings = pcb.local.drc(placement)
    return {
        "crossings": placement.crossings,
        "errors": sum(1 for f in findings if f.severity == "error"),
    }


# --- hard gate ---------------------------------------------------------------


def test_sanity_places_with_zero_drc() -> None:
    placement = _place("sanity")
    findings = pcb.local.drc(placement)
    errors = [f for f in findings if f.severity == "error"]
    assert not errors, pcb.local.findings_text(findings)


# --- measured ----------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(1200)  # a full place anneal per board; CI runners run long
@pytest.mark.parametrize("name", MEASURED)
def test_tier2_measured_board(name: str) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    row = _measure(name)
    want = golden[name]
    deltas = {k: f"{want[k]}→{row[k]}" for k in sorted(want) if want[k] != row[k]}
    if deltas:
        print(
            f"{name}: tier-2 drift — "
            + ", ".join(f"{k} {d}" for k, d in deltas.items())
        )
    assert row == want, (
        f"{name}: tier-2 measured drift — "
        + (", ".join(f"{k} {d}" for k, d in deltas.items()) if deltas else "none")
        + ". If intended, regenerate "
        "tests/workspaces/data/pcb_tier2_goldens.json with the one-liner "
        "in this module's docstring; "
        f"got {json.dumps(row)}"
    )


def test_tier2_golden_covers_every_measured_board() -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert sorted(golden) == sorted(MEASURED)
