---
status: ready
title: context_sentence reads the paper body (in-paper retrieval), not "the abstract or the first chunk"
prio: normal
---

# context_sentence reads the paper body (in-paper retrieval), not "the abstract or the first chunk"

## Motivation / why
`precis.workers.context_sentence._claim` feeds the model
`COALESCE(card_abstract chunk, first body chunk, '')` as the paper's
context block. On prod only 53 of the 841 grounding-source papers have a
`card_abstract` chunk; the other 788 fall through to the first body chunk,
which is the masthead (title + byline + affiliations) or a neighbouring
article's end matter. 7b979887 (gr346458 asks a+b) made the pass refuse
such blocks (`usable_context`, non-terminal `context_sentence_no_abstract`
stamp), so those papers now converge to *no* sentence — honest, but the
feature is dark for ~90% of its cohort. Crossref/S2 abstract enrichment
(`docs/backlog/abstract-fallback-crossref-s2.md`) does not close it:
`refs.meta->>'abstract'` exists on 59 of the 841.

Reto, 2026-09-18 (gr346458 ask c): a synthetic abstract is possible, but
the right input is the paper itself — look inside, read it, retrieve the
passages that say how the work was done. The corpus already holds every
body chunk with an embedding; the input defect is that the pass reads
position (ord 0) instead of content.

## In scope
- Replace the context block's *source*, not the pass's contract. The
  block becomes the top-k body passages of the SAME paper returned by the
  in-paper fused lexical+embedding search (the `scope='pa<id>'` arm of
  `search(kind='paper')`, `handlers/paper.py`), for a small fixed set of
  method-seeking queries ("we calculate / DFT / first-principles",
  "we synthesized / measured / characterized", "experimental setup /
  methods", "simulation details"). Title stays authoritative in the
  prompt exactly as today.
- Keep `card_abstract` as the first choice when it exists; retrieval is
  the fallback that replaces "first body chunk". Keep `usable_context`
  as the guard on the *assembled* block (title + retrieved passages), so
  a paper whose retrieval returns only mastheads/references still takes
  the no-abstract path rather than a model call.
- Exclude chunks whose section role is references / related work /
  acknowledgements (the same hearsay roles the nanopub primary-source
  gate rejects), so a bibliography never becomes "the method".
- Bound the block: k ≤ 4 passages, total ≤ ~1,500 chars, deterministic
  order (score desc, then ord) so the same paper yields the same input
  across sweeps.
- Log, per ref, which chunk ids fed the block (the pass already logs
  every decline/rejection per gr346458 ask b; extend that line).
- Clear the non-terminal `context_sentence_no_abstract` stamp on the
  cohort once the new input lands, so the next sweep revisits those
  papers (the stamp was designed to be revivable for exactly this).

## Explicitly NOT in scope
- No synthetic `card_abstract` chunk is written. A synthesized abstract
  would be a new `ord < 0` card variant via a registered synthesis pass
  and would also feed search/cards — a separate decision with its own
  blast radius. This item feeds the model directly from retrieval and
  stores nothing new in `chunks`.
- No change to the sentence contract (one neutral method/evidence-type
  sentence, blocklist, 35-word cap, NO_CONTEXT hatch) or to the
  population rules (grounding-source cohort only; no corpus-wide sweep).
- No change to how the approved grounding payload freezes the sentence
  (`precis.nanopub.mint.approve`), and no re-approval of already-signed
  hubs — an anchored artifact keeps the sentence it froze.
- Not the Crossref/S2 abstract fallback; that stays its own idea.

## Acceptance criteria
- For a paper with no `card_abstract`, the block passed to the model
  contains ≥1 body passage from a non-hearsay section role and ≠ the
  first body chunk unless retrieval ranked it first.
- The four mastheads of gr346458 (refs 563, 42555, 42557, 42558) produce
  a sentence from body prose, or take the `context_sentence_no_abstract`
  path if their bodies hold no method prose — never a byline-derived
  sentence and never `context_sentence_failed` from a masthead input.
- Ref 207807 (has a real abstract) is unchanged: `card_abstract` still
  wins, same block as today.
- A paper whose retrieval returns only references/masthead still scores
  below `usable_context`'s thresholds and is stamped no-abstract with no
  model call (test with a fixture paper whose only chunks are a byline
  and a bibliography).
- Per-ref log line names the chunk ids that fed the block.
- Prod measurement after deploy (the blast-radius check): of the 788
  no-abstract papers, the share that now gets a sentence, and a 20-paper
  hand-read of sentences against the retrieved passages — every sentence
  must be verifiable from its own input, the property gr346458 is about.

## Target + blast radius
`src/precis/workers/context_sentence.py` (`_claim` SQL, block assembly,
`usable_context` call site, per-ref log), the in-paper search arm of
`src/precis/handlers/paper.py` (read-only reuse; if it is not callable
without the MCP frame, factor the query into a store/search helper both
share). Tests: `tests/workers/test_context_sentence.py`. Consumers
(`nanopub.mint.approve` freeze, `nanopub.assemble` emit, the
`precis_web.nanopub_render` lazy enqueue) are untouched. Model cost:
one call per revived paper, once — the same budget the pass already had
before the guard, now spent on real input.

## Open questions / decisions log
- 2026-09-18, Reto: retrieval over the body, not a synthetic abstract, is
  the primary fix; synthetic abstract noted as possible, deferred.
- Query set and k are implementation choices; calibrate on the 841-paper
  cohort the same way `usable_context`'s thresholds were (prod read-only
  measurement before the model is called on anything).
