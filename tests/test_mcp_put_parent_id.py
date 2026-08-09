"""Regression: ``put(kind='job', parent_id=…)`` must forward ``parent_id``
across the MCP tool boundary (``tools.core.put``).

The kwarg was absent from the tool's signature and dispatch payload, so
strict-schema MCP clients stripped it and every ad-hoc job submit errored
``requires parent_id`` — the documented ``put(kind='job', parent_id=<todo>,
…)`` path was uncallable over MCP. Discovered dogfooding the
``taproot_backfill`` ship (6dbe5e94); see OPEN-ITEMS.

The fix is entirely at the tool boundary: ``core.put`` now declares
``parent_id`` and threads it into the dispatch payload (the handler,
``JobHandler.put``, has always accepted it). These tests pin exactly that
boundary — that the value the client sends reaches dispatch — rather than
any one job_type's downstream ``validate_submit`` (which gates on env /
links first and would make an end-to-end submit brittle).
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from precis.tools import core as tools_core


def test_put_signature_advertises_parent_id() -> None:
    """The wire schema must carry ``parent_id`` — a strict-schema client
    rejects the call before the server can teach it otherwise."""
    assert "parent_id" in inspect.signature(tools_core.put).parameters


def test_put_forwards_parent_id_into_dispatch_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression: the ``parent_id`` an MCP client passes reaches the
    dispatch payload verbatim (before the fix it was silently dropped)."""
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        captured["verb"] = verb
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)

    tools_core.put(kind="job", parent_id=4242, job_type="fix_gripe")

    assert captured["verb"] == "put"
    assert captured["payload"]["parent_id"] == 4242


def test_put_parent_id_accepts_slug_ref_form(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A str parent_id (the polymorphic build-subject case) is
    forwarded untouched — the handler does the int coercion, not the tool."""
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)

    tools_core.put(kind="job", parent_id="1720", job_type="draft_export")

    assert captured["payload"]["parent_id"] == "1720"
