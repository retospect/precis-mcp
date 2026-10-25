"""Every non-Python file under ``scripts/`` is ruff-excluded (gr514997).

ruff parses ANY path handed to it explicitly — ``ruff check
scripts/prod-precis`` reported 138 invalid-syntax "errors" on a bash
script. ``[tool.ruff] force-exclude`` plus an ``extend-exclude`` list
covering every non-Python file under ``scripts/`` makes the explicit call
a skip. This test keeps the list honest in both directions:

- a new bash script / data file that no pattern covers → uncovered failure;
- an exclude pattern matching nothing → stale-entry failure;
- a pattern that swallows a Python file (e.g. ``scripts/*``) → lint loss
  caught by the python-coverage assertion.

"Python" means ``*.py``/``*.pyi`` or an extensionless file whose shebang
names python (mirrors ``tests/test_scripts_use_uv.py``'s inventory rule).
"""

from __future__ import annotations

import glob
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def _is_python(path: Path) -> bool:
    if path.suffix in (".py", ".pyi"):
        return True
    if path.suffix:
        return False
    try:
        first = path.read_text(encoding="utf-8").partition("\n")[0]
    except UnicodeDecodeError:
        return False
    return first.startswith("#!") and "python" in first


def _ruff_config() -> tuple[list[str], bool]:
    cfg = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"][
        "ruff"
    ]
    return cfg["extend-exclude"], cfg.get("force-exclude", False)


def _covered_files() -> dict[str, set[Path]]:
    """Map each extend-exclude pattern to the repo files it covers."""
    patterns, _ = _ruff_config()
    covered: dict[str, set[Path]] = {}
    for pat in patterns:
        hits: set[Path] = set()
        for match in glob.glob(pat, root_dir=ROOT, recursive=True):
            m = Path(match)
            if (ROOT / m).is_dir():
                hits.update(
                    f.relative_to(ROOT) for f in (ROOT / m).rglob("*") if f.is_file()
                )
            else:
                hits.add(m)
        covered[pat] = hits
    return covered


def test_force_exclude_is_on() -> None:
    """Excludes only bind explicit paths when force-exclude is set; a
    refactor that drops it silently re-opens gr514997."""
    _, force = _ruff_config()
    assert force is True, "[tool.ruff] force-exclude must be true"


def test_nonpython_scripts_files_are_excluded() -> None:
    uncovered = [
        str(p)
        for p in sorted(SCRIPTS.rglob("*"))
        if p.is_file()
        and "__pycache__" not in p.parts  # bytecode from other tests' imports
        and not _is_python(p)
        and not any(p.relative_to(ROOT) in hits for hits in _covered_files().values())
    ]
    assert not uncovered, (
        "non-Python files under scripts/ that ruff would parse as Python "
        "when passed explicitly; add them to [tool.ruff] extend-exclude: "
        f"{uncovered}"
    )


def test_exclude_patterns_are_not_stale() -> None:
    stale = [pat for pat, hits in _covered_files().items() if not hits]
    assert not stale, f"extend-exclude entries matching nothing: {stale}"


def test_excludes_never_cover_python() -> None:
    swallowed = [
        f"{pat} -> {sorted(str(h) for h in hits)}"
        for pat, hits in _covered_files().items()
        if any(_is_python(ROOT / h) for h in hits)
    ]
    assert not swallowed, (
        f"extend-exclude patterns covering linted Python files: {swallowed}"
    )
