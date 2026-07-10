# ADR 0054 — The argument graph: lemmas, inferences, and the reasoning shadow beside a draft

- **Status**: proposed (2026-07-10) · design conversation captured, not
  yet sliced. This ADR records the *decisions*; the build slice + skill
  sketches live in the design of record
  [`docs/design/argument-graph.md`](../design/argument-graph.md).
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0030 — `job`/`finding`/`cron` stay separate from `todo`](./0030-job-finding-cron-stay-separate.md)
    — the reasoning that keeps kinds distinct rather than collapsing
    them; this ADR applies the same test and concludes the *opposite*
    for lemmas/inferences (a sub-kind of `memory`, not a new kind).
  - [ADR 0033 — Draft-as-chunks](./0033-draft-chunks-editable-document.md)
    — the editable document the argument graph shadows; a `[pc<id>]`
    citation in prose is the reader-facing endpoint of a lemma.
  - [ADR 0036 — Universal handles](./0036-universal-handles.md) — the
    `pc…`/`dc…`/`mc…` handles that the `entails`/`derived-from` edges and
    the `entailed-by` view resolve against.
  - [ADR 0051 — Turn-taking: persona threads & blackboard convergence](./0051-turn-taking-persona-threads-and-blackboard-convergence.md)
    — the argument graph is a natural convergence substrate: forked
    persona threads deposit lemmas and reconcile via `contradicts`
    edges on the same graph the blackboard reads.
  - [ADR 0052 — Structured `term` registry](./0052-structured-term-registry-parts-and-components.md)
    — the *precedent* for a structured shadow layer beside a draft that
    is authored, hover-surfaced, but not part of the published prose.
  - The `finding` kind (`src/precis/handlers/finding.py`) — the
    evidence-backed chain head this ADR reuses verbatim as the
    "empirically-grounded lemma," and the begat-detail render the
    argument-tree view is modelled on.
  - The `citation` kind (`src/precis/handlers/citation.py`) — the
    verified claim → source pointer (`verifier_confidence`) that a
    lemma's *trust* leans on.
  - `precis-relations` (the closed `link(rel=)` vocabulary) — the
    contract this ADR extends by exactly one relation.

## Context

A `draft` makes **arguments**. Today those arguments live only as prose
plus `[pc<id>]` citations: the *logical skeleton* — which claims rest on
which sources, which conclusions follow from which premises, and by what
step — is nowhere in the corpus. It exists only in the author's head (or
the model's context window) and evaporates when the turn ends.

The user's framing: while writing draft `dc123`, I cite `pc893` and
`pc999`. I want to state:

- **Lemma A**: `pc893` is trustable (a Nature paper, unretracted) and it
  claims *X*.
- **Lemma B**: `pc999` is trustable and it claims *Y*.
- **Inference**: linked to both lemmas, by some logical operation, *from
  X and Y we can infer Z*.
- In the draft, a **reasoning link** points at the inference — a
  writer's aide (for LLM and human alike), **not something published**.
- Recursively, *Z* can serve as a lemma for the next inference.

This is a **shadow structure beside the document**: a graph of small,
reusable, individually-defensible steps. The question this ADR settles
is *how much new machinery it needs* — a new kind? a formal logic? — or
whether discipline over existing primitives suffices.

### What already covers ~80% of this

- A **lemma grounded in one source** is a `finding`: claim + setup +
  a `derived-from` chain to a primary source, read as "what evidence do
  we have for X?". Or a `citation` when a verbatim quote + verifier
  confidence back it.
- An **inference node** is a `memory`: prose plus `autolink_mentions`
  (which already materialises `related-to` links to every `kind:ref`
  handle in the body) plus curated links.
- The **reasoning link** from draft to inference, and premise-to-node
  edges, are `derived-from` / `supports` / `see-also`, which already
  auto-mirror.
- The **"not published"** boundary has precedent: `finding` is *"never
  citable externally"*; ADR 0052's registry is a shadow layer.
- **Contradiction** and **retraction** already have relations
  (`contradicts`, `retracts`, `raises-concern-about`).

### What is genuinely missing

1. **A typed logical edge.** `derived-from` means "came from"
   (provenance). Nothing says *modus ponens*, *∧-elimination*,
   *therefore*. The inference's *rule* is unrepresentable.
2. **Trust vs. entailment as separate axes.** "The source is
   credible" (empirical) and "the conclusion follows" (logical) are
   different claims. `citation.verifier_confidence` covers the first;
   nothing covers the second. Conflating them is the classic
   argument-graph mistake.
3. **Invalidation propagation.** When `pc893` is retracted, every
   inference resting on it should be *flagged stale*. In-context
   chain-of-thought silently keeps the dead premise; a persisted graph
   can ripple the retraction. This is the feature the whole structure
   earns its keep on.

## Decision

### 1. No new kind (v1). Lemmas and inferences are sub-kinds of `memory`.

Apply the ADR 0030 test (does the new thing have a distinct *lifecycle*,
*worker*, or *storage shape*?). It does not: a lemma and an inference are
notes with links. Reuse `memory` with the existing `kind:` open-tag
sub-kind mechanism:

- `kind:lemma` — a stated premise ("`pc893` claims X, and I judge it
  trustable"). When the lemma is *one empirical claim from one source*,
  prefer a `finding` instead (it gets the chase + `verifier_confidence`
  for free); `kind:lemma` on a memory is for the judgment/composite case.
- `kind:inference` — a reasoning step: premises in, one conclusion out,
  tagged with the operator (§3).
- The **conclusion** of an inference, once reusable, is just another
  `kind:lemma` memory that is `entailed-by` the inference. Recursion is
  free.

Revisit a dedicated `argument`/`lemma` kind only if a node grows bespoke
fields or validation that don't fit `memory.meta` (named in §Rejected).

### 2. Add two relations to the closed vocabulary: `entails` and `qualifies`.

- **`entails`** (inverse **`entailed-by`**): "A logically yields B —
  *asserted*, not proven." Directed from an **inference node to its
  conclusion lemma**.
- **`qualifies`** (inverse **`qualified-by`**): "A limits/caveats B."
  Directed from a **caveat node to the claim it bounds** (§7).
- **Premises attach to the inference via `derived-from`** (reuse): the
  inference *was produced from* its premises. The inference node
  **reifies the conjunction** — `L_A derived-into I`, `L_B derived-into
  I`, `I entails Z` reads exactly as "from A and B, infer Z." No premise
  alone claims to entail Z; the node carries the joint step.
- `supports`/`contradicts` keep their **evidential** meaning (this keeps
  trust and entailment on different edges, gap 2).

Two relation pairs added to `precis-relations`. Everything else is
convention.

### 2.1 Relations stay **closed**; open nuance rides in edge `meta`.

The relation vocabulary is a **behavioral contract, not a label**: code
branches on the relation (`blocks` → todo filter, `supersedes` →
soft-delete, `retracts` → the ripple, `entails`/`qualifies` → the
argument view). A relation the system does not know cannot drive
behavior; it is `related-to` with extra characters. And auto-mirroring
(`cites`↔`cited-by`) *requires* the system to know the inverse pair.
Generic author-minted labels are therefore **rejected** — they would
re-import the prod folksonomy drift ADR 0047 was written to kill (52%
singleton OPEN tags; `supports`/`support`/`backs`/`evidence-for` all
meaning one thing, none co-queryable).

**Admission test for a relation:** *does code branch on it?* `entails`
and `qualifies` pass. Anything that does not is `related-to`.

**Escape hatch** (mirrors the closed-axis / open-tag split the tag
system already uses): free-form nuance — `analogous-to`, `motivates`,
`approximates` — is a `meta.note` on a `related-to` edge (links already
carry a meta envelope). Expressive, non-behavioral, and it does **not**
fragment the queryable vocabulary.

### 3. The logical operator is a **label on the inference node, not a checked proof.**

The inference memory carries `meta.rule` (e.g. `"modus-ponens"`,
`"and-intro"`, `"abduction"`, `"statistical"`, `"analogy"`) and a
free-text `meta.warrant` (why the step holds). These are **author
assertions**. precis does **not** verify validity. The value is
provenance, reuse, and invalidation — *not* automated deduction. (See
§Rejected: proof checker.)

### 4. Trust is a recorded judgment, not a computed scalar.

A lemma's trust lives as a closed tag axis **`TRUST:`** on the
lemma/finding (`TRUST:high` | `TRUST:medium` | `TRUST:low` |
`TRUST:retracted`), asserted by the author/model. It is a *judgment
about the source*, orthogonal to `entails`. It is **not** auto-derived
from venue or citation count. Retraction status, however, *does* flow
structurally: an inbound `retracts` / `raises-concern-about` edge on the
cited paper is the machine-readable trigger for §5.

### 5. Invalidation propagation — a view first, a worker later.

- **v1 (this slice): a read.** `get(kind='memory', id=<inference>,
  view='argument')` renders the proof tree (premises → rule →
  conclusion, begat-style like `finding`) **and flags any leaf whose
  cited source now carries a `retracts`/`raises-concern-about` inbound
  edge**, or any premise tagged `TRUST:retracted`. Also a corpus-wide
  report: "arguments resting on now-retracted sources."
- **phase 2 (not this slice): a worker** that walks `entailed-by`
  transitively on a new retraction edge and tags downstream nodes
  `STALE:retracted-premise` so the staleness is queryable, not just
  visible on demand.

### 6. The shadow graph never publishes.

Lemmas and inferences **never** export to `\cite{}` — same rule as
`finding` (`pub_id` is a placeholder). The draft's reader-facing artifact
stays the `[pc<id>]` citation to the *primary source*; the inference is
the writer's-aide layer behind it. A draft chunk may carry a
`meta.reasoning` handle pointing at the inference (a `see-also`-class
link) so the graph is reachable from the prose without leaking into the
export.

### 7. Caveats (rebuttals) propagate by **display**, never by logic.

The soft sibling of retraction (§5): a source's claim was *conditional*
("only validated for n < 100"; "assumes the linear regime") and the
condition gets dropped as the claim propagates, so downstream we believe
a clean claim that was never clean. The formal name is Toulmin's
*rebuttal* / a *defeater*.

- A **caveat is a node** (`memory` tagged `kind:caveat`), not a buried
  field — *because a buried field is precisely what gets forgotten; a
  graph node the argument view walks does not.* It attaches to the claim
  it limits with `qualifies` (§2).
- Distinct from `finding.scope`. Scope is the **positive** setup a claim
  *holds under* (`{ambient: N2}`); a caveat is the **negative/limiting**
  complement — where it *breaks* or is *unproven*. Do not cram caveats
  into `scope`.
- **Propagation is display-only.** `view='argument'` walks premises'
  `qualified-by` edges and lists every caveat the conclusion
  **inherited**, marked *"inherited — confirm still addressed."* precis
  surfaces; the author judges (recording the judgment in the inference's
  `meta.warrant` prose). precis **never auto-decides** whether a caveat
  still bites downstream — that is the defeasible-logic tar pit
  (§Rejected).
- **No edge-scoped discharge in v1.** A dedicated `addresses` relation
  that retires a caveat in one argument but not another is a named
  phase-2 refinement, not a launch requirement.

### 8. Division of labor: the LLM reads, the code remembers.

The recurring objection — *"code can't read the text smartly"* — is
correct and is the point. The failure this ADR targets (a caveat/
retraction *forgotten*) is a **memory-and-routing failure, not a
comprehension failure**. The LLM can judge whether a caveat still bites
— *if it is shown the caveat*. The bug is that nothing puts it in front
of the LLM, because the LLM (thinking Z is an established fact) does not
know it should go check.

So the split is hard:

- **LLM (smart, in the moment):** reads the source, writes the caveat /
  trust judgment / warrant, decides whether Z follows.
- **Code (dumb, forever):** stores the edges and, on *every* later read,
  **mechanically drags the caveat/retraction into the reader's context**
  (graph walk, no reading required) and answers *"every argument resting
  on a retracted source"* **exhaustively** (a SQL walk, not an LLM scan
  of 900k chunks that misses some and hallucinates others).

This is why the value-bearing parts (§5 ripple, §7 propagation,
`view='argument'`) **must be code, not a skill.** A skill saying
*"remember to check for caveats"* only fires if the LLM decides the
situation warrants reading it — and the whole bug is that it does not
realize it does. Code makes surfacing **unconditional**: you cannot
render the argument without the caveats riding along. The mental model
is a spreadsheet recalc (the machine propagates; you understand) or a
linter (flags every mismatch so you cannot miss one), not an oracle.

## Rejected alternatives

- **A dedicated `argument`/`lemma`/`inference` kind (v1).** Rejected: no
  distinct lifecycle/worker/storage per ADR 0030; `memory` sub-tags +
  `finding` already carry the load. A new kind is pure ceremony until a
  node needs fields `memory.meta` can't hold. Left as an explicit
  revisit trigger, not a v1 cost.
- **A formal proof checker / validity engine.** Rejected hard — a tar
  pit that would sink the feature. The operator is a *label*; validity
  is *asserted*. precis records reasoning, it does not adjudicate it.
- **A computed trust scalar** (from venue rank, citation count,
  retraction feeds). Rejected: trust is a judgment worth recording
  explicitly; automating it invites false confidence and an endless
  data-plumbing tail. Retraction *status* flows via existing edges; the
  rest is an asserted `TRUST:` tag.
- **Generic / author-minted link labels** ("provide any label you wish,
  we manage it"). Rejected: relations are a behavioral contract, not
  decoration, and auto-mirroring needs known inverses; open labels
  re-import the ADR 0047 folksonomy drift. Non-behavioral nuance goes in
  edge `meta`, not the relation slug (§2.1).
- **A defeasible-logic engine that auto-discharges caveats** (decides
  whether a rebuttal still applies downstream). Rejected hard — the same
  tar pit as the proof checker. Caveats propagate by *display*; the LLM
  adjudicates each time (§7).
- **Load-bearing `premise-of`** distinct from provenance `derived-from`.
  Deferred, not rejected: for v1 the inference node reifying the
  conjunction suffices. A named future refinement.
- **Shipping only as prose discipline / skills (no relation, no view).**
  Rejected: without the typed edges and the propagation walk the graph
  is unqueryable and retraction-ripple + caveat-inheritance — the whole
  justification for externalising the reasoning — are impossible. "Just
  skills" is not a smaller version of this feature; it is the ~80% that
  already works today and none of the 20% that earns it (§8).

## Consequences

- **Contract**: `precis-relations` gains `entails`/`entailed-by` and
  `qualifies`/`qualified-by` (two mirrored pairs). One closed-axis tag
  prefix `TRUST:` is added (validated by the tag verb). Relations stay
  closed; open nuance rides in edge `meta` (§2.1). No schema migration
  (`memory.meta` + tags + links cover it).
- **Surface**: `memory` gains `view='argument'` (proof tree + inherited
  caveats + stale-premise flags); a corpus report surfaces
  retraction-tainted arguments. Skills: new `precis-argument-help`, plus
  `precis-relations` and `precis-memory-help` updates (sketched in the
  design doc).
- **Code / skill split (§8)**: discipline + the `kind:lemma`/
  `:inference`/`:caveat` sub-kinds are skill-only (open tags, zero
  code); the two relations, the `TRUST:` axis, `view='argument'`, and
  the propagation walk are the small bounded code core (no migration).
- **Feeds 0051**: the blackboard converges over this graph — forked
  threads deposit lemmas, `contradicts` edges mark the open conflicts a
  convergence turn must resolve.
- **Does not make a single argument "smarter."** It makes arguments
  **auditable, reusable, and self-invalidating** across turns, personas,
  and drafts — the actual win.
