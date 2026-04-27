"""AST-based Python code indexer.

Pure logic, zero deps beyond stdlib. Walks a repo, parses every `.py`
file with `ast`, and produces a queryable in-memory index of modules,
classes, functions, methods, line ranges, signatures, and docstrings.

Used by `precis.handlers.python` (slug-addressed kind) and also stands
alone for unit tests / one-off introspection.

Deliberately **not** persisted to Postgres — AST parsing is cheap,
idempotent, and the source-of-truth already lives on disk. An
in-memory `RepoCache` re-stats the tree and reparses only files whose
mtime changed. See `RepoCache` for the invalidation contract.
"""

from __future__ import annotations

from precis.python_index.cache import RepoCache
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
    "RepoCache",
    "RepoIndex",
    "Symbol",
    "SymbolKind",
    "index_module",
    "index_repo",
]
