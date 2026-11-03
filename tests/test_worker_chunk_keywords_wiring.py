"""Regression for gr549751: chunk_keywords' batch-size knob was dead code
in production — the same trap as gr454865's chase_trigger.

The ``_chunk_keywords_pass`` closure inside ``build_ref_passes`` forwarded
the *shared loop* ``batch_size`` (the generic ``--batch-size`` flag,
argparse default ``32``) straight into ``run_chunk_keywords_pass``. That
capped every claim at 32 while the pass's own default (50, then 256) sat
unused, and a 1.6M-chunk backlog drained at ~43 chunks/h. The closure now
passes ``batch_size=None`` so ``PRECIS_CHUNK_KEYWORDS_BATCH_SIZE``
governs, with ``PRECIS_CHUNK_KEYWORDS_MAX_SECONDS`` bounding the
advisory-lock hold.

Tested the same hermetic way as ``test_worker_chase_trigger_wiring.py``:
``run_chunk_keywords_pass`` is stubbed and its call kwargs captured; the
embedder resolve is patched so no model loads and no DB is touched.
"""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli._worker_passes import build_ref_passes
from precis.store import Store
from precis.workers.service_config import ServiceConfigResolver

_RUN_PATH = "precis.workers.chunk_keywords.run_chunk_keywords_pass"
_EMBED_PATH = "precis.cli.worker._resolve_embedder"


def _args() -> argparse.Namespace:
    # ``_resolve_embedder`` (patched out below) plus the unconditional
    # ``args.profile`` read in ``build_ref_passes`` — ``system`` keeps
    # the agent-profile branches off this path.
    return argparse.Namespace(embedder="mock", profile="system")


def test_pass_does_not_forward_the_generic_loop_batch_size() -> None:
    """The closure must call ``run_chunk_keywords_pass(batch_size=None)``
    no matter what the shared loop passes it, so the pass's own env knob
    governs (``None`` -> ``_batch_size_default()`` -> 256)."""
    with (
        patch(_EMBED_PATH, return_value=object()),
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = {"claimed": 0, "ok": 0, "failed": 0}
        ref_passes, _, _, _ = build_ref_passes(
            _args(),
            store=cast(Store, object()),
            handlers=[],
            _register=lambda name: name == "chunk_keywords",
            _svc_resolver=cast(ServiceConfigResolver, object()),
        )
        pass_fn = next(p for p in ref_passes if p.__name__ == "_chunk_keywords_pass")
        # 32 is the runner's generic --batch-size default — the exact
        # value that used to leak through and cap the claim at 32.
        pass_fn(32)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["batch_size"] is None


def test_dedicated_env_keeps_the_shared_pass_off(monkeypatch: Any) -> None:
    """``PRECIS_CHUNK_KEYWORDS_DEDICATED=1`` (ops set it when the
    dedicated lane owns the queue, gr520236) means the rotation never
    registers the ref-pass — the lane and the tick can't double-run."""
    monkeypatch.setenv("PRECIS_CHUNK_KEYWORDS_DEDICATED", "1")
    with (
        patch(_EMBED_PATH, return_value=object()),
        patch(_RUN_PATH) as mock_run,
    ):
        ref_passes, _, _, _ = build_ref_passes(
            _args(),
            store=cast(Store, object()),
            handlers=[],
            _register=lambda name: name == "chunk_keywords",
            _svc_resolver=cast(ServiceConfigResolver, object()),
        )
    assert not any(p.__name__ == "_chunk_keywords_pass" for p in ref_passes)
    mock_run.assert_not_called()


def test_only_overrides_the_dedicated_env(monkeypatch: Any) -> None:
    """An explicit ``--only chunk_keywords`` still registers — the
    operator escape hatch for a manual run while the lane owns prod."""
    monkeypatch.setenv("PRECIS_CHUNK_KEYWORDS_DEDICATED", "1")
    args = _args()
    args.only = ["chunk_keywords"]
    with (
        patch(_EMBED_PATH, return_value=object()),
        patch(_RUN_PATH),
    ):
        ref_passes, _, _, _ = build_ref_passes(
            args,
            store=cast(Store, object()),
            handlers=[],
            _register=lambda name: name == "chunk_keywords",
            _svc_resolver=cast(ServiceConfigResolver, object()),
        )
    assert any(p.__name__ == "_chunk_keywords_pass" for p in ref_passes)
