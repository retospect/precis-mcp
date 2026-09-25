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
way; three completed (the fourth stalled twice on infrastructure, unrelated to
content — see Open questions). Across those three: **222 evidence spans, 100%
expressed as anchors, zero fallbacks to character offsets, zero materialization
errors.** One paper's reader wrote 15.3k tokens of bindings that materialized to
46.2k of pooled record (3.0×) and 82.9k fully expanded (5.4×), because four
table recipes produced 46 of its 66 results. The same paper extracted the old
way produced 6 results and skipped its three tables — **which is a cost result,
not a quality result.** Both runs declared themselves representative rather than
exhaustive, they had different prompts and producers, and neither is a gold
standard; what it shows is that recipes price tabular data low enough to record,
not that the extraction got more correct. Nothing in this item measures accuracy
(see Explicitly NOT in scope).

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
426 escalations over three papers, of which 277 (65%) are `inferred_condition_basis`
almost all saying "Methods states the electrolyte, the result does not restate
it" — ordinary paper structure, indistinguishable in the output from a genuinely
shaky inference. Item 3 removes most of them structurally; the rest need a
severity so a strong-model audit has an order to work in.

**5. Recipe per-row / per-item field overrides.**
A recipe fixes one field block per column, so cells that differ mechanically
leak back to hand-writing: six `<1` upper-bound cells had to be lifted out of a
table recipe purely to change `value_form`. Add per-row overrides, and let the
materializer set `value_form: upper_bound` when a cell literal begins `<`.
Related: a `listed` recipe cannot bind `9.6 ± 1.7`, because numeric anchors are
single atoms — so recipes currently work on un-replicated data and fail on
replicated data, which is backwards.

**6. A `contradicts` relation.**
Nine intra-paper conflicts were found across two papers (150 vs 200 mg;
`7381.1 ± 59.5` vs `± 594.7` for the same quantity; three incompatible
definitions of "activity"). All had to be flattened into prose `gaps` with no
link to the results they qualify. One reader also could not record that the
authors *themselves* declare their electrochemical results non-reproducible —
that is a source-stated reliability claim, not an uncertainty and not a gap in
our reading.

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
  we do not infer column boundaries.
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
5. A table recipe expands a column containing both `12.3` and `<1` into results
   whose `value_form` differs, with no hand-written result.
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

- **Does the 4th pilot paper change item 5?** The stalled paper (8 well-formed
  tables, thermal catalysis, peaks at differing operating points) is the one that
  most stresses recipe overrides and temperature-as-real-condition. Two runs
  stalled on infrastructure before producing output; a third is running. Re-read
  item 5 when it lands. Items 1–4 do not depend on it.
- **Is a chunk-level extraction a new kind, or a finding subtype?** Reusing the
  existing findings infrastructure is an invariant; whether the intermediate
  record needs its own kind is undecided and gates the migration question.
- **Where does the reader pass run?** In-process on melchior like the other
  claude passes, or as a queued job. Affects cost and the escalation loop.
- **Escalation loop shape.** Who adjudicates the sampled audit — a strong model
  pass, a human queue row, or both in sequence.
