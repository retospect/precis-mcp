"""Every Python script under ``scripts/`` runs through uv, never bare ``python3``.

Bare ``python3`` on a Mac is the system 3.9; the project needs >=3.12
(``scripts/main-ci-status`` crashed on ``from datetime import UTC``). Two
shebangs are allowed:

- ``STDLIB``: stdlib-only scripts (all hooks) — no venv, fast, works in a
  worktree that has none. Unquoted ``>=3.12``: ``env -S`` passes quotes
  through to uv literally, which once broke every hook in a session.
- ``VENV``: scripts that import ``precis`` or third-party packages.
"""

from __future__ import annotations

import ast
import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"
STDLIB = "#!/usr/bin/env -S uv run --no-project --python >=3.12 python"
VENV = "#!/usr/bin/env -S uv run python"


def _python_scripts() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for p in sorted(SCRIPTS.rglob("*")):
        if not p.is_file():
            continue
        try:
            first = p.read_text(encoding="utf-8").partition("\n")[0]
        except UnicodeDecodeError:
            continue
        if first.startswith("#!") and "python" in first:
            out.append((p, first))
    return out


SCRIPT_SHEBANGS = _python_scripts()


@pytest.mark.parametrize(
    ("path", "shebang"),
    SCRIPT_SHEBANGS,
    ids=[str(p.relative_to(REPO)) for p, _ in SCRIPT_SHEBANGS],
)
def test_python_script_shebang_runs_through_uv(path: Path, shebang: str) -> None:
    assert shebang in (STDLIB, VENV), f"{path}: {shebang!r}"


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            roots.add(node.module.split(".")[0])
    return roots


def _non_stdlib_imports(path: Path, seen: set[Path]) -> set[str]:
    """Non-stdlib import roots, following sibling modules transitively."""
    if path in seen:
        return set()
    seen.add(path)
    bad: set[str] = set()
    for root in _imported_roots(path):
        if root in sys.stdlib_module_names or root == "__future__":
            continue
        sibling = path.parent / f"{root}.py"
        if sibling.is_file():
            bad |= _non_stdlib_imports(sibling, seen)
        else:
            bad.add(root)
    return bad


STDLIB_SCRIPTS = [p for p, s in SCRIPT_SHEBANGS if s == STDLIB]


@pytest.mark.parametrize(
    "path", STDLIB_SCRIPTS, ids=[str(p.relative_to(REPO)) for p in STDLIB_SCRIPTS]
)
def test_no_project_scripts_import_only_stdlib(path: Path) -> None:
    bad = _non_stdlib_imports(path, set())
    assert not bad, f"{path} imports {sorted(bad)}; use {VENV!r}"


def test_settings_hooks_call_scripts_directly() -> None:
    settings = json.loads(
        (REPO / ".claude" / "settings.json").read_text(encoding="utf-8")
    )
    commands: list[str] = []

    def walk(node: object) -> None:
        if isinstance(node, dict):
            cmd = node.get("command")
            if isinstance(cmd, str):
                commands.append(cmd)
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(settings)
    bare = [c for c in commands if re.search(r"\bpython3?\s+\S*scripts/", c)]
    assert not bare, bare
