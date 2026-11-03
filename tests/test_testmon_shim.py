"""gr558530: testmon fingerprints an extensionless file instead of crashing."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests import _testmon_shim

core = pytest.importorskip("testmon.testmon_core")


def test_extensionless_file_gets_whole_file_fingerprint(tmp_path: Path) -> None:
    _testmon_shim.install()
    (tmp_path / "tool").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "mod.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    tree = core.SourceTree(rootdir=str(tmp_path))

    tool = tree.get_file("tool")
    assert tool is not None
    assert tool.ext == ""
    assert len(tool.blocks) == 1
    assert tree.get_file("mod.py").ext == "py"


def test_install_is_idempotent() -> None:
    _testmon_shim.install()
    assert _testmon_shim.install() is False
    assert core.SourceTree.get_file._precis_ext_safe
