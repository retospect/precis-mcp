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


def env_flag(var: str, *, default: bool = False) -> bool:
    """Truthiness of env var ``var``, with a caller-chosen unset default.

    Unset (``None``) -> ``default``. Set -> :func:`env_truthy` of the
    value, regardless of ``default`` — so a default-ON gate's documented
    opt-out is any non-truthy token (``"0"``, ``"false"``, ...), not just
    the absence of the var.
    """
    raw = os.environ.get(var)
    if raw is None:
        return default
    return env_truthy(raw)


def env_csv_set(var: str) -> frozenset[str]:
    """``var``'s comma-separated value as a set of stripped, non-empty tokens.

    Unset/empty -> the empty set. Shared by ``cli/worker.py``'s per-axis
    ``PRECIS_AXES_ENABLED`` seed and the ``/categorizers`` console's
    effective-default read, so the two parses can't drift.
    """
    raw = os.environ.get(var) or ""
    return frozenset(tok.strip() for tok in raw.split(",") if tok.strip())


__all__ = ["env_csv_set", "env_flag", "env_truthy"]
