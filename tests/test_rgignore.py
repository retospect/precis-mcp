"""``.rgignore`` keeps vendored/minified static assets out of ``rg``.

gr514479: ``rg <pattern> src`` flooded with 60k-char lines from
``src/precis_web/static/`` vendor bundles (katex, mermaid, three, pdfjs,
3dmol, ``*.min.js``). The ignore file must cover every such asset while
leaving authored JS/CSS greppable.
"""

from __future__ import annotations

import fnmatch
import shutil
import subprocess
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
STATIC = PurePosixPath("src/precis_web/static")

#: Vendored directories under ``STATIC`` — everything in them is third
#: party and none of it should ever reach an ``rg`` hit list.
VENDORED_DIRS = (
    "3dmol",
    "katex",
    "mermaid",
    "pdfjs",
    "three",
    "three-cad-viewer",
    "three-r184",
)


def _rules() -> list[str]:
    path = ROOT / ".rgignore"
    assert path.is_file(), ".rgignore missing at repo root"
    return [
        ln.strip()
        for ln in path.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.startswith("#")
    ]


def _ignored(rel: PurePosixPath, rules: list[str]) -> bool:
    """The slice of the gitignore grammar our own rules use: a ``dir/``
    prefix rule and ``*.ext`` globs over the path and its basename."""
    s = str(rel)
    for rule in rules:
        if rule.endswith("/"):
            if s.startswith(rule):
                return True
        elif fnmatch.fnmatch(s, rule) or fnmatch.fnmatch(rel.name, rule):
            return True
    return False


def _is_vendored(rel: PurePosixPath) -> bool:
    return (
        rel.name.endswith((".min.js", ".min.css", ".bundle.mjs"))
        or rel.relative_to(STATIC).parts[0] in VENDORED_DIRS
    )


def _static_files() -> list[PurePosixPath]:
    return sorted(
        PurePosixPath(p.relative_to(ROOT).as_posix())
        for p in (ROOT / STATIC).rglob("*")
        if p.is_file()
    )


def test_every_vendored_static_asset_is_ignored() -> None:
    rules = _rules()
    vendored = [p for p in _static_files() if _is_vendored(p)]
    assert vendored, "expected vendored assets under src/precis_web/static"
    missed = [str(p) for p in vendored if not _ignored(p, rules)]
    assert not missed, f".rgignore misses vendored assets: {missed}"


def test_authored_static_assets_stay_greppable() -> None:
    rules = _rules()
    authored = [p for p in _static_files() if not _is_vendored(p)]
    assert any(p.name == "shortcuts.js" for p in authored)
    overbroad = [str(p) for p in authored if _ignored(p, rules)]
    assert not overbroad, f".rgignore hides authored files: {overbroad}"


def test_rg_files_lists_only_authored() -> None:
    rg = shutil.which("rg")
    if rg is None:
        return  # rule-eval tests above still cover the contract
    out = subprocess.run(
        [rg, "--files", str(STATIC)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.splitlines()
    listed = [PurePosixPath(ln) for ln in out]
    assert listed, "rg --files returned nothing under static/"
    leaks = [str(p) for p in listed if _is_vendored(p)]
    assert not leaks, f"rg still lists vendored assets: {leaks}"
    assert PurePosixPath("src/precis_web/static/shortcuts.js") in listed
