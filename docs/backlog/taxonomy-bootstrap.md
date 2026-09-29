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
