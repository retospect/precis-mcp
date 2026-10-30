"""Slim advertised MCP schema for ``put`` / ``edit`` / ``search``.

``tools/core.py``'s verb functions double as FastMCP tool schemas, so their
~100 kind-specific parameters were paid for by every turn of every session
(docs/backlog/mcp-verb-schema-diet.md). This module registers a *slim*
twin per verb: only the CORE parameters (chosen from 30 days of the prod
tool-call ledger) are advertised; everything else rides ``args={...}``.

The slim function forwards to the unchanged full function in
:mod:`precis.tools.core` (still used in-process, by the CLI adapter and by
the ``command`` profile), after promoting any ``args`` key that names one of
the full function's own parameters back to that parameter, provided the
target kind's handler declares it top-level. Other keys stay in ``args`` and ride the ``__extras__``
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
    "Kind-specific fields go in `args={...}`; an unknown key fails the "
    "call with `[error:BadInput]` listing the kind's accepted ones."
)

#: Full-verb parameters the wrapper itself consumes — they never reach a
#: kind handler, so the ``accepted_top`` handler-kwargs gate must not keep
#: them in ``args``. ``search``'s legacy paging spellings are the case
#: that bites (gr530420): ``limit=3`` top-level was filed into ``args``,
#: died on the kind's unknown-key gate, and the deprecation note aimed
#: the caller at ``args={'limit': 3}`` — a second failing form. Nested
#: inside ``args=`` they still refuse (the args= slot is for kind keys);
#: the dispatcher's refusal names the canonical spelling in ``next:``.
_VERB_CONSUMED_PARAMS: dict[str, frozenset[str]] = {
    "search": frozenset({"k", "limit", "offset"}),
}

_SEARCH_DOC = """Hybrid lexical + semantic search across kinds.

`page_size` ≤ 100 bounds the top-N set (`k=`/`limit=` read as it). Omit
`kind` (or `'*'`) for cross-kind fan-out. `mode=` 'hybrid' (default) /
'lexical' / 'semantic' / 'verbatim'.
`scope=` searches inside one ref (handle or slug); `status=` filters by
`STATUS:` tag on kinds that carry that axis. Rows: `id`, `summary`,
`remaining_words`, `links`, `tags` — the legend is in the skill.
Some kinds are host-gated:
`NotFound` with `routes through host(s): …` means the kind exists but
this process lacks its provider. Kind-specific fields (`exclude`, `reach`,
`folder`, `queries`, `per_paper`, `trust`, `wants`, `compose`,
`property`/`min`/`max`/`unit`, ...) go in `args={...}`; an unknown key
fails the call with `[error:BadInput]` listing the kind's accepted ones.

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


def _removed_search_arg_error(
    verb: str, kind: Any, kwargs: dict[str, Any], args: dict[str, Any]
) -> Any:
    if verb != "search":
        return None
    supplied = set(kwargs) | set(args)
    # Refusal ``next=`` calls echo the caller's kind; 'paper' is the
    # fallback only when no kind was given (gr536312).
    kind_name = kind if isinstance(kind, str) and kind else "paper"
    # Top-level k/limit/offset are verb-consumed — _VERB_CONSUMED_PARAMS
    # promotes them to the full function, which maps k=/limit= onto
    # page_size= and owns the offset= value check (offset=0 no-ops,
    # offset>0 refuses). Only an args=-nested spelling refuses here: it
    # names no kind arg.
    nested = sorted(set(args) & {"k", "limit", "offset"})
    if nested:
        from precis.errors import BadInput
        from precis.tools.core import _get_runtime, _validation_error

        key = "offset" if "offset" in nested else nested[0]
        if key == "offset":
            message = (
                "'offset' counted items in the removed paging model — "
                "search returns a bounded top-N set, not pages"
            )
            next_call = (
                f"search(kind={kind_name!r}, q='<more specific terms>', page_size=10)"
            )
        else:
            message = (
                f"'{key}' is a search-level spelling of 'page_size' — "
                f"pass page_size={args[key]!r} top-level, not inside args="
            )
            next_call = f"search(kind={kind_name!r}, q='…', page_size={args[key]!r})"
        return _validation_error(
            _get_runtime().render_error(BadInput(message, next=next_call))
        )
    if "kinds" in supplied and kind != "skill":
        from precis.errors import BadInput
        from precis.tools.core import _get_runtime, _validation_error

        return _validation_error(
            _get_runtime().render_error(
                BadInput(
                    "kinds= is only a skill catalogue filter; it does not "
                    "select search kinds",
                    next="search(kind='memory,finding', q='…')",
                )
            )
        )
    if "page" in supplied:
        from precis.errors import BadInput
        from precis.tools.core import _get_runtime, _validation_error

        return _validation_error(
            _get_runtime().render_error(
                BadInput(
                    "search no longer paginates; refine the query or reduce "
                    "page_size instead of requesting another page",
                    next=(
                        f"search(kind={kind_name!r}, "
                        "q='<more specific terms>', page_size=10)"
                    ),
                )
            )
        )
    return None


def _handler_top_level(verb: str, kind: Any) -> frozenset[str] | None:
    """Top-level kwargs the ``kind`` handler's ``verb`` declares, else ``None``.

    ``None`` means "cannot tell" (no kind, unknown/disabled kind, handler
    tolerates extras, runtime not up): the caller then lifts every full-verb
    name as before, so the dispatcher's own refusal text is unchanged.
    """
    if not isinstance(kind, str) or not kind:
        return None
    try:
        from precis.protocol import TOLERATES_EXTRA_KWARGS_ATTR
        from precis.runtime.dispatch import _handler_accepted_kwargs
        from precis.tools.core import _get_runtime

        handler = _get_runtime().hub.handler_for(kind)
        method = getattr(handler, verb, None)
        if handler is None or method is None:
            return None
        if getattr(method, TOLERATES_EXTRA_KWARGS_ATTR, False):
            return None
        cls: Any = type(handler)
        return cast(frozenset[str], _handler_accepted_kwargs(cls, verb))
    except Exception:
        return None


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
        refused = _removed_search_arg_error(verb, kwargs.get("kind"), kwargs, args)
        if refused is not None:
            return refused
        deprecated: list[str] = []
        for key, value in kwargs.items():
            if key in slim_names:
                call[key] = value
            elif key in _VERB_CONSUMED_PARAMS.get(verb, ()):
                # Verb-level alias (search's k=/limit=/offset=): the full
                # function consumes it — never a kind's args= key.
                call[key] = coerce_json_container(
                    value, full_sig.parameters[key].annotation
                )
            else:
                # Undeclared top-level key (legacy call shape): treat it as
                # if it had been passed inside ``args``.
                deprecated.append(key)
                args.setdefault(key, value)
        # Promote args keys that name a full-verb parameter back to that
        # parameter, so the verb function's own validation/forwarding runs.
        # Only lift what the target kind's handler declares top-level; any
        # other key (``source`` on pcb ``op=import_epro``) stays in ``args``.
        accepted_top = _handler_top_level(verb, call.get("kind"))
        for key in list(args):
            if key in full_names and key not in slim_names and key not in _INJECTED:
                if accepted_top is not None and key not in accepted_top:
                    continue
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
