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
6. Subject axes: `PdCoP` → {Pd, Co, P, ternary-alloy}; `Pd(111)` → composition
   Pd AND termination fcc-111 (two parents, two axes); `Ce0.5Zr0.5O2` parses;
   widening from `Pd(111)` along composition does not return `Cu(111)`.
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
- **[open]** Module name and whether the census tokenizer is shared with
  `measures_census(key)` in measures-substrate (same read, different input).
- **[open]** Axis vocabulary for `meta.axis` — unvalidated in term-taxonomy
  v1 on purpose; this item's first discovery run decides which axes are real.
