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
Known edge from review: with per_paper=1 a card_combined chunk can consume a
paper's only slot before body-chunk dedup runs. Owner
`src/precis/handlers/paper.py::PaperHandler.search`.

## R14 first slice: body preference before the diversity cap

Verified open on origin/main `ab90f225a`: `PaperHandler.search` has no
`unique_per` argument; `search_chunks_multi` caps fused candidates before
the handler removes redundant cards. Fix this prerequisite only.

Acceptance: in non-verbatim broad paper search with `per_paper=1`, if a
paper's card outranks its matching body chunk in the retrieved candidate
pool, return the body chunk. Keep card-only matches and fill/paginate the
result after this filtering. Larger caps retain the best body chunks.

Implement an opt-in store `prefer_body` flag, enabled by the capped paper
handler; filter redundant cards before the cap and offset/limit. Other
store callers retain their existing behavior. Test deterministic ranked
candidates plus the public handler with lexical test-DB fixtures. No
models, production writes, schema changes, or changes to search defaults.

Remaining: `unique_per` modes/defaults, mode-aware page sizes, `more`,
best-chunk keywords, honest paper/chunk counters and help guidance.
Verbatim's existing early-return path does not apply the diversity cap;
that separate behavior is outside this fused-search prerequisite.
