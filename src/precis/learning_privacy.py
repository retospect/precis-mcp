"""Identify private learning calls before shared logging or result handling.

Learning records have no public refs or continuation handles. Classification
is deliberately broader than accepted syntax so malformed private calls receive
the same payload redaction as valid ones. The private-kind registry
(:mod:`precis.private_kinds`) owns the classification; this module stays the
learning-specific import path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from precis.private_kinds import private_kind_of


def is_learning_call(payload: Mapping[str, Any]) -> bool:
    """Recognize explicit, combined-kind and canonical-prefix spellings."""
    return private_kind_of(payload) == "learning"
