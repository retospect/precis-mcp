"""gr513670 — the ``packaging/geom/README.md`` recipe must stay
executable: every fenced ``python`` block, in order, runs against the
repo root with ``out.run``'s ``scratch/out/`` default redirected to a
tmp dir. gr514456 — a ``text`` block directly after a ``python`` block
must equal the stdout that block printed (the README's "the output
shown is the output it produced" contract), with the redirected
scratch dir normalised back to ``scratch/``.
"""

from __future__ import annotations

import contextlib
import io
import re
from pathlib import Path
from typing import Any

import precis_geom.out as out

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "packaging" / "geom" / "README.md"

_BLOCKS = re.compile(r"```(python|text)\n(.*?)```", re.DOTALL)


def test_readme_python_blocks_execute(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(ROOT)
    scratch = tmp_path / "scratch"
    monkeypatch.setattr(out, "_SCRATCH_DIR", scratch)
    blocks = _BLOCKS.findall(README.read_text(encoding="utf-8"))
    assert sum(1 for kind, _ in blocks if kind == "python") >= 9, (
        "packaging/geom/README.md must keep its runnable ```python blocks"
    )
    ns: dict[str, Any] = {}
    for i, (kind, body) in enumerate(blocks):
        if kind != "python":
            continue
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(compile(body, str(README), "exec"), ns)
        nxt = blocks[i + 1] if i + 1 < len(blocks) else ("", "")
        if nxt[0] == "text":
            got = buf.getvalue().replace(str(scratch), "scratch").strip("\n")
            assert got == nxt[1].strip("\n"), (
                f"README's ```text block after python block {i} is stale:\n{got}"
            )
    # the hexfold section: the prism net is report-clean and o.block
    # wrote its geometry under the redirected scratch/out/.
    assert "net" in ns, "README lost the prism_tube block"
    assert ns["net"].report.ok
    assert (
        tmp_path / "scratch" / "out" / "hexprism" / "blocks" / "prism.extxyz"
    ).is_file()
