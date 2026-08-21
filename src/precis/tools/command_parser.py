"""Parse a `precis(command)`-style call string into (verb, kwargs).

The frontier MCP profile (``PRECIS_MCP_PROFILE=command``) and the
``precis eval`` CLI both accept the same one-string call syntax the
docs already teach — ``get(kind='skill', id='toc')`` — instead of the
typed per-verb tool schemas. This module is the sole parser both
consumers share.

Deliberately narrow: ``ast.parse(..., mode='eval')`` must yield a
single ``Call`` node whose ``func`` is a bare name naming one of the
registered verbs, with keyword-only arguments whose values are
``ast.literal_eval``-safe literals (str/int/float/bool/None/list/
dict/tuple/set). No positional args, no ``**kwargs`` splat, no
attribute/subscript access, no nested calls, no arbitrary
expressions — every rejection maps to a short, actionable message so
a model can self-correct on the next turn.

The verb set is read from :data:`precis.tools.TOOL_REGISTRY` — the
same registry ``core.py``'s functions populate and the MCP server /
CLI / repl already treat as the single source of truth — never
hard-coded here.
"""

from __future__ import annotations

import ast
from typing import Any

_EXAMPLE = "get(kind='skill', id='toc')"


class CommandParseError(ValueError):
    """``command`` isn't a single ``verb(kw=literal, ...)`` call."""


def parse_command(command: str, text: str | None = None) -> tuple[str, dict[str, Any]]:
    """Parse ``command`` into ``(verb, kwargs)``, merging ``text=`` in.

    ``text``, when given, becomes ``kwargs['text']`` — the escape
    hatch for large bodies that would otherwise need quote-escaping
    inside ``command``. Raises :class:`CommandParseError` if
    ``command`` also names ``text=`` (ambiguous which one wins).
    """
    from precis.tools import TOOL_REGISTRY

    try:
        tree = ast.parse(command, mode="eval")
    except SyntaxError as e:
        raise CommandParseError(
            f"command must be a single call like {_EXAMPLE} ({e})"
        ) from e

    call = tree.body
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Name):
        raise CommandParseError(f"command must be a single call like {_EXAMPLE}")

    verb = call.func.id
    if verb not in TOOL_REGISTRY:
        raise CommandParseError(
            f"{verb!r} is not a registered verb; expected one of "
            f"{sorted(TOOL_REGISTRY)}"
        )
    if call.args:
        raise CommandParseError(
            f"{verb}(...) takes keyword arguments only, e.g. {_EXAMPLE} "
            "— no positional args"
        )

    kwargs: dict[str, Any] = {}
    for kw in call.keywords:
        if kw.arg is None:
            raise CommandParseError(f"{verb}(...) doesn't accept a **kwargs splat")
        # ast.parse (unlike compile) tolerates repeated keywords —
        # last-wins here would silently drop a value the model meant.
        if kw.arg in kwargs:
            raise CommandParseError(
                f"{verb}({kw.arg}=...) given more than once — pass each "
                "keyword a single time"
            )
        try:
            kwargs[kw.arg] = ast.literal_eval(kw.value)
        except (ValueError, SyntaxError, TypeError) as e:
            raise CommandParseError(
                f"{verb}({kw.arg}=...) must be a literal (string/number/bool/"
                f"None/list/dict/tuple), not an expression ({e})"
            ) from e

    if text is not None:
        if "text" in kwargs:
            raise CommandParseError(
                "text= given both as the text= parameter and inside command "
                "— pass it once"
            )
        kwargs["text"] = text

    return verb, kwargs
