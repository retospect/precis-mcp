"""Every ``precis`` CLI command that reaches the model router binds the
process store first (gr489244; was docs/backlog/cli-bind-store-audit.md).

Unbound, ``budget.meter.active_store()`` is None and every ``live_config``
override reads as "no override", silently: ``precis cast run`` ignored the
operator's live routing that way. The walk is static
(``tests/_cli_router_graph.py``): from each registered command's ``run``
function to the router, statement by statement, demanding a
``bind_store`` (directly, through ``cli._common.bind_process_store`` /
``connect_router_store``, or inside a callee such as ``build_runtime``)
before the first statement that can reach it.
"""

from __future__ import annotations

import pytest

from precis.cli.registry import JOB_COMMANDS, commands
from tests import _cli_router_graph as graph

_ALL = (*commands(), *JOB_COMMANDS)


def _entry(module: str, run: str) -> str:
    return f"{module}.{run}"


def _routed() -> list[tuple[str, str]]:
    return [
        (c.name, _entry(c.module, c.run))
        for c in _ALL
        if graph.reaches([_entry(c.module, c.run)], graph.ROUTER_SINKS)
    ]


def test_the_walk_finds_the_known_router_commands() -> None:
    """Guard the guard: a resolver regression that found nothing would pass
    the main test vacuously."""
    names = {name for name, _ in _routed()}
    assert {"cast", "llm", "worker", "taproot", "quest", "taxonomy-bootstrap"} <= names


def test_a_command_that_routes_before_binding_is_caught() -> None:
    """``precis cast run`` before its fix: dispatch without a bind."""
    entry = "precis.cli.cast._cmd_run"
    assert graph.reaches([entry], graph.ROUTER_SINKS)
    assert graph.unbound_router_call(entry) is not None


@pytest.mark.parametrize(("name", "entry"), _routed(), ids=[n for n, _ in _routed()])
def test_command_binds_store_before_reaching_router(name: str, entry: str) -> None:
    offence = graph.unbound_router_call(entry)
    assert offence is None, (
        f"precis {name}: reaches the model router before binding the store "
        f"(call cli._common.bind_process_store(store) right after Store.connect, "
        f"or connect_router_store for a command with no database work): {offence}"
    )
