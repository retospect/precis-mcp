"""Precis CLI — ``precis serve | migrate | jobs ...``.

The CLI used to live in ``src/precis/cli.py``; it now lives as a
package so each subcommand implementation has its own module. This
top-level ``__init__`` re-exports the symbols external code
(console-script entry point, tests, help skills) still imports:

- :func:`main` — the console-script ``precis`` entry point.
- :func:`_build_parser` — argparse construction (used by CLI tests).
- :func:`_parse_interval` — ``--every`` spec parser (used by the
  patent-watch CLI tests).

Everything else lives in submodules under :mod:`precis.cli`; nothing
consumed externally by ``from precis.cli import X`` should break.
``_parse_interval`` resolves on first access, so importing the console
script does not import :mod:`precis.cli.patent`.

Memory file coexistence has an explicit ``memory mirror`` namespace. Import
locks refs and compares saved body/metadata/link state; old cutover sync cannot
offer that contract because it retires absent files and drops YAML. Export
creates a fresh directory, preserving header bytes rather than normalizing
user formatting. Files and graph remain snapshots with explicit conflicts,
not a background bidirectional synchronizer.
"""

from __future__ import annotations

from typing import Any

from precis.cli.main import _build_parser, main


def __getattr__(name: str) -> Any:
    if name == "_parse_interval":
        from precis.cli.patent import _parse_interval

        return _parse_interval
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "_build_parser",
    "main",
]
