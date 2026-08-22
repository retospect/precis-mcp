"""In-memory, DB-free markdown index for the `md` kind.

Pure logic, no Postgres. Walks one or more roots configured via
`PRECIS_MD_ROOTS` (`alias1:/abs/path1,alias2:/abs/path2`, parsed by
`precis.handlers._roots.parse_alias_roots`, wired in
`precis.dispatch.boot`), splits every `.md`/`.markdown` file into
blocks with `precis.utils.md_parse`, and produces a queryable
in-memory index with heading breadcrumbs and lexical search — the
`python` kind's pattern (`precis.python_index`) applied to prose
instead of code.

Deliberately **not** persisted to Postgres, and deliberately a
separate kind from the DB-backed `markdown` kind: block-splitting is
cheap, idempotent, and the source of truth already lives on disk, so
there is nothing an ingest/embedding-worker/migration pipeline would
buy here — a repo's own docs/backlog/skills should be searchable
without first being ingested into a database. `MdRepoCache` re-stats
the tree and reparses only files whose mtime changed — see its
docstring for the invalidation contract.

Semantic search layers on top without changing any of the above:
`MdVectorCache` persists one embedding per block under the host's XDG
cache dir (`precis.config.cache_root('md-vectors')`, one npz+json pair
per embedder model/dim), content-addressed by `MdBlockEntry.sha256` so
it survives file moves and dedupes identical prose across roots;
`search.cosine_search` ranks blocks that already have a cached vector;
`search.fuse_blocks` reciprocal-rank-fuses that ranking with
`search_blocks`'s lexical one. A background warm pass
(`precis.server._warm_md_index_background`) fills `MdVectorCache` at
server boot without blocking it or any request; `MdHandler.search`
degrades to lexical-only for any block the warm pass hasn't reached
yet. Still no Postgres.

The `md` kind (`precis.handlers.md.MdHandler`) is the read-only verb
surface over this module; `precis-md-help` (skill) and the `md` row in
`precis-overview` document it for agents.
"""

from __future__ import annotations

from precis.md_index.cache import MdRepoCache
from precis.md_index.indexer import index_file, index_repo
from precis.md_index.search import (
    cosine_search,
    fuse_blocks,
    score_block,
    search_blocks,
)
from precis.md_index.types import MdBlockEntry, MdFileEntry, MdRepoIndex
from precis.md_index.vectors import MdEmbedder, MdVectorCache

__all__ = [
    "MdBlockEntry",
    "MdEmbedder",
    "MdFileEntry",
    "MdRepoCache",
    "MdRepoIndex",
    "MdVectorCache",
    "cosine_search",
    "fuse_blocks",
    "index_file",
    "index_repo",
    "score_block",
    "search_blocks",
]
