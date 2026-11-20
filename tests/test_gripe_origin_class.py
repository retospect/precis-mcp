"""Origin rules leave unsupported provenance unclassified."""

from unittest.mock import MagicMock

import pytest

from precis.cli.gripe import origin_backfill
from precis.utils.gripe_origin_class import classify_origin


@pytest.mark.parametrize(
    ("signal", "expected"),
    [
        ("reto:ask", "human"),
        ("fix:reto", "human"),
        ("User request: x", "human"),
        *[
            (s, "self-improvement")
            for s in (
                "quest-need:x",
                "[callerr:hint] x",
                "watchdog:x",
                "surface-review",
                "dup-suggest:x",
                "memory-lint",
                "backlog-lint",
                "cluster-desk",
            )
        ],
        *[
            (s, "specification")
            for s in ("spec:x", "needs-spec", "wave:x", "Slice 2 of docs/backlog/x")
        ],
        ("unknown", None),
    ],
)
def test_rules(signal: str, expected: str | None) -> None:
    assert classify_origin([], signal) == expected
    assert classify_origin([signal], "unknown") == expected
    assert classify_origin(["origin-class:human", signal], signal) == "human"


def test_writer() -> None:
    assert classify_origin([], "unknown", {"human": "reto"}) == "human"
    assert classify_origin([], "unknown", {"model": "agent"}) is None


def test_backfill_dry_run_writes_nothing() -> None:
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.side_effect = [
        [(1, "quest-need:x", {}), (2, "unknown", {})],
        [],
        [],
    ]
    result = origin_backfill(store)
    assert "self-improvement" in result
    assert "unclassified (1): gr2" in result
    store.add_tag.assert_not_called()


def test_backfill_apply_preserves_override() -> None:
    store = MagicMock()
    conn = store.pool.connection.return_value.__enter__.return_value
    conn.execute.return_value.fetchall.side_effect = [
        [(1, "quest-need:x", {}), (2, "quest-need:x", {})],
        [("OPEN", "origin-class:human")],
        [],
    ]
    origin_backfill(store, apply=True)
    assert store.add_tag.call_count == 1
    assert store.add_tag.call_args.args[0] == 2
