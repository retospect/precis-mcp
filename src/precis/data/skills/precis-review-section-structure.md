---
id: precis-review-section-structure
title: precis — one-pass section-structure review
summary: Does the intro frame the contribution? Do sections deliver what the intro promises? Does the conclusion follow from the sections?
answers:
  - how do I check whether the intro's promised contributions actually land in the results?
  - what are the four structural checks for section-structure review?
  - what order should I run the structure checks in?
applies-to: get (kind='tex'), put (kind='memory')
tags: troubleshooting
kinds: tex, memory
status: active
---

# precis-review-section-structure — does the arc hold?

One review pass. One concern: the document's section-level structure
— the contract between intro, body sections, and conclusion. A paper
can have clean paragraphs and faithful citations but still fail at
this level: the intro promises X, sections deliver Y, the conclusion
claims Z. The reader leaves confused even when every sentence is
defensible.

## The four checks

### 1. Intro frames the contribution

Read the intro. Ask:

- **What's the question?** A real question, not "we study X".
- **Why does it matter?** A real motivation, not "X is important".
- **What's the gap in prior work?** Named explicitly.
- **What does this paper contribute?** One sentence the reader can
  paraphrase.
- **How is the rest of the paper organised?** A roadmap. ("In §2 we
  …, in §3 we …, in §4 we …, §5 concludes.")

Any of these missing or vague is a flag. The "what does this
paper contribute" sentence is the bar — the rest of the paper is
the evidence for it. If you can't paraphrase it after reading the
intro, the paper has no thesis.

### 2. Sections deliver what intro promised

Compare the contribution sentence + the roadmap against the actual
section list:

- Every promised contribution has a section that supports it.
- Every section is on the promised path (no orphan sections doing
  unrelated work).
- Section order matches the roadmap order (or the roadmap is
  outdated — usually means a section was added without updating
  the intro).

A section that exists but doesn't appear in the intro's roadmap is
a flag: either (a) the section is unmotivated and the reader
hits it cold, or (b) the intro is stale.

### 3. Each section's first paragraph frames its own work

Mini-intros. Open the first paragraph of each numbered section. It
should:

- Restate what this section is for (a one-line subset of the
  intro's roadmap entry).
- Set up what's about to happen in this section.

A section that dives into details without this scaffolding is a
flag. The reader needs the local frame to know which level of
the argument they're at.

### 4. Conclusion follows from sections

Read the conclusion. Every claim in the conclusion must trace back
to evidence in a body section. Walk each conclusion paragraph and
mark which section(s) support it.

Failure modes:

- Conclusion claim with no body-section evidence — flag.
  Strongest flag type at this level; the paper claims something
  it didn't show.
- Body-section result that doesn't surface in the conclusion —
  weaker flag, "lost contribution". Usually means the
  conclusion is undercount­ing the paper's own work.
- Conclusion that introduces a new claim absent from intro and
  body — flag. The conclusion is not the place to add results.

## Output: one flag per structural break

`kind='finding'` doesn't fit here — it's a citation-chase target
(`cited_in=` mandatory, "your own synthesis with no single source
→ not a finding"; see `precis-finding-help`). A structural read is
your own synthesis across the manuscript, not a sourced claim, so
record it as a `kind='memory'` linked to the section it's about:

```python
put(
    kind="memory",
    text="""Structure flag: intro promises a comparison of CNT vs
GNR mobility but §4 only covers CNTs. The promised GNR comparison
is missing.

Specifically:
- Intro §1.2: "We compare ballistic mobility in semiconducting
  CNTs against armchair GNRs at room temperature."
- §4 (Mobility): only CNTs covered. No GNR mobility data presented.

Severity: SUBSTANTIVE — the intro contract is broken. Either add a
§4.2 GNR mobility subsection, or trim the intro's promise.""",
    tags=["topic:section-structure-review"],
    link="xc<id>",  # the §4 section handle, from get() output
)
```

Severity guide:

- **SUBSTANTIVE** — missing thesis, conclusion claim with no body
  support, intro promise not delivered.
- **MODERATE** — section without local frame, outdated roadmap,
  lost contribution in conclusion.
- **NITPICK** — section-numbering style. Skip.

## Order of operations

Recommended order — saves time if an early check fails badly:

1. Thesis check (intro). If you can't extract a thesis, **stop
   here** and record that as the single SUBSTANTIVE flag. The
   rest of the review is downstream of fixing this.
2. Roadmap-vs-sections.
3. Per-section mini-intros.
4. Conclusion-to-sections trace.

## Anti-patterns

- Reviewing section content (citation faithfulness, paragraph
  flow). Those are separate passes (precis-review-citation-faithfulness,
  precis-review-paragraph-flow) — don't do them here.
- Style preferences ("I'd put §3 before §2"). Only flag order
  when the *logical* argument requires it.
- Skipping the conclusion-to-sections trace because the
  conclusion "reads well". A well-written conclusion can still
  invent claims the body didn't show.

## See also

- [[precis-review-paragraph-flow]] — paragraph-level
- [[precis-review-citation-faithfulness]] — claim ↔ source
- [[precis-polish-paper]] — runbook tying review passes together
- [[precis-memory-help]] — memory shape, link=/rel= on create
