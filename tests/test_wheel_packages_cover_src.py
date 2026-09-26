"""Regression test for the 2026-09-26 prod outage — a green gate shipped a
wheel that could not start.

``src/precis_surface`` was missing from ``[tool.hatch.build.targets.wheel]
packages``. That was harmless for as long as nothing shipped imported it, and
became a total outage the moment it did: 27f5fc3f made
``precis_se/atomic/generators/__init__.py`` import ``tpms`` unconditionally,
``tpms`` imports ``precis_surface`` at module level, and the chain
``precis_web.app.create_app`` -> ``routes/blocktree_view`` -> ``precis_se``
-> ``handler`` -> ``kinematics_drc`` -> ``atomic/validate`` -> ``generators``
-> ``tpms`` made that a hard ``ModuleNotFoundError`` inside ``create_app``.
The web daemon crash-looped, nginx served 502 for every path, and the ``se``
kind vanished from every MCP on the fleet (the MCP tolerates a failed handler
import, so the kind silently disappeared with no error anywhere).

Why no test caught it: from a worktree ``src`` is on ``sys.path``, so every
package imports whether or not it is declared. The omission is only
observable in an *install*. That makes this a packaging invariant, not an
import test — so assert it against the declaration, where it is cheap and
deterministic, rather than by building and installing a wheel in the gate.

Strict equality is deliberate. If a package ever genuinely should not ship,
add it to ``INTENTIONALLY_UNPACKAGED`` with a reason, so the exclusion is a
decision on the record instead of an omission that looks identical to this
bug.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC = REPO_ROOT / "src"

# Packages under src/ that are deliberately not shipped in the wheel.
# Keep empty unless there is a stated reason; see the module docstring.
INTENTIONALLY_UNPACKAGED: frozenset[str] = frozenset()


def _declared_wheel_packages() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return {entry.removeprefix("src/") for entry in declared}


def _importable_src_packages() -> set[str]:
    return {
        child.name
        for child in SRC.iterdir()
        if child.is_dir() and (child / "__init__.py").is_file()
    }


def test_every_src_package_ships_in_the_wheel() -> None:
    """An importable package under src/ that is not in ``packages`` is absent
    from every install, so any shipped module importing it dies at import
    time — invisibly to a gate that runs from the source tree.
    """
    on_disk = _importable_src_packages() - INTENTIONALLY_UNPACKAGED
    missing = sorted(on_disk - _declared_wheel_packages())
    assert not missing, (
        "these packages exist under src/ but are not in "
        "[tool.hatch.build.targets.wheel] packages, so they will be missing "
        f"from every install: {missing}"
    )


def test_declared_wheel_packages_all_exist() -> None:
    """The other direction: a stale entry means a silent typo, and hatchling
    does not fail the build for a packages entry that matches nothing.
    """
    stale = sorted(_declared_wheel_packages() - _importable_src_packages())
    assert not stale, (
        "[tool.hatch.build.targets.wheel] packages names directories that are "
        f"not importable packages under src/: {stale}"
    )


def test_packages_declaration_is_a_single_flat_list() -> None:
    """Pin the shape the two tests above parse. If the declaration ever grows
    conditional logic or a second wheel target, these assertions stop being
    the whole truth and this test is the tripwire that says so.
    """
    text = PYPROJECT.read_text(encoding="utf-8")
    targets = re.findall(r"^\[tool\.hatch\.build\.targets\.(\w+)\]", text, re.M)
    assert targets.count("wheel") == 1, (
        f"expected exactly one [tool.hatch.build.targets.wheel] section, found {targets}"
    )
