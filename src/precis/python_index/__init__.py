"""AST-based Python code indexer.

Pure logic, zero deps beyond stdlib. Walks a repo, parses every `.py`
file with `ast`, and produces a queryable in-memory index of modules,
classes, functions, methods, line ranges, signatures, and docstrings.

Used by `precis.handlers.python` (slug-addressed kind) and also stands
alone for unit tests / one-off introspection.

Symbols: modules, classes, functions, methods and nested functions
(``outer.inner``, ``Class.meth.helper``; a repeated nested name under one
parent becomes ``name#2``, ``name#3`` in source order). Call edges resolve
through function-local imports and nested functions, and calls inside
lambdas are credited to the enclosing function; function-body imports are
recorded per function (``ModuleIndex.local_imports``). Not indexed: classes
defined inside functions, ``exec``, ``importlib.import_module``.

Deliberately **not** persisted to Postgres — AST parsing is cheap,
idempotent, and the source-of-truth already lives on disk. An
in-memory `RepoCache` re-stats the tree and reparses only files whose
size/mtime/ctime/device/inode changed. Reuse is stat-checked, with content
explicitly not revalidated: scanning every source byte would make navigation
reads scale with corpus size. A before/after identity check retries parsing
once, then omits unstable files rather than stamping an old module as fresh.
Walk observations are non-atomic; failures and retained entries are labelled.

Each module keeps decoded source and an exact raw-byte digest from one read,
separate from its historical newline-normalized ``sha256``. Source rendering
uses that text so line coordinates and fingerprints refer to the same indexed
bytes even if disk changes later. The root fingerprint frames sorted file
paths and byte hashes, covering the represented Python corpus only. It is
neither a Git tree hash nor a guarantee of an atomic checkout snapshot.
Git identity and scoped dirty status are separate time-bounded observations,
never a claim that dirty/untracked indexed bytes equal HEAD. Root authorization
stays in handler construction/``PRECIS_PYTHON_ROOTS``; ``expected_root`` asserts
an explicit alias's identity before indexing and grants no new access.
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
