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
- :func:`render_agent_table` — the **handler-facing** entry point.
  MCP handlers (skill index, TOC, search hits, list views) call
  this for every tabular response so we have a single swap point
  for benchmarking alternative renderers. Reads
  ``PRECIS_AGENT_TABLE_FORMAT`` (default ``"toon"``) to pick the
  backend — flip the env var and every MCP-facing list switches
  format in one go.
- :func:`register` — add a new format at runtime. Tests use it;
  operators can too.

The TOON serialiser is also exported as ``precis.format.toon``
for callers that want to reach for it directly (e.g. tests that
exercise the dump/load roundtrip).
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping, Sequence
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

# Env var picking the agent-facing tabular renderer. Defaults to TOON
# (the only LLM-tuned option today). Setting it to ``json`` or
# ``table`` swaps every MCP-facing list to that format in one go —
# useful for A/B benchmarking token cost or human readability.
_AGENT_TABLE_ENV = "PRECIS_AGENT_TABLE_FORMAT"
_AGENT_TABLE_DEFAULT = "toon"


def render_agent_table(
    rows: Sequence[Mapping[str, Any]] | Mapping[str, Any],
    *,
    schema: Sequence[str] | None = None,
    format: str | None = None,
) -> str:
    """Render a tabular result for an agent-facing response.

    Single chokepoint that MCP handlers call instead of reaching for
    ``toon.dump`` (or any other serialiser) directly. This lets the
    operator swap renderers at runtime via ``PRECIS_AGENT_TABLE_FORMAT``
    without changing handler code — useful for benchmarking
    alternative tabular shapes against the same agent under load.

    Args:
        rows: Either a list of row mappings or a single mapping
            (treated as a 1-row table). Matches ``toon.dump``'s
            contract so existing callsites port cleanly.
        schema: Optional explicit column order. Recommended for stable
            output across reruns.
        format: Override the env-selected backend for this call. If
            unset, reads ``PRECIS_AGENT_TABLE_FORMAT`` (defaulting to
            ``"toon"``).

    Returns:
        The rendered table body. Whatever the chosen backend emits.

    Raises:
        ValueError: If the resolved format isn't in :data:`SERIALIZERS`.
    """
    chosen = format or os.environ.get(_AGENT_TABLE_ENV, _AGENT_TABLE_DEFAULT)
    kwargs: dict[str, Any] = {}
    # JSON ignores ``schema``; TOON + table both honour it. Pass it
    # only when the backend declares the kwarg, to keep extension
    # serialisers free to omit it.
    if schema is not None and chosen in ("toon", "table"):
        kwargs["schema"] = list(schema)
    return serialize(rows, format=chosen, **kwargs)


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
        raise ValueError(f"unknown output format {format!r}; known: {known}") from None
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
    "render_agent_table",
    "serialize",
    "table",
    "toon",
]
