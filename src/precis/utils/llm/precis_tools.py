"""Bridge the precis verb registry + in-process dispatch to the OSS tool loop.

Two adapters, so an open-source model can drive the precis verbs
(get/search/put/edit/delete/tag/link/more) with **no MCP socket round-trip**:

* :func:`precis_tool_specs` — render :data:`precis.tools.TOOL_REGISTRY` into
  :class:`~precis.utils.llm.openai_tools.ToolSpec`s (name + doc + a JSON-Schema
  built from each verb's signature). Pure over the registry.
* :func:`runtime_executor` — wrap ``runtime.dispatch(verb, args) -> str`` as the
  loop's ``execute`` callback. ``dispatch`` already renders errors as text and
  never raises (ADR: MCP expects a string), so a bad call feeds the model a
  legible error instead of aborting the run. The dispatcher is injectable so
  this is unit-testable with a fake; the default uses the same process-global
  runtime the MCP server path uses.

Kept out of :mod:`precis.utils.llm.router` so the router stays free of the
worker/DB import chain — the provider imports this lazily, only when the
OpenAI tools loop actually runs.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.utils.llm.openai_tools import ToolSpec

if TYPE_CHECKING:
    from collections.abc import Callable

    from precis.utils.llm.openai_tools import ToolExecutor

#: ``more`` is pagination over a cursor a verb response hands back — useless
#: without a live prior response, and it confuses a fresh tool-caller, so it is
#: excluded from the advertised set by default.
_DEFAULT_EXCLUDE: frozenset[str] = frozenset({"more"})


def _json_schema_for(pinfo: dict[str, Any], *, description: str = "") -> dict[str, Any]:
    """Map one registry parameter to a JSON-Schema property.

    Annotations are strings (the verb modules use ``from __future__ import
    annotations``), so this reads the string form. Scalars collapse to
    ``string`` — precis coerces ``id`` from ``"42"`` and every verb tolerates
    string scalars — while lists and dicts keep their container shape so the
    model fills them correctly.
    """
    ann = str(pinfo.get("annotation", "")).strip()
    schema: dict[str, Any]
    if pinfo.get("is_list") or ann.startswith("list["):
        schema = {"type": "array", "items": {"type": "string"}}
    elif "dict" in ann:
        schema = {"type": "object", "additionalProperties": True}
    elif ann.startswith("bool"):
        schema = {"type": "boolean"}
    elif ann.startswith("int"):
        schema = {"type": "integer"}
    elif ann.startswith("float"):
        schema = {"type": "number"}
    else:
        schema = {"type": "string"}
    if description:
        schema["description"] = description
    return schema


def _spec_from_registry(name: str, info: dict[str, Any]) -> ToolSpec:
    params: dict[str, dict[str, Any]] = info.get("parameters", {})
    cli_help = info.get("cli_help") or {}
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, pinfo in params.items():
        desc = cli_help.get(pname, "") if isinstance(cli_help, dict) else ""
        props[pname] = _json_schema_for(pinfo, description=desc)
        if pinfo.get("required"):
            required.append(pname)
    parameters: dict[str, Any] = {"type": "object", "properties": props}
    if required:
        parameters["required"] = required
    return ToolSpec(
        name=name,
        description=(info.get("doc") or "").strip(),
        parameters=parameters,
    )


def precis_tool_specs(*, exclude: frozenset[str] = _DEFAULT_EXCLUDE) -> list[ToolSpec]:
    """The precis verbs as OpenAI tool specs, ``more`` excluded by default."""
    from precis.tools import TOOL_REGISTRY

    return [
        _spec_from_registry(name, info)
        for name, info in TOOL_REGISTRY.items()
        if name not in exclude
    ]


def runtime_executor(
    dispatch: Callable[[str, dict[str, Any]], str] | None = None,
) -> ToolExecutor:
    """Build the loop's ``execute`` callback over an in-process verb dispatcher.

    ``dispatch`` defaults to the process-global runtime's (the same one the MCP
    server uses); pass one in tests. Only advertised (non-excluded) tools should
    reach here, but an unknown/mistyped name still resolves to ``dispatch``'s
    rendered-error string, which the model reads and can correct.
    """

    def _resolve() -> Callable[[str, dict[str, Any]], str]:
        if dispatch is not None:
            return dispatch
        from precis.tools.core import _get_runtime

        runtime = _get_runtime()
        return runtime.dispatch

    resolved = _resolve()

    def execute(name: str, args: dict[str, Any]) -> str:
        return resolved(name, args)

    return execute


__all__ = ["precis_tool_specs", "runtime_executor"]
