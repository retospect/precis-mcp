"""Shared ``main.py`` loader for the workspaces tests (gr514208):
write a script under ``tmp_path`` and exec it as a module — the
sandbox's script shape, with the test file playing the driver.

The exec itself delegates to :func:`tests._script.load_script` — one
loader, compiled from source so a same-size rewrite can't serve a
stale pyc (gr516063).
"""

from __future__ import annotations

from pathlib import Path
from types import ModuleType

from tests._script import load_script as _load


def load_module(path: Path, name: str = "main") -> ModuleType:
    return _load(name, path)


def load_script(tmp_path: Path, text: str) -> ModuleType:
    """Write ``main.py`` under ``tmp_path`` and exec it as a module."""
    script = tmp_path / "main.py"
    script.write_text(text, encoding="utf-8")
    return load_module(script)
