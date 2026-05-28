"""Permanent API-decision regression guards for the storage-v2
rewrite.

Phases 1-4 of the storage-v2 rewrite are all real now: refs +
identifiers (Phase 1), blocks→chunks (Phase 2), unified tags +
block search (Phase 3), links + cache (Phase 4). The Phase 3/4
boundary canaries that lived here during the rollout have been
deleted — they were meant as "remove me when the real CRUD lands"
signals.

What remains are the **permanent API-decision guards** — tests
that pin a design call that future refactors could accidentally
unwind. They survive forever.
"""

from __future__ import annotations

from precis.store import Store


class TestApiDecisions:
    """API-decision regression guards — keep these in place forever."""

    def test_has_flag_no_longer_exists(self) -> None:
        """v1 ``has_flag`` was removed outright. Phase 3 unified API
        is ``has_tag(ref_id, namespace, value)``. A future
        ``store.has_flag(...)`` must fail fast at attribute
        resolution rather than silently doing the wrong thing. If
        anyone tries to "helpfully" re-add the method, this test
        catches it on the next CI run.
        """
        stub = Store.__new__(Store)
        assert not hasattr(stub, "has_flag")
