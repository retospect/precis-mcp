"""Shared store singleton — lazy-loaded, used by handlers and tools."""

from __future__ import annotations

from precis.protocol import PrecisError

_store_singleton = None


def get_store():
    """Return the shared acatome-store instance (lazy-loaded).

    Raises:
        PrecisError: If acatome-store is not installed.
    """
    global _store_singleton
    if _store_singleton is not None:
        return _store_singleton
    try:
        from acatome_store.store import Store
        _store_singleton = Store()
        return _store_singleton
    except ImportError:
        raise PrecisError(
            "Store operations require acatome-store.\n"
            "Install with: pip install precis-mcp[paper]"
        )
