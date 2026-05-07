"""Embedded index over a directory of files.

Used by :class:`SkillHandler` to give ``search(kind='skill', q=...)``
genuine semantic recall instead of substring-only matching. The
underlying machinery (chunker, sha256-keyed disk cache, in-memory
cosine search) is **kind-agnostic** so the same class can later
power discovery over ``grimoire/`` prompts, curated wisdom corpora,
or any other ships-with-the-wheel markdown the agent should be able
to reach by topic.

Three pieces:

- :mod:`precis.skill_index.chunker` — split a markdown file into
  one chunk per H2 section (head + body), preserving the heading
  for display.
- :mod:`precis.skill_index.cache` — read/write per-slug embedding
  cache files keyed by ``(cache_namespace, embedder_model,
  chunker_version, file_sha256)``. JSON on disk so the cache is
  self-describing and ``numpy`` stays an optional dep.
- :mod:`precis.skill_index.index` — the public
  :class:`FileCorpusIndex` that ties them together.

Design notes:

- **In-memory + disk cache, not Postgres.** The skill corpus ships
  in the wheel; every host derives its own copy from identical
  inputs. There's no shared write surface to coordinate, so the
  cross-host version-gating that ``oracle`` needs is overkill here.
  Bumping the wheel changes file sha256s → automatic re-embed.
- **Lazy build.** The index doesn't touch the embedder until
  :meth:`FileCorpusIndex.search` is first called. Cold start stays
  fast; the first search pays the embedder load + any uncached
  chunks; subsequent searches hit the in-memory matrix.
- **Lexical fallback.** When no embedder is wired (e.g. a build
  without the ``[paper]`` extra), the index advertises
  ``available=False`` and the caller drops back to its existing
  substring search. No silent quality loss.
"""

from precis.skill_index.chunker import Chunk, chunk_by_h2
from precis.skill_index.index import FileCorpusIndex, SearchHit

__all__ = [
    "Chunk",
    "FileCorpusIndex",
    "SearchHit",
    "chunk_by_h2",
]
