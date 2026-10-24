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
    assert blocks, "packaging/geom/README.md lost its runnable ```python blocks"
    ns: dict[str, Any] = {}
    # Per-block, not a count floor: a bare ``>= N`` drifts with every
    # README addition and has to be recounted by hand (gr514182). A
    # failure names the block, and the end-state asserts below pin the
    # blocks the recipe can't lose.
    for i, block in enumerate(blocks):
        head = block.strip().splitlines()[0]
        try:
            exec(compile(block, f"{README}#block-{i}", "exec"), ns)
        except Exception as exc:
            raise AssertionError(
                f"README ```python block {i} failed ({head!r})"
            ) from exc
    # the hexfold section: the prism net is report-clean and o.block
    # wrote its geometry under the redirected scratch/out/.
    assert "net" in ns, "README lost the prism_tube block"
    assert ns["net"].report.ok
    assert (
        tmp_path / "scratch" / "out" / "hexprism" / "blocks" / "prism.extxyz"
    ).is_file()
