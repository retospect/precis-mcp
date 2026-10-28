"""Optional construction-trace hooks — one soft-import site for hexfold.

``precis_geom.trace`` rides in the same precis-geom wheel but is absent
under a standalone hexfold install (the monorepo fold-in boundary lets
hexfold soft-import the wheel sibling, never ``precis*`` — see
``tests/hexfold/test_import_boundary.py``). Every hook degrades to a
no-op without it: builders stay byte-identical, just untraced.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Iterator
from typing import Any

try:  # construction-trace frames; absent under a standalone hexfold install
    from precis_geom.trace import leaf_id, record, trace, traced
except ImportError:  # pragma: no cover

    def traced(
        fn: Callable[..., Any] | None = None,
        *,
        kind: str = "core",
        name: str | None = None,
    ) -> Any:
        return fn if fn is not None else (lambda f: f)

    def record(
        *, made: dict[str, int] | None = None, touched: dict[str, Any] | None = None
    ) -> None:
        return None

    def leaf_id() -> int | None:
        return None

    @contextlib.contextmanager
    def trace(
        name: str, *, kind: str = "user", args: dict[str, Any] | None = None
    ) -> Iterator[None]:
        yield


__all__ = ["leaf_id", "record", "trace", "traced"]
