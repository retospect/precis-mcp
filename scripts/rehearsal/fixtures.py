"""Test strings that must not appear whole in committed text.

The repository's secret scan (``precis.utils.secret_scan``, run by
``scripts/ship`` and by the hub's pre-receive hook) flags credential-shaped
strings. A scenario that needs one (the refused-push scenario) assembles it
here at run time from fragments none of which is credential-shaped, so this
file passes the same scan it exercises.
"""

from __future__ import annotations


def credential_shaped() -> str:
    """A GitHub-token-shaped string (``gh`` + ``p_`` + 36 characters)."""
    parts = ("gh", "p_", "Q1w2E3r4T5y6", "U7i8O9p0A1s2", "D3f4G5h6J7k8")
    return "".join(parts)
