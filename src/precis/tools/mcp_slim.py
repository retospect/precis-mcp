"""Slim advertised MCP schema for ``put`` / ``edit`` / ``search``.

``tools/core.py``'s verb functions double as FastMCP tool schemas, so their
~100 kind-specific parameters were paid for by every turn of every session
(docs/backlog/mcp-verb-schema-diet.md). This module registers a *slim*
twin per verb: only the CORE parameters (chosen from 30 days of the prod
tool-call ledger) are advertised; everything else rides ``args={...}``.

The slim function forwards to the unchanged full function in
:mod:`precis.tools.core` (still used in-process, by the CLI adapter and by
the ``command`` profile), after promoting any ``args`` key that names one of
the full function's own parameters back to that parameter. Keys the full
function does not declare stay in ``args`` and ride the ``__extras__``
channel through the dispatcher's accepted-kwargs gate (unknown key ->
``BadInput`` listing the kind's accepted keys).

Backward compatibility: FastMCP's argument model silently drops undeclared
top-level keys (``extra='ignore'``), which would turn every legacy
``put(kind=..., rxn_smiles=...)`` into a silent no-op. :func:`allow_extra_arguments`
swaps the registered tool's argument model for one that keeps extras, and the
slim function treats them as if they had been passed inside ``args`` and
appends a one-line deprecation note to the response.
"""

from __future__ import annotations

import inspect
from collections.abc import Callable
from typing import Any, cast

from mcp.types import CallToolResult, TextContent
from pydantic import ConfigDict

from precis.runtime.dispatch import CORE_PARAMS, coerce_json_container

#: FastMCP-injected, not part of the wire schema; kept in the signature so
#: FastMCP still detects it.
_INJECTED = ("ctx",)

_ARGS_LINE = (
    "Kind-specific fields go in `args={...}`; an unknown key lists the "
    "kind's accepted ones."
)

_SEARCH_DOC = """Hybrid lexical + semantic search across kinds.

`page_size` ≤ 100; `page=N` paginates. Omit `kind` (or `'*'`) for cross-kind
fan-out. `mode=` 'hybrid' (default) / 'lexical' / 'semantic' / 'verbatim'.
Kind-specific fields (`exclude`, `reach`, `folder`, `queries`, `per_paper`,
`trust`, `wants`, `compose`, `property`/`min`/`max`/`unit`, ...) go in
`args={...}`; an unknown key lists the kind's accepted ones.

Full docs: get(kind='skill', id='precis-search-help').
"""


def _slim_doc(verb: str, full: Callable[..., Any]) -> str:
    if verb == "search":
        return _SEARCH_DOC
    doc = inspect.cleandoc(full.__doc__ or "")
    marker = "Full reference:"
    head, sep, tail = doc.partition(marker)
    return (
        f"{head.rstrip()}\n\n{_ARGS_LINE}\n\n{sep}{tail}"
        if sep
        else (f"{doc}\n\n{_ARGS_LINE}")
    )


def _append_note(result: Any, note: str) -> Any:
    """Append ``note`` to a str result; lead an error envelope with it.

    On an error the misplaced key is the likeliest cause, so the fix goes
    first rather than under the handler's own complaint.
    """
    if isinstance(result, str):
        return f"{result}\n{note}"
    if isinstance(result, CallToolResult):
        for part in result.content:
            if isinstance(part, TextContent):
                part.text = (
                    f"{note}\n{part.text}" if result.isError else f"{part.text}\n{note}"
                )
                break
        return result
    return result


def deprecation_note(keys: list[str]) -> str:
    joined = ", ".join(keys)
    return f"note: pass {joined} inside args={{...}}; top-level {joined} is deprecated"


def make_slim_verb(verb: str, full: Callable[..., Any]) -> Callable[..., Any]:
    """Build the slim twin of ``full`` advertising only ``CORE_PARAMS[verb]``."""
    full_sig = inspect.signature(full, eval_str=True)
    keep = set(CORE_PARAMS[verb]) | set(_INJECTED)
    slim_params = [p for n, p in full_sig.parameters.items() if n in keep]
    missing = set(CORE_PARAMS[verb]) - set(full_sig.parameters)
    if missing:  # pragma: no cover - guarded by tests
        raise RuntimeError(f"slim {verb}: core params not on full verb: {missing}")
    slim_names = {p.name for p in slim_params}
    full_names = set(full_sig.parameters)

    def slim(**kwargs: Any) -> Any:
        call: dict[str, Any] = {}
        args: dict[str, Any] = dict(kwargs.pop("args", None) or {})
        deprecated: list[str] = []
        for key, value in kwargs.items():
            if key in slim_names:
                call[key] = value
            else:
                # Undeclared top-level key (legacy call shape): treat it as
                # if it had been passed inside ``args``.
                deprecated.append(key)
                args.setdefault(key, value)
        # Promote args keys that name a full-verb parameter back to that
        # parameter, so the verb function's own validation/forwarding runs.
        for key in list(args):
            if key in full_names and key not in slim_names and key not in _INJECTED:
                call[key] = coerce_json_container(
                    args.pop(key), full_sig.parameters[key].annotation
                )
        if args:
            call["args"] = args
        result = full(**call)
        if deprecated:
            result = _append_note(result, deprecation_note(sorted(deprecated)))
        return result

    slim.__name__ = full.__name__
    slim.__qualname__ = full.__qualname__
    slim.__module__ = full.__module__
    slim.__doc__ = _slim_doc(verb, full)
    slim.__annotations__ = {p.name: p.annotation for p in slim_params}
    slim.__annotations__["return"] = full_sig.return_annotation
    cast(Any, slim).__signature__ = full_sig.replace(parameters=slim_params)
    return slim


def allow_extra_arguments(tool: Any) -> None:
    """Make ``tool``'s FastMCP argument model keep undeclared keys.

    FastMCP validates a call against a pydantic model built from the
    function signature (``extra='ignore'`` by default) and then calls the
    function with only the declared fields, so an undeclared key vanishes
    before any of our code runs. Replace the model with a subclass that
    keeps extras and returns them from ``model_dump_one_level``. The
    advertised JSON schema (``tool.parameters``) was computed at
    registration and is not affected.
    """
    base = tool.fn_metadata.arg_model

    def model_dump_one_level(self: Any) -> dict[str, Any]:
        out = base.model_dump_one_level(self)
        out.update(self.model_extra or {})
        return out

    tool.fn_metadata.arg_model = type(
        base.__name__,
        (base,),
        {
            "model_config": ConfigDict(extra="allow", arbitrary_types_allowed=True),
            "model_dump_one_level": model_dump_one_level,
            "__module__": base.__module__,
        },
    )


def slim_verbs(registry: dict[str, dict[str, Any]]) -> dict[str, Callable[..., Any]]:
    """The slim twins for every verb in ``CORE_PARAMS`` present in ``registry``."""
    return {
        v: make_slim_verb(v, registry[v]["func"]) for v in CORE_PARAMS if v in registry
    }
