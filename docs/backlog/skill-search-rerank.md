---
status: idea
title: skill-search identity-embedding rerank (gr496170 slice 2)
pillar: memory-graph
---

# Skill-search rerank, slice 2 — identity-embedding re-score

gr496170. Slice 1 (the deterministic gap cut in `SkillHandler.search`)
shipped: the rendered menu trims at the first row scoring below
`_GAP_CUT_RATIO` × the top score, on the default page of non-verbatim
searches whose top hit isn't title-pinned. It deliberately never cuts a
page carrying a title pin — the skill-eval probe corpus showed the tail
below a pin still carries the named answer.

**Build this only if slice 1 misjudges a probe** — i.e.
`test_search_gap_cut_eval_corpus_recall` (tests/test_skill.py) goes red
on a real corpus change, or transcript review shows cut pages losing
the skill the caller actually wanted.

## What it is

Re-score the page's top-10 against a per-skill **identity embedding**
(slug + title + summary + H2 headers — the same surface
`_skill_title_tokens` builds on): one cached vector per skill alongside
the chunk cache in `skill_index/`, then the same gap cut on the
now-comparable scores. Today's scores mix regimes (cosine ≲1.0, lexical
<0.5, title boost ≥1.5), so the cut can't compare a title pin against a
semantic row honestly; a shared embedding space can — that's what
decides whether "the tail below a pin" is a hedge worth keeping or
bloat to trim.

## Deferred alternative

An LLM rerank in the serving path stays off the table (per-call cost
and latency over ~150 skills × k=10); an LLM judge belongs in the
offline probe harness, if anywhere.
