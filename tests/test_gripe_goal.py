"""gr592749: the ``goal:<value>`` axis — glob mapping, no-match → other,
thread rules outrank cluster catch-alls."""

from unittest.mock import MagicMock

import pytest

from precis.cli.gripe import origin_backfill
from precis.utils.gripe_goal import GOALS, goal_of


@pytest.mark.parametrize(
    ("tags", "expected"),
    [
        (["thread:catalysis-selectivity"], "reaction-graph"),
        (["cluster:pathway-optim"], "pathway"),
        (["cluster:pcb-drc"], "pcb"),
        (["cluster:memory-frag"], "memory-graph"),
        (["cluster:garden"], "memory-graph"),
        (["cluster:dark-factory"], "work-model"),
        (["cluster:release"], "work-model"),
        (["cluster:ship"], "work-model"),
        (["cluster:work-model"], "work-model"),
        (["cluster:result-size-cap"], "search-surface"),
        (["cluster:agent-surface"], "search-surface"),
        (["cluster:skill-taxonomy"], "search-surface"),
        (["cluster:llm-latency"], "llm-router"),
        (["cluster:router"], "llm-router"),
        (["cluster:zzz"], "other"),
        ([], "other"),
        (["area:x", "STATUS:done"], "other"),
        # thread rule wins over cluster when both are present.
        (["cluster:pcb-drc", "thread:catalysis-selectivity"], "reaction-graph"),
        # namespaced spellings normalise the same way.
        (["OPEN:cluster:pcb-drc"], "pcb"),
    ],
)
def test_mapping(tags: list[str], expected: str) -> None:
    assert goal_of(tags) == expected
    assert expected in GOALS


def test_backfill_dry_run_writes_nothing() -> None:
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.side_effect = [
        [(1, "x", {}), (2, "x", {})],
        [("OPEN", "cluster:pcb-drc")],
        [("OPEN", "goal:pathway")],
    ]
    result = origin_backfill(store, goal=True)
    assert "'pcb': 1" in result
    store.add_tag.assert_not_called()


def test_backfill_apply_stamps_goal_and_preserves_override() -> None:
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.side_effect = [
        [(1, "x", {}), (2, "x", {})],
        [("OPEN", "cluster:pcb-drc")],
        [("OPEN", "goal:other")],
    ]
    origin_backfill(store, apply=True, goal=True)
    assert store.add_tag.call_count == 1
    assert store.add_tag.call_args.args[0] == 1
    assert "goal:pcb" in str(store.add_tag.call_args)
