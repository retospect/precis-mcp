"""Identify private learning calls before shared logging or result handling.

Learning records have no public refs or continuation handles. Classification
is deliberately broader than accepted syntax so malformed private calls receive
the same payload redaction as valid ones.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def is_learning_call(payload: Mapping[str, Any]) -> bool:
    """Recognize explicit, combined-kind and canonical-prefix spellings."""
    kind = payload.get("kind")
    if isinstance(kind, str) and "learning" in {
        part.strip().lower() for part in kind.split(",")
    }:
        return True
    ident = payload.get("id")
    ids = ident if isinstance(ident, (list, tuple)) else (ident,)
    return any(
        isinstance(value, str) and value.strip().lower().startswith("learning:")
        for value in ids
    )
