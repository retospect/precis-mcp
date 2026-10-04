---
id: precis-review-citation-faithfulness
title: precis — one-pass citation-faithfulness review
summary: For each claim in a draft, check it is cited (sufficiency), the cited source supports it judged against the paper's full text — linked passages are provenance and may combine — and does not contradict its direction or strength (a separate polarity check), and prefer the living [fi<hub>] form; a claim the source contradicts gets corrected when you may write, flagged with exact replacement text when you may not
answers:
  - how do I check that every claim in a draft is cited and the citation actually supports it?
  - what counts as 'support' for a citation faithfulness check?
  - the linked passage alone does not carry the sentence — is that a miscitation?
  - may several passages of a source together support one claim?
  - may I cite a review or textbook instead of the primary paper?
  - how do I file a finding for each faithfulness problem I find?
  - the cited chunk contradicts the draft claim — do I fix the draft or flag it?
applies-to: get (kind='draft'|'paper'), put (kind='finding'|'todo')
tags: troubleshooting, workflow
kinds: draft, paper, finding, todo
status: active
---

# precis-review-citation-faithfulness — does the cited source actually say this?

One review pass, three concerns, all keyed off the citation tokens
(`[pc<id>]` paper chunk, `[pa<id>]` whole paper, `[pk<id>]` patent,
`[fi<id>]` finding/hub) in body text:

1. **Sufficiency** — every non-obvious claim carries a cite; a claim
   with none is a gap, filed as a todo (see Output below), not a
   finding.
2. **Correctness** — the cited source **actually supports the clause
   it backs**, judged against the paper's full text, and does not
   contradict it (see "What counts as support"). This is the pass's
   core and the single highest-value finding category in any review.
3. **Hub-cite rule** — every cite must be a finding hub `[fi<hub>]`;
   a bare `[pc<id>]`/`[pa<id>]` cite is a legacy form to convert
   (procedure step 8).

**Existence is not this pass's job.** Cite-token resolution and
paper-held status are checked deterministically before you see this
review — every handle in front of you is guaranteed to resolve to a
held paper. Pull the passage to judge *support*; don't spend a turn
confirming a handle resolves.

This is the **citation half** of "does the source support the
claim?" The complementary half — "does the claim actually follow
from this passage?" — lives in precis-review-paper-help under
verifier-loop. Run that separately; here we focus on the
mechanical question first because it's the cheapest catch and
strongly correlates with sloppy writing.

## The procedure

First, scan the passage for non-obvious claims with **no** citation
at all (sufficiency) — file each as the missing-citation todo below,
not a finding. Then, for each citation handle already present:

1. Resolve the handle to the exact chunk(s): `[fi<id>]` →
   `get(id='fi<id>', view='evidence')` for its grounding chunks, then
   `get(id='pc<id>')` on each; a legacy `[pc<id>]` → `get(id='pc<id>')`.
2. Read the linked chunks and compare them against the clause the
   citation backs in the draft. The linked passages are **provenance**
   — where the support was found and where you start reading — not the
   test. Several passages may combine: one carries the method, another
   the number.
3. If the linked passages, alone or together, carry the clause — done,
   no finding.
4. If they do not, **read the paper before you judge**: its TOC, the
   sections around the linked passages, a search for the clause's key
   number or term (`precis-check-source-help`). If the full text
   supports the clause, there is no citation defect — the pin points at
   the wrong passage. Name the passage that does carry it in your tick
   conclusion so the hub can be re-grounded; do not file a finding.
5. If the full text only weakly supports a softened claim — finding:
   weak / inflated citation. Quote both the draft claim and the
   source's actual passage.
6. If the source supports a *different* claim, the writer cited the
   wrong paper, or the sentence attributes a finding to a group
   ("Lee et al. measured…") and the cite is not that group's paper —
   finding: wrong cite.
7. If nothing in the paper bears on the claim — finding: unsupported
   claim. This is the highest-severity finding type. "Nothing" needs a
   real search: every paper behind the hub plus its SI, the acronym and
   its expansion both ways, split phrasings, figure/table captions —
   and the finding lists the patterns searched.
8. **Polarity, as its own check, even when steps 3–5 passed:** does the
   source run against the clause's direction or strength? A passage can
   support half a sentence and contradict the other half. If it does —
   finding: contradicted claim (SUBSTANTIVE). A contradiction is about the
   same quantity, system and comparable conditions. A different value
   measured on a different sample, material, method or condition is a
   different finding, not a contradiction. Before a contradiction or
   disputes call, open the claim's own establishing passage
   (`get(kind='finding', id=<hub>, view='evidence')`) and write down both
   setups (sample/material, method, conditions) — claim's vs passage's.
9. If the cite is a bare `[pc<id>]`/`[pa<id>]` — file a change-request:
   it must become a hub cite. A `◆ taproot:` hint next to it names the
   hub the paper already grounds — switch to `[fi<hub>]`, or
   `[fi<hub>>pc<id>]` to pin this exact passage while riding the living
   resolution; no hint → the writer mints a hub on the passage and runs
   the adversarial check (`precis-citation-help` steps 2–3). Hub
   coverage itself is deterministic — you only act on it.

A citation is a **finding-hub handle written inline** — `[fi41]`, or
several `[fi41][fi92]`; the hub's grounding chunks are the passages you
check. The author never types `\cite{}`; that is export-only output. A
`[me<id>]`/`[dc<id>]` reference is a **link, not a citation** (it
points at our own notes, not the literature) — it is out of scope
here; skip it.

## The source wins — correct the draft when you are allowed to write

Cases 5–8 are **defects in the draft, not authorial intent**. A
draft is a record of what is known; a number a held source does not
support does not become true by surviving review.

- **If your task carries `meta.author`** (the authoring variant of this
  pass, [[precis-review-authoring]]) — **fix it in place**: reword the
  sentence to what the source states, delete an unsupported quantity
  rather than keep it, write both readings when two held sources
  disagree, and cite the `[fi<id>]` hub grounded on the passage you
  read. Never substitute a value you did not read in a source. Keep the
  author's argument intact, and say in your tick conclusion when the
  argument depended on the wrong number.
- **If it does not** — you are read-only, so file the finding below, but
  **carry the exact replacement sentence in the body**. A finding that
  says only "this is wrong" costs the human a second pass; one that ends
  with the corrected sentence and its hub handle is a single re-tick.

Either way the human signs off before anything publishes, so the
correction is cheap and the silent flag backlog is not.

## Output: one finding per problem

Mint `kind='finding'` refs linked to the manuscript ref and the
cited paper. Each finding's body carries the precise diff so a
single re-tick on the writer can fix it.

**Never file a manuscript defect as a `gripe`.** A gripe is a bug in
the precis tool/repo, not a content problem — see `precis-gripe-help`
("A gripe is a bug in *precis*, not a defect in your content"). A
*gap* this pass surfaces that isn't a drift (a claim with **no**
citation at all, an empty section stub, a table with no backing data)
is a `todo` anchored to the draft chunk, not a finding and not a gripe.
Anchor it with `meta.anchor='dc<id>'` and stamp an `AUDIT:<category>`
tag (`missing-citation` / `empty-stub` / `unsupported-claim` /
`citation-drift` / `missing-data`) so the draft reader badges the chunk
by category and `search(kind='todo', tags=['AUDIT:missing-citation'])`
enumerates the backlog:

```python
put(
    kind="todo",
    text="dc1518518: algD operon claim for alginate EPS lacks a "
    "gene-discovery citation — find + cite the foundational paper.",
    meta={"anchor": "dc1518518"},
    tags=["AUDIT:missing-citation"],
)
```

```python
put(
    kind="finding",
    title="Citation drift in dc207 (Results > Kinetics): 12% FE "
    "claim vs pc1843's ~10%",
    body="""The claim "we observed 12% Faradaic efficiency..." cites [pc1843].

pc1843's actual text reads:
"a Faradaic efficiency of approximately 10% was measured"

Severity: SUBSTANTIVE — the cited chunk supports ~10%, not 12%. The
claim's quantitative core breaks.""",
    cited_in="pc1843",
    tags=["AUDIT:cited-without-support"],
)
```

`title=`, `body=`, and `cited_in=` (the chunk handle the claim cites)
are all **mandatory** — `text=`/`link=`/`rel=` are not finding
parameters, and there is no `cited-without-support` link relation;
carry that classification in the `AUDIT:` tag as shown.

Findings stay open until the writer's next tick resolves them.
The `all_child_findings_resolved` auto_check evaluator (T3.1)
closes the parent review-pass todo only when every finding is
either closed (STATUS:done by the writer) or won't-do.

## What counts as "support"

The rule (Reto, 2026-10-03, system-wide):

> Every cited source must support the clause attributed to it, judged
> against the paper's full text, and passages may combine to support it.
> The passage pin is provenance, not the test. The source must not
> contradict the clause's direction or strength; that is a separate
> polarity verdict. Secondary sources are allowed for background and
> definitions, primary preferred. A sentence that attributes a finding
> to a group must cite that group's paper.

Support is the source establishing the clause's substantive core.
Paraphrase is fine; the source does not have to echo the sentence, and
no single passage has to carry all of it. What breaks support, or its
strength:

- Different numbers (claim says 12%, source says 10% — a SUBSTANTIVE
  finding even if the surrounding text matches).
- Different units (mM vs M is the same way).
- Different signs, exponents, ratios.
- "approximately" present in the source but dropped in the claim
  (changes claim strength → citation inflation).
- The claim asserts what the source only suggests / is consistent
  with.

**Polarity is a separate verdict.** "Supported" and "contradicted" are
not two ends of one scale. A source that confirms the method but reports
the opposite trend supports one clause and contradicts another; record
both. Contradiction needs the same quantity in the same system under
comparable conditions. A 5,000 cm²/Vs mobility in a later single-layer
device does not contradict a 10,000 cm²/Vs few-layer measurement.

**Secondary sources.** A review or textbook may back background and
definitions; prefer the primary paper when it is held. A finding — a
measured value, an observed effect, a first demonstration — cites the
paper that did the work, and a sentence that names who found it must cite
that group's paper.

**Reporting.** A count of linked passages that fail alone is a grounding
statistic, not a miscitation count. Write it as "N linked passages do
not individually carry the full sentence". Use "wrong cite",
"unsupported" or "contradicted" only for a source judged against its
full text.

When in doubt after reading the paper, write the finding. False positives
are cheap; an unsupported citation that survives review is expensive.

**Weak support is not the same as under-qualification.** Prose is allowed
to lean on its section for scope and on the cite popover for the rest —
judge it by whether a reader who believed the sentence would be surprised
by the hub's sentence, not by whether every condition is restated inline.
The rule you are reviewing against is [[precis-claim-fidelity-help]].

## Anti-patterns

- "Looks similar" — not a support check. Pull the chunk with
  `get(id='pc<id>')` and read it.
- Trusting the handle without resolving it. The linked chunk is where
  the evidence starts, not all of it.
- Calling a cite wrong because one linked passage does not carry the
  whole sentence. Read the paper first (step 4); passages combine.
- Scoring only support and skipping polarity. A "yes" on half the
  sentence can sit next to a contradiction of the other half (step 8).
- Aggregating findings into one "many cites don't hold" — one finding
  per citation so each can be resolved independently.
- Treating a `[me<id>]`/`[dc<id>]` link as a citation. Those point at
  our own notes (a `related-to` link), never the literature — they
  are not in scope here and never reach the bibliography.
- Re-checking whether a handle resolves. That's a deterministic
  pre-check run before this pass ever starts — spend the turn on
  *support*, never existence.
- Ignoring a `◆ taproot:` hub hint. A bare cite next to one is a
  change-request, not a nice-to-have — file it (step 9).

## See also

- [[precis-draft-help]] — write side: inline [fi<id>] citations
- [[precis-bibliography-help]] — read side: who cites a paper
- [[precis-review-paper-help]] — full adversarial review including claim-support
- [[precis-common-reviewer]] — shared reviewer discipline
- [[precis-finding-help]] — how to write a finding
