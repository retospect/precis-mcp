---
id: precis-review-citation-faithfulness
title: precis — one-pass citation-faithfulness review
summary: For each claim in a draft, check it is cited (sufficiency), the cite supports it (correctness), and prefer the living [fi<hub>] form over a frozen paper cite (living-cite preference); a claim the source contradicts gets corrected when you may write, flagged with exact replacement text when you may not
answers:
  - how do I check that every claim in a draft is cited and the citation actually supports it?
  - what counts as 'support' for a citation faithfulness check?
  - how do I file a finding for each faithfulness problem I find?
  - the cited chunk contradicts the draft claim — do I fix the draft or flag it?
applies-to: get (kind='draft'|'paper'), put (kind='finding'|'todo')
tags: troubleshooting, workflow
kinds: draft, paper, finding, todo
status: active
---

# precis-review-citation-faithfulness — does the cited chunk actually say this?

One review pass, three concerns, all keyed off the citation tokens
(`[pc<id>]` paper chunk, `[pa<id>]` whole paper, `[pk<id>]` patent,
`[fi<id>]` finding/hub) in body text:

1. **Sufficiency** — every non-obvious claim carries a cite; a claim
   with none is a gap, filed as a todo (see Output below), not a
   finding.
2. **Correctness** — the cited chunk **actually supports the claim it
   backs**. This is the pass's core and the single highest-value
   finding category in any review.
3. **Hub-cite rule** — every cite must be a finding hub `[fi<hub>]`;
   a bare `[pc<id>]`/`[pa<id>]` cite is a legacy form to convert
   (procedure step 7).

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
2. Read the chunk's text and compare it against the claim the
   citation backs in the draft.
3. If the chunk directly and substantively supports the claim —
   done, no finding.
4. If the chunk is topically related but only weakly supports a
   softened claim — finding: weak / inflated citation. Quote both the
   draft claim and the chunk's actual passage.
5. If the chunk supports a *different* claim, or the writer cited the
   wrong paper for this one — finding: wrong cite.
6. If the chunk says nothing that bears on the claim — finding:
   unsupported claim. This is the highest-severity finding type.
7. If the cite is a bare `[pc<id>]`/`[pa<id>]` — file a change-request:
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

Cases 4, 5 and 6 are **defects in the draft, not authorial intent**. A
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

Support is the cited chunk establishing the claim's substantive core.
Trivial wording differences between claim and chunk are fine — the
chunk does not have to echo the sentence. What breaks support:

- Different numbers (claim says 12%, chunk says 10% — a SUBSTANTIVE
  finding even if the surrounding text matches).
- Different units (mM vs M is the same way).
- Different signs, exponents, ratios.
- "approximately" present in the chunk but dropped in the claim
  (changes claim strength → citation inflation).
- The claim asserts what the chunk only suggests / is consistent
  with.

When in doubt, write the finding. False positives are cheap; an
unsupported citation that survives review is expensive.

**Weak support is not the same as under-qualification.** Prose is allowed
to lean on its section for scope and on the cite popover for the rest —
judge it by whether a reader who believed the sentence would be surprised
by the hub's sentence, not by whether every condition is restated inline.
The rule you are reviewing against is [[precis-claim-fidelity-help]].

## Anti-patterns

- "Looks similar" — not a support check. Pull the chunk with
  `get(id='pc<id>')` and read it.
- Trusting the handle without resolving it. The cited chunk is the
  *evidence* under test.
- Aggregating findings into one "many cites don't hold" — one finding
  per citation so each can be resolved independently.
- Treating a `[me<id>]`/`[dc<id>]` link as a citation. Those point at
  our own notes (a `related-to` link), never the literature — they
  are not in scope here and never reach the bibliography.
- Re-checking whether a handle resolves. That's a deterministic
  pre-check run before this pass ever starts — spend the turn on
  *support*, never existence.
- Ignoring a `◆ taproot:` hub hint. A bare cite next to one is a
  change-request, not a nice-to-have — file it (step 7).

## See also

- [[precis-draft-help]] — write side: inline [fi<id>] citations
- [[precis-bibliography-help]] — read side: who cites a paper
- [[precis-review-paper-help]] — full adversarial review including claim-support
- [[precis-common-reviewer]] — shared reviewer discipline
- [[precis-finding-help]] — how to write a finding
