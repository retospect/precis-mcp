"""gr514740 / gr514869 / gr514876: an installed first-party package without
``py.typed`` is untyped to mypy, so per-file host typecheck fails on
unchanged imports.

``uv run`` installs precis-mcp as a wheel into ``.venv``; a host
``uv run --frozen mypy tests/test_x.py`` then resolves
``from precis_ws import out`` against site-packages, where PEP 561 says a
package with no ``py.typed`` marker is untyped — every ``import
precis_ws`` / ``precis_se`` / ``precis_pathway`` in a touched test file
reported ``import-untyped`` on lines the change never touched. The
container gate (``scripts/test --typecheck`` → ``mypy src tests``) checks
the source tree, where the marker is irrelevant, so it stayed green.

Strict equality both ways is the ratchet: a wheel package missing the
marker must be listed in ``PY_TYPED_PENDING``, and shipping the marker
must shrink the list — new packages ship ``py.typed`` from day one. The
remaining entries are untested-typing packages whose markers land as
their own batches; marking e.g. ``precis_web`` today would surface
import-not-found on its ``[web]``-extra deps inside a host venv, which is
a separate fix.

gr514897 is the same host/container parity bug on the third-party side:
``jinja2`` lives behind the ``[web]`` extra, which a bare host
``uv sync`` does not install, so ``import jinja2`` in tests/ reports
``import-not-found`` on the host only. The ``ignore_missing_imports``
overrides list in ``pyproject.toml`` already exists for this
optional-dependency surface; the assertion keeps it covering jinja2.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
SRC = REPO_ROOT / "src"

# Declared wheel packages that do not yet ship py.typed. Shrink-only.
PY_TYPED_PENDING: frozenset[str] = frozenset(
    {
        "asa_bot",
        "asa_slack",
        "precis_bio",
        "precis_chem",
        "precis_estimate",
        "precis_surface",
        "precis_web",
    }
)


def _declared_wheel_packages() -> set[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    declared = data["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    return {entry.removeprefix("src/") for entry in declared}


def test_every_wheel_package_ships_py_typed_or_is_pending() -> None:
    missing = {
        name
        for name in _declared_wheel_packages()
        if not (SRC / name / "py.typed").is_file()
    }
    assert missing == PY_TYPED_PENDING, (
        "wheel packages without py.typed drifted from the pending list — "
        "ship the marker and drop the entry, or record why the package "
        "stays untyped: "
        f"missing-not-pending={sorted(missing - PY_TYPED_PENDING)}, "
        f"pending-but-typed={sorted(PY_TYPED_PENDING - missing)}"
    )


def test_jinja2_has_ignore_missing_imports() -> None:
    """gr514897: jinja2 is a ``[web]``-extra dep imported by
    ``tests/test_deploy_monitoring_alerting.py``; a host ``uv sync``
    without extras makes mypy report ``import-not-found`` where the
    container is green. It must stay on the ``ignore_missing_imports``
    overrides list — typed upstream, so installs still get real types.
    """
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    modules = {
        module
        for override in data["tool"]["mypy"]["overrides"]
        if override.get("ignore_missing_imports")
        for module in override["module"]
    }
    assert {"jinja2", "jinja2.*"} <= modules, (
        "jinja2 dropped off the mypy ignore_missing_imports overrides — "
        "host `uv run --frozen mypy` on tests importing it false-fails "
        "where the container gate is green"
    )
