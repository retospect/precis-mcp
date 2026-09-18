"""Per-op validation.

Checks each op against the structure's current views before apply:

- ``site`` indices exist.
- Named ``site`` strings refer to entries in the special_sites
  catalog.
- ``equivalence_class`` exists in the symmetry view.
- ``element`` is a recognized chemical symbol.
- ``fraction`` is in [0, 1].
- For ``intercalate``: the interstitial exists OR (species, count,
  mode) is well-formed.

Returns a list of error strings (empty = valid).
"""

from __future__ import annotations

from typing import Any


def validate_ops(ops: list[dict[str, Any]], *, views: dict[str, Any]) -> list[str]:
    """Validate a list of ops against the structure's current views."""
    raise NotImplementedError("validate_ops — wiring not landed")
