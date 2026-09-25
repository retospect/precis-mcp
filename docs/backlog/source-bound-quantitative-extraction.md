---
status: draft
title: Source-bound quantitative extraction — bind anchors, materialize records
prio: normal
---

# Source-bound quantitative extraction — bind anchors, materialize records

## Motivation / why

We want sourced numeric results out of papers: a measurand, an exact reported
literal, its conditions, and evidence that points at the source rather than
paraphrasing it. The obvious shape — ask a capable reader to emit the finished
record as JSON — makes the reader copy quotations, units and boilerplate enum
fields into every row. That is expensive, and the copying is where fidelity is
lost: a pasted quotation can be silently cropped mid-literal, and a shared
condition can be restated per result until nobody can tell which results it was
actually demonstrated under.

A local pilot (untracked `paper-extraction-pilot/`, code committed without its
source-paper text) worked the alternative: the reader validates a *semantic map*
of one document and emits **bindings** — anchor ids instead of quotations,
shared defaults with explicit per-result exceptions, and recipes that expand
ordinary tables. A script materializes the full record by slicing the snapshot.

Measured over 20 papers, the binding encoding is 35% smaller than the pooled
record it reproduces byte-for-byte, and 67% smaller than the fully-expanded
form. Four fresh reader runs then tested whether a reader can actually work this
way. All four completed with **zero materialization errors**, producing 224
results from 451 evidence spans, of which **97.6% are anchor ids and 11 (2.4%)
are raw character offsets** — every one of those in the same paper, and every
one of them a sub-sentence interval literal (`550–575`, `125~150`) that no
anchor can address without cropping the value. That residue is not reader
sloppiness; it is a missing anchor granularity, and it is item 5 below.

Amplification from what the reader writes to the materialized record is 1.8–3.0×
against the pooled form and 5.2–7.0× against the fully expanded one. The best
case is the paper whose four table recipes produced 46 of its 66 results: 15.3k
of bindings → 46.2k pooled → 82.9k expanded. That same paper extracted the old
way produced 6 results and skipped its three tables — **which is a cost result,
not a quality result.** Both runs declared themselves representative rather than
exhaustive, they had different prompts and producers, and neither is a gold
standard; what it shows is that recipes price tabular data low enough to record,
not that the extraction got more correct. Nothing in this item measures accuracy
(see Explicitly NOT in scope).

Two behaviours worth keeping in the design: readers corrected the selector
rather than trusting it (one recovered two blocks dropped with *zero* flags, one
carrying the paper's basis for its central mechanistic claim), and one caught
and repaired a wrong span of its own during self-verification. Both are load
bearing — the map is a proposal, and the check loop is what makes a fallible
proposal safe to start from.

The pilot also surfaced defects that only appear once real binding documents
exist, and those are what this item is mostly about. They are listed under
In scope in priority order because the first one is a correctness bug, not a
feature gap.

## In scope

**1. Version the anchor scheme (correctness — do this first).**
Anchor ids are positional: `c.n` means "the n-th sentence span of chunk c". They
are only meaningful under the splitter that produced them. Changing the splitter
re-points existing anchors at *different source text* while the source
fingerprint still matches, so nothing catches it. Measured on the three pilot
documents, a one-line splitter improvement silently re-pointed **35 of 222
anchors (16%)** to different text and hard-failed another 18. The pilot now
carries `ANCHOR_SCHEME`, emits it on maps and binding documents, and refuses to
materialize a document whose declared scheme differs — **or that declares none
at all**, since an unstamped document has no provenance for its anchors and
defaulting it to the current scheme would assume exactly what the guard checks.
(The first cut of this guard got that wrong and failed open on a missing field;
review caught it.) Port the guard, fail closed on absence, and treat the scheme
constant as append-only.

**2. Give conditions an explicit role (`operating` | `preparation` | `model`).**
Today the distinction survives only inside a free-text condition *name*, and the
"is a temperature visible?" guard is a keyword match over that name. Both
consequences were observed: one reader named a condition "post-laser heat
treatment", the guard saw no temperature and let it through; renaming it to
contain the word "temperature" made it fire. A second reader worked out that
attaching its 80 °C PET-hydrolysis pre-treatment would have *silently satisfied*
the guard and suppressed the true "no operating temperature stated" signal — so
it left a real condition unattached and filed a gap instead, losing information
to stay honest. The guard is unsound as built; a role field makes it decidable.

**3. Group-level condition sets with per-result exceptions.**
A condition item is (condition, basis, applicability-evidence), so giving every
result its own applicability basis is quadratic: 31 conditions became 224 items
on one paper, 46 became 146 on another. Both readers independently proposed the
same fix, and one noted the rule as written ("every result selects its own
conditions with its own applicability basis") is *not reachable* through a table
recipe at all. Inherit a condition set at group level, override per result, same
mechanism as `defaults.results`.

**4. Rank escalations; stop drowning the auditor.**
503 escalations over four papers, of which 312 (62%) are `inferred_condition_basis`
almost all saying "Methods states the electrolyte, the result does not restate
it" — ordinary paper structure, indistinguishable in the output from a genuinely
shaky inference. A further 25 are a single matcher bug (item 7) and 11 are the
unavoidable interval spans of item 5, so well over two thirds of what an auditor
would read is noise. Item 3 removes most of it structurally; the rest needs a
severity so a strong-model audit has an order to work in. A related blind spot:
where a condition is *defined and applied in the same clause* ("below 200 °C"),
its applicability evidence necessarily repeats its definition, and the
`applicability_not_shown_for_this_result` check cannot tell that apart from
genuinely unsupported applicability.

**5. Sub-sentence anchor granularity, and recipe per-row overrides.**
Two faces of one gap: the anchor vocabulary cannot address a fragment, so an
exact interval literal (`550–575`, `125~150`, `80~400`) can only be bound with
raw `[chunk, start, end]` offsets — which the `sub_sentence_span` check then
flags. All 11 offset spans and all 11 such flags in the round are this, and none
is avoidable without cropping the value. **The check currently penalises the only
correct encoding.** Add a fragment anchor (a numeric-region span that can cover a
range expression, or an explicit sub-sentence id) so intervals are anchorable.
Then, on recipes: a recipe fixes one field block per column, so cells that differ
mechanically leak back to hand-writing — six `<1` upper-bound cells had to be
lifted out purely to change `value_form`. Add per-row overrides, and let the
materializer set `value_form: upper_bound` when a cell literal begins `<`. A
`listed` recipe also cannot bind `9.6 ± 1.7`, because numeric anchors are single
atoms, so recipes work on un-replicated data and fail on replicated data, which
is backwards. Finally, a table recipe binds one unit per column, so a table that
carries its units in a *third column* materializes its results with
`reported_unit: null`.

**5b. `reported_value` is numeric-only, which silently drops categorical rows.**
A spec table whose rows are "Pd", "γ-Al2O3, Ce0.5Zr0.5O2", "Cordierite" cannot
produce results at all; one reader could only preserve them as prose inside an
assertion's limitations, which is not queryable. Either admit a categorical
value form or define where non-numeric sourced facts live — right now they are
lost by construction, quietly.

**6. A `contradicts` relation, and a way to say "stated but unattributed".**
Nine intra-paper conflicts were found across two papers (150 vs 200 mg;
`7381.1 ± 59.5` vs `± 594.7` for the same quantity; three incompatible
definitions of "activity"). All had to be flattened into prose `gaps` with no
link to the results they qualify. One reader also could not record that the
authors *themselves* declare their electrochemical results non-reproducible —
that is a source-stated reliability claim, not an uncertainty and not a gap in
our reading. A third case: two activation energies (95 and 77.6 kJ/mol) are
stated with no citation and no method, and the only available encoding is
`not_established` on all three of evidence_mode / value_generation /
source_attribution — which *understates* what is known, namely that the number
is asserted by this paper and unattributed. "Unattributed assertion" is a
distinct epistemic state from "we could not establish it".

**7. Small verified bugs.** `expansion_directory` is built from a hardcoded path
that ignores the map label (`bindings.py`). The sentence splitter breaks after
`ca.`, `Fig.`, `satd.`, inflating anchor counts and forcing runs — fix under a
new anchor scheme, never in place. `pilot.unit_has_text_support` false-negatives
a `%` unit when the source glues it to a word (`X% NO3`): its letter-lookaround
is right for `mA`, wrong for `%`. That one matcher produced all 25
`unit_without_text_support` escalations on one paper.

**8. Port to `src/precis/` with the right seams.** Map builder, materializer and
deterministic checks are pure functions over chunks — library code, no agent. The
reader call belongs in a worker pass alongside the existing synthesis passes. The
MCP surface is thin reads plus an approve path; the checks and materializer are
NOT MCP verbs.

## Explicitly NOT in scope

- **Scientific adjudication.** Nothing here approves a result. Output stays
  proposals, and integration goes through the existing findings/nanopub path —
  no parallel signing or publication system.
- **Accuracy measurement.** The pilot establishes feasibility, not precision or
  recall. No held-out set and no human-adjudicated benchmark exists; do not let
  the 5.4× amplification or the 66-vs-6 comparison be read as a quality score.
  Both papers in that comparison were already inspected in an earlier round, and
  the two runs had different prompts and producers.
- **Cross-paper work.** Identity resolution across papers, a curated measurand
  ontology, and any comparability engine are all separate and larger.
- **OCR, supplements, figure digitisation, curve refits.** A table that is an
  undelimited character run (`L-Pd/rGO0.03055454.61`) stays `ocr_uncertainty`;
  we do not infer column boundaries. Two of the four pilot papers hit this — a
  collapsed header and a fully undelimited table — and in both the reader
  correctly promoted nothing. Recipes only reach well-formed pipe tables, and
  `table_shape` being non-null does not mean a table holds results: of eight
  shaped chunks in one paper, one held results, three held designed setpoints
  (bound as conditions), three were glossaries and one was OCR-broken.
- **The information-gain research** in `paper-evidence-selection-fisher-rao.md`
  and prod todo td449041. Do not let this item quietly become that project.

## Acceptance criteria

1. A binding document written under anchor scheme N refuses to materialize under
   scheme N+1, **and a document declaring no scheme is refused too**, with a
   test that demonstrates the silent-repoint failure the guard prevents.
2. A condition carries an explicit role; a `preparation` or `model` temperature
   can be attached to a result without satisfying the operating-temperature
   check, and a result with no operating temperature still escalates.
3. A condition set declared at group level and inherited by N results produces
   N materialized results each carrying it, with a per-result override changing
   exactly one of them; item count is linear, not quadratic, in a test fixture.
4. Escalations carry a severity; a fixture where the only issue is "Methods
   states it, result does not restate it" ranks below one with a genuinely
   unsupported condition.
5. An interval literal (`550–575`) is bindable by anchor, materializes with the
   literal intact, and does NOT raise `sub_sentence_span`.
6. A table recipe expands a column containing both `12.3` and `<1` into results
   whose `value_form` differs, with no hand-written result; a table carrying its
   units in a separate column materializes with a non-null `reported_unit`.
6. Two conflicting literals from one paper can be bound to each other as a
   first-class relation and survive materialization.
7. `scripts/test` green; the three verified small bugs each have a regression
   test.

## Target + blast radius

New library module under `src/precis/` (map/materialize/check), a new worker
pass, a migration if extraction records become a kind. Touches the findings
path at integration. Nothing in this item writes to prod, signs or publishes.
The pilot code is the reference implementation, not the port: it lives in an
untracked directory and its data must never enter a shipping commit.

## Open questions / decisions log

- ~~**Does the 4th pilot paper change item 5?**~~ **Resolved — yes, it widened
  it.** That paper (thermal catalysis, 8 shaped tables, peaks at differing
  operating points) landed on the third attempt after two infrastructure stalls.
  It contributed all 11 character-offset spans in the round, all of them exact
  interval literals, which turned item 5 from "recipe ergonomics" into a missing
  anchor granularity plus the finding that `sub_sentence_span` penalises the only
  correct encoding. It also produced 5b (categorical rows), the units-in-a-third-
  column case, and the "stated but unattributed" gap in item 6. It confirmed
  item 2 from the other direction: it kept three real preparation temperatures
  (preheat, premix tank, heating belt) in a condition set referenced by **no**
  result — a deliberate orphan the format cannot mark as intentional.
- **Is a chunk-level extraction a new kind, or a finding subtype?** Reusing the
  existing findings infrastructure is an invariant; whether the intermediate
  record needs its own kind is undecided and gates the migration question.
- **Where does the reader pass run?** In-process on melchior like the other
  claude passes, or as a queued job. Affects cost and the escalation loop.
- **Escalation loop shape.** Who adjudicates the sampled audit — a strong model
  pass, a human queue row, or both in sequence.
