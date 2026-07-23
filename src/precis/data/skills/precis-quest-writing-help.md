---
id: precis-quest-writing-help
title: precis — writing a quest that stays a quest
summary: judgment for authoring a striving — vision vs BHAG vs SMART, one-sentence + plain-language + no-jargon checklist, why technique/paper content belongs in the dossier not the statement, and why quests must stay few
applies-to: put (kind='quest') — the judgment call before you call it
status: active
---

# precis-quest-writing-help — writing a quest that stays a quest

This is the **judgment** companion to [[precis-quest-help]] (the verb
reference — put/tag/link/get, logbook, dossier mechanics). That doc
tells you *how* to call the API once you've decided what to write; this
one is about **what makes a good striving statement**, and where the
technical detail that inevitably wants to attach to it should actually
live.

Wordsmithing the statement against the checklists below doesn't require
delete + recreate — `edit(kind='quest', id=N, mode='replace',
text='…')` rewrites the founding text in place, keeping the id, the
logbook, and every `serves`/`served-by` link intact (`put(id=N,
text=…)` is a different verb: it only ever *appends* a logbook entry).

## The ladder: vision → BHAG → SMART goal

Borrowed from strategy literature (Collins' BHAG, Google's OKRs), three
distinct tiers of ambition map cleanly onto this system's own layers:

| tier | horizon | achieved when | this system's kind |
|---|---|---|---|
| **vision** | 5–10+ yr, open-ended | never fully — it's the asymptote | **quest** |
| **BHAG** | 10–30 yr, concrete finish line, ~70% success bar | a measurable stretch is hit | a **big project** that `serves` the quest |
| **SMART goal** | one cycle, scoped | fully, ~100% expected | an ordinary **todo** |

A quest belongs at the **vision** tier only. Confusing it with a BHAG
(give it a finish line) or a SMART goal (give it a technique name and a
benchmark number) is the single most common way a quest goes wrong —
it stops orienting and starts describing one experiment.

## The one-sentence test

Write the striving as **one sentence, roughly 15–30 words, plain
language** — the existing quests in this system hold the line:

- *"A world that runs light on the planet — energy, materials, and
  manufacturing that heal rather than harm."*
- *"Structures lighter than air — very fine, sturdy-or-precise — that
  float high in the atmosphere."*

Run every draft striving through these checks before minting:

1. **Could a stranger repeat it back after one read?** If it needs a
   footnote, it's not a vision yet.
2. **No acronyms, no benchmark names, no method names.** "DeepSciVerify",
   "Micro-F1", "ROLE3:own" — none of these belong in a striving
   statement. If a sentence needs one to make sense, that sentence is a
   **dossier entry**, not the quest (see below).
3. **Specific enough to exclude something.** "Make precis better" fails
   (it could describe any pursuit unchanged); "don't let precis get
   bamboozled by a bad paper" passes (it names a specific failure mode
   it's against).
4. **Names a future state, not an activity.** "Verify claims" is a task;
   "don't let precis get bamboozled by a bad paper" is a state of the
   world.
5. **No superlatives or empty adjectives** — "best-in-class",
   "world-class", "cutting-edge" signal you're filling space, not
   describing a destination.

The rubric that follows the blank line is **axes, not a paragraph**:
3–5 short noun phrases joined by `·` (`NH₃ selectivity · yield ·
stability`), never a sentence with citations or a specific paper's
metric in it.

## Where the technical detail goes: the dossier, not the quest

A quest and its literature are different documents for a reason. The
**striving + rubric is the compass** — it should read the same in five
years. The **dossier** (`view='dossier'`, [[precis-quest-help]]) is the
living, disposable synthesis — this is where a specific technique, a
specific paper, a specific benchmark number belongs, cited properly
once the paper is in the corpus ([[precis-cite-paper-help]]):

- Quest: *"Don't let precis get bamboozled by a bad paper."*
- Dossier entry: *"Selective evidence escalation — verify a claim
  against the cited paper's abstract first, escalate to full-text only
  when the abstract-level verdict is uncertain [pc1234]. Reports 86.7
  Micro-F1 on SCitance... Caveat: assumes the abstract itself is a
  faithful summary — an inflated or vague abstract lets a bad claim
  through unchecked."*

If the paper isn't ingested yet, log the technique as a **`hypothesis`**
logbook entry (unproven, worth testing) rather than folding it into the
founding text — or better, hold off until it's actually stubbed and
grounded, so the dossier can cite it for real instead of describing it
from memory.

## Quests must stay few

Aspirational-OKR practice caps the count deliberately — roughly one big
aspirational objective per grand pursuit, the rest committed — and the
same discipline applies here. Before minting a new quest, ask: does
this **serve** an existing grand quest instead of standing alone?

```python
link(kind='quest', id=<new>, target='quest:<grand>', rel='serves')
```

A proliferation of unlinked top-level quests is a sign something should
have been a project, not a new vision.

## Worked example — a quest gone wrong, and its fix

A first draft for a literature-integrity quest read:

> *Keep scientific-literature integrity practices at the frontier —
> claim-to-citation alignment, reproducibility, and citation grounding
> — across everything precis reads, cites, and reports.
> Rubric: claim–citation alignment rate · hallucinated/unsupported-citation
> rate down · evidence-retrieval efficiency (resolved without full-text
> where possible) · coverage of adopted techniques across the ingested
> corpus*

Workable, but already leaning technical — and a second draft folded a
specific paper's abstract, Micro-F1 score, and benchmark name directly
into the striving text. Neither belongs at this tier. The corrected
quest:

> *Don't let precis get bamboozled by a bad paper — keep every
> practical pursuit grounded in evidence that actually holds up.
> Rubric: claims traced to real evidence · false leads caught early ·
> checking effort spent where it counts, not wasted chasing certainty*

— one sentence, no jargon, and it `serves` the existing grand quest
("a world that runs light on the planet") instead of standing alone,
because a bogus paper wastes real time on the practical work already
running there. The paper and its benchmark moved to the dossier, to be
cited once actually ingested — not described from memory.

## See also

```python
get(kind='skill', id='precis-quest-help')       # verbs, lifecycle, logbook, dossier mechanics
get(kind='skill', id='precis-cite-paper-help')  # citing a paper properly in the dossier
get(kind='skill', id='precis-perplexity-help')  # research a technique before writing it up
```
