"""precis-geom wheel stub for the ``precis_se`` package.

The real :mod:`precis_se` ``__init__`` imports ``precis_se.handler``
(``SeHandler``), which drags the whole DB-backed server stack into the
import graph. The workspace sandbox ships only the pure generators
(``precis_se.atomic.generators``) plus their ``precis_surface`` and
``precis_se.atomic.surface_target`` support modules, so this stub stands
in for the package init: it deliberately imports nothing.

This file is installed ONLY by the ``precis-geom`` wheel
(``packaging/geom/pyproject.toml`` force-include). It must never be
installed next to the server wheel ``precis-mcp``: both wheels own
``precis_se/__init__.py``, so whichever installs last wins — the stub
winning on a server would shadow ``SeHandler`` and break the ``se``
kind. ``tests/workspaces/test_geom_wheel_imports.py`` pins the shipped
set.
"""

from __future__ import annotations

__all__: list[str] = []
