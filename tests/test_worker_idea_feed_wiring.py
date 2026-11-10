"""The standalone idea_feed CLI pass must bind its BatchResult type."""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli._worker_passes import build_ref_passes
from precis.store import Store


def test_idea_feed_only_pass_returns_batch_result() -> None:
    store = cast(Store, object())
    args = argparse.Namespace(profile="system", only=["idea_feed"])
    result = {
        "claimed": 3,
        "ok": 1,
        "failed": 0,
        "found": 2,
        "judged": 2,
        "digest": 42,
    }

    with patch(
        "precis.workers.idea_feed.run_idea_feed_pass", return_value=result
    ) as run:
        passes, *_ = build_ref_passes(
            args,
            store,
            [],
            lambda name: name == "idea_feed",
            cast(Any, None),
        )
        (idea_feed_pass,) = [p for p in passes if p.__name__ == "_idea_feed_pass"]
        batch = idea_feed_pass(32)

    run.assert_called_once_with(store, enabled=True)
    assert (batch.claimed, batch.ok, batch.failed) == (3, 1, 0)
