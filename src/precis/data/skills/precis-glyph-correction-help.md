---
id: precis-glyph-correction-help
family: paper
title: precis — settle a glyph-damaged passage and record the correction
summary: when extraction lost μ, minus signs or degree marks, settle the reading from the paper's own text or ask a human to read the PDF, restate token by token, record it on the paper, then change the draft
answers:
  - a number in a paper passage looks glyph-damaged — mA or μA?
  - how do I record that a person corrected an OCR reading?
  - how do I ask a human to read the PDF for me?
  - the paper itself prints a wrong value — how do I record that?
  - after a correction, which claim hubs and drafts need rewording?
applies-to: get/put (kind='paper'), edit (kind='draft')
status: active
tags: [workflow]
kinds: [paper, draft]
---

# precis-glyph-correction-help — settle a damaged reading, then record it

Extraction can drop `μ`, minus signs, `°` or exponents ("10 mA/cm2" for
10 μA/cm²). The body text stays as extracted; the **correction record**
(a row anchored on paper plus quoted span, not on the `pc` handle)
renders under the passage and is honoured by the pin term check, the
draft glyph warning and measure `glyph_suspected`.

## Steps — never skip the order

1. **Infer first.** Try the paper's own text: abstract, caption, table,
   arithmetic with other stated quantities. A reading settled this way is
   tier `inferred`, never `human-read`.
2. **Else ask a human.** Send: paper handle and DOI, where in the PDF
   (section, nearest figure), the extracted sentence verbatim, the exact
   tokens in doubt. Keep the value **out of the draft** until answered.
3. The human reads the PDF and answers.
4. **Restate.** Write the whole corrected source sentence with a
   token-by-token list of changes (`"mA" → "μA"`); the human approves.
   This catches slips in the human's own answer (a typed "nM" was nN).
5. **Record** on the paper (below).
6. **Only then** change the draft, and log the change.

## Record the correction

```python
put(kind="paper", id="pa494", args={"correction": {
    "extracted_span": "current density 10 mA/cm2",   # verbatim, as extracted
    "corrected_span": "current density 10 μA/cm²",
    "tier": "human-read",          # | "inferred" | "confirmed-misprint"
    "reader": "Reto", "approved_by": "Reto",
    "source_file": "kleshch2010.pdf", "page": 4,
    "note": "only the tokens listed were checked"}})
get(kind="paper", id="pa494", view="corrections")  # all records on the paper
```

The extracted span must occur in a live body chunk. The two human tiers
(`human-read`, `confirmed-misprint`) require `reader`, `approved_by`,
`source_file` and `page`; `confirmed-misprint` also a `note` with the
evidence. `chunk='pc…'` is an advisory pointer only. The record renders
under the passage text; the pin check reads the passage with the corrected
span in place of the extracted one, so quoting the superseded value stays
uncovered.

## The paper is wrong, the extraction right

Tier `confirmed-misprint`: `extracted_span` is the passage as printed,
`corrected_span` the evidence-backed true value, `note` the evidence
(the relation, a table elsewhere). Example: a caption prints 84.6° where
the paper's own relation gives 83.62°.

## After recording — follow the claim hubs

List claim hubs grounded on the passage. Reword unsigned ones with
`edit(kind="finding", …)`. A signed hub is immutable: supersede it, or
open an ask for the person who signs; until then cite the passage
directly rather than the frozen hub, and note the value on the papers
that repeat it.
