---
status: ready
pillar: memory-graph
---

# Paper search: unique_per='paper' default mode (design resolved, unbuilt)

Make one-row-per-paper (best handle + a `more` count of additional hits +
best-chunk keywords) the default; unique_per='chunk' (today's shape) becomes
the opt-in/drill mode, implicit when `scope=` is set. Ships with mode-aware
page sizes (top_k 25 paper / 10 chunk), a "N papers of M matched (K chunk
hits)" counter, and refine-before-paging guidance in precis-search-help.
Owner `src/precis/handlers/paper.py::PaperHandler.search`.

Prerequisite already on main: capped non-verbatim paper search opts into
`search_chunks_multi(prefer_body=True)`, which drops a paper's card when a
body chunk of the same paper is in the fused pool, before the per-paper cap
and offset/limit; card-only papers survive and exact-title callouts do not
promote rows on a capped page.

Remaining: `unique_per` modes/defaults, mode-aware page sizes, `more`,
best-chunk keywords, honest paper/chunk counters and help guidance.
Verbatim's existing early-return path does not apply the diversity cap;
that separate behavior is outside this fused-search prerequisite.
