"""Small environment-variable coercion helpers.

Kept tiny and dependency-free so any layer (workers, handlers, CLI)
can import it without pulling in config or store.
"""

from __future__ import annotations

import os

#: Tokens that count as "on" for a boolean env var.
_TRUTHY = {"1", "true", "yes", "on"}


def env_truthy(raw: str | None) -> bool:
    """True when ``raw`` is one of ``1``/``true``/``yes``/``on`` (case-insensitive)."""
    return str(raw or "").strip().lower() in _TRUTHY


def env_flag(var: str) -> bool:
    """True when env var ``var`` is set to a truthy token."""
    return env_truthy(os.environ.get(var))


def env_csv_set(var: str) -> frozenset[str]:
    """``var``'s comma-separated value as a set of stripped, non-empty tokens.

    Unset/empty -> the empty set. Shared by ``cli/worker.py``'s per-axis
    ``PRECIS_AXES_ENABLED`` seed and the ``/categorizers`` console's
    effective-default read, so the two parses can't drift.
    """
    raw = os.environ.get(var) or ""
    return frozenset(tok.strip() for tok in raw.split(",") if tok.strip())


__all__ = ["env_csv_set", "env_flag", "env_truthy"]
