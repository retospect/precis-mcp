---
status: draft
title: "corpus-wide duplicate-hub candidate scan (the all-pairs banded read)"
pillar: memory-graph
---

# Corpus-wide candidate scan

Merge itself is settled: applying is a **human door** (the claim page's
`POST /nanopub/fi{hub}/merge`, refuses past `candidate`, `set_by='user'`),
and agents read the dry-run plan with `get(kind='finding', id='fi<winner>',
view='merge-plan', args={'loser': ...})`. The per-hub reader-side need —
"what is near *this* hub?" — is covered by `view='similar'`
(`nearest_hubs`, the same ANN the mint dedup cascade runs), which now points
at `view='merge-plan'`.

What is left is the corpus-wide half the per-hub view does not cover: an
**all-pairs cosine scan over `finding_body` embeddings, banded**, so a dedup
sweep is discoverable without hand-written SQL or walking hubs one at a time.
Read-only, so it carries none of merge's risk. Low priority: `view='similar'`
per hub already serves the reader, and the sweep is a human-paced loop.

When built, apply the strict hub predicate (`claim_hub_predicate_sql`) or it
will surface chase-tree findings as merge candidates. Expect the worst
precision in the numeric-near-miss and narrower-vs-broader bands (two of the
nine pairs in the first real cohort were do-not-merge), so band the output
rather than ranking it flat.
