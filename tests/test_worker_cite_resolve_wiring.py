"""Wiring: cite_resolve's OpenAlex leg must reach the polite pool.

``run_cite_resolve_pass`` takes ``email=`` for the live
``/works?search=`` leg (gr513768 slice 4); the worker wiring must
forward the configured ``contact.polite_email`` or every lookup lands
in the anonymous pool. Hermetic: settings and the pass are stubbed, no
DB/network is touched.
"""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli._worker_passes import build_ref_passes
from precis.store import Store

_RUN_PATH = "precis.workers.cite_resolve.run_cite_resolve_pass"
_SETTINGS_PATH = "precis.cli._worker_passes.settings.get_str"

_PASS_RESULT = {
    "papers_swept": 0,
    "chunks_swept": 0,
    "links_ensured": 0,
    "entries_resolved": 0,
    "entries_missed": 0,
    "deferred": 0,
    "failed": 0,
}


def _cite_resolve_pass() -> Any:
    args = argparse.Namespace(profile="system", only=["cite_resolve"])
    passes, *_ = build_ref_passes(
        args,
        cast(Store, object()),
        [],
        lambda name: name == "cite_resolve",
        cast(Any, None),
    )
    (p,) = [p for p in passes if p.__name__ == "_cite_resolve_pass"]
    return p


def test_worker_wiring_passes_the_configured_polite_email() -> None:
    with (
        patch(_SETTINGS_PATH, return_value="dev@example.org") as mock_get,
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = _PASS_RESULT
        result = _cite_resolve_pass()(8)
    mock_get.assert_called_once_with("contact.polite_email")
    assert mock_run.call_args.kwargs["email"] == "dev@example.org"
    assert (result.claimed, result.ok, result.failed) == (0, 0, 0)


def test_worker_wiring_passes_empty_email_when_unset() -> None:
    """No configured contact → ``""``, never a made-up one."""
    with (
        patch(_SETTINGS_PATH, return_value=None),
        patch(_RUN_PATH) as mock_run,
    ):
        mock_run.return_value = _PASS_RESULT
        _cite_resolve_pass()(8)
    assert mock_run.call_args.kwargs["email"] == ""
