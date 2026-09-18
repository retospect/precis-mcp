"""Shared pytest fixtures for precis-dft.

Keep this lean — heavy fixtures (real DB, real GPAW, real ML weights)
live behind ``needs_*`` markers declared in ``pyproject.toml``.
"""

from __future__ import annotations
