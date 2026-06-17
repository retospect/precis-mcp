"""``wake_runner`` re-queues paused coordinator jobs.

Most of the wake-runner is SQL talking to a real ``_migrations``-
schema database, so the bulk of the coverage lives behind the
``fresh_db`` fixture (skipped when Postgres isn't reachable).

The structural assertions below run anywhere — they pin the
SELECT queries to the documented wake-kind vocabulary and
guarantee the closed STATUS:waiting_* vocabulary stays in sync
with the coordinator executor's mapping.
"""

from __future__ import annotations

import inspect

from precis.workers import wake_runner
from precis.workers.executors import coordinator


def test_wake_runner_status_vocabulary_matches_coordinator() -> None:
    """Both modules pin the same STATUS:waiting_* values.

    Drift between them would mean the coordinator sets a
    STATUS the wake_runner doesn't look for — paused jobs would
    never resume.
    """
    assert wake_runner._WAITING_CHILDREN == coordinator._WAITING_CHILDREN
    assert wake_runner._WAITING_TIME == coordinator._WAITING_TIME
    assert wake_runner._WAITING_ASK_USER == coordinator._WAITING_ASK_USER
    assert wake_runner._WAITING_MANUAL_KICK == coordinator._WAITING_MANUAL_KICK


def test_every_wake_kind_has_a_selector() -> None:
    """Every WakeKind in the coordinator's status map must have a
    corresponding ``_wake_*`` selector in the wake_runner."""
    expected = {
        "children_done": wake_runner._wake_children_done,
        "at_time": wake_runner._wake_at_time,
        "tag_cleared": wake_runner._wake_tag_cleared,
        "tag_added": wake_runner._wake_tag_added,
    }
    for kind in coordinator._STATUS_FOR_WAKE_KIND:
        assert kind in expected, (
            f"WakeKind {kind!r} has no _wake_<kind> selector in wake_runner"
        )


def test_cancel_override_select_is_present() -> None:
    """The cancel-override selector exists separately from the
    four wake-kind selectors. ``STATUS:cancel_requested`` on a
    waiting job re-queues unconditionally so the coordinator's
    cancel-poll fires on its next slice."""
    assert callable(wake_runner._wake_cancel_override)


def test_tag_pattern_glob_handling_documented() -> None:
    """The ``_tag_present`` helper honours trailing ``*`` globs.

    Static smoke check on the source so the documented contract
    (exact match OR ``foo:*`` trailing-glob suffix match) stays
    in place. End-to-end coverage runs through ``fresh_db``.
    """
    source = inspect.getsource(wake_runner._tag_present)
    assert "endswith" in source and "*" in source, (
        "_tag_present must branch on trailing-glob '*' suffix"
    )
    assert "LIKE" in source, "_tag_present must use LIKE for glob match"


def test_pass_entry_point_signature() -> None:
    """``run_wake_pass`` matches the RefPass-adapter contract."""
    sig = inspect.signature(wake_runner.run_wake_pass)
    assert list(sig.parameters)[0] == "store"
    assert "limit" in sig.parameters


def test_runner_adapter_returns_batch_result() -> None:
    """``wake_pass_for_runner`` wraps ``run_wake_pass`` so the
    CLI worker can register it directly as a RefPass."""
    sig = inspect.signature(wake_runner.wake_pass_for_runner)
    assert list(sig.parameters) == ["store", "batch_size"]
