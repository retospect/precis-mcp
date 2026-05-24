"""Output format registry — TOON, JSON, ASCII table.

ADR 0002 picked TOON (Token-Oriented Object Notation) for tabular
agent-facing output and JSON for nested / single-record responses.
This package ships the serialisers and the dispatch table that
the CLI's ``--format`` flag — and any in-process caller — uses to
pick between them.

Public surface
--------------

- :data:`SERIALIZERS` — name → callable lookup. Out of the box:
  ``toon``, ``json``, ``table``.
- :func:`serialize` — the one-line dispatch shim. Call this from
  CLI subcommands; never reach into the toon / table modules
  directly so a future format lands as a single registry entry.
- :func:`register` — add a new format at runtime. Tests use it;
  operators can too.

The TOON serialiser is also exported as ``precis.format.toon``
for callers that want to reach for it directly (e.g. tests that
exercise the dump/load roundtrip).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from precis.format import _json, table, toon

# Module-level singleton. Mutating it (via :func:`register`) is the
# supported extension hook; we expose it directly rather than
# hiding behind a getter so callers can ``"toon" in SERIALIZERS``.
SERIALIZERS: dict[str, Callable[..., str]] = {
    "toon": toon.dump,
    "json": _json.render,
    "table": table.render,
}


def serialize(data: Any, *, format: str = "toon", **kwargs: Any) -> str:
    """Render *data* via the named serialiser.

    Parameters
    ----------
    data
        Whatever the chosen serialiser accepts. The TOON and table
        renderers want a list of row mappings (or a single
        mapping); JSON accepts any JSON-compatible value plus the
        :func:`str` fallback configured in
        :mod:`precis.format._json`.
    format
        Registry key. Default ``"toon"`` matches the pipe-default
        the CLI picks via :func:`precis.cli._common.resolve_format`.
    **kwargs
        Forwarded to the serialiser. Common keys:

        * ``sep`` — column delimiter (TOON only; ignored elsewhere).
        * ``schema`` — explicit column order (TOON, table; ignored
          for JSON).

    Raises
    ------
    ValueError
        If *format* is not a registered serialiser.
    """
    try:
        fn = SERIALIZERS[format]
    except KeyError:
        known = ", ".join(sorted(SERIALIZERS))
        raise ValueError(
            f"unknown output format {format!r}; known: {known}"
        ) from None
    return fn(data, **kwargs)


def register(name: str, fn: Callable[..., str]) -> None:
    """Register a new serialiser.

    Overwriting an existing name is allowed — handy for tests that
    swap in a deterministic renderer, and for operators who want
    a custom shape.  The registry is module-level state; callers
    that mutate it should restore the previous value when done.
    """
    SERIALIZERS[name] = fn


__all__ = [
    "SERIALIZERS",
    "register",
    "serialize",
    "table",
    "toon",
]
