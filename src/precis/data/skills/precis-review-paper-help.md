---
id: precis-review-paper-help
title: precis — adversarial review of scientific writing
summary: adversarial review — unsupported claims, missing counterarguments, methodological weak spots
applies-to: get/search (kind='paper'|'chunk'|'citation'|'todo'), put (kind='finding')
status: active
---

# precis-review-paper-help — find what's wrong

This skill is the discipline for a review-shaped task: reading a
draft (own or others') and producing a structured critique that
surfaces unsupported claims, missing counterarguments, weak
quantification, and verifier-loop failures.

A useful review is **adversarial**. The bar is "what would a serious
opponent attack here?" — not "how can I make this look polished."
The default LLM failure mode is sycophancy; reject it explicitly.

## The quality bar (laundry list — apply every item)

1. **Default to skeptical.** Read every claim asking "is this
   actually supported?" If the cited source is paraphrased, check
   the verbatim quote. If a number is given without an error bar,
   note it. If "studies have shown" appears without an inline cite,
   flag it.

2. **Verifier-loop check.** Every `[cite:N]` marker must resolve to
   a `kind='citation'` ref with a verbatim source quote that
   supports the specific claim. Pull each citation; read its quote;
   judge support strength. Wrong-quote / quote-doesn't-support-claim
   is the most common cite failure and the highest-value catch.

3. **Counterargument audit.** For each major claim, ask "what's the
   strongest objection?" If the draft doesn't address it, that's a
   numbered review point. If the draft addresses a weak strawman
   instead of the real objection, that's another.

4. **Methodology against best practice.** Each method gets
   compared to current best practice in the field. Out-of-date
   instruments, under-powered statistics, missing controls,
   non-blinded measurements, single-sample claims — all numbered
   points. Cite the methodology source.

5. **Quantification gaps.** Claims missing numbers, numbers
   missing error bars, error bars missing N, ranges missing the
   distinguishing conditions — each one numbered.

6. **Scope drift.** Did the draft promise X in the abstract and
   deliver Y in the conclusion? Did a section's title not match
   its content? Note explicitly.

7. **Self-consistency.** Does Table 2 agree with Figure 3? Does the
   discussion's "we found X" align with the results' "we measured
   X ± Δ"? Numbered point per discrepancy.

8. **Distinguish nitpick from substantive.** Each finding is
   labelled:
   - **SUBSTANTIVE** — claim doesn't hold, missing analysis,
     significant methodological flaw. Fix before publication.
   - **MODERATE** — citation drift, missing context, weak
     argument. Addressable but not fatal.
   - **NITPICK** — wording, formatting, minor reference style.
     Not a real issue.
   Nitpicks below 10% of total; if your review is 90% nitpicks,
   you didn't read for substance.

9. **No sycophancy openers.** Skip "The authors have done excellent
   work…" / "This is a thoughtful analysis…". Lead with what's
   wrong. The author already knows what's right.

## Output format

Markdown, numbered findings. Each row:

```
1. **SUBSTANTIVE** [cite:142 doesn't support the claim]
   The text states: "QY rises monotonically with shell thickness."
   [cite:142] (Liu 2024) actually reports: "QY peaks at 2.0 ML and
   decreases beyond 2.5 ML." The cited source contradicts the claim;
   the text needs revision OR a different cite.

2. **MODERATE** [missing counterargument]
   §3.2 argues aqueous synthesis gives higher QY than hot-injection.
   It does not address Garcia 2023's finding that aqueous samples
   degrade 3× faster under continuous illumination. Either rebut or
   acknowledge the trade-off.

3. **SUBSTANTIVE** [unsupported quantification]
   "Up to 40% improvement" appears in the abstract without a cite or
   own-work reference. Either point at the result section row or
   remove the number.

…
```

Footer:

```
## Summary
- Substantive findings: N (fix before resubmit)
- Moderate findings: N
- Nitpicks: N (omit if not asked)
- Overall judgement: <publish-ready | needs-revision | reject-resubmit>
```

## When to split into siblings

A review task is splittable along **dimensions** of review:
- citation-verifier pass (mechanical: pull each cite, judge support)
- counterargument audit (per-claim adversarial pass)
- methodology audit (against current best practice)
- self-consistency audit (cross-section / table / figure)
- quantification audit (numbers / error bars / N)

Each dimension done in parallel by a sibling subtask. The parent
re-tick merges findings into one numbered list with deduplication.

## When to mint a `kind='finding'` ref

If the review surfaces something significant enough to track separately
(e.g., a contradiction between two papers in our corpus that wasn't
known before), mint a `kind='finding'` ref so the finding is reusable
across future reviews. See [[precis-finding-help]].

## Anti-patterns (do not do)

- Praise without identifying a substantive issue first. Review's job is
  critique; praise belongs in the cover letter, not the report.
- "The paper could be strengthened by…" without saying what's currently
  weak. Name the weakness explicitly.
- 90% nitpicks. The point is substance.
- Hedged criticisms ("might want to consider …"). Be direct: "Section
  3.2 lacks the X analysis. Run it or remove the X claim."
- Reviewing only what the draft says (face value). A real review reads
  the cited sources and judges whether they support the claims.
- "Looks good to me" without an audit trail. Show the work.
