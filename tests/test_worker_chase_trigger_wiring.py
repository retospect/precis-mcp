"""Regression for gr454865: chase_trigger's batch-size env knob was dead code
in production.

The CLI closure that runs the pass forwarded the *shared loop* ``batch_size``
(the generic ``--batch-size`` flag, argparse default ``32``) straight into
``run_chase_trigger_pass``. But that function only falls back to
chase_trigger's own ``PRECIS_TAPROOT_CHASE_TRIGGER_BATCH_SIZE`` knob (default
``200``) when its ``batch_size`` kwarg is ``None`` — so the concrete ``32``
always won, chase_trigger claimed 32 chunks/pass instead of 200, and a 3.15M
backlog drained at only ~1k chunks/day.

Exactly the argparse-default-overrides-env-fallback trap as gr342562's
``with_llm`` (see ``test_worker_chase_wiring.py``), and tested the same
hermetic way: ``run_chase_trigger_pass`` is stubbed and its call kwargs
captured; the embedder resolve is patched so no model loads and no DB/LLM is
touched.
"""

from __future__ import annotations

import argparse
from typing import cast
from unittest.mock import patch

from precis.cli.worker import _build_chase_trigger_pass
from precis.store import Store

_RUN_PATH = "precis.workers.chase_trigger.run_chase_trigger_pass"
_EMBED_PATH = "precis.cli.worker._resolve_embedder"


def _args() -> argparse.Namespace:
    # Only ``_resolve_embedder`` reads ``args`` on this path, and it's patched
    # out below, so a bare Namespace is enough.
    return argparse.Namespace(embedder="mock")


def test_pass_does_not_forward_the_generic_loop_batch_size() -> None:
    """The closure must call ``run_chase_trigger_pass(batch_size=None)`` no
    matter what the shared loop passes it, so the pass's own env knob governs
    (``None`` -> ``_batch_size_default()`` -> 200)."""
    with (
        patch(_EMBED_PATH, return_value=object()),
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = {
            "claim_embeds": 0,
            "chunks_swept": 0,
            "due_marked": 0,
            "failed": 0,
        }
        pass_fn = _build_chase_trigger_pass(
            _args(), store=cast(Store, object()), handlers=[]
        )
        # 32 is the runner's generic --batch-size default — the exact value
        # that used to leak through and cap the sweep at 32 chunks/pass.
        pass_fn(32)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["batch_size"] is None


def test_worked_units_roll_up_into_the_batch_result() -> None:
    """The result accounting the runner reads (claimed/ok/failed) still counts
    both swept chunks and refreshed claim embeddings as work — unchanged by
    the extraction, pinned so it can't silently regress."""
    with (
        patch(_EMBED_PATH, return_value=object()),
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = {
            "claim_embeds": 3,
            "chunks_swept": 5,
            "due_marked": 2,
            "failed": 1,
        }
        pass_fn = _build_chase_trigger_pass(
            _args(), store=cast(Store, object()), handlers=[]
        )
        result = pass_fn(32)

    assert result.handler == "chase_trigger"
    assert result.ok == 8  # chunks_swept + claim_embeds
    assert result.failed == 1
    assert result.claimed == 9  # worked + failed
