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

- `kind:lemma` — a **derived or composite** premise: a conclusion of an
  inference, or a sourceless interpretive claim. The *internal node* of
  the graph.
- `kind:inference` — a reasoning step: premises in, one conclusion out,
  tagged with the operator (§3).
- The **conclusion** of an inference, once reusable, is just another
  `kind:lemma` memory that is `entailed-by` the inference. Recursion is
  free.

Revisit a dedicated `argument`/`lemma` kind only if a node grows bespoke
fields or validation that don't fit `memory.meta` (named in §Rejected).

### 1.1 Lemma vs finding: no merge — one boundary the code already draws.

A lemma comes in **two roles, not two flavours of one kind**:

- **Grounded lemma = `finding`.** The *leaf* where the argument touches
  the literature: a claim pinned to a single corpus source, with the
  chase worker, dedup identity, and `tracing→established` lifecycle. If
  it has one `cited_in`, it **is** a finding — never a `kind:lemma`
  memory.
- **Derived/composite lemma = `memory` tagged `kind:lemma`.** The
  *internal node*: a conclusion (no single source) or a sourceless
  judgment.

The routing test is one question — *does it have a single corpus
source?* — and **`finding.put` already enforces it**: a sourceless
`put(kind='finding')` is refused today with *"If this is your own
synthesis with no single source, it is NOT a finding — record a memory
instead"* (`handlers/finding.py`). A conclusion `Z` of `X ∧ Y → Z` is
exactly that sourceless synthesis, so it *cannot* be a finding by the
existing rule. The boundary needs no new code; it is the finding
handler's existing write-time guard, reused. `finding` keeps its chase
lifecycle (0030 kind test); merging would overload it with argument
semantics it does not need (it is also used standalone for citation
chasing). A derived lemma that later acquires a primary source
*graduates* to a finding via `derived-from`/supersede — a path, not a
merge.

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
behavior; it is `related-to` with extra characters. And the **read-time
inverse rewrite** (`links_for` answering a `cited-by` query against
stored `cites` rows via `_INVERSE_RELATIONS` — edges are stored one row
per pair, *not* mirrored at write time) *requires* the system to know
the inverse pair.
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

### 4. Trust = the absence of a concern edge. No `TRUST:` axis in v1.

An asserted `high|medium|low` ordinal is unbacked and drift-prone — the
same theatre §Rejected warns about for a *computed* scalar, merely moved
to a human. So **v1 ships no `TRUST:` tag axis at all.** A source is
trusted **by default** (peer-reviewed, unretracted); the only signal
that matters is *distrust*, and distrust always carries a **reason**:

- a structural `retracts` / `raises-concern-about` edge on the paper
  (machine-checkable, the §5 trigger), or
- a `kind:caveat` node (§7).

Both carry content a bare ordinal lacks. Trust is therefore *derived*
(absence of concern edges + any inherited caveats), surfaced by the
`view='argument'` walk, never a tag on the node. An ordinal is revisited
only on a concrete ranking need (§Risks R3).

### 5. Invalidation propagation — push on the retraction edge, pull everywhere else.

The walk is **kind-scoped**: it traverses only `finding` / `kind:lemma`
/ `kind:inference` nodes, so `derived-from` reused for chase/summary
provenance is never mistaken for a premise edge (this is why no
`premise-of` relation is needed — §Risks R2). A premise is simply a
`derived-from` edge *into a `kind:inference` node*.

Two triggers, covering both event orderings, **both in v1**:

- **Write-time push (link hook).** When a `retracts` /
  `raises-concern-about` edge is created, a bounded kind-scoped walk of
  `entailed-by` tags existing downstream inferences
  `STALE:retracted-premise`. A link-handler hook, not a background
  sweep. Catches arguments that already rest on the now-dead source.
- **Read-time pull (the view).** `get(kind='memory', id=<inference>,
  view='argument')` renders the proof tree (begat-style like `finding`)
  and flags any leaf whose cited source carries an inbound
  `retracts`/`raises-concern-about` edge. Backstop for arguments built
  *after* the retraction. Plus a corpus report: "arguments resting on
  now-retracted sources" (exhaustive SQL walk).

The `STALE:retracted-premise` marker is a **system-set axis**
(`tag_prefixes.writable_by='system'`, alongside `SRC`/`CACHE`/`DENSITY`
in `_SYSTEM_WRITABLE_PREFIXES` — *not* `DREAM:`, which is agent-written),
so the tag verb refuses author add/remove. It is **derived, recomputed
not toggled**: every
retraction-edge add *or* remove reruns the bounded walk and sets/clears
the flag to match reachability, so removing the last retracting edge
clears it while a second still-reaching retraction keeps it. It is
**advisory** (surfaced + queryable), never a blocking `STATUS:`
transition, and transitive by construction (the walk rides the
inference→conclusion→next chain).

**Deferred (phase 2):** a periodic reconciliation sweep, and *push* for
caveats (a new `qualifies` edge rippling downstream). Caveats are
pull-only in v1 — surfaced by the view, not pushed (§7, §Risks R4).

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
- **Edge-scoped discharge is phase 2, shape decided.** A caveat
  neutralised in one argument but not another is a *(inference, caveat)*
  property, so an inference records the caveats it addresses in
  **`meta.addresses` (a list of caveat handles)** — edge-scoped by
  construction, **no global 'addressed' tag** (would wrongly clear it
  everywhere) and **no third relation** (meta, not vocab; the view
  already reads the inference `meta`). The view then renders "addressed
  here (see warrant)" vs "inherited — confirm." Promote to a typed
  `addresses`/`addressed-by` relation only if reverse-query ("all
  inferences discharging C") ever becomes load-bearing — it is not now.

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
  Not needed: kind-scoping the walk (premise = `derived-from` into a
  `kind:inference` node) disambiguates structurally (§5, §Risks R2).
  Revisit only to rank premises *within* one inference — a far-future
  refinement, not a contract change.
- **An asserted `TRUST:` ordinal** (`high|medium|low`). Cut from v1: the
  same unbacked-theatre failure as the computed scalar, moved to a
  human. Trust is the absence of concern edges; distrust is a
  `raises-concern-about` edge or a caveat, both carrying a reason (§4).
- **Shipping only as prose discipline / skills (no relation, no view).**
  Rejected: without the typed edges and the propagation walk the graph
  is unqueryable and retraction-ripple + caveat-inheritance — the whole
  justification for externalising the reasoning — are impossible. "Just
  skills" is not a smaller version of this feature; it is the ~80% that
  already works today and none of the 20% that earns it (§8).

## Consequences

- **Contract**: adding the two relation pairs is a **three-place sync** —
  the `Relation` Literal (`store/types.py`), the `_INVERSE_RELATIONS`
  map, and a **new forward migration** seeding the `relations` rows (the
  established per-pair pattern, e.g. `0054_datasheet_of_relation.sql`).
  One **new *system-set* tag axis `STALE:`** (author-facing `TRUST:`
  stays cut — §4): register in `_CLOSED_VOCAB` +
  `_KIND_ALLOWED_AXES['memory']` + `_SYSTEM_WRITABLE_PREFIXES`, with a
  `tag_prefixes.writable_by='system'` seed in the same migration. So
  **v1 does need one migration** (relations + `STALE:` seed);
  `memory.meta` / `meta.addresses` are the only migration-free parts.
  Relations stay closed; open nuance rides in edge `meta` (§2.1).
- **Surface**: `memory` gains `view='argument'` (proof tree + inherited
  caveats + stale-premise flags), a link-write hook on
  `retracts`/`raises-concern-about` (kind-scoped push tagging
  `STALE:retracted-premise`), and a corpus report. Skills: new
  `precis-argument-help`, plus `precis-relations` and
  `precis-memory-help` updates (sketched in the design doc).
- **Code / skill split (§8)**: discipline + the `kind:lemma`/
  `:inference`/`:caveat` sub-kinds are skill-only (open tags, zero
  code); the two relations, the `STALE:` axis, `view='argument'`, the
  retraction push hook, and the kind-scoped walk are the bounded code
  core, gated on **one migration** (relations + `STALE:` seed).
- **Feeds 0051 (0051 *pulls*)**: 0054's contract ends at recording
  `contradicts` edges between lemmas (a *reused* relation) plus an "open
  conflicts in this argument graph" report entry. The blackboard, when
  built, queries that surface and decides what to raise — surfacing is
  an attention/TTL concern only 0051 can judge. **No blackboard-specific
  hook in 0054** (pushing would hard-wire the argument graph into the
  turn-taking layer); same producer/consumer seam as 0053's §10
  comparison board. The blackboard adds zero to 0054's build.
- **Does not make a single argument "smarter."** It makes arguments
  **auditable, reusable, and self-invalidating** across turns, personas,
  and drafts — the actual win.

## Risks resolved before build

- **R1 — adoption.** Sparse & opt-in: grounded lemmas come free from
  `finding`s; an inference node is created **only at a genuinely
  contestable step**, not per sentence (finding-style spin discipline).
  Auto-extraction is a **phase-2, propose-not-commit** investigation.
  *Acceptance signal:* graph still empty after N real drafts ⇒ the
  feature failed — measured, not assumed.
- **R2 — `derived-from` double duty.** Resolved by kind-scoping the walk
  (§5); no `premise-of`, contract stays at two relations.
- **R3 — `TRUST:` theatre.** Resolved by cutting the axis (§4); trust is
  structural (absence of concern edges) + caveats. Removes a build step.
- **R4 — pull-only ceiling.** Resolved for the high-value retraction
  case by the v1 write-time push hook + read-time backstop (§5). A
  background sweep and caveat-push stay phase 2.
- **R5 — `STALE:` mechanics.** System-set axis
  (`writable_by='system'`, like `SRC`/`CACHE`/`DENSITY`), not
  author-tunable; derived/recomputed on every retraction-edge add *or*
  remove; advisory, non-blocking; transitive by construction (§5).
- **R6 — edge-scoped caveat discharge.** Phase 2; shape fixed as
  `meta.addresses` on the inference (no global tag, no third relation)
  (§7).
- **R7 — blackboard hook.** 0051 *pulls*; 0054 exposes the `contradicts`
  edge + an open-conflicts report and adds no hook (Consequences).
