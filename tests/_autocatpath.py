"""Named skip gate for engine-gated tests (gr514397).

A bare ``pytest.importorskip("autocatpath")`` reports only
``SKIPPED autocatpath`` — silent about *why*, so a builder on a venv
without the optional extra reads the whole pathway cluster as inert
rather than missing an install. Every engine gate goes through
:func:`needs_engine`, which names the ``catalyst`` extra in the -rs
reason.
"""

from __future__ import annotations

from types import ModuleType

import pytest

#: The install hint every skip reason carries.
EXTRA_HINT = "install the 'catalyst' extra (uv sync --extra catalyst)"


def needs_engine(module: str = "autocatpath", *, note: str = "") -> ModuleType:
    """``pytest.importorskip(module)`` with the missing-extra reason attached.

    ``note`` carries a version gate for feature-detection skips (e.g.
    ``note="engine >= 0.38"`` on ``autocatpath.manifest``) — an installed-
    but-old engine misses the module for a different reason than a venv
    that lacks the extra entirely.
    """
    reason = f"needs {module}"
    if note:
        reason = f"{reason} ({note})"
    return pytest.importorskip(module, reason=f"{reason} — {EXTRA_HINT}")
