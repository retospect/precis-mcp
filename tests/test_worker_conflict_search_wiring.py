"""Regression for gr493236: the conflict_search pass held the worker loop ~1 h.

The CLI closure forwarded the shared loop ``batch_size`` (``--batch-size``,
default 32) as ``limit``, overriding the pass's own
``PRECIS_CONFLICT_SEARCH_HUBS_PER_PASS`` (default 4): every prod pass swept
32 hubs × ~7 serial LLM calls (worker_logs: claimed=32, 3336-3775 s). And the
pass was not under the loop's ``PRECIS_REF_PASS_DEADLINE_S`` bound, so nothing
moved the loop on. Same argparse-default trap as chase_trigger's gr454865.
Hermetic: ``run_conflict_search_pass`` is stubbed, no DB/LLM is touched.
"""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli._worker_passes import build_ref_passes
from precis.store import Store
from precis.workers.runner import BoundedPass

_RUN_PATH = "precis.workers.conflict_search.run_conflict_search_pass"


def _conflict_search_pass() -> Any:
    args = argparse.Namespace(profile="system", only=["conflict_search"])
    passes, *_ = build_ref_passes(
        args,
        cast(Store, object()),
        [],
        lambda name: name == "conflict_search",
        cast(Any, None),
    )
    (p,) = [p for p in passes if p.__name__ == "_conflict_search_pass"]
    return p


def test_pass_is_under_the_loop_deadline(monkeypatch: Any) -> None:
    monkeypatch.delenv("PRECIS_REF_PASS_DEADLINE_S", raising=False)
    p = _conflict_search_pass()
    assert isinstance(p, BoundedPass)
    assert p.deadline_s == 300.0


def test_pass_does_not_forward_the_loop_batch_size() -> None:
    # Both names are bound when the pass is built, so patch around the build.
    with (
        patch("precis.cli.worker._resolve_embedder", return_value=object()),
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = {"hubs_claimed": 2, "hubs_swept": 1}
        result = _conflict_search_pass()(32)
    assert mock_run.call_args.kwargs["limit"] is None
    assert (result.claimed, result.ok, result.failed) == (2, 1, 1)
