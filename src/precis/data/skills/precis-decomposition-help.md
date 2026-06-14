---
id: precis-decomposition-help
title: precis — when to split a task and how to size siblings
summary: when to split vs do it yourself, how to size siblings, depth-at-leaves principle
applies-to: put (kind='todo', parent_id=…), link (rel='blocked-by')
status: active
---

# precis-decomposition-help — split well or don't split

This skill is the discipline for the **planner's** core decision:
should you do this task in-tick yourself, or mint children and yield?

The two failure modes the LLM defaults to:

- **Procrastination-by-planning** — splits everything into subtasks,
  never produces direct output, runs up cost. Allen's diagnosis;
  applies to LLMs too.
- **Premature collapse** — does in one tick what should have been
  three children working in parallel with different evidence threads.
  Output is a brittle summary; misses depth.

This skill is how to land between them.

## Self-do test (apply every item)

Do it yourself in one tick when ALL of these are true:

1. You have all the information you need in your current prompt
   (parent body + child summaries + cached skill content). No new
   corpus searches required to produce the output.
2. The output fits in one response (≤2k tokens) and you don't need
   to weave multiple evidence threads.
3. The task is single-shape: one section to write, one question to
   answer, one critique to produce — not "research + write +
   review."
4. You can verify your own output without an external lens. (You
   can't review your own writing as well as a sibling reviewer can.)
5. The skill you're drawing on calls this a "single-tick" output, or
   the task is mechanical (reformatting, cite cleanup, etc.).

If any of those is false, **split**.

## Split test (the decomposition is justified when)

Split into N children when ANY of these are true:

1. **Parallel exploration.** The work has independent dimensions
   that can be explored simultaneously by different children (e.g.,
   different sub-topics in a lit review, different sections of a
   paper, different methodological dimensions of a review).
2. **External data needed.** A child has to web-search and ingest
   new corpus content, or wait for a fetch / ingest pipeline. Mint
   a sibling with the right `executor:*` tag.
3. **Divergent lens.** The output benefits from being produced and
   then critiqued by a second LLM looking at it fresh (writer then
   reviewer, claim then verifier). Sibling lens beats self-review.
4. **>2k tokens of output.** A single tick won't comfortably hold
   it. Split by section / claim / chapter.
5. **Recursive depth.** The work is itself a multi-step plan whose
   substeps are themselves planner-shaped (each requiring its own
   research-then-write-then-review).

## Sizing siblings

When you split, each child should be:

- **Specific.** Body names the deliverable shape, the constraints,
  the depth target. See the per-skill quality bars in
  [[precis-research-help]] / [[precis-write-paper-help]] /
  [[precis-review-paper-help]]. A child body that fits in one
  Perplexity search box is under-specified.
- **Self-contained.** The child has everything it needs from its
  body + the skill it pulls. It does not need to coordinate with
  siblings except via the parent's re-tick.
- **One axis.** Each child explores one axis: one sub-topic, one
  section, one dimension of review. If a child is doing two things,
  split it into two children.
- **Right model.** `LLM:opus` for synthesis, writing, review,
  judgement-heavy work. `LLM:sonnet` for filtered / pattern-matched
  / mid-difficulty work (gap analysis, classification, mid-length
  drafts). `LLM:haiku` rarely — only for trivial reformatting.
  Default is `LLM:sonnet`; upgrade to opus when the work needs it.
- **Right blocked-by.** If child B reads child A's output, mint
  `link(rel='blocked-by', src=B, dst=A)`. Unlinked children run in
  parallel — that's the default. Use blocked-by only for genuine
  dependencies, not "I'd like to read A first."

## Depth at the leaves, summarise only at root

The most important inversion. The default LLM behaviour is to
summarise everything; resist it.

- **Leaf children** produce **detailed output**: numbered findings,
  full quotes, quantified results, explicit contradictions. The
  child's `job_summary` is the actual artefact, not a précis of it.
- **Mid-level ticks** synthesise across children while **preserving
  specificity**: citations carry up, numbers carry up, distinctions
  carry up. The mid-level output is an assembly — Section A is
  child 1's output, Section B is child 2's, etc. — not a single
  flattened summary.
- **Root ticks** summarise **only** if the original goal explicitly
  asked for a summary. Otherwise the root is also an assembly.

The principle: if a consumer (human or downstream LLM) needs the
detail, it has to be there. Premature compression destroys
information that cost real budget to gather.

## When to use `ask-user` instead of split

You're not splitting; you're yielding. Use `ask-user` when:

- The work is **value-laden** in a way only a human can judge ("which
  direction should this paper take?", "is this finding worth
  publishing?").
- You hit a **hard ambiguity** in the parent's intent that no skill
  resolves.
- The work is **destructive or irreversible** (delete this, push
  this, send this) and you don't have explicit authorisation.

Tag: `ask-user:<the question>` so the attention view surfaces the
ask inline.

## When to use `halt:*` instead

Halt is a stronger yield: "do not call me again until a human
intervenes." Use it when:

- You've tried and the work is genuinely stuck (`halt:planner-stuck`).
- A guardrail tripped (`halt:cost-cap`, `halt:tick-cap`).
- You can see this task is impossible as specified and needs the
  goal restated.

Halt removes the task from the doable rotation entirely. Prefer
`ask-user` for "I need a decision" and reserve `halt` for "this is
broken."

## Anti-patterns (do not do)

- **One-child fan-out.** Minting a single child instead of doing it
  yourself. If there's only one direction to go, go that direction
  in-tick.
- **Splitting trivial work.** "Write a one-sentence summary" is not
  three children's worth of work.
- **Children with vague bodies.** "Research this topic" is not a
  child; "Survey CdSe core/shell QY enhancement in the 2018–2024
  literature, target 15 findings with primary cites" is a child.
- **Sibling chains.** If A blocks B blocks C blocks D, you've drawn
  a single chain — that's not parallelism, that's a long sequential
  walk. Either compress it (do it yourself) or actually split into
  parallel work.
- **Re-fire forever.** If you've already re-ticked N times and
  haven't converged, `ask-user` or `halt:planner-stuck`. The tick
  cap will hit eventually; reaching it is a failure mode, not a
  feature.
