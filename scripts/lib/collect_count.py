"""scripts/lib/collect_count.py — count tests a pytest selection would run.

`scripts/test`'s auto-parallelism rule (test-loop-speed Decision 1) needs the
collected item count BEFORE picking -n0/-n6: below the threshold xdist worker
bring-up costs more than the tests save. This runs pytest collection
in-process — the same baked venv and conftest the real run uses — and prints
exactly one integer on stdout: the post-deselection item count. Collection
failures print nothing and exit 1; the caller falls back to -n6 (today's
default) and the real pytest run reports the error itself.

Run inside `uv run` in the dev container (scripts/lib/pytest-inner.sh does);
it is not a host tool — the host venv is torch-free.
"""

from __future__ import annotations

import contextlib
import io
import sys

import pytest


class _Count:
    """Captures the final item list after collection + deselection."""

    def __init__(self) -> None:
        self.n = -1

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        self.n = len(session.items)


def main() -> int:
    counter = _Count()
    # Collection's own chatter ("N tests collected", warnings summary)
    # stays off stdout so the only line emitted is the bare count.
    with contextlib.redirect_stdout(io.StringIO()):
        rc = pytest.main(["--collect-only", "-q", *sys.argv[1:]], plugins=[counter])
    if rc == 0 and counter.n >= 0:
        print(counter.n)
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
