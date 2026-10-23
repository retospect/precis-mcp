"""``precis.workers.child_compute`` — a job compute in a killable child
(gr506395). Every stop path must leave no live process behind: the child
pid and any process it started are confirmed gone before the call
returns."""

from __future__ import annotations

import os
import time
from typing import Any

import pytest

from precis.workers.child_compute import (
    ChildDied,
    ChildStopped,
    run_in_child,
)
from tests import _child_compute_targets as targets


def _gone(pid: int, timeout_s: float = 5.0) -> bool:
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


def test_result_round_trips_and_handle_is_reported() -> None:
    started: list[dict[str, Any]] = []
    ended: list[dict[str, Any]] = []
    assert (
        run_in_child(
            targets.add,
            (2, 3),
            label="add",
            timeout_s=60,
            on_start=started.append,
            on_end=ended.append,
            poll_s=0.1,
        )
        == 5
    )
    assert started[0]["pid"] == started[0]["pgid"] > 0
    assert ended[0]["exitcode"] == 0 and ended[0]["kill_verified"] is True
    assert ended[0]["stopped"] is None
    assert _gone(started[0]["pid"])


def test_child_exception_is_reraised_with_its_type() -> None:
    with pytest.raises(ValueError, match="boom-child-compute"):
        run_in_child(targets.raise_value_error, label="raise", timeout_s=60, poll_s=0.1)


def test_child_exit_without_result_raises_child_died() -> None:
    with pytest.raises(ChildDied) as info:
        run_in_child(targets.exit_hard, label="exit", timeout_s=60, poll_s=0.1)
    assert info.value.handle["exitcode"] == 3


def test_timeout_kills_the_child() -> None:
    with pytest.raises(ChildStopped) as info:
        run_in_child(
            targets.sleep_forever, label="sleep", timeout_s=1.0, poll_s=0.1, grace_s=5
        )
    stop = info.value
    assert stop.reason == "timeout"
    assert stop.handle["kill_verified"] is True
    assert _gone(stop.handle["pid"])


def test_sigterm_ignoring_child_is_sigkilled() -> None:
    ready: list[int] = []
    with pytest.raises(ChildStopped) as info:
        run_in_child(
            targets.ignore_sigterm_and_sleep,
            label="stubborn",
            timeout_s=60,
            should_stop=lambda: "kill" if ready else None,
            on_event=lambda kind, pid: ready.append(pid),
            poll_s=0.1,
            grace_s=1.0,
        )
    stop = info.value
    assert stop.reason == "kill"
    assert stop.handle["kill_verified"] is True
    assert stop.handle["exitcode"] == -9
    assert _gone(ready[0])


def test_stop_kills_the_whole_process_group() -> None:
    grandchild: list[int] = []
    with pytest.raises(ChildStopped) as info:
        run_in_child(
            targets.spawn_grandchild_and_sleep,
            label="group",
            timeout_s=60,
            should_stop=lambda: "cancel" if grandchild else None,
            on_event=lambda kind, pid: grandchild.append(pid),
            poll_s=0.1,
            grace_s=5,
        )
    assert info.value.reason == "cancel"
    assert info.value.handle["kill_verified"] is True
    assert _gone(info.value.handle["pid"])
    assert _gone(grandchild[0])
