"""Regression for gr342562: the CLI env/argparse seam that decides the
finding-chase pass's ``with_llm``.

``precis.cli.worker._build_chase_pass`` computes ``_chase_with_llm =
args.with_llm or env_flag("PRECIS_CHASE_LLM")`` for the embedder-load
decision, but a prior version of the ``_chase_pass`` closure passed
``run_finding_chase_pass(..., with_llm=args.with_llm)`` instead of that
computed flag. ``args.with_llm`` is an argparse ``store_true`` (always
``False`` in production — no playbook/plist ever passes ``--with-llm``),
so the concrete ``False`` overrode ``run_finding_chase_pass``'s own env
fallback, and the LLM verifier — and downstream, the taproot forward
bridge, which requires a verifier verdict — never ran in prod despite
``PRECIS_CHASE_LLM=1`` being deployed.

Hermetic: no DB, no LLM. ``run_finding_chase_pass`` is stubbed and its
call kwargs captured; ``store``/``handlers`` are never touched by the
code path this test drives (the taproot flag is off, so no embedder
resolution is attempted either).
"""

from __future__ import annotations

import argparse
from typing import Any, cast
from unittest.mock import patch

from precis.cli.worker import _build_chase_pass
from precis.store import Store

_RUN_PATH = "precis.workers.chase.run_finding_chase_pass"


def _args(*, with_llm: bool) -> argparse.Namespace:
    return argparse.Namespace(with_llm=with_llm)


def test_env_flag_alone_turns_on_with_llm(monkeypatch: Any) -> None:
    """``--with-llm`` absent (argparse default False) but
    ``PRECIS_CHASE_LLM=1`` set -- the pass must still call
    ``run_finding_chase_pass`` with ``with_llm=True``."""
    monkeypatch.setenv("PRECIS_CHASE_LLM", "1")
    monkeypatch.delenv("PRECIS_TAPROOT_CHASE_ENABLED", raising=False)

    with patch(_RUN_PATH) as mock_run:
        mock_run.return_value = {"claimed": 0, "ok": 0, "failed": 0}
        chase_pass = _build_chase_pass(
            _args(with_llm=False), store=cast(Store, object()), handlers=[]
        )
        chase_pass(5)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["with_llm"] is True


def test_no_flag_and_no_env_leaves_with_llm_off(monkeypatch: Any) -> None:
    """Inverse: neither ``--with-llm`` nor the env var set -- deterministic
    chase, no verifier."""
    monkeypatch.delenv("PRECIS_CHASE_LLM", raising=False)
    monkeypatch.delenv("PRECIS_TAPROOT_CHASE_ENABLED", raising=False)

    with patch(_RUN_PATH) as mock_run:
        mock_run.return_value = {"claimed": 0, "ok": 0, "failed": 0}
        chase_pass = _build_chase_pass(
            _args(with_llm=False), store=cast(Store, object()), handlers=[]
        )
        chase_pass(5)

    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["with_llm"] is False
