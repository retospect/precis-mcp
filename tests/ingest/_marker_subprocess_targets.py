"""Module-level target functions for
:mod:`tests.ingest.test_marker_subprocess_timeout`.

``multiprocessing.get_context("spawn")`` pickles a ``Process`` target
by reference (module + qualname) and re-imports it in the fresh child
interpreter — a closure, lambda, or bound method defined inside a test
function can't survive that boundary. These have to live in their own
importable module so the spawned child can import them by dotted path.
"""

from __future__ import annotations

import time


def fast_return(x: int, y: int) -> dict[str, int]:
    """Returns immediately — exercises the success/round-trip path."""
    return {"sum": x + y}


def sleep_forever() -> None:
    """Never returns — exercises the timeout/kill path."""
    time.sleep(3600)


def raise_value_error() -> None:
    """Raises — exercises the child-exception surfacing path."""
    raise ValueError("boom-marker-subprocess-test")
