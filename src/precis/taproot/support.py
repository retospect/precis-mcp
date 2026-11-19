"""Mechanical backstop for support verdicts with an explicit setup mismatch.

Missing setup judgments remain unknown: this guard cannot infer scientific
identity from titles or lexical overlap. Historical worker verdicts store
their comparison inside ``widen``, ``reground`` or ``inbound_ground``.
"""

from typing import Any


def setup_mismatch(meta: dict[str, Any]) -> bool:
    """Whether any recorded setup comparison explicitly rejects identity."""
    comparisons = [
        meta,
        meta.get("widen"),
        meta.get("reground"),
        meta.get("inbound_ground"),
    ]
    return any(
        isinstance(comparison, dict) and comparison.get("same_setup") is False
        for comparison in comparisons
    )
