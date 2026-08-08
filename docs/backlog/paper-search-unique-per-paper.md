# Paper search: unique_per='paper' default mode (design resolved, unbuilt)

Make one-row-per-paper (best handle + a `more` count of additional hits +
best-chunk keywords) the default; unique_per='chunk' (today's shape) becomes
the opt-in/drill mode, implicit when `scope=` is set. Ships with mode-aware
page sizes (top_k 25 paper / 10 chunk), a "N papers of M matched (K chunk
hits)" counter, and refine-before-paging guidance in precis-search-help.
Known edge from review: with per_paper=1 a card_combined chunk can consume a
paper's only slot before body-chunk dedup runs. Owner
`src/precis/handlers/paper.py::PaperHandler.search`.
