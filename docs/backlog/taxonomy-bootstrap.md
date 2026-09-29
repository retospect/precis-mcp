---
status: draft
title: Taxonomy bootstrap — generate the campaign measurand list and earn `systematic` from corpus usage, no hand-maintained list
prio: normal
blocked-by: term-taxonomy
---

# Taxonomy bootstrap — the measurand list is a generated, versioned artifact

Decided with Reto 2026-09-28 (worktree paper-extraction). Replaces step 2 of
`norr-her-meta.md` ("Reto signs off a seven-entry list"). Reto's objection,
verbatim in spirit: "I worry that this is a static I-sign-off-on-a-list and it
becomes a SQL table thing." So: Reto signs off a **procedure and thresholds**;
the list is an output anyone can regenerate from a corpus snapshot, and every
entry carries the mentions and hubs that put it there.

This item also owns the **usage-promotion pass** that `term-taxonomy.md`
explicitly leaves to "the extraction tree": every seeded taxon node lands
`proposed` and earns `systematic` only here.

## Motivation / why

`norr-her-meta` needs a closed measurand list before its 20-paper round, and
`term-taxonomy` needs a path from `proposed` to `systematic` that is not a
human approving rows. Both are the same computation: count how the corpus
uses terms, decide which usages are systematic, freeze the result with
provenance. Done by hand the list encodes one person's expectations (the
census showed 150 hubs stating a potential with no reference electrode and
yield rates in three normalisations in one sentence — rules decided after
extraction get decided to fit the data). Done as a procedure it is
pre-registration: the list version is frozen before a round, deviations are
logged as escapes, and a changed list is a new version with a diff.

Domain neutrality is a requirement, not a nice-to-have (Reto: "think
sociology numbers"). Nothing below knows chemistry; the census tokenizer is a
generic number+unit grammar, discovery is open-vocabulary, and the only
chemistry lives in campaign configuration files.

## Design — five stages, each rerunnable

1. **Census (deterministic).** Input: a frozen hub/chunk snapshot identified
   by row count + sha256 of the dump + pull time (UTC). Output: one mention
   per number+unit hit — source ref_id, chunk anchor (scheme-stamped), literal,
   raw unit string, ±60-char context. Reference-state phrases (`vs RHE`,
   `pre-tax`, `2015 USD`) are their own mention type. No measurand names.
   **`pint` decides what counts as a unit**, with two guards it needs and does
   not provide. The candidate token after a number is a unit if the registry
   (plus the campaign definitions file) parses it, otherwise the mention is
   emitted unitless — no unit word list in the code, which is what keeps the
   grammar domain-neutral. But pint alone is **not** a sufficient arbiter, as
   the first real run showed: its SI-prefix machinery manufactures valid single
   units out of ordinary words (`has` = hecto+are = hectare, `RR` =
   ronna+molar-gas-constant, `nanoparticles` = nano+particle, `D` = debye), so
   there is no parse failure to detect. Hence (a) a domain-neutral **word
   boundary rule** — a number immediately preceded by a letter or digit is part
   of a larger token (a formula subscript, a material code) and is not a value
   at all; and (b) a campaign-level `glued_unit_denylist` for jargon glued to a
   number, since whether `D` means debye or "two-dimensional" is a property of
   the corpus, not of the language.
   Same input ⇒ byte-identical output. The hand-written
   `~/.claude/projects/-Users-reto-precis-mcp/norr-her-meta/census.py` is a
   chemistry-regex draft of this stage and is NOT the implementation.
2. **Discovery (model, open vocabulary).** Hubs are split into halves A and B
   *before* the run. Per hub sentence with its mentions marked, Sonnet returns
   per mention: measurand string, dimension (7-int SI vector or non-SI
   `dimension_kind`), reference state, convention, normalisation basis,
   conditions the value depends on, subject label. Output rows become
   `proposed` taxon nodes (or mention counts on existing ones) with anchors.
   Haiku is not acceptable here — the survey's Haiku classification pass was
   ~50% wrong and discarded.
3. **Normalisation (deterministic + judge).** Lexical alias merge (case,
   punctuation, Unicode sub/superscripts, US/UK spelling). Unit strings →
   dimension via pint plus a small campaign definitions file (mV/dec, ppm,
   wt%, mA cm⁻²). Dimension mismatch is a hard gate: TOF as s⁻¹ and TOF as µs
   never reach the judge. Near-duplicates above the embedding cutoff go to one
   judge call with definitions + dimension. Any cluster whose members disagree
   on reference state is split, not merged. v1 emits merge *suggestions*
   (term-taxonomy v1 has no merge-as-redirect); they are applied by the
   `dedup` door when it exists.
4. **Selection (rule-based, thresholds signed off).** A measurand enters a
   campaign list when it clears the thresholds below, and a taxon node earns
   `systematic` when it clears the usage test. Selection reads only
   `systematic` nodes.
5. **Freeze + version.** `list.v<N>.yaml`: version, snapshot identity,
   thresholds, procedure version, and per entry the mentions/hubs/cells that
   put it there. Diffable; rerun on a grown corpus produces a diff, a changed
   list is a new version. Every binding document and every measures row
   records the list version it was extracted under (same discipline as
   `ANCHOR_SCHEME`).

### Thresholds — adopted as defaults 2026-09-28 (Reto: "continue")

| knob | default | meaning |
|---|---|---|
| usage test for `systematic` | ≥3 independent papers, present in both halves, dimension-consistent | node status only — asserts *systematic usage*, never truth |
| list inclusion | ≥30 hubs in the campaign's target cells | per measurand |
| join requirement | ≥15 hubs on each side | for any entry plotted experimental vs computational |
| stability | ≥0.80 vocabulary overlap A vs B, mention-weighted | below this: fix the prompt, do not proceed |
| escape rate | >10% of a paper's values fit no entry | forces list regeneration for the next round, never a hand patch |
| discovery model | Sonnet | see stage 2 |

### Generated subject axes (the half of `term-taxonomy` that is computed)

Pure library function, subject label in → memberships per axis out, each
membership a `specialises` edge proposal with `meta.axis`:

- **composition** — `ase.formula.Formula` (core dep) for integer
  stoichiometry; a pre-pass handles fractional subscripts (`Ce0.5Zr0.5O2`),
  class suffixes (`NiFe-LDH`, `Cu-MOF`) and support notation (`Pd/C`). Derived
  classes: alloy / bimetallic / ternary by element count.
- **periodic** — static 118-row table shipped in-tree (group, period, block,
  series, PGM, transition metal …). NOT mendeleev/pymatgen: both sit in the
  `[estimate]` extra prod does not install (see `deploy-extras-gap`).
- **termination** — facet grammar for `(hkl)`/`{hkl}` per crystal system,
  plus site classes (edge, step, vacancy, dual-atom).
- **domain classes** — the only curated rows: the survey's 10 `catalyst:`
  families plus MOF, COF, LDH, MXene, SAC-on-carbon, amorphous (~30 nodes),
  seeded `proposed` like everything else.

### First-run measurements (2026-09-28, stage 1 on the 1664-hub snapshot)

Recorded so they are not re-derived, and because the right-hand column is the
base rate any later change to stage 1 must be compared against.

| quantity | before the guards | after |
|---|---|---|
| mentions | 7093 | **4694** |
| of which `value` | 6218 | **3819** |
| values with a resolvable unit | 3251 (52%) | **3048 (80%)** |
| distinct raw units | 182 | **157** |
| spurious units from formula subscripts | 176 | **3** |

The removed 2399 value mentions — 38.6% of the original — were formula
subscripts and material codes (`NO3RR`, `Ti3C2`, `H2`, `g-C3N4`, `D2O`,
`HSE06`, `Re30/CNT30-F800`), sampled to confirm not one was a measurement.
Without the boundary rule two of every five "values" reaching the paid
discovery stage would have been stoichiometry.

The 3 surviving false positives are `60 C` written for 60 °C without the degree
sign, read as coulombs. Not fixable by the glued denylist (the form is spaced,
and coulomb is a real unit), 3 of 3048, and stage 3's dimension gate separates
charge from temperature anyway — left alone deliberately rather than patched.

Top units (`%` 558, `V` 440, `mA cm⁻²` 298, `mV` 272, `eV` 263, `h` 201,
`mV dec⁻¹` 131, `M` 126, `°C` 56) match the hand-written regex census's
buckets, so stage 1 reproduces the earlier result with no chemistry in the
code.

## In scope

- Stages 1–5 as a library module + CLI under `src/precis/` (module name
  decided at `/ready`), campaign config as YAML files, tests per stage.
- The usage-promotion pass (`proposed` → `systematic`) with the demote verb
  as the human veto; both logged.
- The generated subject axes above, with tests on the labels in the
  norr-her-meta census (PdCoP, Pt3Ni, Mo2C, Ce0.5Zr0.5O2, NiFe-LDH, Pd(111),
  Cu-foam@mesh).
- A derived, never-stored **term-health** read: share of a node's values with
  a stated reference, dimension consistency across mentions, open
  polysemy/inconsistency annotations (findings linked to the node),
  asserted-vs-measured share.
- First run: the `norr-her-meta` hub snapshot (1664 hubs); output compared
  against the seven hand-written entries in `norr-her-meta.md` as the
  baseline.

## Explicitly NOT in scope

- **Truth.** `systematic` means "used systematically in the corpus". Validity,
  polysemy ("activity" has three incompatible definitions) and consensus are
  `finding` annotations linked to the node through the existing
  verdict/dispute path. No trust column on nodes — value trust is the
  measures row's tier, claim trust is the finding's verdict.
- The `taxon` kind, `specialises` edges, contracts, start nodes — `term-taxonomy`.
- `measures` rows, `experiment`, unit conversion — `measures-substrate`.
- Merge-as-redirect and alias repointing — deferred by term-taxonomy v1;
  stage 3 emits suggestions only.
- Embedding neighbours as a decision input. Nodes are embedded (free via the
  card chunk) for *exploration* (siblings of PBE-D3 in embedding space vs in
  the DAG is the brainstorming read Reto wants); embeddings never promote or
  merge on their own.
- Reading full papers. Discovery runs on hub sentences (~1664, a few USD);
  paper text is the 20-paper round's job.

## Acceptance criteria

1. Census on the same snapshot twice yields identical sha256; the snapshot
   record (count, hash, time) is embedded in the output.
2. Discovery on halves A/B reports vocabulary overlap; a fixture below 0.80
   fails the run with the prompt named as the fix target.
3. A node with mentions in 2 papers stays `proposed`; with 3 papers in both
   halves and one dimension it becomes `systematic`; with 3 papers and two
   dimensions it stays `proposed` and is reported.
4. `list.v1.yaml` for norr-her-meta exists, every entry lists its
   contributing hubs, and a rerun on a snapshot with 10% more hubs produces a
   diff rather than an overwrite.
5. `TOF` resolves to two nodes; a mention with unit s⁻¹ binds to
   turnover-frequency, one with µs to time-of-flight, one with no unit
   escalates.
6. Subject axes: `PdCoP` → {Pd, Co, P, **ternary-compound**}; `Pt3Ni` →
   {Pt, Ni, binary-alloy} — `alloy` only when every element is a metal, which
   is why this pair is the test (corrected 09-28: the first draft of this AC
   said `ternary-alloy` for PdCoP, and P is not a metal). `Pd(111)` →
   composition Pd AND termination fcc-111 (two parents, two axes);
   `Ce0.5Zr0.5O2` parses; widening from `Pd(111)` along composition reaches
   `Pd` and never `Cu(111)`. Stated precisely, because the loose version is
   false: the two *do* legitimately share the composition class parent
   `unary-alloy` (both are single-metal formulas) and the termination parent
   `fcc-111`. What must not leak is **element identity** — no composition edge
   puts `cu` in `Pd(111)`'s parent set.
7. A non-chemistry fixture (unemployment rate, 2015 USD per capita) passes
   stages 1–4 with `required_conditions` year + population definition and
   currency flagged `dimension_kind=currency`, never SI-zero.
8. `scripts/test` green; no chemistry string outside the campaign YAML.

## Target + blast radius

New library module + CLI under `src/precis/`; writes `taxon` nodes and
`specialises` edges through term-taxonomy's door once it ships (until then,
stages 1–5 write files under the campaign scratch dir). Reads prod hubs
read-only via `scripts/prod-psql`. No change to `measures`, findings, or the
worker. Touches `deploy/` only if the static element table needs packaging.

## First discovery probe — 2026-09-29, 100 rows / 66 paid calls

`precis taxonomy-bootstrap --stage all --limit 100` on `norr-her-meta`
(snapshot v2 `ec77e84f`, salt default). 270 mentions, 204 value, 204 discovered
rows, **0 warnings**, 182 nodes, 0 systematic, stability 0.046, 36m29s wall.
Stage dumps under the scratch `norr-her-meta/taxonomy/`.

**What the probe proved works.** One row per value mention, 204/204, no
invented indices and no malformed JSON across 66 calls — the stage-2 index and
JSON contracts hold on real output. The qualifier fields are genuinely
answered, not left null: `required_conditions` 136/204, `normalisation_basis`
93, `convention` 90, `reference_state` 77.

**Blocker 1 — dimension wiring (a bug, not a tuning problem).** `normalise`
passes the model's prose `dimension_text` into `resolve_dimension`, whose
parameter is `raw_unit` and which parses with pint. The model returns physical
*quantity names* (`potential`, `mass per time per area`, `current per geometric
electrode area`), which pint cannot parse, so **5 of 182 nodes resolved a
dimension** and 177 rejections read `no dimension resolved`. The mention's own
stage-1 unit (`V`, `%`, `mA cm^-2`; 163/204 = 80% coverage) is present on the
term and unused. Dimension is a hard promotion gate and part of compare
identity, so nothing can promote until this is fixed. **Fix: resolve from
`term.mention.raw_unit` — the observed unit in the corpus — and demote
`dimension_text` to a cross-check.** The 159 green tests missed it because
every fixture sets `dimension_text="V"` / `"1/s"`: the fixtures encoded the
author's belief about the model's output rather than its output. A regression
test must use real prose (`"potential"`).

**Blocker 1 is FIXED (2026-09-29).** `normalise` resolves from
`term.mention.raw_unit`; `dimension_text` stays on the row as audit data and is
never parsed. Measured by replaying stage 3 over the probe's saved
`discovered.jsonl` — free, no model calls — so the fix is validated against real
model output and not only against fixtures: **dimensions resolved 5/182 -> 146/184**
(109 si, 37 dimensionless), and **merge suggestions 0 -> 35**, because that gate
requires comparable dimensions and was dark for the same root cause. Read the
earlier "0 merge suggestions" line above as a symptom of blocker 1, not as
evidence about vocabulary scatter. Two nodes reach `systematic` on 100 rows;
do not over-read that, the paper and half thresholds cannot be exercised at
this sample size. No prose-vs-unit cross-check was added: a real one needs the
prose->dimension mapping that just failed.

The fixtures were the root cause, so they carry the warning. `make_term`'s
docstring now states that `raw_unit` feeds the dimension and that putting a
unit token in `dimension_text` is the mistake that hid this bug from 159 green
tests. 31 call sites moved to `raw_unit=`. Three regression tests: the probe's
exact shape (prose `electrochemical potential` + `V` resolves), prose alone
never inventing a dimension, and an over-merge guard (mass/time/area vs
substance/time/area yield rates stay two nodes).

**Blocker 2 — measurand granularity.** 166 distinct measurand strings over 204
rows. Five spellings of Faradaic efficiency (`Faradaic efficiency for NH3
production`, `NH3 Faradaic efficiency`, `Faradaic efficiency toward NH3`, …),
five of NH3 yield rate, and some fold a condition or the normalisation basis
into the name (`applied electrode potential at which the yield rate and
Faradaic efficiency were measured`, `rate of NH3 production normalized to
electrode area (yield)`). `alias_key` is lexical and cannot merge prose
variants: **0 merge suggestions**. Only the terse `applied electrode potential`
clustered (17). Fix is in `build_prompt`: demand a short canonical noun phrase,
state that conditions/basis/reference belong in their own fields, and show the
shape by example without naming candidate measurands.

**Stability 0.046 is a real reading, not small-sample noise.** An earlier note
in this session predicted the probe's number should be discounted for sample
size. That was wrong: with 166 distinct strings over 204 rows almost no key
appears in both halves, so the metric is correctly reporting a vocabulary that
does not reproduce. Sample size makes it somewhat worse; the defect is the
signal.

**Blocker 3 — non-measurands promoted to measurands.** Miller indices
(`(111)`, `surface orientation`), composition subscripts, and a `compositional
index X labeling the PtBi-Co alloy nanoplate sample` arrive as measurands.
These are subject/facet labels. Stage 1's boundary rule cannot catch a bare
`(111)` — nothing precedes the digit. Needs either a stage-1 facet pattern or
a stage-2 instruction to return no row for a label.

**Cost fact that gates the full run.** 33.2 s per call (66 calls / 2189 s), of
which 2032 s is *user CPU* — the time is burned locally, not waiting on a
remote API, which suggests a subprocess transport rather than an API call.
`discover` is a plain sequential `for` loop, so the full 1231 calls extrapolate
to **11.3 hours**. Even with blockers 1-3 fixed the full run needs concurrency;
confirm the transport first.

**Order to fix:** blocker 1 (one call site + an honest fixture), then the
prompt for 2 and 3, then re-probe the same 100 rows — the default salt is
derived from campaign + snapshot sha, so the A/B split and the row slice are
identical and the comparison is like-for-like. Decide concurrency last.

**Blocker 2 is FIXED in the prompt (2026-09-29), unmeasured until a re-probe.**
`build_prompt` now constrains the *shape* of a measurand without naming one:
a six-word cap (`_MEASURAND_MAX_WORDS`), an explicit per-field redirect table
(conditions / basis / reference state / convention / subject label), an
enumerated ban on the exact constructions the probe produced (parentheses,
`at which …`, `of the … that …`, `normalized to …`, a clause naming which
compared case this is), a rule for differences (`change in X` + the two cases
in `required_conditions`), and one worked example. The example is thermal
transport — a field this campaign does not cover — so that showing the shape
cannot seed the vocabulary; `test_build_prompt_has_no_candidate_measurand_menu`
still passes unchanged. The species stays in the name deliberately: FE toward
NH3 and FE toward H2 are different numbers and the compare identity has no
species field, so the species is part of *which* quantity this is.

`parse_response` gained two warnings that are **not** failures, worded so a
run's warning list distinguishes a working prompt from a broken one: a null
measurand carrying a `skip_reason` reads `declined as not a measurand (…)`,
and an over-cap measurand is **kept** with `over the 6-word cap — kept: '…'`.
Kept, not dropped: the warning exists to count prompt non-compliance across a
run, and dropping the row would destroy the string needed to fix the prompt.
`dimension_text` is still asked for, now labelled in the prompt as audit-only
prose with the unit named as what actually gets parsed — it has no consumer
after blocker 1, but a future cross-check of prose-vs-unit needs it recorded.

Prompt cost roughly tripled: 1236 → 3569 chars (~900 input tokens/call), so
the full 1231-call run carries ~700k extra input tokens. Sonnet input at that
volume is not the binding cost — the 11.3 h wall clock is. Every call's prompt
shares a long identical prefix, so prompt caching is worth checking when the
concurrency work happens.

**Blocker 3 is BIGGER than recorded, and splits in two.** Counted from the
probe's `mentions.jsonl`: of 204 `value` mentions, **41 carry no unit at all**.
The stage-2 decline instruction above covers only part of that, and after
blocker 1 every unit-less mention resolves to `dimension=None` — so this is now
the dominant remaining failure mode, promoted to first-order by the very fix
that made stage 3 correct.

- **3a — Miller indices are a stage-1 false positive**, ~19 of the 41.
  `Pd(111)`, `Ag/Cu(111)`, `Mo₂C(0001)`, `Ni₂P(001)`, `CuPd(100)`. The census
  boundary rule (`text[start - 1].isalnum()`, `census.py` `scan_text`) already
  rejects `SnCu111` and `Ti3C2` correctly — it fails here only because the
  character before the digits is `(`, not a letter. Candidate rule, still
  domain-neutral: *a parenthesised group whose entire content is a digit run of
  length ≥3 with no decimal separator and no resolved unit is an identifier,
  not a value.* That also rejects a bare `(100) facets` (no element prefix to
  key on) and a sociology corpus's `(2019)`. Free to fix and free to validate —
  stage 1 is deterministic — and it **reduces** the paid call count, so it
  should land before the re-probe, not after.
- **3b — stranded units, previously unrecorded.** `the potential required to
  reach HER current densities of 10 and 30 mA cm⁻²` gives `10` no unit, because
  the unit sits after the second number. Same for a `−0.26` limiting potential.
  These are real measurements that now become dimensionless nodes of their own
  — worse than a label, because a label is obviously junk and this is not.
  Needs a shared-unit rule in `_resolve_unit` (a number followed by `and`/`,`
  + another number + a unit borrows that unit). Design work, not a one-liner.

Also in the 41, correctly and needing no fix: `1.23×`/`16.8×` relative yield
ratios and `pH 7.0` are genuinely dimensionless quantities. `m/z 329` is a real
mass-to-charge value; letting the model decline it is acceptable. `2` from
`2D Cu/Fe MOF` and `3` from `3d-metal` are tokenisation noise the
already-correct boundary rule misses for the same reason as 3a (`2D` — digit
*before* the letter).

## Resume (2026-09-29)

State: main holds `bfc0c03c` (blocker 1 fix) and `8e037c01` (blocker 2 prompt
rewrite). `bfc0c03c` has a real full-suite verdict — it was inside the green
gate at `f8f884d1` (23732 passed / 77 skipped / 6 xfailed). `8e037c01` has no
gate verdict: it qlanded, green locally with 178 taxonomy tests + mypy clean,
which is not a gate result. Prod runs `b81bf3cc`; `8e037c01` is not deployed.

Detail on all of the following is in "First discovery probe — 2026-09-29"
above; this section is only the order.

1. **Blocker 3a — Miller indices.** Fix in `census.py::scan_text`
   (domain-neutral rule: a parenthesised group whose entire content is a
   digit run of length ≥3, no decimal separator, no resolved unit ⇒
   identifier, not a value). Free to fix and free to validate (stage 1 is
   deterministic), and it reduces the paid call count — lands before the
   re-probe.
2. **Re-probe the same 100 rows — paid, ~66 calls, ~36 min.** Needs Reto's
   explicit go-ahead first. `precis taxonomy-bootstrap --campaign
   norr-her-meta --stage all --limit 100` (verify flags against
   `src/precis/cli/taxonomy.py` — `--stage` defaults to `census`, which is
   free; `all` is the paid path). Default salt derives from campaign +
   snapshot sha, so the row slice and A/B split are identical to the first
   probe — like for like. The number that matters: A/B vocabulary stability,
   0.046 before, threshold 0.80; `freeze_run` refuses to write a list below
   it. This is the only measurement of whether blocker 2's prompt rewrite
   moved the number.
3. **Blocker 3b — stranded units.** Design work, not a one-liner: a
   shared-unit rule in `census._resolve_unit`.
4. **Concurrency in `discover()`.** Plain sequential loop, 33.2 s/call ⇒
   11.3 h for 1231 calls. First confirm the transport — 2032 s of 2189 s was
   user CPU, which suggests a subprocess rather than an API call. The prompt
   shares a long identical prefix across calls; check prompt caching.
5. **Full run** ⇒ `list.v1.yaml` ⇒ compare against the seven-entry baseline
   in `norr-her-meta.md` step 2 ⇒ 20 papers (~12 expt / ~8 DFT, paired by
   catalyst family) ⇒ quantbind round ⇒ triple count + gold set (Reto
   adjudicates) ⇒ one figure.

**Zero-cost tool:** `replay_stage3.py` (session scratch, `norr-her-meta/`
under the Claude projects dir; being transferred from another machine)
replays stages 3-4 over the saved `discovered.jsonl`. It is how blocker 1 was
validated against real model output. Use it before paying for anything.

Two spec decisions owed by Reto — both already live as rows in the decisions
log below (`domain_classes` element-symbol ids, and the `meta.axis`
vocabulary); the first is due before the first promotion run.

**Do not:** re-run the paid probe without Reto's go-ahead; trust a fixture
over the saved probe output (the fixtures encoded the wrong belief and 159
green tests missed blocker 1); treat green-before-landing as a gate result.

## Open questions / decisions log

- **[decided 2026-09-28]** Sign off thresholds and procedure, never the
  list. Defaults above adopted on Reto's "continue".
- **[decided 2026-09-28]** Promotion is automatic; human = veto (demote,
  never-merge pin). Rationale: growth at extraction speed; `systematic`
  claims almost nothing so it is safe to automate.
- **[decided 2026-09-28, Reto]** No grandfathering of the 55 legacy `core`
  registry rows — this pass is the only path to `systematic` (see
  term-taxonomy decisions log).
- **[decided 2026-09-28]** Module is `src/precis/taxonomy/` — the pipeline
  that *builds* a taxonomy, next to term-taxonomy's `taxon` node door
  (`handlers/taxon.py`, `store/_taxon_ops.py`) without overlapping it. Stage
  1's entry point is `scan_mentions()`, not `census()`, so it cannot be
  misread as the same call as measures-substrate's `measures_census(key)`.
- **[decided 2026-09-28]** The tokenizer is **not** shared with
  `measures_census(key)`. That is a SQL count over already-landed `measures`
  rows grouped by tier/reference; stage 1 is a text scan over chunk text
  emitting mentions. They share the word and nothing else. If a dependency
  appears it runs the other way — `measures_census` could consume stage-1
  mentions to compute the escape rate.
- **[decided 2026-09-28]** The library is built **before** term-taxonomy
  ships. Stages 1-5 and the axes are pure functions over text and labels
  whose output is files under the campaign scratch dir; the `taxon`/`measures`
  writers are a thin adapter added when those doors land. Blocked-by stays
  because nothing is *promoted* until then.
- **[decided 2026-09-28]** Stage 2 runs on `Tier.BIG`, not `Tier.MEDIUM`.
  The router's tiers are capability names, not model names:
  `utils/llm/router.py` documents `BIG` as sonnet-class and `MEDIUM` as
  haiku-class. Reading `MEDIUM` as "the middle model" would have silently put
  discovery on the Haiku rung the survey already measured at ~50% wrong.
- **[decided 2026-09-28]** The package builds its **own** `pint.UnitRegistry`
  rather than reusing `precis.utils.units._registry()`. That singleton is
  shared with cad/structsolve; `registry.define()` mutates it process-wide, so
  loading a campaign's `wt%` or `dec` onto it would leak a campaign's
  vocabulary into unrelated call sites.
- **[decided 2026-09-28]** The computed axes are flat modules
  (`composition.py`, `periodic.py`, `termination.py`, `subjects.py`), not a
  `taxonomy/axes/` package: `src/precis/data/axes/` already means the paper
  auto-tagging vocabulary, and two unrelated things called "axes" in one tree
  is a reading trap.
- **[corrected 2026-09-28]** Cubic facet families have 6 / 8 / 12 members for
  `{100}` / `{111}` / `{110}`. An earlier note in this campaign put `{110}` at
  24, which double-counts: the six coordinate permutations of an already
  mixed-sign index like `(1,-1,0)` collapse onto the same twelve vectors. The
  implementation enumerates permutations x signs and dedupes, so the number is
  derived rather than asserted.
- **[found + fixed 2026-09-28]** The usage test counts **papers**, and the v1
  hub snapshot could not supply them. v1 carried only `n_supporters`, which a
  prod probe showed counts `supports` edges from **4 `quest` refs** — not
  papers at all. The hub-to-paper relation is `corroborates` (1707 edges, 614
  papers) plus `establishes` (30 edges, 9 papers), with the paper as
  `src_ref_id`. Snapshot v2 (`hubs.v2.jsonl`, sha256 `ec77e84f…`, 1664 rows,
  **620 distinct papers, 0 hubs without one**, median ~3 hubs per paper) adds
  `paper_ref_ids`. Counting hub ids as a proxy would have inflated independence
  ~2.7x, so a term appearing in three hubs of one paper would have promoted to
  `systematic` on a single source — defeating the one thing the flag asserts.
- **[found + fixed 2026-09-28]** Hub counts must be keyed by a node's full
  compare identity, not by its `key`. Keying by `key` alone gives every
  reference-state or convention split variant the *union* count, so a
  `vs Ag/AgCl` potential with 20 hubs clears the >=30 threshold on the
  `vs RHE` variant's evidence — which silently undoes the split that stage 3
  just made.
- **[open]** Campaign `domain_classes` ids that are bare element symbols
  (`pd`, `cu`) duplicate nodes the composition/periodic axes already generate,
  so matching a domain class by id puts a spurious `material-class` edge on
  every Pd- or Cu-containing formula. v1 works around it by matching domain
  classes on `label` and leaving the id path to the tag reader. The real fix is
  to drop element-symbol ids from the campaign config and have the tag reader
  resolve `catalyst:pd` to the element node — decide before the first
  promotion run, since it changes which node the mentions attach to.
- **[open]** Axis vocabulary for `meta.axis` — unvalidated in term-taxonomy
  v1 on purpose; this item's first discovery run decides which axes are real.
