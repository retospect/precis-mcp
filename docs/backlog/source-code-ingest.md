---
status: idea
title: Source-code ingest — repos as searchable corpus
---

# Source-code ingest — repos as searchable corpus

From gripe 171844 (closed into this item). Precis can search papers via the
ingest pipeline but has no equivalent for software repositories (starting
with precis-mcp itself), blocking dogfooding of code search on the product
side. Needs a code-aware ingest adapter: walk a repo, chunk by
function/class/logical block rather than prose sections, land refs+chunks so
search/embeddings/discovery work over the codebase.

**Design pass required before any build** — nothing here is scoped:

- **Chunking granularity** — function/class/module? What does a "section"
  mean for code, and what goes in the chunk header (qualified name, path,
  signature)?
- **Language-awareness** — AST-based (tree-sitter?) vs indentation
  heuristics; which languages first (Python only is fine for dogfood).
- **Re-ingest strategy** — body chunks are append-only
  (`docs/codebase.md` invariant); code churns far faster than papers, so
  naive re-ingest strands embeddings/summaries or floods the cascade.
  Likely needs content-hash diffing at chunk level: only DELETE+INSERT
  changed defs.
- **Kind question** — new `kind='code'` (or `repo`) vs an ingest variant on
  an existing ref shape; how a repo-ref relates to its file/def chunks.
- **Overlap check** — repo-dev already has claude-context/Milvus code
  search; this item is the *product* surface (cluster agents searching code
  like they search papers), not a dev aid. State what the product surface
  adds before building.

Big feature; needs an ADR-weight design doc, not a batch fix.
