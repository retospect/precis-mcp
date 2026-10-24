"""Shared ``main.py`` loader for the workspaces tests (gr514208):
write a script under ``tmp_path`` and exec it as a module — the
sandbox's script shape, with the test file playing the driver."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def load_module(path: Path, name: str = "main") -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def load_script(tmp_path: Path, text: str) -> ModuleType:
    """Write ``main.py`` under ``tmp_path`` and exec it as a module."""
    script = tmp_path / "main.py"
    script.write_text(text, encoding="utf-8")
    return load_module(script)
