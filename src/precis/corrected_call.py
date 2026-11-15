"""The in-flight-call contextvar + re-render helpers behind a refusal's
concrete ``next=`` (gr613469).

:func:`precis.errors.render_corrected_call` builds a corrected call
from an explicit arg dict; the helpers here re-render *the caller's own
call* — recorded by ``runtime.dispatch._invoke_handler`` into
:data:`IN_FLIGHT_CALL` around ``method(**clean)`` — with only the bad
key/view swapped. A corrected call that dropped the caller's filters
(todo search's ``tags``/``args``, alert's ``host``/``minutes``,
checklist's ``target``) would aim the retry at a different question
(review td680653), and one naming a non-wire-declared key top-level
would silently drop it on the wire — extras fold into ``args={…}``
(:func:`verb_top_level`).

Import-pure at module level (errors + stdlib only — the precis_geom
carve-out's "never reaches store" contract counts function-level edges
too); ``verb_top_level`` reaches ``dispatch.CORE_PARAMS`` and
``tools.TOOL_REGISTRY`` lazily, at raise time, when both are loaded.
"""

from __future__ import annotations

import contextvars
import inspect
from collections.abc import Mapping
from typing import Any

from precis.errors import render_corrected_call

#: ``(verb, kind, kwargs)`` of the dispatch call in flight — set by
#: ``runtime.dispatch._invoke_handler`` around ``method(**clean)`` so a
#: refusal raised deep inside a handler can re-render the *caller's own
#: call*, not a skeleton. ``None`` outside dispatch; :func:`retry_call`
#: then falls back to the args its caller passes.
IN_FLIGHT_CALL: contextvars.ContextVar[tuple[str, str, dict[str, Any]] | None] = (
    contextvars.ContextVar("precis_in_flight_call", default=None)
)

#: Verb-wrapper defaults that land in the dispatch kwargs on every call
#: whether or not the caller typed them
#: (``dispatch._WRAPPER_DEFAULTED_KWARGS``): ``search(page=1)``,
#: ``edit(mode='find-replace', verdict='approved')``, ``link(mode='add')``.
#: A re-rendered retry drops them — not caller intent (the rule
#: ``runtime.search._PAGE_TOLERATED`` already applies).
_WRAPPER_DEFAULT_VALUES: dict[str, dict[str, tuple[Any, ...]]] = {
    "search": {"page": (1,)},
    "edit": {"mode": ("find-replace",), "verdict": ("approved",)},
    "link": {"mode": ("add",)},
}


def drop_wrapper_defaults(verb: str, args: Mapping[str, Any]) -> dict[str, Any]:
    """``args`` minus :data:`_WRAPPER_DEFAULT_VALUES` — the kwargs the
    verb's wire wrapper injects whether or not the caller typed them.
    A re-rendered retry drops them so the hint names caller intent."""
    tolerated = _WRAPPER_DEFAULT_VALUES.get(verb, {})
    return {k: v for k, v in args.items() if v not in tolerated.get(k, ())}


def verb_top_level(verb: str) -> tuple[str, ...] | None:
    """The params ``verb``'s wire schema declares top-level — the slim
    :data:`~precis.runtime.dispatch.CORE_PARAMS` on slim verbs
    (``put``/``edit``/``search``), the full verb's own signature on the
    rest (``get`` takes ``args=`` for everything else — a flattened
    extras key rendered top-level would be silently dropped by the
    wire schema, the exact bug ``args={…}`` exists to prevent).
    ``None`` when the signature is unknowable —
    :func:`~precis.errors.render_corrected_call` renders every key
    top-level then.
    """
    from precis.runtime.dispatch import CORE_PARAMS

    if verb in CORE_PARAMS:
        return CORE_PARAMS[verb]
    try:
        from precis.tools import TOOL_REGISTRY

        func = TOOL_REGISTRY.get(verb, {}).get("func")
        if func is None:
            return None
        return tuple(
            name for name in inspect.signature(func).parameters if name != "ctx"
        )
    except Exception:
        return None


def retry_call(verb: str | None = None, **changes: Any) -> str | None:
    """The in-flight call re-rendered with ``changes`` applied — the
    corrected-call ``next=`` (gr613469).

    Every caller arg rides the retry — a refusal that re-aims the
    caller at a skeleton drops their filters; only the bad key/view
    changes. Keys the wire schema doesn't declare fold into
    ``args={…}`` (:func:`verb_top_level` + ``render_corrected_call``'s
    ``top_level`` rule); wrapper defaults the caller never typed are
    dropped. Outside a recorded dispatch the render falls back to the
    ``kind=``/kwargs the site passed — ``None`` when it passed no
    ``verb`` either.
    """
    call_verb: str
    kind: str | None
    call_args: dict[str, Any]
    ctx = IN_FLIGHT_CALL.get()
    if ctx is not None:
        call_verb, kind, call_args = ctx
    elif verb is not None:
        call_verb, kind, call_args = verb, changes.get("kind"), {}
    else:
        return None
    merged: dict[str, Any] = {"kind": kind} if kind is not None else {}
    merged.update(drop_wrapper_defaults(call_verb, call_args))
    merged.update(changes)
    return render_corrected_call(call_verb, merged, top_level=verb_top_level(call_verb))
