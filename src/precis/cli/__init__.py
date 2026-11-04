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

``garden restore`` is the operator-only limited restoration of superseded
papers: raw loser lookup and human/operator trust share the write transaction.
It is deliberately absent from MCP/web. Garden merges retain their reversal
record and require ``garden undo`` to recover the original links.

Memory file coexistence has an explicit ``memory mirror`` namespace. Import
locks refs and compares saved body/metadata/link state; old cutover sync cannot
offer that contract because it retires absent files and drops YAML. Export
creates a fresh directory, preserving header bytes rather than normalizing
user formatting. Files and graph remain snapshots with explicit conflicts,
not a background bidirectional synchronizer.
Mirror YAML keeps timestamp values as literal strings so JSON metadata retains
their spelling; only its loader changes, leaving ordinary SafeLoader semantics
and non-string key refusal intact. Raw headers remain the export authority.

The session-start ``memory index --export-dir`` snapshot also carries the
live descendants of glossary taxon tn559460 and their ``pointer``
pointers. ``scripts/memory-lint`` checks those cached pointers against its
own checkout without database access; an absent snapshot is reported as
skipped. This keeps hygiene runs offline while replacing the retired
glossary file's path check. Dotted module and sibling-repo pointers remain
unresolvable findings rather than guessed paths.

``memory recall-set accept`` is a checkout-only operator door: trace proposals
are read from parked review todos, but acceptance only edits the tracked
six-field JSON benchmark and prints its diff. No graph write, commit or push
is hidden in the command; query text can be private, so the operator reviews
the diff before landing it in this public repo.
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
