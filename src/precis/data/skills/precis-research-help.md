---
id: precis-research-help
title: precis — corpus-grounded research with primary-source discipline
summary: corpus searches, primary-source rule, contradiction flagging, quantification targets
applies-to: get/search (kind='paper'|'chunk'|'memory'|'citation'), put (kind='citation')
status: active
---

# precis-research-help — depth over Perplexity

This skill is the discipline for a research-shaped task: surveying
the corpus on a topic, finding what's there, finding what's missing,
producing a numbered findings list with claim-level citations.

Anyone can ask Perplexity for a high-level summary. The bar here is
**rich depth that an expert would respect** — numbered findings,
quantified bounds, primary citations, contradictions flagged. If
your output reads like a high-school book report, the slice was
wasted.

## The quality bar (laundry list — apply every item)

1. **Primary sources, not reviews.** Cite the paper that ran the
   experiment, not the paper that summarised it. If you're reading a
   review for context, that's fine, but mint citations against the
   primary references the review points at. If you can't find the
   primary source in the corpus, mint a sibling `executor:fetch`
   subtask to search and ingest it; don't cite the review as if it
   were the source.

2. **Quantify everything quantifiable.** Numbers, ranges, error
   bars, sample sizes, temperature, pressure, concentration, time
   scales, percentages. "Improves efficiency" is meaningless;
   "raises peak quantum yield from 23% (Smith 2019) to 68% (Liu
   2024) for CdSe/ZnS at 290K" is a finding. If a paper reports a
   value, the citation must include it.

3. **Distinguish, don't average.** Where the literature has
   meaningful subdivisions, surface them rather than collapsing.
   Examples:
   - core-only vs core/shell vs core/shell/shell
   - solution-phase vs film vs single-particle
   - room-temperature vs cryogenic
   - synthesis route (hot-injection / aqueous / microwave / …)
   - measurement technique (TRPL / steady-state PL / PLE / single-
     molecule)
   - theory vs experiment (and at experiment, ensemble vs single-
     particle, vs *in operando* vs ex situ)
   The right cuts depend on the field. Pick the ones that matter.

4. **Flag contradictions explicitly.** Where two groups report
   different values for ostensibly the same condition, that's its
   own finding row: "Group A reports X under condition C; Group B
   reports Y under nominally identical C; possible explanations are
   …". Don't average them.

5. **Time bias.** Weight 2015–now (or the appropriate recent
   window for the field) but include foundational pre-2010 papers
   when they're still the canonical reference. Note when a finding
   is the consensus today vs only the most recent claim.

6. **Methodological skepticism.** Note when a claim depends on a
   specific synthesis, instrument calibration, or analysis pipeline.
   If a measurement is only reproducible by one group, that's
   meaningful context.

7. **Preprint vs peer-reviewed.** Distinguish in the citation. A
   preprint claim can be included but should be flagged.

8. **Citation density.** Each finding's claim is ≤2 sentences and
   carries at least one `kind='citation'` ref with a verbatim
   source quote (not a paraphrase). Multi-source findings carry one
   citation per source.

## Retrieval: cast a wide net before you read

One phrasing of one query is how findings get missed. For any
survey-shaped question, open with **broad retrieval** — several
rephrasings + hypothetical-answer passages fused in one call — and
use `per_paper=1` to triage across many papers before drilling:

```python
search(kind='paper', q='<question>',
       queries=['<rephrasing 1>', '<rephrasing 2>', …],
       answers=['<a sentence the answering passage would contain>'],
       per_paper=1)
```

When the survey matters enough to spend agent time, fire the **deep
campaign** instead and keep working while it runs:

```python
search(kind='paper', q='<question>', good=True)
# → {job: <id>, poll: "get(kind='job', id=…)"} — triage agents fan
#   out over the fused pool; the finished job carries a curated,
#   relevance-ranked shortlist in meta.result.
```

Inside a planner tick, don't busy-poll: mint your other subtasks,
let the tick end, and read the campaign result on a later tick (the
job is durable). Details: `precis-search-help` → Broad retrieval.

## Mint citations as you go

```python
put(kind='citation',
    text='Peak QY 68 ± 4 % for CdSe/ZnS aqueous synthesis at 290 K',
    source_handle='pc456',        # the paper-chunk handle, copied from search/get (a legacy slug~pos also resolves)
    source_quote='We measured a peak quantum yield of 68 ± 4 % '
                 'across n=12 batches…',
    verifier_confidence=0.95,
    link='paper:liu2024',
    rel='cites')
```

A finding without a `kind='citation'` ref is unsupported — don't
include it in the output.

## When the corpus is thin

If your topic has under N citable sources in the corpus (rule of
thumb: N=3 for sub-topics, 10 for a survey), don't paper over the
gap with prose. Either:

- Mint a sibling subtask `executor:fetch` to web-search and ingest
  the missing literature (the parent will re-tick when it lands), OR
- Add an explicit "gap" entry to the findings list: "We have no
  papers in the corpus on X under condition Y; this is a candidate
  for ingestion before the next pass."

The honest gap is more valuable than the dressed-up surface.

## Output format

Markdown numbered list. Each row:

```
1. <claim — ≤2 sentences, with quantification>. [pc142][pc143]
2. <next claim>. [pc144]
…
N. **Contradiction:** <description>. Group A: <quote>. [pc145]
   Group B: <conflicting quote>. [pc146] Possible resolutions: …
```

Each `[pc<id>]` is a paper-chunk handle copied verbatim from search/get
output — the chunk *is* the evidence, so the handle is self-verifying
and there is no separate citation footer to maintain. Never write a bare
number or a constructed handle where a `[pc<id>]` belongs.

Target 15–30 findings for a topic survey, 5–10 for a focused
question, 30+ for a broad lit review. Less than 5 findings = the
question was too narrow or the corpus too thin (mint fetch subtasks).

## When to split into siblings

A research task is splittable along its **distinguishing axes** (see
rule 3). If your topic is "QD luminescence efficiency," good
parallel children are:

- core-only QY landscape
- core/shell QY landscape
- temperature dependence
- synthesis-route sensitivity
- theoretical bounds

…rather than "left half of papers" / "right half of papers." Each
child carries the same depth discipline, applied to its slice.

## Anti-patterns (do not do)

- "Here are five key findings" — five is what Perplexity gives. We
  want fifteen-plus with quantification.
- Paraphrasing the source quote in the citation field. Verbatim or
  it's not a citation.
- Citing a review for a primary claim.
- Bullet-form summaries of paper abstracts. Synthesise across
  papers; an abstract-by-abstract recap is a reading list, not a
  finding.
- Hedging language ("studies suggest", "research has shown") instead
  of the specific source. Say who measured what, when.
