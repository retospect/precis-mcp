"""gr513670 — the ``packaging/geom/README.md`` recipe must stay
executable: every fenced ``python`` block, in order, runs against the
repo root with ``out.run``'s ``scratch/out/`` default redirected to a
tmp dir. Execution-only — the README shows the real output for humans;
this guards the commands themselves.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import precis_geom.out as out

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "packaging" / "geom" / "README.md"

_BLOCKS = re.compile(r"```python\n(.*?)```", re.DOTALL)


def test_readme_python_blocks_execute(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    monkeypatch.setattr(out, "_SCRATCH_DIR", tmp_path / "scratch")
    blocks = _BLOCKS.findall(README.read_text(encoding="utf-8"))
    assert len(blocks) >= 9, (
        "packaging/geom/README.md must keep its runnable ```python blocks"
    )
    ns: dict[str, Any] = {}
    for block in blocks:
        exec(compile(block, str(README), "exec"), ns)
    # the hexfold section: the prism net is report-clean and o.block
    # wrote its geometry under the redirected scratch/out/.
    assert ns["net"].report.ok
    assert (
        tmp_path / "scratch" / "out" / "hexprism" / "blocks" / "prism.extxyz"
    ).is_file()
