"""Load an extensionless executable script (``scripts/board``,
``scripts/fleet-wave``, a tmp copy of one) as a module.

``importlib.util.spec_from_file_location`` can't infer a loader for a
file with no ``.py`` suffix — the spec's ``loader`` comes back ``None``
(gr515483) — so tests that exec a bare script used to hand-roll
``SourceFileLoader`` + ``spec_from_loader`` + the ``spec is not None``
asserts mypy needs. Those copies live here now.

The body is compiled from source, never via the loader's
``__pycache__`` (gr516063): tests rewrite the same script repeatedly,
and a same-size rewrite inside the same timestamp tick matches the
stale pyc's (mtime-s, size) stamp, so the old code runs (CI writes
bytecode; the dev container doesn't — a8d90e685 hit exactly this).
``tests/workspaces/_script.py`` delegates here; there is one loader.
"""

from __future__ import annotations

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


def load_script(name: str, path: str | Path, *, register: bool = False) -> ModuleType:
    """Exec the extensionless python script at ``path`` under module ``name``.

    ``register=True`` puts the module in ``sys.modules`` first —
    ``@dataclass`` (and ``slots=True`` dataclasses) resolve
    ``cls.__module__`` through it, so a script defining dataclasses
    fails to exec otherwise.
    """
    path = Path(path)
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    if register:
        sys.modules[name] = mod
    source = path.read_text(encoding="utf-8")
    exec(compile(source, str(path), "exec"), mod.__dict__)
    return mod
