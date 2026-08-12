# Diff-aware review state — carry review/finding assertions across fork + edit, auto-stale/auto-resolve by content_sha

Plain idea (needs design before it's `ready`).

Two related pains from the nanobuds pre-submission pass, same root cause —
review assertions are not reconciled against current chunk text:

1. **Fork wipes the review ledger.** `put(copy_of=…)` deep-copies chunks but
   the `chunk_review` ledger starts empty (documented, deliberate), so a fork
   made purely to run a review pass shows 0/174 reviewed even where the text is
   byte-identical to the already-reviewed source. Re-reviewing unchanged chunks
   is wasted work. Idea: on fork, optionally carry the source ledger forward but
   mark each entry stale-if-`content_sha`-changed — so only chunks that actually
   diverge from the reviewed original show as unreviewed.

2. **Anchored concern-findings don't self-resolve.** ~20 durable `finding` refs
   (`raises-concern-about`, anchored to `dc<id>`) sat linked to the draft; an
   agent had to read each against the current prose to sort STILL-LIVE vs
   ALREADY-FIXED vs NOISE. Most findings quote the offending span — so a
   deterministic check ("is the flagged substring still present in the current
   chunk?") could auto-mark the obviously-resolved ones and shrink the set an
   agent must judge.

Common primitive: compare a stored review/finding assertion (with the
`content_sha` / quoted span it was made against) to the chunk's current text,
and drive staleness/auto-resolution from that diff. A `view` that lists open
anchored findings with a live/stale/gone verdict per finding would make the
citation-integrity layer as glanceable as `view='review'` is for the human
ledger.

Owner anchor: `chunk_review` + finding-link handling around
`src/precis/handlers/draft.py`; fork path in the same handler's `copy_of`
branch. test: forked draft with one edited chunk shows 1 unreviewed, not all;
a finding whose quoted span was deleted reports `gone`.
