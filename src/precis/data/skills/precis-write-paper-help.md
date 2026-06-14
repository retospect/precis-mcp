---
id: precis-write-paper-help
title: precis — drafting scientific writing with claim-level evidence
summary: drafting scientific writing — claim-level citation density, evidence threading, voice
applies-to: get/search (kind='paper'|'chunk'|'memory'|'citation'), put (kind='citation')
status: active
---

# precis-write-paper-help — writing that holds up

This skill is the discipline for a writing-shaped task: producing a
section / draft / response in scientific or technical prose where the
claims are evidence-bound and every assertion either carries a
citation or is the author's stated contribution.

The failure mode to avoid: LLM-bland prose with "studies have shown"
and "it is widely believed" hedges. That writing is invisible; it
doesn't survive a serious reader.

## The quality bar (laundry list — apply every item)

1. **Claim-level citation density.** Not paragraph-level. Each
   factual assertion that isn't the author's own contribution
   carries an inline citation marker pointing at the `kind='citation'`
   ref you minted. Density target: ~1 citation per 2–3 sentences in
   an evidence-dense section, every sentence in a literature review.

2. **Distinguish own contribution from prior work.** Every claim
   falls in exactly one of three buckets: (a) cited from prior work,
   (b) the author's own measurement / argument / synthesis, (c) a
   logical step combining (a) and (b). The text must make which is
   which obvious. "We show X" vs "Smith showed Y" vs "Combining
   these, X must imply Z."

3. **Cite primary sources, not reviews.** Same rule as
   [[precis-research-help]] — the citation must point at the paper
   that produced the result, not at a summary of it. Use reviews for
   structure / framing, never for primary evidence.

4. **Quantify or quote.** A claim like "QY is improved in core/shell
   structures" is too soft. "Core/shell architectures raise QY from
   ~25% (CdSe, Smith 2019 [cite:142]) to 60–80% (CdSe/ZnS, Liu 2024
   [cite:143])" is publishable. Numbers, ranges, or verbatim quotes
   from cited sources.

5. **Address counterarguments.** A serious section names the
   strongest objection to its claim and addresses it. If there's a
   group whose results contradict yours, cite them and explain why
   your framing still holds (or where they differ in scope,
   methodology, or sample). Silencing dissent is a tell.

6. **Quantify limitations.** Every method section names its
   limitations with the same specificity as its claims. "The
   measurement is sensitive to surface oxidation" is weak;
   "Measurements taken later than 4 hours after synthesis show
   systematic 8±2 % drift attributable to surface oxidation, so the
   reported values are 0–4 h averages" is real.

7. **Active voice for own contribution, attributed voice for
   prior work.** "We measured…" vs "Smith and colleagues report…".
   Passive voice for own work hides who did what.

8. **Structure: claim → evidence → implication.** Each paragraph
   makes a single point. Lead with the claim, support with the
   evidence (cited), close with the implication. Multi-claim
   paragraphs read like notes.

9. **Concrete language wins.** Replace abstract nouns with
   measurable nouns. "Better performance" → "higher QY at lower
   excitation density". "Improved stability" → "<5% drift over 30
   days at 20°C".

## Output format

Markdown, with inline citation markers using `[cite:<id>]` where the
ids point at `kind='citation'` refs you've minted via `put(kind=
'citation', …)`. Multiple cites per claim are bracketed together:
`[cite:142, cite:143]`.

Section ends with a `## Citations` footer that lists every cite id
used in the section with its paper handle + chunk + verbatim source
quote (this is for the verifier loop; without it the cite is
unverified).

Example:

```markdown
Recent work shows substantial QY enhancement when CdSe cores are
shelled with ZnS. [cite:142] reported peak quantum yields of 68 ± 4 %
in CdSe/ZnS prepared by aqueous synthesis, a four-fold improvement
over comparable core-only systems. [cite:143] However, this gain
depends on shell thickness; samples with shells thinner than 1.5
monolayers showed no improvement over bare cores. [cite:144]

We extend this work by measuring QY under continuous illumination at
elevated temperature (60°C, 100 mW cm⁻²). Our data show…

## Citations
- cite:142 — paper:liu2024 chunk 12 ("We measured a peak quantum yield
  of 68 ± 4 % across n=12 batches…")
- cite:143 — paper:smith2019 chunk 7 ("…raised the QY to 23%…")
- cite:144 — paper:zhang2023 chunk 4 ("Below 1.5 ML the shell did not
  passivate surface trap states…")
```

## When to split into siblings

A writing task is splittable along its **sections** (intro, methods,
discussion, etc.) or along its **claim threads** (each major claim
gets its own evidence-gathering subtask). The parent re-tick weaves
the section drafts into the final structure.

If the writing depends on synthesis the corpus doesn't yet support,
mint sibling **research subtasks** (using [[precis-research-help]])
to produce the findings list first; the writing subtask then reads
that list and produces prose.

## Anti-patterns (do not do)

- "Studies have shown" / "Research suggests" / "It is widely believed"
  — hedges that hide the source. Either cite the paper that
  showed it, or say "We do not have evidence in the corpus."
- Paragraph-level cites only (one `[cite:N]` at the end of a
  paragraph that made five claims). Each claim gets its own cite.
- Citing the abstract. Cite the result section's specific claim.
- Inserting a citation that wasn't minted via `put(kind='citation')`
  — the verifier loop can't check it.
- "We will discuss …" / "This paper presents …" meta-prose. The
  reader is reading; tell them what's true, not what they're about
  to read.
- LLM tells like "It is important to note that …", "It should be
  emphasized that …", "Notably, …" used as soft openers. Cut them.

## Verifier loop hand-off

When your section is done, the parent task should kick off a sibling
**review** task (see [[precis-review-paper-help]]) that critiques the
draft for unsupported claims, missing counterarguments, weak quantif-
ication, and verifier-loop failures. Don't mark the parent done until
review passes — depth means surviving criticism.
