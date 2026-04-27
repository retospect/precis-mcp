"""AST-based Python code indexer.

Pure logic, zero deps beyond stdlib. Walks a repo, parses every `.py`
file with `ast`, and produces a queryable in-memory index of modules,
classes, functions, methods, line ranges, signatures, and docstrings.

Used by `precis.handlers.python` (DB-backed, slug-addressed kind) and
also stands alone for unit tests / one-off introspection.

The DB schema in `python_symbols` mirrors the `Symbol` dataclass.
"""

from __future__ import annotations

from precis.python_index.indexer import index_module, index_repo
from precis.python_index.types import (
    CallEdge,
    ModuleIndex,
    RepoIndex,
    Symbol,
    SymbolKind,
)

__all__ = [
    "CallEdge",
    "ModuleIndex",
    "RepoIndex",
    "Symbol",
    "SymbolKind",
    "index_module",
    "index_repo",
]
