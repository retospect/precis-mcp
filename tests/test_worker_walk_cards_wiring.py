"""The standalone walk_cards CLI pass must bind its BatchResult type."""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli._worker_passes import build_ref_passes
from precis.store import Store


def test_walk_cards_only_pass_returns_batch_result() -> None:
    store = cast(Store, object())
    args = argparse.Namespace(profile="system", only=["walk_cards"])
    result = {"claimed": 3, "ok": 2, "failed": 0, "owners": 1}

    with patch(
        "precis.workers.walk_cards.run_walk_cards_pass", return_value=result
    ) as run:
        passes, *_ = build_ref_passes(
            args,
            store,
            [],
            lambda name: name == "walk_cards",
            cast(Any, None),
        )
        (walk_cards_pass,) = [p for p in passes if p.__name__ == "_walk_cards_pass"]
        batch = walk_cards_pass(32)

    run.assert_called_once_with(store)
    assert (batch.claimed, batch.ok, batch.failed) == (3, 2, 0)
