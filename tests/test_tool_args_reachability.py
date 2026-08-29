"""Every handler that reads its payload from ``args=`` must be reachable
through the tool surface that advertises it.

gr267461: ``PcbHandler.put`` takes ``args: dict`` and reads its ENTIRE
payload from it — netlist authoring plus every ``op=`` (place/route/move/
rip/pin_side/plane_net/class_rules). ``precis.tools.core.put`` declared no
``args`` parameter, so every call the ``precis-pcb-help`` skill documents
raised ``unexpected keyword argument 'args'``. The whole pcb write path was
unreachable from the MCP tool, ``precis tools put`` and ``precis eval``
alike — all three bottom out in ``core.put``.

It survived because the handler tests call ``PcbHandler.put(...)`` directly
in Python, bypassing the tool function entirely. The tests exercised a
handler the tool surface could not reach: "tested but structurally
unreachable" (``docs/backlog/pcb-residual-defects-0828.md``), applied to a
kind's whole write surface.

These tests are deliberately signature-level and DB-free, so they run
anywhere and fail for exactly one reason.
"""

from __future__ import annotations

import inspect
from typing import Any

import pytest

from precis.tools import TOOL_REGISTRY

# Handler classes whose methods are checked against the tool surface.
# Imported lazily inside the fixture so a missing optional extra degrades
# to a skip rather than a collection error.
_VERBS = ("get", "put")


def _handler_classes() -> list[type]:
    from precis.handlers.cad import CadHandler
    from precis.handlers.pcb import PcbHandler
    from precis.handlers.random import RandomHandler
    from precis.handlers.structure import StructureHandler

    return [PcbHandler, CadHandler, StructureHandler, RandomHandler]


def _declares_args(method: Any) -> bool:
    """True iff ``method`` opts into the unflattened extras passthrough.

    This mirrors ``precis.runtime.dispatch``'s own test — it forwards the
    ``__extras__`` dict as ``args=`` when ``"args" in accepted``, and
    flattens it into top-level kwargs otherwise.
    """
    try:
        return "args" in inspect.signature(method).parameters
    except (TypeError, ValueError):  # builtins / C-implemented
        return False


@pytest.mark.parametrize("verb", _VERBS)
def test_tool_verb_accepts_args_when_a_handler_declares_it(verb: str) -> None:
    """If ANY handler's ``verb`` reads ``args=``, the tool must accept it.

    The failure this pins is total, not partial: without the parameter the
    call raises TypeError before dispatch, so no server-side error path can
    teach the agent what went wrong.
    """
    declaring = [
        cls.__name__
        for cls in _handler_classes()
        if _declares_args(getattr(cls, verb, None))
    ]
    if not declaring:
        pytest.skip(f"no sampled handler declares args= on .{verb}()")

    tool_fn = TOOL_REGISTRY[verb]["func"]
    assert "args" in inspect.signature(tool_fn).parameters, (
        f"tools.core.{verb}() has no args= parameter, but {declaring} read "
        f"their payload from it — every documented {verb}(args={{...}}) call "
        f"raises 'unexpected keyword argument' before reaching dispatch."
    )


def test_pcb_put_payload_reaches_the_handler_unflattened() -> None:
    """The pcb write path specifically, end to end at the signature level.

    ``PcbHandler.put`` must keep an explicit ``args`` parameter: dispatch
    decides between passing extras through whole and flattening them into
    top-level kwargs by testing for exactly that. Deleting it would make
    the payload arrive as ``components=``/``nets=`` kwargs, which the
    handler's ``**_kw`` catch-all swallows SILENTLY — a worse failure than
    the TypeError this test's sibling covers, because it looks like success.
    """
    from precis.handlers.pcb import PcbHandler

    params = inspect.signature(PcbHandler.put).parameters
    assert "args" in params, (
        "PcbHandler.put lost its explicit args= parameter; dispatch will now "
        "flatten the payload into top-level kwargs and **_kw will swallow it "
        "without error"
    )
    assert "args" in inspect.signature(TOOL_REGISTRY["put"]["func"]).parameters
