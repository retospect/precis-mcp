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


__all__ = ["env_flag", "env_truthy"]
