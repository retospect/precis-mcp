"""Tests for the per-MCP-call audit log (``_log_tool_call``).

The audit line is the diagnostic surface for "what did the agent
actually ask for?" — see :func:`precis.tools.core._log_tool_call`.
These tests pin the two contracts that make it useful for debugging:

* the search query (``q=``) and the addressing / citation fields the
  agent fumbles most are sampled verbatim (truncated), not dropped;
* a **failed** call logs at WARNING with a fuller payload (so it
  survives a server deployed at ``log_level=WARNING`` and the exact
  misuse is reconstructable).
"""

from __future__ import annotations

import logging

from precis.tools.core import _log_tool_call


def test_success_call_samples_query_and_scalars(caplog) -> None:
    """A successful call logs at INFO and includes ``q=`` + scalars."""
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="search",
            payload={"kind": "paper", "q": "photocatalytic NOx reduction"},
            duration_ms=12.0,
            error=False,
        )
    rec = caplog.records[-1]
    assert rec.levelno == logging.INFO
    msg = rec.getMessage()
    assert "verb=search" in msg
    # The query — the single most important "what is the agent asking"
    # field — must be present verbatim, not reduced to its presence.
    assert "photocatalytic NOx reduction" in msg


def test_failed_call_logs_at_warning_with_full_payload(caplog) -> None:
    """A failed call escalates to WARNING and widens the capture.

    The citation/finding fields agents fumble (``cited_in``, ``title``)
    and any otherwise-unsampled kwarg must appear so the exact misuse
    is reconstructable from the log alone.
    """
    with caplog.at_level(logging.WARNING, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="put",
            payload={
                "kind": "finding",
                "title": "gate-bias 2.4 kV / 30 s",
                "cited_in": "doi:10.1234/xyz",
                "scope": {"electrode": "Cu"},
            },
            duration_ms=3.0,
            error=True,
        )
    rec = caplog.records[-1]
    assert rec.levelno == logging.WARNING
    msg = rec.getMessage()
    assert "error=True" in msg
    assert "doi:10.1234/xyz" in msg  # the rejected cited_in
    assert "gate-bias" in msg  # title — only captured on the error path


def test_long_query_is_truncated(caplog) -> None:
    """Oversize free-text is bounded so the line stays grep-friendly."""
    with caplog.at_level(logging.INFO, logger="precis.tools.mcp_calls"):
        _log_tool_call(
            verb="search",
            payload={"q": "x" * 500},
            duration_ms=1.0,
            error=False,
        )
    msg = caplog.records[-1].getMessage()
    assert "…" in msg
    assert "x" * 500 not in msg
