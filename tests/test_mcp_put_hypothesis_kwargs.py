"""The hypothesis-proposal kwargs must survive the MCP tool boundary.

``tools/core.py::put``'s signature *is* the generated MCP JSON Schema, and
its dispatch payload is hardcoded — a kwarg missing from either is
unreachable over MCP no matter what the handler accepts. That is not
hypothetical: ``put(kind='job', parent_id=…)`` shipped broken exactly this
way (see `test_mcp_put_parent_id.py`), and `edit` still drops `doi`/`title`
for the same reason.

The dream agent reaches this door *only* over MCP, so a dropped kwarg here
is a silent behaviour change with no traceback: `hypothesis` falling away
would route the call into ordinary hub mode, and a dropped `motivated_by`
would trip the >=2-motivator guard with a confusing message. These pin the
boundary; `test_finding_hypothesis_put.py` pins what the handler does with
the values once they arrive.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from precis.tools import core as tools_core

_HYPOTHESIS_KWARGS = (
    "hypothesis",
    "motivation",
    "testable_by",
    "motivated_by",
    "from_memory",
)


@pytest.mark.parametrize("name", _HYPOTHESIS_KWARGS)
def test_put_signature_advertises_the_hypothesis_kwargs(name: str) -> None:
    """The wire schema must carry each one — a strict-schema client rejects
    the call before the server can teach it otherwise."""
    assert name in inspect.signature(tools_core.put).parameters


def test_put_forwards_the_hypothesis_kwargs_into_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        captured["verb"] = verb
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)

    tools_core.put(
        kind="finding",
        title="DFT predicts a 12% modulus rise under uniaxial strain.",
        hypothesis=True,
        motivation="Both sources share a mechanism; the transfer is untested.",
        testable_by="nanoindentation of the pressed film versus pristine",
        motivated_by=["pc293", "fi1234"],
        from_memory="me34468",
    )

    payload = captured["payload"]
    assert captured["verb"] == "put"
    assert payload["hypothesis"] is True
    assert payload["motivated_by"] == ["pc293", "fi1234"]
    assert (
        payload["testable_by"] == "nanoindentation of the pressed film versus pristine"
    )
    assert payload["from_memory"] == "me34468"
    assert "transfer is untested" in payload["motivation"]


def test_put_without_hypothesis_does_not_force_the_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``hypothesis=False`` is the "not this mode" default. It rides as
    ``None`` so `_invoke_handler`'s None-filter drops it and the handler's
    own default stands — passing a literal ``False`` through would be
    indistinguishable from an explicit opt-out to any future caller."""
    captured: dict[str, Any] = {}

    def _fake_dispatch(verb: str, payload: dict[str, Any]) -> str:
        captured["payload"] = payload
        return "ok"

    monkeypatch.setattr(tools_core, "_dispatch", _fake_dispatch)

    tools_core.put(kind="finding", title="An ordinary finding.")

    assert captured["payload"]["hypothesis"] is None
