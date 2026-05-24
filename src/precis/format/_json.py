"""JSON serialiser for the format registry.

A thin wrapper around :func:`json.dumps` so JSON is reachable from
the same `precis.format.serialize` shim as TOON and the ASCII
table. Defaults are chosen to round-trip cleanly through MCP
transports and to read decently when an operator pipes the output
through ``less``.
"""

from __future__ import annotations

import json
from typing import Any


def render(data: Any, **kwargs: Any) -> str:
    """Render *data* as indented JSON.

    The ``default=str`` fallback catches everything :func:`json.dumps`
    rejects out of the box (``Path``, ``datetime``, dataclasses
    that don't subclass ``BaseModel``). It mirrors the lenient
    coercion the TOON serialiser does, so a value that renders one
    way in TOON renders compatibly here.

    Extra ``**kwargs`` are forwarded to :func:`json.dumps` so
    callers can override ``indent``, ``sort_keys``, etc. without
    bypassing the registry.
    """
    options: dict[str, Any] = {
        "default": str,
        "ensure_ascii": False,
        "indent": 2,
        "sort_keys": False,
    }
    # `sep` is meaningful to TOON but not JSON; the registry shim
    # forwards every kwarg, so swallow it here rather than failing.
    kwargs.pop("sep", None)
    # `schema` is a column-order hint for tabular formats; JSON
    # preserves dict order natively, so just ignore it.
    kwargs.pop("schema", None)
    options.update(kwargs)
    return json.dumps(data, **options)


__all__ = ["render"]
