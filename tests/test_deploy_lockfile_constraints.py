"""Hygiene gate: every cluster install of ``precis-mcp`` from git constrains
against the local ``uv.lock``, so a fresh dep resolve can't silently drift
from what the ship gate tested.

Background (2026-07-30 incident, see git log / the deleted OPEN-ITEMS entry):
``deploy/`` installs ``precis-mcp[extras] @ git+https://github.com/
retospect/precis-mcp@main`` into every cluster venv via ``uv pip install`` —
a FRESH RESOLVE of the dependency graph. ``scripts/ship``'s gate and every
dev venv install from ``uv.lock`` (locked, reproducible); the cluster install
did not, so a dep whose newest in-range version diverged from the lock could
break prod while the gate stayed green (``mcp 2.0.0`` broke ``precis serve``
cluster-wide that day).

The fix: ``deploy/playbooks/03-precis-constraints.yml`` exports the local
``uv.lock`` to a ``--constraints`` file and lands it on every host BEFORE any
install runs; every install task then passes ``--constraints <path>``. This
test walks every tracked file under ``deploy/`` for a line that installs
``precis-mcp`` from a ``git+`` source and asserts the SAME line (or the
`uv pip install \\`-continued block it belongs to) also carries
``--constraints`` — so a future role added without it fails the gate instead
of silently reintroducing the fresh-resolve bug. Pure text walk, no ansible
needed — runs offline in the normal pytest gate.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEPLOY = _REPO_ROOT / "deploy"

_SKIP_DIRS = {"inventory", ".git", "__pycache__", "collections", ".venv"}
_SCAN_SUFFIXES = {".yml", ".yaml"}

# An actual `uv pip install`-style package spec — a quoted `precis-mcp[...] @
# git+...` — not a comment/prose mention of the same words (a YAML `#`
# comment line explaining the mechanism, which every touched file has).
_PACKAGE_SPEC = re.compile(r"""['"]precis-mcp(\[[^\]]*\])?\s*@\s*git\+""")


def _scannable_files() -> list[Path]:
    if not _DEPLOY.is_dir():
        return []
    out: list[Path] = []
    for path in _DEPLOY.rglob("*"):
        if not path.is_file() or path.suffix not in _SCAN_SUFFIXES:
            continue
        if any(part in _SKIP_DIRS for part in path.relative_to(_DEPLOY).parts):
            continue
        out.append(path)
    return out


def _install_block(lines: list[str], idx: int) -> str:
    """The shell/command block a `precis-mcp ... @ git+...` line belongs to —
    walk backward over the ``\\``-continued lines it's part of (a multi-line
    ``uv pip install`` invocation), so a flag on an earlier continuation line
    (e.g. ``--constraints``, one line above the package spec) still counts."""
    start = idx
    while start > 0 and lines[start - 1].rstrip().endswith("\\"):
        start -= 1
    end = idx
    while lines[end].rstrip().endswith("\\") and end + 1 < len(lines):
        end += 1
    return "\n".join(lines[start : end + 1])


def test_every_precis_mcp_git_install_has_a_constraints_flag() -> None:
    """A ``uv pip install ... precis-mcp[...] @ git+...`` line must also carry
    ``--constraints`` somewhere in its (possibly multi-line) invocation."""
    misses: list[str] = []
    checked = 0
    for path in _scannable_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        rel = path.relative_to(_REPO_ROOT)
        for i, line in enumerate(lines):
            if not _PACKAGE_SPEC.search(line):
                continue
            checked += 1
            block = _install_block(lines, i)
            if "--constraints" not in block:
                misses.append(f"{rel}:{i + 1}: {line.strip()[:120]}")

    assert checked > 0, (
        "no `precis-mcp ... @ git+...` install lines found under deploy/ — "
        "the scan itself is broken (would make this gate vacuously green)"
    )
    assert not misses, (
        "install line(s) missing --constraints (fresh-resolve drift risk, "
        "see 2026-07-30 incident):\n" + "\n".join(misses)
    )


def test_constraints_export_playbook_exists() -> None:
    """The playbook every install line's --constraints path depends on must
    actually exist and be wired into the redeploy flow."""
    export_playbook = _DEPLOY / "playbooks" / "03-precis-constraints.yml"
    assert export_playbook.is_file(), (
        f"{export_playbook.relative_to(_REPO_ROOT)} not found — every "
        "install task's --constraints /opt/precis/constraints.txt depends "
        "on this playbook generating + copying that file first"
    )
    redeploy = (_DEPLOY / "redeploy-precis.yml").read_text(encoding="utf-8")
    assert "03-precis-constraints.yml" in redeploy, (
        "deploy/redeploy-precis.yml does not import "
        "playbooks/03-precis-constraints.yml — the constraints file would "
        "never be generated/copied before the install plays run"
    )
