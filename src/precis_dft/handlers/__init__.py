"""Handlers for the precis-dft kinds.

Each module defines one ``Handler`` subclass with its ``KindSpec``.
Entry points in ``pyproject.toml`` under ``[project.entry-points."precis.handlers"]``
point at them; precis-mcp discovers and registers them at boot via the
existing ``precis.handlers`` plugin mechanism.
"""
