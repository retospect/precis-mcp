"""scripts/lib/collect_count.py — the auto-parallelism counter.

`scripts/test` runs it in-container before pytest when a file/node-id
selection needs its collected count to pick -n0 vs -n6 (test-loop-speed
Decision 1). These run the real script as a subprocess in the same venv —
no DB, plain collectable test files under tmp_path.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "lib" / "collect_count.py"


def _count(*args: str, cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=120,
        check=False,
    )
    return proc.returncode, proc.stdout


def test_counts_all_items_in_a_file(tmp_path: Path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
        encoding="utf-8",
    )
    rc, out = _count(str(f), cwd=tmp_path)
    assert rc == 0
    assert out.strip() == "2"


def test_count_reflects_node_id_and_k_filter(tmp_path: Path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text(
        "def test_a():\n    pass\n\ndef test_b():\n    pass\n",
        encoding="utf-8",
    )
    rc, out = _count(f"{f}::test_a", cwd=tmp_path)
    assert rc == 0
    assert out.strip() == "1"
    rc, out = _count("-k", "b", str(f), cwd=tmp_path)
    assert rc == 0
    assert out.strip() == "1"


def test_parametrized_items_count_individually(tmp_path: Path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text(
        "import pytest\n\n"
        "@pytest.mark.parametrize('n', range(600))\n"
        "def test_big(n):\n    pass\n",
        encoding="utf-8",
    )
    rc, out = _count(str(f), cwd=tmp_path)
    assert rc == 0
    assert out.strip() == "600"


def test_collection_error_prints_nothing_and_fails(tmp_path: Path) -> None:
    f = tmp_path / "test_x.py"
    f.write_text("import does_not_exist_xyz\n", encoding="utf-8")
    rc, out = _count(str(f), cwd=tmp_path)
    assert rc == 1
    assert out.strip() == ""
