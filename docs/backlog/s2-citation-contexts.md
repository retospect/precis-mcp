---
status: idea
pillar: memory-graph
---

# Semantic Scholar citation contexts to pre-match passages (gr513768 second pass)

Idea, 2026-10-24. Semantic Scholar's citation data carries, per citing ->
cited edge, the snippet around the in-text citation ("contexts") and often
an intent label. For gr513768 (resolve `[32]` / Miller 1921 in a paper chunk
to the cited paper and passage), a second pass can match those snippets
against our own chunk text: a context that appears verbatim in a held
paper's chunk pins that chunk to the cited paper without any LLM
resolution, and the cited paper's matching passage can be searched with the
same snippet as the query.

Why: the first pass (bibliography entry -> paper) is deterministic from
`cites` edges (see `openalex-snapshot-cites.md`); contexts would cover the
chunk-level half that is otherwise LLM work. S2 data is already reachable
(`s2_neighbors`, `0106_s2_neighbors.sql`; Graph API through `safe_get` with
the existing outbound row). Licence of the contexts data (S2 API terms vs the
open S2ORC/citation datasets) needs checking before storing any snippet text.

Owner anchor: gr513768 resolver skill/jobs; `src/precis/workers/bib_parse.py`.
test: a held paper with known S2 contexts resolves >= 80% of marker chunks to
the right cited paper with zero LLM calls on a hand-checked sample of 20.
