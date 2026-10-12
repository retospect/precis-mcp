---
status: ready
pillar: memory-graph
title: context_sentence re-clear the no-abstract cohort on prod
prio: normal
---

# context_sentence: re-clear failed stamps on prod (ops)

The code part shipped: the context block in
`precis.workers.context_sentence` keeps `card_abstract` as first choice,
then `refs.meta['abstract']`, then the top-k same-paper body passages
from the in-paper fused lexical+embedding search
(`retrieve_body_block` over `search_chunks_multi` scoped to the ref —
the store arm behind `scope='pa<id>'`), hearsay section roles excluded,
k ≤ 4 passages / ≤ 1,500 chars, `usable_context` guarding the assembled
block, per-ref chunk-id logging, and the embedder wired lazily in
`cli/_worker_passes.py` + the `precis_web.nanopub_render` lazy enqueue.

What remains is prod ops, once the code is deployed:

- Refs 498, 3234 and 42556 carry failed stamps from before the guard;
  re-clear them (remove the failed-sentence stamp so the pass revisits
  them) together with the no-abstract cohort — the
  `context_sentence_no_abstract` stamp is non-terminal by design and was
  meant to be revived for exactly this.
- Prod measurement after deploy (the blast-radius check): of the 788
  no-abstract grounding-source papers, the share that now gets a
  sentence, and a 20-paper hand-read of sentences against the retrieved
  passages — every sentence must be verifiable from its own input, the
  property gr346458 is about.
- The four mastheads of gr346458 (refs 563, 42555, 42557, 42558) should
  produce a sentence from body prose, or take the
  `context_sentence_no_abstract` path if their bodies hold no method
  prose — never a byline-derived sentence, never
  `context_sentence_failed` from a masthead input.
